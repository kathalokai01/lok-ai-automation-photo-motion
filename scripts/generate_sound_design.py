
#!/usr/bin/env python3
"""Free local sound design with resumable tracks and scene-timed SFX cues."""

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

VERSION = "free-sound-design-v2"
SAMPLE_RATE = 22050
BACKGROUND_DURATION = 30
SFX_DURATION = 1.6
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


def positive_int(values, key, default):
    raw = values.get(key, str(default))
    try:
        result = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be a positive integer") from exc

    if result < 1:
        raise RuntimeError(f"{key} must be a positive integer")
    return result


def fingerprint(payload):
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_wav(path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with wave.open(str(temporary), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)

        block_size = 16384
        for start in range(0, len(samples), block_size):
            packed = bytearray()
            for sample in samples[start:start + block_size]:
                value = max(-1.0, min(1.0, sample))
                packed.extend(struct.pack("<h", int(value * 32767)))
            audio.writeframesraw(packed)

    temporary.replace(path)


def validate_wav(path, minimum_duration=1.0):
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1:
            raise RuntimeError(f"Unexpected channel count: {path}")
        if audio.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"Unexpected sample rate: {path}")
        duration = audio.getnframes() / audio.getframerate()
        if duration < minimum_duration:
            raise RuntimeError(f"Audio file is too short: {path}")
        return duration


def make_music():
    """Generate a gentle synthetic harmonic background track."""
    count = SAMPLE_RATE * BACKGROUND_DURATION
    samples = []
    chord_roots = (110.0, 130.81, 98.0, 146.83)
    chord_intervals = (1.0, 1.25, 1.5, 2.0)

    for index in range(count):
        t = index / SAMPLE_RATE
        chord = int(t // 6) % len(chord_roots)

        fade_in = min(1.0, t / 2.5)
        fade_out = min(1.0, (BACKGROUND_DURATION - t) / 2.0)
        envelope = max(0.0, min(fade_in, fade_out))
        swell = 0.78 + 0.22 * math.sin(2 * math.pi * t / 12.0)

        value = 0.0
        root = chord_roots[chord]
        for interval in chord_intervals:
            frequency = root * interval
            value += math.sin(2 * math.pi * frequency * t) * 0.22
            value += math.sin(2 * math.pi * frequency * 2.003 * t) * 0.035

        samples.append(value * AMPLITUDE * envelope * swell)

    return samples


def make_ambient():
    """Generate quiet generic environmental texture, not a real location recording."""
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
    """Generate one short whoosh only; no hidden repeats or long silent tail."""
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
        tonal = math.sin(2 * math.pi * pitch * t) * 0.18

        samples.append((noise * 0.10 + tonal * 0.25) * envelope)

    return samples


def save_track(path, generator, records, category, signature, min_duration):
    previous = records.get(category, {})

    if path.is_file():
        try:
            duration = validate_wav(path, min_duration)
            digest = file_hash(path)
            if (
                previous.get("signature") == signature
                and previous.get("sha256") == digest
            ):
                log("RESUME: reusing", path.relative_to(ROOT))
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": signature,
                    "sha256": digest,
                    "duration_seconds": round(duration, 3),
                    "generation": "local_synthesis_no_external_api",
                }
        except (OSError, ValueError, wave.Error, RuntimeError):
            log("REBUILD: invalid or stale audio", path.name)

    write_wav(path, generator())
    duration = validate_wav(path, min_duration)
    digest = file_hash(path)

    log("CREATED:", path.relative_to(ROOT))
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": signature,
        "sha256": digest,
        "duration_seconds": round(duration, 3),
        "generation": "local_synthesis_no_external_api",
    }


def make_scene_cues(parts, scenes_per_part, sfx_enabled, sfx_path):
    """
    Add a transition cue at the beginning of each scene after scene 1.
    These are generic transition cues, not story-event-aware sound effects.
    """
    cues = []

    if not sfx_enabled:
        return cues

    relative_sfx = sfx_path.relative_to(ROOT).as_posix()

    for global_scene in range(2, parts * scenes_per_part + 1):
        part = ((global_scene - 1) // scenes_per_part) + 1
        local_scene = ((global_scene - 1) % scenes_per_part) + 1

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
    scenes_per_part = positive_int(values, "SCENES", 11)

    switches = {
        "music": enabled(
            values, "MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
            default=True,
        ),
        "ambient": enabled(
            values, "AMBIENT_ENABLED", "AMBIENT", "ENVIRONMENT_SOUND",
            default=True,
        ),
        "sfx": enabled(
            values, "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
            default=True,
        ),
    }

    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    AMBIENT_DIR.mkdir(parents=True, exist_ok=True)
    SFX_DIR.mkdir(parents=True, exist_ok=True)

    config_signature = fingerprint({
        "version": VERSION,
        "sample_rate": SAMPLE_RATE,
        "background_duration": BACKGROUND_DURATION,
        "sfx_duration": SFX_DURATION,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "switches": switches,
    })

    previous_records = {}
    if MANIFEST.is_file():
        try:
            old = json.loads(MANIFEST.read_text(encoding="utf-8"))
            if old.get("version") == VERSION:
                previous_records = old.get("tracks", {})
        except (OSError, ValueError, TypeError):
            previous_records = {}

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

    records = {}
    for category, (path, generator, minimum_duration) in definitions.items():
        if not switches[category]:
            records[category] = {
                "status": "disabled",
                "path": None,
                "generation": "local_synthesis_no_external_api",
            }
            if path.exists():
                path.unlink()
            continue

        signature = fingerprint({
            "config": config_signature,
            "category": category,
        })
        records[category] = save_track(
            path=path,
            generator=generator,
            records=previous_records,
            category=category,
            signature=signature,
            min_duration=minimum_duration,
        )

    sfx_path = SFX_DIR / "transition_sfx.wav"
    cues = make_scene_cues(
        parts=parts,
        scenes_per_part=scenes_per_part,
        sfx_enabled=switches["sfx"],
        sfx_path=sfx_path,
    )

    cue_data = {
        "status": "completed",
        "version": VERSION,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "cue_count": len(cues),
        "cue_policy": (
            "Generic transition whoosh at scene start after scene 1; "
            "not context-aware story-event SFX."
        ),
        "external_api_calls": 0,
        "paid_services_used": False,
        "cues": cues,
    }
    write_json(CUE_MANIFEST, cue_data)

    result = {
        "status": "completed",
        "version": VERSION,
        "generation": "local_synthesis_no_external_api",
        "external_api_calls": 0,
        "paid_services_used": False,
        "sample_rate": SAMPLE_RATE,
        "background_duration_seconds": BACKGROUND_DURATION,
        "sfx_duration_seconds": SFX_DURATION,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "enabled": switches,
        "tracks": records,
        "sfx_cue_manifest": CUE_MANIFEST.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
    }
    write_json(MANIFEST, result)

    log("SOUND DESIGN COMPLETE")
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes_per_part)
    log("SFX CUES:", len(cues))
    log("CUE MANIFEST:", CUE_MANIFEST.relative_to(ROOT))
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)
