
#!/usr/bin/env python3
"""Story-aware sound design with Google Lyria music and local fallback."""

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
FLOW_MUSIC_MANIFEST = OUTPUT / "music" / "flow_music_manifest.json"
AMBIENT_PATH = OUTPUT / "sound" / "ambient_environment.wav"
SFX_DIR = OUTPUT / "sfx"
DESIGN_DIR = OUTPUT / "sound_design"
MANIFEST_PATH = DESIGN_DIR / "sound_design_manifest.json"
CUES_PATH = DESIGN_DIR / "sfx_cues.json"
SCENES_PATH = OUTPUT / "scenes" / "scenes.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"

VERSION = "google-lyria-music-local-fallback-sound-design-v17"
RATE = 22050
SFX_SECONDS = 3.6
SFX_TYPES = ("whoosh", "impact", "mystery", "heartbeat", "chime")
PATH_KEYS = ("output", "audio_path", "path", "file", "audio")

SFX_GAINS = {
    "whoosh": 0.72,
    "impact": 0.82,
    "mystery": 0.66,
    "heartbeat": 0.72,
    "chime": 0.70,
}

SFX_TIME_FRACTIONS = {
    "whoosh": 0.24,
    "heartbeat": 0.20,
    "impact": 0.48,
    "mystery": 0.32,
    "chime": 0.62,
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
    temporary = path.with_suffix(path.suffix + ".tmp")
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
    return hashlib.sha256(
        json.dumps(
            data,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    result = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in "\"'"
        ):
            value = value[1:-1]

        result[key.strip().upper()] = value

    return result


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
        timeout=60,
        check=False,
    )

    if process.returncode:
        raise RuntimeError(
            f"ffprobe failed: {process.stderr[-500:]}"
        )

    try:
        info = json.loads(process.stdout)
        duration = float(info["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Invalid audio duration.") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("Audio duration is invalid.")

    if not any(
        stream.get("codec_type") == "audio"
        for stream in info.get("streams", [])
    ):
        raise RuntimeError("File contains no audio stream.")

    return duration


def safe_project_path(value):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Audio manifest path is missing.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError("Audio path points outside the project.")

    if not path.is_file():
        raise RuntimeError(f"Audio file does not exist: {value}")

    return path


def validate_wav(path, minimum_seconds=0.1):
    path = Path(path)
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


def write_wav(path, sample_factory, duration):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    expected = int(round(RATE * duration))
    count = 0

    try:
        with wave.open(str(temporary), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)

            block = bytearray()
            for sample in sample_factory(duration):
                if count >= expected:
                    break

                sample = max(-0.94, min(0.94, float(sample)))
                block.extend(struct.pack("<h", int(sample * 32767)))
                count += 1

                if len(block) >= 32768:
                    wav.writeframesraw(block)
                    block.clear()

            if block:
                wav.writeframesraw(block)

            if count != expected:
                raise RuntimeError(
                    f"Audio frame mismatch: generated {count}, expected {expected}."
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
    roots = (110.0, 130.81, 98.0, 146.83, 123.47, 92.5)
    pulse_rates = (0.11, 0.13, 0.09, 0.15)

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        root = roots[int(t // 12) % len(roots)]
        pulse = 0.82 + 0.18 * math.sin(
            2 * math.pi * pulse_rates[int(t // 12) % len(pulse_rates)] * t
        )

        pad = (
            0.48 * math.sin(2 * math.pi * root * t)
            + 0.28 * math.sin(2 * math.pi * root * 1.5 * t)
            + 0.19 * math.sin(2 * math.pi * root * 2.0 * t)
            + 0.10 * math.sin(2 * math.pi * root * 0.5 * t)
        )

        harmonic = 0.08 * math.sin(
            2 * math.pi * (root * 2.01) * t
        )
        yield (pad + harmonic) * pulse * envelope(
            t, duration, 2.0, 3.0
        )


def ambient_samples(duration):
    rng = random.Random(271828)
    low_filtered = 0.0
    high_filtered = 0.0

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        noise = rng.uniform(-1.0, 1.0)

        low_filtered = low_filtered * 0.992 + noise * 0.008
        high_filtered = noise - low_filtered

        low = (
            0.22 * math.sin(2 * math.pi * 47 * t)
            + 0.15 * math.sin(2 * math.pi * 69.3 * t)
            + 0.08 * math.sin(2 * math.pi * 93 * t)
        )

        texture = low_filtered * 0.34 + high_filtered * 0.035
        yield (low + texture) * envelope(
            t, duration, 1.8, 2.5
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
                0.62 * filtered
                + 0.36 * math.sin(2 * math.pi * sweep * t)
                + 0.16 * math.sin(2 * math.pi * (sweep * 1.7) * t)
            )

        elif kind == "impact":
            body = math.exp(-2.2 * t)
            sub_bass = math.sin(2 * math.pi * (48 + 10 * math.exp(-t)) * t)
            crack = filtered * math.exp(-5.0 * t)
            rumble = filtered_slow * math.exp(-1.2 * t)
            value = 0.82 * body * sub_bass + 0.34 * crack + 0.22 * rumble

        elif kind == "mystery":
            wobble = 5.0 * math.sin(2 * math.pi * 0.32 * t)
            drone = (
                0.42 * math.sin(2 * math.pi * (146.83 + wobble) * t)
                + 0.28 * math.sin(2 * math.pi * 196 * t)
                + 0.18 * math.sin(2 * math.pi * 220 * t)
            )
            texture = 0.24 * filtered + 0.10 * filtered_slow
            value = drone + texture

        elif kind == "heartbeat":
            beat_phase = (t * 1.15) % 1.0
            beat_one = math.exp(-((beat_phase - 0.10) / 0.055) ** 2)
            beat_two = math.exp(-((beat_phase - 0.28) / 0.075) ** 2)
            pulse = beat_one * 0.95 + beat_two * 0.62
            value = (
                pulse * math.sin(2 * math.pi * 54 * t)
                + 0.16 * filtered_slow
            )

        else:
            decay = math.exp(-0.95 * t)
            shimmer = (
                0.48 * math.sin(2 * math.pi * 880 * t)
                + 0.32 * math.sin(2 * math.pi * 1320 * t)
                + 0.20 * math.sin(2 * math.pi * 1760 * t)
            )
            echo = 0.24 * math.sin(2 * math.pi * 660 * t) * math.exp(-1.4 * t)
            value = shimmer * decay + echo

        yield value * fade


def make_track(path, category, factory, duration, old_tracks):
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
        except (
            OSError, ValueError, RuntimeError,
            wave.Error, EOFError,
        ):
            log("Rebuilding invalid track:", path.relative_to(ROOT))

    log("GENERATING:", category, f"{duration:.2f}s")
    write_wav(path, factory, duration)

    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": sig,
        "sha256": sha256_file(path),
        "duration_seconds": round(validate_wav(path, duration), 6),
        "generation": "deterministic_local_synthesis_no_external_api",
    }


def load_flow_music(background_duration):
    """Validate the Lyria-generated track before passing it to the renderer."""
    data = read_json(FLOW_MUSIC_MANIFEST)

    if not isinstance(data, dict):
        log("FLOW MUSIC: manifest missing; using local music.")
        return None

    if str(data.get("status", "")).lower() != "completed":
        log(
            "FLOW MUSIC: not completed:",
            data.get("status", "unknown"),
            "- using local music.",
        )
        return None

    try:
        path = safe_project_path(data.get("path"))
        duration = ffprobe_audio(path)
        checksum = sha256_file(path)

        expected_hash = data.get("sha256")
        if expected_hash and str(expected_hash).lower() != checksum.lower():
            raise RuntimeError("Lyria music checksum mismatch.")

        if duration + 0.5 < background_duration:
            raise RuntimeError(
                f"Lyria music is too short ({duration:.2f}s); "
                f"need {background_duration:.2f}s."
            )

        declared_duration = data.get("duration_seconds")
        if declared_duration is not None:
            try:
                declared_duration = float(declared_duration)
            except (TypeError, ValueError):
                raise RuntimeError("Invalid duration in Lyria manifest.")

            if (
                not math.isfinite(declared_duration)
                or declared_duration <= 0
                or abs(declared_duration - duration) > 2.0
            ):
                raise RuntimeError("Lyria manifest duration does not match audio.")

        record = {
            "status": "completed",
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": checksum,
            "duration_seconds": round(duration, 6),
            "generation": "google_lyria",
            "provider": data.get("provider", "Google Gemini API"),
            "model": data.get("model", "lyria-3.5"),
            "source_manifest": FLOW_MUSIC_MANIFEST.relative_to(ROOT).as_posix(),
            "source_fingerprint": data.get("source_fingerprint"),
            "paid_services_used": bool(data.get("paid_services_used", True)),
        }

        log(
            "FLOW MUSIC READY:",
            record["provider"],
            record["model"],
            f"{duration:.2f}s",
        )
        return record

    except (
        OSError, ValueError, TypeError, RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        log("FLOW MUSIC INVALID:", str(exc))
        log("Using existing local music fallback.")
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
        raise RuntimeError("Narration manifest missing or invalid.")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("No narration jobs found; generate narration first.")

    result = {}

    for row in jobs:
        if not isinstance(row, dict):
            continue

        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError):
            continue

        if str(row.get("status", "")).strip().lower() != "completed":
            continue

        value = next(
            (
                row.get(key)
                for key in PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        if not value:
            raise RuntimeError(f"Narration path missing for {part}/{scene}.")

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
        raise RuntimeError("No completed narration files found.")

    return result


def scene_rows():
    data = read_json(SCENES_PATH)
    if not isinstance(data, dict):
        raise RuntimeError(f"Missing or invalid scenes file: {SCENES_PATH}")

    result = []
    parts_data = data.get("parts")

    if isinstance(parts_data, list):
        for part_index, part_data in enumerate(parts_data, 1):
            if not isinstance(part_data, dict):
                continue

            part = int(part_data.get("part", part_index))
            items = part_data.get("scenes", [])
            if not isinstance(items, list):
                raise RuntimeError(f"Scenes for part {part} must be a list.")

            for scene_index, scene_data in enumerate(items, 1):
                if not isinstance(scene_data, dict):
                    raise RuntimeError(f"Invalid scene in part {part}.")

                row = dict(scene_data)
                row.setdefault("part", part)
                row.setdefault("scene", scene_index)
                result.append(row)

        if result:
            return result

    items = data.get("scenes")
    if isinstance(items, list):
        return [row for row in items if isinstance(row, dict)]

    raise RuntimeError("scenes.json contains no supported scene list.")


def choose_sfx(scene):
    text = " ".join(
        str(scene.get(key, ""))
        for key in (
            "sfx", "title", "narration", "text", "mood",
            "suspense_prompt", "visual_prompt",
        )
    ).casefold()

    groups = [
        ("heartbeat", (
            "heartbeat", "heart beat", "pulse", "धड़कन", "धड़कन",
            "डर से कांप", "सांसें तेज",
        )),
        ("impact", (
            "impact", "crash", "slam", "bang", "explosion",
            "धमाका", "टकराया", "गिर पड़ा", "टूट गया", "गोली",
            "दरवाजा तोड़", "विस्फोट",
        )),
        ("chime", (
            "bell", "chime", "ring", "घंटी", "फोन बज",
            "अलार्म", "संदेश आया",
        )),
        ("mystery", (
            "mystery", "secret", "suspense", "डर", "भूत",
            "रहस्य", "अंधेरा", "साया", "खौफ", "चौंक",
            "अनजान आवाज", "परछाई", "गायब",
        )),
    ]

    for kind, keywords in groups:
        if any(word in text for word in keywords):
            return kind

    if any(
        word in text
        for word in (
            "transition", "दौड़ा", "भागा", "भाग रही",
            "दरवाजा खुला", "तेजी से मुड़ा",
        )
    ):
        return "whoosh"

    return "mystery" if scene.get("suspense_required") else "whoosh"


def validate_scene_mapping(durations):
    mapping = {}

    for row in scene_rows():
        try:
            key = (
                int(row.get("part")),
                int(row.get("scene", row.get("local_scene"))),
            )
        except (TypeError, ValueError):
            continue

        if key in mapping:
            raise RuntimeError(f"Duplicate scene mapping: {key}.")
        mapping[key] = row

    audio_keys = set(durations)
    scene_keys = set(mapping)

    if audio_keys != scene_keys:
        raise RuntimeError(
            "Narration and story scene manifests differ. "
            f"Missing scenes={sorted(audio_keys - scene_keys)}; "
            f"extra scenes={sorted(scene_keys - audio_keys)}."
        )

    if not audio_keys:
        raise RuntimeError("No actual narration scenes found.")

    return mapping


def main():
    config = settings()

    enabled_tracks = {
        "music": enabled(
            config,
            (
                "MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
                "SOUNDTRACK_ENABLED",
            ),
        ),
        "ambient": enabled(
            config,
            (
                "AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
                "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED",
            ),
        ),
        "sfx": enabled(
            config,
            (
                "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
                "SOUND_EFFECTS_ENABLED",
            ),
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

    # Use exact narration durations, not estimated scene lengths.
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
        log("Music disabled in Input/topic.txt.")

    else:
        flow_record = load_flow_music(background_duration)

        if flow_record:
            # Do not overwrite or convert the validated Lyria MP3.
            MUSIC_PATH.unlink(missing_ok=True)
            tracks["music"] = flow_record
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
            narration = durations[(part, scene)]
            duration = narration["duration"]
            kind = choose_sfx(mapping[(part, scene)])

            cue_time = min(
                duration * SFX_TIME_FRACTIONS[kind],
                max(0.0, duration - 0.35),
            )

            effect_path = SFX_DIR / f"{kind}.wav"
            cues.append({
                "part": part,
                "scene": scene,
                "time": round(cue_time, 3),
                "file": effect_path.relative_to(ROOT).as_posix(),
                "gain": SFX_GAINS[kind],
                "kind": kind,
                "selection_basis": "story_scene_text_keyword_rules",
                "timing_basis": (
                    "actual_narration_duration_scene_relative_offset"
                ),
                "narration_duration_seconds": round(duration, 3),
                "narration_file": narration["path"],
                "narration_sha256": narration["sha256"],
                "effect_duration_seconds": SFX_SECONDS,
            })

    flow_data = read_json(FLOW_MUSIC_MANIFEST, {})
    if not isinstance(flow_data, dict):
        flow_data = {}

    music_record = tracks.get("music")
    using_lyria = (
        isinstance(music_record, dict)
        and music_record.get("generation") == "google_lyria"
    )

    external_api_calls = 0
    if using_lyria:
        try:
            external_api_calls = int(
                flow_data.get("external_api_calls", 1)
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
            "Deterministic local SFX selected from actual story scene text."
        ),
        "timing_basis": (
            "Actual narration duration with scene-relative offsets."
        ),
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
        "flow_music_manifest": (
            FLOW_MUSIC_MANIFEST.relative_to(ROOT).as_posix()
        ),
        "music_provider": (
            "Google Lyria 3.5" if using_lyria
            else "local_synthesis_fallback" if enabled_tracks["music"]
            else "disabled"
        ),
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
        "narration_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "scene_manifest": SCENES_PATH.relative_to(ROOT).as_posix(),
        "mixing_note": (
            "Music uses the validated Google Lyria track when available; "
            "otherwise deterministic local music is generated. Ambient "
            "audio and story-aware SFX remain locally generated. Music and "
            "ambience cover the narration timeline plus an eight-second tail. "
            "Final mixing and narration protection are handled by "
            "render_video.py."
        ),
    }

    write_json(MANIFEST_PATH, manifest)

    log("SOUND DESIGN READY")
    log("Format:", manifest["format"])
    log("Music provider:", manifest["music_provider"])
    log("External API calls:", external_api_calls)
    log("Paid services used:", paid_services_used)
    log("Actual parts:", len(actual_parts))
    log("Part scene counts:", part_scene_counts)
    log("Total scenes:", len(ordered_keys))
    log("Narration duration:", round(total_duration, 2), "seconds")
    log("Music/ambient duration:", round(background_duration, 2), "seconds")
    log("SFX duration:", SFX_SECONDS, "seconds")
    log("SFX cues:", len(cues))
    log("Music enabled:", enabled_tracks["music"])
    log("Ambient enabled:", enabled_tracks["ambient"])
    log("SFX enabled:", enabled_tracks["sfx"])
    log("Manifest:", MANIFEST_PATH.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)
