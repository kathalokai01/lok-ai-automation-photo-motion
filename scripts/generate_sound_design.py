#!/usr/bin/env python3
"""Free, deterministic, resumable local sound design."""

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

VERSION = "free-sound-design-v11-sample-tolerant-resume"
RATE = 22050
SFX_SECONDS = 1.8
MIN_BYTES = 100
SFX_TYPES = ("whoosh", "impact", "mystery", "heartbeat", "chime")
AUDIO_PATH_KEYS = ("output", "audio_path", "path", "file", "audio")
SFX_TIME_FRACTIONS = {
    "whoosh": 0.30,
    "heartbeat": 0.18,
    "impact": 0.60,
    "mystery": 0.48,
    "chime": 0.72,
}

# Allow one sample of rounding difference, plus tiny float precision noise.
DURATION_TOLERANCE = (1.0 / RATE) + 1e-6


def log(*values):
    print(*values, flush=True)


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
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest(data):
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
        value = value.strip()

        if value.startswith(("'", '"')) and len(value) >= 2:
            quote = value[0]
            end = value.find(quote, 1)
            if end >= 0:
                value = value[1:end]
        else:
            value = value.split("#", 1)[0].strip()

        result[key.strip().upper()] = value

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


def write_wav(path, samples, duration):
    """Write an exact integer number of PCM samples atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    expected_frames = int(round(RATE * duration))
    frames_written = 0

    try:
        with wave.open(str(temporary), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(RATE)

            block = bytearray()
            for sample in samples:
                if frames_written >= expected_frames:
                    break

                sample = max(-0.98, min(0.98, float(sample)))
                block.extend(struct.pack("<h", int(sample * 32767)))
                frames_written += 1

                if len(block) >= 32768:
                    audio.writeframesraw(block)
                    block.clear()

            if block:
                audio.writeframesraw(block)

            if frames_written != expected_frames:
                raise RuntimeError(
                    f"Audio generator produced {frames_written} frames; "
                    f"expected {expected_frames}"
                )

        # Re-open and verify before replacing the final file.
        validate_audio(temporary, minimum_seconds=0.1)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_audio(path, minimum_seconds=0.1):
    """Validate WAV format and tolerate one-sample duration rounding."""
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError(f"Audio missing or too small: {path}")

    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise RuntimeError(f"Unexpected WAV format: {path}")
            if audio.getframerate() != RATE:
                raise RuntimeError(f"Unexpected sample rate: {path}")

            frames = audio.getnframes()
            if frames <= 0:
                raise RuntimeError(f"Audio contains no samples: {path}")

            duration = frames / float(audio.getframerate())
    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"Invalid or incomplete WAV: {path}") from exc

    if duration + DURATION_TOLERANCE < minimum_seconds:
        raise RuntimeError(
            f"Audio too short: {path}; "
            f"actual={duration:.6f}s, expected={minimum_seconds:.6f}s"
        )

    return duration


def track_record(path, signature, minimum_seconds):
    duration = validate_audio(path, minimum_seconds)
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": signature,
        "sha256": sha256_file(path),
        "duration_seconds": round(duration, 6),
        "generation": "deterministic_local_synthesis_no_external_api",
    }


def ensure_track(path, category, generator, duration, previous_tracks):
    signature = digest({
        "version": VERSION,
        "category": category,
        "sample_rate": RATE,
        "duration": round(duration, 6),
    })

    previous = previous_tracks.get(category, {})
    if not isinstance(previous, dict):
        previous = {}

    if path.is_file():
        try:
            record = track_record(path, signature, duration)
            if (
                previous.get("signature") == signature
                and previous.get("sha256") == record["sha256"]
            ):
                log("RESUME:", path.relative_to(ROOT))
                return record
        except (OSError, ValueError, wave.Error, EOFError, RuntimeError):
            log("REBUILDING INVALID AUDIO:", path.relative_to(ROOT))

    log("GENERATING:", category, f"{duration:.3f}s")
    write_wav(path, generator(duration), duration)
    return track_record(path, signature, duration)


def envelope(t, duration, fade_in=2.0, fade_out=3.0):
    return max(
        0.0,
        min(
            1.0,
            t / max(0.1, fade_in),
            (duration - t) / max(0.1, fade_out),
        ),
    )


def music_samples(duration):
    """Generate soft layered tonal background ambience."""
    roots = (110.0, 130.81, 98.0, 146.83, 123.47, 92.50)
    ratios = (1.0, 1.25, 1.5, 2.0)

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        root = roots[int(t // 8) % len(roots)]
        slow = 0.75 + 0.25 * math.sin(2 * math.pi * t / 17.0)
        pulse = 0.92 + 0.08 * math.sin(2 * math.pi * t / 5.3)

        tone = sum(
            0.17 / (number + 1)
            * math.sin(2 * math.pi * root * ratio * t + 0.07 * number)
            for number, ratio in enumerate(ratios)
        )
        sub = 0.08 * math.sin(2 * math.pi * (root / 2) * t)
        yield (tone + sub) * envelope(t, duration) * slow * pulse * 0.36


def ambient_samples(duration):
    rng = random.Random(271828)
    filtered = 0.0

    for index in range(int(round(RATE * duration))):
        t = index / RATE
        filtered = filtered * 0.985 + rng.uniform(-1, 1) * 0.015
        low = (
            0.12 * math.sin(2 * math.pi * 47 * t)
            + 0.08 * math.sin(2 * math.pi * 69.3 * t + 0.4)
        )
        yield (
            low + filtered * 0.14
        ) * envelope(t, duration, 1.5, 2.0) * 0.28


def sfx_samples(kind):
    count = int(round(RATE * SFX_SECONDS))
    rng = random.Random(9000 + SFX_TYPES.index(kind))
    filtered = 0.0

    for index in range(count):
        t = index / RATE
        p = index / max(1, count - 1)
        filtered = filtered * 0.72 + rng.uniform(-1, 1) * 0.28
        fade = max(0.0, math.sin(math.pi * p)) ** 1.4

        if kind == "whoosh":
            sweep = 140 + 600 * p
            value = (
                0.28 * filtered
                + 0.18 * math.sin(2 * math.pi * sweep * t)
            )
        elif kind == "impact":
            decay = math.exp(-7.0 * t)
            value = decay * (
                0.60 * math.sin(2 * math.pi * 65 * t)
                + 0.18 * filtered
            )
        elif kind == "mystery":
            wobble = 2.0 * math.sin(2 * math.pi * 0.7 * t)
            value = (
                0.24 * math.sin(2 * math.pi * (196 + wobble) * t)
                + 0.17 * math.sin(2 * math.pi * 246.94 * t)
                + 0.08 * filtered
            )
        elif kind == "heartbeat":
            beat = max(0.0, math.sin(2 * math.pi * 1.5 * t)) ** 12
            value = (
                beat * math.sin(2 * math.pi * 58 * t)
                + 0.025 * filtered
            )
        else:
            value = (
                0.38 * math.sin(2 * math.pi * 880 * t)
                + 0.20 * math.sin(2 * math.pi * 1320 * t)
                + 0.10 * math.sin(2 * math.pi * 1760 * t)
            ) * math.exp(-2.2 * t)

        yield value * fade * 0.60


def scene_rows():
    data = read_json(SCENES_PATH)
    if not isinstance(data, dict):
        raise RuntimeError(f"Missing or invalid scenes file: {SCENES_PATH}")

    rows = []
    parts_data = data.get("parts")

    if isinstance(parts_data, list):
        for part_index, part_data in enumerate(parts_data, 1):
            if not isinstance(part_data, dict):
                continue

            part = int(part_data.get("part", part_index))
            scenes = part_data.get("scenes", [])
            if not isinstance(scenes, list):
                raise RuntimeError(f"Scenes for part {part} must be a list")

            for scene_index, scene_data in enumerate(scenes, 1):
                if not isinstance(scene_data, dict):
                    raise RuntimeError(f"Invalid scene in part {part}")
                row = dict(scene_data)
                row.setdefault("part", part)
                row.setdefault("scene", scene_index)
                rows.append(row)

        if rows:
            return rows

    scenes = data.get("scenes")
    if isinstance(scenes, list):
        return [row for row in scenes if isinstance(row, dict)]

    raise RuntimeError("scenes.json contains no supported scene list")


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

    if any(word in text for word in (
        "transition", "दौड़ा", "भागा", "चल पड़ा",
    )):
        return "whoosh"

    return "mystery" if scene.get("suspense_required") else "whoosh"


def narration_durations():
    data = read_json(AUDIO_MANIFEST)
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("jobs")
            or data.get("audio_jobs")
            or data.get("scenes")
            or []
        )
    else:
        raise RuntimeError("Narration manifest missing or invalid")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("No narration jobs found; generate narration first")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to inspect narration durations")

    result = {}

    for row in rows:
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
            (
                row.get(key)
                for key in AUDIO_PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
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

        probe = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode:
            raise RuntimeError(
                f"ffprobe failed for narration {part}/{scene}: "
                f"{probe.stderr[-500:]}"
            )

        try:
            info = json.loads(probe.stdout)
            duration = float(info["format"]["duration"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Invalid narration duration for {part}/{scene}"
            ) from exc

        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(f"Invalid narration duration for {part}/{scene}")

        if not any(
            item.get("codec_type") == "audio"
            for item in info.get("streams", [])
        ):
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
        raise RuntimeError("No completed narration files were found")

    return result


def cue_offset(kind, duration):
    fraction = SFX_TIME_FRACTIONS.get(kind, 0.35)
    latest_start = max(0.0, duration - min(0.25, SFX_SECONDS * 0.12))
    return round(min(duration * fraction, latest_start), 3)


def build_cues(rows, parts, scenes_per_part, durations):
    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

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
            "Scene mapping mismatch; missing="
            f"{sorted(expected - set(scene_map))}"
        )

    if set(durations) != expected:
        raise RuntimeError(
            "Narration mapping mismatch; missing="
            f"{sorted(expected - set(durations))}; "
            f"extra={sorted(set(durations) - set(expected))}"
        )

    cues = []
    for part, scene in sorted(expected):
        audio = durations[(part, scene)]
        duration = audio["duration"]
        kind = choose_sfx(scene_map[(part, scene)])
        cue_path = SFX_DIR / f"{kind}.wav"

        cues.append({
            "part": part,
            "scene": scene,
            "time": cue_offset(kind, duration),
            "file": cue_path.relative_to(ROOT).as_posix(),
            "gain": 0.045 if kind in {"mystery", "heartbeat"} else 0.06,
            "kind": kind,
            "selection_basis": "scene_text_keyword_rules",
            "timing_basis": "scene_relative_heuristic_using_actual_audio_duration",
            "narration_duration_seconds": round(duration, 3),
            "narration_file": audio["path"],
            "narration_sha256": audio["sha256"],
        })

    return cues


def main():
    config = settings()
    parts = positive_int(config, "PARTS", 1)
    scenes_per_part = positive_int(config, "SCENES", 11)

    enabled = {
        "music": as_bool(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC", "SOUNDTRACK_ENABLED"),
        ),
        "ambient": as_bool(
            config,
            (
                "AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
                "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED",
            ),
        ),
        "sfx": as_bool(
            config,
            ("SFX_ENABLED", "SFX", "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED"),
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
    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    if set(durations) != expected:
        raise RuntimeError(
            "Narration scene count differs from Input/topic.txt. "
            f"Missing={sorted(expected - set(durations))}; "
            f"extra={sorted(set(durations) - expected)}"
        )

    total_duration = sum(item["duration"] for item in durations.values())
    background_duration = max(30.0, total_duration + 2.0)

    tracks = {}
    definitions = {
        "music": (MUSIC_PATH, music_samples),
        "ambient": (AMBIENT_PATH, ambient_samples),
    }

    for category, (path, generator) in definitions.items():
        if not enabled[category]:
            path.unlink(missing_ok=True)
            tracks[category] = None
            continue

        tracks[category] = ensure_track(
            path,
            category,
            lambda duration=background_duration, fn=generator: fn(duration),
            background_duration,
            old_tracks,
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

        previous = old_tracks.get(category, {})
        if not isinstance(previous, dict):
            previous = {}

        reused = False
        if path.is_file():
            try:
                record = track_record(path, signature, SFX_SECONDS)
                if (
                    previous.get("signature") == signature
                    and previous.get("sha256") == record["sha256"]
                ):
                    sfx_tracks[kind] = record
                    log("RESUME:", path.relative_to(ROOT))
                    reused = True
            except (OSError, ValueError, wave.Error, EOFError, RuntimeError):
                log("REBUILDING INVALID SFX:", path.relative_to(ROOT))

        if not reused:
            write_wav(path, sfx_samples(kind), SFX_SECONDS)
            sfx_tracks[kind] = track_record(
                path, signature, SFX_SECONDS
            )

    tracks["sfx"] = sfx_tracks.get("whoosh")
    for kind, record in sfx_tracks.items():
        tracks[f"sfx_{kind}"] = record

    cues = []
    if enabled["sfx"]:
        cues = build_cues(
            scene_rows(), parts, scenes_per_part, durations
        )

        for cue in cues:
            path = (ROOT / cue["file"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
                raise RuntimeError(f"SFX asset missing: {path}")

            validate_audio(path, SFX_SECONDS)

            if (
                cue["time"] < 0
                or cue["time"] >= cue["narration_duration_seconds"]
            ):
                raise RuntimeError(
                    f"SFX cue outside narration: "
                    f"{cue['part']}/{cue['scene']}"
                )

    write_json(CUES_PATH, {
        "status": "completed",
        "version": VERSION,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "cue_count": len(cues),
        "cue_policy": "Scene-text keyword selection; deterministic local timing.",
        "timing_basis": (
            "Actual narration duration with scene-relative heuristic offsets."
            if enabled["sfx"]
            else "SFX disabled in input configuration."
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
        "background_duration_seconds": round(background_duration, 3),
        "sfx_duration_seconds": SFX_SECONDS,
        "total_narration_seconds": round(total_duration, 3),
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "enabled": enabled,
        "tracks": tracks,
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
    })

    log("SOUND DESIGN COMPLETE")
    log("BACKGROUND DURATION:", round(background_duration, 3), "seconds")
    log("SFX CUES:", len(cues))
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SOUND DESIGN FAILED:", str(exc))
        sys.exit(1)