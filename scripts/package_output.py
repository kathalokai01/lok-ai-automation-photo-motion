#!/usr/bin/env python3
"""Selected-format, duplicate-safe Katha Lok AI ZIP packager."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
ARCHIVE = ROOT / "katha-lok-ai-photo-motion-output.zip"
REPORT = OUT / "package_manifest.json"
VALIDATION = OUT / "validation_report.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"

sys.path.insert(0, str(ROOT / "scripts"))
from input_config import load_and_validate, get_format, get_parts, get_scenes

VERSION = "output-packager-v10-selected-format-no-stale-videos"
VIDEO_SIZE = (1280, 720)
MIN_BYTES = 1000


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return data


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_output_path(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(OUT.resolve()):
        raise RuntimeError(f"{label}: path must remain inside output/: {path}")
    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: missing or too small: {path}")
    return path


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to validate video files")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-700:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media information: {path.name}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if duration <= 0 or video is None or audio is None:
        raise RuntimeError(f"{path.name}: requires valid duration, video and audio")

    dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )
    if dimensions != VIDEO_SIZE:
        raise RuntimeError(
            f"{path.name}: expected {VIDEO_SIZE}, got {dimensions}"
        )
    return duration


def relative(path):
    return Path(path).resolve().relative_to(OUT.resolve()).as_posix()


def zip_name(path):
    return "output/" + relative(path)


def validate_render(mode, parts, scenes):
    data = read_json(RENDER)

    if data.get("status") != "completed":
        raise RuntimeError("Render manifest status is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Render manifest FORMAT differs from Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned-in captions")

    expected_count = parts * scenes
    rows = data.get("completed_scenes")
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} completed scene rows in render manifest"
        )
    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Render manifest completed_count mismatch")

    scene_files = []
    scene_records = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render scene row {index}")

        expected_part = (index - 1) // scenes + 1
        expected_local = (index - 1) % scenes + 1
        try:
            part = int(row.get("part", 0))
            local = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Invalid scene mapping row {index}") from exc

        if (part, local, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(f"Scene mapping/order mismatch at row {index}")
        if row.get("status") != "completed":
            raise RuntimeError(f"Scene {index} is not completed")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} has no confirmed burned captions")

        path = safe_output_path(row.get("video"), f"Scene {index}", MIN_BYTES)
        media_info(path)
        scene_files.append(path)
        scene_records.append({
            "part": part,
            "scene": local,
            "global_scene": global_scene,
            "path": relative(path),
            "sha256": sha256(path),
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_files = []
    part_records = []
    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            if int(row.get("part", index)) != index:
                raise RuntimeError(f"Part video order mismatch at row {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part {index} is not completed")
            value = row.get("path") or row.get("video") or row.get("output")
        else:
            value = row

        path = safe_output_path(value, f"Part video {index}", MIN_BYTES)
        media_info(path)
        part_files.append(path)
        part_records.append({
            "part": index,
            "path": relative(path),
            "sha256": sha256(path),
        })

    final_video = safe_output_path(data.get("final_video"), "Final video", MIN_BYTES)
    alias = safe_output_path(data.get("alias"), "Format alias", MIN_BYTES)
    media_info(final_video)
    media_info(alias)

    expected_alias = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(f"Expected {expected_alias}, got {alias.name}")
    if final_video.resolve() == alias.resolve():
        raise RuntimeError("Final video and format alias must be separate files")

    if abs(media_info(final_video) - media_info(alias)) > 1:
        raise RuntimeError("Final video and alias durations differ")

    opposite_alias = (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    for candidate in (OUT / "videos" / opposite_alias, OUT / opposite_alias):
        if candidate.exists():
            raise RuntimeError(
                f"Stale opposite-format video exists: {candidate.relative_to(ROOT)}. "
                "Clean the old format outputs and rerun."
            )

    return {
        "manifest": RENDER,
        "scene_files": scene_files,
        "scene_records": scene_records,
        "part_files": part_files,
        "part_records": part_records,
        "final_video": final_video,
        "alias": alias,
    }


def validate_thumbnail():
    path = safe_output_path("output/thumbnail.jpg", "Thumbnail", MIN_BYTES)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format != "JPEG" or image.size != VIDEO_SIZE:
                raise RuntimeError(
                    f"Thumbnail must be 1280x720 JPEG; got {image.format} {image.size}"
                )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc
    return path


def validate_metadata_and_captions():
    metadata = safe_output_path("output/youtube_metadata.txt", "Metadata", 10)
    captions = safe_output_path(
        "output/captions/final_captions.srt", "Final SRT captions", 10
    )
    text = captions.read_text(encoding="utf-8-sig")
    if "-->" not in text:
        raise RuntimeError("Final SRT has no caption timing cues")
    return metadata, captions


def curated_files(mode, render, thumbnail, metadata, captions):
    """Include required deliverables and support files, but not stale MP4s/ZIPs."""
    required = {
        render["manifest"].resolve(),
        render["final_video"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        captions.resolve(),
        VALIDATION.resolve(),
        *(p.resolve() for p in render["scene_files"]),
        *(p.resolve() for p in render["part_files"]),
    }

    allowed_support_dirs = {
        "photo_motion", "narration", "sound_design", "captions",
        "thumbnail", "images", "depth_maps",
    }
    excluded_dirs = {
        ".git", "__pycache__", ".pytest_cache", "captioned",
    }
    excluded_suffixes = {".zip", ".tmp", ".lock", ".pyc"}
    chosen = set(required)

    for path in OUT.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(OUT.resolve()):
            raise RuntimeError(f"Unsafe output path: {path}")
        if resolved in required:
            continue
        if path.name == REPORT.name:
            continue
        if path.suffix.lower() in excluded_suffixes:
            continue
        if path.name.endswith(".concat.txt"):
            continue
        if any(part in excluded_dirs for part in path.parts):
            continue

        # Only manifest-listed scene/part/final/alias videos are packaged.
        # This prevents old runs and duplicate MP4s from leaking into the ZIP.
        if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
            continue

        # Keep only known support areas and top-level metadata/config reports.
        rel_parts = path.relative_to(OUT).parts
        if rel_parts and rel_parts[0] in allowed_support_dirs:
            chosen.add(resolved)
        elif len(rel_parts) == 1 and path.suffix.lower() in {
            ".json", ".txt", ".srt", ".vtt", ".jpg", ".jpeg", ".png",
        }:
            chosen.add(resolved)

    files = []
    for path in sorted(chosen, key=lambda p: p.as_posix()):
        if not path.is_file():
            raise RuntimeError(f"Required package file is missing: {path}")
        if not path.resolve().is_relative_to(OUT.resolve()):
            raise RuntimeError(f"Package path escapes output/: {path}")
        files.append(path.resolve())

    names = [zip_name(path) for path in files]
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate archive paths detected before ZIP creation")
    return files


def create_archive(files):
    temp = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temp.unlink(missing_ok=True)
    records = []
    seen_hashes = {}

    try:
        with zipfile.ZipFile(
            temp, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=6, allowZip64=True,
        ) as archive:
            for path in files:
                name = zip_name(path)
                digest = sha256(path)

                # Preserve every required manifest-referenced path.
                # Deduplicate identical non-required support files only.
                if digest in seen_hashes and path.resolve() not in REQUIRED_PATHS:
                    continue

                archive.write(path, arcname=name)
                records.append({
                    "path": name,
                    "bytes": path.stat().st_size,
                    "sha256": digest,
                })
                seen_hashes.setdefault(digest, name)

        with zipfile.ZipFile(temp, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"ZIP CRC validation failed: {bad}")
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError("Duplicate ZIP entries detected")
            for item in records:
                payload = archive.read(item["path"])
                if len(payload) != item["bytes"]:
                    raise RuntimeError(f"ZIP size mismatch: {item['path']}")
                if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                    raise RuntimeError(f"ZIP hash mismatch: {item['path']}")

        os.replace(temp, ARCHIVE)
    finally:
        temp.unlink(missing_ok=True)

    return records


def main():
    global REQUIRED_PATHS

    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not OUT.is_dir():
        raise RuntimeError("output/ directory is missing")

    validation = read_json(VALIDATION)
    if validation.get("status") != "completed":
        raise RuntimeError("Final validation has not passed")
    if str(validation.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Validation report FORMAT mismatch")
    if int(validation.get("parts", -1)) != parts:
        raise RuntimeError("Validation report PARTS mismatch")
    if int(validation.get("scenes_per_part", -1)) != scenes:
        raise RuntimeError("Validation report SCENES mismatch")
    if int(validation.get("expected_scenes", -1)) != parts * scenes:
        raise RuntimeError("Validation report expected scene count mismatch")

    checks = validation.get("checks", {})
    for name in (
        "photo_motion", "narration", "captions", "sound_design",
        "render", "thumbnail", "metadata",
    ):
        if not isinstance(checks.get(name), dict):
            raise RuntimeError(f"Validation check missing: {name}")
        if checks[name].get("status") != "valid":
            raise RuntimeError(f"Validation check failed: {name}")

    render = validate_render(mode, parts, scenes)
    thumbnail = validate_thumbnail()
    metadata, captions = validate_metadata_and_captions()

    REQUIRED_PATHS = {
        render["manifest"].resolve(),
        render["final_video"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        captions.resolve(),
        VALIDATION.resolve(),
        *(p.resolve() for p in render["scene_files"]),
        *(p.resolve() for p in render["part_files"]),
    }

    files = curated_files(mode, render, thumbnail, metadata, captions)
    entries = create_archive(files)

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        names = set(archive.namelist())
        for path in REQUIRED_PATHS:
            name = zip_name(path)
            if name not in names:
                raise RuntimeError(f"Required manifest file missing from ZIP: {name}")

        # Ensure the archive has no nested ZIPs or unselected-format alias.
        for name in names:
            if name.lower().endswith(".zip"):
                raise RuntimeError(f"Nested ZIP is forbidden: {name}")
            if mode == "short" and Path(name).name == "katha_lok_ai_full.mp4":
                raise RuntimeError("Full-format video found in short ZIP")
            if mode == "full" and Path(name).name == "katha_lok_ai_short.mp4":
                raise RuntimeError("Short-format video found in full ZIP")

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256(ARCHIVE),
        "file_count": len(entries),
        "files": entries,
        "render_manifest_reconciliation": {
            "status": "valid",
            "scene_videos_verified": len(render["scene_files"]),
            "part_videos_verified": len(render["part_files"]),
            "required_paths_verified": len(REQUIRED_PATHS),
        },
        "external_api_calls_during_packaging": 0,
    }
    atomic_json(REPORT, report)

    log("PACKAGING: PASSED")
    log("FORMAT:", mode)
    log("ZIP:", ARCHIVE.name)
    log("FILES:", len(entries))
    log("SCENE VIDEOS:", len(render["scene_files"]))
    log("PART VIDEOS:", len(render["part_files"]))
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGING: FAILED:", str(exc))
        sys.exit(1)