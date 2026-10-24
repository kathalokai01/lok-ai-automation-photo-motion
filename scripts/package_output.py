#!/usr/bin/env python3
"""Validate render manifests and create a selected-format, duplicate-safe ZIP."""

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

VERSION = "output-packager-v11-manifest-safe-selected-format"
VIDEO_SIZE = (1280, 720)
MIN_BYTES = 1000

REQUIRED_PATHS = set()


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_output_path(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(OUT.resolve()):
        raise RuntimeError(f"{label}: path escapes output/: {path}")
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
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
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
    video = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )

    if duration <= 0 or video is None or audio is None:
        raise RuntimeError(
            f"{path.name}: requires valid duration, video and audio"
        )

    size = (int(video.get("width") or 0), int(video.get("height") or 0))
    if size != VIDEO_SIZE:
        raise RuntimeError(
            f"{path.name}: expected {VIDEO_SIZE}, got {size}"
        )

    return duration


def relative(path):
    return Path(path).resolve().relative_to(OUT.resolve()).as_posix()


def archive_name(path):
    # Preserve the existing ZIP layout contract: all entries under output/.
    return "output/" + relative(path)


def validate_render(mode, parts, scenes):
    data = read_json(RENDER)

    if data.get("status") != "completed":
        raise RuntimeError("Render manifest status is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Render manifest FORMAT mismatch")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    expected_count = parts * scenes
    rows = data.get("completed_scenes")
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} completed scene rows; "
            f"found {len(rows) if isinstance(rows, list) else 'invalid data'}"
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
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

        if (part, local, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(f"Scene mapping/order mismatch at row {index}")
        if row.get("status") != "completed":
            raise RuntimeError(f"Scene {index} is not completed")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} lacks confirmed burned captions")

        path = safe_output_path(
            row.get("video"), f"Captioned scene {index}", MIN_BYTES
        )
        duration = media_info(path)
        scene_files.append(path)
        scene_records.append({
            "part": part,
            "scene": local,
            "global_scene": global_scene,
            "path": relative(path),
            "duration": round(duration, 3),
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
        duration = media_info(path)
        part_files.append(path)
        part_records.append({
            "part": index,
            "path": relative(path),
            "duration": round(duration, 3),
            "sha256": sha256(path),
        })

    final_video = safe_output_path(
        data.get("final_video"), "Final video", MIN_BYTES
    )
    alias = safe_output_path(data.get("alias"), "Selected-format alias", MIN_BYTES)
    final_duration = media_info(final_video)
    alias_duration = media_info(alias)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(f"Expected format alias {expected_alias}; got {alias.name}")
    if final_video.resolve() == alias.resolve():
        raise RuntimeError("Final video and format alias must be separate files")
    if abs(final_duration - alias_duration) > 1:
        raise RuntimeError("Final video and format alias durations differ")

    return {
        "manifest": RENDER,
        "scene_files": scene_files,
        "scene_records": scene_records,
        "part_files": part_files,
        "part_records": part_records,
        "final_video": final_video,
        "alias": alias,
        "final_duration": round(final_duration, 3),
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
                    "Thumbnail must be a 1280x720 JPEG; "
                    f"got {image.format} {image.size}"
                )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc
    return path


def validate_metadata_and_captions():
    metadata = safe_output_path(
        "output/youtube_metadata.txt", "YouTube metadata", 10
    )
    captions = safe_output_path(
        "output/captions/final_captions.srt", "Final SRT captions", 10
    )
    if "-->" not in captions.read_text(encoding="utf-8-sig"):
        raise RuntimeError("Final SRT has no caption timing cues")
    return metadata, captions


def validate_validation_report(mode, parts, scenes):
    data = read_json(VALIDATION)

    if data.get("status") != "completed":
        raise RuntimeError("Final validation has not passed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Validation report FORMAT mismatch")
    if int(data.get("parts", -1)) != parts:
        raise RuntimeError("Validation report PARTS mismatch")
    if int(data.get("scenes_per_part", -1)) != scenes:
        raise RuntimeError("Validation report SCENES mismatch")
    if int(data.get("expected_scenes", -1)) != parts * scenes:
        raise RuntimeError("Validation report expected scene count mismatch")

    checks = data.get("checks", {})
    for name in (
        "photo_motion", "narration", "captions", "sound_design",
        "render", "thumbnail", "metadata",
    ):
        if not isinstance(checks.get(name), dict):
            raise RuntimeError(f"Validation check missing: {name}")
        if checks[name].get("status") != "valid":
            raise RuntimeError(f"Validation check failed: {name}")

    return data


def curated_files(render, thumbnail, metadata, captions):
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

    support_dirs = {
        "photo_motion", "narration", "sound_design", "captions",
        "thumbnail", "images", "depth_maps",
    }
    excluded_dirs = {".git", "__pycache__", ".pytest_cache", "captioned"}
    excluded_suffixes = {".zip", ".tmp", ".lock", ".pyc"}
    video_suffixes = {".mp4", ".mov", ".mkv", ".webm"}
    allowed_top_level = {
        ".json", ".txt", ".srt", ".vtt", ".jpg", ".jpeg", ".png"
    }

    chosen = set(required)

    for path in OUT.rglob("*"):
        if not path.is_file():
            continue

        resolved = path.resolve()
        if not resolved.is_relative_to(OUT.resolve()):
            raise RuntimeError(f"Unsafe output path: {path}")
        if resolved in required or path.name == REPORT.name:
            continue
        if path.suffix.lower() in excluded_suffixes | video_suffixes:
            continue
        if path.name.endswith(".concat.txt"):
            continue
        if any(part in excluded_dirs for part in path.relative_to(OUT).parts):
            continue

        relative_parts = path.relative_to(OUT).parts
        if relative_parts and relative_parts[0] in support_dirs:
            chosen.add(resolved)
        elif len(relative_parts) == 1 and path.suffix.lower() in allowed_top_level:
            chosen.add(resolved)

    files = sorted(chosen, key=lambda item: item.as_posix())
    names = [archive_name(path) for path in files]

    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate archive paths detected before ZIP creation")

    for path in files:
        if not path.is_file() or not path.resolve().is_relative_to(OUT.resolve()):
            raise RuntimeError(f"Missing or unsafe package file: {path}")

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
                name = archive_name(path)
                digest = sha256(path)

                # Do not omit manifest-referenced files, even if hashes match.
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
    if not OUT.is_dir():
        raise RuntimeError("output/ directory is missing")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    validation = validate_validation_report(mode, parts, scenes)
    render = validate_render(mode, parts, scenes)
    thumbnail = validate_thumbnail()
    metadata, captions = validate_metadata_and_captions()

    opposite_alias = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    # Only remove the known stale top-level format alias. Do not delete
    # arbitrary files or any file referenced by the current render manifest.
    stale_candidates = (OUT / "videos" / opposite_alias, OUT / opposite_alias)
    referenced = {
        render["final_video"].resolve(),
        render["alias"].resolve(),
        *(p.resolve() for p in render["scene_files"]),
        *(p.resolve() for p in render["part_files"]),
    }

    for stale in stale_candidates:
        if stale.is_file() and stale.resolve() not in referenced:
            stale.unlink()
            log("Removed stale opposite-format alias:", stale.relative_to(ROOT))

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

    files = curated_files(render, thumbnail, metadata, captions)
    entries = create_archive(files)

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        names = set(archive.namelist())

        for path in REQUIRED_PATHS:
            name = archive_name(path)
            if name not in names:
                raise RuntimeError(f"Required manifest file missing from ZIP: {name}")

        for name in names:
            lower = name.lower()
            if lower.endswith(".zip"):
                raise RuntimeError(f"Nested ZIP is forbidden: {name}")
            basename = Path(name).name.lower()
            if mode == "short" and basename == "katha_lok_ai_full.mp4":
                raise RuntimeError("Full-format video found in short ZIP")
            if mode == "full" and basename == "katha_lok_ai_short.mp4":
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
        "scene_count": len(render["scene_records"]),
        "part_count": len(render["part_records"]),
        "final_video": relative(render["final_video"]),
        "format_alias": relative(render["alias"]),
        "thumbnail": relative(thumbnail),
        "metadata": relative(metadata),
        "captions": relative(captions),
        "validation_report": relative(VALIDATION),
        "render_manifest": relative(RENDER),
        "scene_files": render["scene_records"],
        "part_files": render["part_records"],
        "files": entries,
        "zip_integrity": "valid",
        "nested_zip_count": 0,
        "duplicate_archive_paths": 0,
        "validation_checks": validation.get("checks", {}),
    }
    write_json(REPORT, report)

    # Verify the report itself is written after ZIP creation and that the
    # artifact ZIP remains valid. The report is available in output/ for upload.
    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Final ZIP failed integrity verification")

    log("PACKAGE STATUS: completed")
    log("FORMAT:", mode)
    log("ZIP:", ARCHIVE.name)
    log("ZIP BYTES:", ARCHIVE.stat().st_size)
    log("PACKAGED FILES:", len(entries))
    log("PACKAGE MANIFEST:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"PACKAGE ERROR: {exc}", file=sys.stderr, flush=True)
        raise