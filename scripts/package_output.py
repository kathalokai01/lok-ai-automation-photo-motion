
#!/usr/bin/env python3
"""Validate and package the selected Katha Lok AI video format."""

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
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"

sys.path.insert(0, str(ROOT / "scripts"))
from input_config import load_and_validate, get_format, get_parts, get_scenes

VERSION = "output-packager-v15-format-aware-zip-manifest-consistency"
# Video dimensions are set from FORMAT in main(); thumbnails remain landscape.
VIDEO_SIZE = (1280, 720)
THUMBNAIL_SIZE = (1280, 720)
MIN_BYTES = 1000

REQUIRED_PATHS: set[Path] = set()


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return data


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def safe_output_path(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: manifest path is missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(OUT.resolve()):
        raise RuntimeError(f"{label}: path escapes output/: {path}")

    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: missing or too small: {path}")

    return path


def relative(path):
    return Path(path).resolve().relative_to(OUT.resolve()).as_posix()


def archive_name(path):
    return "output/" + relative(path)


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required")

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
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )

    if not math_is_positive_finite(duration) or video is None or audio is None:
        raise RuntimeError(
            f"{path.name}: positive duration, video and audio are required"
        )

    dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )
    if dimensions != VIDEO_SIZE:
        raise RuntimeError(
            f"{path.name}: expected {VIDEO_SIZE} for selected FORMAT, "
            f"got {dimensions}"
        )

    return duration


def math_is_positive_finite(value):
    import math
    return math.isfinite(value) and value > 0


