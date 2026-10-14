#!/usr/bin/env python3
"""Generate free local music, ambience and resumable scene SFX cues."""

from __future__ import annotations

import hashlib
import json
import math
import random
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT = ROOT / "output"

MUSIC_PATH = OUTPUT / "music" / "background_music.wav"
AMBIENT_PATH = OUTPUT / "sound" / "ambient_environment.wav"
SFX_PATH = OUTPUT / "sfx" / "transition_sfx.wav"

DESIGN_DIR = OUTPUT / "sound_design"
MANIFEST_PATH = DESIGN_DIR / "sound_design_manifest.json"
CUES_PATH = DESIGN_DIR / "sfx_cues.json"

VERSION = "free-sound-design-v4"
SAMPLE_RATE = 22050
BACKGROUND_SECONDS = 30
SFX_SECONDS = 1.6
BLOCK_SIZE = 8192
MIN_AUDIO_BYTES = 100


def log(*items):
    print(*items, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Missing input file: {INPUT_FILE}")

    values = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip("'\"")

    return values


def setting_bool(values, keys, default=True):
    for key in keys:
        if key not in values:
            continue
        value = values[key].strip().lower()
        if value in {"true", "yes", "on", "1", "enabled"}:
            return True
        if value in {"false", "no", "off", "0", "disabled"}:
            return False
        raise RuntimeError(f"{key} must be true or false")
    return default


def positive_int(values, key, default):
    try:
        value = int(values.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be a positive integer") from exc

    if value < 1:
        raise RuntimeError(f"{key} must be a positive integer")
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_signature(data):
    payload = json.dumps(
        data, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_wav(path, samples):
    """Write mono 16-bit PCM in blocks to avoid large temporary lists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with wave.open(str(temporary), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)

        block = bytearray()
        for sample in samples:
            value = max(-1.0, min(1.0, float(sample)))
            block.extend(struct.pack("<h", int(value * 32767)))

            if len(block) >= BLOCK_SIZE * 2:
                audio.writeframesraw(block)
                block.clear()

        if block:
            audio.writeframesraw(block)

    temporary.replace(path)


def validate_wav(path, minimum_seconds):
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1:
            raise RuntimeError(f"Unexpected channel count: {path}")
        if audio.getsampwidth() != 2:
            raise RuntimeError(f"Unexpected sample width: {path}")
        if audio.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"Unexpected sample rate: {path}")

        duration = audio.getnframes() / audio.getframerate()
        if duration < minimum_seconds:
            raise RuntimeError(f"Audio is too short: {path}")
        if path.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError(f"Audio file is too small: {path}")

        return duration


def music_samples():
    count = SAMPLE_RATE * BACKGROUND_SECONDS
    roots = (110.0, 130.81, 98.0, 146.83)
    intervals = (1.0, 1.25, 1.5, 2.0)

    for index in range(count):
        t = index / SAMPLE_RATE
        root = roots[int(t // 6) % len(roots)]
        fade_in = min(1.0, t / 2.5)
        fade_out = min(1.0, (BACKGROUND_SECONDS - t) / 2.0)
        envelope = max(0.0, min(fade_in, fade_out))
        swell = 0.78 + 0.22 * math.sin(2 * math.pi * t / 12.0)

        value = 0.0
        for interval in intervals:
            frequency = root * interval
            value += 0.22 * math.sin(2 * math.pi * frequency * t)
            value += 0.035 * math.sin(
                2 * math.pi * frequency * 2.003 * t
            )

        yield value * 0.22 * envelope * swell


def ambient_samples():
    count = SAMPLE_RATE * BACKGROUND_SECONDS
    rng = random.Random(271828)
    filtered_noise = 0.0

    for index in range(count):
        t = index / SAMPLE_RATE
        filtered_noise = (
            filtered_noise * 0.96
            + rng.uniform(-1.0, 1.0) * 0.04
        )

        low = math.sin(2 * math.pi * 55.0 * t)
        mid = math.sin(2 * math.pi * 82.41 * t + 0.4)
        modulation = 0.7 + 0.3 * math.sin(2 * math.pi * t / 9.0)
        fade = max(
            0.0,
            min(1.0, t / 1.5, (BACKGROUND_SECONDS - t) / 1.5),
        )

        value = low * 0.08 + mid * 0.04 + filtered_noise * 0.12
        yield value * modulation * fade * 0.25


def sfx_samples():
    count = int(SAMPLE_RATE * SFX_SECONDS)
    rng = random.Random(314159)
    filtered_noise = 0.0

    for index in range(count):
        progress = index / max(1, count - 1)
        t = index / SAMPLE_RATE

        filtered_noise = (
            filtered_noise * 0.72
            + rng.uniform(-1.0, 1.0) * 0.28
        )
        envelope = math.sin(math.pi * progress) ** 1.5
        pitch = 180.0 + 500.0 * progress
        tone = math.sin(2 * math.pi * pitch * t) * 0.18

        yield (filtered_noise * 0.10 + tone * 0.25) * envelope


def create_or_resume_track(category, path, generator, minimum_seconds):
    previous_manifest = read_json(MANIFEST_PATH, {})
    previous_tracks = {}

    if previous_manifest.get("version") == VERSION:
        previous_tracks = previous_manifest.get("tracks", {})

    track_signature = make_signature({
        "version": VERSION,
        "category": category,
        "sample_rate": SAMPLE_RATE,
        "duration": (
            SFX_SECONDS if category == "sfx" else BACKGROUND_SECONDS
        ),
    })
    previous = previous_tracks.get(category, {})

    if path.is_file():
        try:
            duration = validate_wav(path, minimum_seconds)
            digest = sha256_file(path)

            if (
                previous.get("signature") == track_signature
                and previous.get("sha256") == digest
            ):
                log("RESUME:", path.relative_to(ROOT))
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": track_signature,
                    "sha256": digest,
                    "duration_seconds": round(duration, 3),
                    "generation": "local_synthesis_no_external_api",
                }
        except (OSError, ValueError, wave.Error, RuntimeError) as exc:
            log("REBUILD:", path.name, str(exc))

    write_wav(path, generator())
    duration = validate_wav(path, minimum_seconds)
    digest = sha256_file(path)

    log("CREATED:", path.relative_to(ROOT))
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": track_signature,
        "sha256": digest,
        "duration_seconds": round(duration, 3),
        "generation": "local_synthesis_no_external_api",
    }


def remove_if_present(path):
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Cannot remove disabled track {path}: {exc}") from exc


def build_cues(parts, scenes, enabled):
    cues = []
    if not enabled:
        return cues

    sfx_relative = SFX_PATH.relative_to(ROOT).as_posix()

    # Avoid a whoosh at the very start of the entire video.
    for global_scene in range(2, parts * scenes + 1):
        part = (global_scene - 1) // scenes + 1
        local_scene = (global_scene - 1) % scenes + 1

        cues.append({
            "part": part,
            "scene": local_scene,
            "time": 0.08,
            "file": sfx_relative,
            "gain": 0.075,
            "kind": "generic_transition_whoosh",
            "timing_basis": "scene_start",
        })

    return cues


def main():
    values = read_settings()
    parts = positive_int(values, "PARTS", 1)
    scenes = positive_int(values, "SCENES", 11)

    enabled = {
        "music": setting_bool(
            values,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
             "SOUNDTRACK_ENABLED"),
        ),
        "ambient": setting_bool(
            values,
            ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
             "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED"),
        ),
        "sfx": setting_bool(
            values,
            ("SFX_ENABLED", "SFX", "SOUND_EFFECTS",
             "SOUND_EFFECTS_ENABLED"),
        ),
    }

    for path in (
        MUSIC_PATH, AMBIENT_PATH, SFX_PATH, MANIFEST_PATH, CUES_PATH
    ):
        path.parent.mkdir(parents=True, exist_ok=True)

    previous_manifest = read_json(MANIFEST_PATH, {})
    previous_tracks = (
        previous_manifest.get("tracks", {})
        if previous_manifest.get("version") == VERSION
        else {}
    )

    definitions = {
        "music": (MUSIC_PATH, music_samples, BACKGROUND_SECONDS),
        "ambient": (AMBIENT_PATH, ambient_samples, BACKGROUND_SECONDS),
        "sfx": (SFX_PATH, sfx_samples, SFX_SECONDS),
    }

    tracks = {}
    for category, (path, generator, minimum_seconds) in definitions.items():
        if not enabled[category]:
            remove_if_present(path)
            tracks[category] = {
                "status": "disabled",
                "path": None,
                "generation": "not_generated",
            }
            continue

        tracks[category] = create_or_resume_track(
            category, path, generator, minimum_seconds
        )

    cues = build_cues(parts, scenes, enabled["sfx"])

    cue_data = {
        "status": "completed",
        "version": VERSION,
        "parts": parts,
        "scenes_per_part": scenes,
        "cue_count": len(cues),
        "cue_policy": (
            "One generic transition whoosh at 0.08 seconds into each "
            "scene except the first scene of the entire video. "
            "Not story-event-aware."
        ),
        "external_api_calls": 0,
        "paid_services_used": False,
        "cues": cues,
    }
    write_json(CUES_PATH, cue_data)

    manifest = {
        "status": "completed",
        "version": VERSION,
        "generation": "local_synthesis_no_external_api",
        "external_api_calls": 0,
        "paid_services_used": False,
        "sample_rate": SAMPLE_RATE,
        "background_duration_seconds": BACKGROUND_SECONDS,
        "sfx_duration_seconds": SFX_SECONDS,
        "parts": parts,
        "scenes_per_part": scenes,
        "enabled": enabled,
        "tracks": tracks,
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
    }
    write_json(MANIFEST_PATH, manifest)

    log("SOUND DESIGN COMPLETE")
    for category in ("music", "ambient", "sfx"):
        log(f"{category.upper()}: {tracks[category]['status']}")
    log("SFX CUES:", len(cues))
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)