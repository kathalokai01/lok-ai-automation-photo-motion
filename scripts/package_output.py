#!/usr/bin/env python3
"""Validate and package Katha Lok AI output with portable ZIP manifests."""

from __future__ import annotations

import hashlib
import json
import math
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

VERSION = "output-packager-v18-strict-portable-manifest"
MIN_BYTES = 1000
VIDEO_SIZE = (1280, 720)

PATH_FIELDS = {
    "video", "output", "path", "final_video", "alias",
    "pre_caption_video", "caption_manifest", "final_srt",
    "source_video", "source_photo_motion_video",
    "source_rendered_video", "source_audio", "narration",
    "audio", "image", "image_path", "depth_map",
    "depth_map_path", "thumbnail", "thumbnail_path",
    "source_photo_manifest", "source_audio_manifest",
}


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bytes_sha(data):
    return hashlib.sha256(data).hexdigest()


def inside_output(path):
    path = Path(path).resolve()
    if not path.is_relative_to(OUT.resolve()):
        raise RuntimeError(f"Path escapes output/: {path}")
    return path


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = inside_output(path)

    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: missing or too small: {path}")
    return path


def rel(path):
    return inside_output(path).relative_to(OUT.resolve()).as_posix()


def zip_path(path):
    return "output/" + rel(path)


def probe(path, expected_size):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media information: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")

    streams = data.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    if video is None or audio is None:
        raise RuntimeError(f"Video/audio stream missing: {path.name}")

    dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )
    if dimensions != expected_size:
        raise RuntimeError(
            f"{path.name}: expected {expected_size}, got {dimensions}"
        )
    return duration


