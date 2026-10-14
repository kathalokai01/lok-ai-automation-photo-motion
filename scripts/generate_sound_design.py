
#!/usr/bin/env python3
"""Free, deterministic, resumable scene-aware music, ambience and SFX."""

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
DESIGN_DIR = OUTPUT / "sound_design"
MANIFEST_PATH = DESIGN_DIR / "sound_design_manifest.json"
CUES_PATH = DESIGN_DIR / "sfx_cues.json"
SCENES_PATH = OUTPUT / "scenes" / "scenes.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"
SFX_DIR = OUTPUT / "sfx"

VERSION = "free-sound-design-v6-scene-aware"
SAMPLE_RATE = 22050
BACKGROUND_SECONDS = 30
SFX_SECONDS = 1.6
BLOCK_SIZE = 8192
MIN_AUDIO_BYTES = 100

SFX_TYPES = (
    "whoosh",
    "impact",
    "mystery",
    "heartbeat",
    "chime",
)


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


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def signature(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Missing input file: {INPUT_FILE}")

    values = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.split("#", 1)[0].strip().strip("'\"")

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


def write_wav(path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
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
    finally:
        temporary.unlink(missing_ok=True)


def validate_wav(path, minimum_seconds):
    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"Audio missing or too small: {path}")

    with wave.open(str(path), "rb") as audio:
        if (
            audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or audio.getframerate() != SAMPLE_RATE
        ):
            raise RuntimeError(f"Unexpected WAV format: {path}")

        duration = audio.getnframes() / audio.getframerate()
        if duration < minimum_seconds:
            raise RuntimeError(f"Audio too short: {path}")

    return duration


def make_track(category, path, samples, duration):
    sig = signature({
        "version": VERSION,
        "category": category,
        "sample_rate": SAMPLE_RATE,
        "duration": duration,
    })

    previous = read_json(MANIFEST_PATH, {})
    old = previous.get("tracks", {}).get(category, {})

    if path.is_file():
        try:
            actual_hash = sha256_file(path)
            actual_duration = validate_wav(path, duration)
            if (
                previous.get("version") == VERSION
                and old.get("signature") == sig
                and old.get("sha256") == actual_hash
            ):
                log("RESUME:", path.relative_to(ROOT))
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": sig,
                    "sha256": actual_hash,
                    "duration_seconds": round(actual_duration, 3),
                    "generation": "local_synthesis_no_external_api",
                }
        except (OSError, ValueError, wave.Error, RuntimeError):
            pass

    write_wav(path, samples())
    actual_duration = validate_wav(path, duration)
    actual_hash = sha256_file(path)

    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": sig,
        "sha256": actual_hash,
        "duration_seconds": round(actual_duration, 3),
        "generation": "local_synthesis_no_external_api",
    }


