#!/usr/bin/env python3
"""Generate and validate story-aware instrumental background music."""

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
VERSION = "photo-motion-lyria-music-v2"


def log(*values):
    print(*values, flush=True)


def clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def truthy(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    result = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                value = value[1:-1]

        result[key.strip().upper()] = value.strip()

    return result


def read_story():
    story = read_json(STORY_FILE)
    if not story:
        raise RuntimeError("output/story/story.json is missing or invalid.")

    title = clean(
        story.get("title")
        or story.get("final_title")
        or story.get("topic")
    )

    story_text = clean(
        story.get("story_text")
        or story.get("source_text")
        or story.get("text")
    )

    if not title and not story_text:
        raise RuntimeError("Story title and story text are empty.")

    return title or "Untitled story", story_text, story


def resolve_project_path(value):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Music manifest contains an empty audio path.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    root = ROOT.resolve()

    if path != root and root not in path.parents:
        raise RuntimeError("Audio path escapes the project directory.")

    return path


def probe_audio(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            "Invalid audio file: " + result.stderr[-1000:]
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            item.get("codec_type") == "audio"
            for item in data.get("streams", [])
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Could not read audio metadata.") from exc

    if not math.isfinite(duration) or duration < 10 or not has_audio:
        raise RuntimeError("Generated audio is empty or too short.")

    return duration


def probe_narration_file(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to measure narration.")

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
        return duration if math.isfinite(duration) and duration > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def load_target_duration():
    """Use actual generated TTS durations, never estimated narration timings."""
    manifest = read_json(
        ROOT / "output" / "narration" / "audio_jobs.json"
    )
    jobs = manifest.get("jobs", [])

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "Narration audio_jobs.json has no generated audio jobs."
        )

    total = 0.0
    seen = set()

    for row in jobs:
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).lower() not in {
            "completed", "reused"
        }:
            continue

        value = (
            row.get("output")
            or row.get("audio_path")
            or row.get("path")
            or row.get("file")
        )
        if not isinstance(value, str) or not value.strip():
            continue

        audio_path = resolve_project_path(value)
        if not audio_path.is_file():
            continue

        identity = str(audio_path)
        if identity in seen:
            continue
        seen.add(identity)

        duration = probe_narration_file(audio_path)
        if duration > 0:
            total += duration

    if total <= 0:
        raise RuntimeError(
            "Could not measure any completed narration audio."
        )

    # A short tail allows the final spoken sentence to finish naturally.
    return round(total + 3.0, 3)


def build_prompt(title, story_text, config):
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"
    style = clean(config.get("MUSIC_STYLE", "cinematic")) or "cinematic"
    genre = clean(config.get("MUSIC_GENRE", "")) or "original film score"
    intensity = clean(config.get("MUSIC_INTENSITY", "evolving")) or "evolving"

    context = story_text[:MAX_PROMPT_CHARS]
    if len(story_text) > MAX_PROMPT_CHARS:
        context += " [remaining story omitted]"

    return (
        "Compose an original instrumental cinematic score for a Hindi "
        "storytelling video. Do not reproduce or imitate any existing song, "
        "film soundtrack, artist or copyrighted composition. "
        f"Story title: {title}. "
        f"Music style: {style}. "
        f"Genre direction: {genre}. "
        f"Emotional mood: {mood}. "
        f"Intensity: {intensity}. "
        "Use instruments and motifs that fit the actual story. "
        "Start restrained so narration is immediately clear. "
        "Develop the music gradually with scene-appropriate tension, "
        "emotional movement and a natural resolution. "
        "Keep the midrange uncluttered for Hindi speech. "
        "Avoid vocals, singing, lyrics, spoken words, abrupt impacts, "
        "excessive bass and sudden loud peaks. "
        "Create a cohesive instrumental passage with a smooth loop point. "
        "Story context: " + context
    )


def decode_audio_block(block):
    if not isinstance(block, dict):
        return None

    encoded = block.get("data")
    if not isinstance(encoded, str) or not encoded:
        return None

    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None


def extract_audio(response_data):
    """Accept audio blocks returned in supported Interactions response shapes."""
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

            if item.get("type") in {"audio", "output_audio"}:
                candidates.append(item)

            content = item.get("content", [])
            if isinstance(content, dict):
                content = [content]

            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") in {
                            "audio", "output_audio"
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
    temporary = SOURCE_AUDIO.with_name("flow_music_source.tmp")

    if len(audio_bytes) < MIN_AUDIO_BYTES:
        raise RuntimeError("Music provider returned an unusually small file.")

    temporary.write_bytes(audio_bytes)
    temporary.replace(SOURCE_AUDIO)

    try:
        duration = probe_audio(SOURCE_AUDIO)
    except Exception:
        SOURCE_AUDIO.unlink(missing_ok=True)
        raise

    return duration


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
                    f"{response.status_code}: {response.text[:800]}"
                )
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue

            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise RuntimeError("Google API returned an invalid response.")

            audio_bytes = extract_audio(data)
            return save_source_audio(audio_bytes)

        except requests.RequestException as exc:
            last_error = f"Google music API request failed: {exc}"
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue

        except (ValueError, TypeError, RuntimeError) as exc:
            last_error = str(exc)
            break

    raise RuntimeError(last_error)

