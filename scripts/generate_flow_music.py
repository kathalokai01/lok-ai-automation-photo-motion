#!/usr/bin/env python3
"""Generate story-aware instrumental background music with Google's Lyria API."""

from __future__ import annotations

import base64
import hashlib
import json
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
SOURCE_AUDIO = AUDIO_DIR / "flow_music_source.mp3"
FINAL_AUDIO = AUDIO_DIR / "background_music.mp3"
MANIFEST = AUDIO_DIR / "flow_music_manifest.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "lyria-3.5"
MAX_PROMPT_CHARS = 5000
REQUEST_TIMEOUT = (30, 900)
TARGET_GENERATION_SECONDS = 170.0
RETRY_DELAYS = (8, 20)


def log(*values):
    print(*values, flush=True)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
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
    result = {}
    if not INPUT_FILE.is_file():
        return result

    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip()

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                value = value[1:-1]

        result[key.strip().upper()] = value

    return result


def truthy(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {
        "true", "yes", "1", "on", "enabled"
    }


def clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def load_story():
    story = read_json(STORY_FILE)
    if not story:
        raise RuntimeError("Completed story.json is missing or invalid.")

    title = clean(
        story.get("title")
        or story.get("final_title")
        or story.get("topic")
    )
    story_text = clean(
        story.get("story_text")
        or story.get("source_text")
    )

    if not title and not story_text:
        raise RuntimeError("Story title and story text are both empty.")

    return title, story_text, story


def load_target_duration():
    path = ROOT / "output" / "sound_design" / "sound_design_manifest.json"
    existing = read_json(path)
    try:
        duration = float(existing.get("total_narration_seconds", 0))
    except (TypeError, ValueError):
        duration = 0.0

    if duration > 0:
        return duration + 8.0

    audio_manifest = read_json(
        ROOT / "output" / "narration" / "audio_jobs.json"
    )
    jobs = audio_manifest.get("jobs", [])
    if not isinstance(jobs, list):
        jobs = []

    total = 0.0
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for music duration.")

    for row in jobs:
        if not isinstance(row, dict) or row.get("status") != "completed":
            continue
        value = row.get("output") or row.get("audio_path") or row.get("path")
        if not isinstance(value, str) or not value.strip():
            continue

        audio_path = Path(value)
        if not audio_path.is_absolute():
            audio_path = ROOT / audio_path

        if not audio_path.is_file():
            continue

        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            try:
                total += max(0.0, float(result.stdout.strip()))
            except ValueError:
                pass

    if total <= 0:
        raise RuntimeError("Could not determine total narration duration.")

    return total + 8.0


def build_prompt(title, story_text, config):
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"
    style = clean(config.get("MUSIC_STYLE", "cinematic")) or "cinematic"

    story_context = story_text[:MAX_PROMPT_CHARS]
    if len(story_text) > MAX_PROMPT_CHARS:
        story_context += " [story continues]"

    return (
        "Create an original, high-quality, instrumental-only cinematic "
        "background score for a Hindi story video. "
        f"Story title: {title}. "
        f"Requested music style: {style}. "
        f"Emotional mood: {mood}. "
        "Use no singing, no lyrics, no spoken words and no vocals. "
        "The music must support narration rather than compete with it. "
        "Use a clear evolving arrangement with a memorable but subtle motif, "
        "cinematic depth, professional stereo production, and smooth musical "
        "transitions. Begin gently under narration, build tension where the "
        "story becomes suspenseful, create a strong emotional climax, then "
        "resolve naturally. Avoid sudden loud peaks and harsh high frequencies. "
        "Make approximately a 2 minute 50 second cohesive instrumental score "
        "that can be looped under a longer narration without an obvious ending. "
        "Story context: "
        + story_context
    )


def extract_audio(response_data):
    output_audio = response_data.get("output_audio")
    if isinstance(output_audio, dict):
        encoded = output_audio.get("data")
        if isinstance(encoded, str) and encoded:
            return base64.b64decode(encoded, validate=True)

    collected = []
    steps = response_data.get("steps", [])

    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("type") != "model_output":
                continue

            content = step.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "audio"
                    and isinstance(block.get("data"), str)
                    and block["data"]
                ):
                    collected.append(
                        base64.b64decode(block["data"], validate=True)
                    )

    if collected:
        return b"".join(collected)

    raise RuntimeError(
        "Lyria response did not contain a decodable audio block."
    )


