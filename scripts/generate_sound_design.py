
#!/usr/bin/env python3
"""Create free, deterministic background music, ambience and transition SFX."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
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
MANIFEST = OUTPUT / "sound_design" / "sound_design_manifest.json"

VERSION = "free-sound-design-v1"
SAMPLE_RATE = 22050
DURATION_SECONDS = 30
AMPLITUDE = 0.22


def log(*args):
    print(*args, flush=True)


def read_settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Missing input file: {INPUT_FILE}")

    values = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.split("#", 1)[0].strip().strip("'\"")
        values[key] = value

    return values


def enabled(values, *keys, default=True):
    for key in keys:
        if key in values:
            return values[key].strip().lower() in {
                "1", "true", "yes", "on", "enabled"
            }
    return default


def fingerprint(payload):
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_wav(path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with wave.open(str(temporary), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)

        block_size = 16384
        for start in range(0, len(samples), block_size):
            block = samples[start:start + block_size]
            packed = bytearray()
            for sample in block:
                value = max(-1.0, min(1.0, sample))
                packed.extend(struct.pack("<h", int(value * 32767)))
            audio.writeframesraw(packed)

    temporary.replace(path)


def make_music():
    """Generate a gentle, royalty-free synthesized harmonic bed."""
    count = SAMPLE_RATE * DURATION_SECONDS
    samples = []
    chord_roots = (110.0, 130.81, 98.0, 146.83)
    chord_intervals = (1.0, 1.25, 1.5, 2.0)

    for index in range(count):
        t = index / SAMPLE_RATE
        chord = int(t // 6) % len(chord_roots)
        phase = t % 6.0

        fade_in = min(1.0, t / 2.5)
        fade_out = min(1.0, (DURATION_SECONDS - t) / 2.0)
        envelope = max(0.0, min(fade_in, fade_out))
        swell = 0.78 + 0.22 * math.sin(2 * math.pi * t / 12.0)

        value = 0.0
        root = chord_roots[chord]

        for interval in chord_intervals:
            frequency = root * interval
            value += math.sin(2 * math.pi * frequency * t) * 0.22
            value += math.sin(
                2 * math.pi * frequency * 2.003 * t
            ) * 0.035

        value *= AMPLITUDE * envelope * swell
        samples.append(value)

    return samples


def make_ambient():
    """Generate a quiet, soft environmental drone."""
    count = SAMPLE_RATE * DURATION_SECONDS
    rng = random.Random(271828)
    samples = []
    noise = 0.0

    for index in range(count):
        t = index / SAMPLE_RATE
        noise = noise * 0.96 + rng.uniform(-1.0, 1.0) * 0.04

        low = math.sin(2 * math.pi * 55.0 * t)
        mid = math.sin(2 * math.pi * 82.41 * t + 0.4)
        modulation = 0.7 + 0.3 * math.sin(2 * math.pi * t / 9.0)

        fade = min(1.0, t / 1.5, (DURATION_SECONDS - t) / 1.5)
        value = (low * 0.08 + mid * 0.04 + noise * 0.12)
        samples.append(value * modulation * max(0.0, fade) * 0.25)

    return samples


def make_sfx():
    """Generate a soft, short cinematic transition whoosh with silence."""
    count = SAMPLE_RATE * DURATION_SECONDS
    rng = random.Random(314159)
    samples = [0.0] * count

    # A short whoosh every 10 seconds; the rest remains silent.
    for start_seconds in (3.0, 13.0, 23.0):
        start = int(start_seconds * SAMPLE_RATE)
        length = int(1.6 * SAMPLE_RATE)
        noise = 0.0

        for offset in range(length):
            index = start + offset
            if index >= count:
                break

            t = offset / SAMPLE_RATE
            progress = offset / max(1, length - 1)
            noise = noise * 0.72 + rng.uniform(-1.0, 1.0) * 0.28

            # Smooth rise and fall avoids sharp clicks.
            envelope = math.sin(math.pi * progress) ** 1.5
            pitch = 180.0 + 500.0 * progress
            tonal = math.sin(2 * math.pi * pitch * t) * 0.18

            samples[index] = (
                noise * 0.10 + tonal * 0.25
            ) * envelope

    return samples


def validate_wav(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1:
            raise RuntimeError(f"Unexpected channel count: {path}")
        if audio.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"Unexpected sample rate: {path}")
        if audio.getnframes() < SAMPLE_RATE:
            raise RuntimeError(f"Audio file is too short: {path}")


def save_track(path, generator, records, category, signature):
    if path.is_file():
        try:
            validate_wav(path)
            existing_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            previous = records.get(category, {})
            if (
                previous.get("signature") == signature
                and previous.get("sha256") == existing_hash
            ):
                log("RESUME: reusing", path.relative_to(ROOT))
                return
        except (OSError, ValueError, wave.Error, RuntimeError):
            pass

    samples = generator()
    write_wav(path, samples)
    validate_wav(path)

    records[category] = {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": signature,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "duration_seconds": DURATION_SECONDS,
        "generation": "local_synthesis_no_external_api",
    }
    log("CREATED:", path.relative_to(ROOT))


def main():
    values = read_settings()

    switches = {
        "music": enabled(
            values, "MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
            default=True
        ),
        "ambient": enabled(
            values, "AMBIENT_ENABLED", "AMBIENT", "ENVIRONMENT_SOUND",
            default=True
        ),
        "sfx": enabled(
            values, "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
            default=True
        ),
    }

    config_fingerprint = fingerprint({
        "version": VERSION,
        "duration": DURATION_SECONDS,
        "sample_rate": SAMPLE_RATE,
        "switches": switches,
    })

    records = {}
    if MANIFEST.is_file():
        try:
            old = json.loads(MANIFEST.read_text(encoding="utf-8"))
            if old.get("version") == VERSION:
                records = old.get("tracks", {})
        except (OSError, ValueError, TypeError):
            records = {}

    generators = {
        "music": (
            MUSIC_DIR / "background_music.wav",
            make_music,
        ),
        "ambient": (
            AMBIENT_DIR / "ambient_environment.wav",
            make_ambient,
        ),
        "sfx": (
            SFX_DIR / "transition_sfx.wav",
            make_sfx,
        ),
    }

    for category, (path, generator) in generators.items():
        if not switches[category]:
            records[category] = {
                "status": "disabled",
                "path": None,
                "generation": "local_synthesis_no_external_api",
            }
            continue

        signature = fingerprint({
            "config": config_fingerprint,
            "category": category,
        })
        save_track(path, generator, records, category, signature)

    # Remove disabled tracks so the renderer cannot accidentally mix them.
    for category, (path, _) in generators.items():
        if not switches[category] and path.exists():
            path.unlink()

    result = {
        "status": "completed",
        "version": VERSION,
        "generation": "local_synthesis_no_external_api",
        "external_api_calls": 0,
        "paid_services_used": False,
        "duration_seconds": DURATION_SECONDS,
        "sample_rate": SAMPLE_RATE,
        "enabled": switches,
        "tracks": records,
    }

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(MANIFEST)

    log("SOUND DESIGN COMPLETE")
    log("No external API calls or paid services were used.")
    log("Next workflow step must run scripts/render_video.py.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)
