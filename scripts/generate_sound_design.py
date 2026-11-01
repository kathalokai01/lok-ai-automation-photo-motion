#!/usr/bin/env python3
"""Generate resumable music, ambience and story-aware sound effects."""

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
OUTPUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"

MUSIC_PATH = OUTPUT / "music" / "background_music.wav"
FLOW_MUSIC_MANIFEST = OUTPUT / "music" / "flow_music_manifest.json"
AMBIENT_PATH = OUTPUT / "sound" / "ambient_environment.wav"
SFX_DIR = OUTPUT / "sfx"
DESIGN_DIR = OUTPUT / "sound_design"

MANIFEST_PATH = DESIGN_DIR / "sound_design_manifest.json"
CUES_PATH = DESIGN_DIR / "sfx_cues.json"
SCENES_PATH = OUTPUT / "scenes" / "scenes.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"

VERSION = "photo-motion-sound-design-v18"
RATE = 22050
SFX_SECONDS = 3.6
SFX_TYPES = ("whoosh", "impact", "mystery", "heartbeat", "chime")
PATH_KEYS = ("output", "audio_path", "path", "file", "audio")

SFX_GAINS = {
    "whoosh": 0.42,
    "impact": 0.48,
    "mystery": 0.38,
    "heartbeat": 0.42,
    "chime": 0.40,
}

SFX_TIME_FRACTIONS = {
    "whoosh": 0.22,
    "heartbeat": 0.20,
    "impact": 0.48,
    "mystery": 0.32,
    "chime": 0.62,
}


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def signature(data):
    raw = json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = {}

    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                value = value[1:-1]

        config[key.strip().upper()] = value

    return config


def enabled(config, keys, default=True):
    for key in keys:
        if key not in config:
            continue

        value = str(config[key]).strip().lower()

        if value in {"true", "yes", "1", "on", "enabled"}:
            return True

        if value in {"false", "no", "0", "off", "disabled"}:
            return False

        raise RuntimeError(f"{key} must be true or false.")

    return default


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
    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        raise RuntimeError("ffprobe is required to inspect audio.")

    process = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if process.returncode:
        raise RuntimeError(
            "ffprobe failed: " + process.stderr[-800:]
        )

    try:
        info = json.loads(process.stdout)
        duration = float(info["format"]["duration"])
        has_audio = any(
            stream.get("codec_type") == "audio"
            for stream in info.get("streams", [])
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Invalid audio metadata.") from exc

    if not math.isfinite(duration) or duration <= 0 or not has_audio:
        raise RuntimeError("Audio is empty or has no audio stream.")

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
                raise RuntimeError("WAV must use 16-bit samples.")

            if audio.getframerate() != RATE:
                raise RuntimeError("WAV sample rate mismatch.")

            frames = audio.getnframes()

            if frames <= 0:
                raise RuntimeError("WAV contains no frames.")

            duration = frames / audio.getframerate()

    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"Invalid WAV file: {path}") from exc

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

                sample = max(-0.92, min(0.92, float(sample)))
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
        temporary.replace(path)

    finally:
        temporary.unlink(missing_ok=True)


def envelope(t, duration, fade_in=1.0, fade_out=1.5):
    attack = min(1.0, t / max(0.1, fade_in))
    release = min(
        1.0,
        max(0.0, duration - t) / max(0.1, fade_out),
    )
    return attack * release