def validate_validation_report(mode, parts, scenes):
    data = read_json(VALIDATION)
    if data.get("status") != "completed":
        raise RuntimeError("Final validation report is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Validation report FORMAT mismatch")

    expected = {
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
    }
    for key, value in expected.items():
        try:
            actual = int(data.get(key, -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid validation field: {key}") from exc
        if actual != value:
            raise RuntimeError(f"Validation report {key} mismatch")

    checks = data.get("checks")
    if not isinstance(checks, dict):
        raise RuntimeError("Validation checks missing")

    required = (
        "photo_motion", "narration", "captions",
        "sound_design", "render", "thumbnail", "metadata",
    )
    for name in required:
        check = checks.get(name)
        if not isinstance(check, dict):
            raise RuntimeError(f"Validation check missing: {name}")
        if check.get("status") != "valid":
            raise RuntimeError(f"Validation check failed: {name}")

    return data


def validate_render(mode, parts, scenes, video_size):
    data = read_json(RENDER)
    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Render manifest FORMAT mismatch")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    expected_count = parts * scenes
    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Render completed_count mismatch")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError("Render scene count mismatch")

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
            raise RuntimeError(f"Invalid scene mapping: {index}") from exc

        if (part, local, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(f"Scene order mismatch: row {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Scene incomplete: {index}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene captions not confirmed: {index}")

        path = safe_file(row.get("video"), f"Captioned scene {index}", MIN_BYTES)
        duration = probe(path, video_size)
        actual_hash = sha256(path)
        declared_hash = row.get("sha256") or row.get("video_sha256")

        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Scene hash mismatch: {index}")

        scene_files.append(path)
        scene_records.append({
            "part": part,
            "scene": local,
            "global_scene": global_scene,
            "path": rel(path),
            "duration": round(duration, 3),
            "bytes": path.stat().st_size,
            "sha256": actual_hash,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError("Part video count mismatch")

    part_files = []
    part_records = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                part_number = int(row.get("part", index))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid part row: {index}") from exc

            if part_number != index:
                raise RuntimeError(f"Part order mismatch: {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part incomplete: {index}")

            value = row.get("path") or row.get("video") or row.get("output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", MIN_BYTES)
        duration = probe(path, video_size)
        part_files.append(path)
        part_records.append({
            "part": index,
            "path": rel(path),
            "duration": round(duration, 3),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })

    final_video = safe_file(data.get("final_video"), "Final video", MIN_BYTES)
    alias = safe_file(data.get("alias"), "Format alias", MIN_BYTES)

    final_duration = probe(final_video, video_size)
    alias_duration = probe(alias, video_size)
    final_hash = sha256(final_video)
    alias_hash = sha256(alias)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(f"Expected alias {expected_alias}")
    if final_video.resolve() == alias.resolve():
        raise RuntimeError("Final video and alias must be separate files")
    if abs(final_duration - alias_duration) > 1:
        raise RuntimeError("Final video and alias duration mismatch")
    if final_hash != alias_hash:
        raise RuntimeError("Final video and alias content hashes differ")

    return {
        "data": data,
        "scene_files": scene_files,
        "scene_records": scene_records,
        "part_files": part_files,
        "part_records": part_records,
        "final_video": final_video,
        "alias": alias,
        "final_duration": round(final_duration, 3),
        "final_hash": final_hash,
        "alias_hash": alias_hash,
        "same_content": True,
    }


def validate_thumbnail():
    path = safe_file("output/thumbnail.jpg", "Thumbnail", MIN_BYTES)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image_format, dimensions = image.format, image.size
    except Exception as exc:
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc

    if image_format != "JPEG" or dimensions != (1280, 720):
        raise RuntimeError(
            f"Thumbnail must be 1280x720 JPEG; got {image_format} {dimensions}"
        )
    return path


def validate_metadata_captions():
    metadata = safe_file("output/youtube_metadata.txt", "Metadata", 10)
    captions = safe_file(
        "output/captions/final_captions.srt", "Final SRT", 10
    )

    content = captions.read_text(encoding="utf-8-sig")
    if "-->" not in content:
        raise RuntimeError("Final SRT has no timing cues")

    return metadata, captions


def optional_metadata_json(mode):
    path = OUT / "publish" / "youtube_metadata.json"
    if not path.is_file():
        return None

    data = read_json(path)
    declared_mode = str(data.get("format", data.get("mode", mode))).lower()
    if declared_mode not in ("", mode):
        raise RuntimeError("Structured metadata FORMAT mismatch")

    for field in ("title", "description"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise RuntimeError(f"Structured metadata {field} missing")

    tags = data.get("tags")
    if not isinstance(tags, (str, list)) or not tags:
        raise RuntimeError("Structured metadata tags missing")
    return path


def packaged_path(value, packaged_names):
    if not isinstance(value, str) or not value.strip():
        return None

    value = value.strip()
    if value.startswith("output/"):
        return value if value in packaged_names else None

    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = inside_output(path)
    except RuntimeError:
        return None

    candidate = "output/" + path.relative_to(OUT.resolve()).as_posix()
    return candidate if candidate in packaged_names else None


def clean_manifest_paths(value, packaged_names):
    if isinstance(value, dict):
        for key in list(value):
            item = value[key]

            if key in PATH_FIELDS and isinstance(item, str):
                mapped = packaged_path(item, packaged_names)
                if mapped is None:
                    value.pop(key, None)
                else:
                    value[key] = mapped
                continue

            if key in PATH_FIELDS and isinstance(item, list):
                mapped_items = []
                for entry in item:
                    if isinstance(entry, str):
                        mapped = packaged_path(entry, packaged_names)
                        if mapped:
                            mapped_items.append(mapped)
                    elif isinstance(entry, (dict, list)):
                        clean_manifest_paths(entry, packaged_names)
                        mapped_items.append(entry)
                    else:
                        mapped_items.append(entry)

                if mapped_items:
                    value[key] = mapped_items
                else:
                    value.pop(key, None)
                continue

            clean_manifest_paths(item, packaged_names)

    elif isinstance(value, list):
        for item in value:
            clean_manifest_paths(item, packaged_names)


def make_portable_render_manifest(render, packaged_names):
    data = json.loads(json.dumps(render["data"]))

    final_name = packaged_path(
        str(render["final_video"].relative_to(ROOT)), packaged_names
    )
    alias_name = packaged_path(
        str(render["alias"].relative_to(ROOT)), packaged_names
    )

    if not alias_name:
        raise RuntimeError("Format alias missing from ZIP")

    if render["same_content"]:
        data["final_video"] = alias_name
        data["alias"] = alias_name
        data["package_video_deduplication"] = {
            "status": "deduplicated",
            "kept_path": alias_name,
            "reason": "identical_sha256",
        }
    else:
        if not final_name:
            raise RuntimeError("Final video missing from ZIP")
        data["final_video"] = final_name
        data["alias"] = alias_name
        data["package_video_deduplication"] = {"status": "not_needed"}

    for row in data.get("completed_scenes", []):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid scene entry in render manifest")
        mapped = packaged_path(row.get("video"), packaged_names)
        if not mapped:
            raise RuntimeError(
                f"Captioned scene missing from ZIP: {row.get('video')}"
            )
        row["video"] = mapped
        row.pop("output", None)

    for index, row in enumerate(data.get("parts", [])):
        if isinstance(row, dict):
            original = row.get("path") or row.get("video") or row.get("output")
            mapped = packaged_path(original, packaged_names)
            if not mapped:
                raise RuntimeError(f"Part video missing from ZIP: {original}")
            row["path"] = mapped
            row.pop("video", None)
            row.pop("output", None)
        elif isinstance(row, str):
            mapped = packaged_path(row, packaged_names)
            if not mapped:
                raise RuntimeError(f"Part video missing from ZIP: {row}")
            data["parts"][index] = mapped
        else:
            raise RuntimeError(f"Invalid part entry: {index + 1}")

    clean_manifest_paths(data, packaged_names)
    data["final_video"] = alias_name if render["same_content"] else final_name
    data["alias"] = alias_name
    data["package_manifest_view"] = True
    data["package_manifest_version"] = VERSION
    return data


def collect_files(render, thumbnail, metadata, captions, metadata_json):
    required = {
        RENDER.resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        captions.resolve(),
        VALIDATION.resolve(),
        *(path.resolve() for path in render["scene_files"]),
        *(path.resolve() for path in render["part_files"]),
    }

    if not render["same_content"]:
        required.add(render["final_video"].resolve())
    if metadata_json:
        required.add(metadata_json.resolve())

    support_dirs = {
        "story", "scenes", "visuals", "photo_motion",
        "narration", "sound_design", "captions",
        "thumbnail", "images", "depth_maps", "publish",
    }
    excluded_dirs = {
        ".git", "__pycache__", ".pytest_cache",
    }
    excluded_suffixes = {".zip", ".tmp", ".lock", ".pyc"}
    video_suffixes = {".mp4", ".mov", ".mkv", ".webm"}
    top_level_suffixes = {
        ".json", ".txt", ".srt", ".vtt", ".jpg", ".jpeg", ".png",
    }

    selected = set(required)

    for path in OUT.rglob("*"):
        if not path.is_file():
            continue

        path = path.resolve()
        if not path.is_relative_to(OUT.resolve()):
            raise RuntimeError(f"Unsafe output file: {path}")

        if path in required or path.name == REPORT.name:
            continue
        if path.suffix.lower() in excluded_suffixes | video_suffixes:
            continue

        pieces = path.relative_to(OUT.resolve()).parts
        if any(piece in excluded_dirs for piece in pieces):
            continue

        if pieces[0] in support_dirs:
            selected.add(path)
        elif len(pieces) == 1 and path.suffix.lower() in top_level_suffixes:
            selected.add(path)

    files = sorted(selected, key=lambda item: item.as_posix())
    names = [zip_path(path) for path in files]

    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate ZIP paths in source file list")

    return files


def create_zip(files, render):
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)
    names = {zip_path(path) for path in files}
    records = []

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in files:
                name = zip_path(path)

                if path.resolve() == RENDER.resolve():
                    payload = (
                        json.dumps(
                            make_portable_render_manifest(render, names),
                            ensure_ascii=False,
                            indent=2,
                        ) + "\n"
                    ).encode("utf-8")
                else:
                    payload = path.read_bytes()

                archive.writestr(name, payload)
                records.append({
                    "path": name,
                    "bytes": len(payload),
                    "sha256": bytes_sha(payload),
                })

        with zipfile.ZipFile(temporary, "r") as archive:
            if archive.testzip():
                raise RuntimeError("ZIP CRC check failed")

            archive_names = archive.namelist()
            if len(archive_names) != len(set(archive_names)):
                raise RuntimeError("Duplicate ZIP entries")

            for record in records:
                payload = archive.read(record["path"])
                if len(payload) != record["bytes"]:
                    raise RuntimeError(f"ZIP size mismatch: {record['path']}")
                if bytes_sha(payload) != record["sha256"]:
                    raise RuntimeError(f"ZIP hash mismatch: {record['path']}")

        os.replace(temporary, ARCHIVE)
    finally:
        temporary.unlink(missing_ok=True)

    return records


def verify_packaged_manifest(archive, mode, parts, scenes):
    names = set(archive.namelist())
    manifest_name = "output/scene_renders/render_manifest.json"

    if manifest_name not in names:
        raise RuntimeError("Packaged render manifest missing")

    data = json.loads(archive.read(manifest_name).decode("utf-8"))
    expected_count = parts * scenes

    if data.get("status") != "completed":
        raise RuntimeError("Packaged render manifest status mismatch")
    if str(data.get("format", "")).lower() != mode:
        raise RuntimeError("Packaged render manifest FORMAT mismatch")

    for key in ("final_video", "alias"):
        value = data.get(key)
        if not isinstance(value, str) or value not in names:
            raise RuntimeError(f"Packaged {key} does not resolve: {value}")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError("Packaged scene list count mismatch")

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid packaged scene row: {index}")
        if row.get("global_scene") != index:
            raise RuntimeError(f"Packaged global scene order mismatch: {index}")
        value = row.get("video")
        if not isinstance(value, str) or value not in names:
            raise RuntimeError(f"Packaged scene path missing: {value}")

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError("Packaged part list count mismatch")

    for row in part_rows:
        value = (
            row.get("path") or row.get("video") or row.get("output")
            if isinstance(row, dict)
            else row
        )
        if not isinstance(value, str) or value not in names:
            raise RuntimeError(f"Packaged part path missing: {value}")

    opposite = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if any(Path(name).name == opposite for name in names):
        raise RuntimeError(f"Opposite-format video included: {opposite}")

    def check_references(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in PATH_FIELDS and isinstance(item, str):
                    if item.startswith("output/") and item not in names:
                        raise RuntimeError(
                            f"Dangling packaged manifest reference: {item}"
                        )
                check_references(item)
        elif isinstance(value, list):
            for item in value:
                check_references(item)

    check_references(data)


def main():
    global VIDEO_SIZE

    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")
    if not OUT.is_dir():
        raise RuntimeError("output/ directory missing")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    VIDEO_SIZE = (720, 1280) if mode == "short" else (1280, 720)

    log("PACKAGE VERSION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)

    # Remove stale opposite-format outputs before validation.
    opposite = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    for stale in (OUT / "videos" / opposite, OUT / opposite):
        if stale.is_file():
            stale.unlink()
            log("Removed stale opposite-format file:", rel(stale))

    validation = validate_validation_report(mode, parts, scenes)
    render = validate_render(mode, parts, scenes, VIDEO_SIZE)
    thumbnail = validate_thumbnail()
    metadata, captions = validate_metadata_captions()
    metadata_json = optional_metadata_json(mode)

    files = collect_files(
        render, thumbnail, metadata, captions, metadata_json
    )
    records = create_zip(files, render)

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        names = set(archive.namelist())

        required = {
            zip_path(render["alias"]),
            zip_path(thumbnail),
            zip_path(metadata),
            zip_path(captions),
            zip_path(VALIDATION),
            *(zip_path(path) for path in render["scene_files"]),
            *(zip_path(path) for path in render["part_files"]),
        }
        if metadata_json:
            required.add(zip_path(metadata_json))
        if not render["same_content"]:
            required.add(zip_path(render["final_video"]))

        missing = required - names
        if missing:
            raise RuntimeError(f"Required ZIP files missing: {sorted(missing)}")

        if render["same_content"] and zip_path(render["final_video"]) in names:
            raise RuntimeError("Duplicate final video copy remains in ZIP")

        if any(name.lower().endswith(".zip") for name in names):
            raise RuntimeError("Nested ZIP file is not allowed")
        if archive.testzip():
            raise RuntimeError("Final ZIP integrity check failed")

        verify_packaged_manifest(archive, mode, parts, scenes)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "video_dimensions": list(VIDEO_SIZE),
        "thumbnail_dimensions": [1280, 720],
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256(ARCHIVE),
        "file_count": len(records),
        "scene_count": len(render["scene_records"]),
        "part_count": len(render["part_records"]),
        "final_video": rel(render["final_video"]),
        "format_alias": rel(render["alias"]),
        "final_video_sha256": render["final_hash"],
        "format_alias_sha256": render["alias_hash"],
        "final_video_same_content_as_alias": render["same_content"],
        "final_video_archive_path": zip_path(render["alias"]),
        "thumbnail": rel(thumbnail),
        "metadata": rel(metadata),
        "metadata_json": rel(metadata_json) if metadata_json else None,
        "captions": rel(captions),
        "validation_report": rel(VALIDATION),
        "render_manifest": rel(RENDER),
        "scene_files": render["scene_records"],
        "part_files": render["part_records"],
        "files": records,
        "zip_integrity": "valid",
        "manifest_zip_consistency": "valid",
        "validation_checks": validation.get("checks", {}),
    }

    write_json(REPORT, report)

    log("PACKAGE STATUS: completed")
    log("ZIP:", ARCHIVE.name)
    log("ZIP BYTES:", ARCHIVE.stat().st_size)
    log("FILES:", len(records))
    log("MANIFEST/ZIP REFERENCES: valid")
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGE ERROR:", str(exc))
        raise