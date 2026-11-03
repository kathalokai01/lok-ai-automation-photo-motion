#!/usr/bin/env python3
"""Story-aware sound design with verified audio timing and resumable assets."""

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

VERSION = "photo-motion-sound-design-v21-story-events"
RATE = 22050
SFX_SECONDS = 4.5

ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}
PATH_KEYS = ("output", "audio_path", "audio_file", "path", "file", "audio")

SFX_TYPES = (
    "impact", "footsteps", "vehicle", "door",
    "weather", "water", "fire", "heartbeat",
    "phone", "crowd", "mystery", "chime",
)

SFX_GAINS = {
    "impact": 0.24, "footsteps": 0.17, "vehicle": 0.19,
    "door": 0.20, "weather": 0.15, "water": 0.14,
    "fire": 0.15, "heartbeat": 0.20, "phone": 0.18,
    "crowd": 0.13, "mystery": 0.15, "chime": 0.15,
}

EVENT_WORDS = {
    "impact": (
        "crash", "slam", "bang", "explosion", "gunshot", "shot",
        "धमाका", "टक्कर", "टकराया", "गिर पड़ा", "टूट गया",
        "गोली", "विस्फोट", "जोरदार प्रहार",
    ),
    "footsteps": (
        "footstep", "footsteps", "walking", "running", " दौड़ा",
        "कदमों", "चलने लगा", "चल रही", "भागा", "भागी",
        "दौड़ पड़ा", "दौड़ने लगा", "पैरों की आहट",
    ),
    "vehicle": (
        "car", "truck", "bus", "train", "motorcycle", "vehicle",
        "engine", "horn", "कार", "ट्रक", "बस", "ट्रेन",
        "मोटरसाइकिल", "गाड़ी", "वाहन", "इंजन", "हॉर्न",
    ),
    "door": (
        "door", "gate", "knock", "lock", "दरवाजा", "दरवाज़ा",
        "दरवाजे", "दरवाज़े", "गेट", "ताला", "खटखटाया",
    ),
    "weather": (
        "storm", "thunder", "lightning", "rain", "wind",
        "तूफान", "तूफ़ान", "बिजली", "गरज", "बारिश", "आंधी",
        "आँधी", "तेज हवा",
    ),
    "water": (
        "river", "sea", "ocean", "water", "wave", "जल",
        "नदी", "समुद्र", "पानी", "लहर", "बाढ़", "झरना",
    ),
    "fire": (
        "fire", "flame", "burning", "smoke", "आग", "लपट",
        "जलता", "जलती", "धुआं", "धुआँ",
    ),
    "heartbeat": (
        "heartbeat", "heart beat", "pulse", "धड़कन", "धड़कन",
        "सांसें तेज", "दिल तेजी से", "घबराहट",
    ),
    "phone": (
        "phone", "mobile", "telephone", "ringtone", "notification",
        "फोन", "मोबाइल", "कॉल", "घंटी बजी", "अलार्म", "संदेश आया",
    ),
    "crowd": (
        "crowd", "audience", "people shouting", "cheering",
        "भीड़", "लोग चिल्लाए", "तालियां", "तालियाँ", "नारे",
    ),
    "mystery": (
        "mystery", "secret", "ghost", "shadow", "unknown voice",
        "रहस्य", "भूत", "साया", "खौफ", "अनजान आवाज",
        "परछाई", "परछाईं", "गायब", "रहस्यमय",
    ),
    "chime": (
        "bell", "chime", "ring", "घंटी", "मंदिर", "शुभ समाचार",
        "खुशी की खबर", "सफलता की खबर",
    ),
}

AMBIENT_KEYWORDS = {
    "city": ("city", "street", "traffic", "शहर", "सड़क", "बाजार", "बाज़ार"),
    "forest": ("forest", "jungle", "woods", "जंगल", "वन", "पेड़"),
    "village": ("village", "farm", "गांव", "गाँव", "खेत", "किसान"),
    "office": ("office", "company", "corporate", "दफ्तर", "कार्यालय", "ऑफिस"),
    "hospital": ("hospital", "clinic", "ward", "अस्पताल", "क्लिनिक"),
    "home": ("home", "house", "room", "घर", "कमरा", "रसोई"),
    "rain": ("rain", "storm", "बारिश", "तूफान", "तूफ़ान"),
    "water": ("river", "sea", "water", "नदी", "समुद्र", "पानी"),
}

def log(*args):
    print(*args, flush=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
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
        data, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str,
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
        command, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-2500:]
        raise RuntimeError(f"Command failed ({result.returncode}): {detail}")
    return result


def safe_project_path(value, must_exist=True):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("A project-relative audio path is missing.")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    root = ROOT.resolve()
    if path != root and root not in path.parents:
        raise RuntimeError("Audio path escapes the project directory.")
    if must_exist and not path.is_file():
        raise RuntimeError(f"Audio file does not exist: {value}")
    return path


