#!/usr/bin/env python3
"""Validate SFX timing against variable per-Part narration scenes and roles."""

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
SOUND_MANIFEST = ROOT / "output" / "sound_design" / "sound_design_manifest.json"
CUES_FILE = ROOT / "output" / "sound_design" / "sfx_cues.json"
OUTPUT_FILE = ROOT / "output" / "sound_design" / "sfx_timing_validation.json"

VERSION = "sfx-validator-v10-variable-scenes-all-narration-roles"
MIN_AUDIO_BYTES = 100
EPSILON = 0.001
MAX_GAIN = 1.0
MAX_CUES_PER_SCENE = 12
DURATION_TOLERANCE = 0.20

TIME_KEYS = (
    "time", "time_seconds", "timestamp",
    "start_seconds", "start",
)
PATH_KEYS = (
    "file", "path", "audio", "output", "audio_path",
)
SFX_KEYS = (
    "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
    "SOUND_EFFECTS_ENABLED",
)
ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}
VALID_ROLES = set(ROLE_ORDER)


def log(*values):
    print(*values, flush=True)


def read_json(path):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read JSON: {path}: {exc}"
        ) from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Invalid JSON structure: {path}")
    return value


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

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
        if value is not None:
            return value
    return None


def config_bool(config, keys, default=True):
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
        raise RuntimeError(f"{key} must be true or false")
    return default


def positive_int(value, label):
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer") from exc
    if result < 1:
        raise RuntimeError(f"{label} must be at least 1")
    return result


