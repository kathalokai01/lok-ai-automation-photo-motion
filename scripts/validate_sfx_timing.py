
#!/usr/bin/env python3
"""Validate SFX cue timestamps against actual scene-audio durations.

No network calls. Never modifies generated media or existing manifests.
Writes output/sound_design/sfx_timing_validation.json.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"

AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
CUE_MANIFEST = OUT / "sound_design" / "sfx_cues.json"
REPORT = OUT / "sound_design" / "sfx_timing_validation.json"

VERSION = "sfx-timing-validator-v1"
MIN_AUDIO_BYTES = 100


def read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path}")
    return value


def write_report(data):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(REPORT)


def rows_from(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def resolve_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is too small")

    return path


def first_path(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def audio_duration(path: Path) -> float:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required to inspect audio duration")

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
            f"ffprobe failed for {path.name}: {result.stderr[-500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid audio metadata: {path.name}") from exc

    streams = data.get("streams", [])
    if not any(item.get("codec_type") == "audio" for item in streams):
        raise RuntimeError(f"No audio stream found: {path.name}")

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid audio duration: {path.name}")

    return duration


def load_scene_durations():
    data = read_json(AUDIO_MANIFEST)
    rows = rows_from(data, "jobs", "audio_jobs")
    if not rows:
        raise RuntimeError("No narration jobs found in audio manifest")

    durations = {}

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration part/scene mapping at row {index}"
            ) from exc

        if key in durations:
            raise RuntimeError(f"Duplicate narration mapping: {key}")

        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration is not completed for scene {key}")

        audio_value = first_path(
            row, ("output", "audio_path", "path", "file", "audio")
        )
        audio_path = resolve_file(
            audio_value, f"Narration scene {key}", MIN_AUDIO_BYTES
        )

        durations[key] = {
            "duration": audio_duration(audio_path),
            "path": audio_path.relative_to(ROOT).as_posix(),
        }

    return durations


def load_cues():
    if not CUE_MANIFEST.is_file():
        raise RuntimeError(f"SFX cue manifest is missing: {CUE_MANIFEST}")

    data = read_json(CUE_MANIFEST)

    if isinstance(data, dict):
        status = data.get("status")
        if status not in (None, "completed"):
            raise RuntimeError("SFX cue manifest is not completed")
        rows = rows_from(data, "cues", "items")
    else:
        rows = data

    if not rows:
        return []

    return rows


def validate():
    durations = load_scene_durations()
    cues = load_cues()

    problems = []
    valid = []
    tolerance = 0.02

    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict):
            problems.append({
                "cue": index,
                "error": "Cue row must be an object",
            })
            continue

        try:
            part = int(cue["part"])
            scene = int(cue["scene"])
            timestamp = float(cue.get("time", 0))
            gain = float(cue.get("gain", 1))
        except (KeyError, TypeError, ValueError):
            problems.append({
                "cue": index,
                "error": "Missing or invalid part, scene, time, or gain",
            })
            continue

        key = (part, scene)

        if key not in durations:
            problems.append({
                "cue": index,
                "part": part,
                "scene": scene,
                "error": "No matching completed narration scene",
            })
            continue

        if not math.isfinite(timestamp) or timestamp < 0:
            problems.append({
                "cue": index,
                "part": part,
                "scene": scene,
                "error": "Cue timestamp must be finite and non-negative",
            })
            continue

        if not math.isfinite(gain) or gain < 0:
            problems.append({
                "cue": index,
                "part": part,
                "scene": scene,
                "error": "Cue gain must be finite and non-negative",
            })
            continue

        cue_path_value = first_path(
            cue, ("file", "path", "audio", "output")
        )

        try:
            cue_path = resolve_file(
                cue_path_value, f"SFX cue {index}", MIN_AUDIO_BYTES
            )
            cue_audio_duration = audio_duration(cue_path)
        except RuntimeError as exc:
            problems.append({
                "cue": index,
                "part": part,
                "scene": scene,
                "error": str(exc),
            })
            continue

        scene_duration = durations[key]["duration"]

        if timestamp >= scene_duration:
            problems.append({
                "cue": index,
                "part": part,
                "scene": scene,
                "time_seconds": round(timestamp, 3),
                "scene_audio_seconds": round(scene_duration, 3),
                "error": "Cue starts at or after scene audio ends",
            })
            continue

        remaining = scene_duration - timestamp

        if cue_audio_duration > remaining + tolerance:
            problems.append({
                "cue": index,
                "part": part,
                "scene": scene,
                "time_seconds": round(timestamp, 3),
                "sfx_duration_seconds": round(cue_audio_duration, 3),
                "remaining_scene_seconds": round(remaining, 3),
                "warning": (
                    "SFX may extend beyond the scene audio; "
                    "the renderer must trim or intentionally allow overlap"
                ),
            })

        valid.append({
            "cue": index,
            "part": part,
            "scene": scene,
            "time_seconds": round(timestamp, 3),
            "sfx_duration_seconds": round(cue_audio_duration, 3),
            "scene_audio_seconds": round(scene_duration, 3),
            "file": cue_path.relative_to(ROOT).as_posix(),
        })

    errors = [
        item for item in problems
        if "error" in item
    ]
    warnings = [
        item for item in problems
        if "warning" in item
    ]

    report = {
        "status": "failed" if errors else "completed",
        "version": VERSION,
        "external_api_calls": 0,
        "paid_services_used": False,
        "narration_scene_count": len(durations),
        "sfx_cue_count": len(cues),
        "valid_cue_count": len(valid),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "valid_cues": valid,
        "issues": problems,
        "limitations": [
            "Checks cue start times against narration scene duration.",
            "Does not prove the SFX is audible or correctly mixed.",
            "Does not modify or trim any audio file.",
            "Uses the cue schema's time field as seconds relative to its scene.",
        ],
    }

    write_report(report)
    return report


def main():
    report = validate()
    print("SFX TIMING VALIDATION:", report["status"].upper())
    print("Cues:", report["sfx_cue_count"])
    print("Errors:", report["error_count"])
    print("Warnings:", report["warning_count"])
    print("Report:", REPORT.relative_to(ROOT).as_posix())

    if report["error_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        failure = {
            "status": "failed",
            "version": VERSION,
            "external_api_calls": 0,
            "error": str(exc),
        }
        try:
            write_report(failure)
        except Exception:
            pass
        print("SFX TIMING VALIDATION FAILED:", exc, file=sys.stderr)
        raise SystemExit(1)