def ffprobe_audio(path):
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required.")
    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ], 90)
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


def audio_loudness(path):
    result = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-",
    ], 120)
    output = result.stderr or ""
    values = {}
    for key, marker in (
        ("mean_volume_db", "mean_volume:"),
        ("peak_volume_db", "max_volume:"),
    ):
        for line in output.splitlines():
            if marker in line:
                try:
                    values[key] = float(
                        line.split(marker, 1)[1].strip().split()[0]
                    )
                except (ValueError, IndexError):
                    pass
    return values


def validate_wav(path, minimum_seconds=0.1):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"WAV missing or too small: {path}")
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise RuntimeError("WAV must be mono 16-bit PCM.")
            if audio.getframerate() != RATE:
                raise RuntimeError("WAV sample rate mismatch.")
            frames = audio.getnframes()
            if frames <= 0:
                raise RuntimeError("WAV contains no frames.")
            duration = frames / audio.getframerate()
    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"Invalid WAV: {path}") from exc
    if duration + 1 / RATE < minimum_seconds:
        raise RuntimeError(f"WAV is too short: {path}")
    return duration


def write_wav(path, samples, duration):
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
            for sample in samples:
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
                raise RuntimeError(f"Audio frame mismatch: {count}/{expected}.")
        validate_wav(temporary, min(0.1, duration))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def envelope(t, duration, attack=0.08, release=0.5):
    return min(1.0, t / max(0.01, attack)) * min(
        1.0, max(0.0, duration - t) / max(0.01, release)
    )


def sfx_samples(kind, duration=SFX_SECONDS, seed=771):
    rng = random.Random(seed)
    low = 0.0
    smooth = 0.0
    count = int(round(RATE * duration))

    for index in range(count):
        t = index / RATE
        p = index / max(1, count - 1)
        noise = rng.uniform(-1.0, 1.0)
        low = low * 0.90 + noise * 0.10
        smooth = smooth * 0.994 + noise * 0.006
        fade = envelope(t, duration, 0.025, 0.45)

        if kind == "impact":
            decay = math.exp(-3.2 * t)
            value = (
                0.48 * math.sin(2 * math.pi * 52 * t) * decay
                + 0.16 * low * math.exp(-4 * t)
            )
        elif kind == "footsteps":
            phase = (t * 2.0) % 1.0
            thump = math.exp(-((phase - 0.08) / 0.055) ** 2)
            value = 0.30 * thump * math.sin(2 * math.pi * 72 * t)
            value += 0.035 * smooth
        elif kind == "vehicle":
            sweep = 65 + 90 * p
            value = 0.20 * math.sin(2 * math.pi * sweep * t)
            value += 0.12 * low + 0.05 * smooth
        elif kind == "door":
            value = 0.28 * math.sin(2 * math.pi * 88 * t) * math.exp(-2.5 * t)
            value += 0.09 * low * math.exp(-2 * t)
        elif kind == "weather":
            value = 0.20 * low + 0.10 * smooth
            value += 0.07 * math.sin(2 * math.pi * 43 * t)
        elif kind == "water":
            value = 0.16 * smooth + 0.08 * low
            value += 0.06 * math.sin(2 * math.pi * (170 + 25 * math.sin(t)) * t)
        elif kind == "fire":
            value = 0.17 * low + 0.12 * smooth
            value += 0.04 * math.sin(2 * math.pi * 120 * t)
        elif kind == "heartbeat":
            phase = (t * 1.05) % 1.0
            pulse = (
                0.82 * math.exp(-((phase - 0.10) / 0.06) ** 2)
                + 0.45 * math.exp(-((phase - 0.28) / 0.075) ** 2)
            )
            value = pulse * math.sin(2 * math.pi * 48 * t)
        elif kind == "phone":
            pulse = 1.0 if int(t * 2.0) % 2 == 0 else 0.0
            value = pulse * (
                0.22 * math.sin(2 * math.pi * 740 * t)
                + 0.12 * math.sin(2 * math.pi * 920 * t)
            )
        elif kind == "crowd":
            value = 0.13 * low + 0.12 * smooth
            value += 0.035 * math.sin(2 * math.pi * 260 * t)
        elif kind == "mystery":
            wobble = 4 * math.sin(2 * math.pi * 0.3 * t)
            value = 0.22 * math.sin(2 * math.pi * (110 + wobble) * t)
            value += 0.10 * math.sin(2 * math.pi * 164.81 * t)
            value += 0.045 * smooth
        else:
            decay = math.exp(-1.0 * t)
            value = decay * (
                0.24 * math.sin(2 * math.pi * 523.25 * t)
                + 0.15 * math.sin(2 * math.pi * 659.25 * t)
                + 0.08 * math.sin(2 * math.pi * 783.99 * t)
            )
        yield value * fade