def music_samples(duration):
    """Low-density evolving cinematic pad that leaves room for narration."""
    roots = (110.0, 130.81, 98.0, 146.83, 123.47, 92.5)
    pulse_rates = (0.07, 0.09, 0.06, 0.08)

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        section = int(t // 16)
        root = roots[section % len(roots)]
        pulse_rate = pulse_rates[section % len(pulse_rates)]

        pulse = 0.90 + 0.10 * math.sin(
            2 * math.pi * pulse_rate * t
        )

        pad = (
            0.34 * math.sin(2 * math.pi * root * t)
            + 0.19 * math.sin(2 * math.pi * root * 1.5 * t)
            + 0.12 * math.sin(2 * math.pi * root * 2.0 * t)
            + 0.08 * math.sin(2 * math.pi * root * 0.5 * t)
        )

        harmonic = 0.035 * math.sin(
            2 * math.pi * root * 2.01 * t
        )

        yield (pad + harmonic) * pulse * envelope(
            t, duration, 2.5, 4.0
        )


def ambient_samples(duration):
    """Soft non-verbal atmospheric texture."""
    rng = random.Random(271828)
    low_filtered = 0.0
    high_filtered = 0.0

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        noise = rng.uniform(-1.0, 1.0)

        low_filtered = low_filtered * 0.994 + noise * 0.006
        high_filtered = noise - low_filtered

        low = (
            0.12 * math.sin(2 * math.pi * 47 * t)
            + 0.08 * math.sin(2 * math.pi * 69.3 * t)
            + 0.04 * math.sin(2 * math.pi * 93 * t)
        )

        texture = low_filtered * 0.20 + high_filtered * 0.018

        yield (low + texture) * envelope(
            t, duration, 2.0, 3.0
        )


def sfx_samples(kind):
    count = int(round(RATE * SFX_SECONDS))
    rng = random.Random(9000 + SFX_TYPES.index(kind))
    filtered = 0.0
    filtered_slow = 0.0

    for index in range(count):
        t = index / RATE
        progress = index / max(1, count - 1)

        noise = rng.uniform(-1.0, 1.0)
        filtered = filtered * 0.78 + noise * 0.22
        filtered_slow = filtered_slow * 0.992 + noise * 0.008

        fade_in = min(1.0, t / 0.10)
        fade_out = min(1.0, max(0.0, SFX_SECONDS - t) / 0.55)
        fade = fade_in * fade_out

        if kind == "whoosh":
            sweep = 110 + 850 * (progress ** 1.25)
            value = (
                0.48 * filtered
                + 0.28 * math.sin(2 * math.pi * sweep * t)
                + 0.10 * math.sin(2 * math.pi * sweep * 1.7 * t)
            )

        elif kind == "impact":
            body = math.exp(-2.2 * t)
            sub = math.sin(
                2 * math.pi * (48 + 10 * math.exp(-t)) * t
            )
            crack = filtered * math.exp(-5.0 * t)
            rumble = filtered_slow * math.exp(-1.2 * t)
            value = 0.62 * body * sub + 0.24 * crack + 0.16 * rumble

        elif kind == "mystery":
            wobble = 5.0 * math.sin(2 * math.pi * 0.32 * t)
            drone = (
                0.30 * math.sin(2 * math.pi * (146.83 + wobble) * t)
                + 0.20 * math.sin(2 * math.pi * 196 * t)
                + 0.12 * math.sin(2 * math.pi * 220 * t)
            )
            value = drone + 0.12 * filtered + 0.06 * filtered_slow

        elif kind == "heartbeat":
            phase = (t * 1.15) % 1.0
            beat_one = math.exp(-((phase - 0.10) / 0.055) ** 2)
            beat_two = math.exp(-((phase - 0.28) / 0.075) ** 2)
            pulse = beat_one * 0.85 + beat_two * 0.52
            value = (
                pulse * math.sin(2 * math.pi * 54 * t)
                + 0.10 * filtered_slow
            )

        else:
            decay = math.exp(-0.95 * t)
            shimmer = (
                0.35 * math.sin(2 * math.pi * 880 * t)
                + 0.22 * math.sin(2 * math.pi * 1320 * t)
                + 0.14 * math.sin(2 * math.pi * 1760 * t)
            )
            echo = 0.12 * math.sin(
                2 * math.pi * 660 * t
            ) * math.exp(-1.4 * t)
            value = shimmer * decay + echo

        yield value * fade


def make_track(path, category, factory, duration, old_tracks):
    sig = signature({
        "version": VERSION,
        "category": category,
        "sample_rate": RATE,
        "duration": round(duration, 3),
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
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": sig,
                    "sha256": checksum,
                    "duration_seconds": round(actual_duration, 6),
                    "generation": "local_deterministic_synthesis",
                }

        except (OSError, ValueError, RuntimeError, wave.Error):
            log("Rebuilding invalid track:", path)

    log("Generating local", category, f"({duration:.1f}s)")
    write_wav(path, factory, duration)

    actual_duration = validate_wav(path, duration)

    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": sig,
        "sha256": sha256_file(path),
        "duration_seconds": round(actual_duration, 6),
        "generation": "local_deterministic_synthesis",
    }


def load_flow_music(background_duration):
    """Use Lyria only when its manifest and audio checksum are valid."""
    data = read_json(FLOW_MUSIC_MANIFEST, {})

    if not isinstance(data, dict) or data.get("status") != "completed":
        return None

    try:
        source = safe_project_path(data.get("path"))
        actual_hash = sha256_file(source)

        if data.get("sha256") and data["sha256"] != actual_hash:
            raise RuntimeError("Lyria source checksum mismatch.")

        source_duration = ffprobe_audio(source)

        declared = data.get("duration_seconds")
        if declared is not None:
            declared = float(declared)
            if (
                not math.isfinite(declared)
                or abs(declared - source_duration) > 2.0
            ):
                raise RuntimeError("Lyria duration metadata mismatch.")

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for Lyria music.")

        temporary = MUSIC_PATH.with_name("background_music.building.wav")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)

        command = [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1",
            "-i", str(source),
            "-t", f"{background_duration:.3f}",
            "-vn",
            "-af",
            (
                "aresample=22050,"
                "loudnorm=I=-25:TP=-2.0:LRA=11,"
                f"afade=t=out:st={max(0.0, background_duration - 4.0):.3f}:d=4"
            ),
            "-ac", "1",
            "-ar", str(RATE),
            "-c:a", "pcm_s16le",
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
            raise RuntimeError(
                "Could not prepare Lyria music: " + result.stderr[-1000:]
            )

        try:
            duration = validate_wav(temporary, background_duration)
            temporary.replace(MUSIC_PATH)
        finally:
            temporary.unlink(missing_ok=True)

        return {
            "status": "completed",
            "path": MUSIC_PATH.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(MUSIC_PATH),
            "duration_seconds": round(duration, 6),
            "generation": "google_lyria",
            "provider": data.get("provider", "google_lyria"),
            "model": data.get("model", "unknown"),
            "source_audio": source.relative_to(ROOT).as_posix(),
            "source_sha256": actual_hash,
            "source_manifest": FLOW_MUSIC_MANIFEST.relative_to(ROOT).as_posix(),
            "source_fingerprint": data.get("source_fingerprint"),
            "paid_services_used": bool(data.get("paid_services_used", True)),
        }

    except Exception as exc:
        log("LYRIA MUSIC REJECTED:", str(exc))
        return None


def narration_durations():
    data = read_json(AUDIO_MANIFEST)

    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = (
            data.get("jobs")
            or data.get("audio_jobs")
            or data.get("scenes")
            or []
        )
    else:
        raise RuntimeError("Narration audio manifest is missing or invalid.")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("No narration audio jobs found.")

    result = {}

    for row in jobs:
        if not isinstance(row, dict):
            continue

        if str(row.get("status", "")).lower() not in {
            "completed", "reused"
        }:
            continue

        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError):
            continue

        if part < 1 or scene < 1:
            raise RuntimeError("Narration contains an invalid scene number.")

        value = next(
            (
                row.get(key)
                for key in PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )

        if not value:
            raise RuntimeError(
                f"Narration audio path missing for part {part}, scene {scene}."
            )

        path = safe_project_path(value)
        duration = ffprobe_audio(path)
        key = (part, scene)

        if key in result:
            raise RuntimeError(f"Duplicate narration job: {key}.")

        result[key] = {
            "duration": duration,
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(path),
        }

    if not result:
        raise RuntimeError("No completed narration audio was found.")

    return result


def scene_rows():
    data = read_json(SCENES_PATH)

    if not isinstance(data, dict):
        raise RuntimeError("scenes.json is missing or invalid.")

    result = []
    parts_data = data.get("parts")

    if isinstance(parts_data, list):
        for part_index, part_data in enumerate(parts_data, 1):
            if not isinstance(part_data, dict):
                raise RuntimeError("Invalid part in scenes.json.")

            part = int(part_data.get("part", part_index))
            items = part_data.get("scenes", [])

            if not isinstance(items, list):
                raise RuntimeError(f"Scenes for part {part} must be a list.")

            for scene_index, scene_data in enumerate(items, 1):
                if not isinstance(scene_data, dict):
                    raise RuntimeError(
                        f"Invalid scene in part {part}."
                    )

                row = dict(scene_data)
                row.setdefault("part", part)
                row.setdefault("scene", scene_index)
                result.append(row)

        if result:
            return result

    items = data.get("scenes")

    if isinstance(items, list):
        if not all(isinstance(row, dict) for row in items):
            raise RuntimeError("scenes.json contains invalid scene rows.")
        return items

    raise RuntimeError("scenes.json contains no supported scene list.")


def validate_scene_mapping(durations):
    mapping = {}

    for row in scene_rows():
        try:
            key = (
                int(row.get("part")),
                int(row.get("scene", row.get("local_scene"))),
            )
        except (TypeError, ValueError):
            raise RuntimeError("Scene manifest contains invalid numbering.")

        if key[0] < 1 or key[1] < 1:
            raise RuntimeError(f"Invalid scene number: {key}.")

        if key in mapping:
            raise RuntimeError(f"Duplicate scene mapping: {key}.")

        mapping[key] = row

    audio_keys = set(durations)
    scene_keys = set(mapping)

    if audio_keys != scene_keys:
        raise RuntimeError(
            "Narration and scene manifests do not match. "
            f"Missing scene rows={sorted(audio_keys - scene_keys)}; "
            f"extra scene rows={sorted(scene_keys - audio_keys)}."
        )

    return mapping


def choose_sfx(scene):
    text = " ".join(
        str(scene.get(key, ""))
        for key in (
            "sfx", "sfx_prompt", "title", "narration", "text",
            "mood", "suspense_prompt", "visual_prompt",
            "beat_description", "role",
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

    if any(
        word in text
        for word in (
            "transition", "दौड़ा", "भागा", "भाग रही",
            "दरवाजा खुला", "तेजी से मुड़ा", "चल पड़ा",
        )
    ):
        return "whoosh"

    if scene.get("suspense_required"):
        return "mystery"

    return "whoosh"


def create_cue(part, scene, narration, scene_data):
    kind = choose_sfx(scene_data)
    duration = narration["duration"]

    # Keep the effect start inside the actual spoken scene.
    start = min(
        duration * SFX_TIME_FRACTIONS[kind],
        max(0.0, duration - 0.25),
    )

    path = SFX_DIR / f"{kind}.wav"

    return {
        "part": part,
        "scene": scene,
        "time": round(start, 3),
        "file": path.relative_to(ROOT).as_posix(),
        "sha256": sha256_file(path),
        "gain": SFX_GAINS[kind],
        "kind": kind,
        "selection_basis": "story_scene_text_and_scene_role",
        "timing_basis": "actual_narration_audio_duration",
        "narration_duration_seconds": round(duration, 3),
        "narration_file": narration["path"],
        "narration_sha256": narration["sha256"],
        "effect_duration_seconds": round(validate_wav(path), 6),
    }


def validate_track_record(record, category, minimum_duration):
    if not isinstance(record, dict):
        raise RuntimeError(f"{category} track record is invalid.")

    if record.get("status") != "completed":
        raise RuntimeError(f"{category} track is not completed.")

    path = safe_project_path(record.get("path"))
    duration = ffprobe_audio(path)
    checksum = sha256_file(path)

    if record.get("sha256") != checksum:
        raise RuntimeError(f"{category} track checksum mismatch.")

    if duration + 0.5 < minimum_duration:
        raise RuntimeError(
            f"{category} track is shorter than narration."
        )

    record["duration_seconds"] = round(duration, 6)
    return record


def main():
    config = settings()

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

    durations = narration_durations()
    mapping = validate_scene_mapping(durations)
    ordered_keys = sorted(durations)

    total_duration = sum(
        durations[key]["duration"] for key in ordered_keys
    )
    background_duration = max(30.0, total_duration + 8.0)

    actual_parts = sorted({part for part, _ in ordered_keys})
    part_scene_counts = {
        str(part): sum(1 for p, _ in ordered_keys if p == part)
        for part in actual_parts
    }

    tracks = {}

    if not enabled_tracks["music"]:
        MUSIC_PATH.unlink(missing_ok=True)
        tracks["music"] = None
        log("Music disabled.")
    else:
        flow_record = load_flow_music(background_duration)

        if flow_record:
            tracks["music"] = validate_track_record(
                flow_record, "music", total_duration
            )
            log("Using verified Google Lyria music.")
        else:
            tracks["music"] = make_track(
                MUSIC_PATH,
                "music",
                music_samples,
                background_duration,
                old_tracks,
            )
            tracks["music"]["generation"] = "local_fallback"
            tracks["music"]["provider"] = "local_synthesis"

    if not enabled_tracks["ambient"]:
        AMBIENT_PATH.unlink(missing_ok=True)
        tracks["ambient"] = None
    else:
        tracks["ambient"] = make_track(
            AMBIENT_PATH,
            "ambient",
            ambient_samples,
            background_duration,
            old_tracks,
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
        for part, scene in ordered_keys:
            cues.append(
                create_cue(
                    part,
                    scene,
                    durations[(part, scene)],
                    mapping[(part, scene)],
                )
            )

    music_record = tracks.get("music")
    using_lyria = (
        isinstance(music_record, dict)
        and music_record.get("generation") == "google_lyria"
    )

    flow_data = read_json(FLOW_MUSIC_MANIFEST, {})
    if not isinstance(flow_data, dict):
        flow_data = {}

    external_api_calls = 0
    if using_lyria:
        try:
            external_api_calls = max(
                0, int(flow_data.get("external_api_calls", 1))
            )
        except (TypeError, ValueError):
            external_api_calls = 1

    paid_services_used = bool(
        using_lyria
        and music_record.get("paid_services_used", True)
    )

    cue_manifest = {
        "status": "completed",
        "version": VERSION,
        "format": config.get("FORMAT", "unknown").strip().lower(),
        "dynamic_parts": True,
        "actual_parts": len(actual_parts),
        "part_numbers": actual_parts,
        "part_scene_counts": part_scene_counts,
        "total_scenes": len(ordered_keys),
        "cue_count": len(cues),
        "cue_policy": (
            "Story-aware local SFX selected from each scene's content."
        ),
        "timing_basis": "Actual measured narration audio durations.",
        "external_api_calls": 0,
        "paid_services_used": False,
        "cues": cues,
    }
    write_json(CUES_PATH, cue_manifest)

    manifest = {
        "status": "completed",
        "version": VERSION,
        "generation": (
            "google_lyria_music_with_local_ambient_and_sfx"
            if using_lyria
            else "local_synthesis_with_lyria_fallback"
        ),
        "external_api_calls": external_api_calls,
        "paid_services_used": paid_services_used,
        "sample_rate": RATE,
        "background_duration_seconds": round(background_duration, 3),
        "sfx_duration_seconds": SFX_SECONDS,
        "total_narration_seconds": round(total_duration, 3),
        "format": config.get("FORMAT", "unknown").strip().lower(),
        "dynamic_parts": True,
        "parts": len(actual_parts),
        "part_numbers": actual_parts,
        "part_scene_counts": part_scene_counts,
        "total_scenes": len(ordered_keys),
        "enabled": enabled_tracks,
        "tracks": tracks,
        "flow_music_manifest": FLOW_MUSIC_MANIFEST.relative_to(ROOT).as_posix(),
        "music_provider": (
            "Google Lyria" if using_lyria
            else "local_synthesis_fallback" if enabled_tracks["music"]
            else "disabled"
        ),
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
        "narration_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "scene_manifest": SCENES_PATH.relative_to(ROOT).as_posix(),
        "mixing_note": (
            "Music and ambience are prepared to cover measured narration "
            "duration plus an eight-second tail. SFX cues are timed from "
            "actual scene narration audio. Final ducking and mixing are "
            "handled by render_video.py."
        ),
    }

    write_json(MANIFEST_PATH, manifest)

    log("SOUND DESIGN READY")
    log("Format:", manifest["format"])
    log("Music provider:", manifest["music_provider"])
    log("Narration duration:", round(total_duration, 2), "seconds")
    log("Music/ambient duration:", round(background_duration, 2), "seconds")
    log("Parts:", len(actual_parts))
    log("Scenes:", len(ordered_keys))
    log("SFX cues:", len(cues))
    log("Manifest:", MANIFEST_PATH.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        raise SystemExit(1)