def finite_number(value, label, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}: invalid number {value!r}") from exc

    if not math.isfinite(number):
        raise RuntimeError(f"{label}: number must be finite")
    if minimum is not None and number < minimum:
        raise RuntimeError(f"{label}: value must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise RuntimeError(f"{label}: value must be <= {maximum}")
    return number


def required_number(row, keys, label, minimum=None, maximum=None):
    value = first_value(row, keys)
    if value is None or (
        isinstance(value, str) and not value.strip()
    ):
        raise RuntimeError(f"{label}: required numeric field is missing")
    return finite_number(value, label, minimum, maximum)


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path is missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is too small: {path}")
    return path


def relative(path):
    return Path(path).resolve().relative_to(
        ROOT.resolve()
    ).as_posix()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def media_info(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required but not installed")

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
        raise RuntimeError(
            f"ffprobe timed out for {path.name}"
        ) from exc

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid media metadata: {path.name}"
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")

    has_audio = any(
        isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
        for stream in data.get("streams", [])
    )
    if not has_audio:
        raise RuntimeError(f"Audio stream missing: {path.name}")

    return {
        "duration": duration,
        "bytes": path.stat().st_size,
    }


def narration_identity(row, index):
    if not isinstance(row, dict):
        raise RuntimeError(f"Invalid narration row {index}")

    scene_id = str(row.get("scene_id") or row.get("job_id") or "").strip()
    if not scene_id:
        raise RuntimeError(f"Narration row {index} has no scene_id")

    try:
        part = positive_int(row.get("part"), f"Narration {scene_id} Part")
    except RuntimeError as exc:
        raise RuntimeError(f"Invalid narration Part at row {index}") from exc

    role = str(row.get("role") or "main").strip().lower()
    if role not in VALID_ROLES:
        raise RuntimeError(
            f"Narration {scene_id} has unsupported role {role!r}"
        )

    scene_value = row.get("scene")
    if role == "main":
        scene = positive_int(
            scene_value,
            f"Narration {scene_id} scene",
        )
    else:
        if scene_value not in (None, ""):
            raise RuntimeError(
                f"Extra narration role {scene_id} must have scene=None"
            )
        scene = None

    return {
        "scene_id": scene_id,
        "part": part,
        "scene": scene,
        "role": role,
    }


def load_narration(configured_parts, scenes_min, scenes_max, mode):
    data = read_json(AUDIO_FILE)
    if not isinstance(data, dict):
        raise RuntimeError("Narration manifest must be a JSON object")
    if data.get("status") != "completed":
        raise RuntimeError("Narration manifest is not completed")

    manifest_format = str(data.get("format", "")).strip().lower()
    if manifest_format and manifest_format != mode:
        raise RuntimeError(
            f"Narration FORMAT mismatch: Input={mode}, "
            f"manifest={manifest_format}"
        )

    jobs = rows_from(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        raise RuntimeError("No narration jobs found")

    mapping = {}
    main_scenes = {}
    role_counts = {}

    for index, row in enumerate(jobs, 1):
        identity = narration_identity(row, index)
        scene_id = identity["scene_id"]

        if scene_id in mapping:
            raise RuntimeError(f"Duplicate narration scene_id: {scene_id}")

        status = str(row.get("status", "")).strip().lower()
        if status not in {"completed", "reused"}:
            raise RuntimeError(f"Narration is incomplete: {scene_id}")

        path = safe_file(
            first_value(row, PATH_KEYS),
            f"Narration {scene_id}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        actual_hash = sha256_file(path)

        declared_hash = (
            row.get("sha256")
            or row.get("audio_sha256")
            or row.get("audio_hash")
        )
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(
                f"Narration checksum mismatch: {scene_id}"
            )

        declared_duration = first_value(
            row,
            (
                "actual_audio_duration_seconds",
                "audio_duration",
                "duration",
            ),
        )
        if declared_duration is None:
            raise RuntimeError(
                f"Narration duration is missing: {scene_id}"
            )

        declared_duration = finite_number(
            declared_duration,
            f"Narration duration {scene_id}",
            minimum=0.01,
        )
        if abs(declared_duration - info["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Narration duration mismatch: {scene_id}"
            )

        duration_source = (
            row.get("duration_source")
            or row.get("audio_duration_source")
        )
        if duration_source != "ffprobe_measured":
            raise RuntimeError(
                f"Narration duration is not ffprobe-measured: {scene_id}"
            )

        item = {
            **identity,
            "path": path,
            "relative_path": relative(path),
            "duration": info["duration"],
            "bytes": info["bytes"],
            "sha256": actual_hash,
            "text": str(row.get("text") or ""),
        }
        mapping[scene_id] = item

        role_counts.setdefault(identity["part"], {
            role: 0 for role in ROLE_ORDER
        })
        role_counts[identity["part"]][identity["role"]] += 1

        if identity["role"] == "main":
            key = (identity["part"], identity["scene"])
            if key in main_scenes:
                raise RuntimeError(
                    f"Duplicate Main scene mapping: {key}"
                )
            main_scenes[key] = item

    discovered_parts = sorted({
        item["part"] for item in mapping.values()
    })
    if not discovered_parts:
        raise RuntimeError("Narration manifest contains no Parts")
    if discovered_parts != list(
        range(discovered_parts[0], discovered_parts[-1] + 1)
    ):
        raise RuntimeError("Narration Part numbering is not continuous")
    if discovered_parts[0] != 1:
        raise RuntimeError("Narration Parts must start at Part 1")

    actual_parts = len(discovered_parts)
    if actual_parts != configured_parts:
        raise RuntimeError(
            f"Narration Part count mismatch: configured={configured_parts}, "
            f"actual={actual_parts}"
        )

    declared_parts = data.get("total_parts", data.get("parts"))
    if isinstance(declared_parts, int) and declared_parts != actual_parts:
        raise RuntimeError("Narration declared Part count mismatch")

    actual_counts = {}
    for part in discovered_parts:
        scenes = sorted(
            scene for current_part, scene in main_scenes
            if current_part == part
        )
        if not scenes:
            raise RuntimeError(f"Part {part} has no Main scenes")

        expected = list(range(1, len(scenes) + 1))
        if scenes != expected:
            raise RuntimeError(
                f"Part {part} Main scene numbering has gaps or duplicates: "
                f"{scenes}"
            )

        count = len(scenes)
        if not scenes_min <= count <= scenes_max:
            raise RuntimeError(
                f"Part {part} has {count} Main scenes; "
                f"configured SCENES range is {scenes_min}-{scenes_max}"
            )
        actual_counts[str(part)] = count

    final_part = discovered_parts[-1]
    for part in discovered_parts:
        counts = role_counts[part]
        if counts["hook"] != 1:
            raise RuntimeError(f"Part {part} must have exactly one Hook")
        if counts["suspense"] != (1 if part != final_part else 0):
            raise RuntimeError(f"Part {part} has invalid Suspense role count")
        if counts["resolution"] != (1 if part == final_part else 0):
            raise RuntimeError(f"Part {part} has invalid Resolution role count")

    expected_records = (
        sum(actual_counts.values())
        + actual_parts
        + max(0, actual_parts - 1)
        + 1
    )
    if len(mapping) != expected_records:
        raise RuntimeError(
            f"Narration record count mismatch: expected={expected_records}, "
            f"found={len(mapping)}"
        )

    log("FORMAT:", mode)
    log("ACTUAL PARTS:", actual_parts)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL MAIN SCENES PER PART:", actual_counts)
    log("ALL NARRATION RECORDS:", len(mapping))
    log("ROLE COUNTS:", role_counts)

    return mapping, actual_parts, actual_counts, role_counts

def validate_sound_manifest(
    mode,
    parts,
    actual_counts,
    narration,
):
    manifest = read_json(SOUND_MANIFEST)
    if not isinstance(manifest, dict):
        raise RuntimeError("Sound-design manifest must be an object")
    if manifest.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")

    manifest_format = str(
        manifest.get("format", "")
    ).strip().lower()
    if manifest_format != mode:
        raise RuntimeError(
            f"Sound-design FORMAT mismatch: {manifest_format} != {mode}"
        )

    if positive_int(manifest.get("parts"), "Sound-design parts") != parts:
        raise RuntimeError("Sound-design actual Part count mismatch")

    manifest_counts = manifest.get("actual_scene_counts")
    if not isinstance(manifest_counts, dict):
        raise RuntimeError(
            "Sound-design actual_scene_counts is missing"
        )

    normalized_manifest_counts = {}
    for part, count in actual_counts.items():
        value = manifest_counts.get(part)
        if value is None:
            value = manifest_counts.get(str(part))
        normalized_manifest_counts[str(part)] = positive_int(
            value,
            f"Sound-design Part {part} scene count",
        )

    if normalized_manifest_counts != actual_counts:
        raise RuntimeError(
            "Sound-design actual scene counts do not match narration: "
            f"expected={actual_counts}, "
            f"found={normalized_manifest_counts}"
        )

    expected_records = len(narration)
    declared_records = manifest.get("total_audio_records")
    if declared_records is not None:
        if positive_int(
            declared_records,
            "Sound-design total_audio_records",
        ) != expected_records:
            raise RuntimeError(
                "Sound-design narration record count mismatch"
            )

    enabled_tracks = manifest.get("enabled")
    if not isinstance(enabled_tracks, dict):
        raise RuntimeError("Sound-design enabled settings are missing")

    for name in ("music", "ambient", "sfx"):
        if not isinstance(enabled_tracks.get(name), bool):
            raise RuntimeError(
                f"Sound-design enabled.{name} must be boolean"
            )

    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks must be an object")

    total_duration = sum(
        row["duration"] for row in narration.values()
    )

    for category in ("music", "ambient"):
        record = tracks.get(category)

        if not enabled_tracks[category]:
            if record is not None:
                raise RuntimeError(
                    f"{category} is disabled but a track is declared"
                )
            continue

        if not isinstance(record, dict):
            raise RuntimeError(
                f"{category} is enabled but its track is missing"
            )
        if record.get("status") != "completed":
            raise RuntimeError(f"{category} track is not completed")

        path = safe_file(
            record.get("path"),
            f"{category} track",
            MIN_AUDIO_BYTES,
        )
        actual_hash = sha256_file(path)
        if record.get("sha256") != actual_hash:
            raise RuntimeError(
                f"{category} track checksum mismatch"
            )

        info = media_info(path)
        if info["duration"] + 0.5 < total_duration:
            raise RuntimeError(
                f"{category} track is shorter than narration: "
                f"{info['duration']:.2f}s < {total_duration:.2f}s"
            )

    return manifest


def validate_cues(
    parts,
    actual_counts,
    narration,
    sfx_enabled,
    sound_manifest,
):
    data = read_json(CUES_FILE)
    if not isinstance(data, dict):
        raise RuntimeError("SFX cue manifest must be a JSON object")
    if data.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed")

    cue_format = str(data.get("format", "")).strip().lower()
    if cue_format != sound_manifest.get("format"):
        raise RuntimeError(
            "SFX cue and sound-design FORMAT mismatch"
        )

    declared_parts = data.get("parts", data.get("actual_parts"))
    if positive_int(declared_parts, "SFX cue Parts") != parts:
        raise RuntimeError("SFX cue Part count mismatch")

    cue_counts = data.get("actual_scene_counts")
    if not isinstance(cue_counts, dict):
        raise RuntimeError("SFX actual_scene_counts is missing")

    normalized_cue_counts = {}
    for part, count in actual_counts.items():
        value = cue_counts.get(part)
        if value is None:
            value = cue_counts.get(str(part))
        normalized_cue_counts[str(part)] = positive_int(
            value,
            f"SFX Part {part} scene count",
        )

    if normalized_cue_counts != actual_counts:
        raise RuntimeError(
            "SFX actual scene counts do not match narration"
        )

    declared_total = data.get(
        "total_audio_records",
        data.get("total_scenes"),
    )
    if declared_total is not None:
        if positive_int(
            declared_total,
            "SFX total narration records",
        ) != len(narration):
            raise RuntimeError(
                "SFX cue narration record count mismatch"
            )

    cues = rows_from(data, "cues", "items")

    if not sfx_enabled and cues:
        raise RuntimeError(
            "SFX is disabled but stale cues remain. "
            "Regenerate sound design."
        )
    if sfx_enabled and not cues:
        raise RuntimeError(
            "SFX is enabled but the cue manifest contains zero cues"
        )

    seen = set()
    reports = []
    warnings = []
    per_scene = {}

    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue at row {index}")

        scene_id = str(cue.get("scene_id") or "").strip()
        if not scene_id or scene_id not in narration:
            raise RuntimeError(
                f"SFX cue references unknown narration scene_id: "
                f"{scene_id or index}"
            )

        narration_row = narration[scene_id]
        try:
            part = positive_int(
                cue.get("part"),
                f"SFX cue {index} Part",
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"Invalid SFX Part mapping at row {index}"
            ) from exc

        role = str(cue.get("role") or "main").strip().lower()
        if role not in VALID_ROLES:
            raise RuntimeError(
                f"SFX cue {index} has invalid role: {role}"
            )

        scene_value = cue.get("scene")
        if role == "main":
            scene = positive_int(
                scene_value,
                f"SFX cue {index} scene",
            )
        else:
            if scene_value not in (None, ""):
                raise RuntimeError(
                    f"Extra-role SFX cue must have scene=None: {scene_id}"
                )
            scene = None

        if (
            part != narration_row["part"]
            or role != narration_row["role"]
            or scene != narration_row["scene"]
        ):
            raise RuntimeError(
                f"SFX cue identity mismatch for narration {scene_id}"
            )

        start = required_number(
            cue,
            TIME_KEYS,
            f"SFX cue {index} timestamp",
            minimum=0,
        )
        gain = required_number(
            cue,
            ("gain", "volume", "volume_gain"),
            f"SFX cue {index} gain",
            minimum=0,
            maximum=MAX_GAIN,
        )

        kind = str(cue.get("kind", "unspecified")).strip() or "unspecified"
        cue_id = (scene_id, round(start, 6), kind)
        if cue_id in seen:
            raise RuntimeError(f"Duplicate SFX cue: {cue_id}")
        seen.add(cue_id)

        path = safe_file(
            first_value(cue, PATH_KEYS),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        actual_hash = sha256_file(path)

        declared_hash = cue.get("sha256")
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(
                f"SFX checksum mismatch: cue {index}"
            )

        declared_duration = cue.get(
            "duration_seconds",
            cue.get("effect_duration_seconds"),
        )
        if declared_duration is not None:
            declared_duration = finite_number(
                declared_duration,
                f"SFX cue {index} declared duration",
                minimum=0.01,
            )
            if abs(declared_duration - info["duration"]) > 0.15:
                warnings.append({
                    "scene_id": scene_id,
                    "part": part,
                    "scene": scene,
                    "role": role,
                    "kind": kind,
                    "warning": (
                        "Declared SFX duration differs from "
                        "the actual audio file duration"
                    ),
                    "declared_seconds": round(declared_duration, 3),
                    "actual_seconds": round(info["duration"], 3),
                })

        scene_duration = narration_row["duration"]
        if start >= scene_duration:
            raise RuntimeError(
                f"SFX begins outside narration {scene_id}: "
                f"{start:.3f}s >= {scene_duration:.3f}s"
            )

        if not sfx_enabled:
            raise RuntimeError(
                "Unexpected SFX cue while SFX is disabled"
            )

        available = max(0.0, scene_duration - start)
        effective = min(info["duration"], available)
        truncated = max(0.0, info["duration"] - effective)

        maximum_start = max(
            0.0,
            scene_duration - info["duration"] - 0.05,
        )
        renderer_start = min(start, maximum_start)
        renderer_effective = min(
            info["duration"],
            max(0.0, scene_duration - renderer_start),
        )
        renderer_truncated = max(
            0.0,
            info["duration"] - renderer_effective,
        )

        if renderer_start + EPSILON < start:
            warnings.append({
                "scene_id": scene_id,
                "part": part,
                "scene": scene,
                "role": role,
                "kind": kind,
                "declared_start_seconds": round(start, 4),
                "renderer_start_seconds": round(renderer_start, 4),
                "warning": (
                    "Renderer will shift this SFX earlier "
                    "to preserve its duration"
                ),
            })

        if renderer_truncated > 0.02:
            warnings.append({
                "scene_id": scene_id,
                "part": part,
                "scene": scene,
                "role": role,
                "kind": kind,
                "truncated_seconds": round(renderer_truncated, 4),
                "warning": (
                    "SFX file is longer than the full narration record"
                ),
            })

        report = {
            "scene_id": scene_id,
            "part": part,
            "scene": scene,
            "role": role,
            "kind": kind,
            "start_seconds": round(start, 4),
            "end_seconds": round(start + effective, 4),
            "renderer_start_seconds": round(renderer_start, 4),
            "renderer_end_seconds": round(
                renderer_start + renderer_effective, 4
            ),
            "gain": gain,
            "narration_duration_seconds": round(scene_duration, 4),
            "file_duration_seconds": round(info["duration"], 4),
            "effective_duration_seconds": round(effective, 4),
            "renderer_effective_duration_seconds": round(
                renderer_effective, 4
            ),
            "truncated_seconds": round(truncated, 4),
            "renderer_truncated_seconds": round(
                renderer_truncated, 4
            ),
            "file": relative(path),
            "sha256": actual_hash,
            "timing_status": (
                "fits_scene"
                if renderer_truncated <= 0.02
                else "may_be_trimmed"
            ),
        }

        reports.append(report)
        per_scene.setdefault(scene_id, []).append(report)

        if len(per_scene[scene_id]) > MAX_CUES_PER_SCENE:
            raise RuntimeError(
                f"Too many SFX cues in narration {scene_id}: "
                f"{len(per_scene[scene_id])}"
            )

    if sfx_enabled:
        expected_ids = set(narration)
        actual_ids = set(per_scene)
        if actual_ids != expected_ids:
            raise RuntimeError(
                "SFX narration coverage mismatch: "
                f"missing={sorted(expected_ids - actual_ids)[:30]}; "
                f"unknown={sorted(actual_ids - expected_ids)[:30]}"
            )

    reports.sort(
        key=lambda item: (
            item["part"],
            ROLE_ORDER[item["role"]],
            item["scene"] or 0,
            item["start_seconds"],
            item["kind"],
        )
    )

    overlap_warnings = []
    scene_reports = []

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
        for previous, current in zip(
            scene_cues,
            scene_cues[1:],
        ):
            overlap = (
                previous["renderer_end_seconds"]
                - current["renderer_start_seconds"]
            )
            if overlap > EPSILON:
                item = {
                    "scene_id": scene_id,
                    "part": narration_row["part"],
                    "scene": narration_row["scene"],
                    "role": narration_row["role"],
                    "previous_kind": previous["kind"],
                    "next_kind": current["kind"],
                    "overlap_seconds": round(overlap, 4),
                    "note": (
                        "Overlapping effects are permitted; "
                        "check that they do not mask narration."
                    ),
                }
                overlaps.append(item)
                overlap_warnings.append(item)

        scene_reports.append({
            "scene_id": scene_id,
            "part": narration_row["part"],
            "scene": narration_row["scene"],
            "role": narration_row["role"],
            "narration_duration_seconds": round(
                narration_row["duration"], 4
            ),
            "sfx_cue_count": len(scene_cues),
            "overlap_count": len(overlaps),
            "overlaps": overlaps,
            "status": "has_sfx" if scene_cues else "no_sfx",
        })

    return {
        "status": "valid",
        "enabled": sfx_enabled,
        "cue_count": len(reports),
        "scenes_with_sfx": sum(
            bool(value) for value in per_scene.values()
        ),
        "scenes_without_sfx": (
            len(narration) - len(per_scene)
        ),
        "cues": reports,
        "scene_audit": scene_reports,
        "truncation_warnings": warnings,
        "overlap_warnings": overlap_warnings,
        "limitations": [
            "This validates cue timing and media files, not perceived loudness.",
            "The renderer still must mix the validated tracks correctly.",
            "This does not create word-level narration alignment.",
        ],
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")

    for path in (
        INPUT_FILE,
        AUDIO_FILE,
        SOUND_MANIFEST,
        CUES_FILE,
    ):
        if not path.is_file():
            raise RuntimeError(f"Required file missing: {path}")

    config = load_and_validate(INPUT_FILE)
    mode = str(get_format(config)).strip().lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    configured_parts = positive_int(get_parts(config), "PARTS")
    scenes_min, scenes_max = get_scenes_range(config)

    sfx_enabled = config_bool(
        config,
        SFX_KEYS,
        default=True,
    )

    narration, actual_parts, actual_counts, role_counts = (
        load_narration(
            configured_parts,
            scenes_min,
            scenes_max,
            mode,
        )
    )

    sound_manifest = validate_sound_manifest(
        mode,
        actual_parts,
        actual_counts,
        narration,
    )

    manifest_sfx_enabled = sound_manifest["enabled"]["sfx"]
    if manifest_sfx_enabled != sfx_enabled:
        raise RuntimeError(
            "SFX_ENABLED in Input/topic.txt does not match "
            "sound-design manifest. Regenerate sound design."
        )

    sfx = validate_cues(
        actual_parts,
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
        "total_parts": actual_parts,
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
        "total_narration_records": len(narration),
        "expected_scenes": sum(actual_counts.values()),
        "external_api_calls": 0,
        "paid_services_used": False,
        "sfx_enabled": sfx_enabled,
        "narration_scenes_checked": len(narration),
        "narration_total_seconds": round(narration_duration, 3),
        "duration_source": (
            "ffprobe duration of actual narration audio files"
        ),
        "sound_design_manifest_sha256": sha256_file(SOUND_MANIFEST),
        "sfx_cue_manifest_sha256": sha256_file(CUES_FILE),
        "narration_manifest_sha256": sha256_file(AUDIO_FILE),
        "sound_tracks": {
            "music_enabled": sound_manifest["enabled"]["music"],
            "ambient_enabled": sound_manifest["enabled"]["ambient"],
            "sfx_enabled": sfx_enabled,
        },
        "sfx": sfx,
    }

    write_json(OUTPUT_FILE, report)

    log("=" * 60)
    log("SFX TIMING VALIDATION: PASSED")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("CONFIGURED PARTS:", configured_parts)
    log("ACTUAL PARTS:", actual_parts)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL MAIN SCENES:", actual_counts)
    log("TOTAL NARRATION RECORDS:", len(narration))
    log("NARRATION DURATION:", report["narration_total_seconds"])
    log("SFX ENABLED:", sfx_enabled)
    log("SFX CUES:", sfx["cue_count"])
    log("RECORDS WITHOUT SFX:", sfx["scenes_without_sfx"])
    log("TIMING WARNINGS:", len(sfx["truncation_warnings"]))
    log("OVERLAPPING CUE PAIRS:", len(sfx["overlap_warnings"]))
    log("REPORT:", relative(OUTPUT_FILE))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SFX TIMING VALIDATION: FAILED:", str(exc))
        sys.exit(1)