def validate_render(mode, parts, scenes):
    data = read_json(RENDER)

    if data.get("status") != "completed":
        raise RuntimeError("Render manifest status is not completed")

    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Render manifest FORMAT does not match Input/topic.txt")

    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    expected_count = parts * scenes
    rows = data.get("completed_scenes")

    if not isinstance(rows, list) or len(rows) != expected_count:
        actual = len(rows) if isinstance(rows, list) else "invalid"
        raise RuntimeError(
            f"Expected {expected_count} completed scenes; found {actual}"
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
            local_scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

        if (part, local_scene, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(f"Scene order/mapping mismatch at row {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Scene {index} is not completed")

        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} has no confirmed burned captions")

        path = safe_output_path(
            row.get("video"), f"Captioned scene {index}", MIN_BYTES
        )
        duration = media_info(path)
        actual_hash = sha256(path)
        declared_hash = row.get("sha256") or row.get("video_sha256")

        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Scene {index} SHA-256 mismatch")

        scene_files.append(path)
        scene_records.append({
            "part": part,
            "scene": local_scene,
            "global_scene": global_scene,
            "path": relative(path),
            "duration": round(duration, 3),
            "bytes": path.stat().st_size,
            "sha256": actual_hash,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_files = []
    part_records = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                actual_part = int(row.get("part", index))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid part number at row {index}") from exc

            if actual_part != index:
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
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })

    final_video = safe_output_path(
        data.get("final_video"), "Final video", MIN_BYTES
    )
    alias = safe_output_path(
        data.get("alias"), "Selected-format alias", MIN_BYTES
    )

    final_duration = media_info(final_video)
    alias_duration = media_info(alias)
    final_hash = sha256(final_video)
    alias_hash = sha256(alias)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    if alias.name != expected_alias:
        raise RuntimeError(
            f"Expected selected-format alias {expected_alias}; got {alias.name}"
        )

    if final_video.resolve() == alias.resolve():
        raise RuntimeError("Final video and alias must be separate files")

    if abs(final_duration - alias_duration) > 1:
        raise RuntimeError("Final video and alias durations differ")

    same_content = final_hash == alias_hash

    return {
        "manifest": RENDER,
        "manifest_data": data,
        "scene_files": scene_files,
        "scene_records": scene_records,
        "part_files": part_files,
        "part_records": part_records,
        "final_video": final_video,
        "alias": alias,
        "final_duration": round(final_duration, 3),
        "final_video_sha256": final_hash,
        "alias_sha256": alias_hash,
        "final_video_same_content_as_alias": same_content,
        "final_video_archive_path": (
            archive_name(alias) if same_content else archive_name(final_video)
        ),
    }


def validate_thumbnail():
    path = safe_output_path("output/thumbnail.jpg", "Thumbnail", MIN_BYTES)

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            image_format = image.format
            dimensions = image.size
    except Exception as exc:
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc

    if image_format != "JPEG" or dimensions != THUMBNAIL_SIZE:
        raise RuntimeError(
            f"Thumbnail must be 1280x720 JPEG; got {image_format} {dimensions}"
        )

    return path


def validate_metadata_and_captions():
    metadata = safe_output_path(
        "output/youtube_metadata.txt", "YouTube metadata", 10
    )
    captions = safe_output_path(
        "output/captions/final_captions.srt", "Final SRT captions", 10
    )

    content = captions.read_text(encoding="utf-8-sig")
    if "-->" not in content:
        raise RuntimeError("Final SRT has no timing cues")

    if not content.strip():
        raise RuntimeError("Final SRT is empty")

    return metadata, captions


def validate_validation_report(mode, parts, scenes):
    data = read_json(VALIDATION)

    if data.get("status") != "completed":
        raise RuntimeError("Final validation report is not completed")

    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Validation report FORMAT mismatch")

    expected_fields = {
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
    }

    for key, expected in expected_fields.items():
        try:
            actual = int(data.get(key, -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid validation field: {key}") from exc

        if actual != expected:
            raise RuntimeError(
                f"Validation report {key} mismatch: expected {expected}, got {actual}"
            )

    checks = data.get("checks")
    if not isinstance(checks, dict):
        raise RuntimeError("Validation report checks object is missing")

    for name in (
        "photo_motion",
        "narration",
        "captions",
        "sound_design",
        "render",
        "thumbnail",
        "metadata",
    ):
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            raise RuntimeError(f"Validation check missing or failed: {name}")

    return data


def validate_optional_metadata_json(mode):
    """Include the structured metadata file when it is valid and available."""
    if not METADATA_JSON.is_file():
        log("Optional structured metadata JSON not found; using text metadata.")
        return None

    data = read_json(METADATA_JSON)

    declared_mode = str(data.get("format", data.get("mode", mode))).strip().lower()
    if declared_mode not in ("", mode):
        raise RuntimeError(
            f"YouTube metadata JSON FORMAT mismatch: {declared_mode} != {mode}"
        )

    title = data.get("title")
    description = data.get("description")
    tags = data.get("tags")

    if not isinstance(title, str) or not title.strip():
        raise RuntimeError("YouTube metadata JSON has no title")

    if not isinstance(description, str) or not description.strip():
        raise RuntimeError("YouTube metadata JSON has no description")

    if not isinstance(tags, (str, list)) or not tags:
        raise RuntimeError("YouTube metadata JSON has no tags")

    return METADATA_JSON


def curated_files(render, thumbnail, metadata, captions, metadata_json):
    required = {
        render["manifest"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        captions.resolve(),
        VALIDATION.resolve(),
        *(path.resolve() for path in render["scene_files"]),
        *(path.resolve() for path in render["part_files"]),
    }

    if not render["final_video_same_content_as_alias"]:
        required.add(render["final_video"].resolve())

    if metadata_json is not None:
        required.add(metadata_json.resolve())

    chosen = set(required)

    # Keep useful non-video supporting outputs. Do not indiscriminately package
    # every video from previous runs or temporary rendering files.
    support_dirs = {
        "story",
        "scenes",
        "visuals",
        "photo_motion",
        "narration",
        "sound_design",
        "captions",
        "thumbnail",
        "images",
        "depth_maps",
        "publish",
    }

    excluded_dirs = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        "captioned",
    }

    excluded_suffixes = {".zip", ".tmp", ".lock", ".pyc"}
    video_suffixes = {".mp4", ".mov", ".mkv", ".webm"}
    allowed_top_level = {
        ".json", ".txt", ".srt", ".vtt", ".jpg", ".jpeg", ".png"
    }

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

        relative_parts = path.relative_to(OUT).parts

        if any(part in excluded_dirs for part in relative_parts):
            continue

        if relative_parts and relative_parts[0] in support_dirs:
            chosen.add(resolved)
        elif (
            len(relative_parts) == 1
            and path.suffix.lower() in allowed_top_level
        ):
            chosen.add(resolved)

    files = sorted(chosen, key=lambda path: path.as_posix())
    names = [archive_name(path) for path in files]

    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate archive paths detected before ZIP creation")

    for path in files:
        if (
            not path.is_file()
            or not path.resolve().is_relative_to(OUT.resolve())
        ):
            raise RuntimeError(f"Missing or unsafe package file: {path}")

    return files


def packaged_render_manifest(render, packaged_names):
    """
    Build a ZIP-only manifest that references packaged files.

    Source/provenance files are omitted from this ZIP copy when they are not
    included in the package. The original on-disk manifest is not modified.
    """
    data = json.loads(json.dumps(render["manifest_data"]))

    final_path = archive_name(render["final_video"])
    alias_path = archive_name(render["alias"])

    if render["final_video_same_content_as_alias"]:
        data["final_video"] = alias_path
        data["alias"] = alias_path
        data["package_video_deduplication"] = {
            "status": "deduplicated",
            "original_final_video": final_path,
            "kept_path": alias_path,
            "reason": "identical_sha256",
        }
    else:
        data["final_video"] = final_path
        data["alias"] = alias_path
        data["package_video_deduplication"] = {"status": "not_needed"}

    for row in data.get("completed_scenes", []):
        if not isinstance(row, dict):
            continue

        video_value = row.get("video")
        if isinstance(video_value, str):
            path = Path(video_value)
            if not path.is_absolute():
                path = (ROOT / path).resolve()
            else:
                path = path.resolve()

            if path.is_relative_to(OUT.resolve()):
                packaged_path = archive_name(path)
                if packaged_path not in packaged_names:
                    raise RuntimeError(
                        "Render manifest references a scene video missing "
                        f"from ZIP: {packaged_path}"
                    )
                row["video"] = packaged_path

        for key in (
            "source_video",
            "narration",
            "source_rendered_video",
            "source_rendered_video_sha256",
            "background_tracks",
        ):
            row.pop(key, None)

    for row in data.get("parts", []):
        if isinstance(row, dict):
            value = row.get("path") or row.get("video") or row.get("output")
            if isinstance(value, str):
                path = Path(value)
                if not path.is_absolute():
                    path = (ROOT / path).resolve()
                else:
                    path = path.resolve()

                if path.is_relative_to(OUT.resolve()):
                    packaged_path = archive_name(path)
                    if packaged_path not in packaged_names:
                        raise RuntimeError(
                            f"Render manifest references missing part video: {packaged_path}"
                        )
                    row["path"] = packaged_path
                    row["video"] = packaged_path

    for key in ("caption_manifest", "final_srt"):
        value = data.get(key)
        if not isinstance(value, str):
            continue

        path = Path(value)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        else:
            path = path.resolve()

        if path.is_relative_to(OUT.resolve()):
            packaged_path = archive_name(path)
            if packaged_path in packaged_names:
                data[key] = packaged_path
            else:
                data.pop(key, None)

    data["package_manifest_view"] = True
    data["package_manifest_version"] = VERSION
    return data


def create_archive(files, render):
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)

    records = []
    seen_hashes = {}
    file_names = {archive_name(path) for path in files}

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in files:
                name = archive_name(path)

                if name == archive_name(render["manifest"]):
                    payload = (
                        json.dumps(
                            packaged_render_manifest(render, file_names),
                            ensure_ascii=False,
                            indent=2,
                        ) + "\n"
                    ).encode("utf-8")
                else:
                    payload = path.read_bytes()

                digest = digest_bytes(payload)

                # Never drop a required file merely because its bytes match
                # another file. Optional support files may be deduplicated.
                if digest in seen_hashes and path.resolve() not in REQUIRED_PATHS:
                    log("Skipped duplicate-content support asset:", relative(path))
                    continue

                archive.writestr(name, payload)
                records.append({
                    "path": name,
                    "bytes": len(payload),
                    "sha256": digest,
                })
                seen_hashes.setdefault(digest, name)

        with zipfile.ZipFile(temporary, "r") as archive:
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

                if digest_bytes(payload) != item["sha256"]:
                    raise RuntimeError(f"ZIP SHA-256 mismatch: {item['path']}")

        os.replace(temporary, ARCHIVE)
    finally:
        temporary.unlink(missing_ok=True)

    return records


def main():
    global REQUIRED_PATHS, VIDEO_SIZE

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

    # Shorts are portrait 9:16; full videos are landscape 16:9.
    VIDEO_SIZE = (720, 1280) if mode == "short" else (1280, 720)

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    log("PACKAGE VERSION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)
    log("EXPECTED VIDEO DIMENSIONS:", VIDEO_SIZE)

    validation = validate_validation_report(mode, parts, scenes)
    render = validate_render(mode, parts, scenes)
    thumbnail = validate_thumbnail()
    metadata, captions = validate_metadata_and_captions()
    metadata_json = validate_optional_metadata_json(mode)

    opposite_alias = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    referenced = {
        render["final_video"].resolve(),
        render["alias"].resolve(),
        *(path.resolve() for path in render["scene_files"]),
        *(path.resolve() for path in render["part_files"]),
    }

    # Remove only the known stale aliases; never delete manifest-referenced files.
    for stale in (OUT / "videos" / opposite_alias, OUT / opposite_alias):
        if stale.is_file() and stale.resolve() not in referenced:
            stale.unlink()
            log("Removed stale opposite-format alias:", relative(stale))

    REQUIRED_PATHS = {
        render["manifest"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        captions.resolve(),
        VALIDATION.resolve(),
        *(path.resolve() for path in render["scene_files"]),
        *(path.resolve() for path in render["part_files"]),
    }

    if metadata_json is not None:
        REQUIRED_PATHS.add(metadata_json.resolve())

    if not render["final_video_same_content_as_alias"]:
        REQUIRED_PATHS.add(render["final_video"].resolve())

    files = curated_files(render, thumbnail, metadata, captions, metadata_json)
    entries = create_archive(files, render)

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        names = set(archive.namelist())

        for path in REQUIRED_PATHS:
            name = archive_name(path)
            if name not in names:
                raise RuntimeError(f"Required file missing from ZIP: {name}")

        expected_alias_name = archive_name(render["alias"])
        if expected_alias_name not in names:
            raise RuntimeError("Selected-format alias is missing from ZIP")

        if render["final_video_same_content_as_alias"]:
            if archive_name(render["final_video"]) in names:
                raise RuntimeError(
                    "Duplicate final-video copy unexpectedly remains in ZIP"
                )

        for name in names:
            lower = name.lower()
            if lower.endswith(".zip"):
                raise RuntimeError(f"Nested ZIP is forbidden: {name}")

            basename = Path(name).name.lower()
            if mode == "short" and basename == "katha_lok_ai_full.mp4":
                raise RuntimeError("Full-format video found in short ZIP")
            if mode == "full" and basename == "katha_lok_ai_short.mp4":
                raise RuntimeError("Short-format video found in full ZIP")

        if archive.testzip() is not None:
            raise RuntimeError("Final ZIP failed integrity verification")

        archived_render_name = archive_name(render["manifest"])
        archived_render = json.loads(
            archive.read(archived_render_name).decode("utf-8")
        )

        for key in ("final_video", "alias"):
            value = archived_render.get(key)
            if not isinstance(value, str) or value not in names:
                raise RuntimeError(
                    f"Packaged render manifest has missing {key}: {value}"
                )

        for row in archived_render.get("completed_scenes", []):
            if isinstance(row, dict):
                value = row.get("video")
                if value and value not in names:
                    raise RuntimeError(
                        f"Packaged render manifest scene path is missing: {value}"
                    )

        for row in archived_render.get("parts", []):
            if isinstance(row, dict):
                value = row.get("path") or row.get("video") or row.get("output")
                if value and value not in names:
                    raise RuntimeError(
                        f"Packaged render manifest part path is missing: {value}"
                    )

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "video_dimensions": list(VIDEO_SIZE),
        "thumbnail_dimensions": list(THUMBNAIL_SIZE),
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256(ARCHIVE),
        "file_count": len(entries),
        "scene_count": len(render["scene_records"]),
        "part_count": len(render["part_records"]),
        "final_video": relative(render["final_video"]),
        "format_alias": relative(render["alias"]),
        "final_video_sha256": render["final_video_sha256"],
        "format_alias_sha256": render["alias_sha256"],
        "final_video_same_content_as_alias": (
            render["final_video_same_content_as_alias"]
        ),
        "final_video_archive_path": render["final_video_archive_path"],
        "final_video_deduplication": (
            {
                "status": "deduplicated",
                "omitted_path": archive_name(render["final_video"]),
                "kept_path": archive_name(render["alias"]),
                "reason": "identical_sha256",
                "archived_manifest_remapped": True,
            }
            if render["final_video_same_content_as_alias"]
            else {"status": "not_needed"}
        ),
        "thumbnail": relative(thumbnail),
        "metadata": relative(metadata),
        "metadata_json": (
            relative(metadata_json) if metadata_json is not None else None
        ),
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
        "manifest_zip_consistency": "valid",
    }

    write_json(REPORT, report)

    log("PACKAGE STATUS: completed")
    log("FORMAT:", mode)
    log("ZIP:", ARCHIVE.name)
    log("ZIP BYTES:", ARCHIVE.stat().st_size)
    log("PACKAGED FILES:", len(entries))
    log("FINAL VIDEO DEDUPLICATED:", render["final_video_same_content_as_alias"])
    log("ZIP/MANIFEST CONSISTENCY: valid")
    log("PACKAGE MANIFEST:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"PACKAGE ERROR: {exc}", file=sys.stderr, flush=True)
        raise
