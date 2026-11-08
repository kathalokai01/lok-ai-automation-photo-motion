#!/usr/bin/env python3
"""Render verified, role-aware photo-motion clips with measured narration audio."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes_range,
    get_fps,
)

OUT = ROOT / "output"
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
SOUND_MANIFEST = OUT / "sound_design" / "sound_design_manifest.json"
SFX_VALIDATION = OUT / "sound_design" / "sfx_timing_validation.json"
STORY_FILE = OUT / "story" / "story.json"
TITLE_FILE = OUT / "story" / "final_title.txt"

RENDER_DIR = OUT / "scene_renders"
PART_DIR = OUT / "parts"
VIDEO_DIR = OUT / "videos"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
CHECKPOINT_FILE = OUT / "checkpoints" / "render_video_progress.json"
PRE_CAPTION_VIDEO = VIDEO_DIR / "pre_caption_render.mp4"

VERSION = "render-video-v65-all-role-voiceover-timeline"
AUDIO_RATE = 48000
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
FPS_TOLERANCE = 0.03
DURATION_TOLERANCE = 1.25
SFX_END_MARGIN = 0.08

NARRATION_GAIN = 1.0
MUSIC_GAIN = 0.50
AMBIENT_GAIN = 0.24
SFX_GAIN_MULTIPLIER = 1.90
MASTER_LIMIT = 0.95

ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}


def log(*values):
    print(*values, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_data(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, label, required=True, default=None):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"{label} missing: {path}")
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
        data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant, object_pairs_hook=unique_object)
    except Exception as exc:
        raise RuntimeError(f"Invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return data


def safe_path(value, label, must_exist=True):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path is missing")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(f"{label}: path escapes project directory") from exc
    if must_exist and not path.is_file():
        raise RuntimeError(f"{label}: file does not exist: {path}")
    return path


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def require_file(path, label, minimum=MIN_VIDEO_BYTES):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label} missing or too small: {path}")
    return path


def run(command, label, timeout=1800):
    try:
        result = subprocess.run(
            [str(value) for value in command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} timed out") from exc
    if result.stdout:
        log(result.stdout[-3000:])
    if result.returncode:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}"
        )
    return result


def parse_fps(value):
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        rate = Fraction(str(value))
        return float(rate) if rate > 0 else None
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path}: {result.stderr[-1500:]}"
        )
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except Exception as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc

    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    fps_value = (
        (video or {}).get("avg_frame_rate")
        or (video or {}).get("r_frame_rate")
    )
    return {
        "duration": duration,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "fps": parse_fps(fps_value),
    }


def validate_fps(info, expected, label):
    actual = info.get("fps")
    if actual is None or abs(actual - float(expected)) > FPS_TOLERANCE:
        raise RuntimeError(
            f"FPS mismatch at {label}: {actual}, expected {expected}"
        )


def positive_int(value, label):
    if isinstance(value, bool):
        raise RuntimeError(f"Invalid {label}: {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc
    if number < 1 or str(value).strip() != str(number):
        raise RuntimeError(f"{label} must be a canonical positive integer")
    return number


def finite_number(value, label, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(number):
        raise RuntimeError(f"{label} must be finite")
    if minimum is not None and number < minimum:
        raise RuntimeError(f"{label} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise RuntimeError(f"{label} cannot exceed {maximum}")
    return number


def config_bool(config, key, default=True):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "yes", "1", "on", "enabled"}:
        return True
    if value in {"false", "no", "0", "off", "disabled", ""}:
        return False
    raise RuntimeError(f"{key} must be true or false")


def validate_completed(data, label):
    if not isinstance(data, dict) or data.get("status") != "completed":
        status = data.get("status") if isinstance(data, dict) else None
        raise RuntimeError(f"{label} is not completed: {status}")


def normalize_role(row):
    value = str(
        row.get("role")
        or row.get("scene_type")
        or row.get("type")
        or "main"
    ).strip().lower()
    aliases = {
        "opening": "hook",
        "intro": "hook",
        "part_hook": "hook",
        "story": "main",
        "main_scene": "main",
        "cliffhanger": "suspense",
        "ending": "resolution",
        "final": "resolution",
    }
    value = aliases.get(value, value)
    if value not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported scene role: {value!r}")
    return value


def row_scene_id(row, label):
    value = str(row.get("scene_id") or row.get("id") or "").strip()
    if not value:
        raise RuntimeError(f"{label}: scene_id is missing")
    return value


def first_path(row, keys, label):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return safe_path(value, label)
    raise RuntimeError(f"{label}: no valid path field found")


def selected_part_numbers(config, manifest, count):
    for candidate in (
        manifest.get("part_numbers"),
        config.get("SELECTED_PART_NUMBERS"),
        config.get("PART_NUMBERS"),
    ):
        if isinstance(candidate, list) and candidate:
            numbers = [
                positive_int(item, "part number") for item in candidate
            ]
            if (
                len(numbers) != count
                or numbers != sorted(set(numbers))
                or numbers != list(range(numbers[0], numbers[0] + count))
            ):
                raise RuntimeError("Selected part number list is inconsistent")
            return numbers

    start = manifest.get("first_part", manifest.get("start_part", 1))
    start = positive_int(start, "first selected part")
    return list(range(start, start + count))


def story_output_slug():
    title = TITLE_FILE.read_text(encoding="utf-8-sig").strip() if TITLE_FILE.is_file() else ""
    if not title and STORY_FILE.is_file():
        story = read_json(STORY_FILE, "Story manifest")
        title = str(story.get("final_title") or story.get("title") or story.get("topic") or "").strip()
    title = title or "photo-motion-story"
    slug = re.sub(r"[^\w-]+", "-", title, flags=re.UNICODE).strip("-_")
    slug = re.sub(r"-{2,}", "-", slug)[:72].strip("-_")
    if not slug:
        raise RuntimeError("Could not derive a safe story-based output filename")
    return slug.lower()


def expected_role_sequence(parts):
    return {
        part: (
            ["hook", "main", "suspense"]
            if index < len(parts) - 1
            else ["hook", "main", "resolution"]
        )
        for index, part in enumerate(parts)
    }


def actual_counts_from_photo_rows(manifest, rows, parts, minimum, maximum):
    """Use actual Main rows; never assume the configured minimum is the count."""
    declared = manifest.get("actual_scene_counts")
    counts = {}

    for part in parts:
        main_numbers = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                row_part = int(row.get("part"))
            except (TypeError, ValueError):
                continue
            if row_part != part or normalize_role(row) != "main":
                continue
            local = positive_int(
                row.get(
                    "local_scene",
                    row.get("source_scene_number", row.get("scene")),
                ),
                f"Part {part} Main scene",
            )
            if local in main_numbers:
                raise RuntimeError(
                    f"Photo Motion has duplicate Main scene {part}/{local}"
                )
            main_numbers.add(local)

        if not main_numbers:
            raise RuntimeError(f"Photo Motion has no Main scenes for Part {part}")

        expected_numbers = set(range(1, max(main_numbers) + 1))
        if main_numbers != expected_numbers:
            raise RuntimeError(
                f"Part {part} Main scene numbers are not continuous: "
                f"{sorted(main_numbers)}"
            )

        count = len(main_numbers)
        if not minimum <= count <= maximum:
            raise RuntimeError(
                f"Part {part} has {count} Main scenes; allowed range is "
                f"{minimum}-{maximum}"
            )

        if isinstance(declared, dict):
            candidates = (str(part), f"part_{part:02d}", part)
            declared_value = next(
                (declared[key] for key in candidates if key in declared),
                None,
            )
            if declared_value is not None:
                if positive_int(
                    declared_value, f"Part {part} declared scene count"
                ) != count:
                    raise RuntimeError(
                        f"Part {part} declared/actual scene count mismatch"
                    )

        counts[part] = count

    if isinstance(declared, dict):
        for key in declared:
            try:
                declared_part = int(str(key).replace("part_", ""))
            except (TypeError, ValueError):
                continue
            if declared_part not in parts:
                raise RuntimeError(
                    f"Photo Motion has scene counts for unselected Part "
                    f"{declared_part}"
                )

    return counts


def load_photo_map(parts, scene_counts, mode, width, height, fps):
    data = read_json(PHOTO_MANIFEST, "Photo Motion manifest")
    validate_completed(data, "Photo Motion manifest")

    if str(data.get("format", "")).lower() != mode:
        raise RuntimeError("Photo Motion FORMAT mismatch")
    if (
        int(data.get("width", 0)) != width
        or int(data.get("height", 0)) != height
        or int(data.get("fps", 0)) != fps
    ):
        raise RuntimeError("Photo Motion dimensions/FPS mismatch")

    rows = data.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Photo Motion manifest has no scenes")
    if int(data.get("completed_count", -1)) != len(rows):
        raise RuntimeError("Photo Motion completed_count mismatch")

    by_id = {}
    main_map = {}
    role_counts = {role: {} for role in ROLE_ORDER}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Photo Motion row {index} is incomplete")

        sid = row_scene_id(row, f"Photo Motion row {index}")
        if sid in by_id:
            raise RuntimeError(f"Duplicate Photo Motion scene_id: {sid}")

        part = positive_int(row.get("part"), "Photo Motion part")
        if part not in parts:
            raise RuntimeError(f"{sid}: part outside selected range")

        role = normalize_role(row)
        global_scene = positive_int(
            row.get("global_scene", row.get("global_scene_number", index)),
            f"{sid} global scene",
        )

        if role == "main":
            local_scene = positive_int(
                row.get(
                    "local_scene",
                    row.get("source_scene_number", row.get("scene")),
                ),
                f"{sid} local scene",
            )
            if local_scene > scene_counts[part]:
                raise RuntimeError(
                    f"{sid}: scene exceeds Part {part} actual scene count"
                )
            expected_id = f"part_{part:02d}_scene_{local_scene:02d}"
        else:
            local_scene = 0
            expected_id = f"part_{part:02d}_{role}"

        if sid != expected_id:
            raise RuntimeError(f"{sid}: expected canonical ID {expected_id}")

        video = first_path(
            row,
            ("video", "output", "path"),
            f"Photo Motion {sid}",
        )
        require_file(video, f"Photo Motion {sid}")
        checksum = sha256_file(video)

        declared = row.get("video_sha256") or row.get("sha256")
        if not declared or declared != checksum:
            raise RuntimeError(f"Photo Motion SHA-256 mismatch: {sid}")

        info = probe(video)
        if not info["has_video"]:
            raise RuntimeError(f"Photo Motion {sid} has no video stream")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Photo Motion {sid} dimensions mismatch")
        validate_fps(info, fps, sid)

        voiceover_enabled = row.get("voiceover_enabled")
        narration_source = row.get("narration_source")
        source_text_hash = str(row.get("source_text_sha256") or "")
        if role == "main":
            if voiceover_enabled is not True or narration_source != "locked_part_text":
                raise RuntimeError(f"{sid}: Main voiceover metadata is invalid.")
            if len(source_text_hash) != 64 or any(ch not in "0123456789abcdef" for ch in source_text_hash):
                raise RuntimeError(f"{sid}: Main source text fingerprint is invalid.")
        else:
            if voiceover_enabled is not True or narration_source != "ai_generated_role_narration":
                raise RuntimeError(f"{sid}: narrated extra-role metadata is invalid.")
            if not row.get("audio_path") or not row.get("audio_sha256"):
                raise RuntimeError(f"{sid}: narrated extra-role TTS audio is missing.")

        by_id[sid] = {
            "scene_id": sid,
            "part": part,
            "role": role,
            "local_scene": local_scene,
            "global_scene": global_scene,
            "video": video,
            "video_sha256": checksum,
            "duration": info["duration"],
            "audio_sha256": str(row.get("audio_sha256") or ""),
            "voiceover_enabled": voiceover_enabled,
            "narration_source": narration_source,
            "source_text_sha256": source_text_hash,
            "width": width,
            "height": height,
            "fps": fps,
        }
        role_counts[role][part] = role_counts[role].get(part, 0) + 1

        if role == "main":
            key = (part, local_scene)
            if key in main_map:
                raise RuntimeError(f"Duplicate Main scene: {key}")
            main_map[key] = by_id[sid]

    expected_main = {
        (part, scene)
        for part in parts
        for scene in range(1, scene_counts[part] + 1)
    }
    if set(main_map) != expected_main:
        raise RuntimeError(
            "Photo Motion Main scene mapping mismatch; "
            f"missing={sorted(expected_main - set(main_map))}; "
            f"extra={sorted(set(main_map) - expected_main)}"
        )

    for part, required in expected_role_sequence(parts).items():
        for role in required:
            expected_count = scene_counts[part] if role == "main" else 1
            actual_count = role_counts[role].get(part, 0)
            if actual_count != expected_count:
                raise RuntimeError(
                    f"Part {part} requires {expected_count} {role} clips; "
                    f"found {actual_count}"
                )
        for role in ROLE_ORDER:
            if role not in required and role_counts[role].get(part, 0):
                raise RuntimeError(
                    f"Part {part} has an invalid {role} clip"
                )

    ordered = sorted(
        by_id.values(),
        key=lambda item: (
            item["part"],
            ROLE_ORDER[item["role"]],
            item["local_scene"],
            item["global_scene"],
        ),
    )
    global_numbers = [item["global_scene"] for item in ordered]
    if global_numbers != list(range(1, len(ordered) + 1)):
        raise RuntimeError(
            "Photo Motion global scene sequence is not continuous"
        )

    return by_id, ordered, data


def load_audio_map(parts, scene_counts):
    data = read_json(AUDIO_MANIFEST, "Narration audio manifest")
    validate_completed(data, "Narration audio manifest")
    rows = data.get("jobs") or data.get("audio_jobs") or data.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Narration manifest has no Main Scene audio jobs.")

    mapping = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration job {index}")
        sid = row_scene_id(row, f"Narration job {index}")
        if sid in mapping:
            raise RuntimeError(f"Duplicate narration scene_id: {sid}")
        if str(row.get("status", "")).lower() not in {"completed", "reused"}:
            raise RuntimeError(f"Narration job is incomplete: {sid}")
        part = positive_int(row.get("part"), f"{sid} Part")
        if part not in parts:
            raise RuntimeError(f"{sid}: narration Part outside selection")
        role = normalize_role(row)
        if role == "main":
            local_scene = positive_int(row.get("scene", row.get("local_scene")), f"{sid} scene number")
            if local_scene > scene_counts[part]:
                raise RuntimeError(f"{sid}: narration scene exceeds actual scene count")
            expected_id = f"part_{part:02d}_scene_{local_scene:02d}"
        else:
            local_scene = 0
            expected_id = f"part_{part:02d}_{role}"
            if row.get("voiceover_enabled") is not True or row.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{sid}: extra-role narration metadata is invalid.")
        if sid != expected_id:
            raise RuntimeError(f"Narration {sid}: expected canonical ID {expected_id}")

        audio = first_path(row, ("output", "audio_path", "audio_file", "path", "file", "audio"), f"Narration {sid}")
        require_file(audio, f"Narration {sid}", MIN_AUDIO_BYTES)
        info = probe(audio)
        if not info["has_audio"]:
            raise RuntimeError(f"Narration {sid} has no audio stream")
        checksum = sha256_file(audio)
        declared = row.get("audio_sha256") or row.get("sha256")
        if not declared or declared != checksum:
            raise RuntimeError(f"Narration SHA-256 mismatch: {sid}")
        declared_duration = row.get("actual_audio_duration_seconds", row.get("audio_duration", row.get("duration")))
        if declared_duration is None:
            raise RuntimeError(f"Narration duration is missing: {sid}")
        declared_duration = finite_number(declared_duration, f"{sid} measured duration", minimum=0.01)
        if abs(declared_duration - info["duration"]) > 0.15:
            raise RuntimeError(f"Narration measured duration mismatch: {sid}")
        if (row.get("duration_source") or row.get("audio_duration_source")) != "ffprobe_measured":
            raise RuntimeError(f"Narration duration is not ffprobe-measured: {sid}")
        text_value = str(row.get("text") or row.get("narration") or "")
        text_hash = hashlib.sha256(text_value.strip().encode("utf-8")).hexdigest()
        if not text_value.strip() or row.get("text_sha256") != text_hash:
            raise RuntimeError(f"Locked narration text hash mismatch: {sid}")

        mapping[sid] = {
            "scene_id": sid, "part": part, "role": role,
            "local_scene": local_scene, "audio": audio,
            "audio_sha256": checksum, "text_sha256": text_hash,
            "text": text_value, "duration": info["duration"],
            "provider": row.get("provider"), "model": row.get("model"),
        }

    expected_ids = {
        f"part_{part:02d}_scene_{scene:02d}"
        for part in parts
        for scene in range(1, scene_counts[part] + 1)
    } | {
        f"part_{part:02d}_{role}"
        for part in parts
        for role in (("hook", "suspense") if part < parts[-1] else ("hook", "resolution"))
    }
    if set(mapping) != expected_ids:
        raise RuntimeError(
            "Main narration mapping mismatch; "
            f"missing={sorted(expected_ids - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected_ids)}"
        )
    return mapping, data


def validate_photo_audio_pairing(photo_by_id, audio_by_id):
    if set(audio_by_id) != set(photo_by_id):
        raise RuntimeError(
            "TTS jobs must match every Photo Motion role; "
            f"missing_audio={sorted(set(photo_by_id)-set(audio_by_id))}; "
            f"unexpected_audio={sorted(set(audio_by_id)-set(photo_by_id))}"
        )
    for sid, photo in photo_by_id.items():
        audio = audio_by_id[sid]
        if photo["part"] != audio["part"] or photo["role"] != audio["role"] or photo["local_scene"] != audio["local_scene"]:
            raise RuntimeError(f"{sid}: Photo Motion/TTS role mapping mismatch.")
        if photo.get("source_text_sha256") != audio["text_sha256"]:
            raise RuntimeError(f"{sid}: Photo Motion/TTS text fingerprint mismatch.")
        if photo.get("audio_sha256") != audio["audio_sha256"]:
            raise RuntimeError(f"{sid}: Photo Motion/TTS audio fingerprint mismatch.")
        if photo.get("voiceover_enabled") is not True:
            raise RuntimeError(f"{sid}: role voiceover must be enabled.")


def load_sound_assets(parts, photo_by_id, audio_by_id, mode, audio_manifest_hash):
    manifest = read_json(SOUND_MANIFEST, "Sound design manifest")
    validate_completed(manifest, "Sound design manifest")

    validation = read_json(SFX_VALIDATION, "SFX timing validation")
    validate_completed(validation, "SFX timing validation")

    if str(manifest.get("format", "")).lower() != mode:
        raise RuntimeError("Sound design FORMAT does not match Input")
    if str(validation.get("format", "")).lower() != mode:
        raise RuntimeError("SFX validation FORMAT does not match Input")
    if validation.get("duration_source") != "all_role_tts_audio_durations":
        raise RuntimeError("SFX validation lacks Main TTS/visual Scene duration provenance")
    if validation.get("narration_manifest_sha256") != audio_manifest_hash:
        raise RuntimeError("SFX validation belongs to a different narration manifest")
    if validation.get("sound_design_manifest_sha256") != sha256_file(SOUND_MANIFEST):
        raise RuntimeError("SFX validation belongs to a different sound-design manifest")
    cue_manifest_hash = validation.get("sfx_cue_manifest_sha256")
    if not isinstance(cue_manifest_hash, str) or len(cue_manifest_hash) != 64:
        raise RuntimeError("SFX validation cue-manifest fingerprint is missing")
    expected_signature = hashlib.sha256((audio_manifest_hash + sha256_file(SOUND_MANIFEST) + cue_manifest_hash + "sfx-validator-v13-visual-role-scene-timing").encode("utf-8")).hexdigest()
    if validation.get("validator_source_signature") != expected_signature:
        raise RuntimeError("SFX validation source signature mismatch")

    manifest_parts = manifest.get("part_numbers")
    if isinstance(manifest_parts, list):
        if [int(value) for value in manifest_parts] != parts:
            raise RuntimeError("Sound design selected Parts mismatch")
    elif int(manifest.get("parts", manifest.get("total_parts", -1))) != len(parts):
        raise RuntimeError("Sound design Part count mismatch")

    source_audio_hash = manifest.get("source_audio_manifest_sha256")
    if source_audio_hash and source_audio_hash != audio_manifest_hash:
        raise RuntimeError(
            "Sound design was generated from a different narration manifest"
        )

    tracks = manifest.get("tracks", {})
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound design tracks must be an object")

    assets = {
        "music": None,
        "ambient": None,
        "sfx": {},
        "warnings": [],
    }
    track_hashes = {}

    for category in ("music", "ambient"):
        record = tracks.get(category)
        if record is None:
            continue
        if not isinstance(record, dict) or record.get("status") != "completed":
            raise RuntimeError(f"{category} track is not completed")

        path = first_path(
            record,
            ("path", "audio_path", "output"),
            f"{category} track",
        )
        require_file(path, f"{category} track", MIN_AUDIO_BYTES)
        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(f"{category} track has no audio stream")

        checksum = sha256_file(path)
        declared = record.get("sha256") or record.get("audio_sha256")
        if not isinstance(declared, str) or declared != checksum:
            raise RuntimeError(f"{category} track SHA-256 missing or mismatched")

        assets[category] = {
            "path": path,
            "sha256": checksum,
            "duration": info["duration"],
        }
        track_hashes[category] = checksum

    sfx_data = validation.get("sfx", {})
    cues = sfx_data.get("cues", []) if isinstance(sfx_data, dict) else []
    if not isinstance(cues, list):
        raise RuntimeError("SFX validation cues must be a list")

    seen_cues = set()
    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        sid = str(cue.get("scene_id") or "").strip()
        if not sid or sid not in photo_by_id:
            raise RuntimeError(f"SFX cue {index} references an unknown visual Scene.")
        scene_row = photo_by_id[sid]
        audio_row = audio_by_id.get(sid)
        cue_part = positive_int(cue.get("part"), f"SFX cue {index} Part")
        if cue_part != scene_row["part"]:
            raise RuntimeError(f"SFX cue Part mismatch: {sid}")
        cue_role = normalize_role(cue)
        if cue_role != scene_row["role"]:
            raise RuntimeError(f"SFX cue role mismatch: {sid}")
        cue_scene = cue.get("scene")
        if cue_role == "main":
            if audio_row is None or positive_int(cue_scene, f"SFX cue {index} scene") != scene_row["local_scene"]:
                raise RuntimeError(f"SFX cue Main scene mapping mismatch: {sid}")
        elif cue_scene not in (None, "", 0, "0"):
            raise RuntimeError(f"Extra-role SFX cue must have scene=None: {sid}")

        start_value = cue.get(
            "start_seconds",
            cue.get("actual_start_seconds", cue.get("time", cue.get("start"))),
        )
        start = finite_number(
            start_value, f"SFX cue {index} start", minimum=0
        )
        gain = finite_number(
            cue.get("gain", 0.5),
            f"SFX cue {index} gain",
            minimum=0,
            maximum=1,
        )

        kind = str(cue.get("kind") or cue.get("type") or "effect").strip()
        cue_key = (sid, round(start, 6), kind)
        if cue_key in seen_cues:
            raise RuntimeError(
                f"Duplicate SFX cue: {sid} at {start:.3f}s"
            )
        seen_cues.add(cue_key)

        path = first_path(
            cue, ("file", "path", "audio_path", "output"),
            f"SFX cue {index}",
        )
        require_file(path, f"SFX cue {index}", MIN_AUDIO_BYTES)
        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(f"SFX cue {index} has no audio stream")

        checksum = sha256_file(path)
        declared = cue.get("sha256") or cue.get("audio_sha256")
        if not isinstance(declared, str) or declared != checksum:
            raise RuntimeError(f"SFX cue SHA-256 missing or mismatched: {index}")
        if cue_role == "main":
            if cue.get("timing_basis") != "measured_tts_audio_duration":
                raise RuntimeError(f"SFX cue Main timing basis mismatch: {sid}")
            if cue.get("narration_sha256") != audio_row["audio_sha256"]:
                raise RuntimeError(f"SFX cue Main narration hash mismatch: {sid}")
            if cue.get("narration_file") != relative(audio_row["audio"]):
                raise RuntimeError(f"SFX cue Main narration path mismatch: {sid}")
            declared_duration = finite_number(cue.get("narration_duration_seconds"), f"SFX cue narration duration {sid}", minimum=0.01)
            if abs(declared_duration - audio_row["duration"]) > 0.15:
                raise RuntimeError(f"SFX cue measured narration duration mismatch: {sid}")
            duration = audio_row["duration"]
        else:
            if cue.get("timing_basis") != "measured_tts_audio_duration":
                raise RuntimeError(f"SFX cue narrated-role timing basis mismatch: {sid}")
            if cue.get("narration_sha256") != audio_row["audio_sha256"]:
                raise RuntimeError(f"SFX cue narrated-role audio hash mismatch: {sid}")
            if cue.get("narration_file") != relative(audio_row["audio"]):
                raise RuntimeError(f"SFX cue narrated-role audio path mismatch: {sid}")
            declared_duration = finite_number(cue.get("narration_duration_seconds"), f"SFX narrated-role duration {sid}", minimum=0.01)
            if abs(declared_duration - audio_row["duration"]) > 0.15:
                raise RuntimeError(f"SFX narrated-role duration mismatch: {sid}")
            duration = audio_row["duration"]
        if start >= duration:
            raise RuntimeError(f"SFX cue starts outside narration: {sid}")

        declared_duration = finite_number(cue.get("duration_seconds"), f"SFX cue {index} declared duration", minimum=0.01)
        if abs(declared_duration - info["duration"]) > 0.15:
            raise RuntimeError(f"SFX cue declared/file duration mismatch: {sid}")
        actual_start = start
        usable_duration = info["duration"]
        if actual_start + usable_duration > duration + 0.02:
            raise RuntimeError(f"SFX cue would be truncated by narration boundary: {sid}")
        if actual_start + usable_duration > duration - SFX_END_MARGIN + 0.02:
            assets["warnings"].append({"scene_id": sid, "warning": "SFX tail reaches narration end margin"})

        assets["sfx"].setdefault(sid, []).append({
            "path": path,
            "sha256": checksum,
            "start": actual_start,
            "duration": usable_duration,
            "gain": min(1.0, gain * SFX_GAIN_MULTIPLIER),
            "kind": kind,
        })
        track_hashes[f"sfx_{index}"] = checksum

    for sid in assets["sfx"]:
        assets["sfx"][sid].sort(key=lambda item: (item["start"], item["kind"]))
        for previous_cue, current_cue in zip(assets["sfx"][sid], assets["sfx"][sid][1:]):
            overlap = previous_cue["start"] + previous_cue["duration"] - current_cue["start"]
            if overlap > 2.5 + 0.001:
                raise RuntimeError(f"Excessive SFX overlap in {sid}: {overlap:.3f}s")

    assets["fingerprint"] = digest_data({
        "sound_manifest_sha256": sha256_file(SOUND_MANIFEST),
        "sfx_validation_sha256": sha256_file(SFX_VALIDATION),
        "track_hashes": track_hashes,
        "gains": {
            "narration": NARRATION_GAIN,
            "music": MUSIC_GAIN,
            "ambient": AMBIENT_GAIN,
            "sfx": SFX_GAIN_MULTIPLIER,
            "limiter": MASTER_LIMIT,
        },
    })
    return assets, manifest


def scene_fingerprint(photo, audio, sound_assets, mode, width, height, fps):
    return digest_data({
        "version": VERSION,
        "format": mode,
        "width": width,
        "height": height,
        "fps": fps,
        "scene_id": photo["scene_id"],
        "role": photo["role"],
        "part": photo["part"],
        "photo_sha256": photo["video_sha256"],
        "audio_sha256": audio["audio_sha256"] if audio else "",
        "audio_duration": round(audio["duration"], 5) if audio else round(photo["duration"], 5),
        "source_text_sha256": photo.get("source_text_sha256", ""),
        "sound_fingerprint": sound_assets["fingerprint"],
    })


def previous_manifest(mode):
    data = read_json(
        RENDER_MANIFEST,
        "previous render manifest",
        required=False,
        default={},
    )
    if not isinstance(data, dict) or str(data.get("format", "")).lower() != mode:
        return {}

    rows = (
        data.get("all_completed_scenes")
        or data.get("completed_scenes")
        or data.get("scenes")
        or []
    )
    result = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("scene_id"):
                result[row["scene_id"]] = row
    return result


def reusable_scene(output, old_row, signature, width, height, fps, duration):
    if not isinstance(old_row, dict):
        return None
    if old_row.get("status") != "completed":
        return None
    if old_row.get("fingerprint") != signature:
        return None
    source = output
    if not source.is_file():
        old_value = old_row.get("video") or old_row.get("output") or old_row.get("path")
        if isinstance(old_value, str) and old_value.strip():
            try:
                source = safe_path(old_value, "Previous rendered scene")
            except Exception:
                return None
        else:
            return None
    if source.stat().st_size < MIN_VIDEO_BYTES:
        return None

    try:
        checksum = sha256_file(source)
        if old_row.get("sha256") != checksum:
            return None
        info = probe(output)
        if not info["has_video"] or not info["has_audio"]:
            return None
        if (info["width"], info["height"]) != (width, height):
            return None
        validate_fps(info, fps, output.name)
        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            return None
        if source != output:
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(output.stem + ".reuse.tmp.mp4")
            shutil.copy2(source, temporary)
            if sha256_file(temporary) != checksum:
                temporary.unlink(missing_ok=True)
                return None
            os.replace(temporary, output)
        return info, checksum
    except Exception:
        return None


def build_audio_filter(photo, audio, sound_assets, timeline_offset, width, height, fps):
    duration = audio["duration"] if audio else photo["duration"]
    inputs = [
        "-stream_loop", "-1", "-i", str(photo["video"]),
    ]
    filters = [
        (
            f"[0:v]scale={width}:{height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps},"
            "format=yuv420p[v]"
        )
    ]
    mix_labels = []
    input_index = 1

    if audio:
        inputs.extend(["-i", str(audio["audio"])])
        filters.append(
            f"[{input_index}:a]aresample={AUDIO_RATE},"
            f"volume={NARRATION_GAIN:.3f},"
            "asetpts=PTS-STARTPTS[narr]"
        )
        mix_labels.append("[narr]")
        input_index += 1

    bed_labels = []
    for category, gain in (("music", MUSIC_GAIN), ("ambient", AMBIENT_GAIN)):
        track = sound_assets.get(category)
        if not track:
            continue
        offset = timeline_offset % track["duration"]
        inputs.extend([
            "-stream_loop", "-1", "-ss", f"{offset:.4f}",
            "-i", str(track["path"]),
        ])
        label = f"bed{input_index}"
        filters.append(
            f"[{input_index}:a]aresample={AUDIO_RATE},"
            f"atrim=duration={duration:.4f},"
            "asetpts=PTS-STARTPTS,"
            f"volume={gain:.3f},"
            "afade=t=in:st=0:d=0.25,"
            f"afade=t=out:st={max(0.0, duration - 0.4):.4f}:d=0.4"
            f"[{label}]"
        )
        bed_labels.append(f"[{label}]")
        input_index += 1

    if bed_labels:
        filters.append(
            "".join(bed_labels)
            + f"amix=inputs={len(bed_labels)}:duration=longest:normalize=0,"
            "volume=1.0[bed]"
        )
        if audio:
            filters.append(
                "[bed][narr]sidechaincompress="
                "threshold=0.025:ratio=5:attack=180:release=700:"
                "makeup=1[ducked]"
            )
            mix_labels.append("[ducked]")
        else:
            mix_labels.append("[bed]")

    sfx_labels = []
    for cue_index, cue in enumerate(sound_assets["sfx"].get(photo["scene_id"], [])):
        usable = min(cue["duration"], duration - cue["start"])
        if usable <= 0:
            continue
        inputs.extend(["-i", str(cue["path"])])
        label = f"sfx{cue_index}"
        delay = max(0, int(round(cue["start"] * 1000)))
        fade = min(0.4, max(0.05, usable * 0.2))
        filters.append(
            f"[{input_index}:a]aresample={AUDIO_RATE},"
            f"atrim=duration={usable:.4f},"
            "asetpts=PTS-STARTPTS,"
            f"volume={cue['gain']:.3f},"
            f"afade=t=in:st=0:d={min(0.12, usable / 3):.4f},"
            f"afade=t=out:st={max(0.0, usable - fade):.4f}:d={fade:.4f},"
            f"adelay={delay}|{delay}[{label}]"
        )
        sfx_labels.append(f"[{label}]")
        input_index += 1

    mix_labels.extend(sfx_labels)
    inputs.extend([
        "-f", "lavfi", "-t", f"{duration:.4f}",
        "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo",
    ])
    filters.append(
        f"[{input_index}:a]atrim=duration={duration:.4f},"
        f"aresample={AUDIO_RATE}[silence]"
    )
    mix_labels.append("[silence]")

    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:duration=first:"
        "dropout_transition=0:normalize=0,"
        f"alimiter=limit={MASTER_LIMIT:.3f}:level=disabled,"
        f"aresample={AUDIO_RATE}[aout]"
    )
    return inputs, filters


def render_scene(
    photo, audio, sound_assets, output, signature, old_row,
    width, height, fps, timeline_offset, resume, skip_completed,
):
    output.parent.mkdir(parents=True, exist_ok=True)

    if resume and skip_completed:
        expected_duration = audio["duration"] if audio else photo["duration"]
        cached = reusable_scene(
            output, old_row, signature, width, height, fps, expected_duration
        )
        if cached:
            log("REUSED:", photo["scene_id"])
            return cached[0], cached[1], True

    temporary = output.with_name(output.stem + ".rendering.mp4")
    temporary.unlink(missing_ok=True)

    duration = audio["duration"] if audio else photo["duration"]
    inputs, filters = build_audio_filter(
        photo, audio, sound_assets, timeline_offset,
        width, height, fps,
    )
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
        "-map", "[aout]",
        "-t", f"{duration:.4f}",
        "-r", str(fps),
        "-fps_mode", "cfr",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart",
        str(temporary),
    ]

    try:
        run(command, f"Render {photo['scene_id']}")
        require_file(temporary, "Rendered scene")
        info = probe(temporary)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(
                f"{photo['scene_id']}: rendered output lacks audio/video"
            )
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"{photo['scene_id']}: rendered dimensions mismatch"
            )
        validate_fps(info, fps, photo["scene_id"])
        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"{photo['scene_id']}: rendered duration mismatch")

        os.replace(temporary, output)
        return probe(output), sha256_file(output), False
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def concat_videos(paths, destination, width, height, fps, minimum=10000):
    if not paths:
        raise RuntimeError(f"No input clips for {destination.name}")

    for path in paths:
        require_file(path, f"Concat input {path.name}")
        info = probe(path)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Concat input lacks audio/video: {path}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Concat dimensions mismatch: {path}")
        validate_fps(info, fps, path.name)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".joining.mp4")
    list_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="render_concat_",
            encoding="utf-8",
            delete=False,
        ) as stream:
            list_file = Path(stream.name)
            for path in paths:
                resolved = str(Path(path).resolve())
                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in concat path")
                stream.write(
                    "file '" + resolved.replace("'", "'\\''") + "'\n"
                )

        temporary.unlink(missing_ok=True)
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-r", str(fps),
                "-fps_mode", "cfr",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "19",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", str(AUDIO_RATE),
                "-movflags", "+faststart",
                str(temporary),
            ],
            f"Join {destination.name}",
            timeout=2400,
        )
        require_file(temporary, "Joined video", minimum)
        info = probe(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Joined video lacks audio/video: {destination}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Joined video dimensions mismatch: {destination}")
        validate_fps(info, fps, destination.name)
        os.replace(temporary, destination)
        return probe(destination)
    finally:
        if list_file:
            list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def make_render_row(
    photo, audio, output, info, checksum, signature, reused,
    width, height, fps, timeline_offset, sound_assets,
):
    return {
        "scene_id": photo["scene_id"],
        "role": photo["role"],
        "scene_type": photo["role"],
        "part": photo["part"],
        "scene": photo["local_scene"] if photo["role"] == "main" else None,
        "local_scene": photo["local_scene"] if photo["role"] == "main" else None,
        "global_scene": photo["global_scene"],
        "photo_motion_global_scene": photo["global_scene"],
        "status": "completed",
        "source_photo_motion_video": relative(photo["video"]),
        "source_audio": relative(audio["audio"]) if audio else None,
        "source_photo_sha256": photo["video_sha256"],
        "source_audio_sha256": audio["audio_sha256"] if audio else "",
        "voiceover_enabled": bool(audio),
        "narration_source": (
            "locked_part_text" if photo["role"] == "main" else "ai_generated_role_narration"
        ),
        "source_text_sha256": photo.get("source_text_sha256", ""),
        "source_rendered_video": relative(output),
        "video": relative(output),
        "output": relative(output),
        "path": relative(output),
        "duration": round(info["duration"], 5),
        "width": width,
        "height": height,
        "fps": fps,
        "fingerprint": signature,
        "sha256": checksum,
        "captions_burned": False,
        "cache_reused": reused,
        "audio_mix": {
            "narration": bool(audio),
            "music": bool(sound_assets["music"]),
            "ambient": bool(sound_assets["ambient"]),
            "sfx_count": len(sound_assets["sfx"].get(photo["scene_id"], [])),
            "global_timeline_offset": round(timeline_offset, 5),
        },
    }


def verify_rendered_scene(row, width, height, fps):
    path = safe_path(
        row.get("video") or row.get("output") or row.get("path"),
        f"Rendered scene {row.get('scene_id')}",
    )
    require_file(path, "Rendered scene")
    checksum = sha256_file(path)
    if checksum != row.get("sha256"):
        raise RuntimeError(
            f"Rendered scene SHA-256 mismatch: {row.get('scene_id')}"
        )

    info = probe(path)
    if not info["has_video"] or not info["has_audio"]:
        raise RuntimeError(
            f"Rendered scene lacks audio/video: {row.get('scene_id')}"
        )
    if (info["width"], info["height"]) != (width, height):
        raise RuntimeError(
            f"Rendered scene dimensions mismatch: {row.get('scene_id')}"
        )
    validate_fps(info, fps, str(row.get("scene_id")))
    if abs(info["duration"] - float(row.get("duration", 0))) > DURATION_TOLERANCE:
        raise RuntimeError(
            f"Rendered scene duration mismatch: {row.get('scene_id')}"
        )
    return info


def build_manifest(
    mode, fps, width, height, parts, part_numbers, scene_counts,
    completed, part_rows, photo_hash, audio_hash, sound_fingerprint,
    status, error=None,
):
    all_rows = [
        completed[item["scene_id"]]
        for item in CURRENT_ORDERED_PHOTOS
        if item["scene_id"] in completed
    ]
    main_rows = [row for row in all_rows if row["role"] == "main"]
    extra_rows = [row for row in all_rows if row["role"] != "main"]

    result = {
        "status": status,
        "version": VERSION,
        "format": mode,
        "fps": fps,
        "width": width,
        "height": height,
        "total_parts": parts,
        "parts_count": parts,
        "part_numbers": part_numbers,
        "first_part": part_numbers[0],
        "last_part": part_numbers[-1],
        "actual_scene_counts": {
            str(part): scene_counts[part] for part in part_numbers
        },
        "scenes_per_part": None,
        "expected_scene_count": len(all_rows),
        "completed_count": len(all_rows),
        "main_scene_count": len(main_rows),
        "all_completed_scene_count": len(all_rows),
        "extra_scene_count": len(extra_rows),
        "captions_burned": False,
        "source_photo_manifest": relative(PHOTO_MANIFEST),
        "source_audio_manifest": relative(AUDIO_MANIFEST),
        "source_sound_manifest": relative(SOUND_MANIFEST),
        "source_sfx_validation": relative(SFX_VALIDATION),
        "source_photo_manifest_sha256": photo_hash,
        "source_audio_manifest_sha256": audio_hash,
        "source_sound_fingerprint": sound_fingerprint,
        "completed_scenes": main_rows,
        "scenes": main_rows,
        "extra_scenes": extra_rows,
        "all_completed_scenes": all_rows,
        "parts": sorted(part_rows, key=lambda row: int(row["part"])),
        "part_outputs_only": False,
        "pre_caption_video": relative(PRE_CAPTION_VIDEO),
        "pre_caption_video_sha256": (
            sha256_file(PRE_CAPTION_VIDEO)
            if PRE_CAPTION_VIDEO.is_file()
            else None
        ),
        "audio_mix": {
            "narration_gain": NARRATION_GAIN,
            "music_gain": MUSIC_GAIN,
            "ambient_gain": AMBIENT_GAIN,
            "sfx_gain_multiplier": SFX_GAIN_MULTIPLIER,
            "master_limiter": MASTER_LIMIT,
            "sample_rate": AUDIO_RATE,
            "music_ambient_global_timeline": True,
            "music_ambient_looped_if_needed": True,
            "narration_sidechain_ducking": True,
            "sfx_tail_protection": True,
        },
    }
    if error:
        result["last_error"] = str(error)[:2000]
    return result


def build_progress(completed, ordered, status, error=None):
    rows = []
    for item in ordered:
        record = completed.get(item["scene_id"])
        rows.append({
            "scene_id": item["scene_id"],
            "part": item["part"],
            "role": item["role"],
            "global_scene": item["global_scene"],
            "status": (
                record.get("status", "pending")
                if isinstance(record, dict)
                else "pending"
            ),
            "video": (
                record.get("video")
                if isinstance(record, dict)
                else None
            ),
        })
    result = {
        "version": VERSION,
        "stage": "render_video",
        "status": status,
        "total_scenes": len(ordered),
        "completed_scenes": sum(
            row["status"] == "completed" for row in rows
        ),
        "jobs": rows,
    }
    if error:
        result["last_error"] = str(error)[:2000]
    return result


CURRENT_ORDERED_PHOTOS = []


def main():
    global CURRENT_ORDERED_PHOTOS

    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    output_slug = story_output_slug()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    parts_count = positive_int(get_parts(config), "PARTS")
    scene_minimum, scene_maximum = get_scenes_range(config)
    fps = positive_int(get_fps(config), "FPS")
    if fps > 60:
        raise RuntimeError("FPS cannot exceed 60")

    width, height = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    photo_data = read_json(PHOTO_MANIFEST, "Photo Motion manifest")
    part_numbers = selected_part_numbers(config, photo_data, parts_count)
    photo_rows = photo_data.get("scenes")
    if not isinstance(photo_rows, list) or not photo_rows:
        raise RuntimeError("Photo Motion manifest has no scene rows")

    scene_counts = actual_counts_from_photo_rows(
        photo_data,
        photo_rows,
        part_numbers,
        scene_minimum,
        scene_maximum,
    )

    photo_by_id, ordered_photos, photo_data = load_photo_map(
        part_numbers,
        scene_counts,
        mode,
        width,
        height,
        fps,
    )
    CURRENT_ORDERED_PHOTOS = ordered_photos

    audio_by_id, audio_data = load_audio_map(part_numbers, scene_counts)
    validate_photo_audio_pairing(photo_by_id, audio_by_id)

    audio_manifest_hash = sha256_file(AUDIO_MANIFEST)
    sound_assets, sound_data = load_sound_assets(
        part_numbers,
        photo_by_id,
        audio_by_id,
        mode,
        audio_manifest_hash,
    )
    if str(sound_data.get("format", "")).lower() != mode:
        raise RuntimeError("Sound design FORMAT does not match Input")

    photo_hash = sha256_file(PHOTO_MANIFEST)
    resume = config_bool(config, "RESUME_ENABLED", True)
    skip_completed = config_bool(config, "SKIP_COMPLETED_SCENES", True)
    previous = previous_manifest(mode)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    timeline_offsets = {}
    total_duration = 0.0
    for photo in ordered_photos:
        sid = photo["scene_id"]
        timeline_offsets[sid] = total_duration
        total_duration += photo["duration"]

    completed = {}
    part_rows = []
    new_renders = 0
    reused_renders = 0

    def save_checkpoint(status="in_progress", error=None):
        write_json(
            RENDER_MANIFEST,
            build_manifest(
                mode,
                fps,
                width,
                height,
                parts_count,
                part_numbers,
                scene_counts,
                completed,
                part_rows,
                photo_hash,
                audio_manifest_hash,
                sound_assets["fingerprint"],
                status,
                error,
            ),
        )
        write_json(
            CHECKPOINT_FILE,
            build_progress(completed, ordered_photos, status, error),
        )

    log("RENDER VERSION:", VERSION)
    log("FORMAT:", mode)
    log("PARTS:", part_numbers)
    log("SCENE RANGE:", f"{scene_minimum}-{scene_maximum}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("RESOLUTION:", f"{width}x{height}", "FPS:", fps)
    log("MAIN SCENES:", sum(scene_counts.values()))
    log("ALL ROLE CLIPS:", len(ordered_photos))
    log("VISUAL SCENE TIMELINE SECONDS:", round(total_duration, 3))
    log("MUSIC:", bool(sound_assets["music"]))
    log("AMBIENT:", bool(sound_assets["ambient"]))
    log("SFX CUES:", sum(len(items) for items in sound_assets["sfx"].values()))

    save_checkpoint()

    try:
        for part in part_numbers:
            part_photos = [
                item for item in ordered_photos if item["part"] == part
            ]
            if not part_photos:
                raise RuntimeError(f"Part {part} has no render clips")

            scene_paths = []
            for item in part_photos:
                sid = item["scene_id"]
                audio = audio_by_id.get(sid)
                if audio is None:
                    raise RuntimeError(f"{sid}: TTS audio is missing for role {item['role']}.")
                signature = scene_fingerprint(
                    item,
                    audio,
                    sound_assets,
                    mode,
                    width,
                    height,
                    fps,
                )

                global_number = item["global_scene"]
                output = (
                    RENDER_DIR
                    / f"part_{part:02d}"
                    / f"{output_slug}_part_{part:02d}_scene_{global_number:03d}.mp4"
                )

                info, checksum, reused = render_scene(
                    item,
                    audio,
                    sound_assets,
                    output,
                    signature,
                    previous.get(sid),
                    width,
                    height,
                    fps,
                    timeline_offsets[sid],
                    resume,
                    skip_completed,
                )
                new_renders += int(not reused)
                reused_renders += int(reused)

                row = make_render_row(
                    item,
                    audio,
                    output,
                    info,
                    checksum,
                    signature,
                    reused,
                    width,
                    height,
                    fps,
                    timeline_offsets[sid],
                    sound_assets,
                )
                completed[sid] = row
                scene_paths.append(output)
                save_checkpoint()

                log(
                    f"CHECKPOINT {len(completed)}/{len(ordered_photos)} "
                    f"part={part} role={item['role']} scene_id={sid}"
                )

            part_output = PART_DIR / f"part_{part:02d}.mp4"
            part_info = concat_videos(
                scene_paths,
                part_output,
                width,
                height,
                fps,
            )

            part_rows = [
                row for row in part_rows if int(row["part"]) != part
            ]
            part_rows.append({
                "part": part,
                "status": "completed",
                "scene_count": scene_counts[part],
                "total_clip_count": len(part_photos),
                "roles": [item["role"] for item in part_photos],
                "path": relative(part_output),
                "output": relative(part_output),
                "duration": round(part_info["duration"], 5),
                "sha256": sha256_file(part_output),
                "fps": fps,
                "width": width,
                "height": height,
            })
            part_rows.sort(key=lambda row: int(row["part"]))
            save_checkpoint()
            log("PART COMPLETE:", relative(part_output))

        if len(completed) != len(ordered_photos):
            raise RuntimeError(
                f"Rendered {len(completed)}/{len(ordered_photos)} clips"
            )

        for item in ordered_photos:
            row = completed.get(item["scene_id"])
            if not isinstance(row, dict) or row.get("status") != "completed":
                raise RuntimeError(
                    f"Render manifest row missing: {item['scene_id']}"
                )
            verify_rendered_scene(row, width, height, fps)

        if len(part_rows) != parts_count:
            raise RuntimeError(
                f"Expected {parts_count} Part videos, found {len(part_rows)}"
            )

        for index, part in enumerate(part_numbers):
            row = part_rows[index]
            if int(row["part"]) != part:
                raise RuntimeError("Part output order mismatch")
            path = safe_path(row["path"], f"Part {part} video")
            if sha256_file(path) != row["sha256"]:
                raise RuntimeError(f"Part {part} SHA-256 mismatch")
            info = probe(path)
            if not info["has_video"] or not info["has_audio"]:
                raise RuntimeError(f"Part {part} lacks video/audio")
            if (info["width"], info["height"]) != (width, height):
                raise RuntimeError(f"Part {part} dimensions mismatch")
            validate_fps(info, fps, f"Part {part}")

        main_rows = [
            completed[item["scene_id"]]
            for item in ordered_photos
            if item["role"] == "main"
        ]
        extra_rows = [
            completed[item["scene_id"]]
            for item in ordered_photos
            if item["role"] != "main"
        ]

        if len(main_rows) != sum(scene_counts.values()):
            raise RuntimeError("Main scene render count mismatch")
        if len(extra_rows) != parts_count * 2:
            raise RuntimeError("Hook/Suspense/Resolution count mismatch")

        for part in part_numbers:
            roles = [
                item["role"] for item in ordered_photos
                if item["part"] == part
            ]
            if roles.count("hook") != 1:
                raise RuntimeError(f"Part {part}: Hook count mismatch")
            if part == part_numbers[-1]:
                if roles.count("resolution") != 1 or "suspense" in roles:
                    raise RuntimeError(
                        f"Final Part {part}: Resolution/Suspense mismatch"
                    )
            else:
                if roles.count("suspense") != 1 or "resolution" in roles:
                    raise RuntimeError(
                        f"Part {part}: Suspense/Resolution mismatch"
                    )

        ordered_part_paths = [
            safe_path(
                next(
                    row["path"] for row in part_rows
                    if int(row["part"]) == part
                ),
                f"Part {part} output",
            )
            for part in part_numbers
        ]

        concat_videos(
            ordered_part_paths,
            PRE_CAPTION_VIDEO,
            width,
            height,
            fps,
            minimum=10000,
        )

        pre_caption_info = probe(PRE_CAPTION_VIDEO)
        if not pre_caption_info["has_video"] or not pre_caption_info["has_audio"]:
            raise RuntimeError("Pre-caption video lacks audio/video")
        if (pre_caption_info["width"], pre_caption_info["height"]) != (width, height):
            raise RuntimeError("Pre-caption video dimensions mismatch")
        validate_fps(pre_caption_info, fps, "pre-caption video")

        final_manifest = build_manifest(
            mode,
            fps,
            width,
            height,
            parts_count,
            part_numbers,
            scene_counts,
            completed,
            part_rows,
            photo_hash,
            audio_manifest_hash,
            sound_assets["fingerprint"],
            "completed",
        )
        final_manifest.update({
            "story_title": output_slug,
            "output_basename": output_slug,
            "story_named_scene_files": True,
            "part_filename_contract": "part_{part:02d}.mp4",
            "format_dimensions": f"{width}x{height}",
            "timeline_duration_basis": "sum_of_ffprobe_measured_narration_audio_durations",
            "captions_burned": False,
            "rendered_new": new_renders,
            "rendered_reused": reused_renders,
            "main_scene_count": len(main_rows),
            "extra_scene_count": len(extra_rows),
            "hook_count": sum(row["role"] == "hook" for row in extra_rows),
            "suspense_count": sum(row["role"] == "suspense" for row in extra_rows),
            "resolution_count": sum(row["role"] == "resolution" for row in extra_rows),
            "narration_duration_seconds": round(total_duration, 5),
            "pre_caption_video_duration": round(pre_caption_info["duration"], 5),
            "pre_caption_video_sha256": sha256_file(PRE_CAPTION_VIDEO),
            "audio_mix_summary": {
                "music_included": bool(sound_assets["music"]),
                "ambient_included": bool(sound_assets["ambient"]),
                "sfx_cues_included": sum(
                    len(items) for items in sound_assets["sfx"].values()
                ),
                "sfx_adjustments": len(sound_assets["warnings"]),
                "narration_gain": NARRATION_GAIN,
                "music_gain": MUSIC_GAIN,
                "ambient_gain": AMBIENT_GAIN,
                "sfx_gain_multiplier": SFX_GAIN_MULTIPLIER,
                "master_limiter": MASTER_LIMIT,
                "sample_rate": AUDIO_RATE,
                "music_ambient_global_timeline": True,
                "music_ambient_looped_if_needed": True,
                "narration_sidechain_ducking": True,
                "sfx_tail_protection": True,
            },
        })
        write_json(RENDER_MANIFEST, final_manifest)

        saved = read_json(RENDER_MANIFEST, "Final render manifest")
        validate_completed(saved, "Final render manifest")
        if saved.get("part_numbers") != part_numbers:
            raise RuntimeError("Final render manifest Part range mismatch")
        if len(saved.get("all_completed_scenes", [])) != len(ordered_photos):
            raise RuntimeError("Final render manifest scene count mismatch")
        if len(saved.get("parts", [])) != parts_count:
            raise RuntimeError("Final render manifest Part output count mismatch")
        if saved.get("pre_caption_video_sha256") != sha256_file(PRE_CAPTION_VIDEO):
            raise RuntimeError("Pre-caption video read-back checksum mismatch")
        if saved.get("story_title") != output_slug or saved.get("format_dimensions") != f"{width}x{height}":
            raise RuntimeError("Final render manifest story/format metadata mismatch")
        if saved.get("source_photo_manifest_sha256") != photo_hash or saved.get("source_audio_manifest_sha256") != audio_manifest_hash:
            raise RuntimeError("Final render manifest source hash mismatch")
        if saved.get("source_sound_fingerprint") != sound_assets["fingerprint"]:
            raise RuntimeError("Final render manifest sound fingerprint mismatch")

        write_json(
            CHECKPOINT_FILE,
            build_progress(completed, ordered_photos, "completed"),
        )

    except Exception as exc:
        save_checkpoint("failed", exc)
        log("RENDER FAILED:", str(exc))
        raise

    log("=" * 60)
    log("RENDER AND AUDIO MIX COMPLETED")
    log("FORMAT:", mode, "PARTS:", part_numbers)
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("MAIN SCENES:", len(main_rows))
    log("ALL ROLE CLIPS:", len(ordered_photos))
    log("NEW RENDERS:", new_renders, "REUSED:", reused_renders)
    log("PART OUTPUTS:", len(part_rows))
    log("PRE-CAPTION VIDEO:", relative(PRE_CAPTION_VIDEO))
    log("MANIFEST:", relative(RENDER_MANIFEST))
    log("CHECKPOINT:", relative(CHECKPOINT_FILE))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("RENDER INTERRUPTED; completed scene checkpoints retained.")
        sys.exit(130)
    except Exception as exc:
        log("RENDER FAILED:", str(exc))
        sys.exit(1)