def make_sfx(kind, path, old_record, source_sig):
    path = Path(path)
    sig = signature({
        "version": VERSION, "kind": kind, "duration": SFX_SECONDS,
        "rate": RATE, "source": source_sig,
    })
    if path.is_file() and isinstance(old_record, dict):
        try:
            duration = validate_wav(path, SFX_SECONDS)
            checksum = sha256_file(path)
            if (
                old_record.get("signature") == sig
                and old_record.get("sha256") == checksum
            ):
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": sig, "sha256": checksum,
                    "duration_seconds": round(duration, 6),
                    "generation": "local_event_sfx",
                }
        except Exception:
            pass

    seed = int(sig[:8], 16)
    write_wav(path, sfx_samples(kind, SFX_SECONDS, seed), SFX_SECONDS)
    duration = validate_wav(path, SFX_SECONDS)
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": sig, "sha256": sha256_file(path),
        "duration_seconds": round(duration, 6),
        "generation": "local_event_sfx",
    }

def music_samples(duration, seed=2718):
    roots = (110.0, 130.81, 98.0, 146.83, 123.47, 92.5)
    phase = random.Random(seed).random() * math.tau
    for index in range(int(round(RATE * duration))):
        t = index / RATE
        root = roots[int(t // 16) % len(roots)]
        pad = (
            0.22 * math.sin(2 * math.pi * root * t)
            + 0.12 * math.sin(2 * math.pi * root * 1.5 * t)
            + 0.07 * math.sin(2 * math.pi * root * 2.0 * t)
        )
        pulse = 0.94 + 0.06 * math.sin(
            2 * math.pi * 0.075 * t + phase
        )
        yield pad * pulse * envelope(t, duration, 2.5, 4.0)


def make_music(duration, old_record, source_sig):
    sig = signature({
        "version": VERSION, "source": source_sig,
        "duration": round(duration, 3), "rate": RATE,
    })
    if MUSIC_PATH.is_file() and isinstance(old_record, dict):
        try:
            actual = validate_wav(MUSIC_PATH, duration)
            checksum = sha256_file(MUSIC_PATH)
            if (
                old_record.get("signature") == sig
                and old_record.get("sha256") == checksum
            ):
                return {
                    "status": "completed",
                    "path": MUSIC_PATH.relative_to(ROOT).as_posix(),
                    "signature": sig, "sha256": checksum,
                    "duration_seconds": round(actual, 6),
                    "generation": "local_cinematic_music",
                    "provider": "local_synthesis",
                }
        except Exception:
            pass

    write_wav(MUSIC_PATH, music_samples(duration), duration)
    actual = validate_wav(MUSIC_PATH, duration)
    return {
        "status": "completed",
        "path": MUSIC_PATH.relative_to(ROOT).as_posix(),
        "signature": sig, "sha256": sha256_file(MUSIC_PATH),
        "duration_seconds": round(actual, 6),
        "generation": "local_cinematic_music",
        "provider": "local_synthesis",
    }


def load_flow_music(duration):
    data = read_json(FLOW_MUSIC_MANIFEST, {})
    if not isinstance(data, dict) or data.get("status") != "completed":
        return None
    try:
        source = safe_project_path(data.get("path") or data.get("audio_path"))
        checksum = sha256_file(source)
        if data.get("sha256") != checksum:
            raise RuntimeError("Flow music SHA256 mismatch.")
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
            "-t", f"{duration:.3f}", "-vn",
            "-af",
            (
                "aresample=22050,lowpass=f=9000,"
                "loudnorm=I=-27:TP=-3.0:LRA=11,"
                f"afade=t=out:st={max(0.0, duration - 4):.3f}:d=4"
            ),
            "-ac", "1", "-ar", str(RATE),
            "-c:a", "pcm_s16le", str(temporary),
        ], timeout=900)
        actual = validate_wav(temporary, duration)
        os.replace(temporary, MUSIC_PATH)
        return {
            "status": "completed",
            "path": MUSIC_PATH.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(MUSIC_PATH),
            "duration_seconds": round(actual, 6),
            "generation": "google_flow_music",
            "provider": data.get("provider", "google_flow"),
            "model": data.get("model"),
            "source_audio": source.relative_to(ROOT).as_posix(),
            "source_sha256": checksum,
            "source_fingerprint": data.get("source_fingerprint"),
            "external_api_calls": data.get("external_api_calls", 1),
            "paid_services_used": data.get("paid_services_used", True),
        }
    except Exception as exc:
        log("FLOW MUSIC REJECTED:", str(exc))
        return None


def ambient_source_candidates():
    candidates = [
        AMBIENT_PATH,
        OUTPUT / "sound" / "ambient_sources",
        ROOT / "assets" / "sounds" / "ambient",
        ROOT / "assets" / "audio" / "ambient",
    ]
    result = []
    for candidate in candidates:
        if candidate.is_file():
            result.append(candidate)
        elif candidate.is_dir():
            result.extend(
                sorted(
                    p for p in candidate.rglob("*")
                    if p.is_file()
                    and p.suffix.lower() in {
                        ".wav", ".mp3", ".m4a", ".ogg", ".flac"
                    }
                )
            )
    return list(dict.fromkeys(result))


