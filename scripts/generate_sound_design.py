#!/usr/bin/env python3
"""Local resumable sound design with more audible SFX and manifest compatibility."""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT = ROOT / "output"

MUSIC_PATH = OUTPUT / "music" / "background_music.wav"
AMBIENT_PATH = OUTPUT / "sound" / "ambient_environment.wav"
SFX_DIR = OUTPUT / "sfx"
DESIGN_DIR = OUTPUT / "sound_design"
MANIFEST_PATH = DESIGN_DIR / "sound_design_manifest.json"
CUES_PATH = DESIGN_DIR / "sfx_cues.json"
SCENES_PATH = OUTPUT / "scenes" / "scenes.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"

VERSION = "free-sound-design-v12-audible-sfx"
RATE = 22050
SFX_SECONDS = 1.8
SFX_TYPES = ("whoosh", "impact", "mystery", "heartbeat", "chime")
PATH_KEYS = ("output", "audio_path", "path", "file", "audio")

# Increased from the previous very quiet 0.045/0.060 levels.
SFX_GAINS = {
    "whoosh": 0.15,
    "impact": 0.20,
    "mystery": 0.13,
    "heartbeat": 0.15,
    "chime": 0.16,
}
SFX_TIME_FRACTIONS = {
    "whoosh": 0.30,
    "heartbeat": 0.18,
    "impact": 0.60,
    "mystery": 0.48,
    "chime": 0.72,
}


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def signature(data):
    raw = json.dumps(
        data, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Missing input file: {INPUT_FILE}")

    result = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip().upper()] = value
    return result


def positive_int(config, key, default):
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{key} must be a positive integer")
    return value


def enabled(config, keys, default=True):
    for key in keys:
        if key not in config:
            continue
        value = str(config[key]).strip().lower()
        if value in {"true", "yes", "1", "on", "enabled"}:
            return True
        if value in {"false", "no", "0", "off", "disabled"}:
            return False
        raise RuntimeError(f"{key} must be true or false")
    return default


def validate_wav(path, minimum_seconds=0.1):
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"Audio missing or too small: {path}")

    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise RuntimeError(f"Unexpected WAV format: {path}")
            if wav.getframerate() != RATE or wav.getnframes() <= 0:
                raise RuntimeError(f"Invalid WAV sample rate or frames: {path}")
            duration = wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"Invalid WAV file: {path}") from exc

    if duration + 1 / RATE < minimum_seconds:
        raise RuntimeError(f"Audio too short: {path}")
    return duration


def write_wav(path, samples, duration):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    expected = int(round(RATE * duration))
    count = 0

    try:
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            block = bytearray()

            for sample in samples:
                if count >= expected:
                    break
                sample = max(-0.92, min(0.92, float(sample)))
                block.extend(struct.pack("<h", int(sample * 32767)))
                count += 1
                if len(block) >= 32768:
                    wav.writeframesraw(block)
                    block.clear()

            if block:
                wav.writeframesraw(block)

            if count != expected:
                raise RuntimeError(
                    f"Generated {count} audio frames; expected {expected}"
                )

        validate_wav(tmp, min(0.1, duration))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def envelope(t, duration, fade_in=1.5, fade_out=2.0):
    return max(
        0.0,
        min(
            1.0,
            t / max(fade_in, 0.1),
            (duration - t) / max(fade_out, 0.1),
        ),
    )


