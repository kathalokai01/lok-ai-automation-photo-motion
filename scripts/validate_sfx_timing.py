
#!/usr/bin/env python3
"""Validate SFX cues against actual narration durations and report trimming."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_parts, get_scenes

INPUT_FILE = ROOT / "Input" / "topic.txt"
AUDIO_FILE = ROOT / "output" / "narration" / "audio_jobs.json"
CUES_FILE = ROOT / "output" / "sound_design" / "sfx_cues.json"
OUTPUT_FILE = ROOT / "output" / "sound_design" / "sfx_timing_validation.json"

VERSION = "sfx-timing-validator-v5-aliases-scene-audit"
MIN_AUDIO_BYTES = 100
TIME_EPSILON = 0.001
MAX_GAIN = 1.0


def log(*items):
    print(*items, flush=True)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON file {path}: {exc}") from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure in {path}")

    return value


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def rows_from(data, *keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def first_path(row, names):
    if not isinstance(row, dict):
        return None

    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def first_value(row, names, default=None):
    if not isinstance(row, dict):
        return default

    for name in names:
        if name in row and row[name] is not None:
            return row[name]

    return default


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing file path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory")

    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")

    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is too small: {path.name}")

    return path


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def media_info(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError(
            "ffprobe is required to validate actual audio durations"
        )

    result = subprocess.run(
        [
            executable,
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

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid media duration for {path.name}"
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration for {path.name}")

    has_audio = any(
        isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
        for stream in data.get("streams", [])
    )

    if not has_audio:
        raise RuntimeError(f"No audio stream found in {path.name}")

    return {
        "duration": duration,
        "has_audio": True,
        "bytes": path.stat().st_size,
    }


def expected_scene_keys(parts, scenes):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }


def load_narration(parts, scenes):
    data = read_json(AUDIO_FILE)
    jobs = rows_from(data, "jobs", "audio_jobs", "scenes")

    if not jobs:
        raise RuntimeError("No narration jobs found in audio_jobs.json")

    mapping = {}

    for index, row in enumerate(jobs, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration job at row {index}")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration part/scene mapping at row {index}"
            ) from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")

        if str(row.get("status", "")).strip().lower() != "completed":
            raise RuntimeError(f"Narration is not completed for scene {key}")

        path = safe_file(
            first_path(
                row,
                ("output", "audio_path", "path", "file", "audio"),
            ),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)
        mapping[key] = {
            "path": path,
            "relative_path": relative(path),
            "duration": info["duration"],
            "bytes": info["bytes"],
            "sha256": sha256_file(path),
        }

    expected = expected_scene_keys(parts, scenes)
    actual = set(mapping)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)

    if missing or extra:
        raise RuntimeError(
            f"Narration scene mismatch: missing={missing}; extra={extra}"
        )

    return mapping


def cue_timestamp(row):
    """Read common timestamp aliases without silently accepting bad values."""
    value = first_value(
        row,
        ("time", "time_seconds", "timestamp", "start_seconds", "start"),
        0,
    )

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid SFX timestamp: {value!r}") from exc

    if not math.isfinite(result) or result < 0:
        raise RuntimeError(f"SFX timestamp must be finite and non-negative")

    return result


def cue_gain(row):
    value = first_value(row, ("gain", "volume", "volume_gain"), 0.075)

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid SFX gain: {value!r}") from exc

    if not math.isfinite(result) or not 0 <= result <= MAX_GAIN:
        raise RuntimeError("SFX gain must be between 0 and 1")

    return result


def validate_cues(parts, scenes, narration):
    data = read_json(CUES_FILE)

    if not isinstance(data, dict):
        raise RuntimeError("SFX cue manifest must be a JSON object")

    if data.get("status") not in (None, "completed"):
        raise RuntimeError("SFX cue manifest is not completed")

    rows = rows_from(data, "cues", "items")
    seen = set()
    reports = []
    warnings = []
    cue_scenes = set()
    per_scene = {}

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid SFX cue at row {index}")

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid SFX scene mapping at row {index}"
            ) from exc

        key = (part, scene)

        if not (1 <= part <= parts and 1 <= scene <= scenes):
            raise RuntimeError(f"SFX cue scene is out of range: {key}")

        if key not in narration:
            raise RuntimeError(f"SFX cue references unknown narration: {key}")

        timestamp = cue_timestamp(row)
        gain = cue_gain(row)
        kind = str(row.get("kind", "unspecified")).strip() or "unspecified"

        cue_id = (part, scene, round(timestamp, 6), kind)
        if cue_id in seen:
            raise RuntimeError(f"Duplicate SFX cue: {cue_id}")
        seen.add(cue_id)

        narration_duration = narration[key]["duration"]

        if timestamp >= narration_duration:
            raise RuntimeError(
                f"SFX cue starts at or after narration end for {key}: "
                f"cue={timestamp:.3f}s, "
                f"narration={narration_duration:.3f}s"
            )

        path = safe_file(
            first_path(row, ("file", "path", "audio", "output")),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)
        remaining = max(0.0, narration_duration - timestamp)

        # This is the maximum duration that can fit before the narration/
        # scene ends. The validator does not modify the audio file.
        effective_duration = min(info["duration"], remaining)
        truncated_duration = max(0.0, info["duration"] - effective_duration)

        if truncated_duration > TIME_EPSILON:
            timing_status = "will_be_trimmed_at_narration_end"
            warnings.append({
                "part": part,
                "scene": scene,
                "kind": kind,
                "cue_time_seconds": round(timestamp, 3),
                "sfx_file_duration_seconds": round(info["duration"], 3),
                "effective_duration_seconds": round(effective_duration, 3),
                "truncated_seconds": round(truncated_duration, 3),
                "note": (
                    "The SFX extends beyond the remaining narration duration. "
                    "This is a timing warning, not proof of the final mix."
                ),
            })
        else:
            timing_status = "fits_remaining_narration"

        cue_scenes.add(key)
        per_scene.setdefault(key, []).append({
            "time_seconds": timestamp,
            "kind": kind,
            "gain": gain,
            "duration_seconds": effective_duration,
        })

        reports.append({
            "part": part,
            "scene": scene,
            "kind": kind,
            "time_seconds": round(timestamp, 3),
            "gain": gain,
            "narration_duration_seconds": round(narration_duration, 3),
            "sfx_file_duration_seconds": round(info["duration"], 3),
            "remaining_narration_seconds": round(remaining, 3),
            "effective_duration_seconds": round(effective_duration, 3),
            "truncated_seconds": round(truncated_duration, 3),
            "timing_status": timing_status,
            "file": relative(path),
            "bytes": info["bytes"],
            "sha256": sha256_file(path),
        })

    reports.sort(
        key=lambda item: (
            item["part"],
            item["scene"],
            item["time_seconds"],
            item["kind"],
        )
    )

    scene_report = []

    for part in range(1, parts + 1):
        for scene in range(1, scenes + 1):
            key = (part, scene)
            scene_cues = sorted(
                per_scene.get(key, []),
                key=lambda item: item["time_seconds"],
            )

            narration_duration = narration[key]["duration"]
            effective_total = sum(
                item["duration_seconds"] for item in scene_cues
            )

            scene_report.append({
                "part": part,
                "scene": scene,
                "narration_duration_seconds": round(narration_duration, 3),
                "sfx_cue_count": len(scene_cues),
                "effective_sfx_seconds_sum": round(effective_total, 3),
                "sfx_time_sum_exceeds_scene_duration": (
                    effective_total > narration_duration + TIME_EPSILON
                ),
                "note": (
                    "Summed SFX durations may overlap and therefore do not "
                    "represent unique audible coverage."
                ),
            })

    return {
        "status": "valid",
        "cue_count": len(reports),
        "scenes_with_sfx": len(cue_scenes),
        "scenes_without_sfx": len(narration) - len(cue_scenes),
        "cues": reports,
        "scene_audit": scene_report,
        "warnings": warnings,
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required but was not found")

    if not AUDIO_FILE.is_file():
        raise RuntimeError(f"Narration manifest missing: {AUDIO_FILE}")

    if not CUES_FILE.is_file():
        raise RuntimeError(f"SFX cue manifest missing: {CUES_FILE}")

    config = load_and_validate(INPUT_FILE)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must both be positive")

    narration = load_narration(parts, scenes)
    sfx_report = validate_cues(parts, scenes, narration)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": "local_audio_validation",
        "external_api_calls": 0,
        "paid_services_used": False,
        "expected_scenes": parts * scenes,
        "narration_scenes_checked": len(narration),
        "narration_total_seconds": round(
            sum(item["duration"] for item in narration.values()), 3
        ),
        "renderer_behavior": {
            "scene_duration_source": "actual narration file duration",
            "sfx_end_limit": "narration/scene end",
            "note": (
                "This validator reports potential SFX trimming. It does not "
                "modify the source audio, cue manifest or final video."
            ),
        },
        "sfx": sfx_report,
        "limitations": [
            "This validates file paths, stream presence and declared timing.",
            "It does not measure perceived loudness or narration masking.",
            "Summed SFX durations can overlap and must not be treated as unique coverage.",
            "A valid cue does not prove the final renderer mixed it correctly.",
            "This script makes no external API calls and generates no audio.",
        ],
    }

    write_json(OUTPUT_FILE, report)

    log("SFX TIMING VALIDATION: PASSED")
    log("Narration scenes checked:", len(narration))
    log("SFX cues checked:", sfx_report["cue_count"])
    log("Scenes without SFX:", sfx_report["scenes_without_sfx"])
    log("Potentially trimmed cues:", len(sfx_report["warnings"]))
    log("Report:", relative(OUTPUT_FILE))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SFX TIMING VALIDATION: FAILED:", str(exc))
        sys.exit(1)
