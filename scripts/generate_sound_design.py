#!/usr/bin/env python3
"""Role-aware sound design with verified TTS timing, safe resume and SFX cues."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

from input_config import load_and_validate, get_parts, get_scenes, get_format

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"

AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"
NARRATION_FILE = OUTPUT / "narration" / "narration.json"
SCENES_FILE = OUTPUT / "scenes" / "scenes.json"
FLOW_MUSIC_MANIFEST = OUTPUT / "music" / "flow_music_manifest.json"

MUSIC_PATH = OUTPUT / "music" / "background_music.wav"
AMBIENT_PATH = OUTPUT / "sound" / "ambient_environment.wav"
SFX_DIR = OUTPUT / "sfx"
DESIGN_DIR = OUTPUT / "sound_design"

MANIFEST_PATH = DESIGN_DIR / "sound_design_manifest.json"
CUES_PATH = DESIGN_DIR / "sfx_cues.json"
CHECKPOINT_PATH = OUTPUT / "checkpoints" / "sound_design_progress.json"

VERSION = "photo-motion-sound-design-v20-verified-role-timeline"
RATE = 22050
SFX_SECONDS = 3.6
SFX_TYPES = ("whoosh", "impact", "mystery", "heartbeat", "chime")
ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}
PATH_KEYS = ("output", "audio_path", "audio_file", "path", "file", "audio")

SFX_GAINS = {
    "whoosh": 0.20,
    "impact": 0.24,
    "mystery": 0.18,
    "heartbeat": 0.20,
    "chime": 0.16,
}

SFX_TIME_FRACTIONS = {
    "whoosh": 0.20,
    "heartbeat": 0.18,
    "impact": 0.48,
    "mystery": 0.32,
    "chime": 0.62,
}


def log(*args):
    print(*args, flush=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value
    except (OSError, ValueError, TypeError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def signature(data):
    raw = json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command, timeout=180):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-2000:]
        raise RuntimeError(
            f"Command failed ({result.returncode}): {detail}"
        )
    return result


def safe_project_path(value):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Audio path is missing.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    root = ROOT.resolve()

    if path != root and root not in path.parents:
        raise RuntimeError("Audio path escapes the project directory.")
    if not path.is_file():
        raise RuntimeError(f"Audio file does not exist: {value}")

    return path


def ffprobe_audio(path):
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required to inspect audio.")

    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ], timeout=90)

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            item.get("codec_type") == "audio"
            for item in data.get("streams", [])
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid audio metadata: {path}") from exc

    if not math.isfinite(duration) or duration <= 0 or not has_audio:
        raise RuntimeError(f"Audio is empty or has no audio stream: {path}")

    return duration


def validate_wav(path, minimum_seconds=0.1):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"WAV missing or too small: {path}")

    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1:
                raise RuntimeError("WAV must be mono.")
            if audio.getsampwidth() != 2:
                raise RuntimeError("WAV must be 16-bit PCM.")
            if audio.getframerate() != RATE:
                raise RuntimeError("WAV sample rate mismatch.")
            frames = audio.getnframes()
            if frames <= 0:
                raise RuntimeError("WAV contains no frames.")
            duration = frames / audio.getframerate()
    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"Invalid WAV: {path}") from exc

    if duration + 1 / RATE < minimum_seconds:
        raise RuntimeError(f"WAV is shorter than required: {path}")

    return duration


def write_wav(path, sample_factory, duration):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".building")
    expected = int(round(RATE * duration))
    count = 0

    try:
        with wave.open(str(temporary), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(RATE)
            block = bytearray()

            for sample in sample_factory(duration):
                if count >= expected:
                    break
                sample = max(-0.90, min(0.90, float(sample)))
                block.extend(struct.pack("<h", int(sample * 32767)))
                count += 1

                if len(block) >= 32768:
                    audio.writeframesraw(block)
                    block.clear()

            if block:
                audio.writeframesraw(block)

            if count != expected:
                raise RuntimeError(
                    f"Audio frame mismatch: {count}/{expected}."
                )

        validate_wav(temporary, min(0.1, duration))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def envelope(t, duration, fade_in=1.0, fade_out=1.5):
    attack = min(1.0, t / max(0.1, fade_in))
    release = min(
        1.0,
        max(0.0, duration - t) / max(0.1, fade_out),
    )
    return attack * release


def music_samples(duration, seed=2718):
    roots = (110.0, 130.81, 98.0, 146.83, 123.47, 92.5)
    phase = random.Random(seed).random() * math.tau

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        root = roots[int(t // 16) % len(roots)]
        pulse = 0.94 + 0.06 * math.sin(
            2 * math.pi * 0.075 * t + phase
        )
        pad = (
            0.22 * math.sin(2 * math.pi * root * t)
            + 0.12 * math.sin(2 * math.pi * root * 1.5 * t)
            + 0.07 * math.sin(2 * math.pi * root * 2.0 * t)
            + 0.045 * math.sin(2 * math.pi * root * 0.5 * t)
        )
        yield pad * pulse * envelope(t, duration, 2.5, 4.0)


def ambient_samples(duration, seed=271828):
    rng = random.Random(seed)
    low_filtered = 0.0

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        noise = rng.uniform(-1.0, 1.0)
        low_filtered = low_filtered * 0.997 + noise * 0.003
        bed = (
            0.075 * math.sin(2 * math.pi * 43 * t)
            + 0.05 * math.sin(2 * math.pi * 61 * t)
            + 0.03 * math.sin(2 * math.pi * 82 * t)
            + 0.010 * low_filtered
        )
        yield bed * envelope(t, duration, 2.0, 3.0)


def sfx_samples(kind, duration=SFX_SECONDS, seed=None):
    count = int(round(RATE * duration))
    rng = random.Random(
        seed if seed is not None else 9000 + SFX_TYPES.index(kind)
    )
    low_noise = 0.0
    smooth_noise = 0.0

    for index in range(count):
        t = index / RATE
        progress = index / max(1, count - 1)
        noise = rng.uniform(-1.0, 1.0)
        low_noise = low_noise * 0.91 + noise * 0.09
        smooth_noise = smooth_noise * 0.996 + noise * 0.004
        fade = min(1.0, t / 0.12) * min(
            1.0, max(0.0, duration - t) / 0.65
        )

        if kind == "whoosh":
            sweep = 90 + 420 * (progress ** 1.15)
            value = (
                0.20 * low_noise
                + 0.20 * math.sin(2 * math.pi * sweep * t)
                + 0.07 * math.sin(2 * math.pi * sweep * 0.5 * t)
            )
        elif kind == "impact":
            body = math.exp(-2.5 * t)
            sub = math.sin(
                2 * math.pi * (45 + 8 * math.exp(-t)) * t
            )
            value = (
                0.48 * body * sub
                + 0.10 * low_noise * math.exp(-4.0 * t)
                + 0.08 * smooth_noise * math.exp(-1.2 * t)
            )
        elif kind == "mystery":
            wobble = 2.5 * math.sin(2 * math.pi * 0.25 * t)
            value = (
                0.20 * math.sin(2 * math.pi * (110 + wobble) * t)
                + 0.14 * math.sin(2 * math.pi * 164.81 * t)
                + 0.06 * smooth_noise
            )
        elif kind == "heartbeat":
            phase = (t * 1.08) % 1.0
            first = math.exp(-((phase - 0.10) / 0.06) ** 2)
            second = math.exp(-((phase - 0.28) / 0.08) ** 2)
            pulse = first * 0.78 + second * 0.46
            value = (
                pulse * math.sin(2 * math.pi * 48 * t)
                + 0.04 * smooth_noise
            )
        else:
            decay = math.exp(-1.15 * t)
            tone = (
                0.25 * math.sin(2 * math.pi * 523.25 * t)
                + 0.16 * math.sin(2 * math.pi * 659.25 * t)
                + 0.09 * math.sin(2 * math.pi * 783.99 * t)
            )
            value = tone * decay

        yield value * fade


def make_track(path, category, factory, duration, old_tracks, source_sig):
    path = Path(path)
    sig = signature({
        "version": VERSION,
        "category": category,
        "sample_rate": RATE,
        "duration": round(duration, 3),
        "source_signature": source_sig,
    })

    previous = old_tracks.get(category, {})
    if not isinstance(previous, dict):
        previous = {}

    if path.is_file():
        try:
            actual = validate_wav(path, duration)
            checksum = sha256_file(path)
            if (
                previous.get("signature") == sig
                and previous.get("sha256") == checksum
            ):
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": sig,
                    "sha256": checksum,
                    "duration_seconds": round(actual, 6),
                    "generation": "local_deterministic_synthesis",
                }
        except (OSError, ValueError, RuntimeError, wave.Error):
            log("Rebuilding invalid audio track:", path)

    log("Generating", category, f"({duration:.1f}s)")
    factory_duration = duration
    write_wav(path, lambda d: factory(factory_duration), duration)
    actual = validate_wav(path, duration)

    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": sig,
        "sha256": sha256_file(path),
        "duration_seconds": round(actual, 6),
        "generation": "local_deterministic_synthesis",
    }


def record_identity(row):
    try:
        part = int(row["part"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Narration audio record has invalid part.") from exc

    role = str(row.get("role", "main")).strip().lower()
    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported audio record role: {role!r}")

    if role == "main":
        try:
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Main audio record has invalid scene.") from exc
        scene_id = str(
            row.get("scene_id")
            or f"part_{part:02d}_scene_{scene:03d}"
        ).strip()
    else:
        scene = None
        scene_id = str(
            row.get("scene_id") or f"part_{part:02d}_{role}"
        ).strip()

    if part < 1 or not scene_id:
        raise RuntimeError("Invalid narration record identity.")

    return scene_id, part, role, scene


def load_audio_records():
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("TTS audio manifest is missing or incomplete.")

    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("TTS audio manifest contains no jobs.")

    result = {}

    for row in jobs:
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError("TTS manifest contains an incomplete audio job.")

        scene_id, part, role, scene = record_identity(row)
        if scene_id in result:
            raise RuntimeError(f"Duplicate TTS audio scene_id: {scene_id}")

        audio_value = next(
            (
                row.get(key)
                for key in PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        if not audio_value:
            raise RuntimeError(f"Audio path missing for {scene_id}.")

        path = safe_project_path(audio_value)
        duration = ffprobe_audio(path)
        checksum = sha256_file(path)

        if row.get("sha256") != checksum:
            raise RuntimeError(f"Audio checksum missing or mismatched: {scene_id}")

        declared = row.get(
            "actual_audio_duration_seconds",
            row.get("duration"),
        )
        if declared is None:
            raise RuntimeError(
                f"Actual audio duration missing for {scene_id}."
            )

        try:
            declared = float(declared)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid declared duration for {scene_id}."
            ) from exc

        if abs(declared - duration) > 0.20:
            raise RuntimeError(
                f"Measured duration mismatch for {scene_id}."
            )

        if row.get("duration_source") not in (
            "ffprobe_measured", None
        ) and row.get("audio_duration_source") not in (
            "ffprobe_measured", None
        ):
            raise RuntimeError(
                f"Duration is not marked as ffprobe-measured: {scene_id}"
            )

        result[scene_id] = {
            "scene_id": scene_id,
            "part": part,
            "role": role,
            "scene": scene,
            "duration": duration,
            "path": path,
            "path_relative": path.relative_to(ROOT).as_posix(),
            "sha256": checksum,
            "text": str(row.get("text", row.get("narration", ""))),
            "provider": row.get("provider"),
            "voice": row.get("voice"),
            "tts_fingerprint": row.get("fingerprint"),
        }

    if not result:
        raise RuntimeError("No valid completed TTS audio records found.")

    return result


def ordered_audio_records(records):
    return sorted(
        records.values(),
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
            row["scene"] or 0,
            row["scene_id"],
        ),
    )


def validate_record_structure(ordered, expected_parts, scenes_per_part):
    if not ordered:
        raise RuntimeError("No narration records are available.")

    parts = sorted({row["part"] for row in ordered})
    if len(parts) != expected_parts:
        raise RuntimeError(
            f"TTS part count mismatch: expected {expected_parts}, "
            f"found {len(parts)}."
        )

    if parts != list(range(parts[0], parts[0] + expected_parts)):
        raise RuntimeError("Narration part numbering is not continuous.")

    by_part = {}
    for row in ordered:
        by_part.setdefault(row["part"], []).append(row)

    final_part = parts[-1]

    for part in parts:
        rows = by_part[part]
        roles = [row["role"] for row in rows]

        if roles.count("hook") != 1:
            raise RuntimeError(f"Part {part} must have exactly one Hook.")
        if part != final_part and roles.count("suspense") != 1:
            raise RuntimeError(
                f"Non-final Part {part} must have exactly one Suspense."
            )
        if part == final_part and roles.count("resolution") != 1:
            raise RuntimeError(
                "Final part must have exactly one Resolution."
            )
        if part == final_part and "suspense" in roles:
            raise RuntimeError("Final part must not contain Suspense.")
        if part != final_part and "resolution" in roles:
            raise RuntimeError(
                f"Resolution is not allowed before final Part {final_part}."
            )

        main_scenes = sorted(
            row["scene"] for row in rows if row["role"] == "main"
        )
        if main_scenes != list(range(1, scenes_per_part + 1)):
            raise RuntimeError(
                f"Part {part} Main scene list does not match "
                f"configured SCENES={scenes_per_part}: {main_scenes}"
            )

    expected_count = (
        expected_parts * scenes_per_part
        + expected_parts
        + max(0, expected_parts - 1)
        + 1
    )
    if len(ordered) != expected_count:
        raise RuntimeError(
            f"Audio record count mismatch: expected {expected_count}, "
            f"found {len(ordered)}."
        )


def load_scene_records():
    data = read_json(SCENES_FILE, {})
    if not isinstance(data, dict):
        raise RuntimeError("scenes.json is missing or invalid.")

    rows = data.get("scenes")
    if not isinstance(rows, list):
        rows = []

    if not rows and isinstance(data.get("parts"), list):
        for part_index, part_data in enumerate(data["parts"], 1):
            if not isinstance(part_data, dict):
                continue

            part = int(part_data.get("part", part_index))

            for scene_index, item in enumerate(
                part_data.get("scenes", []), 1
            ):
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("part", part)
                    row.setdefault("scene", scene_index)
                    row.setdefault("role", "main")
                    rows.append(row)

            for role in ("hook", "suspense", "resolution"):
                item = part_data.get(f"{role}_scene")
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("part", part)
                    row.setdefault("role", role)
                    row.setdefault("scene_id", f"part_{part:02d}_{role}")
                    rows.append(row)

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        role = str(row.get("role", "main")).strip().lower()
        if role not in ROLE_ORDER:
            continue

        try:
            part = int(row["part"])
        except (KeyError, TypeError, ValueError):
            continue

        scene = None
        if role == "main":
            try:
                scene = int(row.get("scene", row.get("local_scene")))
            except (TypeError, ValueError):
                continue

        scene_id = str(
            row.get("scene_id")
            or (
                f"part_{part:02d}_scene_{scene:03d}"
                if role == "main"
                else f"part_{part:02d}_{role}"
            )
        ).strip()

        if scene_id in result:
            raise RuntimeError(f"Duplicate scene record: {scene_id}")

        result[scene_id] = row

    return result


def choose_sfx(row, audio):
    text = " ".join(
        str(value or "")
        for value in (
            row.get("sfx"),
            row.get("sfx_prompt"),
            row.get("title"),
            row.get("narration"),
            row.get("text"),
            row.get("mood"),
            row.get("visual_prompt"),
            row.get("beat_description"),
            row.get("suspense_prompt"),
            audio.get("text"),
            row.get("role"),
        )
    ).casefold()

    groups = [
        ("heartbeat", (
            "heartbeat", "heart beat", "pulse", "धड़कन", "धड़कन",
            "कांप रहा", "कांप रही", "सांसें तेज", "दिल तेजी",
        )),
        ("impact", (
            "impact", "crash", "slam", "bang", "explosion",
            "धमाका", "टकराया", "गिर पड़ा", "टूट गया", "गोली",
            "दरवाजा तोड़", "विस्फोट", "जोरदार टक्कर",
        )),
        ("chime", (
            "bell", "chime", "ring", "घंटी", "फोन बज",
            "अलार्म", "संदेश आया", "notification",
        )),
        ("mystery", (
            "mystery", "secret", "suspense", "डर", "भूत",
            "रहस्य", "अंधेरा", "साया", "खौफ", "चौंक",
            "अनजान आवाज", "परछाई", "गायब", "रहस्यमय",
        )),
    ]

    for kind, keywords in groups:
        if any(word in text for word in keywords):
            return kind

    role = str(row.get("role", audio.get("role", "main"))).lower()

    if role in {"hook", "suspense"}:
        return "mystery"

    if any(
        word in text
        for word in (
            "transition", "दौड़ा", "भागा", "भाग रही",
            "दरवाजा खुला", "तेजी से मुड़ा", "चल पड़ा",
        )
    ):
        return "whoosh"

    if role == "resolution":
        return "chime"

    return "whoosh"


def create_cue(audio, scene_row, track_records):
    kind = choose_sfx(scene_row or {}, audio)
    duration = audio["duration"]
    start = min(
        duration * SFX_TIME_FRACTIONS[kind],
        max(0.0, duration - 0.25),
    )
    track = track_records.get(f"sfx_{kind}")

    if not isinstance(track, dict) or track.get("status") != "completed":
        raise RuntimeError(f"Required SFX track is unavailable: {kind}")

    cue_duration = min(SFX_SECONDS, max(0.1, duration - start))

    return {
        "scene_id": audio["scene_id"],
        "part": audio["part"],
        "scene": audio["scene"],
        "role": audio["role"],
        "kind": kind,
        "time": round(start, 3),
        "start_seconds": round(start, 3),
        "duration_seconds": round(cue_duration, 3),
        "file": track["path"],
        "sha256": track["sha256"],
        "gain": SFX_GAINS[kind],
        "selection_basis": "story_scene_narration_and_role",
        "timing_basis": "measured_tts_audio_duration",
        "narration_duration_seconds": round(duration, 3),
        "narration_file": audio["path_relative"],
        "narration_sha256": audio["sha256"],
    }


def validate_track(record, category, minimum_duration):
    if not isinstance(record, dict) or record.get("status") != "completed":
        raise RuntimeError(f"{category} track is not completed.")

    path = safe_project_path(record.get("path"))
    duration = ffprobe_audio(path)
    checksum = sha256_file(path)

    if record.get("sha256") != checksum:
        raise RuntimeError(f"{category} track checksum mismatch.")
    if duration + 0.5 < minimum_duration:
        raise RuntimeError(f"{category} track is shorter than narration.")

    record["duration_seconds"] = round(duration, 6)
    return record


def build_timeline(ordered):
    offset = 0.0
    timeline = []

    for row in ordered:
        item = {
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "start_seconds": round(offset, 4),
            "duration_seconds": round(row["duration"], 4),
            "end_seconds": round(offset + row["duration"], 4),
            "audio": row["path_relative"],
            "audio_sha256": row["sha256"],
        }
        timeline.append(item)
        offset += row["duration"]

    return timeline, round(offset, 4)


def load_previous_tracks():
    old = read_json(MANIFEST_PATH, {})
    if not isinstance(old, dict):
        return {}
    tracks = old.get("tracks", {})
    return tracks if isinstance(tracks, dict) else {}


def enabled(config, *keys, default=True):
    for key in keys:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "yes", "1", "on", "enabled"}:
            return True
        if text in {"false", "no", "0", "off", "disabled"}:
            return False
        raise RuntimeError(f"{key} must be true or false.")
    return default


def load_flow_music(background_duration):
    """Accept only a completed Flow music artifact with a matching hash."""
    data = read_json(FLOW_MUSIC_MANIFEST, {})
    if not isinstance(data, dict) or data.get("status") != "completed":
        return None

    try:
        source_value = data.get("path") or data.get("audio_path")
        source = safe_project_path(source_value)
        source_hash = sha256_file(source)

        if data.get("sha256") != source_hash:
            raise RuntimeError("Flow music checksum missing or mismatched.")

        source_duration = ffprobe_audio(source)
        declared = data.get("duration_seconds")

        if declared is not None and abs(float(declared) - source_duration) > 2:
            raise RuntimeError("Flow music duration metadata mismatch.")

        temporary = MUSIC_PATH.with_name("background_music.building.wav")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)

        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(source),
            "-t", f"{background_duration:.3f}",
            "-vn",
            "-af",
            (
                "aresample=22050,"
                "lowpass=f=9000,"
                "loudnorm=I=-27:TP=-3.0:LRA=11,"
                f"afade=t=out:st={max(0.0, background_duration - 4):.3f}:d=4"
            ),
            "-ac", "1", "-ar", str(RATE),
            "-c:a", "pcm_s16le", str(temporary),
        ], timeout=900)

        try:
            duration = validate_wav(temporary, background_duration)
            os.replace(temporary, MUSIC_PATH)
        finally:
            temporary.unlink(missing_ok=True)

        return {
            "status": "completed",
            "path": MUSIC_PATH.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(MUSIC_PATH),
            "duration_seconds": round(duration, 6),
            "generation": "google_flow_music",
            "provider": data.get("provider", "google_flow"),
            "model": data.get("model"),
            "source_audio": source.relative_to(ROOT).as_posix(),
            "source_sha256": source_hash,
            "source_manifest": FLOW_MUSIC_MANIFEST.relative_to(ROOT).as_posix(),
            "source_fingerprint": data.get("source_fingerprint"),
            "paid_services_used": bool(data.get("paid_services_used", True)),
            "external_api_calls": data.get("external_api_calls", 1),
        }
    except Exception as exc:
        log("FLOW MUSIC REJECTED:", str(exc))
        return None

def generate_tracks(enabled_tracks, total_duration, old_tracks, source_sig):
    background_duration = max(30.0, total_duration + 8.0)
    tracks = {}

    if not enabled_tracks["music"]:
        tracks["music"] = None
        log("Music disabled by Input.")
    else:
        flow_record = load_flow_music(background_duration)

        if flow_record:
            tracks["music"] = validate_track(
                flow_record,
                "music",
                total_duration,
            )
            log("Using verified Google Flow/Lyria music.")
        else:
            tracks["music"] = make_track(
                MUSIC_PATH,
                "music",
                music_samples,
                background_duration,
                old_tracks,
                source_sig,
            )
            tracks["music"].update({
                "generation": "local_fallback",
                "provider": "local_synthesis",
                "model": "deterministic_cinematic_pad",
            })

    if not enabled_tracks["ambient"]:
        tracks["ambient"] = None
        log("Ambient sound disabled by Input.")
    else:
        tracks["ambient"] = make_track(
            AMBIENT_PATH,
            "ambient",
            ambient_samples,
            background_duration,
            old_tracks,
            source_sig,
        )

    for kind in SFX_TYPES:
        category = f"sfx_{kind}"
        path = SFX_DIR / f"{kind}.wav"

        if not enabled_tracks["sfx"]:
            tracks[category] = None
            continue

        tracks[category] = make_track(
            path,
            category,
            lambda duration, selected=kind: sfx_samples(
                selected,
                duration,
            ),
            SFX_SECONDS,
            old_tracks,
            source_sig,
        )

    tracks["sfx"] = tracks.get("sfx_whoosh")
    return tracks, background_duration


def validate_all_tracks(tracks, total_duration, enabled_tracks):
    for category in ("music", "ambient"):
        record = tracks.get(category)

        if enabled_tracks[category]:
            if record is None:
                raise RuntimeError(f"{category} is enabled but missing.")
            tracks[category] = validate_track(
                record,
                category,
                total_duration,
            )
        elif record is not None:
            raise RuntimeError(
                f"{category} is disabled but an active track was recorded."
            )

    for kind in SFX_TYPES:
        category = f"sfx_{kind}"
        record = tracks.get(category)

        if enabled_tracks["sfx"]:
            if record is None:
                raise RuntimeError(f"SFX {kind} is enabled but missing.")
            tracks[category] = validate_track(
                record,
                category,
                0.1,
            )
        elif record is not None:
            raise RuntimeError(
                f"SFX {kind} is disabled but a track was recorded."
            )


def build_cues(ordered, scene_records, tracks, enabled_sfx):
    if not enabled_sfx:
        return []

    cues = []

    for audio in ordered:
        scene_row = scene_records.get(audio["scene_id"], {})
        cues.append(create_cue(audio, scene_row, tracks))

    return cues


def validate_cues(cues, ordered, enabled_sfx):
    if not enabled_sfx:
        if cues:
            raise RuntimeError("SFX is disabled but cues were generated.")
        return

    expected_ids = [row["scene_id"] for row in ordered]
    actual_ids = [cue.get("scene_id") for cue in cues]

    if actual_ids != expected_ids:
        raise RuntimeError(
            "SFX cues do not map one-to-one to ordered narration records."
        )

    for cue, audio in zip(cues, ordered):
        if cue.get("timing_basis") != "measured_tts_audio_duration":
            raise RuntimeError(
                f"Invalid SFX timing basis: {cue.get('scene_id')}"
            )

        if cue.get("narration_sha256") != audio["sha256"]:
            raise RuntimeError(
                f"SFX cue narration hash mismatch: {cue.get('scene_id')}"
            )

        start = float(cue["start_seconds"])
        duration = float(cue["duration_seconds"])

        if start < 0 or duration <= 0:
            raise RuntimeError(
                f"Invalid SFX cue timing: {cue.get('scene_id')}"
            )

        if start + duration > audio["duration"] + 0.05:
            raise RuntimeError(
                f"SFX cue exceeds narration duration: {cue.get('scene_id')}"
            )


def build_part_summary(ordered, cues):
    cue_map = {cue["scene_id"]: cue for cue in cues}
    result = {}

    for row in ordered:
        part_key = str(row["part"])
        part = result.setdefault(part_key, {
            "part": row["part"],
            "records": [],
            "role_counts": {
                "hook": 0,
                "main": 0,
                "suspense": 0,
                "resolution": 0,
            },
            "duration_seconds": 0.0,
            "sfx_cues": [],
        })

        part["records"].append({
            "scene_id": row["scene_id"],
            "scene": row["scene"],
            "role": row["role"],
            "duration_seconds": round(row["duration"], 4),
            "audio": row["path_relative"],
            "audio_sha256": row["sha256"],
        })

        part["role_counts"][row["role"]] += 1
        part["duration_seconds"] += row["duration"]

        if row["scene_id"] in cue_map:
            part["sfx_cues"].append(cue_map[row["scene_id"]])

    for part in result.values():
        part["duration_seconds"] = round(
            part["duration_seconds"],
            4,
        )

    return result


def main():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_FILE)

    fmt = str(get_format(config)).strip().lower()
    expected_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if fmt not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {fmt}")

    if expected_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    enabled_tracks = {
        "music": enabled(
            config,
            "MUSIC_ENABLED",
            "MUSIC",
            "BACKGROUND_MUSIC",
            "SOUNDTRACK_ENABLED",
            default=True,
        ),
        "ambient": enabled(
            config,
            "AMBIENT_ENABLED",
            "AMBIENT",
            "AMBIENT_SOUND",
            "ENVIRONMENT_SOUND",
            "AMBIENCE_ENABLED",
            default=True,
        ),
        "sfx": enabled(
            config,
            "SFX_ENABLED",
            "SFX",
            "SOUND_EFFECTS",
            "SOUND_EFFECTS_ENABLED",
            default=True,
        ),
    }

    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "music").mkdir(parents=True, exist_ok=True)
    (OUTPUT / "sound").mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Both ffmpeg and ffprobe are required.")

    log("=" * 64)
    log("KATHA LOK AI - SOUND DESIGN")
    log("=" * 64)
    log("FORMAT:", fmt)
    log("CONFIGURED PARTS:", expected_parts)
    log("SCENES PER PART:", scenes_per_part)
    log("MUSIC:", enabled_tracks["music"])
    log("AMBIENT:", enabled_tracks["ambient"])
    log("SFX:", enabled_tracks["sfx"])

    audio_records = load_audio_records()
    ordered = ordered_audio_records(audio_records)

    validate_record_structure(
        ordered,
        expected_parts,
        scenes_per_part,
    )

    actual_parts = sorted({row["part"] for row in ordered})
    scene_records = load_scene_records()

    # TTS files, measured durations and SHA256 values form the source identity.
    source_payload = {
        "version": VERSION,
        "format": fmt,
        "parts": actual_parts,
        "scenes_per_part": scenes_per_part,
        "enabled": enabled_tracks,
        "audio_records": [
            {
                "scene_id": row["scene_id"],
                "part": row["part"],
                "scene": row["scene"],
                "role": row["role"],
                "duration": round(row["duration"], 4),
                "sha256": row["sha256"],
                "tts_fingerprint": row["tts_fingerprint"],
            }
            for row in ordered
        ],
        "scene_manifest_sha256": (
            sha256_file(SCENES_FILE)
            if SCENES_FILE.is_file()
            else None
        ),
    }
    source_sig = signature(source_payload)

    timeline, total_duration = build_timeline(ordered)
    old_tracks = load_previous_tracks()

    tracks, background_duration = generate_tracks(
        enabled_tracks,
        total_duration,
        old_tracks,
        source_sig,
    )

    validate_all_tracks(
        tracks,
        total_duration,
        enabled_tracks,
    )

    cues = build_cues(
        ordered,
        scene_records,
        tracks,
        enabled_tracks["sfx"],
    )

    validate_cues(
        cues,
        ordered,
        enabled_tracks["sfx"],
    )

    scene_cue_map = {
        cue["scene_id"]: cue
        for cue in cues
    }

    part_summary = build_part_summary(ordered, cues)

    role_counts = {
        role: sum(
            1 for row in ordered
            if row["role"] == role
        )
        for role in ROLE_ORDER
    }

    main_scene_counts = {
        str(part): sum(
            1 for row in ordered
            if row["part"] == part and row["role"] == "main"
        )
        for part in actual_parts
    }

    flow_data = read_json(FLOW_MUSIC_MANIFEST, {})
    if not isinstance(flow_data, dict):
        flow_data = {}

    music_record = tracks.get("music")
    using_flow = (
        isinstance(music_record, dict)
        and music_record.get("generation") == "google_flow_music"
    )

    external_api_calls = 0
    if using_flow:
        try:
            external_api_calls = max(
                0,
                int(
                    music_record.get(
                        "external_api_calls",
                        flow_data.get("external_api_calls", 1),
                    )
                ),
            )
        except (TypeError, ValueError):
            external_api_calls = 1

    paid_services_used = bool(
        using_flow
        and music_record.get("paid_services_used", True)
    )

    cue_manifest = {
        "status": "completed",
        "version": VERSION,
        "format": fmt,
        "dynamic_parts": True,
        "part_numbers": actual_parts,
        "parts": len(actual_parts),
        "main_scene_counts": main_scene_counts,
        "role_counts": role_counts,
        "total_audio_records": len(ordered),
        "cue_count": len(cues),
        "cues": cues,
        "scene_cues": scene_cue_map,
        "part_cues": {
            key: value["sfx_cues"]
            for key, value in part_summary.items()
        },
        "timing_basis": "Measured TTS audio duration from ffprobe.",
        "timeline": timeline,
        "source_signature": source_sig,
        "source_audio_manifest_sha256": sha256_file(AUDIO_MANIFEST),
        "sfx_policy": (
            "Story-aware cues selected from scene text, narration and "
            "Hook/Suspense/Resolution role. All cue timing is based on "
            "the measured duration of the matching narration audio."
        ),
        "external_api_calls": 0,
        "paid_services_used": False,
    }

    write_json(CUES_PATH, cue_manifest)

    tracks_signature = signature({
        "version": VERSION,
        "source_signature": source_sig,
        "enabled": enabled_tracks,
        "tracks": {
            key: (
                value.get("sha256")
                if isinstance(value, dict)
                else None
            )
            for key, value in tracks.items()
        },
        "cues": cues,
    })

    manifest = {
        "status": "completed",
        "version": VERSION,
        "generation": (
            "google_flow_music_with_local_ambient_and_sfx"
            if using_flow
            else "local_synthesis_with_verified_flow_fallback"
        ),
        "external_api_calls": external_api_calls,
        "paid_services_used": paid_services_used,
        "sample_rate": RATE,
        "format": fmt,
        "dynamic_parts": True,
        "parts": len(actual_parts),
        "part_numbers": actual_parts,
        "main_scene_counts": main_scene_counts,
        "role_counts": role_counts,
        "total_audio_records": len(ordered),
        "total_narration_seconds": round(total_duration, 4),
        "background_duration_seconds": round(background_duration, 3),
        "sfx_duration_seconds": SFX_SECONDS,
        "enabled": enabled_tracks,
        "tracks": tracks,
        "timeline": timeline,
        "part_summary": part_summary,
        "sfx_cues": cues,
        "sfx_cue_map": scene_cue_map,
        "flow_music_manifest": (
            FLOW_MUSIC_MANIFEST.relative_to(ROOT).as_posix()
        ),
        "music_provider": (
            "Google Flow/Lyria"
            if using_flow
            else "local_synthesis_fallback"
            if enabled_tracks["music"]
            else "disabled"
        ),
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
        "audio_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "narration_manifest": NARRATION_FILE.relative_to(ROOT).as_posix(),
        "scene_manifest": SCENES_FILE.relative_to(ROOT).as_posix(),
        "source_audio_manifest_sha256": sha256_file(AUDIO_MANIFEST),
        "source_narration_sha256": (
            sha256_file(NARRATION_FILE)
            if NARRATION_FILE.is_file()
            else None
        ),
        "source_scene_sha256": (
            sha256_file(SCENES_FILE)
            if SCENES_FILE.is_file()
            else None
        ),
        "source_signature": source_sig,
        "sound_design_signature": tracks_signature,
        "mixing_note": (
            "Music and ambience cover the complete measured narration "
            "timeline plus an eight-second tail. SFX cues are mapped to "
            "each Hook, Main, Suspense and Resolution narration record. "
            "Final narration ducking and audio mixing are performed by "
            "render_video.py."
        ),
        "completed_at": utc_now(),
    }

    # Read-after-write validation prevents a partial manifest being treated
    # as a completed sound-design stage.
    write_json(MANIFEST_PATH, manifest)

    saved_manifest = read_json(MANIFEST_PATH)
    saved_cues = read_json(CUES_PATH)

    if not isinstance(saved_manifest, dict):
        raise RuntimeError("Sound design manifest failed read-back.")
    if saved_manifest.get("status") != "completed":
        raise RuntimeError("Sound design manifest is not completed.")
    if saved_manifest.get("sound_design_signature") != tracks_signature:
        raise RuntimeError("Sound design manifest signature mismatch.")
    if not isinstance(saved_cues, dict):
        raise RuntimeError("SFX cue manifest failed read-back.")
    if saved_cues.get("source_signature") != source_sig:
        raise RuntimeError("SFX cue source signature mismatch.")
    if len(saved_cues.get("cues", [])) != len(cues):
        raise RuntimeError("SFX cue count mismatch after write.")

    write_json(CHECKPOINT_PATH, {
        "status": "completed",
        "version": VERSION,
        "source_signature": source_sig,
        "sound_design_signature": tracks_signature,
        "total_audio_records": len(ordered),
        "completed_tracks": [
            key for key, value in tracks.items()
            if isinstance(value, dict)
            and value.get("status") == "completed"
        ],
        "cue_count": len(cues),
        "updated_at": utc_now(),
    })

    log("=" * 64)
    log("SOUND DESIGN READY")
    log("FORMAT:", fmt)
    log("MUSIC PROVIDER:", manifest["music_provider"])
    log("PARTS:", len(actual_parts))
    log("MAIN SCENES:", sum(main_scene_counts.values()))
    log("HOOK:", role_counts["hook"])
    log("SUSPENSE:", role_counts["suspense"])
    log("RESOLUTION:", role_counts["resolution"])
    log("TOTAL AUDIO RECORDS:", len(ordered))
    log("NARRATION DURATION:", round(total_duration, 2), "seconds")
    log("MUSIC/AMBIENT DURATION:", round(background_duration, 2), "seconds")
    log("SFX CUES:", len(cues))
    log("MANIFEST:", MANIFEST_PATH.relative_to(ROOT))
    log("=" * 64)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("SOUND DESIGN INTERRUPTED.")
        raise SystemExit(130)
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        raise SystemExit(1)