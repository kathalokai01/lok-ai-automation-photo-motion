#!/usr/bin/env python3
"""Build and verify the final Katha Lok AI Photo Motion package."""

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
from typing import Any

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

VERSION = "output-packager-v41-safe-verified-package"
MIN_VIDEO_BYTES = 1000
FPS_TOLERANCE = 0.02
PROBE_TIMEOUT = 90

EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules",
}

EXCLUDED_SUFFIXES = {
    ".zip", ".tmp", ".lock", ".pyc",
    ".part", ".download",
}

PATH_FIELDS = {
    "video", "output", "path", "final_video", "alias",
    "pre_caption_video", "caption_manifest", "final_srt",
    "source_video", "source_photo_motion_video",
    "source_rendered_video", "source_audio", "narration",
    "audio", "image", "image_path", "depth_map",
    "depth_map_path", "thumbnail", "thumbnail_path",
    "source_photo_manifest", "source_audio_manifest",
    "file", "audio_path", "source_image", "source_manifest",
    "video_path", "audio_file", "image_file",
}


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")

    if not isinstance(data, dict):
        fail(f"Expected JSON object: {path}")

    return data


def write_json(path: Path, data: dict) -> None:
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


def inside_output(path: Path) -> Path:
    resolved = Path(path).resolve()

    try:
        resolved.relative_to(OUT.resolve())
    except ValueError:
        fail(f"Path escapes output directory: {resolved}")

    return resolved


