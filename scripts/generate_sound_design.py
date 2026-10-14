
#!/usr/bin/env python3
"""Resumable, local-only sound design with strict scene/audio validation."""

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

VERSION = "free-sound-design-v8-strict-narration-timing"
RATE = 22050
BACKGROUND_SECONDS = 30
SFX_SECONDS = 1.6
MIN_BYTES = 100
SFX_TYPES = ("whoosh", "impact", "mystery", "heartbeat", "chime")


def log(*values):
    print(*values, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest(data):
    return hashlib.sha256(
        json.dumps(
            data, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Missing input file: {INPUT_FILE}")

    result = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip().upper()] = (
            value.split("#", 1)[0].strip().strip("'\"")
        )
    return result


def as_bool(config, keys, default=True):
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


def positive_int(config, key, default):
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{key} must be a positive integer"
        ) from exc
    if value < 1:
        raise RuntimeError(f"{key} must be a positive integer")
    return value


def write_wav(path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")

    try:
        with wave.open(str(temp), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(RATE)

            block = bytearray()
            for sample in samples:
                sample = max(-1.0, min(1.0, float(sample)))
                block.extend(struct.pack("<h", int(sample * 32767)))
                if len(block) >= 16384:
                    audio.writeframesraw(block)
                    block.clear()

            if block:
                audio.writeframesraw(block)

        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def validate_audio(path, minimum_seconds=0.1):
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError(f"Audio missing or too small: {path}")

    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise RuntimeError(f"Unexpected WAV format: {path}")
        if audio.getframerate() != RATE:
            raise RuntimeError(f"Unexpected WAV sample rate: {path}")
        duration = audio.getnframes() / audio.getframerate()

    if duration < minimum_seconds:
        raise RuntimeError(f"Audio too short: {path}")
    return duration


def track_record(path, signature, minimum_seconds):
    duration = validate_audio(path, minimum_seconds)
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": signature,
        "sha256": sha256_file(path),
        "duration_seconds": round(duration, 3),
        "generation": "deterministic_local_synthesis_no_external_api",
    }


def ensure_track(path, category, generator, duration, previous_tracks):
    signature = digest({
        "version": VERSION,
        "category": category,
        "sample_rate": RATE,
        "duration": duration,
    })
    previous = previous_tracks.get(category, {})

    if path.is_file():
        try:
            record = track_record(path, signature, duration)
            if (
                previous.get("signature") == signature
                and previous.get("sha256") == record["sha256"]
            ):
                log("RESUME:", path.relative_to(ROOT))
                return record
        except (OSError, ValueError, wave.Error, RuntimeError):
            pass

    write_wav(path, generator())
    return track_record(path, signature, duration)


def music_samples():
    count = RATE * BACKGROUND_SECONDS
    roots = (110.0, 130.81, 98.0, 146.83)
    ratios = (1.0, 1.25, 1.5, 2.0)

    for index in range(count):
        t = index / RATE
        root = roots[int(t // 6) % len(roots)]
        fade = min(1.0, t / 2.5, (BACKGROUND_SECONDS - t) / 2.0)
        swell = 0.78 + 0.22 * math.sin(2 * math.pi * t / 12)
        tone = sum(
            0.22 * math.sin(2 * math.pi * root * ratio * t)
            + 0.035 * math.sin(
                2 * math.pi * root * ratio * 2.003 * t
            )
            for ratio in ratios
        )
        yield tone * 0.22 * max(0.0, fade) * swell


def ambient_samples():
    rng = random.Random(271828)
    filtered = 0.0

    for index in range(RATE * BACKGROUND_SECONDS):
        t = index / RATE
        filtered = filtered * 0.96 + rng.uniform(-1, 1) * 0.04
        fade = max(
            0.0,
            min(1.0, t / 1.5, (BACKGROUND_SECONDS - t) / 1.5),
        )
        value = (
            0.08 * math.sin(2 * math.pi * 55 * t)
            + 0.04 * math.sin(2 * math.pi * 82.41 * t + 0.4)
            + filtered * 0.12
        )
        yield value * fade * 0.22


def sfx_samples(kind):
    count = int(RATE * SFX_SECONDS)
    rng = random.Random(9000 + SFX_TYPES.index(kind))
    filtered = 0.0

    for index in range(count):
        t = index / RATE
        p = index / max(1, count - 1)
        fade = max(0.0, math.sin(math.pi * p)) ** 1.5
        filtered = filtered * 0.72 + rng.uniform(-1, 1) * 0.28

        if kind == "whoosh":
            value = (
                filtered * 0.25
                + math.sin(2 * math.pi * (180 + 500 * p) * t) * 0.18
            )
        elif kind == "impact":
            decay = math.exp(-8 * t)
            value = decay * (
                0.65 * math.sin(2 * math.pi * 72 * t)
                + 0.25 * filtered
            )
        elif kind == "mystery":
            value = (
                0.30 * math.sin(2 * math.pi * 220 * t)
                + 0.22 * math.sin(2 * math.pi * 277.18 * t)
                + 0.10 * filtered
            )
        elif kind == "heartbeat":
            beat = max(
                0.0, math.sin(2 * math.pi * 1.6 * t)
            ) ** 10
            value = (
                beat * math.sin(2 * math.pi * 65 * t)
                + filtered * 0.025
            )
        else:
            value = (
                0.40 * math.sin(2 * math.pi * 880 * t)
                + 0.22 * math.sin(2 * math.pi * 1320 * t)
                + 0.10 * math.sin(2 * math.pi * 1760 * t)
            ) * math.exp(-2.6 * t)

        yield value * fade * 0.55


def scene_rows():
    data = read_json(SCENES_PATH)
    if not isinstance(data, dict):
        raise RuntimeError(f"Missing or invalid scenes file: {SCENES_PATH}")

    parts_data = data.get("parts")
    if isinstance(parts_data, list):
        rows = []
        for part_index, part_data in enumerate(parts_data, 1):
            if not isinstance(part_data, dict):
                continue
            try:
                part_number = int(part_data.get("part", part_index))
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"Invalid part number at part index {part_index}"
                )

            scene_list = part_data.get("scenes", [])
            if not isinstance(scene_list, list):
                raise RuntimeError(
                    f"Scenes for part {part_number} must be a list"
                )

            for scene_index, scene_data in enumerate(scene_list, 1):
                if not isinstance(scene_data, dict):
                    continue
                row = dict(scene_data)
                row.setdefault("part", part_number)
                row.setdefault("scene", scene_index)
                rows.append(row)

        if rows:
            return rows

    scene_list = data.get("scenes")
    if isinstance(scene_list, list):
        return [
            row for row in scene_list if isinstance(row, dict)
        ]

    raise RuntimeError("scenes.json contains no supported scene list")


def choose_sfx(scene):
    text = " ".join(
        str(scene.get(key, ""))
        for key in (
            "sfx", "title", "narration", "mood", "suspense_prompt"
        )
    ).casefold()

    groups = [
        ("heartbeat", (
            "heartbeat", "heart beat", "pulse",
            "धड़कन", "धड़कन", "दिल जोर",
        )),
        ("impact", (
            "impact", "crash", "slam", "bang", "explosion",
            "धमाका", "टकराया", "गिर पड़ा", "टूट गया", "गोली",
        )),
        ("chime", (
            "bell", "chime", "ring", "घंटी", "फोन बज",
            "अलार्म", "खुशखबरी",
        )),
        ("mystery", (
            "mystery", "secret", "suspense", "डर", "भूत",
            "रहस्य", "अंधेरा", "साया", "खौफ", "चौंक",
            "अनजान आवाज",
        )),
    ]

    for kind, keywords in groups:
        if any(keyword in text for keyword in keywords):
            return kind

    if any(
        word in text
        for word in ("transition", "दौड़ा", "भागा", "चल पड़ा")
    ):
        return "whoosh"

    return "mystery" if scene.get("suspense_required") else "whoosh"


def narration_durations():
    data = read_json(AUDIO_MANIFEST)
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("jobs") or data.get("audio_jobs") or []
    else:
        raise RuntimeError(
            f"Missing or invalid narration manifest: {AUDIO_MANIFEST}"
        )

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            "Narration manifest has no jobs; run TTS before sound design."
        )

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError(
            "ffprobe is required to verify actual narration durations."
        )

    result = {}
    errors = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError):
            continue

        if row.get("status") != "completed":
            continue

        value = next((
            row.get(name)
            for name in (
                "output", "audio_path", "path", "file", "audio"
            )
            if isinstance(row.get(name), str) and row[name].strip()
        ), None)

        if not value:
            errors.append(f"{part}/{scene}: missing audio path")
            continue

        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        path = path.resolve()

        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            errors.append(f"{part}/{scene}: audio file missing or unsafe")
            continue

        try:
            probe = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            duration = (
                float(probe.stdout.strip())
                if probe.returncode == 0 else 0.0
            )
            if not math.isfinite(duration) or duration <= 0:
                errors.append(f"{part}/{scene}: invalid audio duration")
                continue
        except (OSError, ValueError, subprocess.TimeoutExpired):
            errors.append(f"{part}/{scene}: ffprobe failed")
            continue

        key = (part, scene)
        if key in result:
            errors.append(f"{part}/{scene}: duplicate narration job")
            continue
        result[key] = duration

    if errors:
        raise RuntimeError(
            "Narration validation failed: " + "; ".join(errors[:20])
        )

    return result


def build_cues(rows, parts, scenes_per_part, durations, enabled):
    if not enabled:
        return []

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    scene_map = {}

    for row in rows:
        try:
            part = int(row.get("part"))
            scene_value = row.get("scene", row.get("local_scene"))
            scene = int(scene_value)
        except (TypeError, ValueError):
            continue

        key = (part, scene)
        if key not in expected:
            continue
        if key in scene_map:
            raise RuntimeError(
                f"Duplicate scene mapping: {part}/{scene}"
            )
        scene_map[key] = row

    missing_scenes = sorted(expected - set(scene_map))
    if missing_scenes:
        preview = ", ".join(
            f"{part}/{scene}" for part, scene in missing_scenes[:20]
        )
        raise RuntimeError(
            "Scene list is incomplete; missing mappings: " + preview
        )

    missing_audio = sorted(expected - set(durations))
    if missing_audio:
        preview = ", ".join(
            f"{part}/{scene}" for part, scene in missing_audio[:20]
        )
        raise RuntimeError(
            "Actual narration duration missing for: " + preview
        )

    cues = []
    for part, scene in sorted(expected):
        duration = durations[(part, scene)]
        if duration <= 0:
            raise RuntimeError(
                f"Invalid narration duration for {part}/{scene}"
            )

        # Start at zero: valid even for very short narration clips.
        # Never guess duration or schedule cues after narration ends.
        cue_time = 0.0
        kind = choose_sfx(scene_map[(part, scene)])
        path = SFX_DIR / f"{kind}.wav"

        cues.append({
            "part": part,
            "scene": scene,
            "time": cue_time,
            "file": path.relative_to(ROOT).as_posix(),
            "gain": 0.045 if kind in {"mystery", "heartbeat"} else 0.06,
            "kind": kind,
            "selection_basis": "scene_text_keyword_rules",
            "timing_basis": "ffprobe_actual_narration_duration_verified",
            "narration_duration_seconds": round(duration, 3),
        })

    return cues


def main():
    config = settings()
    parts = positive_int(config, "PARTS", 1)
    scenes_per_part = positive_int(config, "SCENES", 11)

    enabled = {
        "music": as_bool(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
             "SOUNDTRACK_ENABLED"),
        ),
        "ambient": as_bool(
            config,
            ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
             "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED"),
        ),
        "sfx": as_bool(
            config,
            ("SFX_ENABLED", "SFX", "SOUND_EFFECTS",
             "SOUND_EFFECTS_ENABLED"),
        ),
    }

    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    SFX_DIR.mkdir(parents=True, exist_ok=True)

    old_manifest = read_json(MANIFEST_PATH, {})
    if not isinstance(old_manifest, dict):
        old_manifest = {}
    old_tracks = old_manifest.get("tracks", {})
    if not isinstance(old_tracks, dict):
        old_tracks = {}

    tracks = {}
    definitions = {
        "music": (MUSIC_PATH, music_samples, BACKGROUND_SECONDS),
        "ambient": (AMBIENT_PATH, ambient_samples, BACKGROUND_SECONDS),
    }

    for category, (path, generator, duration) in definitions.items():
        if not enabled[category]:
            path.unlink(missing_ok=True)
            tracks[category] = None
            continue
        tracks[category] = ensure_track(
            path, category, generator, duration, old_tracks
        )

    sfx_tracks = {}
    for kind in SFX_TYPES:
        path = SFX_DIR / f"{kind}.wav"
        category = f"sfx_{kind}"

        if not enabled["sfx"]:
            path.unlink(missing_ok=True)
            sfx_tracks[kind] = None
            continue

        signature = digest({
            "version": VERSION,
            "category": category,
            "sample_rate": RATE,
            "duration": SFX_SECONDS,
        })
        old = old_tracks.get(category, {})

        if path.is_file():
            try:
                record = track_record(path, signature, SFX_SECONDS)
                if (
                    old.get("signature") == signature
                    and old.get("sha256") == record["sha256"]
                ):
                    sfx_tracks[kind] = record
                    log("RESUME:", path.relative_to(ROOT))
                    continue
            except (OSError, ValueError, wave.Error, RuntimeError):
                pass

        write_wav(path, sfx_samples(kind))
        sfx_tracks[kind] = track_record(path, signature, SFX_SECONDS)

    # Preserve the legacy track key required by final validation.
    tracks["sfx"] = sfx_tracks["whoosh"] if enabled["sfx"] else None
    for kind, record in sfx_tracks.items():
        tracks[f"sfx_{kind}"] = record

    cues = []
    if enabled["sfx"]:
        rows = scene_rows()
        durations = narration_durations()
        cues = build_cues(
            rows, parts, scenes_per_part, durations, True
        )

        for cue in cues:
            path = (ROOT / cue["file"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
                raise RuntimeError(f"SFX cue asset missing: {path}")
            validate_audio(path, SFX_SECONDS)

            duration = cue["narration_duration_seconds"]
            if not 0 <= cue["time"] < duration:
                raise RuntimeError(
                    "SFX cue outside narration: "
                    f"{cue['part']}/{cue['scene']}"
                )

    write_json(CUES_PATH, {
        "status": "completed",
        "version": VERSION,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "cue_count": len(cues),
        "cue_policy": (
            "Deterministic scene-text selection; locally synthesized SFX."
        ),
        "timing_basis": (
            "Every cue is checked against ffprobe narration duration."
            if enabled["sfx"] else "SFX disabled by input configuration."
        ),
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
    for name in ("music", "ambient", "sfx"):
        log(f"{name.upper()}: {'enabled' if enabled[name] else 'disabled'}")
    log("SFX cue count:", len(cues))
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)