def choose_ambient_source(scene_records):
    story_text = " ".join(
        str(value or "")
        for row in scene_records.values()
        for value in (
            row.get("narration"), row.get("text"),
            row.get("visual_prompt"), row.get("location"),
            row.get("setting"), row.get("environment"),
        )
    ).casefold()

    desired = []
    for category, words in AMBIENT_KEYWORDS.items():
        if any(word in story_text for word in words):
            desired.append(category)

    candidates = ambient_source_candidates()
    if not candidates:
        return None, desired

    # Prefer a supplied asset whose filename identifies the story setting.
    ranked = []
    for path in candidates:
        name = path.stem.casefold()
        score = sum(
            1 for category in desired
            if category in name
        )
        ranked.append((score, path))
    ranked.sort(key=lambda item: (-item[0], item[1].as_posix()))
    return ranked[0][1], desired


def make_ambient(total_duration, scene_records, old_record, source_sig):
    source, desired = choose_ambient_source(scene_records)
    if source is None:
        return {
            "status": "skipped",
            "reason": "No real ambient audio asset is available; "
                      "synthetic ambience was not substituted.",
            "requested_environment": desired,
        }

    source_hash = sha256_file(source)
    sig = signature({
        "version": VERSION, "source": source_hash,
        "timeline": round(total_duration, 3),
        "source_signature": source_sig,
    })

    if AMBIENT_PATH.is_file() and isinstance(old_record, dict):
        try:
            actual = ffprobe_audio(AMBIENT_PATH)
            checksum = sha256_file(AMBIENT_PATH)
            if (
                old_record.get("signature") == sig
                and old_record.get("sha256") == checksum
                and actual + 0.5 >= total_duration
            ):
                return {
                    "status": "completed",
                    "path": AMBIENT_PATH.relative_to(ROOT).as_posix(),
                    "signature": sig, "sha256": checksum,
                    "duration_seconds": round(actual, 6),
                    "generation": "reused_real_ambient_asset",
                    "source_audio": source.relative_to(ROOT).as_posix(),
                    "source_sha256": source_hash,
                    "environment": desired,
                }
        except Exception:
            pass

    temporary = AMBIENT_PATH.with_name("ambient_environment.building.wav")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1", "-i", str(source),
        "-t", f"{total_duration + 8.0:.3f}", "-vn",
        "-af",
        (
            "aresample=22050,highpass=f=35,lowpass=f=10000,"
            "loudnorm=I=-31:TP=-4:LRA=12,"
            f"afade=t=out:st={max(0.0, total_duration + 4):.3f}:d=4"
        ),
        "-ac", "1", "-ar", str(RATE),
        "-c:a", "pcm_s16le", str(temporary),
    ], timeout=900)
    actual = ffprobe_audio(temporary)
    os.replace(temporary, AMBIENT_PATH)
    return {
        "status": "completed",
        "path": AMBIENT_PATH.relative_to(ROOT).as_posix(),
        "signature": sig, "sha256": sha256_file(AMBIENT_PATH),
        "duration_seconds": round(actual, 6),
        "generation": "real_ambient_asset",
        "source_audio": source.relative_to(ROOT).as_posix(),
        "source_sha256": source_hash,
        "environment": desired,
    }


