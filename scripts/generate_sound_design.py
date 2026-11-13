#!/usr/bin/env python3
"""Verified, resumable story-aware sound design for photo-motion videos."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts_range,
    get_selected_parts,
    get_scenes_range,
)

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
NARRATION_FILE = OUT / "narration" / "narration.json"
SCENES_FILE = OUT / "scenes" / "scenes.json"
FLOW_MANIFEST = OUT / "music" / "flow_music_manifest.json"

MUSIC_PATH = OUT / "music" / "background_music.wav"
AMBIENT_PATH = OUT / "sound" / "ambient_environment.wav"
SFX_DIR = OUT / "sfx"
DESIGN_DIR = OUT / "sound_design"

MANIFEST_PATH = DESIGN_DIR / "sound_design_manifest.json"
CUES_PATH = DESIGN_DIR / "sfx_cues.json"
SFX_VALIDATION_PATH = DESIGN_DIR / "sfx_timing_validation.json"
CHECKPOINT_PATH = OUT / "checkpoints" / "sound_design_progress.json"

VERSION = "photo-motion-sound-design-v36-tonal-ambient-fallback"
RATE = 22050
SFX_SECONDS = 8.0
DEFAULT_VISUAL_SCENE_SECONDS = 6.0
MIN_AUDIO_BYTES = 100

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

SFX_TYPES = (
    "whoosh", "impact", "footsteps", "vehicle", "door",
    "weather", "water", "fire", "heartbeat", "phone",
    "crowd", "mystery", "chime",
)

# Cue gain is later multiplied by render_video.py's 1.65 factor.
# Values are intentionally stronger than the previous v30 values.
SFX_GAINS = {
    "whoosh": 0.82,
    "impact": 0.92,
    "footsteps": 0.82,
    "vehicle": 0.84,
    "door": 0.88,
    "weather": 0.78,
    "water": 0.76,
    "fire": 0.78,
    "heartbeat": 0.80,
    "phone": 0.88,
    "crowd": 0.82,
    "mystery": 0.82,
    "chime": 0.90,
}

EVENT_WORDS = {
    "impact": (
        "crash", "slam", "bang", "explosion", "gunshot",
        "धमाका", "टक्कर", "टकराया", "गिर पड़ा", "टूट गया",
        "गोली", "विस्फोट", "प्रहार",
    ),
    "footsteps": (
        "footstep", "footsteps", "walking", "running",
        "कदमों", "चलने लगा", "चल रही", "भागा", "भागी",
        "दौड़ पड़ा", "दौड़ने लगा", "पैरों की आहट",
    ),
    "vehicle": (
        "car", "truck", "bus", "train", "motorcycle",
        "vehicle", "engine", "horn", "कार", "ट्रक",
        "बस", "ट्रेन", "मोटरसाइकिल", "गाड़ी", "वाहन",
        "इंजन", "हॉर्न",
    ),
    "door": (
        "door", "gate", "knock", "lock", "दरवाजा",
        "दरवाज़ा", "दरवाजे", "दरवाज़े", "गेट", "ताला",
        "खटखटाया",
    ),
    "weather": (
        "storm", "thunder", "lightning", "rain", "wind",
        "तूफान", "तूफ़ान", "बिजली", "गरज", "बारिश",
        "आंधी", "आँधी", "तेज हवा",
    ),
    "water": (
        "river", "sea", "ocean", "water", "wave",
        "नदी", "समुद्र", "पानी", "लहर", "बाढ़", "झरना",
    ),
    "fire": (
        "fire", "flame", "burning", "smoke",
        "आग", "लपट", "जलता", "जलती", "धुआं", "धुआँ",
    ),
    "heartbeat": (
        "heartbeat", "heart beat", "pulse",
        "धड़कन", "धड़कन", "सांसें तेज", "घबराहट",
    ),
    "phone": (
        "phone", "mobile", "telephone", "ringtone",
        "notification", "फोन", "मोबाइल", "कॉल",
        "घंटी बजी", "अलार्म", "संदेश आया",
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
        "bell", "chime", "ring", "घंटी", "मंदिर",
        "शुभ समाचार", "खुशी की खबर", "सफलता की खबर",
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


def log(*values):
    print(*values, flush=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def signature(data):
    raw = json.dumps(
        data, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path, required=True, default=None):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required JSON missing: {path}")
        return default
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (OSError, ValueError, TypeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON file {path}: {exc}") from exc
        log("IGNORING INVALID OPTIONAL SOUND-DESIGN CHECKPOINT:", path, str(exc)[:300])
        return default
    if not isinstance(value, (dict, list)):
        if required:
            raise RuntimeError(f"Invalid JSON root: {path}")
        return default
    return value


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_project_path(value, label="audio", must_exist=True):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path is missing.")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory.")
    if must_exist and not path.is_file():
        raise RuntimeError(f"{label}: file does not exist: {path}")
    return path


def run(command, timeout=300):
    result = subprocess.run(
        [str(item) for item in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-2500:]
        raise RuntimeError(
            f"Command failed ({result.returncode}): {detail}"
        )
    return result


def require_media_tools():
    missing = [
        name for name in ("ffmpeg", "ffprobe")
        if not shutil.which(name)
    ]
    if missing:
        raise RuntimeError(
            "Required media tools missing: " + ", ".join(missing)
        )


def ffprobe_audio(path):
    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ], timeout=90)
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            row.get("codec_type") == "audio"
            for row in data.get("streams", [])
        )
    except Exception as exc:
        raise RuntimeError(f"Invalid audio metadata: {path}") from exc
    if not math.isfinite(duration) or duration <= 0 or not has_audio:
        raise RuntimeError(f"Audio is empty or has no audio stream: {path}")
    return duration


def audio_loudness(path):
    result = run([
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path), "-af", "volumedetect",
        "-f", "null", "-",
    ], timeout=120)
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
    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"WAV missing or too small: {path}")
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1:
                raise RuntimeError("WAV must be mono.")
            if audio.getsampwidth() != 2:
                raise RuntimeError("WAV must use 16-bit PCM.")
            if audio.getframerate() != RATE:
                raise RuntimeError("WAV sample rate must be 22050 Hz.")
            frames = audio.getnframes()
            if frames <= 0:
                raise RuntimeError("WAV contains no audio frames.")
            duration = frames / audio.getframerate()
    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"Invalid WAV file: {path}") from exc
    if duration + 1 / RATE < minimum_seconds:
        raise RuntimeError(f"WAV is shorter than required: {path}")
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
                raise RuntimeError(
                    f"Audio frame mismatch: {count}/{expected}."
                )
        validate_wav(temporary, min(0.1, duration))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def envelope(t, duration, attack=0.08, release=0.5):
    return (
        min(1.0, t / max(0.01, attack))
        * min(1.0, max(0.0, duration - t) / max(0.01, release))
    )


def sfx_samples(kind, duration=SFX_SECONDS, seed=771):
    # Deterministic tonal/transient design replaces the previous broadband
    # noise-heavy whoosh/ambient cues that sounded like continuous hiss.
    count = int(round(RATE * duration))
    phase = random.Random(seed).random() * math.tau
    for index in range(count):
        t = index / RATE
        progress = index / max(1, count - 1)
        fade = envelope(t, duration, 0.035, 0.65)

        if kind == "whoosh":
            swell = math.sin(math.pi * progress) ** 1.6
            frequency = 95.0 + 360.0 * progress
            value = swell * (
                0.30 * math.sin(2 * math.pi * frequency * t)
                + 0.10 * math.sin(2 * math.pi * frequency * 0.5 * t)
            )
        elif kind == "impact":
            decay = math.exp(-3.2 * t)
            value = 0.62 * math.sin(2 * math.pi * 52 * t) * decay
            value += 0.12 * math.sin(2 * math.pi * 104 * t) * math.exp(-5 * t)
        elif kind == "footsteps":
            phase_step = (t * 2.0) % 1.0
            thump = math.exp(-((phase_step - 0.08) / 0.055) ** 2)
            value = 0.48 * thump * math.sin(2 * math.pi * 72 * t)
            value += 0.08 * thump * math.sin(2 * math.pi * 144 * t)
        elif kind == "vehicle":
            frequency = 58.0 + 70.0 * progress
            value = 0.30 * math.sin(2 * math.pi * frequency * t)
            value += 0.12 * math.sin(2 * math.pi * frequency * 2.0 * t + phase)
        elif kind == "door":
            value = 0.42 * math.sin(2 * math.pi * 88 * t) * math.exp(-2.5 * t)
            value += 0.16 * math.sin(2 * math.pi * 176 * t) * math.exp(-3.5 * t)
        elif kind == "weather":
            # Soft low-mid wind bed, deliberately no high-frequency hiss.
            modulation = 0.65 + 0.35 * math.sin(2 * math.pi * 0.12 * t + phase)
            value = modulation * (
                0.16 * math.sin(2 * math.pi * 54 * t)
                + 0.10 * math.sin(2 * math.pi * 81 * t + phase)
            )
        elif kind == "water":
            value = 0.16 * math.sin(2 * math.pi * 92 * t)
            value += 0.10 * math.sin(2 * math.pi * 138 * t + phase)
            value += 0.05 * math.sin(2 * math.pi * 184 * t)
        elif kind == "fire":
            flicker = 0.75 + 0.25 * math.sin(2 * math.pi * 2.7 * t + phase)
            value = flicker * (
                0.12 * math.sin(2 * math.pi * 68 * t)
                + 0.07 * math.sin(2 * math.pi * 102 * t)
            )
        elif kind == "heartbeat":
            phase_beat = (t * 1.05) % 1.0
            pulse = (
                0.82 * math.exp(-((phase_beat - 0.10) / 0.06) ** 2)
                + 0.45 * math.exp(-((phase_beat - 0.28) / 0.075) ** 2)
            )
            value = pulse * math.sin(2 * math.pi * 48 * t)
        elif kind == "phone":
            pulse = 1.0 if int(t * 2.0) % 2 == 0 else 0.0
            value = pulse * (
                0.22 * math.sin(2 * math.pi * 620 * t)
                + 0.12 * math.sin(2 * math.pi * 780 * t)
            )
        elif kind == "crowd":
            value = (
                0.09 * math.sin(2 * math.pi * 110 * t)
                + 0.07 * math.sin(2 * math.pi * 147 * t + phase)
                + 0.05 * math.sin(2 * math.pi * 196 * t)
            )
        elif kind == "mystery":
            wobble = 4 * math.sin(2 * math.pi * 0.3 * t)
            value = 0.28 * math.sin(2 * math.pi * (110 + wobble) * t)
            value += 0.13 * math.sin(2 * math.pi * 164.81 * t)
        else:
            decay = math.exp(-1.0 * t)
            value = decay * (
                0.26 * math.sin(2 * math.pi * 523.25 * t)
                + 0.16 * math.sin(2 * math.pi * 659.25 * t)
                + 0.09 * math.sin(2 * math.pi * 783.99 * t)
            )
        yield value * fade


def music_samples(duration, seed=2718):
    # Original cinematic instrumental bed: evolving minor/major harmony,
    # low pulse and a restrained arpeggio. No broadband hiss or static.
    progressions = (
        (110.00, 130.81, 164.81, 196.00),
        (98.00, 116.54, 146.83, 174.61),
        (130.81, 155.56, 196.00, 233.08),
        (92.50, 110.00, 138.59, 164.81),
    )
    phase = random.Random(seed).random() * math.tau
    for index in range(int(round(RATE * duration))):
        t = index / RATE
        chord = progressions[int(t // 8) % len(progressions)]
        beat_phase = (t * 1.5) % 1.0
        beat = math.exp(-((beat_phase - 0.08) / 0.12) ** 2)
        pad = (
            0.17 * math.sin(2 * math.pi * chord[0] * t)
            + 0.12 * math.sin(2 * math.pi * chord[1] * t)
            + 0.09 * math.sin(2 * math.pi * chord[2] * t)
            + 0.05 * math.sin(2 * math.pi * chord[3] * t)
        )
        arpeggio_note = chord[int((t * 2.0) % 4)]
        arp_envelope = 0.5 + 0.5 * math.sin(math.pi * ((t * 2.0) % 1.0))
        arpeggio = 0.055 * math.sin(2 * math.pi * arpeggio_note * 2.0 * t) * arp_envelope
        bass = 0.08 * math.sin(2 * math.pi * (chord[0] * 0.5) * t) * (0.7 + 0.3 * beat)
        pulse = 0.90 + 0.10 * math.sin(2 * math.pi * 0.08 * t + phase)
        yield (pad + arpeggio + bass) * pulse * envelope(t, duration, 1.8, 3.0)


def make_local_track(
    path, duration, old_record, source_sig, track_name, generator,
):
    track_signature = signature({
        "version": VERSION,
        "track": track_name,
        "duration": round(duration, 4),
        "rate": RATE,
        "source_signature": source_sig,
    })
    if path.is_file() and isinstance(old_record, dict):
        try:
            actual = validate_wav(path, duration)
            checksum = sha256_file(path)
            if (
                old_record.get("signature") == track_signature
                and old_record.get("sha256") == checksum
                and abs(actual - duration) <= 0.10
            ):
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": track_signature,
                    "sha256": checksum,
                    "duration_seconds": round(actual, 6),
                    "generation": "local_deterministic_synthesis",
                    "provider": "local_synthesis",
                    "reused": True,
                }
        except Exception:
            pass
    write_wav(path, generator(duration), duration)
    actual = validate_wav(path, duration)
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": track_signature,
        "sha256": sha256_file(path),
        "duration_seconds": round(actual, 6),
        "generation": "local_deterministic_synthesis",
        "provider": "local_synthesis",
        "reused": False,
    }


def make_sfx(kind, path, old_record, source_sig):
    path = Path(path)
    track_signature = signature({
        "version": VERSION,
        "kind": kind,
        "duration": SFX_SECONDS,
        "rate": RATE,
        "source_signature": source_sig,
    })
    if path.is_file() and isinstance(old_record, dict):
        try:
            duration = validate_wav(path, SFX_SECONDS)
            checksum = sha256_file(path)
            if (
                old_record.get("signature") == track_signature
                and old_record.get("sha256") == checksum
                and abs(duration - SFX_SECONDS) <= 0.10
            ):
                return {
                    "status": "completed",
                    "path": path.relative_to(ROOT).as_posix(),
                    "signature": track_signature,
                    "sha256": checksum,
                    "duration_seconds": round(duration, 6),
                    "generation": "local_event_sfx",
                    "reused": True,
                }
        except Exception:
            pass
    seed = int(track_signature[:8], 16)
    write_wav(path, sfx_samples(kind, SFX_SECONDS, seed), SFX_SECONDS)
    normalized = path.with_name(path.stem + ".normalized.wav")
    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=8,alimiter=limit=0.95:level=disabled",
            "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le",
            str(normalized),
        ], timeout=180)
        validate_wav(normalized, SFX_SECONDS)
        os.replace(normalized, path)
    finally:
        normalized.unlink(missing_ok=True)
    duration = validate_wav(path, SFX_SECONDS)
    loudness = audio_loudness(path)
    if loudness.get("mean_volume_db", -999) < -25.0:
        raise RuntimeError(f"Generated {kind} SFX is too quiet after normalization.")
    return {
        "status": "completed",
        "path": path.relative_to(ROOT).as_posix(),
        "signature": track_signature,
        "sha256": sha256_file(path),
        "duration_seconds": round(duration, 6),
        "loudness": loudness,
        "generation": "local_event_sfx_normalized",
        "provider": "local_synthesis",
        "reused": False,
    }


def make_ambient(total_duration, scene_records, old_record, source_sig):
    candidates = [
        OUT / "sound" / "ambient_sources",
        ROOT / "assets" / "sounds" / "ambient",
        ROOT / "assets" / "audio" / "ambient",
    ]
    available = []
    extensions = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
    for candidate in candidates:
        if candidate.is_file():
            available.append(candidate)
        elif candidate.is_dir():
            available.extend(sorted(
                p for p in candidate.rglob("*")
                if p.is_file() and p.suffix.lower() in extensions
            ))
    if not available:
        # Keep an enabled ambient track valid even when no external ambience
        # file is bundled. This deterministic local fallback is resumable.
        fallback = make_local_track(
            AMBIENT_PATH,
            total_duration + 3.0,
            old_record,
            source_sig,
            "ambient_fallback",
            lambda seconds: music_samples(
                seconds, seed=int(source_sig[:8], 16)
            ),
        )
        fallback.update({
            "generation": "local_deterministic_ambient_fallback",
            "provider": "local_synthesis",
            "reason": "No real ambient audio asset was available; local fallback used.",
            "environment": [],
        })
        return fallback

    story_text = " ".join(
        str(value or "")
        for row in scene_records.values()
        for value in (
            row.get("narration"), row.get("text"),
            row.get("visual_prompt"), row.get("location"),
            row.get("setting"), row.get("environment"),
        )
    ).casefold()
    desired = [
        category for category, words in AMBIENT_KEYWORDS.items()
        if any(word in story_text for word in words)
    ]
    ranked = []
    for path in available:
        name = path.stem.casefold()
        score = sum(1 for category in desired if category in name)
        ranked.append((score, path))
    ranked.sort(key=lambda item: (-item[0], item[1].as_posix()))
    source = ranked[0][1]
    source_hash = sha256_file(source)
    duration_needed = total_duration + 3.0
    track_signature = signature({
        "version": VERSION,
        "source_sha256": source_hash,
        "duration": round(duration_needed, 3),
        "source_signature": source_sig,
    })

    if AMBIENT_PATH.is_file() and isinstance(old_record, dict):
        try:
            actual = ffprobe_audio(AMBIENT_PATH)
            checksum = sha256_file(AMBIENT_PATH)
            if (
                old_record.get("signature") == track_signature
                and old_record.get("sha256") == checksum
                and actual + 0.5 >= total_duration
            ):
                return {
                    "status": "completed",
                    "path": AMBIENT_PATH.relative_to(ROOT).as_posix(),
                    "signature": track_signature,
                    "sha256": checksum,
                    "duration_seconds": round(actual, 6),
                    "generation": "reused_real_ambient_asset",
                    "source_audio": source.relative_to(ROOT).as_posix(),
                    "source_sha256": source_hash,
                    "environment": desired,
                    "reused": True,
                }
        except Exception:
            pass

    temporary = AMBIENT_PATH.with_name("ambient_environment.building.wav")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1", "-i", str(source),
        "-t", f"{duration_needed:.3f}", "-vn",
        "-af", (
            "aresample=22050,highpass=f=35,lowpass=f=10000,"
            "loudnorm=I=-23:TP=-2:LRA=10,"
            f"afade=t=out:st={max(0.0, total_duration - 1.5):.3f}:d=4.5"
        ),
        "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le",
        str(temporary),
    ], timeout=900)
    actual = ffprobe_audio(temporary)
    if actual + 0.5 < total_duration:
        raise RuntimeError("Ambient audio is shorter than narration timeline.")
    os.replace(temporary, AMBIENT_PATH)
    return {
        "status": "completed",
        "path": AMBIENT_PATH.relative_to(ROOT).as_posix(),
        "signature": track_signature,
        "sha256": sha256_file(AMBIENT_PATH),
        "duration_seconds": round(actual, 6),
        "generation": "real_ambient_asset",
        "source_audio": source.relative_to(ROOT).as_posix(),
        "source_sha256": source_hash,
        "environment": desired,
        "reused": False,
    }


def record_identity(row):
    if not isinstance(row, dict):
        raise RuntimeError("Narration audio record must be an object.")
    try:
        part = int(row["part"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Narration audio record has invalid Part.") from exc

    role = str(row.get("role", "main")).strip().lower()
    role = {
        "opening": "hook", "intro": "hook",
        "cliffhanger": "suspense", "ending": "resolution",
        "final": "resolution",
    }.get(role, role)
    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported audio role: {role!r}")

    if role == "main":
        try:
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Main narration record has no Scene number.") from exc
        if scene < 1:
            raise RuntimeError("Main Scene number must be positive.")
        scene_id = str(row.get("scene_id") or f"part_{part:02d}_scene_{scene:02d}").strip()
    else:
        scene = None
        scene_id = str(row.get("scene_id") or f"part_{part:02d}_{role}").strip()

    if part < 1 or not scene_id:
        raise RuntimeError("Invalid narration record identity.")
    return scene_id, part, role, scene


def parse_visual_duration(scene, scene_id):
    value = scene.get("scene_duration") or scene.get("duration")
    try:
        duration = float(str(value).strip().rstrip("sS"))
    except (TypeError, ValueError):
        duration = DEFAULT_VISUAL_SCENE_SECONDS
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid visual Scene duration: {scene_id}")
    return duration


def load_audio_records():
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("TTS audio manifest is missing or incomplete.")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("TTS audio manifest contains no narration jobs.")

    audio_records = {}
    for row in jobs:
        if not isinstance(row, dict) or row.get("status") not in {"completed", "reused"}:
            raise RuntimeError("TTS manifest contains an incomplete job.")
        scene_id, part, role, scene = record_identity(row)
        if scene_id in audio_records:
            raise RuntimeError(f"Duplicate narration scene_id: {scene_id}")
        audio_value = next((
            row.get(key) for key in (
                "audio_path", "audio_file", "path", "file", "audio"
            )
            if isinstance(row.get(key), str) and row[key].strip()
        ), None)
        path = safe_project_path(audio_value, f"Narration audio {scene_id}")
        duration = ffprobe_audio(path)
        checksum = sha256_file(path)
        if row.get("sha256") != checksum:
            raise RuntimeError(f"Narration SHA256 mismatch: {scene_id}")
        try:
            declared = float(row.get("actual_audio_duration_seconds", row.get("duration")))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Missing measured duration: {scene_id}") from exc
        if not math.isfinite(declared) or abs(declared - duration) > 0.20:
            raise RuntimeError(f"Measured duration mismatch: {scene_id}")
        if (row.get("duration_source") or row.get("audio_duration_source")) != "ffprobe_measured":
            raise RuntimeError(f"Unverified audio duration source: {scene_id}")
        fingerprint = row.get("tts_fingerprint") or row.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise RuntimeError(f"TTS fingerprint is missing: {scene_id}")
        if role != "main" and (
            row.get("voiceover_enabled") is not True
            or row.get("narration_source") != "ai_generated_role_narration"
        ):
            raise RuntimeError(f"{scene_id}: extra-role TTS metadata is invalid.")
        audio_records[scene_id] = {
            "scene_id": scene_id, "part": part, "scene": scene,
            "role": role, "duration": duration,
            "path": path, "path_relative": path.relative_to(ROOT).as_posix(),
            "sha256": checksum,
            "text": str(row.get("text", row.get("narration", ""))),
            "provider": row.get("provider"), "voice": row.get("voice"),
            "tts_fingerprint": fingerprint, "visual_only": False,
        }

    scene_records = load_scene_records()
    records = {}
    for scene_id, scene_row in scene_records.items():
        scene_id_check, part, role, scene = record_identity(scene_row)
        if scene_id_check != scene_id:
            raise RuntimeError(f"Visual Scene identity mismatch: {scene_id}")
        if scene_id in records:
            raise RuntimeError(f"Duplicate visual Scene ID: {scene_id}")
        if scene_row.get("status") != "completed":
            raise RuntimeError(f"Visual Scene is incomplete: {scene_id}")
        source_signature = str(scene_row.get("source_scene_signature") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", source_signature):
            raise RuntimeError(f"Visual Scene source signature is invalid: {scene_id}")

        audio = audio_records.pop(scene_id, None)
        if audio is None:
            raise RuntimeError(f"Scene has no matching TTS audio: {scene_id}")
        if re.sub(r"\s+", " ", audio["text"]).strip() != re.sub(
            r"\s+", " ", str(scene_row.get("narration", ""))
        ).strip():
            raise RuntimeError(f"Scene narration differs from TTS text: {scene_id}")
        if scene_row.get("voiceover_enabled") is not True:
            raise RuntimeError(f"{scene_id}: role voiceover must be enabled.")
        if role != "main" and scene_row.get("narration_source") != "ai_generated_role_narration":
            raise RuntimeError(f"{scene_id}: extra-role narration source is invalid.")
        audio["source_scene_signature"] = source_signature
        records[scene_id] = audio

    if audio_records:
        raise RuntimeError(
            "TTS jobs do not match scenes.json: "
            + ", ".join(sorted(audio_records)[:20])
        )
    return records

def ordered_audio_records(records):
    return sorted(
        records.values(),
        key=lambda row: (
            row["part"], ROLE_ORDER[row["role"]],
            row["scene"] or 0, row["scene_id"],
        ),
    )


def validate_record_structure(ordered, expected_parts, scenes_min, scenes_max, actual_scene_counts):
    parts = sorted({row["part"] for row in ordered})
    if len(parts) != expected_parts:
        raise RuntimeError(
            f"PARTS mismatch: selected {expected_parts}, audio has {len(parts)}."
        )
    if parts != list(range(1, expected_parts + 1)):
        raise RuntimeError("Part numbering must be continuous from 1.")
    if len(actual_scene_counts) != expected_parts:
        raise RuntimeError("actual_scene_counts does not match Part count.")

    by_part = {}
    for row in ordered:
        by_part.setdefault(row["part"], []).append(row)

    for part in parts:
        rows = by_part[part]
        roles = [row["role"] for row in rows]
        expected_suspense = 1 if part < expected_parts else 0
        expected_resolution = 1 if part == expected_parts else 0

        if roles.count("hook") != 1:
            raise RuntimeError(f"Part {part} must have exactly one Hook.")
        if roles.count("suspense") != expected_suspense:
            raise RuntimeError(f"Part {part} has invalid Suspense count.")
        if roles.count("resolution") != expected_resolution:
            raise RuntimeError(f"Part {part} has invalid Resolution count.")

        main = sorted(row["scene"] for row in rows if row["role"] == "main")
        expected_count = actual_scene_counts[part - 1]
        if not scenes_min <= expected_count <= scenes_max:
            raise RuntimeError(
                f"Part {part} Scene count is outside {scenes_min}-{scenes_max}."
            )
        if main != list(range(1, expected_count + 1)):
            raise RuntimeError(
                f"Part {part} Main Scene IDs mismatch: expected 1-{expected_count}, found {main}."
            )

        expected_records = expected_count + 1 + expected_suspense + expected_resolution
        if len(rows) != expected_records:
            raise RuntimeError(f"Part {part} narration record count mismatch.")

    ids = [row["scene_id"] for row in ordered]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate narration scene IDs detected.")


def load_scene_records():
    data = read_json(SCENES_FILE)
    if not isinstance(data, dict):
        raise RuntimeError("scenes.json root must be an object.")

    rows = data.get("scenes")
    if not isinstance(rows, list):
        rows = []

    if not rows and isinstance(data.get("parts"), list):
        for index, part_data in enumerate(data["parts"], 1):
            if not isinstance(part_data, dict):
                continue
            part = int(part_data.get("part", index))
            for scene_index, item in enumerate(part_data.get("scenes", []), 1):
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

        scene_id = str(row.get("scene_id") or (
            f"part_{part:02d}_scene_{scene:02d}"
            if role == "main" else f"part_{part:02d}_{role}"
        )).strip()
        if scene_id in result:
            raise RuntimeError(f"Duplicate scene record: {scene_id}")
        result[scene_id] = row

    if not result:
        raise RuntimeError("scenes.json contains no usable scene records.")
    return result


def choose_sfx(scene_row, audio):
    text = " ".join(str(value or "") for value in (
        scene_row.get("sfx"), scene_row.get("sfx_prompt"),
        scene_row.get("title"), scene_row.get("narration"),
        scene_row.get("text"), scene_row.get("mood"),
        scene_row.get("visual_prompt"), scene_row.get("beat_description"),
        scene_row.get("suspense_prompt"), scene_row.get("location"),
        audio.get("text"),
    )).casefold()

    matches = []
    for kind, words in EVENT_WORDS.items():
        count = sum(1 for word in words if word.casefold() in text)
        if count:
            matches.append((count, kind))
    if matches:
        matches.sort(key=lambda item: (-item[0], item[1]))
        return matches[0][1]
    if audio["role"] in {"hook", "suspense"}:
        return "mystery"

    resolution_words = (
        "resolved", "reunited", "safe", "victory", "truth revealed",
        "समाधान", "मिल गया", "मिल गई", "सच सामने", "जीत",
        "सुरक्षित", "न्याय मिला",
    )
    if audio["role"] == "resolution" and any(word in text for word in resolution_words):
        # Avoid a short electronic beep as the only ending sound.
        return "impact"
    # Every Main Scene receives a restrained cinematic transition cue when
    # no literal event-specific effect is present; silence is not reported
    # as a completed SFX design.
    if audio["role"] == "main":
        return "whoosh"
    if audio["role"] == "resolution":
        return "impact"
    return None

def build_timeline(ordered):
    offset = 0.0
    timeline = []
    for row in ordered:
        end = offset + row["duration"]
        timeline.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "start_seconds": round(offset, 4),
            "duration_seconds": round(row["duration"], 4),
            "end_seconds": round(end, 4),
            "audio": row.get("path_relative", ""),
            "audio_sha256": row.get("sha256", ""),
            "source_scene_signature": row.get("source_scene_signature", ""),
            "visual_only": bool(row.get("visual_only", False)),
        })
        offset = end
    return timeline, round(offset, 4)


def load_previous_tracks():
    old = read_json(MANIFEST_PATH, required=False, default={})
    tracks = old.get("tracks", {}) if isinstance(old, dict) else {}
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
    path = safe_project_path(record.get("path"), f"{name} track")
    duration = ffprobe_audio(path)
    checksum = sha256_file(path)
    if record.get("sha256") != checksum:
        raise RuntimeError(f"{name} track SHA256 mismatch.")
    if duration + 0.5 < minimum_duration:
        raise RuntimeError(f"{name} track is shorter than required.")
    if name.startswith("sfx_") and abs(duration - SFX_SECONDS) > 0.10:
        raise RuntimeError(f"{name} duration must be exactly {SFX_SECONDS:.1f} seconds.")
    record["duration_seconds"] = round(duration, 6)
    record["loudness"] = audio_loudness(path)
    if name.startswith("sfx_"):
        mean = record["loudness"].get("mean_volume_db")
        peak = record["loudness"].get("peak_volume_db")
        if mean is None or peak is None or not math.isfinite(mean) or not math.isfinite(peak):
            raise RuntimeError(f"{name} loudness metadata is incomplete.")
        if mean < -25.0 or peak > -0.1:
            raise RuntimeError(f"{name} loudness is outside safe audible limits.")
    return record


def validate_cues(cues, ordered):
    seen = set()
    audio_map = {row["scene_id"]: row for row in ordered}
    for cue in cues:
        sid = cue.get("scene_id")
        if not sid or sid in seen:
            raise RuntimeError("Duplicate or missing SFX cue scene_id.")
        seen.add(sid)
        audio = audio_map.get(sid)
        if audio is None:
            raise RuntimeError(f"SFX cue source mapping is missing: {sid}")
        expected_basis = (
            "visual_scene_duration" if audio.get("visual_only")
            else "measured_tts_audio_duration"
        )
        if cue.get("timing_basis") != expected_basis:
            raise RuntimeError(f"Invalid SFX timing basis: {sid}")
        if cue.get("narration_sha256", "") != audio["sha256"]:
            raise RuntimeError(f"SFX cue source hash mismatch: {sid}")

        start = float(cue["start_seconds"])
        duration = float(cue["duration_seconds"])
        gain = float(cue["gain"])
        if (
            not math.isfinite(start) or not math.isfinite(duration)
            or not math.isfinite(gain) or start < 0 or duration <= 0
            or not 0 <= gain <= 1
            or start + duration > audio["duration"] + 0.05
        ):
            raise RuntimeError(f"Invalid SFX cue timing or gain: {sid}")

        path = safe_project_path(cue["file"], f"SFX cue {sid}")
        if sha256_file(path) != cue["sha256"]:
            raise RuntimeError(f"SFX cue asset checksum mismatch: {sid}")
        if ffprobe_audio(path) <= 0:
            raise RuntimeError(f"SFX cue asset has no audio: {sid}")


def create_cue(audio, scene_row, tracks):
    kind = choose_sfx(scene_row or {}, audio)
    if kind is None:
        return None
    track = tracks.get(f"sfx_{kind}")
    if not isinstance(track, dict) or track.get("status") != "completed":
        raise RuntimeError(f"Required event SFX is missing: {kind}")

    # Keep events short and recognizable; avoid stretching a cue over
    # almost the entire narration. Long beds are handled separately.
    cue_limits = {
        "whoosh": 4.0, "impact": 2.2, "footsteps": 7.5,
        "vehicle": 7.5, "door": 2.8, "weather": 8.0,
        "water": 7.5, "fire": 7.0, "heartbeat": 7.0,
        "phone": 3.5, "crowd": 7.5, "mystery": 8.0, "chime": 2.8,
    }
    fractions = {
        "whoosh": 0.12, "impact": 0.20, "footsteps": 0.36,
        "vehicle": 0.40, "door": 0.22, "weather": 0.42,
        "water": 0.40, "fire": 0.38, "heartbeat": 0.36,
        "phone": 0.25, "crowd": 0.40, "mystery": 0.40, "chime": 0.22,
    }

    narration_duration = audio["duration"]
    cue_duration = min(
        cue_limits[kind], SFX_SECONDS, narration_duration
    )
    if cue_duration < 0.20:
        return None

    requested_start = narration_duration * fractions[kind]
    latest_start = max(0.0, narration_duration - cue_duration)
    start = min(requested_start, latest_start)
    usable = min(cue_duration, narration_duration - start)
    if usable <= 0:
        return None

    return {
        "scene_id": audio["scene_id"],
        "part": audio["part"],
        "scene": audio["scene"],
        "role": audio["role"],
        "kind": kind,
        "time": round(start, 3),
        "start_seconds": round(start, 3),
        "duration_seconds": round(usable, 3),
        "file": track["path"],
        "sha256": track["sha256"],
        "gain": SFX_GAINS[kind],
        "selection_basis": "story_event_keywords_and_scene_context",
        "timing_basis": (
            "visual_scene_duration" if audio.get("visual_only")
            else "measured_tts_audio_duration"
        ),
        "scene_duration_seconds": round(narration_duration, 3),
        "narration_duration_seconds": (
            None if audio.get("visual_only") else round(narration_duration, 3)
        ),
        "narration_file": audio.get("path_relative") or None,
        "narration_sha256": audio["sha256"],
    }


def build_part_summary(ordered, cues):
    cue_map = {}
    for cue in cues:
        cue_map.setdefault(cue["scene_id"], []).append(cue)
    result = {}
    for row in ordered:
        key = str(row["part"])
        part = result.setdefault(key, {
            "part": row["part"], "records": [],
            "role_counts": {role: 0 for role in ROLE_ORDER},
            "duration_seconds": 0.0, "sfx_cues": [],
        })
        part["records"].append({
            "scene_id": row["scene_id"], "scene": row["scene"],
            "role": row["role"],
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


def build_mix_records(tracks, cues):
    mix = []
    for category, role, gain, target in (
        ("music", "music", 0.38, 0.18),
        ("ambient", "ambient", 0.16, 0.10),
    ):
        record = tracks.get(category)
        if isinstance(record, dict) and record.get("status") == "completed":
            mix.append({
                "track_id": "background_music" if category == "music" else "environment_ambience",
                "role": role,
                "path": record["path"],
                "sha256": record["sha256"],
                "start_seconds": 0.0,
                "duration_seconds": record["duration_seconds"],
                "gain": gain,
                "ducking": {
                    "enabled": True, "target_gain": target,
                    "attack_ms": 180, "release_ms": 650,
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


def validate_flow_manifest(ordered):
    if not FLOW_MANIFEST.is_file():
        return None
    data = read_json(FLOW_MANIFEST, required=False, default=None)
    if not isinstance(data, dict):
        raise RuntimeError("Flow Music manifest exists but is invalid JSON.")
    if data.get("status") != "completed":
        return None

    source = safe_project_path(
        data.get("path") or data.get("audio_path"), "Flow Music master"
    )
    master_hash = sha256_file(source)
    if data.get("sha256") != master_hash:
        raise RuntimeError(
            "Completed Flow Music master SHA256 mismatch; local fallback is prohibited."
        )
    master_duration = ffprobe_audio(source)
    try:
        declared = float(data["duration_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Completed Flow Music manifest has no valid duration.") from exc
    if abs(declared - master_duration) > 0.30:
        raise RuntimeError("Completed Flow Music master duration mismatch.")

    segments = data.get("role_segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError(
            "Completed Flow Music has no role_segments; local fallback is prohibited."
        )

    expected = {row["scene_id"]: row for row in ordered}
    by_id = {}
    for item in segments:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid Flow Music role segment record.")
        sid = str(item.get("scene_id") or "").strip()
        if not sid or sid in by_id or sid not in expected:
            raise RuntimeError(f"Unknown or duplicate Flow Music segment: {sid}")
        by_id[sid] = item

    if set(by_id) != set(expected):
        missing = sorted(set(expected) - set(by_id))
        extra = sorted(set(by_id) - set(expected))
        raise RuntimeError(
            f"Flow Music role segments do not cover all narration. Missing={missing[:10]}, extra={extra[:10]}"
        )

    validated = []
    timeline, _ = build_timeline(ordered)
    for row in timeline:
        item = by_id[row["scene_id"]]
        if item.get("role") != row["role"] or int(item.get("part", 0)) != row["part"]:
            raise RuntimeError(f"Flow Music role/Part mismatch: {row['scene_id']}")
        if item.get("narration_sha256") != row["audio_sha256"]:
            raise RuntimeError(f"Flow Music narration hash mismatch: {row['scene_id']}")
        if item.get("source_scene_signature") != row.get("source_scene_signature", ""):
            raise RuntimeError(f"Flow Music visual Scene source signature mismatch: {row['scene_id']}")
        try:
            start = float(item["start_seconds"])
            duration = float(item["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid Flow Music timing: {row['scene_id']}") from exc
        if (
            not math.isfinite(start) or not math.isfinite(duration)
            or abs(start - row["start_seconds"]) > 0.15
            or abs(duration - row["duration_seconds"]) > 0.20
        ):
            raise RuntimeError(f"Flow Music segment timing mismatch: {row['scene_id']}")
        path = safe_project_path(item.get("path"), f"Flow Music segment {row['scene_id']}")
        checksum = sha256_file(path)
        if checksum != item.get("sha256"):
            raise RuntimeError(f"Flow Music segment SHA256 mismatch: {row['scene_id']}")
        actual = ffprobe_audio(path)
        if abs(actual - duration) > 0.30:
            raise RuntimeError(f"Flow Music segment media duration mismatch: {row['scene_id']}")
        validated.append({
            "scene_id": row["scene_id"], "role": row["role"],
            "part": row["part"], "path": path,
            "sha256": checksum, "start_seconds": start,
            "duration_seconds": duration,
        })
    return data, source, master_hash, validated


def build_role_music(flow_data, master_hash, segments, total_duration, source_sig, old_record):
    role_gains = {
        "hook": 0.82, "main": 0.70,
        "suspense": 0.95, "resolution": 0.86,
    }
    segment_signature = signature({
        "version": VERSION,
        "source_signature": source_sig,
        "master_sha256": master_hash,
        "segments": [{
            "scene_id": item["scene_id"], "role": item["role"],
            "sha256": item["sha256"], "start": item["start_seconds"],
            "duration": item["duration_seconds"],
        } for item in segments],
    })

    if MUSIC_PATH.is_file() and isinstance(old_record, dict):
        try:
            actual = validate_wav(MUSIC_PATH, total_duration)
            checksum = sha256_file(MUSIC_PATH)
            if (
                old_record.get("role_segment_signature") == segment_signature
                and old_record.get("sha256") == checksum
                and actual + 0.15 >= total_duration
            ):
                return {
                    "status": "completed",
                    "path": MUSIC_PATH.relative_to(ROOT).as_posix(),
                    "sha256": checksum,
                    "duration_seconds": round(actual, 6),
                    "generation": "google_flow_role_segments_reused",
                    "provider": flow_data.get("provider", "google_flow"),
                    "model": flow_data.get("model"),
                    "role_segment_count": len(segments),
                    "role_segment_signature": segment_signature,
                    "source_master_sha256": master_hash,
                    "external_api_calls": int(flow_data.get("external_api_calls", 0)),
                    "paid_services_used": bool(flow_data.get("paid_services_used", False)),
                    "role_gains": role_gains,
                    "reused": True,
                }
        except Exception:
            pass

    work_dir = DESIGN_DIR / "flow_role_music_build"
    work_dir.mkdir(parents=True, exist_ok=True)
    concat_file = work_dir / "segments.txt"
    converted = []
    try:
        for index, item in enumerate(segments, 1):
            target = work_dir / f"segment_{index:04d}.wav"
            temporary = target.with_name(target.name + ".tmp.wav")
            temporary.unlink(missing_ok=True)
            gain = role_gains[item["role"]]
            run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(item["path"]),
                "-t", f"{item['duration_seconds']:.4f}", "-vn",
                "-af", (
                    "aresample=22050,highpass=f=35,lowpass=f=10000,"
                    f"volume={gain:.4f},"
                    "loudnorm=I=-20:TP=-2:LRA=10,"
                    "alimiter=limit=0.92:level=disabled"
                ),
                "-ac", "1", "-ar", str(RATE),
                "-c:a", "pcm_s16le", str(temporary),
            ], timeout=300)
            actual = validate_wav(temporary, 0.05)
            if abs(actual - item["duration_seconds"]) > 0.30:
                raise RuntimeError(
                    "Converted Flow Music duration mismatch: " + item["scene_id"]
                )
            os.replace(temporary, target)
            converted.append(target)

        with concat_file.open("w", encoding="utf-8") as handle:
            for path in converted:
                resolved = str(path.resolve())
                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in Flow Music segment path.")
                handle.write("file '" + resolved.replace("'", "'\\''") + "'\n")

        temporary_master = MUSIC_PATH.with_name("background_music.role_building.wav")
        temporary_master.unlink(missing_ok=True)
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-af", (
                "aresample=22050,loudnorm=I=-20:TP=-2:LRA=10,"
                f"apad=pad_dur=2,atrim=duration={total_duration:.4f},"
                f"afade=t=out:st={max(0.0, total_duration - 4):.4f}:d=4"
            ),
            "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le",
            str(temporary_master),
        ], timeout=900)
        actual = validate_wav(temporary_master, total_duration)
        if abs(actual - total_duration) > 0.15:
            raise RuntimeError("Final Flow Music timeline duration mismatch.")
        os.replace(temporary_master, MUSIC_PATH)
        return {
            "status": "completed",
            "path": MUSIC_PATH.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(MUSIC_PATH),
            "duration_seconds": round(actual, 6),
            "generation": "google_flow_role_segments",
            "provider": flow_data.get("provider", "google_flow"),
            "model": flow_data.get("model"),
            "role_segment_count": len(segments),
            "role_segment_signature": segment_signature,
            "source_master_sha256": master_hash,
            "external_api_calls": int(flow_data.get("external_api_calls", 0)),
            "paid_services_used": bool(flow_data.get("paid_services_used", False)),
            "role_gains": role_gains,
            "reused": False,
        }
    finally:
        for path in converted:
            path.unlink(missing_ok=True)
        concat_file.unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass


def make_fallback_music(duration, old_record, source_sig):
    return make_local_track(
        MUSIC_PATH, duration, old_record, source_sig,
        "background_music", lambda seconds: music_samples(seconds),
    )


def build_cues(ordered, scene_records, tracks):
    cues = []
    for audio in ordered:
        scene = scene_records.get(audio["scene_id"])
        if scene is None:
            raise RuntimeError("scenes.json missing narration ID: " + audio["scene_id"])
        scene_role = str(scene.get("role", audio["role"])).strip().lower()
        scene_part = int(scene.get("part", audio["part"]))
        if scene_role != audio["role"] or scene_part != audio["part"]:
            raise RuntimeError("Scene/narration role or Part mismatch: " + audio["scene_id"])
        if audio["role"] == "main":
            scene_number = int(scene.get("scene", scene.get("local_scene", 0)))
            if scene_number != audio["scene"]:
                raise RuntimeError("Scene number mismatch: " + audio["scene_id"])
        elif scene.get("scene") not in (None, ""):
            raise RuntimeError("Extra role must use scene=None: " + audio["scene_id"])
        cue = create_cue(audio, scene, tracks)
        if cue is not None:
            cues.append(cue)
    validate_cues(cues, ordered)
    return cues


def validate_tracks_and_cues(tracks, cues, ordered, total_duration, enabled_tracks):
    if enabled_tracks["music"]:
        tracks["music"] = validate_track(tracks["music"], "music", total_duration)
    if enabled_tracks["ambient"]:
        ambient = tracks.get("ambient")
        if isinstance(ambient, dict) and ambient.get("status") == "completed":
            tracks["ambient"] = validate_track(ambient, "ambient", total_duration)
    if enabled_tracks["sfx"]:
        for kind in SFX_TYPES:
            key = f"sfx_{kind}"
            tracks[key] = validate_track(tracks[key], key, 0.1)
    tracks["sfx"] = tracks.get("sfx_impact") if enabled_tracks["sfx"] else None
    validate_cues(cues, ordered)


def build_validation_report(fmt, ordered, cues, total_duration, scenes_min, scenes_max, actual_scene_counts):
    cue_errors = []
    narration_map = {row["scene_id"]: row for row in ordered}
    seen = set()
    for cue in cues:
        sid = cue.get("scene_id")
        if sid in seen:
            cue_errors.append(f"Duplicate cue: {sid}")
            continue
        seen.add(sid)
        audio = narration_map.get(sid)
        if audio is None:
            cue_errors.append(f"Unknown narration ID: {sid}")
            continue
        try:
            start = float(cue["start_seconds"])
            duration = float(cue["duration_seconds"])
            gain = float(cue["gain"])
            if (
                not math.isfinite(start) or not math.isfinite(duration)
                or not math.isfinite(gain) or start < 0 or duration <= 0
                or gain < 0 or gain > 1
                or start + duration > audio["duration"] + 0.05
            ):
                cue_errors.append(f"Out-of-range cue: {sid}")
        except Exception:
            cue_errors.append(f"Invalid cue timing: {sid}")

    return {
        "status": "completed" if not cue_errors else "failed",
        "version": VERSION,
        "format": fmt,
        "timing_basis": "Measured TTS audio for Hook, Main, Suspense and Resolution",
        "total_visual_scene_records": len(ordered),
        "total_scene_timeline_seconds": round(total_duration, 4),
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "actual_scene_counts": actual_scene_counts,
        "cue_count": len(cues),
        "invalid_cues": len(cue_errors),
        "out_of_range_cues": len(cue_errors),
        "timing_errors": len(cue_errors),
        "failed_checks": len(cue_errors),
        "errors": cue_errors,
        "sfx": {
            "status": "completed" if not cue_errors else "failed",
            "cues": cues,
            "cue_count": len(cues),
        },
        "created_at": utc_now(),
    }


def save_track_checkpoint(source_sig, tracks, completed):
    write_json(CHECKPOINT_PATH, {
        "status": "in_progress",
        "version": VERSION,
        "source_signature": source_sig,
        "completed_tracks": sorted(completed),
        "tracks": tracks,
        "track_hashes": {
            key: value.get("sha256")
            for key, value in tracks.items()
            if isinstance(value, dict) and value.get("sha256")
        },
        "updated_at": utc_now(),
    })


def main():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")
    require_media_tools()
    config = load_and_validate(INPUT_FILE)
    fmt = str(get_format(config)).strip().lower()

    parts_min, parts_max = get_parts_range(config)
    selected_parts = get_selected_parts(config, required=True)
    if selected_parts is None:
        raise RuntimeError("Selected PARTS count is not available.")
    if not parts_min <= int(selected_parts) <= parts_max:
        raise RuntimeError("Selected PARTS count is outside configured range.")
    expected_parts = int(selected_parts)
    scenes_min, scenes_max = get_scenes_range(config)

    if fmt not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {fmt}")
    if fmt == "full" and expected_parts != 1:
        raise RuntimeError("FULL format must contain exactly one Part.")

    narration_data = read_json(NARRATION_FILE)
    if not isinstance(narration_data, dict):
        raise RuntimeError("narration.json root must be an object.")
    actual_scene_counts = narration_data.get("actual_scene_counts")
    if not isinstance(actual_scene_counts, list) or len(actual_scene_counts) != expected_parts:
        raise RuntimeError("Narration actual_scene_counts missing or invalid.")

    normalized_counts = []
    for part, value in enumerate(actual_scene_counts, 1):
        if isinstance(value, bool):
            raise RuntimeError(f"Part {part} actual Scene count is invalid.")
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Part {part} actual Scene count is invalid.") from exc
        if not scenes_min <= count <= scenes_max:
            raise RuntimeError(
                f"Part {part} has {count} Main Scenes; allowed range is {scenes_min}-{scenes_max}."
            )
        normalized_counts.append(count)

    if (
        int(narration_data.get("scenes_min", -1)) != scenes_min
        or int(narration_data.get("scenes_max", -1)) != scenes_max
    ):
        raise RuntimeError("Narration SCENES range does not match Input/topic.txt.")

    audio_records = load_audio_records()
    ordered = ordered_audio_records(audio_records)
    validate_record_structure(
        ordered, expected_parts, scenes_min, scenes_max, normalized_counts
    )
    actual_parts = sorted({row["part"] for row in ordered})
    scene_records = load_scene_records()

    missing_scene_ids = [
        row["scene_id"] for row in ordered
        if row["scene_id"] not in scene_records
    ]
    if missing_scene_ids:
        raise RuntimeError(
            "scenes.json is missing narration IDs: " + ", ".join(missing_scene_ids[:20])
        )

    enabled_tracks = {
        "music": enabled(
            config, "MUSIC_ENABLED", "MUSIC",
            "BACKGROUND_MUSIC", "SOUNDTRACK_ENABLED", default=True,
        ),
        "ambient": enabled(
            config, "AMBIENT_ENABLED", "AMBIENT",
            "AMBIENT_SOUND", "ENVIRONMENT_SOUND",
            "AMBIENCE_ENABLED", default=True,
        ),
        "sfx": enabled(
            config, "SFX_ENABLED", "SFX",
            "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED", default=True,
        ),
    }

    for directory in (
        DESIGN_DIR, SFX_DIR, OUT / "music", OUT / "sound",
        CHECKPOINT_PATH.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    source_payload = {
        "version": VERSION,
        "format": fmt,
        "parts": actual_parts,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": normalized_counts,
        "enabled": enabled_tracks,
        "audio_records": [{
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "duration": round(row["duration"], 4),
            "sha256": row["sha256"],
            "tts_fingerprint": row["tts_fingerprint"],
        } for row in ordered],
        "scene_manifest_sha256": sha256_file(SCENES_FILE),
    }
    source_sig = signature(source_payload)
    timeline, total_duration = build_timeline(ordered)
    old_tracks = load_previous_tracks()
    background_duration = max(30.0, total_duration + 3.0)

    tracks = {}
    completed_tracks = set()
    checkpoint = read_json(CHECKPOINT_PATH, required=False, default={})
    checkpoint_tracks = (
        checkpoint.get("tracks", {})
        if isinstance(checkpoint, dict)
        and checkpoint.get("source_signature") == source_sig
        and isinstance(checkpoint.get("tracks"), dict)
        else {}
    )
    # A checkpoint track is reused only after the same validators used for
    # final publication verify its bytes, hash and duration.
    for key, value in checkpoint_tracks.items():
        if isinstance(value, dict) and value.get("status") == "completed":
            try:
                validate_track(value, key, 0.1)
                tracks[key] = value
                completed_tracks.add(key)
            except Exception:
                log("CHECKPOINT TRACK INVALID; REGENERATING:", key)

    if enabled_tracks["music"] and "music" not in tracks:
        if FLOW_MANIFEST.is_file():
            flow_result = validate_flow_manifest(ordered)
            if flow_result is None:
                tracks["music"] = make_fallback_music(
                    background_duration, old_tracks.get("music"), source_sig
                )
            else:
                flow_data, _flow_master, flow_hash, role_segments = flow_result
                tracks["music"] = build_role_music(
                    flow_data, flow_hash, role_segments, total_duration,
                    source_sig, old_tracks.get("music"),
                )
        else:
            tracks["music"] = make_fallback_music(
                background_duration, old_tracks.get("music"), source_sig
            )
    elif not enabled_tracks["music"]:
        tracks["music"] = None
    if isinstance(tracks.get("music"), dict):
        completed_tracks.add("music")
        save_track_checkpoint(source_sig, tracks, completed_tracks)

    if enabled_tracks["ambient"] and "ambient" not in tracks:
        tracks["ambient"] = make_ambient(
            total_duration, scene_records, old_tracks.get("ambient"), source_sig
        )
    elif not enabled_tracks["ambient"]:
        tracks["ambient"] = None
    if isinstance(tracks.get("ambient"), dict):
        completed_tracks.add("ambient")
        save_track_checkpoint(source_sig, tracks, completed_tracks)

    if enabled_tracks["sfx"]:
        for kind in SFX_TYPES:
            key = f"sfx_{kind}"
            if key in tracks:
                continue
            tracks[key] = make_sfx(
                kind, SFX_DIR / f"{kind}.wav",
                old_tracks.get(key), source_sig,
            )
            completed_tracks.add(key)
            save_track_checkpoint(source_sig, tracks, completed_tracks)
    else:
        for kind in SFX_TYPES:
            tracks[f"sfx_{kind}"] = None

    cues = build_cues(ordered, scene_records, tracks) if enabled_tracks["sfx"] else []
    validate_tracks_and_cues(
        tracks, cues, ordered, total_duration, enabled_tracks
    )

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
    cue_map = {cue["scene_id"]: cue for cue in cues}
    part_summary = build_part_summary(ordered, cues)
    mix_records = build_mix_records(tracks, cues)

    music = tracks.get("music")
    flow_music_data = read_json(FLOW_MANIFEST, required=False, default={})
    role_compositions = (
        flow_music_data.get("role_compositions", {})
        if isinstance(flow_music_data, dict) else {}
    )
    music_providers = sorted({
        str(item.get("provider") or "lyria")
        for item in role_compositions.values()
        if isinstance(item, dict)
    })
    using_flow = (
        isinstance(music, dict)
        and str(music.get("generation", "")).startswith("google_flow")
    )
    external_api_calls = (
        int(music.get("external_api_calls", 0)) if using_flow else 0
    )
    paid_services_used = bool(
        using_flow and music.get("paid_services_used", False)
    )

    cue_manifest = {
        "status": "completed",
        "version": VERSION,
        "format": fmt,
        "dynamic_parts": True,
        "part_numbers": actual_parts,
        "parts": len(actual_parts),
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "actual_scene_counts": normalized_counts,
        "main_scene_counts": main_scene_counts,
        "role_counts": role_counts,
        "total_visual_scene_records": len(ordered),
        "cue_count": len(cues),
        "cues": cues,
        "scene_cues": cue_map,
        "part_cues": {
            key: value["sfx_cues"] for key, value in part_summary.items()
        },
        "timing_basis": "Measured TTS narration duration for Hook, Main, Suspense and Resolution.",
        "timeline": timeline,
        "source_signature": source_sig,
        "source_audio_manifest_sha256": sha256_file(AUDIO_MANIFEST),
        "sfx_policy": (
            "Story-event and scene-context keyword selection; "
            "no forced generic whoosh on every scene. Event assets are "
            "eight-second normalized WAVs; cues are clipped to measured "
            "narration boundaries and carry the source narration hash."
        ),
        "external_api_calls": 0,
        "paid_services_used": False,
        "created_at": utc_now(),
    }
    write_json(CUES_PATH, cue_manifest)

    validation_report = build_validation_report(
        fmt, ordered, cues, total_duration,
        scenes_min, scenes_max, normalized_counts,
    )
    if validation_report["status"] != "completed":
        raise RuntimeError(
            "SFX timing validation failed: "
            + json.dumps(validation_report["errors"], ensure_ascii=False)
        )
    write_json(SFX_VALIDATION_PATH, validation_report)

    mix_signature = signature({
        "source_signature": source_sig,
        "tracks": {
            key: value.get("sha256") if isinstance(value, dict) else None
            for key, value in tracks.items()
        },
        "mix_records": mix_records,
    })

    manifest = {
        "status": "completed",
        "version": VERSION,
        "generation": (
            "google_flow_role_music_with_event_sfx"
            if using_flow else "local_music_with_event_sfx"
        ),
        "external_api_calls": external_api_calls,
        "paid_services_used": paid_services_used,
        "music_providers": music_providers,
        "music_role_compositions": sorted(role_compositions),
        "sample_rate": RATE,
        "format": fmt,
        "dynamic_parts": True,
        "parts": len(actual_parts),
        "part_numbers": actual_parts,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": normalized_counts,
        "main_scene_counts": main_scene_counts,
        "role_counts": role_counts,
        "total_visual_scene_records": len(ordered),
        "total_scene_timeline_seconds": round(total_duration, 4),
        "background_duration_seconds": round(background_duration, 3),
        "sfx_duration_seconds": SFX_SECONDS,
        "sfx_normalization_target_lufs": -16.0,
        "sfx_true_peak_ceiling_dbtp": -1.5,
        "mix_gain_policy": {
            "music": 0.38, "ambient": 0.16,
            "sfx_gain_range": [min(SFX_GAINS.values()), max(SFX_GAINS.values())],
            "narration_ducking": True,
        },
        "enabled": enabled_tracks,
        "tracks": tracks,
        "timeline": timeline,
        "part_summary": part_summary,
        "sfx_cues": cues,
        "sfx_cue_map": cue_map,
        "mix_records": mix_records,
        "mix_signature": mix_signature,
        "flow_music_manifest": FLOW_MANIFEST.relative_to(ROOT).as_posix(),
        "music_provider": (
            "Google Flow/Lyria role segments" if using_flow
            else "local_synthesis_fallback" if enabled_tracks["music"]
            else "disabled"
        ),
        "music_role_segments_used": bool(
            isinstance(music, dict) and music.get("role_segment_count", 0) > 0
        ),
        "music_role_segment_count": (
            music.get("role_segment_count", 0) if isinstance(music, dict) else 0
        ),
        "ambient_status": (
            tracks.get("ambient", {}).get("status")
            if isinstance(tracks.get("ambient"), dict) else "disabled"
        ),
        "ambient_note": (
            tracks.get("ambient", {}).get("reason")
            if isinstance(tracks.get("ambient"), dict) else None
        ),
        "sfx_cue_manifest": CUES_PATH.relative_to(ROOT).as_posix(),
        "sfx_validation_manifest": SFX_VALIDATION_PATH.relative_to(ROOT).as_posix(),
        "sfx_cue_count": len(cues),
        "audio_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "narration_manifest": NARRATION_FILE.relative_to(ROOT).as_posix(),
        "scene_manifest": SCENES_FILE.relative_to(ROOT).as_posix(),
        "source_audio_manifest_sha256": sha256_file(AUDIO_MANIFEST),
        "source_narration_sha256": sha256_file(NARRATION_FILE),
        "source_scene_sha256": sha256_file(SCENES_FILE),
        "source_signature": source_sig,
        "sound_design_signature": mix_signature,
        "mixing_note": (
            "Role-specific Flow Music segments are independently validated "
            "and aligned to measured narration timeline. Hook, Main, Suspense "
            "and Resolution use distinct gains. Ambient and SFX remain "
            "separate timed tracks."
        ),
        "completed_at": utc_now(),
    }
    write_json(MANIFEST_PATH, manifest)

    saved_manifest = read_json(MANIFEST_PATH)
    saved_cues = read_json(CUES_PATH)
    saved_validation = read_json(SFX_VALIDATION_PATH)
    if (
        not isinstance(saved_manifest, dict)
        or saved_manifest.get("status") != "completed"
        or saved_manifest.get("sound_design_signature") != mix_signature
    ):
        raise RuntimeError("Sound design manifest read-back verification failed.")
    if (
        not isinstance(saved_cues, dict)
        or saved_cues.get("source_signature") != source_sig
        or len(saved_cues.get("cues", [])) != len(cues)
    ):
        raise RuntimeError("SFX cue manifest read-back verification failed.")
    if (
        not isinstance(saved_validation, dict)
        or saved_validation.get("status") != "completed"
        or saved_validation.get("format") != fmt
        or saved_validation.get("invalid_cues") != 0
        or saved_validation.get("out_of_range_cues") != 0
        or saved_validation.get("timing_errors") != 0
        or saved_validation.get("failed_checks") != 0
    ):
        raise RuntimeError("SFX timing validation read-back verification failed.")

    write_json(CHECKPOINT_PATH, {
        "status": "completed",
        "version": VERSION,
        "source_signature": source_sig,
        "sound_design_signature": mix_signature,
        "total_visual_scene_records": len(ordered),
        "completed_tracks": [
            key for key, value in tracks.items()
            if isinstance(value, dict) and value.get("status") == "completed"
        ],
        "cue_count": len(cues),
        "music_role_segment_count": (
            music.get("role_segment_count", 0) if isinstance(music, dict) else 0
        ),
        "updated_at": utc_now(),
    })

    log("SOUND DESIGN READY")
    log("FORMAT:", fmt)
    log("PARTS:", len(actual_parts))
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", normalized_counts)
    log("MAIN SCENES:", sum(main_scene_counts.values()))
    log("HOOK:", role_counts["hook"])
    log("SUSPENSE:", role_counts["suspense"])
    log("RESOLUTION:", role_counts["resolution"])
    log("NARRATION SECONDS:", round(total_duration, 2))
    log("FLOW ROLE MUSIC SEGMENTS:", manifest["music_role_segment_count"])
    log("SFX CUES:", len(cues))
    log("AMBIENT:", (tracks.get("ambient") or {}).get("status", "disabled"))
    log("EXTERNAL API CALLS:", manifest["external_api_calls"])
    log("PAID SERVICES USED:", manifest["paid_services_used"])
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