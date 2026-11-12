#!/usr/bin/env python3
"""Validate role-aware SFX timing against measured narration audio."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes_range,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
AUDIO_FILE = ROOT / "output" / "narration" / "audio_jobs.json"
SCENES_FILE = ROOT / "output" / "scenes" / "scenes.json"
DEFAULT_VISUAL_SCENE_SECONDS = 6.0
SOUND_MANIFEST = ROOT / "output" / "sound_design" / "sound_design_manifest.json"
FLOW_MANIFEST = ROOT / "output" / "music" / "flow_music_manifest.json"
CUES_FILE = ROOT / "output" / "sound_design" / "sfx_cues.json"
OUTPUT_FILE = ROOT / "output" / "sound_design" / "sfx_timing_validation.json"

VERSION = "sfx-validator-v17-allow-trimmed-cue-duration"
SOUND_DESIGN_VERSION = "photo-motion-sound-design-v34-all-role-measured-tts-timeline"
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 0.20
SFX_END_MARGIN = 0.08
MAX_GAIN = 1.0
MAX_CUES_PER_SCENE = 12
MAX_CUE_DURATION_SECONDS = 12.0
MAX_OVERLAP_SECONDS = 2.5
EPSILON = 0.001

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}
VALID_ROLES = set(ROLE_ORDER)

ROLE_ALIASES = {
    "opening": "hook",
    "intro": "hook",
    "part_hook": "hook",
    "cliffhanger": "suspense",
    "ending": "resolution",
    "final": "resolution",
    "story": "main",
    "main_scene": "main",
}

PATH_KEYS = ("file", "path", "audio", "output", "audio_path")
DURATION_KEYS = (
    "actual_audio_duration_seconds",
    "audio_duration",
    "duration",
)


def log(*values):
    print(*values, flush=True)


def read_json(path):
    path = Path(path)
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
        raise RuntimeError(f"Cannot read strict JSON {path}: {exc}") from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Invalid JSON root: {path}")
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


def rows_from(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def first_value(row, keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def positive_int(value, label):
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be a positive integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be a positive integer.") from exc
    if number < 1 or str(value).strip() != str(number):
        raise RuntimeError(f"{label} must be a canonical positive integer.")
    return number


def finite_number(value, label, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} is not numeric: {value!r}") from exc

    if not math.isfinite(number):
        raise RuntimeError(f"{label} must be finite.")
    if minimum is not None and number < minimum:
        raise RuntimeError(f"{label} must be >= {minimum}.")
    if maximum is not None and number > maximum:
        raise RuntimeError(f"{label} must be <= {maximum}.")
    return number


def validate_cue_file_duration(declared_duration, measured_file_duration, scene_id):
    """A cue may intentionally use only the opening portion of a longer SFX file."""
    declared = finite_number(
        declared_duration, f"SFX cue duration {scene_id}", minimum=0.01
    )
    measured = finite_number(
        measured_file_duration, f"SFX file duration {scene_id}", minimum=0.01
    )
    if declared > measured + DURATION_TOLERANCE:
        raise RuntimeError(
            f"SFX cue duration exceeds measured file duration: {scene_id}."
        )
    return declared, measured


def config_bool(config, keys, default=True):
    for key in keys:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "yes", "1", "on", "enabled"}:
            return True
        if normalized in {"false", "no", "0", "off", "disabled"}:
            return False
        raise RuntimeError(f"{key} must be true or false.")
    return default


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: audio file path is missing.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory.")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is empty or too small: {path}")
    return path


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def media_info(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required but not installed.")

    try:
        result = subprocess.run(
            [
                executable, "-v", "error",
                "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffprobe timed out: {path.name}") from exc

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid audio duration: {path.name}")

    has_audio = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in data.get("streams", [])
    )
    if not has_audio:
        raise RuntimeError(f"Audio stream missing: {path.name}")

    return {
        "duration": duration,
        "bytes": path.stat().st_size,
    }


def normalize_role(value):
    role = str(value or "main").strip().lower()
    role = ROLE_ALIASES.get(role, role)
    if role not in VALID_ROLES:
        raise RuntimeError(f"Unsupported narration role: {role!r}")
    return role


def narration_identity(row, index):
    if not isinstance(row, dict):
        raise RuntimeError(f"Invalid narration record at row {index}.")

    scene_id = str(row.get("scene_id") or "").strip()
    if not scene_id:
        raise RuntimeError(f"Narration row {index} has no scene_id.")

    part = positive_int(row.get("part"), f"Narration {scene_id} Part")
    role = normalize_role(row.get("role"))

    if role == "main":
        scene = positive_int(row.get("scene"), f"Narration {scene_id} scene")
    else:
        if row.get("scene") not in (None, ""):
            raise RuntimeError(
                f"Extra narration role {scene_id} must have scene=None."
            )
        scene = None

    return {
        "scene_id": scene_id,
        "part": part,
        "scene": scene,
        "role": role,
    }


def visual_scene_duration(row, scene_id):
    value = row.get("scene_duration") or row.get("duration")
    try:
        duration = float(str(value).strip().rstrip("sS"))
    except (TypeError, ValueError):
        duration = DEFAULT_VISUAL_SCENE_SECONDS
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid visual Scene duration: {scene_id}")
    return duration


def load_narration(configured_parts, scenes_min, scenes_max, mode):
    data = read_json(AUDIO_FILE)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("TTS audio manifest must be a completed JSON object.")
    manifest_format = str(data.get("format", "")).strip().lower()
    if manifest_format and manifest_format != mode:
        raise RuntimeError(f"TTS FORMAT mismatch: Input={mode}, manifest={manifest_format}.")
    jobs = rows_from(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        raise RuntimeError("TTS manifest contains no narration audio jobs.")

    narration_audio = {}
    for index, row in enumerate(jobs, 1):
        identity = narration_identity(row, index)
        scene_id = identity["scene_id"]
        if scene_id in narration_audio:
            raise RuntimeError(f"Duplicate TTS scene_id: {scene_id}")
        if str(row.get("status", "")).strip().lower() not in {"completed", "reused"}:
            raise RuntimeError(f"TTS job is incomplete: {scene_id}")
        path = safe_file(first_value(row, PATH_KEYS), f"TTS {scene_id}", MIN_AUDIO_BYTES)
        info = media_info(path)
        actual_hash = sha256_file(path)
        declared_hash = row.get("sha256") or row.get("audio_sha256") or row.get("audio_hash")
        if not isinstance(declared_hash, str) or declared_hash != actual_hash:
            raise RuntimeError(f"TTS SHA-256 missing or mismatched: {scene_id}")
        declared_duration = finite_number(first_value(row, DURATION_KEYS), f"TTS duration {scene_id}", minimum=0.01)
        if abs(declared_duration - info["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError(f"TTS duration mismatch: {scene_id}")
        if (row.get("duration_source") or row.get("audio_duration_source")) != "ffprobe_measured":
            raise RuntimeError(f"TTS duration is not ffprobe-measured: {scene_id}")
        fingerprint = row.get("tts_fingerprint") or row.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise RuntimeError(f"TTS fingerprint missing: {scene_id}")
        text = str(row.get("text") or row.get("narration") or "")
        if not text.strip():
            raise RuntimeError(f"Locked Main narration text is empty: {scene_id}")
        if identity["role"] != "main" and (
            row.get("voiceover_enabled") is not True
            or row.get("narration_source") != "ai_generated_role_narration"
        ):
            raise RuntimeError(f"{scene_id}: extra-role TTS metadata is invalid.")
        narration_audio[scene_id] = {
            **identity, "path": path, "relative_path": relative(path),
            "duration": info["duration"], "bytes": info["bytes"],
            "sha256": actual_hash, "text": text,
            "tts_fingerprint": fingerprint, "visual_only": False,
        }

    scenes_data = read_json(SCENES_FILE)
    if not isinstance(scenes_data, dict) or scenes_data.get("status") != "completed":
        raise RuntimeError("scenes.json must be completed.")
    scene_rows = rows_from(scenes_data, "scenes")
    if not scene_rows:
        raise RuntimeError("scenes.json contains no visual Scene records.")

    mapping, main_scenes, role_counts = {}, {}, {}
    for index, row in enumerate(scene_rows, 1):
        identity = narration_identity(row, index)
        scene_id = identity["scene_id"]
        if scene_id in mapping:
            raise RuntimeError(f"Duplicate visual scene_id: {scene_id}")
        if row.get("status") != "completed":
            raise RuntimeError(f"Visual Scene is incomplete: {scene_id}")
        source_signature = str(row.get("source_scene_signature") or "").strip()
        if len(source_signature) != 64 or any(ch not in "0123456789abcdef" for ch in source_signature):
            raise RuntimeError(f"Visual Scene source signature is invalid: {scene_id}")

        audio = narration_audio.pop(scene_id, None)
        if audio is None:
            raise RuntimeError(f"Scene has no matching TTS audio: {scene_id}")
        source_text = str(row.get("narration") or "")
        if " ".join(source_text.split()) != " ".join(audio["text"].split()):
            raise RuntimeError(f"Scene narration differs from TTS source: {scene_id}")
        if row.get("voiceover_enabled") is not True:
            raise RuntimeError(f"{scene_id}: role voiceover must be enabled.")
        if identity["role"] != "main" and row.get("narration_source") != "ai_generated_role_narration":
            raise RuntimeError(f"{scene_id}: extra-role narration source is invalid.")
        item = {**audio, "source_scene_signature": source_signature}
        if identity["role"] == "main":
            key = (identity["part"], identity["scene"])
            if key in main_scenes:
                raise RuntimeError(f"Duplicate Main scene mapping: {key}")
            main_scenes[key] = item
        mapping[scene_id] = item
        role_counts.setdefault(identity["part"], {role: 0 for role in ROLE_ORDER})
        role_counts[identity["part"]][identity["role"]] += 1

    if narration_audio:
        raise RuntimeError("TTS jobs do not match scenes.json: " + ", ".join(sorted(narration_audio)[:20]))

    discovered_parts = sorted({row["part"] for row in mapping.values()})
    if discovered_parts != list(range(1, configured_parts + 1)):
        raise RuntimeError(f"Visual Scene Part numbering mismatch: {discovered_parts}.")
    actual_counts, final_part = {}, discovered_parts[-1]
    for part in discovered_parts:
        scenes = sorted(scene for current_part, scene in main_scenes if current_part == part)
        if scenes != list(range(1, len(scenes) + 1)):
            raise RuntimeError(f"Part {part} Main Scene numbering has gaps: {scenes}")
        if not scenes_min <= len(scenes) <= scenes_max:
            raise RuntimeError(f"Part {part} Main Scene count is outside {scenes_min}-{scenes_max}.")
        actual_counts[str(part)] = len(scenes)
        counts = role_counts[part]
        if counts["hook"] != 1:
            raise RuntimeError(f"Part {part} must have exactly one visual Hook.")
        if counts["suspense"] != (1 if part < final_part else 0):
            raise RuntimeError(f"Part {part} has an invalid visual Suspense count.")
        if counts["resolution"] != (1 if part == final_part else 0):
            raise RuntimeError(f"Part {part} has an invalid visual Resolution count.")
        expected_records = len(scenes) + 1 + (1 if part < final_part else 0) + (1 if part == final_part else 0)
        if sum(counts.values()) != expected_records:
            raise RuntimeError(f"Part {part} visual Scene record count mismatch.")

    declared_jobs = data.get("total_jobs")
    main_total = sum(actual_counts.values())
    if declared_jobs is not None and int(declared_jobs) != main_total:
        raise RuntimeError("TTS manifest total_jobs does not match Main Scene count.")

    log("VISUAL PARTS:", len(discovered_parts))
    log("ACTUAL MAIN SCENES:", actual_counts)
    log("VISUAL ROLE COUNTS:", role_counts)
    log("TTS MAIN RECORDS:", main_total)
    log("TOTAL VISUAL SCENES:", len(mapping))
    return mapping, discovered_parts, actual_counts, role_counts


def normalize_scene_counts(value, parts, label):
    if isinstance(value, list):
        if len(value) != parts:
            raise RuntimeError(f"{label}: Part count mismatch.")
        source = {
            str(index): count
            for index, count in enumerate(value, 1)
        }
    elif isinstance(value, dict):
        source = value
    else:
        raise RuntimeError(f"{label}: actual_scene_counts is missing.")

    result = {}
    for part in range(1, parts + 1):
        raw = source.get(str(part), source.get(part))
        result[str(part)] = positive_int(
            raw, f"{label} Part {part} scene count"
        )
    return result


def validate_track(record, category, minimum_duration):
    if not isinstance(record, dict) or record.get("status") != "completed":
        raise RuntimeError(f"{category} track is not completed.")

    path = safe_file(
        first_value(record, PATH_KEYS),
        f"{category} track",
        MIN_AUDIO_BYTES,
    )
    checksum = sha256_file(path)
    declared_hash = record.get("sha256") or record.get("audio_sha256")

    if not isinstance(declared_hash, str) or declared_hash != checksum:
        raise RuntimeError(f"{category} track SHA-256 missing or mismatched.")

    info = media_info(path)
    if info["duration"] + 0.5 < minimum_duration:
        raise RuntimeError(
            f"{category} track is shorter than narration: "
            f"{info['duration']:.3f}s < {minimum_duration:.3f}s."
        )

    return {
        "path": path,
        "relative_path": relative(path),
        "sha256": checksum,
        "duration": info["duration"],
    }


def validate_sound_manifest(mode, parts, actual_counts, narration, audio_hash):
    manifest = read_json(SOUND_MANIFEST)
    if not isinstance(manifest, dict):
        raise RuntimeError("Sound-design manifest must be an object.")
    if manifest.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed.")

    if str(manifest.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Sound-design FORMAT does not match Input/topic.txt.")

    declared_parts = positive_int(
        manifest.get("parts", manifest.get("total_parts")),
        "Sound-design Parts",
    )
    if declared_parts != parts:
        raise RuntimeError("Sound-design Part count mismatch.")

    part_numbers = manifest.get("part_numbers")
    if isinstance(part_numbers, list):
        try:
            normalized_parts = [int(value) for value in part_numbers]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid sound-design part_numbers.") from exc
        if normalized_parts != list(range(1, parts + 1)):
            raise RuntimeError("Sound-design part_numbers are not continuous.")

    manifest_counts = normalize_scene_counts(
        manifest.get("actual_scene_counts"),
        parts,
        "Sound-design manifest",
    )
    if manifest_counts != actual_counts:
        raise RuntimeError(
            f"Sound-design scene counts mismatch: "
            f"narration={actual_counts}, sound={manifest_counts}."
        )

    main_counts = manifest.get("main_scene_counts")
    if isinstance(main_counts, dict):
        normalized_main = normalize_scene_counts(
            main_counts, parts, "Sound-design main_scene_counts"
        )
        if normalized_main != actual_counts:
            raise RuntimeError("Sound-design Main Scene counts mismatch.")

    total_records = manifest.get("total_visual_scene_records")
    if total_records is not None:
        if positive_int(total_records, "Sound-design visual Scene record count") != len(narration):
            raise RuntimeError("Sound-design visual Scene record count mismatch.")

    source_audio_hash = manifest.get("source_audio_manifest_sha256")
    if source_audio_hash != audio_hash:
        raise RuntimeError(
            "Sound design was generated from a different narration manifest."
        )

    if manifest.get("version") != SOUND_DESIGN_VERSION:
        raise RuntimeError("Sound-design manifest version is unsupported or stale.")

    enabled_tracks = manifest.get("enabled")
    if not isinstance(enabled_tracks, dict):
        raise RuntimeError("Sound-design enabled settings are missing.")

    for name in ("music", "ambient", "sfx"):
        if not isinstance(enabled_tracks.get(name), bool):
            raise RuntimeError(f"Sound-design enabled.{name} must be boolean.")

    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks must be an object.")

    total_duration = sum(row["duration"] for row in narration.values())

    validated_tracks = {}

    for category in ("music", "ambient"):
        record = tracks.get(category)

        if not enabled_tracks[category]:
            if record is not None:
                raise RuntimeError(
                    f"{category} is disabled but its manifest record is not null. "
                    "The renderer requires a null track when disabled."
                )
            validated_tracks[category] = None
            continue

        validated_tracks[category] = validate_track(
            record, category, total_duration
        )

    if not enabled_tracks["sfx"]:
        if tracks.get("sfx") is not None:
            raise RuntimeError(
                "SFX is disabled but the sound-design manifest still declares an SFX track."
            )
        for key, record in tracks.items():
            if key.startswith("sfx_") and record is not None:
                raise RuntimeError(
                    f"SFX is disabled but {key} is not null."
                )

    return manifest, validated_tracks


def validate_cues(parts, actual_counts, narration, sfx_enabled, sound_manifest):
    data = read_json(CUES_FILE)
    if not isinstance(data, dict):
        raise RuntimeError("SFX cue manifest must be a JSON object.")
    if data.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed.")

    mode = str(sound_manifest.get("format", "")).strip().lower()
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("SFX cue FORMAT does not match sound-design FORMAT.")

    declared_parts = positive_int(
        data.get("parts", data.get("actual_parts")),
        "SFX cue Parts",
    )
    if declared_parts != parts:
        raise RuntimeError("SFX cue Part count mismatch.")

    cue_counts = normalize_scene_counts(
        data.get("actual_scene_counts"),
        parts,
        "SFX cue manifest",
    )
    if cue_counts != actual_counts:
        raise RuntimeError("SFX cue scene counts do not match narration.")

    declared_total = data.get("total_visual_scene_records")
    if declared_total is not None:
        if positive_int(declared_total, "SFX visual Scene record count") != len(narration):
            raise RuntimeError("SFX cue visual Scene record count mismatch.")

    audio_hash = sha256_file(AUDIO_FILE)
    if data.get("source_audio_manifest_sha256") != audio_hash:
        raise RuntimeError(
            "SFX cues were generated from a different narration manifest."
        )

    sound_signature = sound_manifest.get("source_signature")
    cue_signature = data.get("source_signature")
    if (
        not isinstance(sound_signature, str)
        or not sound_signature
        or cue_signature != sound_signature
    ):
        raise RuntimeError(
            "SFX cue source_signature does not match sound-design manifest."
        )

    cues = rows_from(data, "cues", "items")
    declared_cue_count = data.get("cue_count")
    if declared_cue_count is not None:
        if isinstance(declared_cue_count, bool) or int(declared_cue_count) != len(cues):
            raise RuntimeError("SFX cue_count does not match the actual cue list.")

    if not sfx_enabled and cues:
        raise RuntimeError("SFX is disabled but the cue manifest contains cues.")

    tracks = sound_manifest.get("tracks", {})
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks are invalid.")

    seen = set()
    per_scene = {}
    reports = []
    warnings = []
    overlap_warnings = []

    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue at row {index}.")

        scene_id = str(cue.get("scene_id") or "").strip()
        if not scene_id or scene_id not in narration:
            raise RuntimeError(
                f"SFX cue {index} references an unknown narration scene_id: "
                f"{scene_id or '(missing)'}."
            )

        narration_row = narration[scene_id]
        part = positive_int(cue.get("part"), f"SFX cue {index} Part")
        role = normalize_role(cue.get("role"))

        if role == "main":
            scene = positive_int(cue.get("scene"), f"SFX cue {index} scene")
        else:
            if cue.get("scene") not in (None, ""):
                raise RuntimeError(
                    f"Extra-role SFX cue must have scene=None: {scene_id}."
                )
            scene = None

        if (
            part != narration_row["part"]
            or role != narration_row["role"]
            or scene != narration_row["scene"]
        ):
            raise RuntimeError(
                f"SFX cue Part/role/scene mapping mismatch: {scene_id}."
            )

        if not sfx_enabled:
            raise RuntimeError("Unexpected SFX cue while SFX is disabled.")

        expected_basis = (
            "visual_scene_duration" if narration_row.get("visual_only")
            else "measured_tts_audio_duration"
        )
        if cue.get("timing_basis") != expected_basis:
            raise RuntimeError(f"SFX cue timing basis mismatch: {scene_id}.")
        if cue.get("narration_sha256", "") != narration_row["sha256"]:
            raise RuntimeError(f"SFX cue source hash mismatch: {scene_id}.")
        if cue.get("narration_file") != narration_row["relative_path"]:
            raise RuntimeError(f"SFX cue narration path mismatch: {scene_id}.")
        duration_field = (
            "scene_duration_seconds" if narration_row.get("visual_only")
            else "narration_duration_seconds"
        )
        declared_source_duration = finite_number(
            cue.get(duration_field),
            f"SFX cue source duration {scene_id}",
            minimum=0.01,
        )
        if abs(declared_source_duration - narration_row["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError(f"SFX cue source duration mismatch: {scene_id}.")

        start = finite_number(
            cue.get("start_seconds", cue.get("time")),
            f"SFX cue {index} start",
            minimum=0,
        )
        gain = finite_number(
            cue.get("gain"),
            f"SFX cue {index} gain",
            minimum=0,
            maximum=MAX_GAIN,
        )
        declared_duration = finite_number(
            cue.get("duration_seconds"),
            f"SFX cue {index} duration",
            minimum=0.01,
        )

        narration_duration = narration_row["duration"]
        if start >= narration_duration:
            raise RuntimeError(
                f"SFX cue starts outside narration: {scene_id}."
            )
        if declared_duration > MAX_CUE_DURATION_SECONDS:
            raise RuntimeError(f"SFX cue duration exceeds {MAX_CUE_DURATION_SECONDS}s: {scene_id}.")
        if start + declared_duration > narration_duration + 0.05:
            raise RuntimeError(
                f"SFX cue declared duration exceeds narration: {scene_id}."
            )

        kind = str(cue.get("kind") or "").strip().lower()
        if not kind:
            raise RuntimeError(f"SFX cue {index} has no kind.")

        cue_key = (scene_id, round(start, 6), kind)
        if cue_key in seen:
            raise RuntimeError(
                f"Duplicate SFX cue: {scene_id} at {start:.3f}s ({kind})."
            )
        seen.add(cue_key)

        path = safe_file(
            first_value(cue, PATH_KEYS),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        actual_hash = sha256_file(path)

        declared_hash = cue.get("sha256") or cue.get("audio_sha256")
        if not isinstance(declared_hash, str) or declared_hash != actual_hash:
            raise RuntimeError(
                f"SFX cue {index} SHA-256 missing or mismatched."
            )

        track = tracks.get(f"sfx_{kind}")
        if not isinstance(track, dict) or track.get("status") != "completed":
            raise RuntimeError(
                f"SFX cue {scene_id} has no completed sound-design track: {kind}."
            )

        track_path = safe_file(
            first_value(track, PATH_KEYS),
            f"SFX track {kind}",
            MIN_AUDIO_BYTES,
        )
        track_hash = sha256_file(track_path)
        track_declared_hash = track.get("sha256") or track.get("audio_sha256")

        if track_hash != actual_hash or track_declared_hash != actual_hash:
            raise RuntimeError(
                f"SFX cue file does not match sound-design track {kind}."
            )

        validate_cue_file_duration(
            declared_duration, info["duration"], scene_id
        )
        if cue.get("source_track_sha256") not in (None, track_hash):
            raise RuntimeError(f"SFX cue source-track hash mismatch: {scene_id}.")
        if cue.get("size_bytes") is not None and positive_int(
            cue.get("size_bytes"), f"SFX cue {index} byte size"
        ) != path.stat().st_size:
            raise RuntimeError(f"SFX cue byte-size mismatch: {scene_id}.")

        latest_start = max(
            0.0,
            narration_duration - info["duration"] - SFX_END_MARGIN,
        )
        renderer_start = min(start, latest_start)
        renderer_duration = min(
            info["duration"],
            max(0.0, narration_duration - renderer_start),
        )
        renderer_end = renderer_start + renderer_duration
        truncated = max(0.0, info["duration"] - renderer_duration)

        if renderer_start + EPSILON < start:
            warnings.append({
                "scene_id": scene_id,
                "part": part,
                "scene": scene,
                "role": role,
                "kind": kind,
                "declared_start_seconds": round(start, 4),
                "renderer_start_seconds": round(renderer_start, 4),
                "warning": "Renderer shifts this cue earlier to preserve its audio tail.",
            })

        if truncated > 0.02:
            warnings.append({
                "scene_id": scene_id,
                "part": part,
                "scene": scene,
                "role": role,
                "kind": kind,
                "truncated_seconds": round(truncated, 4),
                "warning": "SFX file is longer than the available narration interval.",
            })

        report = {
            "scene_id": scene_id,
            "part": part,
            "scene": scene,
            "role": role,
            "kind": kind,
            "start_seconds": round(start, 4),
            "renderer_start_seconds": round(renderer_start, 4),
            "end_seconds": round(start + min(declared_duration, narration_duration - start), 4),
            "renderer_end_seconds": round(renderer_end, 4),
            "duration_seconds": round(info["duration"], 4),
            "declared_duration_seconds": round(declared_duration, 4),
            "file_duration_seconds": round(info["duration"], 4),
            "effective_duration_seconds": round(
                min(info["duration"], narration_duration - start), 4
            ),
            "renderer_effective_duration_seconds": round(renderer_duration, 4),
            "truncated_seconds": round(
                max(0.0, info["duration"] - min(info["duration"], narration_duration - start)),
                4,
            ),
            "renderer_truncated_seconds": round(truncated, 4),
            "scene_duration_seconds": round(narration_duration, 4),
            "narration_duration_seconds": (
                None if narration_row.get("visual_only") else round(narration_duration, 4)
            ),
            "narration_file": narration_row["relative_path"],
            "narration_sha256": narration_row["sha256"],
            "file": relative(path),
            "sha256": actual_hash,
            "gain": gain,
            "timing_basis": expected_basis,
            "timing_status": "fits_narration" if truncated <= 0.02 else "may_be_trimmed",
            "source_track_sha256": track_hash,
            "file_size_bytes": path.stat().st_size,
        }

        reports.append(report)
        per_scene.setdefault(scene_id, []).append(report)

        if len(per_scene[scene_id]) > MAX_CUES_PER_SCENE:
            raise RuntimeError(
                f"Too many SFX cues in narration {scene_id}: "
                f"{len(per_scene[scene_id])}."
            )

    if len(reports) != len(cues):
        raise RuntimeError("Not all SFX cues were validated.")

    reports.sort(
        key=lambda item: (
            item["part"],
            ROLE_ORDER[item["role"]],
            item["scene"] or 0,
            item["start_seconds"],
            item["kind"],
        )
    )

    scene_audit = []
    ordered_narration = sorted(
        narration.values(),
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
            row["scene"] or 0,
            row["scene_id"],
        ),
    )

    for narration_row in ordered_narration:
        scene_id = narration_row["scene_id"]
        scene_cues = sorted(
            per_scene.get(scene_id, []),
            key=lambda item: item["renderer_start_seconds"],
        )
        overlaps = []

        for previous, current in zip(scene_cues, scene_cues[1:]):
            overlap = (
                previous["renderer_end_seconds"]
                - current["renderer_start_seconds"]
            )
            if overlap > MAX_OVERLAP_SECONDS:
                raise RuntimeError(
                    f"Excessive overlapping SFX cues in {scene_id}: {overlap:.3f}s."
                )
            if overlap > EPSILON:
                warning = {
                    "scene_id": scene_id,
                    "part": narration_row["part"],
                    "scene": narration_row["scene"],
                    "role": narration_row["role"],
                    "previous_kind": previous["kind"],
                    "next_kind": current["kind"],
                    "overlap_seconds": round(overlap, 4),
                    "note": "Intentional layered effects; overlap is bounded by policy.",
                }
                overlaps.append(warning)
                overlap_warnings.append(warning)

        scene_audit.append({
            "scene_id": scene_id,
            "part": narration_row["part"],
            "scene": narration_row["scene"],
            "role": narration_row["role"],
            "narration_duration_seconds": round(
                narration_row["duration"], 4
            ),
            "narration_sha256": narration_row["sha256"],
            "sfx_cue_count": len(scene_cues),
            "overlap_count": len(overlaps),
            "overlaps": overlaps,
            "status": "has_sfx" if scene_cues else "no_sfx",
        })

    return {
        "status": "completed",
        "enabled": sfx_enabled,
        "cue_count": len(reports),
        "scenes_with_sfx": sum(bool(value) for value in per_scene.values()),
        "scenes_without_sfx": len(narration) - len(per_scene),
        "cues": reports,
        "scene_audit": scene_audit,
        "truncation_warnings": warnings,
        "overlap_warnings": overlap_warnings,
        "max_allowed_overlap_seconds": MAX_OVERLAP_SECONDS,
        "max_cue_duration_seconds": MAX_CUE_DURATION_SECONDS,
        "duration_source": "ffprobe_measured_all_role_tts_audio_plus_sfx_files",
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required but not installed.")

    for path in (INPUT_FILE, AUDIO_FILE, SOUND_MANIFEST, CUES_FILE):
        if not path.is_file():
            raise RuntimeError(f"Required file missing: {path}")

    config = load_and_validate(INPUT_FILE)
    mode = str(get_format(config)).strip().lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    configured_parts = positive_int(get_parts(config), "PARTS")
    scenes_min, scenes_max = get_scenes_range(config)

    if mode == "full" and configured_parts != 1:
        raise RuntimeError("FULL format must contain exactly one Part.")

    sfx_enabled = config_bool(
        config,
        ("SFX_ENABLED", "SFX", "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED"),
        default=True,
    )

    audio_hash = sha256_file(AUDIO_FILE)

    narration, part_numbers, actual_counts, role_counts = load_narration(
        configured_parts,
        scenes_min,
        scenes_max,
        mode,
    )

    music_segments_checked = 0
    music_providers = []
    if FLOW_MANIFEST.is_file():
        music_manifest = read_json(FLOW_MANIFEST)
        if music_manifest.get("status") == "completed":
            compositions = music_manifest.get("role_compositions", {})
            if not isinstance(compositions, dict):
                raise RuntimeError("Music role_compositions must be an object.")
            music_providers = sorted({
                str(item.get("provider") or "lyria")
                for item in compositions.values()
                if isinstance(item, dict)
            })
            segments = music_manifest.get("role_segments", [])
            if not isinstance(segments, list):
                raise RuntimeError("Music role_segments must be a list.")
            seen_music = set()
            for segment in segments:
                if not isinstance(segment, dict):
                    raise RuntimeError("Invalid music segment record.")
                scene_id = str(segment.get("scene_id") or "").strip()
                if not scene_id or scene_id in seen_music or scene_id not in narration:
                    raise RuntimeError(f"Music segment has duplicate or unknown scene ID: {scene_id}")
                seen_music.add(scene_id)
                narration_row = narration[scene_id]
                expected_duration = float(narration_row["duration"])
                actual_duration = float(segment.get("duration_seconds", -1))
                if abs(actual_duration - expected_duration) > DURATION_TOLERANCE:
                    raise RuntimeError(f"{scene_id}: music segment duration is not aligned to narration.")
                if segment.get("narration_sha256") != narration_row["sha256"]:
                    raise RuntimeError(f"{scene_id}: music segment is linked to different narration audio.")
                path_value = segment.get("path")
                if not path_value:
                    raise RuntimeError(f"{scene_id}: music segment path is missing.")
                music_path = Path(path_value)
                if not music_path.is_absolute():
                    music_path = ROOT / music_path
                if not music_path.is_file() or music_path.stat().st_size < MIN_AUDIO_BYTES:
                    raise RuntimeError(f"{scene_id}: music segment file is missing or too small.")
                music_segments_checked += 1
            if segments and seen_music != set(narration):
                raise RuntimeError("Music segments do not cover every narration scene exactly once.")

    sound_manifest, validated_tracks = validate_sound_manifest(
        mode,
        len(part_numbers),
        actual_counts,
        narration,
        audio_hash,
    )

    manifest_sfx_enabled = sound_manifest["enabled"]["sfx"]
    if manifest_sfx_enabled != sfx_enabled:
        raise RuntimeError(
            "SFX setting in Input/topic.txt does not match sound-design manifest."
        )

    sfx_report = validate_cues(
        len(part_numbers),
        actual_counts,
        narration,
        sfx_enabled,
        sound_manifest,
    )

    narration_duration = sum(
        row["duration"] for row in narration.values()
    )

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "configured_parts": configured_parts,
        "total_parts": len(part_numbers),
        "part_numbers": part_numbers,
        "dynamic_parts": mode == "short",
        "dynamic_scenes": True,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": actual_counts,
        "main_scene_counts": actual_counts,
        "role_counts": {
            str(part): counts
            for part, counts in role_counts.items()
        },
        "total_main_scenes": sum(actual_counts.values()),
        "total_visual_scene_records": len(narration),
        "visual_scenes_checked": len(narration),
        "scene_timeline_seconds": round(narration_duration, 4),
        "duration_source": "all_role_tts_audio_durations",
        "narration_manifest": relative(AUDIO_FILE),
        "narration_manifest_sha256": audio_hash,
        "sound_design_manifest": relative(SOUND_MANIFEST),
        "sound_design_manifest_sha256": sha256_file(SOUND_MANIFEST),
        "sfx_cue_manifest": relative(CUES_FILE),
        "sfx_cue_manifest_sha256": sha256_file(CUES_FILE),
        "validator_source_signature": __import__("hashlib").sha256((audio_hash + sha256_file(SOUND_MANIFEST) + sha256_file(CUES_FILE) + VERSION).encode("utf-8")).hexdigest(),
        "sound_tracks": {
            "music_enabled": sound_manifest["enabled"]["music"],
            "ambient_enabled": sound_manifest["enabled"]["ambient"],
            "sfx_enabled": sfx_enabled,
            "music": (
                {
                    "path": validated_tracks["music"]["relative_path"],
                    "sha256": validated_tracks["music"]["sha256"],
                    "duration_seconds": round(
                        validated_tracks["music"]["duration"], 4
                    ),
                }
                if validated_tracks["music"] else None
            ),
            "ambient": (
                {
                    "path": validated_tracks["ambient"]["relative_path"],
                    "sha256": validated_tracks["ambient"]["sha256"],
                    "duration_seconds": round(
                        validated_tracks["ambient"]["duration"], 4
                    ),
                }
                if validated_tracks["ambient"] else None
            ),
        },
        "sfx": sfx_report,
        "external_api_calls": 0,
        "paid_services_used": False,
        "created_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }

    write_json(OUTPUT_FILE, report)

    saved = read_json(OUTPUT_FILE)
    if (
        not isinstance(saved, dict)
        or saved.get("status") != "completed"
        or saved.get("format") != mode
        or saved.get("narration_manifest_sha256") != audio_hash
        or saved.get("sound_design_manifest_sha256") != sha256_file(SOUND_MANIFEST)
        or saved.get("sfx_cue_manifest_sha256") != sha256_file(CUES_FILE)
        or saved.get("validator_source_signature") != report["validator_source_signature"]
        or saved.get("sfx", {}).get("cue_count") != len(sfx_report["cues"])
        or saved.get("sfx", {}).get("duration_source") != "ffprobe_measured_all_role_tts_audio_plus_sfx_files"
    ):
        raise RuntimeError("SFX timing validation report read-back failed.")

    log("=" * 60)
    log("SFX TIMING VALIDATION: PASSED")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("PARTS:", len(part_numbers))
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENES:", actual_counts)
    log("VISUAL SCENE RECORDS:", len(narration))
    log("SCENE TIMELINE SECONDS:", round(narration_duration, 3))
    log("SFX ENABLED:", sfx_enabled)
    log("SFX CUES:", sfx_report["cue_count"])
    log("SCENES WITH SFX:", sfx_report["scenes_with_sfx"])
    log("SCENES WITHOUT SFX:", sfx_report["scenes_without_sfx"])
    log("TIMING WARNINGS:", len(sfx_report["truncation_warnings"]))
    log("OVERLAP WARNINGS:", len(sfx_report["overlap_warnings"]))
    log("REPORT:", relative(OUTPUT_FILE))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SFX TIMING VALIDATION: FAILED:", str(exc))
        sys.exit(1)