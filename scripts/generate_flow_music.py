#!/usr/bin/env python3
"""Story-aware Google Lyria music generation, validation and resume."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
AUDIO_JOBS = ROOT / "output" / "narration" / "audio_jobs.json"

AUDIO_DIR = ROOT / "output" / "music"
SOURCE_AUDIO = AUDIO_DIR / "flow_music_source.audio"
FINAL_AUDIO = AUDIO_DIR / "background_music.mp3"
MANIFEST = AUDIO_DIR / "flow_music_manifest.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

DEFAULT_MODEL = "lyria-3.5"
REQUEST_TIMEOUT = (30, 900)
RETRY_DELAYS = (8, 20)
MAX_PROMPT_CHARS = 6500
MIN_AUDIO_BYTES = 10000
MIN_AUDIO_DURATION = 10.0
DURATION_TOLERANCE = 0.5
VERSION = "photo-motion-lyria-music-v3"

AUDIO_PATH_KEYS = (
    "output",
    "audio_path",
    "audio_file",
    "path",
    "file",
    "audio",
)

COMPLETED_STATUSES = {"completed", "reused"}


def log(*values):
    print(*values, flush=True)


def clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def truthy(value, default=False):
    if value is None:
        return default

    value = str(value).strip().lower()

    if value in {"1", "true", "yes", "on", "enabled"}:
        return True

    if value in {"0", "false", "no", "off", "disabled"}:
        return False

    raise RuntimeError(f"Invalid boolean value: {value!r}")


def read_json(path):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as stream:
        for block in iter(
            lambda: stream.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def fingerprint(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")

    return hashlib.sha256(raw).hexdigest()


def read_settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    result = {}

    for raw in INPUT_FILE.read_text(
        encoding="utf-8-sig"
    ).splitlines():

        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        result[key.strip().upper()] = value.strip()

    return result


def setting(config, keys, default=None):
    for key in keys:
        if key in config:
            return config[key]
    return default


def resolve_project_path(value):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Audio manifest contains an empty path.")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    root = ROOT.resolve()

    if path != root and root not in path.parents:
        raise RuntimeError(
            "Audio path escapes the project directory."
        )

    return path


def run(command, timeout=90):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            "Command failed: "
            + (result.stderr or result.stdout)[-1500:]
        )

    return result


def probe_audio(path):
    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    path = Path(path)

    if not path.is_file():
        raise RuntimeError(f"Audio file does not exist: {path}")

    result = run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of", "json",
            str(path),
        ],
        timeout=90,
    )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])

        has_audio = any(
            item.get("codec_type") == "audio"
            for item in data.get("streams", [])
        )

    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            "Could not read audio metadata."
        ) from exc

    if (
        not math.isfinite(duration)
        or duration < MIN_AUDIO_DURATION
        or not has_audio
    ):
        raise RuntimeError(
            "Audio is empty, invalid or too short."
        )

    return duration


def probe_narration_file(path):
    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        raise RuntimeError(
            "ffprobe is required to measure narration."
        )

    if not path.is_file():
        return 0.0

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode:
        return 0.0

    try:
        duration = float(result.stdout.strip())

        if math.isfinite(duration) and duration > 0:
            return duration

    except (TypeError, ValueError):
        pass

    return 0.0


def load_target_duration():
    """Measure actual completed narration audio, not estimates."""

    manifest = read_json(AUDIO_JOBS)

    if manifest.get("status") != "completed":
        raise RuntimeError(
            "Narration audio_jobs.json is not completed."
        )

    jobs = manifest.get("jobs")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "Narration audio_jobs.json contains no jobs."
        )

    total = 0.0
    seen_ids = set()
    measured = 0

    for row in jobs:

        if not isinstance(row, dict):
            continue

        status = str(
            row.get("status", "")
        ).strip().lower()

        if status not in COMPLETED_STATUSES:
            continue

        identity = clean(
            row.get("scene_id")
            or row.get("job_id")
            or ""
        )

        if identity:
            if identity in seen_ids:
                raise RuntimeError(
                    f"Duplicate narration job: {identity}"
                )
            seen_ids.add(identity)

        audio_value = next(
            (
                row.get(key)
                for key in AUDIO_PATH_KEYS
                if isinstance(row.get(key), str)
                and row[key].strip()
            ),
            None,
        )

        if not audio_value:
            raise RuntimeError(
                f"Narration audio path missing: {identity or 'unknown'}"
            )

        audio_path = resolve_project_path(audio_value)

        if not audio_path.is_file():
            raise RuntimeError(
                f"Narration audio is missing: {audio_value}"
            )

        duration = probe_narration_file(audio_path)

        if duration <= 0:
            raise RuntimeError(
                f"Could not measure narration: {audio_value}"
            )

        declared = row.get(
            "actual_audio_duration_seconds",
            row.get("duration"),
        )

        if declared is not None:
            try:
                declared = float(declared)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid duration for {identity}."
                ) from exc

            if (
                not math.isfinite(declared)
                or declared <= 0
                or abs(declared - duration) > 0.20
            ):
                raise RuntimeError(
                    f"Narration duration mismatch: {identity}"
                )

        total += duration
        measured += 1

    if measured == 0 or total <= 0:
        raise RuntimeError(
            "No valid completed narration audio was measured."
        )

    return round(total + 3.0, 3)


def read_story():
    story = read_json(STORY_FILE)

    if not story:
        raise RuntimeError(
            "output/story/story.json is missing or invalid."
        )

    title = clean(
        story.get("title")
        or story.get("final_title")
        or story.get("generated_title")
        or story.get("topic")
    )

    story_text = clean(
        story.get("story_text")
        or story.get("final_story")
        or story.get("source_text")
        or story.get("text")
        or story.get("content")
    )

    summary = clean(
        story.get("summary")
        or story.get("story_summary")
        or story.get("synopsis")
        or story.get("description")
    )

    if not title and not story_text and not summary:
        raise RuntimeError(
            "Story title, summary and story text are empty."
        )

    if not title:
        title = "Untitled story"

    return title, story_text, summary, story


def story_context(story_text, summary):
    """Preserve story beginning, middle and ending in the music prompt."""

    if summary:
        context = summary

        if story_text:
            context += " Story details: " + story_text

    else:
        context = story_text

    context = clean(context)

    if len(context) <= MAX_PROMPT_CHARS:
        return context

    remaining = MAX_PROMPT_CHARS - 1000

    beginning_size = int(remaining * 0.45)
    ending_size = int(remaining * 0.30)
    middle_size = remaining - beginning_size - ending_size

    beginning = context[:beginning_size]
    middle_start = max(
        0,
        (len(context) - middle_size) // 2,
    )
    middle = context[
        middle_start:middle_start + middle_size
    ]

    ending = context[-ending_size:]

    return clean(
        beginning
        + " [middle story context] "
        + middle
        + " [story ending and resolution] "
        + ending
    )


def build_prompt(title, story_text, summary, config):

    mood = clean(
        config.get("MOOD", "dramatic")
    ) or "dramatic"

    style = clean(
        config.get("MUSIC_STYLE", "cinematic")
    ) or "cinematic"

    genre = clean(
        config.get("MUSIC_GENRE", "original film score")
    ) or "original film score"

    intensity = clean(
        config.get("MUSIC_INTENSITY", "evolving")
    ) or "evolving"

    context = story_context(story_text, summary)

    return (
        "Compose an original instrumental cinematic score "
        "for a Hindi storytelling video. "
        "Do not reproduce, sample or imitate any existing "
        "song, film soundtrack, artist or copyrighted composition. "

        f"Story title: {title}. "
        f"Music style: {style}. "
        f"Genre direction: {genre}. "
        f"Emotional mood: {mood}. "
        f"Intensity: {intensity}. "

        "Read the entire supplied story context, including "
        "its ending, before choosing the musical direction. "

        "Use original musical motifs appropriate to the actual "
        "characters, setting, events and emotional progression. "

        "Begin with restrained instrumentation so narration "
        "is immediately understandable. "

        "Develop the music gradually. "
        "Use controlled tension during suspenseful passages, "
        "emotional depth during important revelations, "
        "and a natural musical resolution matching the story ending. "

        "Keep the midrange uncluttered for Hindi speech. "
        "Avoid vocals, singing, lyrics, spoken words, "
        "sudden loud peaks, excessive bass and distracting percussion. "

        "Maintain consistent musical identity throughout. "
        "Create a cohesive instrumental passage with a smooth "
        "loop point and no abrupt ending. "

        "Story context: "
        + context
    )


def decode_audio_block(block):
    if not isinstance(block, dict):
        return None

    encoded = block.get("data")

    if not isinstance(encoded, str) or not encoded:
        return None

    try:
        return base64.b64decode(
            encoded,
            validate=True,
        )
    except (ValueError, TypeError):
        return None


def extract_audio(response_data):
    """Extract valid audio from supported Interactions response shapes."""

    if not isinstance(response_data, dict):
        raise RuntimeError(
            "Google API returned a non-object response."
        )

    candidates = []

    output_audio = response_data.get("output_audio")

    if isinstance(output_audio, dict):
        candidates.append(output_audio)

    for key in ("outputs", "output", "steps"):

        items = response_data.get(key, [])

        if isinstance(items, dict):
            items = [items]

        if not isinstance(items, list):
            continue

        for item in items:

            if not isinstance(item, dict):
                continue

            if item.get("type") in {
                "audio",
                "output_audio",
            }:
                candidates.append(item)

            content = item.get("content", [])

            if isinstance(content, dict):
                content = [content]

            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") in {
                            "audio",
                            "output_audio",
                        }
                    ):
                        candidates.append(block)

    for block in candidates:

        audio = decode_audio_block(block)

        if audio and len(audio) >= MIN_AUDIO_BYTES:
            return audio

    raise RuntimeError(
        "Google response did not contain a valid audio data block."
    )


def save_source_audio(audio_bytes):

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    temporary = SOURCE_AUDIO.with_name(
        "flow_music_source.tmp"
    )

    if len(audio_bytes) < MIN_AUDIO_BYTES:
        raise RuntimeError(
            "Music provider returned an unusually small file."
        )

    try:
        temporary.write_bytes(audio_bytes)
        probe_audio(temporary)
        temporary.replace(SOURCE_AUDIO)

    finally:
        temporary.unlink(missing_ok=True)

    return probe_audio(SOURCE_AUDIO)


def request_music(api_key, model, prompt):

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    payload = {
        "model": model,
        "input": prompt,
    }

    last_error = "Unknown Lyria API error."

    for attempt in range(len(RETRY_DELAYS) + 1):

        response = None

        try:
            response = requests.post(
                API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in {
                408, 429, 500, 502, 503, 504
            }:

                last_error = (
                    f"Temporary Google API error "
                    f"{response.status_code}: "
                    f"{response.text[:800]}"
                )

                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue

                break

            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google API rejected request "
                    f"({response.status_code}): "
                    f"{response.text[:1200]}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "Google API returned invalid JSON."
                ) from exc

            if not isinstance(data, dict):
                raise RuntimeError(
                    "Google API returned an invalid response."
                )

            audio_bytes = extract_audio(data)

            return save_source_audio(audio_bytes)

        except requests.Timeout as exc:

            last_error = (
                f"Google music request timed out: {exc}"
            )

            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue

        except requests.ConnectionError as exc:

            last_error = (
                f"Google music connection failed: {exc}"
            )

            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue

        except requests.RequestException as exc:

            last_error = (
                f"Google music API request failed: {exc}"
            )
            break

        except (ValueError, TypeError, RuntimeError) as exc:

            last_error = str(exc)
            break

    raise RuntimeError(last_error)

def prepare_final_music(source, destination, target_duration):

    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is required to prepare background music."
        )

    if target_duration <= 0 or not math.isfinite(target_duration):
        raise RuntimeError(
            "Invalid target music duration."
        )

    source = Path(source)
    destination = Path(destination)

    if not source.is_file():
        raise RuntimeError(
            "Generated source music is missing."
        )

    source_duration = probe_audio(source)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = destination.with_name(
        destination.stem + ".building.mp3"
    )

    temporary.unlink(missing_ok=True)

    fade_duration = min(
        5.0,
        max(1.0, target_duration * 0.04),
    )

    fade_start = max(
        0.0,
        target_duration - fade_duration,
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",

        "-stream_loop", "-1",
        "-i", str(source),

        "-t", f"{target_duration:.3f}",
        "-vn",

        "-af",
        (
            "aresample=44100,"
            "loudnorm=I=-24:TP=-2.0:LRA=11,"
            f"afade=t=out:st={fade_start:.3f}:"
            f"d={fade_duration:.3f}"
        ),

        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",

        "-map_metadata", "-1",
        str(temporary),
    ]

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )

        if result.returncode:
            raise RuntimeError(
                "FFmpeg could not prepare background music: "
                + result.stderr[-1500:]
            )

        if not temporary.is_file():
            raise RuntimeError(
                "FFmpeg did not create the output music file."
            )

        if temporary.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError(
                "Prepared music file is unexpectedly small."
            )

        actual_duration = probe_audio(temporary)

        if abs(actual_duration - target_duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                "Prepared music duration mismatch: "
                f"actual={actual_duration:.3f}s, "
                f"required={target_duration:.3f}s."
            )

        temporary.replace(destination)

        return {
            "duration": actual_duration,
            "source_duration": source_duration,
            "size_bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }

    finally:
        temporary.unlink(missing_ok=True)


def existing_track_is_valid(
    source_fingerprint,
    target_duration,
):
    """Reuse only a completed, matching and checksum-verified track."""

    previous = read_json(MANIFEST)

    if previous.get("status") != "completed":
        return False

    if previous.get("version") != VERSION:
        return False

    if previous.get("source_fingerprint") != source_fingerprint:
        return False

    if previous.get("provider") != "google_lyria":
        return False

    if not FINAL_AUDIO.is_file():
        return False

    try:

        expected_path = FINAL_AUDIO.relative_to(
            ROOT
        ).as_posix()

        if previous.get("path") != expected_path:
            return False

        actual_duration = probe_audio(FINAL_AUDIO)
        actual_hash = sha256_file(FINAL_AUDIO)

        if actual_hash != previous.get("sha256"):
            return False

        if abs(
            actual_duration - target_duration
        ) > DURATION_TOLERANCE:
            return False

        declared_duration = float(
            previous.get("duration_seconds", 0)
        )

        if abs(
            declared_duration - actual_duration
        ) > DURATION_TOLERANCE:
            return False

        log("RESUME: verified existing Lyria music reused.")
        log("Duration:", round(actual_duration, 2), "seconds")
        log("SHA256:", actual_hash)

        return True

    except Exception as exc:

        log(
            "Existing music failed validation; "
            "regeneration required:",
            str(exc),
        )

        return False


def write_unavailable(
    reason,
    model=None,
    target_duration=None,
    source_fingerprint=None,
):
    record = {
        "status": "unavailable",
        "provider": "google_lyria",
        "product": "Google Lyria",
        "model": model or DEFAULT_MODEL,
        "reason": str(reason)[:2000],
        "fallback": "local_procedural_music",
        "paid_services_used": False,
        "external_api_calls": 0,
        "version": VERSION,
    }

    if target_duration is not None:
        record["required_duration_seconds"] = round(
            float(target_duration), 3
        )

    if source_fingerprint:
        record["source_fingerprint"] = source_fingerprint

    write_json(MANIFEST, record)


def write_disabled():

    write_json(
        MANIFEST,
        {
            "status": "disabled",
            "provider": "google_lyria",
            "reason": (
                "Background music is disabled in Input/topic.txt."
            ),
            "paid_services_used": False,
            "external_api_calls": 0,
            "version": VERSION,
        },
    )


def get_api_key():

    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def build_source_fingerprint(
    title,
    story_text,
    summary,
    config,
    model,
    target_duration,
    prompt,
):
    return fingerprint({
        "version": VERSION,
        "provider": "google_lyria",
        "model": model,
        "title": title,
        "story_text": story_text,
        "summary": summary,
        "mood": config.get("MOOD", "dramatic"),
        "music_style": config.get(
            "MUSIC_STYLE", "cinematic"
        ),
        "music_genre": config.get(
            "MUSIC_GENRE", "original film score"
        ),
        "music_intensity": config.get(
            "MUSIC_INTENSITY", "evolving"
        ),
        "target_duration": round(target_duration, 3),
        "prompt": prompt,
    })


def validate_model(model):

    model = clean(model)

    if not model:
        raise RuntimeError(
            "FLOW_MUSIC_MODEL is empty."
        )

    if len(model) > 200:
        raise RuntimeError(
            "FLOW_MUSIC_MODEL is unexpectedly long."
        )

    if any(char.isspace() for char in model):
        raise RuntimeError(
            "FLOW_MUSIC_MODEL contains invalid whitespace."
        )

    return model


def main():

    AUDIO_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    config = read_settings()

    music_enabled = truthy(
        setting(
            config,
            (
                "MUSIC_ENABLED",
                "MUSIC",
                "BACKGROUND_MUSIC",
            ),
            "true",
        ),
        True,
    )

    if not music_enabled:

        write_disabled()

        log("MUSIC DISABLED.")
        return 0

    model = validate_model(
        setting(
            config,
            ("FLOW_MUSIC_MODEL",),
            DEFAULT_MODEL,
        )
        or DEFAULT_MODEL
    )

    # Billable execution is permitted by default.
    # Explicit false remains an intentional user restriction.
    global_billable = truthy(
        os.environ.get(
            "ALLOW_BILLABLE_PROVIDERS",
            "true",
        ),
        True,
    )

    music_billable = truthy(
        config.get(
            "MUSIC_ALLOW_BILLABLE",
            "true",
        ),
        True,
    )

    if not global_billable or not music_billable:

        write_unavailable(
            "Billable music generation is disabled by configuration.",
            model=model,
        )

        log(
            "LYRIA SKIPPED: billable music permission is disabled."
        )

        return 0

    api_key = get_api_key()

    if not api_key:

        write_unavailable(
            "GEMINI_API_KEY and GOOGLE_API_KEY are not configured.",
            model=model,
        )

        log(
            "LYRIA UNAVAILABLE: Google API key is missing."
        )

        return 0

    target_duration = None
    source_fingerprint = None

    try:

        if not shutil.which("ffmpeg"):
            raise RuntimeError(
                "ffmpeg is not installed."
            )

        if not shutil.which("ffprobe"):
            raise RuntimeError(
                "ffprobe is not installed."
            )

        title, story_text, summary, story = read_story()

        target_duration = load_target_duration()

        prompt = build_prompt(
            title,
            story_text,
            summary,
            config,
        )

        source_fingerprint = build_source_fingerprint(
            title,
            story_text,
            summary,
            config,
            model,
            target_duration,
            prompt,
        )

        # Reuse must happen before any external API request.
        if existing_track_is_valid(
            source_fingerprint,
            target_duration,
        ):
            return 0

        log("=" * 60)
        log("GOOGLE LYRIA MUSIC GENERATION")
        log("VERSION:", VERSION)
        log("MODEL:", model)
        log("STORY:", title)
        log(
            "REQUIRED DURATION:",
            round(target_duration, 2),
            "seconds",
        )
        log("STORY-AWARE PROMPT:", True)
        log("INSTRUMENTAL MODE:", True)
        log("BILLABLE PROVIDER:", True)
        log("=" * 60)

        # Generate only when no valid reusable output exists.
        source_duration = request_music(
            api_key,
            model,
            prompt,
        )

        if source_duration < MIN_AUDIO_DURATION:
            raise RuntimeError(
                "Lyria source music duration is invalid."
            )

        prepared = prepare_final_music(
            SOURCE_AUDIO,
            FINAL_AUDIO,
            target_duration,
        )

        checksum = sha256_file(FINAL_AUDIO)

        if checksum != prepared["sha256"]:
            raise RuntimeError(
                "Final music checksum changed after preparation."
            )

        # Verify final file independently before publishing manifest.
        verified_duration = probe_audio(FINAL_AUDIO)

        if abs(
            verified_duration - target_duration
        ) > DURATION_TOLERANCE:
            raise RuntimeError(
                "Final music failed independent duration verification."
            )

        record = {
            "status": "completed",
            "provider": "google_lyria",
            "product": "Google Lyria",
            "model": model,
            "generation_method": "google_interactions_api",

            "source_audio": (
                SOURCE_AUDIO.relative_to(ROOT).as_posix()
            ),

            "path": (
                FINAL_AUDIO.relative_to(ROOT).as_posix()
            ),

            "source_duration_seconds": round(
                source_duration, 3
            ),

            "duration_seconds": round(
                verified_duration, 3
            ),

            "required_duration_seconds": round(
                target_duration, 3
            ),

            "sha256": checksum,

            "source_sha256": sha256_file(
                SOURCE_AUDIO
            ),

            "source_fingerprint": source_fingerprint,

            "instrumental_only": True,
            "lyrics_requested": False,

            "looped_to_narration_duration": (
                target_duration > source_duration + 0.5
            ),

            "fade_out_enabled": True,
            "narration_duration_source": (
                "measured_completed_tts_audio_files"
            ),

            "story_aware_prompt": True,
            "story_ending_considered": True,

            "external_api_calls": 1,
            "paid_services_used": True,

            "size_bytes": FINAL_AUDIO.stat().st_size,
            "version": VERSION,
        }

        # Publish completion only after all checks pass.
        write_json(
            MANIFEST,
            record,
        )

        # Read back the published manifest.
        published = read_json(MANIFEST)

        if (
            published.get("status") != "completed"
            or published.get("sha256") != checksum
            or published.get("source_fingerprint")
            != source_fingerprint
        ):
            raise RuntimeError(
                "Published music manifest verification failed."
            )

        log("=" * 60)
        log("LYRIA MUSIC READY.")
        log("MODEL:", model)
        log(
            "SOURCE DURATION:",
            round(source_duration, 2),
            "seconds",
        )
        log(
            "FINAL DURATION:",
            round(verified_duration, 2),
            "seconds",
        )
        log("API CALLS: 1")
        log("PAID SERVICES USED: True")
        log("CHECKSUM:", checksum)
        log("OUTPUT:", FINAL_AUDIO.relative_to(ROOT))
        log("MANIFEST:", MANIFEST.relative_to(ROOT))
        log("=" * 60)

        return 0

    except KeyboardInterrupt:
        raise

    except Exception as exc:

        write_unavailable(
            reason=exc,
            model=model,
            target_duration=target_duration,
            source_fingerprint=source_fingerprint,
        )

        log("=" * 60)
        log("LYRIA MUSIC UNAVAILABLE.")
        log("LOCAL FALLBACK WILL BE USED.")
        log("REASON:", str(exc)[:1500])
        log("NO FALSE COMPLETION RECORDED.")
        log("=" * 60)

        return 0


if __name__ == "__main__":

    try:
        raise SystemExit(main())

    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)