def prepare_final_music(source, destination, target_duration):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to prepare background music.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.stem + ".building.mp3"
    )
    temporary.unlink(missing_ok=True)

    fade_duration = min(5.0, max(1.0, target_duration * 0.04))
    fade_start = max(0.0, target_duration - fade_duration)

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
            f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}"
        ),
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-map_metadata", "-1",
        str(temporary),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    if result.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "FFmpeg could not prepare background music: "
            + result.stderr[-1200:]
        )

    try:
        actual_duration = probe_audio(temporary)
        if actual_duration + 0.5 < target_duration:
            raise RuntimeError(
                f"Prepared music is too short: {actual_duration:.2f}s; "
                f"required {target_duration:.2f}s."
            )

        if temporary.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError("Prepared music file is unexpectedly small.")

        temporary.replace(destination)
        return actual_duration

    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def existing_track_is_valid(source_fingerprint, target_duration):
    previous = read_json(MANIFEST)

    if previous.get("status") != "completed":
        return False

    if previous.get("source_fingerprint") != source_fingerprint:
        return False

    if not FINAL_AUDIO.is_file():
        return False

    try:
        duration = probe_audio(FINAL_AUDIO)
        checksum = sha256_file(FINAL_AUDIO)

        if duration + 0.5 < target_duration:
            return False

        if checksum != previous.get("sha256"):
            return False

        if previous.get("path") != FINAL_AUDIO.relative_to(ROOT).as_posix():
            return False

        log("RESUME: verified existing Lyria music reused.")
        log("Duration:", round(duration, 2), "seconds")
        return True

    except Exception as exc:
        log("Existing music failed validation; regenerating:", exc)
        return False


def write_unavailable(reason, model=None, target_duration=None):
    record = {
        "status": "unavailable",
        "provider": "google_lyria",
        "model": model or DEFAULT_MODEL,
        "reason": str(reason)[:2000],
        "fallback": "local_procedural_music",
        "paid_services_used": False,
        "version": VERSION,
    }

    if target_duration is not None:
        record["required_duration_seconds"] = round(
            float(target_duration), 3
        )

    write_json(MANIFEST, record)


def main():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    try:
        config = read_settings()
    except Exception as exc:
        write_unavailable(exc)
        log("MUSIC CONFIGURATION ERROR:", exc)
        return 0

    music_enabled = truthy(
        config.get(
            "MUSIC_ENABLED",
            config.get("MUSIC", config.get("BACKGROUND_MUSIC", "true")),
        ),
        True,
    )

    model = (
        clean(config.get("FLOW_MUSIC_MODEL", DEFAULT_MODEL))
        or DEFAULT_MODEL
    )

    if not music_enabled:
        write_json(MANIFEST, {
            "status": "disabled",
            "provider": "google_lyria",
            "reason": "Background music is disabled in Input/topic.txt.",
            "paid_services_used": False,
            "version": VERSION,
        })
        log("MUSIC DISABLED.")
        return 0

    global_billable = truthy(
        os.environ.get("ALLOW_BILLABLE_PROVIDERS", "false")
    )
    music_billable = truthy(
        config.get("MUSIC_ALLOW_BILLABLE", "true"),
        True,
    )

    if not global_billable or not music_billable:
        write_unavailable(
            "Billable music generation is disabled by configuration.",
            model,
        )
        log("LYRIA SKIPPED: billable music permission is disabled.")
        return 0

    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()

    if not api_key:
        write_unavailable(
            "GEMINI_API_KEY and GOOGLE_API_KEY are not configured.",
            model,
        )
        log("LYRIA UNAVAILABLE: Google API key is missing.")
        return 0

    try:
        title, story_text, story = read_story()
        target_duration = load_target_duration()
        prompt = build_prompt(title, story_text, config)

        source_fingerprint = fingerprint({
            "version": VERSION,
            "provider": "google_lyria",
            "model": model,
            "title": title,
            "story_text": story_text,
            "mood": config.get("MOOD", "dramatic"),
            "music_style": config.get("MUSIC_STYLE", "cinematic"),
            "music_genre": config.get("MUSIC_GENRE", ""),
            "music_intensity": config.get("MUSIC_INTENSITY", "evolving"),
            "target_duration": round(target_duration, 3),
            "prompt": prompt,
        })

        if existing_track_is_valid(
            source_fingerprint,
            target_duration,
        ):
            return 0

        log("Generating original instrumental music with Google Lyria.")
        log("Model:", model)
        log("Story:", title)
        log("Required duration:", round(target_duration, 2), "seconds")

        source_duration = request_music(
            api_key,
            model,
            prompt,
        )

        final_duration = prepare_final_music(
            SOURCE_AUDIO,
            FINAL_AUDIO,
            target_duration,
        )

        checksum = sha256_file(FINAL_AUDIO)

        record = {
            "status": "completed",
            "provider": "google_lyria",
            "product": "Google Lyria",
            "model": model,
            "generation_method": "google_interactions_api",
            "source_audio": SOURCE_AUDIO.relative_to(ROOT).as_posix(),
            "path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "source_duration_seconds": round(source_duration, 3),
            "duration_seconds": round(final_duration, 3),
            "required_duration_seconds": round(target_duration, 3),
            "sha256": checksum,
            "source_fingerprint": source_fingerprint,
            "instrumental_only": True,
            "lyrics_requested": False,
            "looped_to_narration_duration": (
                final_duration > source_duration + 1.0
            ),
            "external_api_calls": 1,
            "paid_services_used": True,
            "version": VERSION,
        }

        write_json(MANIFEST, record)

        log("LYRIA MUSIC READY.")
        log("Source duration:", round(source_duration, 2), "seconds")
        log("Final duration:", round(final_duration, 2), "seconds")
        log("Output:", FINAL_AUDIO.relative_to(ROOT))
        return 0

    except KeyboardInterrupt:
        raise

    except Exception as exc:
        write_unavailable(
            exc,
            model=model,
        )
        log("LYRIA MUSIC FAILED; local fallback will be used.")
        log("Reason:", str(exc)[:1200])
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)