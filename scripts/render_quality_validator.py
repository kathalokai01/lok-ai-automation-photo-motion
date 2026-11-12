#!/usr/bin/env python3
"""Technical ffprobe validation for every final captioned Part video."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Direct execution (python scripts/render_quality_validator.py) places only
# the scripts directory on sys.path. Add the repository root so the
# namespace-package import "scripts.input_config" works in GitHub Actions.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "output"
INPUT = ROOT / "Input" / "topic.txt"
RENDER = OUT / "scene_renders" / "render_manifest.json"
REPORT = OUT / "quality" / "render_quality_report.json"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    from scripts.input_config import (
        load_and_validate,
        get_format,
        get_fps,
        get_selected_parts,
    )

    cfg = load_and_validate(INPUT)
    mode = get_format(cfg)
    fps = float(get_fps(cfg))
    selected = int(get_selected_parts(cfg, required=True))
    expected = (720, 1280) if mode == "short" else (1280, 720)

    if not RENDER.is_file():
        raise RuntimeError("Render manifest is missing")

    doc = json.loads(RENDER.read_text(encoding="utf-8"))
    parts = doc.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Render manifest has no Part outputs")
    if len(parts) != selected:
        raise RuntimeError(
            f"Render Part count {len(parts)} does not match selected PARTS {selected}"
        )

    part_numbers = []
    rows = []
    for row in parts:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid render Part record")

        part_number = int(row.get("part", 0))
        if part_number < 1:
            raise RuntimeError("Render Part number is missing or invalid")
        part_numbers.append(part_number)

        raw = row.get("path") or row.get("video_path") or row.get("output_path")
        if not raw:
            raise RuntimeError("Render Part has no video path")

        path = Path(raw)
        path = path if path.is_absolute() else ROOT / path
        try:
            path.resolve().relative_to(ROOT.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"Rendered video escapes repository: {path}"
            ) from exc

        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size < 1000
        ):
            raise RuntimeError(f"Missing/unsafe rendered video: {path}")

        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"ffprobe failed for {path}: {result.stderr[-500:]}"
            )

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next(
            (item for item in streams if item.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (item for item in streams if item.get("codec_type") == "audio"),
            None,
        )
        if not video or not audio:
            raise RuntimeError(f"Video/audio stream missing: {path}")

        dimensions = (
            int(video.get("width", 0)),
            int(video.get("height", 0)),
        )
        if dimensions != expected:
            raise RuntimeError(
                f"Wrong {mode} dimensions for {path}: {dimensions}"
            )

        rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
        actual_fps = float(Fraction(rate))
        if not math.isfinite(actual_fps) or abs(actual_fps - fps) > 0.05:
            raise RuntimeError(f"FPS mismatch for {path}: {actual_fps}")

        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(f"Invalid duration for {path}")

        rows.append(
            {
                "part": row.get("part"),
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha(path),
                "bytes": path.stat().st_size,
                "dimensions": list(dimensions),
                "fps": actual_fps,
                "duration": round(duration, 4),
                "audio_stream": True,
            }
        )

    if (
        sorted(part_numbers) != list(range(1, selected + 1))
        or len(set(part_numbers)) != selected
    ):
        raise RuntimeError(
            "Render manifest does not cover each selected Part exactly once"
        )

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "completed",
        "version": "render-quality-v2-import-path-fix",
        "format": mode,
        "expected_dimensions": list(expected),
        "expected_fps": fps,
        "part_count": len(rows),
        "parts": rows,
    }
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("RENDER QUALITY VALIDATION PASSED:", len(rows), "parts")


# _PHOTO_MOTION_EVENT_WRAPPED: preserve stage events and factual outcomes.
_photo_motion_original_main = main


def main():
    import time as _pm_time

    _pm_started = _pm_time.monotonic()
    try:
        try:
            from production_events import record_event as _pm_record
        except ImportError:
            from scripts.production_events import record_event as _pm_record
        _pm_record("render_quality_validator", "running")
    except Exception:
        _pm_record = None

    try:
        _pm_result = _photo_motion_original_main()
        if _pm_record:
            _pm_record(
                "render_quality_validator",
                "completed" if _pm_result in (None, 0) else "failed",
                metadata={"return_code": _pm_result},
                started_at=_pm_started,
            )
        return _pm_result
    except BaseException as _pm_exc:
        if _pm_record:
            _pm_record(
                "render_quality_validator",
                "failed",
                error=f"{type(_pm_exc).__name__}: {_pm_exc}",
                started_at=_pm_started,
            )
        raise


if __name__ == "__main__":
    main()
