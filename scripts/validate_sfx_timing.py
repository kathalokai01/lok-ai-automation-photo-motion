#!/usr/bin/env python3
"""Validate SFX cues against real narration durations before rendering."""

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
OUTPUT_FILE = (
    ROOT / "output" / "sound_design" / "sfx_timing_validation.json"
)

VERSION = "sfx-timing-validator-v3"
MIN_AUDIO_BYTES = 100
TIME_EPSILON = 0.001
MAX_GAIN = 1.0


def log(*items):
    print(*items, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON file {path}: {exc}") from exc


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
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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
        timeout=60,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-800:]}"
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

    streams = data.get("streams", [])

    has_audio = any(
        isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
        for stream in streams
    )

    if not has_audio:
        raise RuntimeError(f"No audio stream found in {path.name}")

    return {"duration": duration, "has_audio": has_audio}


def expected_scene_keys(parts, scenes):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }


def load_narration(parts, scenes):
    data = read_json(AUDIO_FILE)
    rows = rows_from(data, "jobs", "audio_jobs")

    if not rows:
        raise RuntimeError("No narration jobs found in audio_jobs.json")

    mapping = {}

    for index, row in enumerate(rows, start=1):
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

        value = first_path(
            row,
            ("output", "audio_path", "path", "file", "audio"),
        )

        path = safe_file(
            value,
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)

        mapping[key] = {
            "path": path,
            "duration": info["duration"],
            "sha256": sha256_file(path),
        }

    expected = expected_scene_keys(parts, scenes)
    actual = set(mapping)

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)

    if missing or extra:
        raise RuntimeError(
            "Narration scene mismatch: "
            f"missing={missing}; extra={extra}"
        )

    return mapping


def validate_cues(parts, scenes, narration):
    data = read_json(CUES_FILE)

    if not isinstance(data, dict):
        raise RuntimeError("SFX cue manifest must be a JSON object")

    if data.get("status") not in (None, "completed"):
        raise RuntimeError("SFX cue manifest is not completed")

    rows = rows_from(data, "cues", "items")

    seen = set()
    valid = []
    warnings = []
    cue_scenes = set()

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid SFX cue at row {index}")

        try:
            part = int(row["part"])
            scene = int(row["scene"])
            timestamp = float(row.get("time", 0))
            gain = float(row.get("gain", 0.075))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid SFX mapping, timestamp or gain at row {index}"
            ) from exc

        key = (part, scene)

        if not (1 <= part <= parts and 1 <= scene <= scenes):
            raise RuntimeError(f"SFX cue scene is out of range: {key}")

        if key not in narration:
            raise RuntimeError(f"SFX cue references unknown narration: {key}")

        kind = str(row.get("kind", "unspecified")).strip() or "unspecified"

        cue_id = (part, scene, round(timestamp, 6), kind)

        if cue_id in seen:
            raise RuntimeError(f"Duplicate SFX cue: {cue_id}")

        seen.add(cue_id)
        cue_scenes.add(key)

        if not math.isfinite(timestamp) or timestamp < 0:
            raise RuntimeError(f"Invalid SFX timestamp at row {index}")

        if not math.isfinite(gain) or not 0 <= gain <= MAX_GAIN:
            raise RuntimeError(f"SFX gain must be between 0 and 1: row {index}")

        narration_duration = narration[key]["duration"]

        if timestamp >= narration_duration:
            raise RuntimeError(
                f"SFX cue starts after narration ends for {key}: "
                f"cue={timestamp:.3f}s, "
                f"narration={narration_duration:.3f}s"
            )

        path_value = first_path(
            row,
            ("file", "path", "audio", "output"),
        )

        path = safe_file(
            path_value,
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)
        remaining = narration_duration - timestamp
        tail_overrun = info["duration"] - remaining

        cue_report = {
            "part": part,
            "scene": scene,
            "kind": kind,
            "time_seconds": round(timestamp, 3),
            "gain": gain,
            "narration_duration_seconds": round(narration_duration, 3),
            "sfx_duration_seconds": round(info["duration"], 3),
            "remaining_narration_seconds": round(remaining, 3),
            "file": path.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(path),
            "timing_status": "valid",
        }

        if tail_overrun > TIME_EPSILON:
            cue_report["timing_status"] = "warning_sfx_may_extend"
            warnings.append({
                "part": part,
                "scene": scene,
                "kind": kind,
                "warning": "SFX may extend beyond narration duration",
                "overrun_seconds": round(tail_overrun, 3),
                "sfx_duration_seconds": round(info["duration"], 3),
                "remaining_narration_seconds": round(remaining, 3),
            })

        valid.append(cue_report)

    return {
        "status": "valid",
        "cue_count": len(valid),
        "scenes_with_sfx": len(cue_scenes),
        "scenes_without_sfx": len(narration) - len(cue_scenes),
        "cues": valid,
        "warnings": warnings,
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required but was not found")

    config = load_and_validate(INPUT_FILE)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    narration = load_narration(parts, scenes)
    cue_report = validate_cues(parts, scenes, narration)

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
        "sfx": cue_report,
        "limitations": [
            "This validates media timing, not subjective sound quality.",
            "A valid cue may still be too loud or masked by narration.",
            "A warning indicates that an SFX may continue after narration ends.",
            "This script validates existing files; it does not generate audio.",
        ],
    }

    write_json(OUTPUT_FILE, report)

    log("SFX TIMING VALIDATION: PASSED")
    log("Narration scenes checked:", len(narration))
    log("SFX cues checked:", cue_report["cue_count"])
    log("Scenes without SFX:", cue_report["scenes_without_sfx"])
    log("Timing warnings:", len(cue_report["warnings"]))
    log("Report:", OUTPUT_FILE.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SFX TIMING VALIDATION: FAILED")
        log(str(exc))
        sys.exit(1)