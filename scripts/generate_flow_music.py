#!/usr/bin/env python3
"""Story-aware Google Lyria music generation with verified timing and resume."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
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
CHECKPOINT = ROOT / "output" / "checkpoints" / "flow_music_progress.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "lyria-3.5"
VERSION = "photo-motion-lyria-music-v10-role-aware-verified"
REQUEST_TIMEOUT = (30, 900)
RETRY_DELAYS = (8, 20)
MAX_PROMPT_CHARS = 6500
MIN_AUDIO_BYTES = 10000
MIN_AUDIO_DURATION = 10.0
DURATION_TOLERANCE = 0.5

AUDIO_PATH_KEYS = (
    "output", "audio_path", "audio_file", "path", "file", "audio"
)
COMPLETED_STATUSES = {"completed", "reused"}
ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}


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
    raise RuntimeError(f"Invalid boolean setting: {value!r}")


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
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
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        result[key.strip().upper()] = value.strip()
    return result


def setting(config, names, default=None):
    for name in names:
        if name in config:
            return config[name]
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
        raise RuntimeError("Audio path escapes the project directory.")
    return path


def run(command, timeout=120):
    result = subprocess.run(
        command, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-1800:]
        raise RuntimeError(f"Command failed: {detail}")
    return result


def probe_audio(path, minimum=0.01):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"Audio file is missing: {path}")

    result = run([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ])
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            row.get("codec_type") == "audio"
            for row in data.get("streams", [])
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid audio metadata: {path}") from exc

    if not math.isfinite(duration) or duration < minimum or not has_audio:
        raise RuntimeError(f"Audio is empty or invalid: {path}")
    return duration


def read_story():
    story = read_json(STORY_FILE)
    if not story:
        raise RuntimeError("output/story/story.json is missing or invalid.")

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
        raise RuntimeError("Story title, text and summary are all empty.")

    return title or "Untitled story", story_text, summary, story


def read_narration_jobs():
    manifest = read_json(AUDIO_JOBS)
    if manifest.get("status") != "completed":
        raise RuntimeError("TTS audio_jobs.json is not completed.")

    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("TTS audio_jobs.json contains no jobs.")

    rows = []
    seen_ids = set()

    for row in jobs:
        if not isinstance(row, dict):
            raise RuntimeError("TTS manifest contains an invalid job.")

        if row.get("status") not in COMPLETED_STATUSES:
            raise RuntimeError(
                f"Incomplete TTS job: {row.get('scene_id', 'unknown')}"
            )

        scene_id = clean(row.get("scene_id") or row.get("job_id"))
        if not scene_id:
            raise RuntimeError("TTS job has no scene_id.")
        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate TTS scene_id: {scene_id}")
        seen_ids.add(scene_id)

        role = clean(row.get("role") or "main").lower()
        if role not in ROLE_ORDER:
            raise RuntimeError(f"Unsupported narration role: {role}")

        try:
            part = int(row["part"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid part for {scene_id}") from exc
        if part < 1:
            raise RuntimeError(f"Invalid part number for {scene_id}")

        if role == "main":
            try:
                scene = int(row["scene"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid Main scene number: {scene_id}") from exc
            if scene < 1:
                raise RuntimeError(f"Invalid Main scene number: {scene_id}")
        else:
            scene = None

        audio_value = next(
            (
                row.get(key) for key in AUDIO_PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        if not audio_value:
            raise RuntimeError(f"Audio path missing for {scene_id}")

        path = resolve_project_path(audio_value)
        if not path.is_file():
            raise RuntimeError(f"Audio file missing for {scene_id}: {path}")

        actual_duration = probe_audio(path)
        checksum = sha256_file(path)

        if row.get("sha256") != checksum:
            raise RuntimeError(f"TTS SHA256 mismatch: {scene_id}")

        declared = row.get(
            "actual_audio_duration_seconds",
            row.get("duration"),
        )
        try:
            declared = float(declared)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Actual measured duration missing: {scene_id}"
            ) from exc

        if not math.isfinite(declared) or declared <= 0:
            raise RuntimeError(f"Invalid declared duration: {scene_id}")
        if abs(declared - actual_duration) > 0.20:
            raise RuntimeError(
                f"Duration mismatch for {scene_id}: "
                f"manifest={declared:.3f}, ffprobe={actual_duration:.3f}"
            )

        duration_source = (
            row.get("duration_source")
            or row.get("audio_duration_source")
        )
        if duration_source != "ffprobe_measured":
            raise RuntimeError(
                f"Duration is not ffprobe-measured: {scene_id}"
            )

        rows.append({
            "scene_id": scene_id,
            "part": part,
            "scene": scene,
            "role": role,
            "duration_seconds": actual_duration,
            "audio_path": path.relative_to(ROOT).as_posix(),
            "audio_sha256": checksum,
            "text": clean(row.get("text") or row.get("narration")),
        })

    rows.sort(key=lambda row: (
        row["part"],
        ROLE_ORDER[row["role"]],
        row["scene"] or 0,
        row["scene_id"],
    ))
    return rows


def validate_role_structure(rows, config):
    if not rows:
        raise RuntimeError("No completed narration audio records.")

    parts = sorted({row["part"] for row in rows})
    if not parts or parts != list(range(parts[0], parts[-1] + 1)):
        raise RuntimeError("Narration part numbering is not continuous.")

    scenes_value = setting(config, ("SCENES",), "1")
    try:
        scenes_per_part = int(str(scenes_value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SCENES must be a positive integer.") from exc
    if scenes_per_part < 1:
        raise RuntimeError("SCENES must be a positive integer.")

    by_part = {}
    for row in rows:
        by_part.setdefault(row["part"], []).append(row)

    final_part = parts[-1]
    for part in parts:
        group = by_part[part]
        roles = [row["role"] for row in group]

        if roles.count("hook") != 1:
            raise RuntimeError(f"Part {part} must contain exactly one Hook.")
        if roles.count("main") < 1:
            raise RuntimeError(f"Part {part} contains no Main narration.")
        if roles.count("suspense") != (1 if part != final_part else 0):
            raise RuntimeError(f"Part {part} has an invalid Suspense count.")
        if roles.count("resolution") != (1 if part == final_part else 0):
            raise RuntimeError(f"Part {part} has an invalid Resolution count.")

        main_scenes = sorted(
            row["scene"] for row in group if row["role"] == "main"
        )
        if main_scenes != list(range(1, scenes_per_part + 1)):
            raise RuntimeError(
                f"Part {part} Main scene grid mismatch: {main_scenes}"
            )

    expected_total = (
        len(parts) * scenes_per_part
        + len(parts)
        + max(0, len(parts) - 1)
        + 1
    )
    if len(rows) != expected_total:
        raise RuntimeError(
            f"Narration record count mismatch: "
            f"expected {expected_total}, found {len(rows)}"
        )

    return {
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "main_count": len(parts) * scenes_per_part,
        "hook_count": len(parts),
        "suspense_count": max(0, len(parts) - 1),
        "resolution_count": 1,
    }


def story_context(story_text, summary):
    context = summary
    if story_text:
        context = (context + " Story details: " + story_text).strip()
    if not context:
        context = story_text

    context = clean(context)
    if len(context) <= MAX_PROMPT_CHARS:
        return context

    available = MAX_PROMPT_CHARS - 500
    beginning_size = int(available * 0.42)
    ending_size = int(available * 0.30)
    middle_size = available - beginning_size - ending_size
    middle_start = max(0, (len(context) - middle_size) // 2)

    return clean(
        context[:beginning_size]
        + " [middle story events] "
        + context[middle_start:middle_start + middle_size]
        + " [complete ending and resolution context] "
        + context[-ending_size:]
    )


def role_context(rows):
    sections = []
    for role in ("hook", "main", "suspense", "resolution"):
        selected = [
            row for row in rows
            if row["role"] == role and row["text"]
        ]
        if not selected:
            continue
        sample = " ".join(row["text"] for row in selected[:8])
        sections.append(f"{role.upper()} narration: {sample[:1000]}")
    return "\n".join(sections)


def build_prompt(title, story_text, summary, rows, config):
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"
    style = clean(config.get("MUSIC_STYLE", "cinematic")) or "cinematic"
    genre = clean(config.get("MUSIC_GENRE", "original film score")) or "original film score"
    intensity = clean(config.get("MUSIC_INTENSITY", "evolving")) or "evolving"

    return (
        "Compose an original instrumental cinematic score for a Hindi "
        "storytelling video. Do not copy, sample, quote, or imitate any "
        "existing song, soundtrack, artist, or copyrighted composition. "
        f"Story title: {title}. "
        f"Music style: {style}. Genre: {genre}. Mood: {mood}. "
        f"Intensity: {intensity}. "
        "Understand the full story, its beginning, middle, climax, and "
        "ending before choosing the music. "
        "The score must fit the actual characters, setting, events, and "
        "emotional progression. Start restrained under the Hook, maintain "
        "a coherent cinematic motif through Main narration, build controlled "
        "tension under Suspense sections, and reach a natural emotional "
        "resolution for the final ending. "
        "Do not create separate unrelated musical identities between parts. "
        "Keep the midrange clear for Hindi speech; avoid vocals, lyrics, "
        "spoken words, sudden loud peaks, excessive bass, and distracting "
        "percussion. Use smooth transitions and a loopable musical phrase. "
        "Story summary and complete context: "
        + story_context(story_text, summary)
        + "\nNarration role context:\n"
        + role_context(rows)
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
    if not isinstance(response_data, dict):
        raise RuntimeError("Google API returned a non-object response.")

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
                candidates.extend(
                    block for block in content
                    if isinstance(block, dict)
                    and block.get("type") in {"audio", "output_audio"}
                )

    for block in candidates:
        audio = decode_audio_block(block)
        if audio and len(audio) >= MIN_AUDIO_BYTES:
            return audio

    raise RuntimeError("Google response contains no valid audio data block.")


def save_source_audio(audio_bytes):
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SOURCE_AUDIO.with_name(SOURCE_AUDIO.name + ".tmp")

    if len(audio_bytes) < MIN_AUDIO_BYTES:
        raise RuntimeError("Music provider returned an unusually small file.")

    try:
        temporary.write_bytes(audio_bytes)
        probe_audio(temporary, MIN_AUDIO_DURATION)
        temporary.replace(SOURCE_AUDIO)
    finally:
        temporary.unlink(missing_ok=True)

    return probe_audio(SOURCE_AUDIO, MIN_AUDIO_DURATION)


def request_music(api_key, model, prompt):
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {"model": model, "input": prompt}
    last_error = "Unknown Google Lyria API error."

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = requests.post(
                API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in {408, 429, 500, 502, 503, 504}:
                last_error = (
                    f"Temporary Google API error {response.status_code}: "
                    f"{response.text[:800]}"
                )
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                break

            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google API rejected request "
                    f"({response.status_code}): {response.text[:1200]}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("Google API returned invalid JSON.") from exc

            audio_bytes = extract_audio(data)
            return save_source_audio(audio_bytes)

        except requests.Timeout as exc:
            last_error = f"Google music request timed out: {exc}"
        except requests.ConnectionError as exc:
            last_error = f"Google music connection failed: {exc}"
        except requests.RequestException as exc:
            raise RuntimeError(f"Google music request failed: {exc}") from exc

        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])

    raise RuntimeError(last_error)


def prepare_final_music(source, destination, target_duration):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to prepare background music.")
    if target_duration <= 0 or not math.isfinite(target_duration):
        raise RuntimeError("Invalid target music duration.")

    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise RuntimeError("Generated source music is missing.")

    source_duration = probe_audio(source, MIN_AUDIO_DURATION)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)

    fade_duration = min(5.0, max(1.0, target_duration * 0.04))
    fade_start = max(0.0, target_duration - fade_duration)

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1", "-i", str(source),
        "-t", f"{target_duration:.3f}", "-vn",
        "-af",
        (
            "aresample=44100,"
            "loudnorm=I=-24:TP=-2.0:LRA=11,"
            f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}"
        ),
        "-c:a", "libmp3lame", "-b:a", "192k",
        "-ar", "44100", "-ac", "2",
        "-map_metadata", "-1", str(temporary),
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=900, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                "FFmpeg could not prepare background music: "
                + result.stderr[-1500:]
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError("Prepared music file is missing or too small.")

        actual_duration = probe_audio(temporary, MIN_AUDIO_DURATION)
        if abs(actual_duration - target_duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Prepared music duration mismatch: "
                f"{actual_duration:.3f}s != {target_duration:.3f}s"
            )

        temporary.replace(destination)
        return {
            "duration_seconds": actual_duration,
            "source_duration_seconds": source_duration,
            "size_bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }
    finally:
        temporary.unlink(missing_ok=True)


def build_source_fingerprint(
    title, story_text, summary, rows, config, model, target_duration, prompt
):
    return fingerprint({
        "version": VERSION,
        "provider": "google_lyria",
        "model": model,
        "title": title,
        "story_text": story_text,
        "summary": summary,
        "narration_records": [
            {
                "scene_id": row["scene_id"],
                "part": row["part"],
                "scene": row["scene"],
                "role": row["role"],
                "duration_seconds": round(row["duration_seconds"], 4),
                "audio_sha256": row["audio_sha256"],
            }
            for row in rows
        ],
        "music_settings": {
            key: config.get(key)
            for key in (
                "MUSIC", "MUSIC_ENABLED", "MUSIC_STYLE",
                "MUSIC_GENRE", "MUSIC_INTENSITY", "MOOD",
                "FLOW_MUSIC_MODEL", "MUSIC_ALLOW_BILLABLE",
            )
        },
        "target_duration_seconds": round(target_duration, 3),
        "prompt": prompt,
    })


def validate_model(config):
    model = clean(
        config.get("FLOW_MUSIC_MODEL")
        or os.environ.get("FLOW_MUSIC_MODEL")
        or DEFAULT_MODEL
    )
    if not model or len(model) > 200 or any(ch.isspace() for ch in model):
        raise RuntimeError("FLOW_MUSIC_MODEL is invalid.")
    return model


def existing_track_is_valid(source_signature, target_duration):
    previous = read_json(MANIFEST)
    if previous.get("status") != "completed":
        return False
    if previous.get("version") != VERSION:
        return False
    if previous.get("source_fingerprint") != source_signature:
        return False
    if previous.get("provider") != "google_lyria":
        return False
    if not FINAL_AUDIO.is_file():
        return False

    try:
        expected_path = FINAL_AUDIO.relative_to(ROOT).as_posix()
        if previous.get("path") != expected_path:
            return False

        actual_duration = probe_audio(FINAL_AUDIO, MIN_AUDIO_DURATION)
        actual_hash = sha256_file(FINAL_AUDIO)

        if actual_hash != previous.get("sha256"):
            return False
        if abs(actual_duration - target_duration) > DURATION_TOLERANCE:
            return False

        declared = float(previous.get("duration_seconds", 0))
        if abs(declared - actual_duration) > DURATION_TOLERANCE:
            return False

        log("RESUME: verified existing Lyria music reused.")
        log("DURATION:", round(actual_duration, 2), "seconds")
        log("SHA256:", actual_hash)
        return True
    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def write_unavailable(reason, model=None, target_duration=None, source_signature=None):
    record = {
        "status": "unavailable",
        "provider": "google_lyria",
        "product": "Google Lyria",
        "model": model or DEFAULT_MODEL,
        "reason": str(reason)[:2000],
        "fallback": "sound_design_local_fallback",
        "paid_services_used": False,
        "external_api_calls": 0,
        "version": VERSION,
    }
    if target_duration is not None:
        record["required_duration_seconds"] = round(float(target_duration), 3)
    if source_signature:
        record["source_fingerprint"] = source_signature

    write_json(MANIFEST, record)
    write_json(CHECKPOINT, {
        "status": "unavailable",
        "version": VERSION,
        "reason": str(reason)[:2000],
    })


def write_disabled():
    record = {
        "status": "disabled",
        "provider": "google_lyria",
        "reason": "Background music is disabled in Input/topic.txt.",
        "paid_services_used": False,
        "external_api_calls": 0,
        "version": VERSION,
    }
    write_json(MANIFEST, record)
    write_json(CHECKPOINT, {
        "status": "disabled",
        "version": VERSION,
    })


def get_api_key():
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def main():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    config = read_settings()

    enabled = truthy(
        setting(config, ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC"), "true"),
        True,
    )
    if not enabled:
        write_disabled()
        log("MUSIC DISABLED; no API request made.")
        return 0

    model = validate_model(config)

    global_billable = truthy(
        os.environ.get("ALLOW_BILLABLE_PROVIDERS", "true"), True
    )
    music_billable = truthy(
        config.get("MUSIC_ALLOW_BILLABLE", "true"), True
    )
    if not global_billable or not music_billable:
        write_unavailable(
            "Billable music generation is disabled by configuration.",
            model=model,
        )
        log("LYRIA SKIPPED: billable permission is disabled.")
        return 0

    api_key = get_api_key()
    if not api_key:
        write_unavailable(
            "GEMINI_API_KEY and GOOGLE_API_KEY are not configured.",
            model=model,
        )
        log("LYRIA UNAVAILABLE: Google API key is missing.")
        return 0

    target_duration = None
    source_signature = None
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("ffmpeg and ffprobe are required.")

        title, story_text, summary, story = read_story()
        rows = read_narration_jobs()
        role_summary = validate_role_structure(rows, config)

        # Sum each completed narration record exactly once.
        narration_duration = sum(row["duration_seconds"] for row in rows)
        if not math.isfinite(narration_duration) or narration_duration <= 0:
            raise RuntimeError("Measured narration duration is invalid.")
        target_duration = round(narration_duration + 3.0, 3)

        prompt = build_prompt(
            title, story_text, summary, rows, config
        )
        source_signature = build_source_fingerprint(
            title, story_text, summary, rows,
            config, model, target_duration, prompt,
        )

        # Reuse happens before any external API request.
        if existing_track_is_valid(source_signature, target_duration):
            write_json(CHECKPOINT, {
                "status": "completed",
                "version": VERSION,
                "source_fingerprint": source_signature,
                "reused": True,
                "api_calls": 0,
                "updated_at": time.time(),
            })
            return 0

        write_json(CHECKPOINT, {
            "status": "in_progress",
            "version": VERSION,
            "source_fingerprint": source_signature,
            "target_duration_seconds": target_duration,
            "narration_records": len(rows),
            "role_summary": role_summary,
            "api_calls_started": 1,
        })

        log("=" * 60)
        log("GOOGLE LYRIA MUSIC GENERATION")
        log("VERSION:", VERSION)
        log("MODEL:", model)
        log("STORY:", title)
        log("PARTS:", len(role_summary["parts"]))
        log("NARRATION RECORDS:", len(rows))
        log("MEASURED NARRATION:", round(narration_duration, 2), "seconds")
        log("REQUIRED MUSIC:", target_duration, "seconds")
        log("ROLE COUNTS:", role_summary)
        log("BILLABLE PROVIDER:", True)
        log("=" * 60)

        source_duration = request_music(api_key, model, prompt)
        prepared = prepare_final_music(
            SOURCE_AUDIO, FINAL_AUDIO, target_duration
        )

        checksum = sha256_file(FINAL_AUDIO)
        if checksum != prepared["sha256"]:
            raise RuntimeError("Final music SHA256 changed after preparation.")

        verified_duration = probe_audio(FINAL_AUDIO, MIN_AUDIO_DURATION)
        if abs(verified_duration - target_duration) > DURATION_TOLERANCE:
            raise RuntimeError("Final music duration failed verification.")

        source_checksum = sha256_file(SOURCE_AUDIO)
        record = {
            "status": "completed",
            "provider": "google_lyria",
            "product": "Google Lyria",
            "model": model,
            "generation_method": "google_interactions_api",
            "source_audio": SOURCE_AUDIO.relative_to(ROOT).as_posix(),
            "path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "audio_path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "duration_seconds": round(verified_duration, 3),
            "actual_audio_duration_seconds": round(verified_duration, 3),
            "required_duration_seconds": target_duration,
            "source_duration_seconds": round(source_duration, 3),
            "sha256": checksum,
            "source_sha256": source_checksum,
            "source_fingerprint": source_signature,
            "instrumental_only": True,
            "lyrics_requested": False,
            "looped_to_narration_duration": target_duration > source_duration + 0.5,
            "fade_out_enabled": True,
            "narration_duration_source": "sum_of_ffprobe_measured_tts_audio_files",
            "narration_records": len(rows),
            "role_summary": role_summary,
            "story_aware_prompt": True,
            "story_ending_considered": True,
            "external_api_calls": 1,
            "paid_services_used": True,
            "size_bytes": FINAL_AUDIO.stat().st_size,
            "version": VERSION,
        }

        write_json(MANIFEST, record)
        published = read_json(MANIFEST)
        if (
            published.get("status") != "completed"
            or published.get("sha256") != checksum
            or published.get("source_fingerprint") != source_signature
            or published.get("path") != FINAL_AUDIO.relative_to(ROOT).as_posix()
        ):
            raise RuntimeError("Published Flow Music manifest failed verification.")

        write_json(CHECKPOINT, {
            "status": "completed",
            "version": VERSION,
            "source_fingerprint": source_signature,
            "sha256": checksum,
            "duration_seconds": round(verified_duration, 3),
            "target_duration_seconds": target_duration,
            "api_calls": 1,
            "paid_services_used": True,
            "updated_at": time.time(),
        })

        log("=" * 60)
        log("LYRIA MUSIC READY.")
        log("MODEL:", model)
        log("SOURCE DURATION:", round(source_duration, 2), "seconds")
        log("FINAL DURATION:", round(verified_duration, 2), "seconds")
        log("API CALLS: 1")
        log("PAID SERVICES USED: True")
        log("SHA256:", checksum)
        log("OUTPUT:", FINAL_AUDIO.relative_to(ROOT))
        log("MANIFEST:", MANIFEST.relative_to(ROOT))
        log("=" * 60)
        return 0

    except KeyboardInterrupt:
        write_json(CHECKPOINT, {
            "status": "interrupted",
            "version": VERSION,
            "source_fingerprint": source_signature,
            "updated_at": time.time(),
        })
        raise

    except Exception as exc:
        write_unavailable(
            reason=exc,
            model=model,
            target_duration=target_duration,
            source_signature=source_signature,
        )
        log("LYRIA MUSIC UNAVAILABLE.")
        log("SOUND DESIGN LOCAL FALLBACK MAY BE USED.")
        log("REASON:", str(exc)[:1500])
        log("NO FALSE COMPLETION RECORDED.")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)