def music_samples():
    count = SAMPLE_RATE * BACKGROUND_SECONDS
    roots = (110.0, 130.81, 98.0, 146.83)
    intervals = (1.0, 1.25, 1.5, 2.0)

    for index in range(count):
        t = index / SAMPLE_RATE
        root = roots[int(t // 6) % len(roots)]
        envelope = min(1.0, t / 2.5, (BACKGROUND_SECONDS - t) / 2.0)
        swell = 0.78 + 0.22 * math.sin(2 * math.pi * t / 12)
        value = sum(
            0.22 * math.sin(2 * math.pi * root * ratio * t)
            + 0.035 * math.sin(2 * math.pi * root * ratio * 2.003 * t)
            for ratio in intervals
        )
        yield value * 0.22 * max(0.0, envelope) * swell


def ambient_samples():
    rng = random.Random(271828)
    filtered_noise = 0.0

    for index in range(SAMPLE_RATE * BACKGROUND_SECONDS):
        t = index / SAMPLE_RATE
        filtered_noise = filtered_noise * 0.96 + rng.uniform(-1, 1) * 0.04
        fade = max(0.0, min(1.0, t / 1.5, (BACKGROUND_SECONDS - t) / 1.5))
        value = (
            0.08 * math.sin(2 * math.pi * 55 * t)
            + 0.04 * math.sin(2 * math.pi * 82.41 * t + 0.4)
            + filtered_noise * 0.12
        )
        yield value * fade * 0.22


def sfx_samples(kind):
    count = int(SAMPLE_RATE * SFX_SECONDS)
    rng = random.Random(9000 + SFX_TYPES.index(kind))
    filtered_noise = 0.0

    for index in range(count):
        t = index / SAMPLE_RATE
        p = index / max(1, count - 1)
        fade = max(0.0, math.sin(math.pi * p)) ** 1.5
        filtered_noise = filtered_noise * 0.72 + rng.uniform(-1, 1) * 0.28

        if kind == "whoosh":
            signal = filtered_noise * 0.25 + math.sin(2 * math.pi * (180 + 500 * p) * t) * 0.18
        elif kind == "impact":
            decay = math.exp(-8 * t)
            signal = decay * (
                0.65 * math.sin(2 * math.pi * 72 * t)
                + 0.25 * filtered_noise
            )
        elif kind == "mystery":
            signal = (
                0.30 * math.sin(2 * math.pi * 220 * t)
                + 0.22 * math.sin(2 * math.pi * 277.18 * t)
                + 0.10 * filtered_noise
            )
        elif kind == "heartbeat":
            beat = max(0.0, math.sin(2 * math.pi * 1.6 * t)) ** 10
            signal = beat * math.sin(2 * math.pi * 65 * t) + filtered_noise * 0.025
        else:  # chime
            signal = (
                0.40 * math.sin(2 * math.pi * 880 * t)
                + 0.22 * math.sin(2 * math.pi * 1320 * t)
                + 0.10 * math.sin(2 * math.pi * 1760 * t)
            ) * math.exp(-2.6 * t)

        yield signal * fade * 0.55


def load_scene_rows():
    data = read_json(SCENES_PATH)
    if not isinstance(data, dict):
        raise RuntimeError(f"Missing or invalid scene manifest: {SCENES_PATH}")

    parts = data.get("parts")
    if isinstance(parts, list):
        rows = []
        for p_index, part in enumerate(parts, 1):
            if not isinstance(part, dict):
                continue
            p_num = int(part.get("part", p_index))
            scenes = part.get("scenes", [])
            if not isinstance(scenes, list):
                continue
            for s_index, scene in enumerate(scenes, 1):
                if isinstance(scene, dict):
                    row = dict(scene)
                    row.setdefault("part", p_num)
                    row.setdefault("scene", s_index)
                    rows.append(row)
        if rows:
            return rows

    rows = data.get("scenes")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]

    raise RuntimeError("scenes.json has no supported scenes list")


def choose_sfx(scene):
    """Choose a deterministic sound category from scene sound/narration text."""
    text = " ".join(
        str(scene.get(key, ""))
        for key in ("sfx", "title", "narration", "mood", "suspense_prompt")
    ).casefold()

    groups = [
        ("heartbeat", (
            "heartbeat", "heart beat", "धड़कन", "धड़कन", "दिल जोर",
            "heartbeat", "pulse",
        )),
        ("impact", (
            "impact", "crash", "slam", "bang", "धमाका", "टकराया",
            "गिर पड़ा", "टूट गया", "गोली", "explosion",
        )),
        ("chime", (
            "bell", "chime", "ring", "घंटी", "फोन बज", "अलार्म",
            "खुशखबरी", "magic reveal",
        )),
        ("mystery", (
            "mystery", "secret", "suspense", " डर", "भूत", "रहस्य",
            "अंधेरा", "साया", "खौफ", "चौंक", "अनजान आवाज",
        )),
    ]

    for kind, keywords in groups:
        if any(keyword in text for keyword in keywords):
            return kind

    if any(word in text for word in ("transition", "चल पड़ा", "दौड़ा", "भागा")):
        return "whoosh"

    return "mystery" if scene.get("suspense_required") else "whoosh"


def load_audio_durations():
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, (dict, list)):
        return {}

    rows = data if isinstance(data, list) else (
        data.get("jobs") or data.get("audio_jobs") or []
    )
    durations = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        value = next(
            (
                row.get(name) for name in ("output", "audio_path", "path", "file", "audio")
                if isinstance(row.get(name), str) and row[name].strip()
            ),
            None,
        )
        if not value:
            continue

        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        path = path.resolve()

        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            continue

        try:
            import shutil
            import subprocess

            ffprobe = shutil.which("ffprobe")
            if not ffprobe:
                break
            result = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            duration = float(result.stdout.strip()) if result.returncode == 0 else 0
            if math.isfinite(duration) and duration > 0:
                durations[key] = duration
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue

    return durations


def build_cues(scene_rows, parts, scenes_per_part, enabled, durations):
    if not enabled:
        return []

    cues = []
    seen = set()

    for row in scene_rows:
        try:
            part = int(row.get("part"))
            scene = int(row.get("scene", row.get("local_scene")))
        except (TypeError, ValueError):
            continue

        if part < 1 or scene < 1 or part > parts or scene > scenes_per_part:
            continue
        if (part, scene) in seen:
            raise RuntimeError(f"Duplicate scene mapping in scenes.json: {part}/{scene}")
        seen.add((part, scene))

        kind = choose_sfx(row)
        duration = durations.get((part, scene))
        # Use a safe relative offset. Never start a cue beyond known narration.
        cue_time = 0.08
        if duration is not None:
            if duration <= cue_time:
                continue
            cue_time = min(cue_time, max(0.0, duration * 0.1))

        path = SFX_DIR / f"{kind}.wav"
        cues.append({
            "part": part,
            "scene": scene,
            "time": round(cue_time, 3),
            "file": path.relative_to(ROOT).as_posix(),
            "gain": 0.045 if kind in {"mystery", "heartbeat"} else 0.06,
            "kind": kind,
            "selection_basis": "scene_text_keyword_rules",
            "timing_basis": "scene_start_relative_seconds",
        })

    if not cues:
        log("WARNING: no scene-aware SFX cues were produced")
    return cues


