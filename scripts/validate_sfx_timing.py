
#!/usr/bin/env python3
"""Strict, free and resumeless SFX timing audit against real narration files."""

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

VERSION = "sfx-timing-validator-v6-strict-timestamps-overlap-audit"
MIN_AUDIO_BYTES = 100
EPSILON = 0.001
MAX_GAIN = 1.0
TIME_KEYS = ("time", "time_seconds", "timestamp", "start_seconds", "start")
PATH_KEYS = ("file", "path", "audio", "output", "audio_path")


def log(*values):
    print(*values, flush=True)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON: {path}: {exc}") from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Invalid JSON structure: {path}")

    return value


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def rows_from(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def first_value(row, keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def required_number(row, keys, label, minimum=None, maximum=None):
    value = first_value(row, keys)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RuntimeError(f"{label}: required numeric field is missing")

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
        raise RuntimeError("ffprobe is required but not installed")

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

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")

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


def expected_keys(parts, scenes):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }


def load_narration(parts, scenes):
    data = read_json(AUDIO_FILE)
    jobs = rows_from(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        raise RuntimeError("No narration jobs found")

    mapping = {}

    for index, row in enumerate(jobs, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration mapping at row {index}"
            ) from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        if str(row.get("status", "")).strip().lower() != "completed":
            raise RuntimeError(f"Narration is incomplete: {key}")

        path = safe_file(
            first_value(row, PATH_KEYS),
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

    expected = expected_keys(parts, scenes)
    actual = set(mapping)

    if actual != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
            f"missing={sorted(expected - actual)}; "
            f"extra={sorted(actual - expected)}"
        )

    return mapping


def validate_cues(parts, scenes, narration):
    data = read_json(CUES_FILE)

    if not isinstance(data, dict):
        raise RuntimeError("SFX cue manifest must be a JSON object")
    if data.get("status") not in (None, "completed"):
        raise RuntimeError("SFX cue manifest is incomplete")

    cues = rows_from(data, "cues", "items")
    seen = set()
    reports = []
    warnings = []
    per_scene = {}

    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue at row {index}")

        try:
            key = (int(cue["part"]), int(cue["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid SFX scene mapping at row {index}"
            ) from exc

        if key not in narration or not (
            1 <= key[0] <= parts and 1 <= key[1] <= scenes
        ):
            raise RuntimeError(f"SFX cue references unknown scene: {key}")

        start = required_number(
            cue, TIME_KEYS, f"SFX cue {index} timestamp", minimum=0
        )
        gain = required_number(
            cue, ("gain", "volume", "volume_gain"),
            f"SFX cue {index} gain", minimum=0, maximum=MAX_GAIN,
        )
        kind = str(cue.get("kind", "unspecified")).strip() or "unspecified"

        cue_id = (key, round(start, 6), kind)
        if cue_id in seen:
            raise RuntimeError(f"Duplicate SFX cue: {cue_id}")
        seen.add(cue_id)

        audio_duration = narration[key]["duration"]
        if start >= audio_duration:
            raise RuntimeError(
                f"SFX begins outside narration scene {key}: "
                f"{start:.3f}s >= {audio_duration:.3f}s"
            )

        path = safe_file(
            first_value(cue, PATH_KEYS),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)

        available = max(0.0, audio_duration - start)
        effective = min(info["duration"], available)
        truncated = max(0.0, info["duration"] - effective)
        end = start + effective

        if truncated > EPSILON:
            warnings.append({
                "part": key[0],
                "scene": key[1],
                "kind": kind,
                "start_seconds": round(start, 3),
                "file_duration_seconds": round(info["duration"], 3),
                "effective_duration_seconds": round(effective, 3),
                "truncated_seconds": round(truncated, 3),
                "warning": "SFX extends beyond the narration scene end",
            })

        report = {
            "part": key[0],
            "scene": key[1],
            "kind": kind,
            "start_seconds": round(start, 4),
            "end_seconds": round(end, 4),
            "gain": gain,
            "narration_duration_seconds": round(audio_duration, 4),
            "file_duration_seconds": round(info["duration"], 4),
            "effective_duration_seconds": round(effective, 4),
            "truncated_seconds": round(truncated, 4),
            "file": relative(path),
            "sha256": sha256_file(path),
            "timing_status": (
                "may_be_trimmed" if truncated > EPSILON else "fits_scene"
            ),
        }
        reports.append(report)
        per_scene.setdefault(key, []).append(report)

    overlap_warnings = []
    scene_reports = []

    for part in range(1, parts + 1):
        for scene in range(1, scenes + 1):
            key = (part, scene)
            scene_cues = sorted(
                per_scene.get(key, []),
                key=lambda item: item["start_seconds"],
            )
            duration = narration[key]["duration"]
            overlaps = []

            for previous, current in zip(scene_cues, scene_cues[1:]):
                overlap = (
                    previous["end_seconds"] - current["start_seconds"]
                )
                if overlap > EPSILON:
                    item = {
                        "part": part,
                        "scene": scene,
                        "previous_kind": previous["kind"],
                        "next_kind": current["kind"],
                        "overlap_seconds": round(overlap, 4),
                        "note": (
                            "Overlapping SFX can be intentional; inspect "
                            "the mix if effects mask narration."
                        ),
                    }
                    overlaps.append(item)
                    overlap_warnings.append(item)

            scene_reports.append({
                "part": part,
                "scene": scene,
                "narration_duration_seconds": round(duration, 4),
                "sfx_cue_count": len(scene_cues),
                "overlap_count": len(overlaps),
                "overlaps": overlaps,
                "status": "has_sfx" if scene_cues else "no_sfx",
            })

    reports.sort(
        key=lambda item: (
            item["part"], item["scene"],
            item["start_seconds"], item["kind"],
        )
    )

    return {
        "status": "valid",
        "cue_count": len(reports),
        "scenes_with_sfx": sum(bool(v) for v in per_scene.values()),
        "scenes_without_sfx": len(narration) - len(per_scene),
        "cues": reports,
        "scene_audit": scene_reports,
        "truncation_warnings": warnings,
        "overlap_warnings": overlap_warnings,
        "limitations": [
            "Overlap is reported, not automatically treated as an error.",
            "This checks cue declarations and file durations, not perceived loudness.",
            "It does not prove that the final renderer mixed each cue correctly.",
            "It does not create true word-level narration alignment.",
        ],
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")

    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input file missing: {INPUT_FILE}")
    if not AUDIO_FILE.is_file():
        raise RuntimeError(f"Narration manifest missing: {AUDIO_FILE}")
    if not CUES_FILE.is_file():
        raise RuntimeError(f"SFX cue manifest missing: {CUES_FILE}")

    config = load_and_validate(INPUT_FILE)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    narration = load_narration(parts, scenes)
    sfx = validate_cues(parts, scenes, narration)

    report = {
        "status": "completed",
        "version": VERSION,
        "external_api_calls": 0,
        "paid_services_used": False,
        "expected_scenes": parts * scenes,
        "narration_scenes_checked": len(narration),
        "narration_total_seconds": round(
            sum(row["duration"] for row in narration.values()), 3
        ),
        "duration_source": "ffprobe duration of actual narration files",
        "sfx": sfx,
    }

    write_json(OUTPUT_FILE, report)

    log("SFX TIMING VALIDATION: PASSED")
    log("Narration scenes:", len(narration))
    log("SFX cues:", sfx["cue_count"])
    log("Scenes without SFX:", sfx["scenes_without_sfx"])
    log("Possible truncations:", len(sfx["truncation_warnings"]))
    log("Overlapping cue pairs:", len(sfx["overlap_warnings"]))
    log("Report:", relative(OUTPUT_FILE))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SFX TIMING VALIDATION: FAILED:", str(exc))
        sys.exit(1)
