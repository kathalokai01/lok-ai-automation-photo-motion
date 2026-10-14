#!/usr/bin/env python3
"""Generate resumable local background audio and timed SFX cues."""

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

MUSIC_DIR = OUTPUT / "music"
AMBIENT_DIR = OUTPUT / "sound"
SFX_DIR = OUTPUT / "sfx"
DESIGN_DIR = OUTPUT / "sound_design"

MANIFEST = DESIGN_DIR / "sound_design_manifest.json"
CUE_MANIFEST = DESIGN_DIR / "sfx_cues.json"

VERSION = "free-sound-design-v3"
SAMPLE_RATE = 22050
BACKGROUND_DURATION = 30
SFX_DURATION = 1.6


def log(*args):
    print(*args, flush=True)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


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


def as_bool(values, *keys, default=True):
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
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signature(data):
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_wav(path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")

    with wave.open(str(temp), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)

        block_size = 16384
        for start in range(0, len(samples), block_size):
            block = bytearray()

            for sample in samples[start:start + block_size]:
                value = max(-1.0, min(1.0, sample))
                block.extend(struct.pack("<h", int(value * 32767)))

            audio.writeframesraw(block)

    temp.replace(path)


def validate_wav(path, minimum_duration):
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1:
            raise RuntimeError(f"Unexpected channel count: {path}")
        if audio.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"Unexpected sample rate: {path}")

        duration = audio.getnframes() / audio.getframerate()
        if duration < minimum_duration:
            raise RuntimeError(f"Audio is too short: {path}")

        return duration