def output_path(
    value: Any,
    label: str,
    minimum: int = 1,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path missing")

    candidate = Path(value.strip())

    if not candidate.is_absolute():
        candidate = ROOT / candidate

    resolved = inside_output(candidate)

    if not resolved.is_file():
        fail(f"{label}: file not found: {resolved}")

    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small: {resolved}")

    return resolved


def relative_output(path: Path) -> str:
    return inside_output(path).relative_to(OUT.resolve()).as_posix()


def archive_name(path: Path) -> str:
    return "output/" + relative_output(path)


def parse_rate(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (ValueError, TypeError, ZeroDivisionError):
        fail(f"{label}: invalid frame rate {value!r}")

    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid frame rate {value!r}")

    return result


def probe_media(
    path: Path,
    expected_size: tuple[int, int] | None = None,
    expected_fps: float | None = None,
) -> dict:
    executable = shutil.which("ffprobe")

    if not executable:
        fail("ffprobe is required")

    try:
        result = subprocess.run(
            [
                executable,
                "-v", "error",
                "-show_entries",
                "format=duration:"
                "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        fail(f"ffprobe timed out: {path.name}")

    if result.returncode != 0:
        fail(
            f"ffprobe failed for {path.name}: "
            f"{result.stderr[-500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media information for {path.name}: {exc}")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {path.name}")

    streams = data.get("streams", [])

    if not isinstance(streams, list):
        fail(f"Invalid media streams: {path.name}")

    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    if video is None or audio is None:
        fail(f"Audio or video stream missing: {path.name}")

    dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )

    if expected_size and dimensions != expected_size:
        fail(
            f"{path.name}: expected {expected_size}, "
            f"got {dimensions}"
        )

    rate_value = video.get("avg_frame_rate")

    if not rate_value or rate_value in {"0/0", "N/A"}:
        rate_value = video.get("r_frame_rate")

    actual_fps = parse_rate(rate_value, path.name)

    if expected_fps is not None:
        if abs(actual_fps - expected_fps) > FPS_TOLERANCE:
            fail(
                f"FPS mismatch in {path.name}: "
                f"expected {expected_fps}, got {actual_fps:.4f}"
            )

    return {
        "duration": duration,
        "width": dimensions[0],
        "height": dimensions[1],
        "fps": actual_fps,
    }


def validate_validation_report(
    mode: str,
    configured_parts: int,
    scenes: int,
) -> tuple[dict, int]:
    data = read_json(VALIDATION)

    if data.get("status") != "completed":
        fail("Final validation report is not completed")

    if str(data.get("format", "")).strip().lower() != mode:
        fail("Validation report FORMAT mismatch")

    try:
        reported_parts = int(data["parts"])
    except (KeyError, TypeError, ValueError):
        fail("Validation report part count is invalid")

    if reported_parts < 1:
        fail("Validation report part count must be positive")

    if mode == "short":
        declared = data.get("configured_parts")

        if declared is not None:
            try:
                declared = int(declared)
            except (TypeError, ValueError):
                fail("Validation report configured_parts is invalid")

            if declared != configured_parts:
                fail("Validation report configured_parts mismatch")

        if data.get("dynamic_short_parts") is not True:
            fail("SHORT validation report must confirm dynamic part count")

        actual_parts = reported_parts
    else:
        if reported_parts != configured_parts:
            fail("FULL validation report part count mismatch")

        actual_parts = configured_parts

    expected_fields = {
        "parts": actual_parts,
        "scenes_per_part": scenes,
        "expected_scenes": actual_parts * scenes,
    }

    for key, expected in expected_fields.items():
        try:
            actual = int(data.get(key, -1))
        except (TypeError, ValueError):
            fail(f"Invalid validation report field: {key}")

        if actual != expected:
            fail(
                f"Validation report {key} mismatch: "
                f"expected {expected}, got {actual}"
            )

    checks = data.get("checks")

    if not isinstance(checks, dict):
        fail("Validation report checks are missing")

    required = (
        "photo_motion", "narration", "sound_design",
        "render", "captions", "thumbnail", "metadata",
    )

    for name in required:
        item = checks.get(name)

        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Validation check missing or invalid: {name}")

    return data, actual_parts


def validate_render_manifest(
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
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

    if not math.isfinite(manifest_fps):
        fail("Render manifest FPS is not finite")

    if abs(manifest_fps - expected_fps) > FPS_TOLERANCE:
        fail("Render manifest FPS does not match input FPS")

    expected_count = parts * scenes

    try:
        completed_count = int(data.get("completed_count", -1))
    except (TypeError, ValueError):
        completed_count = -1

    if completed_count != expected_count:
        fail(
            f"Render scene count mismatch: "
            f"{completed_count}/{expected_count}"
        )

    rows = data.get("completed_scenes")

    if not isinstance(rows, list) or len(rows) != expected_count:
        fail("Render completed_scenes list is incomplete")

    scene_files = []
    scene_records = []

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render scene row: {index}")

        expected_part = (index - 1) // scenes + 1
        expected_local = (index - 1) % scenes + 1

        try:
            mapping = (
                int(row.get("part", 0)),
                int(row.get("local_scene", row.get("scene", 0))),
                int(row.get("global_scene", index)),
            )
        except (TypeError, ValueError):
            fail(f"Invalid render scene mapping: {index}")

        if mapping != (expected_part, expected_local, index):
            fail(f"Render scene order mismatch: {index}")

        if row.get("status") != "completed":
            fail(f"Render scene is not completed: {index}")

        if row.get("captions_burned") is not True:
            fail(f"Rendered scene captions not confirmed: {index}")

        path = output_path(
            row.get("video") or row.get("output"),
            f"Rendered scene {index}",
            MIN_VIDEO_BYTES,
        )

        info = probe_media(path, expected_size, expected_fps)
        actual_hash = sha256_file(path)
        declared_hash = row.get("sha256") or row.get("video_sha256")

        if declared_hash and declared_hash != actual_hash:
            fail(f"Rendered scene hash mismatch: {index}")

        scene_files.append(path)

        scene_records.append({
            "part": expected_part,
            "scene": expected_local,
            "global_scene": index,
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": actual_hash,
        })

    part_rows = data.get("parts")

    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Expected exactly {parts} rendered part videos")

    part_files = []
    part_records = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                part_number = int(row.get("part", index))
            except (TypeError, ValueError):
                fail(f"Invalid part number: {index}")

            if part_number != index:
                fail(f"Rendered part order mismatch: {index}")

            if row.get("status") not in (None, "completed"):
                fail(f"Rendered part is incomplete: {index}")

            value = row.get("path") or row.get("video") or row.get("output")
        else:
            value = row

        path = output_path(
            value,
            f"Rendered part {index}",
            MIN_VIDEO_BYTES,
        )

        info = probe_media(path, expected_size, expected_fps)
        part_files.append(path)

        part_records.append({
            "part": index,
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })

    final_video = output_path(
        data.get("final_video"),
        "Final video",
        MIN_VIDEO_BYTES,
    )
    alias = output_path(
        data.get("alias"),
        "Format alias",
        MIN_VIDEO_BYTES,
    )

    final_info = probe_media(final_video, expected_size, expected_fps)
    alias_info = probe_media(alias, expected_size, expected_fps)

    final_hash = sha256_file(final_video)
    alias_hash = sha256_file(alias)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    if alias.name != expected_alias:
        fail(f"Wrong format alias: expected {expected_alias}")

    if final_video.resolve() == alias.resolve():
        fail("Final video and alias must be separate files")

    if final_hash != alias_hash:
        fail("Final video and alias content do not match")

    if abs(final_info["duration"] - alias_info["duration"]) > 1:
        fail("Final video and alias durations differ")

    scene_duration = sum(row["duration"] for row in scene_records)
    part_duration = sum(row["duration"] for row in part_records)

    if abs(final_info["duration"] - part_duration) > max(2.0, float(parts)):
        fail("Final video duration differs from combined part duration")

    if abs(final_info["duration"] - scene_duration) > max(
        2.0, float(expected_count)
    ):
        fail("Final video duration differs from combined scene duration")

    return {
        "data": data,
        "scene_files": scene_files,
        "scene_records": scene_records,
        "part_files": part_files,
        "part_records": part_records,
        "final_video": final_video,
        "alias": alias,
        "final_duration": round(final_info["duration"], 3),
        "final_hash": final_hash,
        "alias_hash": alias_hash,
        "same_content": final_hash == alias_hash,
    }


def validate_thumbnail(mode: str) -> Path:
    path = output_path(
        "output/thumbnail.jpg",
        "Thumbnail",
        MIN_VIDEO_BYTES,
    )

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            image_format = image.format
            dimensions = image.size

    except Exception as exc:
        fail(f"Thumbnail image is invalid: {exc}")

    if image_format != "JPEG" or dimensions != (1280, 720):
        fail(
            f"Thumbnail must be 1280x720 JPEG; "
            f"found {image_format} {dimensions}"
        )

    manifest_path = OUT / "thumbnail" / "thumbnail_manifest.json"

    if not manifest_path.is_file():
        fail("Thumbnail manifest missing")

    manifest = read_json(manifest_path)

    if manifest.get("status") != "completed":
        fail("Thumbnail manifest is not completed")

    if str(manifest.get("format", "")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")

    declared = (
        manifest.get("thumbnail")
        or manifest.get("path")
        or manifest.get("output")
    )

    if declared:
        declared_path = output_path(declared, "Thumbnail manifest output")

        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to a different thumbnail")

    return path


def validate_metadata(mode: str) -> tuple[Path, Path]:
    text_path = output_path(
        "output/youtube_metadata.txt",
        "YouTube metadata text",
        10,
    )
    json_path = output_path(
        "output/publish/youtube_metadata.json",
        "YouTube metadata JSON",
        10,
    )

    try:
        content = text_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read YouTube metadata text: {exc}")

    sections = {}
    current = None

    for line in content.splitlines():
        label = line.strip().strip("[]").rstrip(":").strip().upper()

        if label in {"TITLE", "DESCRIPTION", "TAGS"}:
            current = label
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.strip())

    title = " ".join(sections.get("TITLE", [])).strip()
    description = " ".join(sections.get("DESCRIPTION", [])).strip()
    tags_text = " ".join(sections.get("TAGS", [])).strip()

    if not title or not description or not tags_text:
        fail("Metadata text must contain TITLE, DESCRIPTION and TAGS")

    if len(title) > 100:
        fail("YouTube title exceeds 100 characters")

    metadata = read_json(json_path)

    if metadata.get("status") != "completed":
        fail("Structured metadata status is not completed")

    if str(metadata.get("format", "")).lower() != mode:
        fail("Structured metadata FORMAT mismatch")

    json_title = metadata.get("title")
    json_description = metadata.get("description")
    tags = metadata.get("tags")

    if not isinstance(json_title, str) or not json_title.strip():
        fail("Structured metadata title missing")

    if not isinstance(json_description, str) or not json_description.strip():
        fail("Structured metadata description missing")

    if len(json_title) > 100 or len(json_description) > 5000:
        fail("Structured metadata exceeds YouTube text limits")

    if not isinstance(tags, list) or not 8 <= len(tags) <= 20:
        fail("Metadata must contain 8–20 tags")

    normalized_tags = []
    seen = set()

    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            fail("Metadata contains an invalid tag")

        tag = " ".join(tag.split()).strip(" ,#")

        if not tag:
            fail("Metadata contains an empty tag")

        if len(tag) > 100:
            fail("Metadata tag exceeds 100 characters")

        key = tag.casefold()

        if key not in seen:
            seen.add(key)
            normalized_tags.append(tag)

    if len(normalized_tags) < 8:
        fail("Metadata contains fewer than 8 unique tags")

    if json_title.strip() != title:
        fail("Metadata text and JSON titles differ")

    if json_description.strip() != description:
        fail("Metadata text and JSON descriptions differ")

    text_tags = {
        item.strip().lstrip("#").casefold()
        for item in tags_text.split(",")
        if item.strip()
    }
    json_tags = {item.casefold() for item in normalized_tags}

    if text_tags != json_tags:
        fail("Metadata text tags and JSON tags differ")

    return text_path, json_path


def validate_captions() -> Path:
    path = output_path(
        "output/captions/final_captions.srt",
        "Final captions SRT",
        10,
    )

    try:
        content = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read final SRT: {exc}")

    if "-->" not in content:
        fail("Final SRT contains no timing cues")

    return path

def collect_output_files(
    render: dict,
    thumbnail: Path,
    metadata: Path,
    metadata_json: Path,
    captions: Path,
) -> list[Path]:
    required = {
        RENDER.resolve(),
        VALIDATION.resolve(),
        render["final_video"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        metadata_json.resolve(),
        captions.resolve(),
        *(path.resolve() for path in render["scene_files"]),
        *(path.resolve() for path in render["part_files"]),
    }

    selected = set(required)

    for path in OUT.rglob("*"):
        if not path.is_file():
            continue

        resolved = path.resolve()

        if not resolved.is_relative_to(OUT.resolve()):
            fail(f"Output symlink escapes output directory: {path}")

        pieces = resolved.relative_to(OUT.resolve()).parts

        if any(piece in EXCLUDED_DIRS for piece in pieces):
            continue

        if resolved.suffix.lower() in EXCLUDED_SUFFIXES:
            continue

        if resolved == REPORT.resolve():
            continue

        if resolved.name.startswith(".resume-"):
            continue

        selected.add(resolved)

    missing = [
        path for path in required
        if not path.is_file()
    ]

    if missing:
        fail(f"Required package files missing: {missing}")

    files = sorted(selected, key=lambda item: item.as_posix())
    names = [archive_name(path) for path in files]

    if len(names) != len(set(names)):
        fail("Duplicate paths detected while collecting package files")

    return files


def remap_manifest_paths(
    value: Any,
    packaged_names: set[str],
    context: str,
) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in PATH_FIELDS and isinstance(item, str):
                if item.startswith(("http://", "https://")):
                    continue

                candidate = Path(item.strip())

                if not candidate.is_absolute():
                    candidate = ROOT / candidate

                try:
                    resolved = inside_output(candidate)
                except RuntimeError:
                    if item.startswith("output/"):
                        fail(f"{context}: unsafe packaged path: {item}")
                    continue

                member = archive_name(resolved)

                if not resolved.is_file():
                    fail(f"{context}: referenced file missing: {item}")

                if member not in packaged_names:
                    fail(f"{context}: file not included in ZIP: {member}")

                value[key] = member

            elif isinstance(item, (dict, list)):
                remap_manifest_paths(item, packaged_names, context)

    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                remap_manifest_paths(item, packaged_names, context)


def portable_render_manifest(
    render: dict,
    packaged_names: set[str],
) -> dict:
    data = json.loads(json.dumps(render["data"]))

    remap_manifest_paths(
        data,
        packaged_names,
        "render manifest",
    )

    final_member = archive_name(render["final_video"])
    alias_member = archive_name(render["alias"])

    if final_member not in packaged_names:
        fail("Final video is not included in ZIP")

    if alias_member not in packaged_names:
        fail("Format alias is not included in ZIP")

    data["final_video"] = final_member
    data["alias"] = alias_member
    data["package_manifest_view"] = True
    data["package_manifest_version"] = VERSION
    data["package_video_integrity"] = {
        "sha256": render["final_hash"],
        "alias_sha256": render["alias_hash"],
        "same_content": render["same_content"],
    }

    return data


def build_archive(
    files: list[Path],
    render: dict,
) -> list[dict]:
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)

    packaged_names = {archive_name(path) for path in files}
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
                member = archive_name(path)

                if path.resolve() == RENDER.resolve():
                    payload = (
                        json.dumps(
                            portable_render_manifest(
                                render,
                                packaged_names,
                            ),
                            ensure_ascii=False,
                            indent=2,
                        ) + "\n"
                    ).encode("utf-8")
                else:
                    payload = path.read_bytes()

                archive.writestr(member, payload)

                records.append({
                    "path": member,
                    "bytes": len(payload),
                    "sha256": sha256_bytes(payload),
                })

        with zipfile.ZipFile(temporary, "r") as archive:
            bad_member = archive.testzip()

            if bad_member:
                fail(f"ZIP CRC verification failed: {bad_member}")

            members = archive.namelist()

            if len(members) != len(set(members)):
                fail("ZIP contains duplicate file paths")

            if any(name.startswith("/") for name in members):
                fail("ZIP contains an absolute path")

            for name in members:
                pure = PurePosixPath(name)

                if ".." in pure.parts or "\\" in name:
                    fail(f"ZIP contains an unsafe path: {name}")

            for record in records:
                payload = archive.read(record["path"])

                if len(payload) != record["bytes"]:
                    fail(f"ZIP size verification failed: {record['path']}")

                if sha256_bytes(payload) != record["sha256"]:
                    fail(f"ZIP hash verification failed: {record['path']}")

        os.replace(temporary, ARCHIVE)

    finally:
        temporary.unlink(missing_ok=True)

    return records


def verify_zip_video(
    archive: zipfile.ZipFile,
    member: str,
    label: str,
    expected_size: tuple[int, int],
    expected_fps: float,
    temporary_directory: str,
) -> dict:
    names = set(archive.namelist())

    if not isinstance(member, str) or member not in names:
        fail(f"{label}: video missing from ZIP: {member}")

    suffix = PurePosixPath(member).suffix or ".mp4"

    temporary_path = Path(temporary_directory) / (
        hashlib.sha256(member.encode("utf-8")).hexdigest() + suffix
    )

    try:
        with archive.open(member, "r") as source:
            with temporary_path.open("wb") as target:
                shutil.copyfileobj(source, target)

        if temporary_path.stat().st_size < MIN_VIDEO_BYTES:
            fail(f"{label}: packaged video is too small")

        info = probe_media(
            temporary_path,
            expected_size,
            expected_fps,
        )

        return {
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": temporary_path.stat().st_size,
        }

    finally:
        temporary_path.unlink(missing_ok=True)


def verify_packaged_archive(
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    if not ARCHIVE.is_file() or ARCHIVE.stat().st_size < MIN_VIDEO_BYTES:
        fail("Final ZIP is missing or too small")

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        bad_member = archive.testzip()

        if bad_member:
            fail(f"Final ZIP CRC error: {bad_member}")

        members = archive.namelist()
        names = set(members)

        if len(members) != len(names):
            fail("Final ZIP contains duplicate entries")

        render_member = "output/scene_renders/render_manifest.json"

        if render_member not in names:
            fail("Packaged render manifest missing")

        try:
            data = json.loads(archive.read(render_member).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            fail(f"Packaged render manifest is invalid: {exc}")

        if not isinstance(data, dict):
            fail("Packaged render manifest must be a JSON object")

        if data.get("status") != "completed":
            fail("Packaged render status mismatch")

        if str(data.get("format", "")).lower() != mode:
            fail("Packaged render FORMAT mismatch")

        try:
            manifest_fps = float(data.get("fps"))
        except (TypeError, ValueError):
            fail("Packaged render FPS missing")

        if abs(manifest_fps - expected_fps) > FPS_TOLERANCE:
            fail("Packaged render FPS mismatch")

        expected_count = parts * scenes
        rows = data.get("completed_scenes")

        if not isinstance(rows, list) or len(rows) != expected_count:
            fail("Packaged scene count mismatch")

        with tempfile.TemporaryDirectory(
            prefix="katha-lok-package-verify-"
        ) as temporary_directory:

            scene_durations = []

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
                    fail(f"Packaged scene order mismatch: {index}")

                if row.get("status") != "completed":
                    fail(f"Packaged scene is incomplete: {index}")

                if row.get("captions_burned") is not True:
                    fail(f"Packaged scene captions not confirmed: {index}")

                member = row.get("video")

                info = verify_zip_video(
                    archive,
                    member,
                    f"Packaged scene {index}",
                    expected_size,
                    expected_fps,
                    temporary_directory,
                )

                scene_durations.append(info["duration"])

            part_rows = data.get("parts")

            if not isinstance(part_rows, list) or len(part_rows) != parts:
                fail("Packaged part count mismatch")

            part_durations = []

            for index, row in enumerate(part_rows, 1):
                if isinstance(row, dict):
                    try:
                        part_number = int(row.get("part", index))
                    except (TypeError, ValueError):
                        fail(f"Invalid packaged part number: {index}")

                    if part_number != index:
                        fail(f"Packaged part order mismatch: {index}")

                    member = (
                        row.get("path")
                        or row.get("video")
                        or row.get("output")
                    )
                else:
                    member = row

                info = verify_zip_video(
                    archive,
                    member,
                    f"Packaged part {index}",
                    expected_size,
                    expected_fps,
                    temporary_directory,
                )

                part_durations.append(info["duration"])

            for key in ("final_video", "alias"):
                member = data.get(key)

                verify_zip_video(
                    archive,
                    member,
                    f"Packaged {key}",
                    expected_size,
                    expected_fps,
                    temporary_directory,
                )

            final_member = data["final_video"]
            alias_member = data["alias"]

            if final_member == alias_member:
                fail("Packaged final video and alias must be separate entries")

            opposite = (
                "katha_lok_ai_full.mp4"
                if mode == "short"
                else "katha_lok_ai_short.mp4"
            )

            if any(PurePosixPath(name).name == opposite for name in names):
                fail(f"Opposite-format video included in ZIP: {opposite}")

            final_info = verify_zip_video(
                archive,
                final_member,
                "Packaged final video",
                expected_size,
                expected_fps,
                temporary_directory,
            )

            if abs(final_info["duration"] - sum(part_durations)) > max(
                2.0, float(parts)
            ):
                fail("Packaged final duration differs from part durations")

            if abs(final_info["duration"] - sum(scene_durations)) > max(
                2.0, float(expected_count)
            ):
                fail("Packaged final duration differs from scene durations")

        def check_references(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if (
                        key in PATH_FIELDS
                        and isinstance(item, str)
                        and item.startswith("output/")
                        and item not in names
                    ):
                        fail(f"Dangling packaged reference: {item}")

                    check_references(item)

            elif isinstance(value, list):
                for item in value:
                    check_references(item)

        check_references(data)

        required = {
            "output/thumbnail.jpg",
            "output/youtube_metadata.txt",
            "output/publish/youtube_metadata.json",
            "output/captions/final_captions.srt",
            "output/validation_report.json",
            render_member,
        }

        missing = required - names

        if missing:
            fail(f"Required deliverables missing from ZIP: {sorted(missing)}")

        if any(name.lower().endswith(".zip") for name in names):
            fail("Nested ZIP files are not allowed")

        return {
            "status": "valid",
            "entry_count": len(names),
            "crc": "valid",
            "manifest_references": "valid",
            "scene_videos": expected_count,
            "part_videos": parts,
            "final_video": final_member,
            "alias": alias_member,
        }


def main() -> None:
    if not shutil.which("ffprobe"):
        fail("ffprobe is required")

    if not OUT.is_dir():
        fail("output/ directory missing")

    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    expected_fps = float(get_fps(config))

    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")

    if configured_parts < 1 or scenes < 1:
        fail("PARTS and SCENES must be positive")

    if not math.isfinite(expected_fps) or expected_fps <= 0:
        fail("Invalid expected FPS")

    expected_size = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    log("=" * 65)
    log("KATHA LOK AI — FINAL OUTPUT PACKAGER")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("CONFIGURED PARTS:", configured_parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED FPS:", expected_fps)

    validation, actual_parts = validate_validation_report(
        mode,
        configured_parts,
        scenes,
    )

    log("ACTUAL PARTS:", actual_parts)
    log("EXPECTED SCENES:", actual_parts * scenes)

    render = validate_render_manifest(
        mode,
        actual_parts,
        scenes,
        expected_size,
        expected_fps,
    )

    thumbnail = validate_thumbnail(mode)
    metadata, metadata_json = validate_metadata(mode)
    captions = validate_captions()

    files = collect_output_files(
        render,
        thumbnail,
        metadata,
        metadata_json,
        captions,
    )

    log("FILES TO PACKAGE:", len(files))

    records = build_archive(files, render)

    zip_validation = verify_packaged_archive(
        mode,
        actual_parts,
        scenes,
        expected_size,
        expected_fps,
    )

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": actual_parts,
        "configured_parts": configured_parts,
        "dynamic_short_parts": mode == "short",
        "scenes_per_part": scenes,
        "expected_scenes": actual_parts * scenes,
        "video_dimensions": list(expected_size),
        "expected_fps": expected_fps,
        "thumbnail_dimensions": [1280, 720],
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256_file(ARCHIVE),
        "file_count": len(records),
        "scene_count": len(render["scene_records"]),
        "part_count": len(render["part_records"]),
        "final_video": relative_output(render["final_video"]),
        "format_alias": relative_output(render["alias"]),
        "final_video_sha256": render["final_hash"],
        "format_alias_sha256": render["alias_hash"],
        "thumbnail": relative_output(thumbnail),
        "metadata": relative_output(metadata),
        "metadata_json": relative_output(metadata_json),
        "captions": relative_output(captions),
        "validation_report": relative_output(VALIDATION),
        "render_manifest": relative_output(RENDER),
        "scene_files": render["scene_records"],
        "part_files": render["part_records"],
        "files": records,
        "zip_integrity": "valid",
        "manifest_zip_consistency": "valid",
        "zip_video_fps_validation": "valid",
        "zip_validation": zip_validation,
        "validation_checks": validation["checks"],
    }

    write_json(REPORT, report)

    saved_report = read_json(REPORT)

    if saved_report.get("status") != "completed":
        fail("Package report status verification failed")

    if saved_report.get("format") != mode:
        fail("Package report FORMAT verification failed")

    if int(saved_report.get("file_count", 0)) != len(records):
        fail("Package report file_count verification failed")

    if saved_report.get("zip_integrity") != "valid":
        fail("Package report ZIP integrity verification failed")

    if saved_report.get("archive_sha256") != sha256_file(ARCHIVE):
        fail("Package report archive hash verification failed")

    log("-" * 65)
    log("PACKAGE STATUS: completed")
    log("FORMAT:", mode)
    log("PARTS:", actual_parts)
    log("SCENES:", actual_parts * scenes)
    log("ZIP:", ARCHIVE.name)
    log("ZIP SIZE:", ARCHIVE.stat().st_size)
    log("FILES:", len(records))
    log("ZIP CRC: valid")
    log("VIDEO FPS: valid")
    log("MANIFEST REFERENCES: valid")
    log("REPORT:", REPORT.relative_to(ROOT))
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGE ERROR:", str(exc))
        raise