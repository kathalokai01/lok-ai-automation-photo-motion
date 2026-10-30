#!/usr/bin/env python3
"""Build and verify a portable Katha Lok AI output package."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from fractions import Fraction
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
ARCHIVE = ROOT / "katha-lok-ai-photo-motion-output.zip"
REPORT = OUT / "package_manifest.json"
VALIDATION = OUT / "validation_report.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"

sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_fps,
)

VERSION = "output-packager-v21-fps-and-zip-video-validation"
MIN_BYTES = 1000
FPS_TOLERANCE = 0.02

PATH_FIELDS = {
    "video", "output", "path", "final_video", "alias",
    "pre_caption_video", "caption_manifest", "final_srt",
    "source_video", "source_photo_motion_video",
    "source_rendered_video", "source_audio", "narration",
    "audio", "image", "image_path", "depth_map",
    "depth_map_path", "thumbnail", "thumbnail_path",
    "source_photo_manifest", "source_audio_manifest",
    "file", "audio_path",
}

SUPPORT_DIRS = {
    "story", "scenes", "visuals", "photo_motion",
    "narration", "sound_design", "captions",
    "thumbnail", "images", "depth_maps", "publish",
}

EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".zip", ".tmp", ".lock", ".pyc"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
TOP_LEVEL_SUFFIXES = {
    ".json", ".txt", ".srt", ".vtt", ".jpg", ".jpeg", ".png",
}


def log(*values):
    print(*values, flush=True)


def fail(message):
    raise RuntimeError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bytes_sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")

    if not isinstance(data, dict):
        fail(f"Expected JSON object: {path}")

    return data


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inside_output(path):
    path = Path(path).resolve()

    try:
        path.relative_to(OUT.resolve())
    except ValueError:
        fail(f"Path escapes output/: {path}")

    return path


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: path missing")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = inside_output(path)

    if not path.is_file():
        fail(f"{label}: file not found: {path}")

    if path.stat().st_size < minimum:
        fail(f"{label}: file too small: {path.name}")

    return path


def relative_output(path):
    return inside_output(path).relative_to(OUT.resolve()).as_posix()


def zip_path(path):
    return "output/" + relative_output(path)


def parse_rate(value, label):
    try:
        rate = Fraction(str(value))
        result = float(rate)
    except (ValueError, ZeroDivisionError, TypeError):
        fail(f"{label}: invalid video frame rate {value!r}")

    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid video frame rate {value!r}")

    return result


def probe(path, expected_size=None, expected_fps=None):
    executable = shutil.which("ffprobe")
    if not executable:
        fail("ffprobe is required")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries",
            (
                "format=duration:"
                "stream=codec_type,width,height,"
                "avg_frame_rate,r_frame_rate"
            ),
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode:
        fail(f"ffprobe failed for {Path(path).name}: {result.stderr[-500:]}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError):
        fail(f"Invalid media information: {Path(path).name}")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {Path(path).name}")

    streams = data.get("streams", [])
    video = next(
        (row for row in streams if row.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (row for row in streams if row.get("codec_type") == "audio"),
        None,
    )

    if video is None or audio is None:
        fail(f"Audio/video stream missing: {Path(path).name}")

    dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )

    if expected_size and dimensions != expected_size:
        fail(
            f"{Path(path).name}: expected dimensions {expected_size}, "
            f"got {dimensions}"
        )

    rate_value = video.get("avg_frame_rate")
    if not rate_value or rate_value in ("0/0", "N/A"):
        rate_value = video.get("r_frame_rate")

    actual_fps = parse_rate(rate_value, Path(path).name)

    if expected_fps is not None:
        if abs(actual_fps - expected_fps) > FPS_TOLERANCE:
            fail(
                f"FPS mismatch in {Path(path).name}: "
                f"expected {expected_fps}, got {actual_fps:.4f}"
            )

    return {
        "duration": duration,
        "width": dimensions[0],
        "height": dimensions[1],
        "fps": actual_fps,
    }


def validate_validation_report(mode, parts, scenes):
    data = read_json(VALIDATION)

    if data.get("status") != "completed":
        fail("Final validation report is not completed")

    if str(data.get("format", "")).strip().lower() != mode:
        fail("Validation report FORMAT mismatch")

    expected = {
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
    }

    for key, value in expected.items():
        try:
            actual = int(data.get(key, -1))
        except (TypeError, ValueError):
            fail(f"Invalid validation field: {key}")

        if actual != value:
            fail(f"Validation report {key} mismatch")

    checks = data.get("checks")
    if not isinstance(checks, dict):
        fail("Validation checks missing")

    required = (
        "photo_motion", "narration", "captions",
        "sound_design", "render", "thumbnail", "metadata",
    )

    for name in required:
        check = checks.get(name)
        if not isinstance(check, dict):
            fail(f"Validation check missing: {name}")

        status = check.get("status")
        if status is not None and status != "valid":
            fail(f"Validation check failed: {name}")

    return data


def validate_render(mode, parts, scenes, expected_size, expected_fps):
    data = read_json(RENDER)

    if data.get("status") != "completed":
        fail("Render manifest is not completed")

    if str(data.get("format", "")).strip().lower() != mode:
        fail("Render manifest FORMAT mismatch")

    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions")

    try:
        manifest_fps = float(data.get("fps"))
    except (TypeError, ValueError):
        fail("Render manifest FPS missing or invalid")

    if abs(manifest_fps - expected_fps) > FPS_TOLERANCE:
        fail(
            f"Render manifest FPS mismatch: expected {expected_fps}, "
            f"got {manifest_fps}"
        )

    expected_count = parts * scenes

    try:
        completed_count = int(data.get("completed_count", -1))
    except (TypeError, ValueError):
        completed_count = -1

    if completed_count != expected_count:
        fail("Render completed_count mismatch")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list) or len(rows) != expected_count:
        fail("Render scene count mismatch")

    scene_files = []
    scene_records = []

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render scene row: {index}")

        expected_part = (index - 1) // scenes + 1
        expected_local = (index - 1) % scenes + 1

        try:
            part = int(row.get("part", 0))
            local = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (TypeError, ValueError):
            fail(f"Invalid render scene mapping: {index}")

        if (part, local, global_scene) != (
            expected_part, expected_local, index
        ):
            fail(f"Render scene order mismatch: {index}")

        if row.get("status") != "completed":
            fail(f"Render scene incomplete: {index}")

        if row.get("captions_burned") is not True:
            fail(f"Scene captions not confirmed: {index}")

        path = safe_file(
            row.get("video") or row.get("output"),
            f"Captioned scene {index}",
            MIN_BYTES,
        )

        info = probe(path, expected_size, expected_fps)
        actual_hash = sha256(path)
        declared_hash = row.get("sha256") or row.get("video_sha256")

        if declared_hash and declared_hash != actual_hash:
            fail(f"Scene hash mismatch: {index}")

        scene_files.append(path)
        scene_records.append({
            "part": part,
            "scene": local,
            "global_scene": global_scene,
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": actual_hash,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail("Part video count mismatch")

    part_files = []
    part_records = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                part_number = int(row.get("part", index))
            except (TypeError, ValueError):
                fail(f"Invalid part row: {index}")

            if part_number != index:
                fail(f"Part order mismatch: {index}")

            if row.get("status") not in (None, "completed"):
                fail(f"Part incomplete: {index}")

            value = row.get("path") or row.get("video") or row.get("output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", MIN_BYTES)
        info = probe(path, expected_size, expected_fps)
        part_files.append(path)

        part_records.append({
            "part": index,
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })

    final_video = safe_file(data.get("final_video"), "Final video", MIN_BYTES)
    alias = safe_file(data.get("alias"), "Format alias", MIN_BYTES)

    final_info = probe(final_video, expected_size, expected_fps)
    alias_info = probe(alias, expected_size, expected_fps)
    final_duration = final_info["duration"]
    alias_duration = alias_info["duration"]
    final_hash = sha256(final_video)
    alias_hash = sha256(alias)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    if alias.name != expected_alias:
        fail(f"Expected format alias {expected_alias}")

    if final_video.resolve() == alias.resolve():
        fail("Final video and alias must be separate files")

    if final_hash != alias_hash:
        fail("Final video and alias content hashes differ")

    if abs(final_duration - alias_duration) > 1:
        fail("Final video and alias duration mismatch")

    opposite = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    for stale in (OUT / "videos" / opposite, OUT / opposite):
        if stale.exists():
            fail(f"Opposite-format output exists: {stale}")

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
        "same_content": final_hash == alias_hash,
        "fps": expected_fps,
    }


def validate_thumbnail():
    path = safe_file("output/thumbnail.jpg", "Thumbnail", MIN_BYTES)

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            image_format = image.format
            dimensions = image.size

    except Exception as exc:
        fail(f"Invalid thumbnail: {exc}")

    if image_format != "JPEG" or dimensions != (1280, 720):
        fail(
            f"Thumbnail must be 1280x720 JPEG; "
            f"got {image_format} {dimensions}"
        )

    return path


def validate_metadata_captions():
    metadata = safe_file(
        "output/youtube_metadata.txt", "Metadata", 10
    )
    captions = safe_file(
        "output/captions/final_captions.srt", "Final SRT", 10
    )

    content = captions.read_text(encoding="utf-8-sig")
    if "-->" not in content:
        fail("Final SRT has no timing cues")

    return metadata, captions


def optional_metadata_json(mode):
    path = OUT / "publish" / "youtube_metadata.json"
    if not path.is_file():
        return None

    data = read_json(path)
    declared_mode = str(
        data.get("format", data.get("mode", mode))
    ).strip().lower()

    if declared_mode != mode:
        fail("Structured metadata FORMAT mismatch")

    for field in ("title", "description"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            fail(f"Structured metadata {field} missing")

    tags = data.get("tags")
    if not isinstance(tags, (str, list)) or not tags:
        fail("Structured metadata tags missing")

    return path


def packaged_path(value, packaged_names):
    if not isinstance(value, str) or not value.strip():
        return None

    value = value.strip()

    if value in packaged_names:
        return value

    if value.startswith(("http://", "https://")):
        return value

    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path

    try:
        resolved = inside_output(path)
    except RuntimeError:
        return None

    candidate = "output/" + resolved.relative_to(
        OUT.resolve()
    ).as_posix()

    return candidate if candidate in packaged_names else None


def remap_manifest_paths(value, packaged_names, context="manifest"):
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in PATH_FIELDS and isinstance(item, str):
                mapped = packaged_path(item, packaged_names)

                if mapped is None:
                    if item.startswith(("http://", "https://")):
                        continue
                    if item.startswith("output/"):
                        fail(f"{context}: packaged path missing: {item}")
                    continue

                value[key] = mapped

            elif isinstance(item, (dict, list)):
                remap_manifest_paths(item, packaged_names, context)

    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                remap_manifest_paths(item, packaged_names, context)


def make_portable_render_manifest(render, packaged_names):
    data = json.loads(json.dumps(render["data"]))

    final_name = packaged_path(
        relative_output(render["final_video"]), packaged_names
    )
    alias_name = packaged_path(
        relative_output(render["alias"]), packaged_names
    )

    if not final_name or not alias_name:
        fail("Final video or format alias missing from ZIP")

    data["final_video"] = final_name
    data["alias"] = alias_name

    if render["same_content"]:
        data["package_video_deduplication"] = {
            "status": "same_content",
            "sha256": render["final_hash"],
        }

    rows = data.get("completed_scenes")
    if not isinstance(rows, list):
        fail("Render manifest completed_scenes missing")

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid packaged scene row: {index}")

        original = row.get("video") or row.get("output")
        mapped = packaged_path(original, packaged_names)

        if not mapped:
            fail(f"Captioned scene missing from ZIP: {original}")

        row["video"] = mapped
        row.pop("output", None)

    part_rows = data.get("parts")
    if not isinstance(part_rows, list):
        fail("Render manifest parts missing")

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            original = (
                row.get("path") or row.get("video") or row.get("output")
            )
            mapped = packaged_path(original, packaged_names)

            if not mapped:
                fail(f"Part video missing from ZIP: {original}")

            row["path"] = mapped
            row.pop("video", None)
            row.pop("output", None)

        elif isinstance(row, str):
            mapped = packaged_path(row, packaged_names)
            if not mapped:
                fail(f"Part video missing from ZIP: {row}")
            part_rows[index - 1] = mapped

        else:
            fail(f"Invalid part row: {index}")

    remap_manifest_paths(data, packaged_names, "render manifest")
    data["final_video"] = final_name
    data["alias"] = alias_name
    data["package_manifest_view"] = True
    data["package_manifest_version"] = VERSION

    return data


def collect_files(render, thumbnail, metadata, captions, metadata_json):
    required = {
        RENDER.resolve(),
        render["final_video"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        captions.resolve(),
        VALIDATION.resolve(),
        *(path.resolve() for path in render["scene_files"]),
        *(path.resolve() for path in render["part_files"]),
    }

    if metadata_json:
        required.add(metadata_json.resolve())

    selected = set(required)

    for path in OUT.rglob("*"):
        if not path.is_file():
            continue

        path = path.resolve()

        if not path.is_relative_to(OUT.resolve()):
            fail(f"Unsafe output file: {path}")

        if path in required or path.name == REPORT.name:
            continue

        pieces = path.relative_to(OUT.resolve()).parts

        if any(piece in EXCLUDED_DIRS for piece in pieces):
            continue

        if path.suffix.lower() in EXCLUDED_SUFFIXES | VIDEO_SUFFIXES:
            continue

        if pieces[0] in SUPPORT_DIRS:
            selected.add(path)
        elif (
            len(pieces) == 1
            and path.suffix.lower() in TOP_LEVEL_SUFFIXES
        ):
            selected.add(path)

    files = sorted(selected, key=lambda item: item.as_posix())
    names = [zip_path(path) for path in files]

    if len(names) != len(set(names)):
        fail("Duplicate ZIP paths in source file list")

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
                fail("ZIP CRC verification failed")

            archive_names = archive.namelist()
            if len(archive_names) != len(set(archive_names)):
                fail("Duplicate ZIP entries")

            for record in records:
                payload = archive.read(record["path"])
                if len(payload) != record["bytes"]:
                    fail(f"ZIP size mismatch: {record['path']}")
                if bytes_sha(payload) != record["sha256"]:
                    fail(f"ZIP hash mismatch: {record['path']}")

        os.replace(temporary, ARCHIVE)

    finally:
        temporary.unlink(missing_ok=True)

    return records


def verify_zip_video(archive, member, label, expected_size, expected_fps, temp_dir):
    if not isinstance(member, str) or member not in archive.namelist():
        fail(f"{label}: video path missing from ZIP: {member}")

    suffix = PurePosixPath(member).suffix or ".mp4"
    destination = Path(temp_dir) / (
        hashlib.sha256(member.encode("utf-8")).hexdigest() + suffix
    )

    try:
        with archive.open(member, "r") as source:
            with destination.open("wb") as target:
                shutil.copyfileobj(source, target)

        if destination.stat().st_size < MIN_BYTES:
            fail(f"{label}: packaged video is too small")

        info = probe(destination, expected_size, expected_fps)

        return {
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
        }

    finally:
        destination.unlink(missing_ok=True)


def verify_packaged_manifest(
    archive, mode, parts, scenes, expected_size, expected_fps
):
    names = set(archive.namelist())
    manifest_name = "output/scene_renders/render_manifest.json"

    if manifest_name not in names:
        fail("Packaged render manifest missing")

    data = json.loads(archive.read(manifest_name).decode("utf-8"))

    if data.get("status") != "completed":
        fail("Packaged render status mismatch")

    if str(data.get("format", "")).lower() != mode:
        fail("Packaged render FORMAT mismatch")

    try:
        packaged_fps = float(data.get("fps"))
    except (TypeError, ValueError):
        fail("Packaged render manifest FPS missing")

    if abs(packaged_fps - expected_fps) > FPS_TOLERANCE:
        fail("Packaged render manifest FPS mismatch")

    for key in ("final_video", "alias"):
        value = data.get(key)
        if not isinstance(value, str) or value not in names:
            fail(f"Packaged {key} does not resolve: {value}")

    expected_count = parts * scenes
    rows = data.get("completed_scenes")

    if not isinstance(rows, list) or len(rows) != expected_count:
        fail("Packaged scene count mismatch")

    with tempfile.TemporaryDirectory(prefix="lokai-zip-fps-") as temp_dir:
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                fail(f"Invalid packaged scene row: {index}")

            expected_part = (index - 1) // scenes + 1
            expected_local = (index - 1) % scenes + 1

            try:
                mapping = (
                    int(row.get("part", 0)),
                    int(row.get("local_scene", row.get("scene", 0))),
                    int(row.get("global_scene", index)),
                )
            except (TypeError, ValueError):
                fail(f"Invalid packaged scene mapping: {index}")

            if mapping != (expected_part, expected_local, index):
                fail(f"Packaged scene mapping mismatch: {index}")

            if row.get("status") != "completed":
                fail(f"Packaged scene incomplete: {index}")

            value = row.get("video")
            if not isinstance(value, str) or value not in names:
                fail(f"Packaged scene path missing: {value}")

            verify_zip_video(
                archive,
                value,
                f"Packaged scene {index}",
                expected_size,
                expected_fps,
                temp_dir,
            )

        part_rows = data.get("parts")
        if not isinstance(part_rows, list) or len(part_rows) != parts:
            fail("Packaged part count mismatch")

        for index, row in enumerate(part_rows, 1):
            if isinstance(row, dict):
                value = row.get("path") or row.get("video") or row.get("output")
                try:
                    part_number = int(row.get("part", index))
                except (TypeError, ValueError):
                    fail(f"Invalid packaged part mapping: {index}")
                if part_number != index:
                    fail(f"Packaged part order mismatch: {index}")
            else:
                value = row

            if not isinstance(value, str) or value not in names:
                fail(f"Packaged part path missing: {value}")

            verify_zip_video(
                archive,
                value,
                f"Packaged part {index}",
                expected_size,
                expected_fps,
                temp_dir,
            )

        for key in ("final_video", "alias"):
            verify_zip_video(
                archive,
                data[key],
                f"Packaged {key}",
                expected_size,
                expected_fps,
                temp_dir,
            )

    opposite = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    if any(PurePosixPath(name).name == opposite for name in names):
        fail(f"Opposite-format video included: {opposite}")

    def check_references(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in PATH_FIELDS and isinstance(item, str):
                    if item.startswith("output/") and item not in names:
                        fail(f"Dangling packaged path: {item}")
                check_references(item)

        elif isinstance(value, list):
            for item in value:
                check_references(item)

    check_references(data)


def main():
    if not shutil.which("ffprobe"):
        fail("ffprobe is required")

    if not OUT.is_dir():
        fail("output/ directory missing")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    expected_fps = float(get_fps(config))

    if mode not in ("short", "full"):
        fail(f"Unsupported FORMAT: {mode}")

    if parts < 1 or scenes < 1:
        fail("PARTS and SCENES must be positive")

    expected_size = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    log("PACKAGE VERSION:", VERSION)
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED FPS:", expected_fps)

    validation = validate_validation_report(mode, parts, scenes)
    render = validate_render(
        mode, parts, scenes, expected_size, expected_fps
    )
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
            zip_path(render["final_video"]),
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

        missing = required - names
        if missing:
            fail(f"Required ZIP files missing: {sorted(missing)}")

        if any(name.lower().endswith(".zip") for name in names):
            fail("Nested ZIP file is not allowed")

        if archive.testzip():
            fail("Final ZIP integrity check failed")

        verify_packaged_manifest(
            archive,
            mode,
            parts,
            scenes,
            expected_size,
            expected_fps,
        )

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "video_dimensions": list(expected_size),
        "expected_fps": expected_fps,
        "thumbnail_dimensions": [1280, 720],
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256(ARCHIVE),
        "file_count": len(records),
        "scene_count": len(render["scene_records"]),
        "part_count": len(render["part_records"]),
        "final_video": relative_output(render["final_video"]),
        "format_alias": relative_output(render["alias"]),
        "final_video_sha256": render["final_hash"],
        "format_alias_sha256": render["alias_hash"],
        "thumbnail": relative_output(thumbnail),
        "metadata": relative_output(metadata),
        "metadata_json": (
            relative_output(metadata_json) if metadata_json else None
        ),
        "captions": relative_output(captions),
        "validation_report": relative_output(VALIDATION),
        "render_manifest": relative_output(RENDER),
        "scene_files": render["scene_records"],
        "part_files": render["part_records"],
        "files": records,
        "zip_integrity": "valid",
        "manifest_zip_consistency": "valid",
        "zip_video_fps_validation": "valid",
        "validation_checks": validation.get("checks", {}),
    }

    write_json(REPORT, report)

    log("PACKAGE STATUS: completed")
    log("ZIP:", ARCHIVE.name)
    log("ZIP BYTES:", ARCHIVE.stat().st_size)
    log("FILES:", len(records))
    log("FPS VALIDATION: valid")
    log("ZIP VIDEO FPS VALIDATION: valid")
    log("ZIP CRC: valid")
    log("MANIFEST REFERENCES: valid")
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGE ERROR:", str(exc))
        raise