def main():
    values = read_settings()
    parts = positive_int(values, "PARTS", 1)
    scenes_per_part = positive_int(values, "SCENES", 11)

    enabled = {
        "music": setting_bool(values, ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC", "SOUNDTRACK_ENABLED")),
        "ambient": setting_bool(values, ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND", "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED")),
        "sfx": setting_bool(values, ("SFX_ENABLED", "SFX", "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED")),
    }

    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    SFX_DIR.mkdir(parents=True, exist_ok=True)

    old_manifest = read_json(MANIFEST_PATH, {})
    old_tracks = old_manifest.get("tracks", {}) if isinstance(old_manifest, dict) else {}
    tracks = {}

    definitions = {
        "music": (MUSIC_PATH, music_samples, BACKGROUND_SECONDS),
        "ambient": (AMBIENT_PATH, ambient_samples, BACKGROUND_SECONDS),
    }

    for category, (path, generator, duration) in definitions.items():
        if not enabled[category]:
            path.unlink(missing_ok=True)
            tracks[category] = {"status": "disabled", "path": None}
        else:
            tracks[category] = make_track(category, path, generator, duration)

    sfx_tracks = {}
    for kind in SFX_TYPES:
        path = SFX_DIR / f"{kind}.wav"
        if not enabled["sfx"]:
            path.unlink(missing_ok=True)
            sfx_tracks[kind] = {"status": "disabled", "path": None}
            continue

        old = old_tracks.get(f"sfx_{kind}", {})
        sig = signature({
            "version": VERSION,
            "kind": kind,
            "sample_rate": SAMPLE_RATE,
            "duration": SFX_SECONDS,
        })

        if path.is_file():
            try:
                digest = sha256_file(path)
                duration = validate_wav(path, SFX_SECONDS)
                if (
                    old_manifest.get("version") == VERSION
                    and old.get("signature") == sig
                    and old.get("sha256") == digest
                ):
                    sfx_tracks[kind] = {
                        "status": "completed",
                        "path": path.relative_to(ROOT).as_posix(),
                        "signature": sig,
                        "sha256": digest,
                        "duration_seconds": round(duration, 3),
                    }
                    continue
            except (OSError, ValueError, wave.Error, RuntimeError):
                pass

        write_wav(path, sfx_samples(kind))
        duration = validate_wav(path, SFX_SECONDS)
        sfx_tracks[kind] = {
            "status": "completed",
            "path": path.relative_to(ROOT).as_posix(),
            "signature": sig,
            "sha256": sha256_file(path),
            "duration_seconds": round(duration, 3),
        }

    scene_rows = load_scene_rows() if enabled["sfx"] else []
    durations = load_audio_durations()
    cues = build_cues(scene_rows, parts, scenes_per_part, enabled["sfx"], durations)

    # Ensure every cue file exists and its cue begins within known scene audio.
    for cue in cues:
        path = ROOT / cue["file"]
        if not path.is_file():
            raise RuntimeError(f"SFX cue asset missing: {path}")
        duration = durations.get((cue["part"], cue["scene"]))
        if duration is not None and cue["time"] >= duration:
            raise RuntimeError(
                f"SFX cue starts outside narration duration: "
                f"{cue['part']}/{cue['scene']}"
            )

    write_json(CUES_PATH, {
        "status": "completed",
        "version": VERSION,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "cue_count": len(cues),
        "cue_policy": "Scene-text keyword selection from deterministic local SFX assets.",
        "timing_basis": "Seconds relative to scene start; narration duration checked when available.",
        "external_api_calls": 0,
        "paid_services_used": False,
        "cues": cues,
    })

    tracks.update({
        f"sfx_{kind}": record for kind, record in sfx_tracks.items()
    })

    write_json(MANIFEST_PATH, {
        "status": "completed",
        "version": VERSION,
        "generation": "local_synthesis_no_external_api",
        "external_api_calls": 0,
        "paid_services_used": False,
        "sample_rate": SAMPLE_RATE,
        "background_duration_seconds": BACKGROUND_SECONDS,
        "sfx_duration_seconds": SFX_SECONDS,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "enabled": enabled,
        "tracks": tracks,
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
    })

    log("SOUND DESIGN COMPLETE")
    for category in ("music", "ambient", "sfx"):
        log(f"{category.upper()}: {'enabled' if enabled[category] else 'disabled'}")
    log("SFX CUES:", len(cues))
    log("SFX TYPES:", ", ".join(SFX_TYPES))
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)
