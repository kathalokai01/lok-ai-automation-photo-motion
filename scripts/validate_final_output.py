
#!/usr/bin/env python3
"""Validate final photo-motion video outputs against the active workflow."""

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

OUT = ROOT / "output"
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
CAPTION_MANIFEST = OUT / "captions" / "captions.json"
RENDER_MANIFEST = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
FINAL_SRT = OUT / "captions" / "final_captions.srt"
REPORT = OUT / "validation_report.json"
VERSION = "final-validator-v6"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
DURATION_TOLERANCE = 0.8


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is empty or too small: {path}")

    return path


def rel(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def rows_from(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def media_info(path, require_audio=False, require_video=False):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr[-600:]}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")
    if require_video and video is None:
        raise RuntimeError(f"Video stream missing: {path.name}")
    if require_audio and audio is None:
        raise RuntimeError(f"Audio stream missing: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else None,
        "height": int(video.get("height") or 0) if video else None,
        "has_audio": audio is not None,
        "has_video": video is not None,
    }


def get_scene_key(row, index):
    try:
        part = int(row["part"])
        scene = int(row.get("local_scene", row.get("scene", 0)))
        global_scene = int(row.get("global_scene", row.get("scene", index)))
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeError(f"Invalid scene mapping at row {index}") from exc
    return part, scene, global_scene


def validate_contiguous(rows, parts, scenes, label):
    expected = parts * scenes
    if len(rows) != expected:
        raise RuntimeError(f"{label}: expected {expected} rows, got {len(rows)}")

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: row {index} is not an object")

        part, scene, global_scene = get_scene_key(row, index)
        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1

        if (part, scene, global_scene) != (expected_part, expected_scene, index):
            raise RuntimeError(
                f"{label}: scene order mismatch at row {index}; "
                f"found {(part, scene, global_scene)}"
            )

    return expected


def validate_photo_manifest(parts, scenes):
    data = read_json(PHOTO_MANIFEST)
    rows = rows_from(data, "scenes", "videos", "completed_scenes", "items")
    validate_contiguous(rows, parts, scenes, "Photo-motion manifest")

    for index, row in enumerate(rows, start=1):
        value = next(
            (
                row.get(k) for k in ("video", "output", "video_path")
                if isinstance(row.get(k), str) and row[k].strip()
            ),
            None,
        )
        path = safe_file(value, f"Photo-motion scene {index}", MIN_VIDEO_BYTES)
        media_info(path, require_video=True)

    return {"status": "valid", "scene_count": len(rows)}


def validate_audio(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    rows = rows_from(data, "jobs", "audio_jobs")
    mapping = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, ValueError, TypeError):
            continue

        if key in mapping:
            raise RuntimeError(f"Duplicate audio scene: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Audio job incomplete: {key}")

        value = next(
            (
                row.get(k) for k in ("output", "audio_path", "path", "file", "audio")
                if isinstance(row.get(k), str) and row[k].strip()
            ),
            None,
        )
        path = safe_file(value, f"Audio {key}", MIN_AUDIO_BYTES)
        info = media_info(path, require_audio=True)
        mapping[key] = {"duration": info["duration"], "path": rel(path)}

    expected = {
        (p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes + 1)
    }
    if set(mapping) != expected:
        raise RuntimeError(
            f"Audio mapping mismatch: missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "narration_seconds": round(sum(x["duration"] for x in mapping.values()), 3),
    }


def validate_captions(parts, scenes):
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
            raise RuntimeError(f"Duplicate caption scene: {key}")
        if not str(row.get("text", "")).strip():
            raise RuntimeError(f"Empty caption text: {key}")
        mapping[key] = True

    expected = {
        (p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes + 1)
    }
    if set(mapping) != expected:
        raise RuntimeError("Caption scene mapping does not match PARTS × SCENES")

    srt = safe_file(rel(FINAL_SRT), "Final SRT", 10)
    srt_text = srt.read_text(encoding="utf-8-sig")
    if "-->" not in srt_text or " --> " not in srt_text:
        raise RuntimeError("SRT has no recognizable cue timestamps")

    cues = srt_text.count(" --> ")
    if cues != parts * scenes:
        raise RuntimeError(
            f"SRT cue count mismatch: expected {parts * scenes}, got {cues}"
        )

    return {"status": "valid", "scene_count": len(mapping), "srt_cues": cues}


def validate_render(mode, parts, scenes):
    data = read_json(RENDER_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Render manifest must be an object")
    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if data.get("format") != mode:
        raise RuntimeError("Render format does not match Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list):
        raise RuntimeError("Render manifest has no completed_scenes")
    expected = validate_contiguous(rows, parts, scenes, "Rendered scenes")
    resolution = (1280, 720) if mode == "full" else (720, 1280)

    for index, row in enumerate(rows, start=1):
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} is not marked captioned")

        source_value = row.get("source_rendered_video")
        output_value = row.get("video")
        source = safe_file(source_value, f"Source scene {index}", MIN_VIDEO_BYTES)
        output = safe_file(output_value, f"Captioned scene {index}", MIN_VIDEO_BYTES)

        if source.resolve() == output.resolve():
            raise RuntimeError(f"Scene {index}: source and captioned paths are identical")

        info = media_info(output, require_audio=True, require_video=True)
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Captioned scene {index} resolution mismatch")

        try:
            expected_duration = float(row["duration"])
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Scene {index}: invalid manifest duration") from exc

        if abs(info["duration"] - expected_duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Captioned scene {index} duration mismatch")

    if int(data.get("completed_count", -1)) != expected:
        raise RuntimeError("Render completed_count mismatch")

    part_values = data.get("parts")
    if not isinstance(part_values, list) or len(part_values) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_reports = []
    for index, value in enumerate(part_values, start=1):
        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = media_info(path, require_audio=True, require_video=True)
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Part {index} resolution mismatch")
        part_reports.append({
            "path": rel(path),
            "duration": round(info["duration"], 3),
            "bytes": path.stat().st_size,
        })

    final_path = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias_path = safe_file(data.get("alias"), "Format alias", MIN_VIDEO_BYTES)
    final_info = media_info(final_path, require_audio=True, require_video=True)
    alias_info = media_info(alias_path, require_audio=True, require_video=True)

    for label, info in (("Final", final_info), ("Alias", alias_info)):
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"{label} video resolution mismatch")

    if abs(final_info["duration"] - alias_info["duration"]) > DURATION_TOLERANCE:
        raise RuntimeError("Final video and alias durations differ")

    manifest_duration = float(data.get("duration", 0))
    if abs(final_info["duration"] - manifest_duration) > DURATION_TOLERANCE:
        raise RuntimeError("Final video duration does not match manifest")

    return {
        "status": "valid",
        "scene_count": expected,
        "parts": part_reports,
        "final_video": rel(final_path),
        "final_duration": round(final_info["duration"], 3),
        "final_bytes": final_path.stat().st_size,
        "alias": rel(alias_path),
        "resolution": list(resolution),
    }


def validate_thumbnail(mode):
    path = safe_file(rel(THUMBNAIL), "Thumbnail", MIN_IMAGE_BYTES)

    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
    except Exception as exc:
        raise RuntimeError(f"Thumbnail is invalid: {exc}") from exc

    manifest = read_json(THUMBNAIL_MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        raise RuntimeError("Thumbnail manifest is not completed")
    if manifest.get("format") != mode:
        raise RuntimeError("Thumbnail format does not match input format")
    if not str(manifest.get("title", "")).strip():
        raise RuntimeError("Thumbnail title is empty")

    expected_size = (1080, 1920) if mode == "short" else (1280, 720)
    if (width, height) != expected_size:
        raise RuntimeError(
            f"Thumbnail dimensions mismatch: expected {expected_size}, "
            f"got {(width, height)}"
        )

    manifest_path = manifest.get("thumbnail")
    if manifest_path:
        declared = safe_file(manifest_path, "Thumbnail manifest path", MIN_IMAGE_BYTES)
        if declared.resolve() != path.resolve():
            raise RuntimeError("Thumbnail manifest points to a different image")

    return {
        "status": "valid",
        "path": rel(path),
        "width": width,
        "height": height,
        "bytes": path.stat().st_size,
        "title": manifest["title"],
    }


def validate_metadata():
    # The current workflow writes metadata here, not into output/metadata/.
    path = safe_file(rel(METADATA), "YouTube metadata", 20)
    content = path.read_text(encoding="utf-8-sig").strip()

    required_labels = ("Title:", "Description:", "Tags:")
    missing = [label for label in required_labels if label not in content]
    if missing:
        raise RuntimeError(f"Metadata labels missing: {missing}")

    sections = content.splitlines()
    if not any(line.strip() for line in sections[1:]):
        raise RuntimeError("Metadata content is empty")

    title = content.split("Description:", 1)[0].replace("Title:", "").strip()
    description = content.split("Description:", 1)[1].split("Tags:", 1)[0].strip()
    tags = content.split("Tags:", 1)[1].strip()

    if not title:
        raise RuntimeError("Metadata title is empty")
    if not description:
        raise RuntimeError("Metadata description is empty")
    if not tags:
        raise RuntimeError("Metadata tags are empty")

    return {
        "status": "valid",
        "path": rel(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "title_characters": len(title),
        "description_characters": len(description),
        "tag_characters": len(tags),
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers")

    report = {
        "status": "in_progress",
        "validator": VERSION,
        "format": mode,
        "expected_scenes": parts * scenes,
        "checks": {},
        "errors": [],
    }

    checks = [
        ("photo_motion", lambda: validate_photo_manifest(parts, scenes)),
        ("audio", lambda: validate_audio(parts, scenes)),
        ("captions", lambda: validate_captions(parts, scenes)),
        ("render", lambda: validate_render(mode, parts, scenes)),
        ("thumbnail", lambda: validate_thumbnail(mode)),
        ("metadata", validate_metadata),
    ]

    log("FINAL OUTPUT VALIDATION")
    log(f"FORMAT={mode} PARTS={parts} SCENES_PER_PART={scenes}")

    for name, callback in checks:
        try:
            report["checks"][name] = callback()
            log("PASS:", name)
        except Exception as exc:
            report["errors"].append({"check": name, "error": str(exc)})
            log("FAIL:", name, str(exc))

    report["passed_checks"] = len(report["checks"])
    report["failed_checks"] = len(report["errors"])
    report["status"] = "completed" if not report["errors"] else "failed"
    write_json(REPORT, report)

    log("STATUS:", report["status"].upper())
    log("PASSED:", report["passed_checks"])
    log("FAILED:", report["failed_checks"])
    log("REPORT:", rel(REPORT))

    if report["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("VALIDATION PIPELINE FAILED:", str(exc))
        sys.exit(1)