def make_music():
    count = SAMPLE_RATE * BACKGROUND_DURATION
    roots = (110.0, 130.81, 98.0, 146.83)
    intervals = (1.0, 1.25, 1.5, 2.0)
    samples = []

    for index in range(count):
        t = index / SAMPLE_RATE
        root = roots[int(t // 6) % len(roots)]

        fade_in = min(1.0, t / 2.5)
        fade_out = min(1.0, (BACKGROUND_DURATION - t) / 2.0)
        envelope = max(0.0, min(fade_in, fade_out))
        swell = 0.78 + 0.22 * math.sin(2 * math.pi * t / 12.0)

        value = 0.0
        for interval in intervals:
            frequency = root * interval
            value += math.sin(2 * math.pi * frequency * t) * 0.22
            value += math.sin(
                2 * math.pi * frequency * 2.003 * t
            ) * 0.035

        samples.append(value * 0.22 * envelope * swell)

    return samples


def make_ambient():
    count = SAMPLE_RATE * BACKGROUND_DURATION
    rng = random.Random(271828)
    samples = []
    noise = 0.0

    for index in range(count):
        t = index / SAMPLE_RATE
        noise = noise * 0.96 + rng.uniform(-1.0, 1.0) * 0.04

        low = math.sin(2 * math.pi * 55.0 * t)
        mid = math.sin(2 * math.pi * 82.41 * t + 0.4)
        modulation = 0.7 + 0.3 * math.sin(2 * math.pi * t / 9.0)
        fade = min(1.0, t / 1.5, (BACKGROUND_DURATION - t) / 1.5)

        value = low * 0.08 + mid * 0.04 + noise * 0.12
        samples.append(value * modulation * max(0.0, fade) * 0.25)

    return samples


def make_transition_sfx():
    count = int(SAMPLE_RATE * SFX_DURATION)
    rng = random.Random(314159)
    samples = []
    noise = 0.0

    for index in range(count):
        progress = index / max(1, count - 1)
        t = index / SAMPLE_RATE

        noise = noise * 0.72 + rng.uniform(-1.0, 1.0) * 0.28
        envelope = math.sin(math.pi * progress) ** 1.5
        pitch = 180.0 + 500.0 * progress
        tone = math.sin(2 * math.pi * pitch * t) * 0.18

        samples.append((noise * 0.10 + tone * 0.25) * envelope)

    return samples


def create_or_resume_track(
    category,
    path,
    generator,
    minimum_duration,
    previous_records,
    config_signature,
):
    track_signature = signature({
        "version": VERSION,
        "category": category,
        "config_signature": config_signature,
    })

    previous = previous_records.get(category, {})

    if path.is_file():
        try:
            duration = validate_wav(path, minimum_duration)
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
        except (OSError, ValueError, wave.Error, RuntimeError):
            log("REBUILD:", path.name)

    write_wav(path, generator())
    duration = validate_wav(path, minimum_duration)
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


def build_cues(parts, scenes, enabled, sfx_path):
    cues = []

    if not enabled:
        return cues

    relative_sfx = sfx_path.relative_to(ROOT).as_posix()

    for global_scene in range(2, parts * scenes + 1):
        part = (global_scene - 1) // scenes + 1
        local_scene = (global_scene - 1) % scenes + 1

        cues.append({
            "part": part,
            "scene": local_scene,
            "time": 0.08,
            "file": relative_sfx,
            "gain": 0.075,
            "kind": "generic_transition_whoosh",
            "timing_basis": "scene_start",
        })

    return cues


def main():
    values = read_settings()
    parts = positive_int(values, "PARTS", 1)
    scenes = positive_int(values, "SCENES", 11)

    switches = {
        "music": as_bool(
            values, "MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
            default=True,
        ),
        "ambient": as_bool(
            values, "AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
            "ENVIRONMENT_SOUND", default=True,
        ),
        "sfx": as_bool(
            values, "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
            default=True,
        ),
    }

    for directory in (MUSIC_DIR, AMBIENT_DIR, SFX_DIR, DESIGN_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    config_signature = signature({
        "parts": parts,
        "scenes": scenes,
        "switches": switches,
        "sample_rate": SAMPLE_RATE,
        "background_duration": BACKGROUND_DURATION,
        "sfx_duration": SFX_DURATION,
    })

    previous_manifest = read_json(MANIFEST, {})
    previous_records = {}

    if previous_manifest.get("version") == VERSION:
        previous_records = previous_manifest.get("tracks", {})

    definitions = {
        "music": (
            MUSIC_DIR / "background_music.wav",
            make_music,
            BACKGROUND_DURATION,
        ),
        "ambient": (
            AMBIENT_DIR / "ambient_environment.wav",
            make_ambient,
            BACKGROUND_DURATION,
        ),
        "sfx": (
            SFX_DIR / "transition_sfx.wav",
            make_transition_sfx,
            SFX_DURATION,
        ),
    }

    tracks = {}

    for category, (path, generator, minimum_duration) in definitions.items():
        if not switches[category]:
            tracks[category] = {
                "status": "disabled",
                "path": None,
                "generation": "local_synthesis_no_external_api",
            }

            if path.exists():
                path.unlink()

            continue

        tracks[category] = create_or_resume_track(
            category=category,
            path=path,
            generator=generator,
            minimum_duration=minimum_duration,
            previous_records=previous_records,
            config_signature=config_signature,
        )

    sfx_path = SFX_DIR / "transition_sfx.wav"
    cues = build_cues(
        parts=parts,
        scenes=scenes,
        enabled=switches["sfx"],
        sfx_path=sfx_path,
    )

    cue_data = {
        "status": "completed",
        "version": VERSION,
        "parts": parts,
        "scenes_per_part": scenes,
        "cue_count": len(cues),
        "cue_policy": (
            "One generic transition whoosh at 0.08 seconds into each "
            "scene after scene 1. Not story-event-aware."
        ),
        "external_api_calls": 0,
        "paid_services_used": False,
        "cues": cues,
    }
    write_json(CUE_MANIFEST, cue_data)

    manifest = {
        "status": "completed",
        "version": VERSION,
        "generation": "local_synthesis_no_external_api",
        "external_api_calls": 0,
        "paid_services_used": False,
        "sample_rate": SAMPLE_RATE,
        "background_duration_seconds": BACKGROUND_DURATION,
        "sfx_duration_seconds": SFX_DURATION,
        "parts": parts,
        "scenes_per_part": scenes,
        "enabled": switches,
        "tracks": tracks,
        "sfx_cue_manifest": CUE_MANIFEST.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
    }
    write_json(MANIFEST, manifest)

    log("SOUND DESIGN COMPLETE")
    log("MUSIC:", tracks["music"]["status"])
    log("AMBIENT:", tracks["ambient"]["status"])
    log("SFX:", tracks["sfx"]["status"])
    log("SFX CUES:", len(cues))
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)