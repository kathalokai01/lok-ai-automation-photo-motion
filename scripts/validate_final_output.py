
#!/usr/bin/env python3
"""Strict final-output validation for the photo-motion video pipeline."""

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

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUTPUT = ROOT / "output"
PHOTO_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"
CAPTION_MANIFEST = OUTPUT / "captions" / "captions.json"
RENDER_MANIFEST = OUTPUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUTPUT / "thumbnail" / "thumbnail_manifest.json"
FINAL_SRT = OUTPUT / "captions" / "final_captions.srt"
METADATA_DIR = OUTPUT / "metadata"
REPORT_PATH = OUTPUT / "validation_report.json"

MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
DURATION_TOLERANCE = 0.75
VERSION = "final-validator-v5"


def log(*args):
    print(*args, flush=True)


def read_json(path, required=True):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if required:
            raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_file(value, label, minimum_bytes=1, required=True):
    if not isinstance(value, str) or not value.strip():
        if required:
            raise RuntimeError(f"{label}: path missing")
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        if required:
            raise RuntimeError(f"{label}: file missing: {path}")
        return None
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file too small: {path}")

    return path


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def rows_from(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def probe_media(path, require_audio=False, require_video=False):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-800:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    streams = data.get("streams", [])
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")
    if require_video and video is None:
        raise RuntimeError(f"No video stream: {path.name}")
    if require_audio and audio is None:
        raise RuntimeError(f"No audio stream: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else None,
        "height": int(video.get("height") or 0) if video else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def check_scene_mapping(rows, parts, scenes, label):
    expected_count = parts * scenes
    if len(rows) != expected_count:
        raise RuntimeError(
            f"{label}: expected {expected_count} rows, found {len(rows)}"
        )

    seen = set()

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: row {index} is not an object")

        try:
            part = int(row["part"])
            local_scene = int(
                row.get("local_scene", row.get("scene", 0))
            )
            global_scene = int(
                row.get("global_scene", row.get("scene", index))
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError(
                f"{label}: invalid scene mapping at row {index}"
            ) from exc

        expected_part = ((index - 1) // scenes) + 1
        expected_local = ((index - 1) % scenes) + 1

        if (
            part != expected_part
            or local_scene != expected_local
            or global_scene != index
        ):
            raise RuntimeError(
                f"{label}: non-contiguous mapping at row {index}: "
                f"part={part}, scene={local_scene}, global={global_scene}"
            )

        key = (part, local_scene)
        if key in seen:
            raise RuntimeError(f"{label}: duplicate mapping {key}")
        seen.add(key)

    return expected_count


def validate_photo_manifest(parts, scenes, expected_count):
    data = read_json(PHOTO_MANIFEST)
    rows = rows_from(data, "scenes", "videos", "completed_scenes", "items")

    check_scene_mapping(rows, parts, scenes, "Photo-motion manifest")

    for index, row in enumerate(rows, start=1):
        value = next(
            (
                row.get(key)
                for key in ("video", "output", "video_path")
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        path = safe_file(value, f"Photo-motion scene {index}", MIN_VIDEO_BYTES)
        info = probe_media(path, require_video=True)

        if not info["has_video"]:
            raise RuntimeError(f"Photo-motion scene {index} has no video")

    return {
        "status": "valid",
        "scenes": expected_count,
    }


def validate_audio_manifest(parts, scenes, expected_count):
    data = read_json(AUDIO_MANIFEST)
    rows = rows_from(data, "jobs", "audio_jobs")
    mapping = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        if key in mapping:
            raise RuntimeError(f"Audio manifest: duplicate scene {key}")

        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Audio manifest: incomplete job {key}")

        value = next(
            (
                row.get(field)
                for field in ("output", "audio_path", "path", "file", "audio")
                if isinstance(row.get(field), str) and row[field].strip()
            ),
            None,
        )
        path = safe_file(value, f"Audio scene {key}", MIN_AUDIO_BYTES)
        info = probe_media(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Audio scene {key} contains no audio stream")

        mapping[key] = {
            "path": relative(path),
            "duration": info["duration"],
            "sha256": sha256_file(path),
        }

    expected_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    if set(mapping) != expected_keys:
        raise RuntimeError(
            "Audio manifest scene mapping mismatch: "
            f"missing={sorted(expected_keys - set(mapping))}, "
            f"extra={sorted(set(mapping) - expected_keys)}"
        )

    return {
        "status": "valid",
        "scenes": len(mapping),
        "total_audio_seconds": round(
            sum(item["duration"] for item in mapping.values()), 3
        ),
    }


def validate_caption_manifest(parts, scenes, expected_count):
    data = read_json(CAPTION_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    rows = data.get("captions")
    if not isinstance(rows, list):
        raise RuntimeError("Caption manifest has no captions list")

    mapping = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        if key in mapping:
            raise RuntimeError(f"Caption manifest: duplicate mapping {key}")

        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Caption text is empty for {key}")

        mapping[key] = text.strip()

    expected_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    if set(mapping) != expected_keys:
        raise RuntimeError(
            "Caption scene mapping mismatch: "
            f"missing={sorted(expected_keys - set(mapping))}, "
            f"extra={sorted(set(mapping) - expected_keys)}"
        )

    srt = safe_file(str(FINAL_SRT.relative_to(ROOT)), "Final SRT", 10)
    srt_text = srt.read_text(encoding="utf-8-sig").strip()

    if "-->" not in srt_text:
        raise RuntimeError("Final SRT has no valid cue timestamps")

    cue_count = srt_text.count(" --> ")
    if cue_count != expected_count:
        raise RuntimeError(
            f"Final SRT cue count mismatch: expected {expected_count}, "
            f"found {cue_count}"
        )

    return {
        "status": "valid",
        "scenes": len(mapping),
        "srt": relative(srt),
        "srt_cues": cue_count,
    }


def validate_render_manifest(mode, parts, scenes, expected_count):
    data = read_json(RENDER_MANIFEST)

    if not isinstance(data, dict):
        raise RuntimeError("Render manifest must be a JSON object")
    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if data.get("format") != mode:
        raise RuntimeError("Render manifest format does not match Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Final render manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list):
        raise RuntimeError("Render manifest has no completed_scenes list")

    check_scene_mapping(rows, parts, scenes, "Final render manifest")

    resolution = (1280, 720) if mode == "full" else (720, 1280)

    for index, row in enumerate(rows, start=1):
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} does not confirm burned captions")

        source_value = row.get("source_rendered_video")
        output_value = row.get("video")

        source = safe_file(
            source_value, f"Original rendered scene {index}", MIN_VIDEO_BYTES
        )
        output = safe_file(
            output_value, f"Captioned scene {index}", MIN_VIDEO_BYTES
        )

        if source.resolve() == output.resolve():
            raise RuntimeError(
                f"Scene {index}: source and captioned paths are identical"
            )

        info = probe_media(output, require_audio=True, require_video=True)

        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Captioned scene {index} resolution mismatch")

        if abs(info["duration"] - float(row.get("duration", 0))) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Captioned scene {index} duration differs from manifest"
            )

    if data.get("completed_count") != expected_count:
        raise RuntimeError("Render manifest completed_count mismatch")

    parts_list = data.get("parts")
    if not isinstance(parts_list, list) or len(parts_list) != parts:
        raise RuntimeError("Render manifest part list count mismatch")

    checked_parts = []
    for index, value in enumerate(parts_list, start=1):
        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = probe_media(path, require_audio=True, require_video=True)

        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Part video {index} resolution mismatch")

        checked_parts.append({
            "path": relative(path),
            "duration": info["duration"],
            "bytes": path.stat().st_size,
        })

    final_value = data.get("final_video")
    alias_value = data.get("alias")

    final_path = safe_file(final_value, "Final video", MIN_VIDEO_BYTES)
    alias_path = safe_file(alias_value, "Format alias", MIN_VIDEO_BYTES)

    final_info = probe_media(final_path, require_audio=True, require_video=True)
    alias_info = probe_media(alias_path, require_audio=True, require_video=True)

    for label, info in (("Final video", final_info), ("Alias video", alias_info)):
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"{label} resolution mismatch")

    if abs(final_info["duration"] - alias_info["duration"]) > DURATION_TOLERANCE:
        raise RuntimeError("Final video and format alias durations differ")

    if abs(final_info["duration"] - float(data.get("duration", 0))) > DURATION_TOLERANCE:
        raise RuntimeError("Final video duration differs from render manifest")

    return {
        "status": "valid",
        "scenes": expected_count,
        "parts": checked_parts,
        "final_video": relative(final_path),
        "final_duration": final_info["duration"],
        "final_bytes": final_path.stat().st_size,
        "alias": relative(alias_path),
        "resolution": list(resolution),
    }


def validate_thumbnail():
    path = safe_file(
        str(THUMBNAIL.relative_to(ROOT)),
        "Thumbnail",
        MIN_IMAGE_BYTES,
    )

    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
    except Exception as exc:
        raise RuntimeError(f"Thumbnail image is invalid: {exc}") from exc

    data = read_json(THUMBNAIL_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Thumbnail manifest is not completed")
    if data.get("format") not in ("short", "full"):
        raise RuntimeError("Thumbnail manifest format is invalid")

    expected_size = (
        (1080, 1920) if data["format"] == "short" else (1280, 720)
    )
    if (width, height) != expected_size:
        raise RuntimeError(
            f"Thumbnail size mismatch: expected {expected_size}, "
            f"found {(width, height)}"
        )

    if not str(data.get("title", "")).strip():
        raise RuntimeError("Thumbnail title is empty")

    return {
        "status": "valid",
        "path": relative(path),
        "width": width,
        "height": height,
        "bytes": path.stat().st_size,
    }


def validate_metadata():
    if not METADATA_DIR.exists():
        raise RuntimeError("Metadata directory is missing")

    files = sorted(
        path for path in METADATA_DIR.rglob("*.txt")
        if path.is_file()
    )

    if not files:
        raise RuntimeError("No .txt metadata files found")

    metadata = {}

    for path in files:
        safe_file(str(path.relative_to(ROOT)), "Metadata file", 1)
        content = path.read_text(encoding="utf-8-sig").strip()

        if not content:
            raise RuntimeError(f"Metadata file is empty: {relative(path)}")

        metadata[relative(path)] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    return {
        "status": "valid",
        "files": metadata,
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required for final validation")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    expected_count = parts * scenes

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers")

    log("VALIDATING FINAL OUTPUT")
    log("FORMAT:", mode)
    log("EXPECTED SCENES:", expected_count)

    report = {
        "status": "in_progress",
        "validator": VERSION,
        "format": mode,
        "expected_scenes": expected_count,
        "checks": {},
        "errors": [],
    }

    checks = [
        (
            "photo_motion",
            lambda: validate_photo_manifest(parts, scenes, expected_count),
        ),
        (
            "audio",
            lambda: validate_audio_manifest(parts, scenes, expected_count),
        ),
        (
            "captions",
            lambda: validate_caption_manifest(parts, scenes, expected_count),
        ),
        (
            "render",
            lambda: validate_render_manifest(mode, parts, scenes, expected_count),
        ),
        ("thumbnail", validate_thumbnail),
        ("metadata", validate_metadata),
    ]

    for name, validator in checks:
        try:
            report["checks"][name] = validator()
            log("PASS:", name)
        except Exception as exc:
            report["errors"].append({
                "check": name,
                "error": str(exc),
            })
            log("FAIL:", name, str(exc))

    report["status"] = "completed" if not report["errors"] else "failed"
    report["passed_checks"] = len(report["checks"])
    report["failed_checks"] = len(report["errors"])

    write_json(REPORT_PATH, report)

    log("=" * 58)
    log("VALIDATION STATUS:", report["status"].upper())
    log("PASSED:", report["passed_checks"])
    log("FAILED:", report["failed_checks"])
    log("REPORT:", relative(REPORT_PATH))
    log("=" * 58)

    if report["errors"]:
        for error in report["errors"]:
            log(f"- {error['check']}: {error['error']}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("VALIDATION PIPELINE FAILED:", str(exc))
        sys.exit(1)