def probe_audio(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            "Generated music is not a valid audio file: "
            + result.stderr[-1000:]
        )

    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    has_audio = any(
        item.get("codec_type") == "audio"
        for item in data.get("streams", [])
    )

    if duration < 10 or not has_audio:
        raise RuntimeError("Generated music is empty or too short.")

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

    last_error = None

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
                    f"Temporary Lyria API error {response.status_code}: "
                    f"{response.text[:1200]}"
                )
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue

            response.raise_for_status()
            data = response.json()
            audio_bytes = extract_audio(data)

            if len(audio_bytes) < 10000:
                raise RuntimeError("Lyria returned an unusually small audio file.")

            temporary = SOURCE_AUDIO.with_suffix(".mp3.tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(audio_bytes)
            temporary.replace(SOURCE_AUDIO)

            duration = probe_audio(SOURCE_AUDIO)
            return duration

        except requests.RequestException as exc:
            last_error = f"Lyria network/API request failed: {exc}"
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
        except (ValueError, KeyError, TypeError) as exc:
            last_error = f"Invalid Lyria response: {exc}"
            break

    raise RuntimeError(last_error or "Lyria music generation failed.")


def extend_music(source, destination, target_duration):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to prepare the music track.")

    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)

    fade_duration = min(5.0, max(1.0, target_duration * 0.04))
    fade_start = max(0.0, target_duration - fade_duration)

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1",
        "-i", str(source),
        "-t", f"{target_duration:.3f}",
        "-vn",
        "-af",
        (
            "aresample=44100,"
            "loudnorm=I=-22:TP=-2.0:LRA=11,"
            f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}"
        ),
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
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
            "Could not extend Lyria music: " + result.stderr[-1500:]
        )

    duration = probe_audio(temporary)
    if duration + 1.0 < target_duration:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "Prepared music is shorter than the required video duration."
        )

    temporary.replace(destination)
    return duration


def main():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    config = read_settings()

    if not truthy(config.get("MUSIC", "true"), True):
        write_json(MANIFEST, {
            "status": "disabled",
            "provider": "google_lyria",
            "reason": "MUSIC=false",
        })
        log("MUSIC DISABLED: skipping Lyria.")
        return 0

    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()

    if not api_key:
        write_json(MANIFEST, {
            "status": "unavailable",
            "provider": "google_lyria",
            "reason": "GEMINI_API_KEY and GOOGLE_API_KEY are not configured",
        })
        log("LYRIA UNAVAILABLE: no Google API key; local music fallback will run.")
        return 0

    try:
        title, story_text, story = load_story()
        target_duration = load_target_duration()
        model = clean(config.get("FLOW_MUSIC_MODEL", DEFAULT_MODEL)) or DEFAULT_MODEL
        prompt = build_prompt(title, story_text, config)

        source_fingerprint = fingerprint({
            "provider": "google_lyria",
            "model": model,
            "title": title,
            "story_text": story_text,
            "mood": config.get("MOOD", "dramatic"),
            "music_style": config.get("MUSIC_STYLE", "cinematic"),
            "target_duration": round(target_duration, 2),
        })

        previous = read_json(MANIFEST)
        if (
            previous.get("status") == "completed"
            and previous.get("source_fingerprint") == source_fingerprint
            and FINAL_AUDIO.is_file()
        ):
            try:
                existing_duration = probe_audio(FINAL_AUDIO)
                checksum = sha256_file(FINAL_AUDIO)
                if (
                    existing_duration + 1.0 >= target_duration
                    and previous.get("sha256") == checksum
                ):
                    log("LYRIA MUSIC RESUMED: reusing verified existing track.")
                    return 0
            except Exception:
                pass

        log("Generating story-aware music with:", model)
        source_duration = request_music(api_key, model, prompt)
        final_duration = extend_music(
            SOURCE_AUDIO,
            FINAL_AUDIO,
            target_duration,
        )

        record = {
            "status": "completed",
            "provider": "google_lyria",
            "product": "Google Flow Music / Gemini API",
            "model": model,
            "generation_method": "official_gemini_interactions_api",
            "source_audio": SOURCE_AUDIO.relative_to(ROOT).as_posix(),
            "path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "source_duration_seconds": round(source_duration, 3),
            "duration_seconds": round(final_duration, 3),
            "required_duration_seconds": round(target_duration, 3),
            "sha256": sha256_file(FINAL_AUDIO),
            "source_fingerprint": source_fingerprint,
            "instrumental_only": True,
            "lyrics_requested": False,
            "looped_to_narration_duration": final_duration > source_duration + 1,
            "external_api_calls": 1,
            "paid_services_used": True,
        }
        write_json(MANIFEST, record)

        log("LYRIA MUSIC READY")
        log("Model:", model)
        log("Generated source:", round(source_duration, 2), "seconds")
        log("Prepared music:", round(final_duration, 2), "seconds")
        log("Output:", FINAL_AUDIO.relative_to(ROOT))
        return 0

    except Exception as exc:
        write_json(MANIFEST, {
            "status": "unavailable",
            "provider": "google_lyria",
            "model": clean(config.get("FLOW_MUSIC_MODEL", DEFAULT_MODEL)),
            "reason": str(exc)[:2000],
            "fallback": "local_procedural_music",
        })
        log("LYRIA MUSIC FAILED; local music fallback will run:", exc)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)