def record_identity(row):
    try:
        part = int(row["part"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Narration audio record has invalid part.") from exc

    role = str(row.get("role", "main")).strip().lower()
    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported audio role: {role!r}")

    if role == "main":
        try:
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Main narration record has no scene number.") from exc
        scene_id = str(
            row.get("scene_id")
            or f"part_{part:02d}_scene_{scene:03d}"
        ).strip()
        if scene < 1:
            raise RuntimeError("Main scene number must be positive.")
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

    records = {}
    for row in jobs:
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError("TTS manifest contains an incomplete job.")

        scene_id, part, role, scene = record_identity(row)
        if scene_id in records:
            raise RuntimeError(f"Duplicate narration scene_id: {scene_id}")

        audio_value = next(
            (
                row.get(key) for key in PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        path = safe_project_path(audio_value)
        duration = ffprobe_audio(path)
        checksum = sha256_file(path)
        if row.get("sha256") != checksum:
            raise RuntimeError(f"Narration SHA256 mismatch: {scene_id}")

        declared = row.get(
            "actual_audio_duration_seconds", row.get("duration")
        )
        if declared is None or abs(float(declared) - duration) > 0.20:
            raise RuntimeError(f"Measured duration mismatch: {scene_id}")

        duration_source = row.get(
            "duration_source", row.get("audio_duration_source")
        )
        if duration_source not in (None, "ffprobe_measured"):
            raise RuntimeError(
                f"Unverified duration source for {scene_id}: {duration_source}"
            )

        records[scene_id] = {
            "scene_id": scene_id,
            "part": part, "scene": scene, "role": role,
            "duration": duration,
            "path": path,
            "path_relative": path.relative_to(ROOT).as_posix(),
            "sha256": checksum,
            "text": str(row.get("text", row.get("narration", ""))),
            "provider": row.get("provider"),
            "voice": row.get("voice"),
            "tts_fingerprint": row.get("fingerprint"),
        }
    return records


def ordered_audio_records(records):
    return sorted(
        records.values(),
        key=lambda row: (
            row["part"], ROLE_ORDER[row["role"]],
            row["scene"] or 0, row["scene_id"],
        ),
    )


def validate_record_structure(ordered, expected_parts, scenes_per_part):
    parts = sorted({row["part"] for row in ordered})
    if len(parts) != expected_parts:
        raise RuntimeError(
            f"PARTS mismatch: configured {expected_parts}, audio has {len(parts)}."
        )
    if not parts or parts != list(range(parts[0], parts[0] + expected_parts)):
        raise RuntimeError("Part numbering is not continuous.")

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
            raise RuntimeError(f"Part {part} must have exactly one Suspense.")
        if part == final_part and roles.count("resolution") != 1:
            raise RuntimeError("Final part must have exactly one Resolution.")
        if part == final_part and "suspense" in roles:
            raise RuntimeError("Final part cannot contain Suspense.")
        if part != final_part and "resolution" in roles:
            raise RuntimeError("Resolution is only allowed in the final part.")

        main_scenes = sorted(
            row["scene"] for row in rows if row["role"] == "main"
        )
        if main_scenes != list(range(1, scenes_per_part + 1)):
            raise RuntimeError(
                f"Part {part} main scenes mismatch: {main_scenes}"
            )

    expected = (
        expected_parts * scenes_per_part
        + expected_parts + max(0, expected_parts - 1) + 1
    )
    if len(ordered) != expected:
        raise RuntimeError(
            f"Narration record count mismatch: expected {expected}, "
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
            for scene_index, item in enumerate(part_data.get("scenes", []), 1):
                if isinstance(item, dict):
                    record = dict(item)
                    record.setdefault("part", part)
                    record.setdefault("scene", scene_index)
                    record.setdefault("role", "main")
                    rows.append(record)
            for role in ("hook", "suspense", "resolution"):
                item = part_data.get(f"{role}_scene")
                if isinstance(item, dict):
                    record = dict(item)
                    record.setdefault("part", part)
                    record.setdefault("role", role)
                    record.setdefault("scene_id", f"part_{part:02d}_{role}")
                    rows.append(record)

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
                if role == "main" else f"part_{part:02d}_{role}"
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
            row.get("sfx"), row.get("sfx_prompt"),
            row.get("title"), row.get("narration"),
            row.get("text"), row.get("mood"),
            row.get("visual_prompt"), row.get("beat_description"),
            row.get("suspense_prompt"), row.get("location"),
            audio.get("text"),
        )
    ).casefold()

    matches = []
    for kind, words in EVENT_WORDS.items():
        count = sum(1 for word in words if word.casefold() in text)
        if count:
            matches.append((count, kind))
    if matches:
        matches.sort(key=lambda item: (-item[0], item[1]))
        return matches[0][1]

    role = audio["role"]
    if role in {"hook", "suspense"}:
        return "mystery"

    resolution_words = (
        "resolved", "reunited", "safe", "victory", "truth revealed",
        "समाधान", "मिल गया", "मिल गई", "सच सामने", "जीत",
        "सुरक्षित", "न्याय मिला",
    )
    if role == "resolution" and any(
        word in text for word in resolution_words
    ):
        return "chime"

    # No event-grounded match means no cue. Never force a generic whoosh.
    return None


def create_cue(audio, scene_row, tracks):
    kind = choose_sfx(scene_row or {}, audio)
    if kind is None:
        return None

    track = tracks.get(f"sfx_{kind}")
    if not isinstance(track, dict) or track.get("status") != "completed":
        raise RuntimeError(f"Required event SFX is missing: {kind}")

    duration = audio["duration"]
    fraction = {
        "impact": 0.48, "footsteps": 0.22, "vehicle": 0.25,
        "door": 0.38, "weather": 0.18, "water": 0.35,
        "fire": 0.30, "heartbeat": 0.18, "phone": 0.58,
        "crowd": 0.42, "mystery": 0.32, "chime": 0.62,
    }[kind]
    start = min(duration * fraction, max(0.0, duration - 0.25))
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
        "selection_basis": "story_event_keywords_and_scene_context",
        "timing_basis": "measured_tts_audio_duration",
        "narration_duration_seconds": round(duration, 3),
        "narration_file": audio["path_relative"],
        "narration_sha256": audio["sha256"],
    }


def build_timeline(ordered):
    offset = 0.0
    timeline = []
    for row in ordered:
        end = offset + row["duration"]
        timeline.append({
            "scene_id": row["scene_id"],
            "part": row["part"], "scene": row["scene"],
            "role": row["role"],
            "start_seconds": round(offset, 4),
            "duration_seconds": round(row["duration"], 4),
            "end_seconds": round(end, 4),
            "audio": row["path_relative"],
            "audio_sha256": row["sha256"],
        })
        offset = end
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
        value = str(value).strip().lower()
        if value in {"true", "yes", "1", "on", "enabled"}:
            return True
        if value in {"false", "no", "0", "off", "disabled"}:
            return False
        raise RuntimeError(f"{key} must be true or false.")
    return default


def validate_track(record, name, minimum_duration):
    if not isinstance(record, dict) or record.get("status") != "completed":
        raise RuntimeError(f"{name} track is not completed.")
    path = safe_project_path(record.get("path"))
    duration = ffprobe_audio(path)
    checksum = sha256_file(path)
    if record.get("sha256") != checksum:
        raise RuntimeError(f"{name} track SHA256 mismatch.")
    if duration + 0.5 < minimum_duration:
        raise RuntimeError(f"{name} track is shorter than required.")
    record["duration_seconds"] = round(duration, 6)
    record["loudness"] = audio_loudness(path)
    return record


def validate_cues(cues, ordered):
    seen = set()
    for cue in cues:
        scene_id = cue.get("scene_id")
        if not scene_id or scene_id in seen:
            raise RuntimeError("Duplicate or missing SFX cue scene_id.")
        seen.add(scene_id)
        if cue.get("timing_basis") != "measured_tts_audio_duration":
            raise RuntimeError(f"Invalid SFX timing basis: {scene_id}")
        audio = next(
            (row for row in ordered if row["scene_id"] == scene_id), None
        )
        if audio is None or cue.get("narration_sha256") != audio["sha256"]:
            raise RuntimeError(f"SFX cue narration mapping mismatch: {scene_id}")
        start = float(cue["start_seconds"])
        duration = float(cue["duration_seconds"])
        if start < 0 or duration <= 0:
            raise RuntimeError(f"Invalid SFX cue timing: {scene_id}")
        if start + duration > audio["duration"] + 0.05:
            raise RuntimeError(f"SFX cue exceeds narration: {scene_id}")


def build_mix_records(tracks, cues, total_duration):
    mix = []
    music = tracks.get("music")
    if isinstance(music, dict) and music.get("status") == "completed":
        mix.append({
            "track_id": "background_music",
            "role": "music",
            "path": music["path"],
            "sha256": music["sha256"],
            "start_seconds": 0.0,
            "duration_seconds": music["duration_seconds"],
            "gain": 0.24,
            "ducking": {
                "enabled": True,
                "target_gain": 0.12,
                "attack_ms": 180,
                "release_ms": 650,
                "controlled_by": "narration",
            },
        })

    ambient = tracks.get("ambient")
    if isinstance(ambient, dict) and ambient.get("status") == "completed":
        mix.append({
            "track_id": "environment_ambience",
            "role": "ambient",
            "path": ambient["path"],
            "sha256": ambient["sha256"],
            "start_seconds": 0.0,
            "duration_seconds": ambient["duration_seconds"],
            "gain": 0.16,
            "ducking": {
                "enabled": True,
                "target_gain": 0.10,
                "attack_ms": 200,
                "release_ms": 700,
                "controlled_by": "narration",
            },
        })

    for index, cue in enumerate(cues, 1):
        mix.append({
            "track_id": f"sfx_{index:04d}_{cue['scene_id']}",
            "role": "sound_effect",
            "scene_id": cue["scene_id"],
            "part": cue["part"],
            "scene": cue["scene"],
            "narration_role": cue["role"],
            "kind": cue["kind"],
            "path": cue["file"],
            "sha256": cue["sha256"],
            "start_seconds": cue["start_seconds"],
            "duration_seconds": cue["duration_seconds"],
            "gain": cue["gain"],
            "ducking": {"enabled": False},
        })
    return mix


def build_part_summary(ordered, cues):
    cue_map = {}
    for cue in cues:
        cue_map.setdefault(cue["scene_id"], []).append(cue)
    result = {}
    for row in ordered:
        key = str(row["part"])
        part = result.setdefault(key, {
            "part": row["part"], "records": [],
            "role_counts": {
                "hook": 0, "main": 0, "suspense": 0, "resolution": 0,
            },
            "duration_seconds": 0.0, "sfx_cues": [],
        })
        part["records"].append({
            "scene_id": row["scene_id"],
            "scene": row["scene"], "role": row["role"],
            "duration_seconds": round(row["duration"], 4),
            "audio": row["path_relative"],
            "audio_sha256": row["sha256"],
        })
        part["role_counts"][row["role"]] += 1
        part["duration_seconds"] += row["duration"]
        part["sfx_cues"].extend(cue_map.get(row["scene_id"], []))
    for part in result.values():
        part["duration_seconds"] = round(part["duration_seconds"], 4)
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
        raise RuntimeError("PARTS and SCENES must be positive.")

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Both ffmpeg and ffprobe are required.")

    enabled_tracks = {
        "music": enabled(
            config, "MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
            "SOUNDTRACK_ENABLED", default=True,
        ),
        "ambient": enabled(
            config, "AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
            "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED", default=True,
        ),
        "sfx": enabled(
            config, "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
            "SOUND_EFFECTS_ENABLED", default=True,
        ),
    }

    for directory in (
        DESIGN_DIR, SFX_DIR, OUTPUT / "music",
        OUTPUT / "sound", CHECKPOINT_PATH.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    audio_records = load_audio_records()
    ordered = ordered_audio_records(audio_records)
    validate_record_structure(ordered, expected_parts, scenes_per_part)

    actual_parts = sorted({row["part"] for row in ordered})
    scene_records = load_scene_records()

    # Missing scene records are not silently replaced by generic cues.
    missing_scene_ids = [
        row["scene_id"] for row in ordered
        if row["scene_id"] not in scene_records
    ]
    if missing_scene_ids:
        raise RuntimeError(
            "scenes.json is missing narration IDs: "
            + ", ".join(missing_scene_ids[:20])
        )

    for row in ordered:
        scene = scene_records[row["scene_id"]]
        scene_role = str(scene.get("role", row["role"])).strip().lower()
        scene_part = int(scene.get("part", row["part"]))
        if scene_role != row["role"] or scene_part != row["part"]:
            raise RuntimeError(
                f"Scene/narration role or part mismatch: {row['scene_id']}"
            )
        if row["role"] == "main":
            scene_number = int(scene.get("scene", scene.get("local_scene", 0)))
            if scene_number != row["scene"]:
                raise RuntimeError(
                    f"Scene number mismatch: {row['scene_id']}"
                )
        elif scene.get("scene") not in (None, ""):
            raise RuntimeError(
                f"Extra role must use scene=None: {row['scene_id']}"
            )

    source_payload = {
        "version": VERSION,
        "format": fmt,
        "parts": actual_parts,
        "scenes_per_part": scenes_per_part,
        "enabled": enabled_tracks,
        "audio_records": [
            {
                "scene_id": row["scene_id"],
                "part": row["part"], "scene": row["scene"],
                "role": row["role"],
                "duration": round(row["duration"], 4),
                "sha256": row["sha256"],
                "tts_fingerprint": row["tts_fingerprint"],
            }
            for row in ordered
        ],
        "scene_manifest_sha256": sha256_file(SCENES_FILE),
    }
    source_sig = signature(source_payload)

    timeline, total_duration = build_timeline(ordered)
    old_tracks = load_previous_tracks()

    background_duration = max(30.0, total_duration + 8.0)
    tracks = {}

    if enabled_tracks["music"]:
        flow = load_flow_music(background_duration)
        tracks["music"] = flow or make_music(
            background_duration, old_tracks.get("music"), source_sig
        )
    else:
        tracks["music"] = None

    if enabled_tracks["ambient"]:
        tracks["ambient"] = make_ambient(
            total_duration, scene_records,
            old_tracks.get("ambient"), source_sig,
        )
    else:
        tracks["ambient"] = {
            "status": "skipped", "reason": "Disabled in Input configuration."
        }

    if enabled_tracks["sfx"]:
        for kind in SFX_TYPES:
            category = f"sfx_{kind}"
            tracks[category] = make_sfx(
                kind, SFX_DIR / f"{kind}.wav",
                old_tracks.get(category), source_sig,
            )
    else:
        for kind in SFX_TYPES:
            tracks[f"sfx_{kind}"] = None

    tracks["sfx"] = tracks.get("sfx_impact")

    if enabled_tracks["music"]:
        tracks["music"] = validate_track(
            tracks["music"], "music", total_duration
        )
    if tracks.get("ambient", {}).get("status") == "completed":
        tracks["ambient"] = validate_track(
            tracks["ambient"], "ambient", total_duration
        )

    for kind in SFX_TYPES:
        category = f"sfx_{kind}"
        if enabled_tracks["sfx"]:
            tracks[category] = validate_track(
                tracks[category], category, 0.1
            )

    cues = []
    if enabled_tracks["sfx"]:
        for audio in ordered:
            cue = create_cue(
                audio, scene_records[audio["scene_id"]], tracks
            )
            if cue is not None:
                cues.append(cue)

    validate_cues(cues, ordered)
    cue_map = {cue["scene_id"]: cue for cue in cues}
    part_summary = build_part_summary(ordered, cues)
    mix_records = build_mix_records(tracks, cues, total_duration)

    role_counts = {
        role: sum(1 for row in ordered if row["role"] == role)
        for role in ROLE_ORDER
    }
    main_scene_counts = {
        str(part): sum(
            1 for row in ordered
            if row["part"] == part and row["role"] == "main"
        )
        for part in actual_parts
    }

    music = tracks.get("music")
    using_flow = (
        isinstance(music, dict)
        and music.get("generation") == "google_flow_music"
    )
    external_api_calls = (
        int(music.get("external_api_calls", 1))
        if using_flow else 0
    )
    paid_services_used = bool(
        using_flow and music.get("paid_services_used", True)
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
        "scene_cues": cue_map,
        "part_cues": {
            key: value["sfx_cues"]
            for key, value in part_summary.items()
        },
        "timing_basis": "Measured TTS audio duration from ffprobe.",
        "timeline": timeline,
        "source_signature": source_sig,
        "source_audio_manifest_sha256": sha256_file(AUDIO_MANIFEST),
        "sfx_policy": (
            "Only story-event or role-grounded cues are emitted. "
            "No generic whoosh is forced. Extra roles retain scene=None."
        ),
        "external_api_calls": 0,
        "paid_services_used": False,
    }
    write_json(CUES_PATH, cue_manifest)

    mix_signature = signature({
        "source_signature": source_sig,
        "tracks": {
            key: (
                value.get("sha256")
                if isinstance(value, dict) else None
            )
            for key, value in tracks.items()
        },
        "mix_records": mix_records,
    })

    manifest = {
        "status": "completed",
        "version": VERSION,
        "generation": (
            "google_flow_music_with_event_sfx"
            if using_flow else "local_music_with_event_sfx"
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
        "sfx_cue_map": cue_map,
        "mix_records": mix_records,
        "mix_signature": mix_signature,
        "flow_music_manifest": FLOW_MUSIC_MANIFEST.relative_to(ROOT).as_posix(),
        "music_provider": (
            "Google Flow/Lyria" if using_flow
            else "local_synthesis_fallback"
            if enabled_tracks["music"] else "disabled"
        ),
        "ambient_status": tracks.get("ambient", {}).get("status"),
        "ambient_note": tracks.get("ambient", {}).get("reason"),
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
        "audio_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "narration_manifest": NARRATION_FILE.relative_to(ROOT).as_posix(),
        "scene_manifest": SCENES_FILE.relative_to(ROOT).as_posix(),
        "source_audio_manifest_sha256": sha256_file(AUDIO_MANIFEST),
        "source_narration_sha256": (
            sha256_file(NARRATION_FILE)
            if NARRATION_FILE.is_file() else None
        ),
        "source_scene_sha256": sha256_file(SCENES_FILE),
        "source_signature": source_sig,
        "sound_design_signature": mix_signature,
        "mixing_note": (
            "Music and available real ambience span the measured narration "
            "timeline. Music and ambience have separate ducking records. "
            "SFX are independently timed against measured narration audio. "
            "Final mix and narration ducking are applied by render_video.py."
        ),
        "completed_at": utc_now(),
    }

    write_json(MANIFEST_PATH, manifest)
    saved_manifest = read_json(MANIFEST_PATH)
    saved_cues = read_json(CUES_PATH)

    if not isinstance(saved_manifest, dict):
        raise RuntimeError("Sound design manifest read-back failed.")
    if saved_manifest.get("status") != "completed":
        raise RuntimeError("Sound design manifest is not completed.")
    if saved_manifest.get("sound_design_signature") != mix_signature:
        raise RuntimeError("Sound design signature mismatch.")
    if not isinstance(saved_cues, dict):
        raise RuntimeError("SFX cue manifest read-back failed.")
    if saved_cues.get("source_signature") != source_sig:
        raise RuntimeError("SFX cue source signature mismatch.")
    if len(saved_cues.get("cues", [])) != len(cues):
        raise RuntimeError("SFX cue count mismatch.")

    write_json(CHECKPOINT_PATH, {
        "status": "completed",
        "version": VERSION,
        "source_signature": source_sig,
        "sound_design_signature": mix_signature,
        "total_audio_records": len(ordered),
        "completed_tracks": [
            key for key, value in tracks.items()
            if isinstance(value, dict)
            and value.get("status") == "completed"
        ],
        "cue_count": len(cues),
        "updated_at": utc_now(),
    })

    log("SOUND DESIGN READY")
    log("FORMAT:", fmt)
    log("PARTS:", len(actual_parts))
    log("MAIN SCENES:", sum(main_scene_counts.values()))
    log("HOOK:", role_counts["hook"])
    log("SUSPENSE:", role_counts["suspense"])
    log("RESOLUTION:", role_counts["resolution"])
    log("NARRATION SECONDS:", round(total_duration, 2))
    log("SFX CUES:", len(cues))
    log("AMBIENT:", tracks.get("ambient", {}).get("status"))
    log("MANIFEST:", MANIFEST_PATH.relative_to(ROOT))
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