def music_samples(duration):
    roots = (110.0, 130.81, 98.0, 146.83, 123.47, 92.5)
    for i in range(int(round(RATE * duration))):
        t = i / RATE
        root = roots[int(t // 8) % len(roots)]
        slow = 0.8 + 0.2 * math.sin(2 * math.pi * t / 17)
        tone = (
            0.30 * math.sin(2 * math.pi * root * t)
            + 0.19 * math.sin(2 * math.pi * root * 1.5 * t)
            + 0.12 * math.sin(2 * math.pi * root * 2 * t)
        )
        yield tone * slow * envelope(t, duration, 2.0, 3.0)


def ambient_samples(duration):
    rng = random.Random(271828)
    filtered = 0.0
    for i in range(int(round(RATE * duration))):
        t = i / RATE
        filtered = filtered * 0.985 + rng.uniform(-1, 1) * 0.015
        low = (
            0.12 * math.sin(2 * math.pi * 47 * t)
            + 0.08 * math.sin(2 * math.pi * 69.3 * t)
        )
        yield (low + filtered * 0.14) * envelope(t, duration, 1.5, 2.0)


def sfx_samples(kind):
    count = int(round(RATE * SFX_SECONDS))
    rng = random.Random(9000 + SFX_TYPES.index(kind))
    filtered = 0.0

    for i in range(count):
        t = i / RATE
        p = i / max(1, count - 1)
        filtered = filtered * 0.72 + rng.uniform(-1, 1) * 0.28
        fade = max(0.0, math.sin(math.pi * p)) ** 1.1

        if kind == "whoosh":
            value = (
                0.42 * filtered
                + 0.28 * math.sin(2 * math.pi * (140 + 600 * p) * t)
            )
        elif kind == "impact":
            decay = math.exp(-7.0 * t)
            value = decay * (
                0.75 * math.sin(2 * math.pi * 65 * t)
                + 0.25 * filtered
            )
        elif kind == "mystery":
            wobble = 2.0 * math.sin(2 * math.pi * 0.7 * t)
            value = (
                0.32 * math.sin(2 * math.pi * (196 + wobble) * t)
                + 0.20 * math.sin(2 * math.pi * 246.94 * t)
                + 0.12 * filtered
            )
        elif kind == "heartbeat":
            beat = max(0.0, math.sin(2 * math.pi * 1.5 * t)) ** 10
            value = beat * math.sin(2 * math.pi * 58 * t) + 0.05 * filtered
        else:
            value = (
                0.45 * math.sin(2 * math.pi * 880 * t)
                + 0.24 * math.sin(2 * math.pi * 1320 * t)
                + 0.12 * math.sin(2 * math.pi * 1760 * t)
            ) * math.exp(-2.2 * t)

        yield value * fade


def scene_rows():
    data = read_json(SCENES_PATH)
    if not isinstance(data, dict):
        raise RuntimeError(f"Missing or invalid scenes file: {SCENES_PATH}")

    rows = []
    parts_data = data.get("parts")
    if isinstance(parts_data, list):
        for pi, part_data in enumerate(parts_data, 1):
            if not isinstance(part_data, dict):
                continue
            part = int(part_data.get("part", pi))
            scenes = part_data.get("scenes", [])
            if not isinstance(scenes, list):
                raise RuntimeError(f"Scenes for part {part} must be a list")
            for si, scene in enumerate(scenes, 1):
                if not isinstance(scene, dict):
                    raise RuntimeError(f"Invalid scene in part {part}")
                row = dict(scene)
                row.setdefault("part", part)
                row.setdefault("scene", si)
                rows.append(row)
        if rows:
            return rows

    scenes = data.get("scenes")
    if isinstance(scenes, list):
        return [row for row in scenes if isinstance(row, dict)]
    raise RuntimeError("scenes.json contains no supported scene list")


def choose_sfx(scene):
    text = " ".join(
        str(scene.get(k, ""))
        for k in (
            "sfx", "title", "narration", "text", "mood",
            "suspense_prompt", "visual_prompt",
        )
    ).casefold()

    groups = [
        ("heartbeat", (
            "heartbeat", "heart beat", "pulse", "धड़कन", "धड़कन",
        )),
        ("impact", (
            "impact", "crash", "slam", "bang", "explosion",
            "धमाका", "टकराया", "गिर पड़ा", "टूट गया", "गोली",
        )),
        ("chime", (
            "bell", "chime", "ring", "घंटी", "फोन बज", "अलार्म",
        )),
        ("mystery", (
            "mystery", "secret", "suspense", "डर", "भूत", "रहस्य",
            "अंधेरा", "साया", "खौफ", "चौंक", "अनजान आवाज",
        )),
    ]
    for kind, keywords in groups:
        if any(word in text for word in keywords):
            return kind

    if any(word in text for word in ("transition", "दौड़ा", "भागा")):
        return "whoosh"
    return "mystery" if scene.get("suspense_required") else "whoosh"


def narration_durations():
    data = read_json(AUDIO_MANIFEST)
    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = data.get("jobs") or data.get("audio_jobs") or data.get("scenes") or []
    else:
        raise RuntimeError("Narration manifest missing or invalid")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("No narration jobs found; generate narration first")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to inspect narration")

    result = {}
    for row in jobs:
        if not isinstance(row, dict):
            continue
        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError):
            continue

        if str(row.get("status", "")).lower() != "completed":
            continue

        value = next(
            (row.get(k) for k in PATH_KEYS
             if isinstance(row.get(k), str) and row[k].strip()),
            None,
        )
        if not value:
            raise RuntimeError(f"Narration path missing for {part}/{scene}")

        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        path = path.resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            raise RuntimeError(f"Narration file missing or unsafe: {part}/{scene}")

        proc = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode:
            raise RuntimeError(f"ffprobe failed for {part}/{scene}: {proc.stderr[-500:]}")

        try:
            info = json.loads(proc.stdout)
            duration = float(info["format"]["duration"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Invalid narration duration for {part}/{scene}") from exc

        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(f"Invalid narration duration for {part}/{scene}")
        if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
            raise RuntimeError(f"No audio stream for narration {part}/{scene}")

        key = (part, scene)
        if key in result:
            raise RuntimeError(f"Duplicate narration job: {key}")
        result[key] = {
            "duration": duration,
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(path),
        }

    if not result:
        raise RuntimeError("No completed narration files found")
    return result


def make_track(path, category, generator, duration, old_tracks):
    sig = signature({
        "version": VERSION,
        "category": category,
        "sample_rate": RATE,
        "duration": round(duration, 6),
    })
    previous = old_tracks.get(category, {})
    if not isinstance(previous, dict):
        previous = {}

    if path.is_file():
        try:
            actual_duration = validate_wav(path, duration)
            checksum = sha256_file(path)
            if (
                previous.get("signature") == sig
                and previous.get("sha256") == checksum
            ):
                log("RESUME:", path.relative_to(ROOT))
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": sig,
                    "sha256": checksum,
                    "duration_seconds": round(actual_duration, 6),
                    "generation": "deterministic_local_synthesis_no_external_api",
                }
        except (OSError, ValueError, RuntimeError, wave.Error, EOFError):
            log("REBUILDING INVALID AUDIO:", path.relative_to(ROOT))

    log("GENERATING:", category, f"{duration:.2f}s")
    write_wav(path, generator(duration), duration)
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": sig,
        "sha256": sha256_file(path),
        "duration_seconds": round(validate_wav(path, duration), 6),
        "generation": "deterministic_local_synthesis_no_external_api",
    }


def main():
    config = settings()
    parts = positive_int(config, "PARTS", 1)
    scenes_per_part = positive_int(config, "SCENES", 11)

    enabled_tracks = {
        "music": enabled(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC", "SOUNDTRACK_ENABLED"),
        ),
        "ambient": enabled(
            config,
            ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
             "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED"),
        ),
        "sfx": enabled(
            config,
            ("SFX_ENABLED", "SFX", "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED"),
        ),
    }

    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    SFX_DIR.mkdir(parents=True, exist_ok=True)

    old = read_json(MANIFEST_PATH, {})
    if not isinstance(old, dict):
        old = {}
    old_tracks = old.get("tracks", {})
    if not isinstance(old_tracks, dict):
        old_tracks = {}

    durations = narration_durations()
    expected = {
        (p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes_per_part + 1)
    }
    if set(durations) != expected:
        raise RuntimeError(
            "Narration scene count differs from Input/topic.txt. "
            f"Missing={sorted(expected - set(durations))}; "
            f"extra={sorted(set(durations) - expected)}"
        )

    total_duration = sum(x["duration"] for x in durations.values())
    background_duration = max(30.0, total_duration + 2.0)

    tracks = {}
    definitions = {
        "music": (MUSIC_PATH, music_samples),
        "ambient": (AMBIENT_PATH, ambient_samples),
    }

    for category, (path, generator) in definitions.items():
        if not enabled_tracks[category]:
            path.unlink(missing_ok=True)
            tracks[category] = None
            continue
        tracks[category] = make_track(
            path, category, generator, background_duration, old_tracks
        )

    sfx_tracks = {}
    for kind in SFX_TYPES:
        path = SFX_DIR / f"{kind}.wav"
        category = f"sfx_{kind}"

        if not enabled_tracks["sfx"]:
            path.unlink(missing_ok=True)
            sfx_tracks[kind] = None
            continue

        sfx_tracks[kind] = make_track(
            path,
            category,
            lambda duration, selected=kind: sfx_samples(selected),
            SFX_SECONDS,
            old_tracks,
        )

    tracks["sfx"] = sfx_tracks.get("whoosh")
    for kind, record in sfx_tracks.items():
        tracks[f"sfx_{kind}"] = record

    cues = []
    if enabled_tracks["sfx"]:
        rows = scene_rows()
        scene_map = {}
        for row in rows:
            try:
                key = (
                    int(row.get("part")),
                    int(row.get("scene", row.get("local_scene"))),
                )
            except (TypeError, ValueError):
                continue
            if key not in expected:
                continue
            if key in scene_map:
                raise RuntimeError(f"Duplicate scene mapping: {key}")
            scene_map[key] = row

        if set(scene_map) != expected:
            raise RuntimeError(
                "Scene mapping mismatch: "
                f"missing={sorted(expected - set(scene_map))}"
            )

        for part, scene in sorted(expected):
            narration = durations[(part, scene)]
            duration = narration["duration"]
            kind = choose_sfx(scene_map[(part, scene)])
            cue_time = min(
                duration * SFX_TIME_FRACTIONS[kind],
                max(0.0, duration - 0.25),
            )
            cues.append({
                "part": part,
                "scene": scene,
                "time": round(cue_time, 3),
                "file": (SFX_DIR / f"{kind}.wav").relative_to(ROOT).as_posix(),
                "gain": SFX_GAINS[kind],
                "kind": kind,
                "selection_basis": "scene_text_keyword_rules",
                "timing_basis": "actual_narration_duration_scene_relative_offset",
                "narration_duration_seconds": round(duration, 3),
                "narration_file": narration["path"],
                "narration_sha256": narration["sha256"],
            })

    write_json(CUES_PATH, {
        "status": "completed",
        "version": VERSION,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "cue_count": len(cues),
        "cue_policy": "Deterministic local SFX selected from scene text.",
        "timing_basis": "Actual narration duration with scene-relative offsets.",
        "external_api_calls": 0,
        "paid_services_used": False,
        "cues": cues,
    })

    write_json(MANIFEST_PATH, {
        "status": "completed",
        "version": VERSION,
        "generation": "local_synthesis_no_external_api",
        "external_api_calls": 0,
        "paid_services_used": False,
        "sample_rate": RATE,
        "background_duration_seconds": round(background_duration, 3),
        "sfx_duration_seconds": SFX_SECONDS,
        "total_narration_seconds": round(total_duration, 3),
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "enabled": enabled_tracks,
        "tracks": tracks,
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
    })

    log("SOUND DESIGN COMPLETE")
    log("SFX cues:", len(cues))
    log("External API calls: 0; paid services: false")
    log("NOTE: render_video.py still applies low fixed background gains.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)