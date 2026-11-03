#!/usr/bin/env python3
"""Validate and package only the selected-format, story-named Part videos."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any

from input_config import (
    get_format,
    get_fps,
    get_parts,
    get_scenes,
    load_and_validate,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"

REPORT = OUT / "package_manifest.json"
VALIDATION = OUT / "validation_report.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
METADATA_TEXT = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
TAGS = OUT / "publish" / "youtube_tags.txt"
FINAL_SRT = OUT / "captions" / "final_captions.srt"

VERSION = "output-packager-v61-story-named-part-only-integrity"

MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
FPS_TOLERANCE = 0.03
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120
ZIP_COMPRESSION_LEVEL = 6

EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}

EXCLUDED_SUFFIXES = {
    ".zip",
    ".tmp",
    ".lock",
    ".pyc",
    ".part",
    ".download",
    ".rendering",
    ".joining",
    ".building",
}

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}

PATH_FIELDS = {
    "video",
    "output",
    "path",
    "final_video",
    "alias",
    "pre_caption_video",
    "caption_manifest",
    "final_srt",
    "source_video",
    "source_photo_motion_video",
    "source_rendered_video",
    "source_audio",
    "narration",
    "audio",
    "image",
    "image_path",
    "depth_map",
    "depth_map_path",
    "thumbnail",
    "thumbnail_path",
    "source_photo_manifest",
    "source_audio_manifest",
    "source_sound_manifest",
    "source_sfx_validation",
    "file",
    "audio_path",
    "source_image",
    "source_manifest",
    "video_path",
    "audio_file",
    "image_file",
    "font_path",
    "caption_font_file",
    "srt",
    "manifest",
}

SOURCE_ONLY_FIELDS = {
    "source_rendered_video",
    "source_photo_motion_video",
    "source_video",
    "source_audio",
    "source_image",
    "depth_map",
    "depth_map_path",
    "pre_caption_video",
    "final_video",
    "alias",
}

REQUIRED_MEMBERS = {
    "output/thumbnail.jpg",
    "output/youtube_metadata.txt",
    "output/publish/youtube_metadata.json",
    "output/publish/youtube_tags.txt",
    "output/captions/final_captions.srt",
    "output/validation_report.json",
    "output/scene_renders/render_manifest.json",
    "output/package_manifest.json",
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
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")

    if not isinstance(value, dict):
        fail(f"Expected a JSON object: {path}")

    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_output_path(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path is missing.")

    candidate = Path(value.strip())

    if not candidate.is_absolute():
        candidate = ROOT / candidate

    resolved = candidate.resolve()

    try:
        resolved.relative_to(OUT.resolve())
    except ValueError:
        fail(f"{label}: path escapes output directory.")

    if not resolved.is_file():
        fail(f"{label}: file not found: {resolved}")

    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small.")

    return resolved


def relative_output(path: Path) -> str:
    resolved = Path(path).resolve()

    try:
        return resolved.relative_to(OUT.resolve()).as_posix()
    except ValueError:
        fail(f"Path is outside output/: {resolved}")


def archive_member(path: Path) -> str:
    return "output/" + relative_output(path)


def parse_rate(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (ValueError, TypeError, ZeroDivisionError):
        fail(f"{label}: invalid frame rate.")

    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid frame rate.")

    return result


def probe_media(
    path: Path,
    expected_size: tuple[int, int] | None = None,
    expected_fps: float | None = None,
    require_audio: bool = True,
) -> dict:
    executable = shutil.which("ffprobe")

    if not executable:
        fail("ffprobe is required but was not found.")

    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration:"
                "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
                "-of",
                "json",
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
            f"{(result.stderr or '')[-700:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media information for {path.name}: {exc}")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {path.name}")

    streams = data.get("streams")

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

    if video is None:
        fail(f"Video stream missing: {path.name}")

    if require_audio and audio is None:
        fail(f"Audio stream missing: {path.name}")

    dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )

    if expected_size and dimensions != expected_size:
        fail(
            f"{path.name}: expected dimensions {expected_size}, "
            f"found {dimensions}"
        )

    rate_value = video.get("avg_frame_rate")

    if not rate_value or rate_value in {"0/0", "N/A"}:
        rate_value = video.get("r_frame_rate")

    actual_fps = parse_rate(rate_value, path.name)

    if expected_fps is not None:
        if abs(actual_fps - expected_fps) > FPS_TOLERANCE:
            fail(
                f"FPS mismatch in {path.name}: "
                f"expected {expected_fps}, found {actual_fps:.4f}"
            )

    return {
        "duration": duration,
        "width": dimensions[0],
        "height": dimensions[1],
        "fps": actual_fps,
        "has_video": True,
        "has_audio": audio is not None,
    }


def normalize_role(row: dict) -> str:
    value = str(
        row.get("role")
        or row.get("scene_type")
        or row.get("type")
        or "main"
    ).strip().lower()

    aliases = {
        "opening": "hook",
        "intro": "hook",
        "part_hook": "hook",
        "story": "main",
        "main_scene": "main",
        "cliffhanger": "suspense",
        "ending": "resolution",
        "final": "resolution",
    }

    value = aliases.get(value, value)

    if value not in {"main", "hook", "suspense", "resolution"}:
        fail(f"Unknown scene role: {value!r}")

    return value


def get_part_numbers(
    render: dict,
    validation: dict,
    configured_parts: int,
    actual_parts: int,
) -> list[int]:
    candidates = [
        render.get("part_numbers"),
        validation.get("part_numbers"),
    ]

    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            try:
                numbers = [int(item) for item in candidate]
            except (TypeError, ValueError):
                fail("Invalid part_numbers metadata.")

            if (
                len(numbers) != actual_parts
                or numbers != sorted(set(numbers))
            ):
                fail("Part numbers are duplicated, unordered or incomplete.")

            return numbers

    first = (
        render.get("first_part")
        or render.get("start_part")
        or validation.get("first_part")
        or validation.get("start_part")
        or 1
    )

    try:
        first = int(first)
    except (TypeError, ValueError):
        fail("Invalid first part number.")

    if first < 1:
        fail("Part numbers must be positive.")

    return list(range(first, first + actual_parts))


def validate_validation_report(
    mode: str,
    configured_parts: int,
    scenes: int,
) -> tuple[dict, int]:
    data = read_json(VALIDATION)

    if data.get("status") != "completed":
        fail("Final validation report is not completed.")

    if str(data.get("format", "")).strip().lower() != mode:
        fail("Validation report FORMAT mismatch.")

    try:
        actual_parts = int(data["parts"])
    except (KeyError, TypeError, ValueError):
        fail("Validation report part count is invalid.")

    if actual_parts < 1:
        fail("Validation report part count must be positive.")

    if mode == "full" and actual_parts != configured_parts:
        fail("FULL part count differs from configured PARTS.")

    if mode == "short":
        declared = data.get("configured_parts")

        if declared is not None:
            try:
                declared = int(declared)
            except (TypeError, ValueError):
                fail("Validation report configured_parts is invalid.")

            if declared != configured_parts:
                fail("SHORT configured PARTS mismatch.")

        if data.get("dynamic_short_parts") is not True:
            fail("SHORT report does not confirm dynamic part selection.")

    expected_fields = {
        "parts": actual_parts,
        "scenes_per_part": scenes,
        "expected_scenes": actual_parts * scenes,
    }

    for key, expected in expected_fields.items():
        try:
            found = int(data.get(key, -1))
        except (TypeError, ValueError):
            fail(f"Invalid validation report field: {key}")

        if found != expected:
            fail(
                f"Validation report {key} mismatch: "
                f"expected {expected}, found {found}"
            )

    checks = data.get("checks")

    if not isinstance(checks, dict):
        fail("Validation report checks are missing.")

    required_checks = (
        "photo_motion",
        "narration",
        "sound_design",
        "render",
        "captions",
        "thumbnail",
        "metadata",
    )

    for name in required_checks:
        item = checks.get(name)

        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Validation check missing or invalid: {name}")

    return data, actual_parts


def normalize_render_rows(data: dict) -> list[dict]:
    rows = data.get("all_completed_scenes")

    if not isinstance(rows, list):
        main_rows = data.get("completed_scenes") or data.get("scenes") or []
        extra_rows = data.get("extra_scenes") or []

        if not isinstance(main_rows, list):
            fail("Render scene records are invalid.")

        if not isinstance(extra_rows, list):
            fail("Render extra scene records are invalid.")

        rows = list(main_rows) + list(extra_rows)

    if not rows:
        fail("Render manifest contains no scene records.")

    result = []
    seen = set()

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render scene row: {index}")

        scene_id = str(row.get("scene_id") or row.get("id") or "").strip()

        if not scene_id:
            fail(f"Render scene {index} has no scene_id.")

        if scene_id in seen:
            fail(f"Duplicate render scene_id: {scene_id}")

        seen.add(scene_id)
        result.append(row)

    return result


def validate_role_records(
    rows: list[dict],
    part_numbers: list[int],
    scenes_per_part: int,
) -> dict:
    expected_main = {
        (part, scene)
        for part in part_numbers
        for scene in range(1, scenes_per_part + 1)
    }

    actual_main = set()
    role_counts: dict[str, dict[int, int]] = {}

    for row in rows:
        role = normalize_role(row)

        try:
            part = int(row.get("part"))
        except (TypeError, ValueError):
            fail(f"Invalid part number in {row.get('scene_id')}.")

        if part not in part_numbers:
            fail(f"Scene belongs to an unselected part: {part}")

        if role == "main":
            try:
                scene = int(
                    row.get("local_scene")
                    or row.get("scene")
                    or 0
                )
            except (TypeError, ValueError):
                fail(f"Invalid Main scene number: {row.get('scene_id')}")

            key = (part, scene)

            if key in actual_main:
                fail(f"Duplicate Main scene: {key}")

            actual_main.add(key)

        else:
            role_counts.setdefault(role, {})
            role_counts[role][part] = (
                role_counts[role].get(part, 0) + 1
            )

    if actual_main != expected_main:
        fail(
            "Main scene mapping mismatch: "
            f"missing={sorted(expected_main - actual_main)}, "
            f"extra={sorted(actual_main - expected_main)}"
        )

    for part in part_numbers:
        if role_counts.get("hook", {}).get(part, 0) != 1:
            fail(f"Part {part} must contain exactly one Hook.")

    for part in part_numbers[:-1]:
        if role_counts.get("suspense", {}).get(part, 0) != 1:
            fail(f"Non-final part {part} must contain exactly one Suspense.")

    final_part = part_numbers[-1]

    if role_counts.get("suspense", {}).get(final_part, 0) != 0:
        fail("Final part must not contain a Suspense scene.")

    if role_counts.get("resolution", {}).get(final_part, 0) != 1:
        fail("Final part must contain exactly one Resolution.")

    for role, counts in role_counts.items():
        for part, count in counts.items():
            if role in {"hook", "resolution"} and count != 1:
                fail(f"Duplicate {role} record in part {part}.")

            if role == "suspense" and count != 1:
                fail(f"Duplicate Suspense record in part {part}.")

    expected_extra_count = len(part_numbers) * 2

    actual_extra_count = sum(
        1 for row in rows if normalize_role(row) != "main"
    )

    if actual_extra_count != expected_extra_count:
        fail(
            "Hook/Suspense/Resolution record count mismatch: "
            f"{actual_extra_count}/{expected_extra_count}"
        )

    return {
        "main_scene_count": len(actual_main),
        "extra_role_scene_count": actual_extra_count,
        "role_counts": role_counts,
    }


def validate_render_manifest(
    mode: str,
    part_numbers: list[int],
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    data = read_json(RENDER)

    if data.get("status") != "completed":
        fail("Render manifest is not completed.")

    if str(data.get("format", "")).strip().lower() != mode:
        fail("Render manifest FORMAT mismatch.")

    try:
        width = int(data.get("width", expected_size[0]))
        height = int(data.get("height", expected_size[1]))
        manifest_fps = float(data.get("fps"))
    except (TypeError, ValueError):
        fail("Render dimensions or FPS are invalid.")

    if (width, height) != expected_size:
        fail("Render dimensions do not match selected format.")

    if (
        not math.isfinite(manifest_fps)
        or abs(manifest_fps - expected_fps) > FPS_TOLERANCE
    ):
        fail("Render FPS does not match Input/topic.txt.")

    rows = normalize_render_rows(data)
    role_report = validate_role_records(rows, part_numbers, scenes)

    checked_rows = []
    total_scene_duration = 0.0

    for index, row in enumerate(rows, 1):
        if row.get("status") != "completed":
            fail(f"Render scene is incomplete: {row.get('scene_id')}")

        role = normalize_role(row)

        value = (
            row.get("video")
            or row.get("output")
            or row.get("path")
        )

        path = safe_output_path(
            value,
            f"Rendered scene {row.get('scene_id')}",
            MIN_VIDEO_BYTES,
        )

        info = probe_media(path, expected_size, expected_fps)
        digest = sha256_file(path)

        declared_hash = row.get("sha256") or row.get("video_sha256")

        if declared_hash and declared_hash != digest:
            fail(f"Rendered scene SHA256 mismatch: {row.get('scene_id')}")

        checked_rows.append({
            "scene_id": row["scene_id"],
            "role": role,
            "part": int(row["part"]),
            "local_scene": (
                int(row.get("local_scene") or row.get("scene") or 0)
                if role == "main"
                else None
            ),
            "duration": round(info["duration"], 3),
            "sha256": digest,
            "captions_burned": row.get("captions_burned"),
        })

        total_scene_duration += info["duration"]

    part_rows = data.get("parts")

    if not isinstance(part_rows, list) or len(part_rows) != len(part_numbers):
        fail("Render manifest does not contain exactly the selected Part videos.")

    checked_parts = []
    total_part_duration = 0.0
    part_paths = []

    for index, row in enumerate(part_rows):
        expected_part = part_numbers[index]

        if isinstance(row, dict):
            try:
                part = int(row.get("part", expected_part))
            except (TypeError, ValueError):
                fail(f"Invalid rendered Part number: {index + 1}")

            if part != expected_part:
                fail(f"Rendered Part order mismatch: expected {expected_part}.")

            if row.get("status") not in (None, "completed"):
                fail(f"Rendered Part {part} is incomplete.")

            value = row.get("path") or row.get("video") or row.get("output")
            declared_hash = row.get("sha256")
        else:
            value = row
            declared_hash = None

        path = safe_output_path(
            value,
            f"Rendered Part {expected_part}",
            MIN_VIDEO_BYTES,
        )

        if path.suffix.lower() != ".mp4":
            fail(f"Part {expected_part} is not an MP4 file.")

        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(f"Part {expected_part} is not stored in output/parts/.")

        expected_filename = f"part_{expected_part:02d}.mp4"

        if path.name != expected_filename:
            fail(
                f"Part filename mismatch: expected {expected_filename}, "
                f"found {path.name}"
            )

        info = probe_media(path, expected_size, expected_fps)
        digest = sha256_file(path)

        if declared_hash and declared_hash != digest:
            fail(f"Part {expected_part} SHA256 mismatch.")

        checked_parts.append({
            "part": expected_part,
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "width": info["width"],
            "height": info["height"],
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })

        part_paths.append(path)
        total_part_duration += info["duration"]

    if abs(total_part_duration - total_scene_duration) > max(
        DURATION_TOLERANCE,
        float(len(rows)),
    ):
        fail("Part video duration differs significantly from scene durations.")

    return {
        "source": data,
        "scene_rows": rows,
        "checked_scene_rows": checked_rows,
        "parts": checked_parts,
        "part_paths": part_paths,
        "part_duration_total": round(total_part_duration, 3),
        "scene_duration_total": round(total_scene_duration, 3),
        **role_report,
    }


def validate_thumbnail(
    mode: str,
    expected_size: tuple[int, int],
) -> tuple[Path, dict]:
    path = safe_output_path(
        "output/thumbnail.jpg",
        "Thumbnail",
        MIN_IMAGE_BYTES,
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

    if image_format != "JPEG":
        fail(f"Thumbnail must be JPEG; found {image_format}.")

    if dimensions != expected_size:
        fail(
            f"{mode.upper()} thumbnail must be {expected_size[0]}x"
            f"{expected_size[1]}; found {dimensions}."
        )

    manifest_path = OUT / "thumbnail" / "thumbnail_manifest.json"

    if not manifest_path.is_file():
        fail("Thumbnail manifest is missing.")

    manifest = read_json(manifest_path)

    if manifest.get("status") != "completed":
        fail("Thumbnail manifest is not completed.")

    if str(manifest.get("format", "")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch.")

    declared = (
        manifest.get("thumbnail")
        or manifest.get("path")
        or manifest.get("output")
    )

    if declared:
        declared_path = safe_output_path(
            declared,
            "Thumbnail manifest image",
            MIN_IMAGE_BYTES,
        )

        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to another image.")

    actual_hash = sha256_file(path)

    for item in manifest.get("thumbnails", []):
        if not isinstance(item, dict):
            continue

        filename = str(item.get("file") or "").strip()

        if filename in {
            "output/thumbnail.jpg",
            "thumbnail.jpg",
        }:
            declared_hash = item.get("sha256")

            if declared_hash and declared_hash != actual_hash:
                fail("Thumbnail SHA256 mismatch.")

    return path, {
        "path": relative_output(path),
        "format": image_format,
        "dimensions": list(dimensions),
        "sha256": actual_hash,
    }


def parse_tags(value: str) -> list[str]:
    value = value.strip()

    if not value:
        return []

    raw = (
        value.replace("\r", "\n").split("\n")
        if "\n" in value or "\r" in value
        else value.split(",")
    )

    result = []
    seen = set()

    for item in raw:
        tag = " ".join(item.split()).strip(" ,#")

        if not tag:
            continue

        key = tag.casefold()

        if key not in seen:
            seen.add(key)
            result.append(tag)

    return result


def validate_metadata(mode: str) -> tuple[Path, Path, Path, dict]:
    text_path = safe_output_path(
        "output/youtube_metadata.txt",
        "YouTube metadata text",
        10,
    )

    json_path = safe_output_path(
        "output/publish/youtube_metadata.json",
        "YouTube metadata JSON",
        10,
    )

    tags_path = safe_output_path(
        "output/publish/youtube_tags.txt",
        "YouTube tags",
        5,
    )

    content = text_path.read_text(encoding="utf-8-sig").strip()

    sections: dict[str, list[str]] = {}
    current = None

    for line in content.splitlines():
        label = line.strip().strip("[]").rstrip(":").strip().upper()

        if label in {"TITLE", "DESCRIPTION", "TAGS"}:
            current = label
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.rstrip())

    title = " ".join(sections.get("TITLE", [])).strip()
    description = "\n".join(sections.get("DESCRIPTION", [])).strip()
    text_tags = parse_tags(
        " ".join(sections.get("TAGS", [])).strip()
    )

    if not title or not description or not text_tags:
        fail("Metadata text must contain TITLE, DESCRIPTION and TAGS.")

    if len(title) > 100:
        fail("YouTube title exceeds 100 characters.")

    if len(description) > 5000:
        fail("YouTube description exceeds 5000 characters.")

    metadata = read_json(json_path)

    if metadata.get("status") != "completed":
        fail("Structured metadata is not completed.")

    if str(metadata.get("format", "")).lower() != mode:
        fail("Metadata FORMAT mismatch.")

    json_title = metadata.get("title")
    json_description = metadata.get("description")
    tags = metadata.get("tags")

    if not isinstance(json_title, str) or not json_title.strip():
        fail("Structured metadata title is missing.")

    if not isinstance(json_description, str) or not json_description.strip():
        fail("Structured metadata description is missing.")

    if len(json_title) > 100 or len(json_description) > 5000:
        fail("Structured metadata exceeds YouTube limits.")

    if not isinstance(tags, list) or not 5 <= len(tags) <= 20:
        fail("Metadata must contain 5–20 tags.")

    normalized_tags = []
    seen = set()

    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            fail("Metadata contains an invalid tag.")

        normalized = " ".join(tag.split()).strip(" ,#")

        if not normalized or len(normalized) > 100:
            fail("Metadata contains an invalid tag.")

        key = normalized.casefold()

        if key not in seen:
            seen.add(key)
            normalized_tags.append(normalized)

    if not 5 <= len(normalized_tags) <= 20:
        fail("Metadata must contain 5–20 unique tags.")

    if json_title.strip() != title:
        fail("Metadata text and JSON titles differ.")

    if json_description.strip() != description:
        fail("Metadata text and JSON descriptions differ.")

    expected = {item.casefold() for item in normalized_tags}

    dedicated_tags = parse_tags(
        tags_path.read_text(encoding="utf-8-sig")
    )

    if {item.casefold() for item in text_tags} != expected:
        fail("Metadata text tags differ from structured tags.")

    if {item.casefold() for item in dedicated_tags} != expected:
        fail("Dedicated tags differ from structured tags.")

    return text_path, json_path, tags_path, {
        "title": title,
        "description_sha256": hashlib.sha256(
            description.encode("utf-8")
        ).hexdigest(),
        "tags": normalized_tags,
    }


def validate_srt() -> tuple[Path, dict]:
    path = safe_output_path(
        "output/captions/final_captions.srt",
        "Final captions SRT",
        10,
    )

    content = path.read_text(encoding="utf-8-sig")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()

    if "-->" not in content:
        fail("Final SRT contains no timing cues.")

    blocks = [
        block.strip()
        for block in re.split(r"\n[ \t]*\n", content)
        if block.strip()
    ]

    previous_end = -1.0
    cue_count = 0
    maximum_end = 0.0

    def to_seconds(value: str) -> float:
        match = re.fullmatch(
            r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
            value.strip(),
        )

        if not match:
            fail(f"Invalid SRT timestamp: {value}")

        hours, minutes, seconds, millis = map(int, match.groups())

        return (
            hours * 3600
            + minutes * 60
            + seconds
            + millis / 1000
        )

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()

        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index} is incomplete.")

        if int(lines[0].strip()) != index:
            fail(f"SRT cue numbering is not continuous at {index}.")

        if lines[1].count("-->") != 1:
            fail(f"SRT cue {index} has invalid timing.")

        left, right = [
            value.strip() for value in lines[1].split("-->")
        ]

        start = to_seconds(left)
        end = to_seconds(right)

        if start < 0 or end <= start:
            fail(f"SRT cue {index} has invalid duration.")

        if start < previous_end - 0.001:
            fail(f"SRT cue {index} overlaps or is out of order.")

        caption = "\n".join(lines[2:]).strip()

        if not caption:
            fail(f"SRT cue {index} has no caption text.")

        previous_end = end
        maximum_end = max(maximum_end, end)
        cue_count += 1

    if cue_count == 0:
        fail("Final SRT contains no valid captions.")

    return path, {
        "path": relative_output(path),
        "cue_count": cue_count,
        "last_cue_end": round(maximum_end, 3),
        "sha256": sha256_file(path),
    }


def read_story_title() -> str:
    candidates = [
        OUT / "story" / "story.json",
        OUT / "story" / "ai_story.json",
    ]

    title = ""

    for path in candidates:
        if not path.is_file():
            continue

        data = read_json(path)

        for key in (
            "final_title",
            "generated_title",
            "story_title",
            "title",
        ):
            value = data.get(key)

            if isinstance(value, str) and value.strip():
                title = value.strip()
                break

        if title:
            break

        nested = data.get("story")

        if isinstance(nested, dict):
            for key in (
                "final_title",
                "generated_title",
                "story_title",
                "title",
            ):
                value = nested.get(key)

                if isinstance(value, str) and value.strip():
                    title = value.strip()
                    break

        if title:
            break

    if not title:
        title_file = OUT / "story" / "final_title.txt"

        if title_file.is_file():
            title = title_file.read_text(
                encoding="utf-8-sig"
            ).strip()

    if not title:
        fail("Story title is missing; generic ZIP names are prohibited.")

    return title


def safe_slug(title: str) -> str:
    slug = re.sub(
        r"[^\w]+",
        "-",
        title,
        flags=re.UNICODE,
    ).strip("-_")

    slug = slug[:90].strip("-_")

    if not slug:
        fail("Story title cannot form a safe ZIP filename.")

    return slug


def portable_render_manifest(
    render: dict,
    package_file_names: set[str],
) -> dict:
    data = json.loads(json.dumps(render["source"]))

    # The package contains Part videos only. Source scene clips and
    # combined/alias videos are deliberately not included.
    for key in (
        "scenes",
        "completed_scenes",
        "all_completed_scenes",
        "extra_scenes",
        "completed_count",
        "all_completed_scene_count",
        "final_video",
        "alias",
        "combined_video",
        "combined_output",
        "pre_caption_video",
    ):
        data.pop(key, None)

    portable_parts = []

    for item in render["parts"]:
        member = "output/" + item["path"]

        if member not in package_file_names:
            fail(f"Part video missing from package: {member}")

        portable_parts.append({
            "part": item["part"],
            "path": member,
            "status": "completed",
            "duration": item["duration"],
            "width": item["width"],
            "height": item["height"],
            "fps": item["fps"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        })

    data["parts"] = portable_parts
    data["part_numbers"] = [item["part"] for item in portable_parts]
    data["part_videos_only"] = True
    data["combined_video_included"] = False
    data["format_alias_included"] = False
    data["package_manifest_view"] = True
    data["package_manifest_version"] = VERSION

    data["package_integrity"] = {
        "part_count": len(portable_parts),
        "part_sha256": {
            str(item["part"]): item["sha256"]
            for item in portable_parts
        },
    }

    return data


def should_exclude_file(path: Path) -> bool:
    if not path.is_file():
        return True

    if path.is_symlink():
        fail(f"Output symlink is not allowed: {path}")

    if not path.resolve().is_relative_to(OUT.resolve()):
        fail(f"Output path escapes output/: {path}")

    pieces = path.relative_to(OUT).parts

    if any(piece in EXCLUDED_DIRS for piece in pieces):
        return True

    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True

    if path.name.startswith(".resume-"):
        return True

    if path.name.endswith(".tmp"):
        return True

    return False


def collect_package_files(
    render: dict,
    thumbnail: Path,
    metadata: Path,
    metadata_json: Path,
    tags: Path,
    captions: Path,
) -> list[Path]:
    required = {
        RENDER.resolve(),
        VALIDATION.resolve(),
        REPORT.resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        metadata_json.resolve(),
        tags.resolve(),
        captions.resolve(),
        *(path.resolve() for path in render["part_paths"]),
    }

    selected = set(required)

    # Include the selected format's supporting output files, but never
    # include scene-level video clips or combined/alias videos.
    for path in OUT.rglob("*"):
        if should_exclude_file(path):
            continue

        resolved = path.resolve()

        if resolved in required:
            continue

        if path.suffix.lower() in VIDEO_SUFFIXES:
            continue

        if path.suffix.lower() == ".zip":
            continue

        if path.name in {
            "katha_lok_ai_final.mp4",
            "katha_lok_ai_short.mp4",
            "katha_lok_ai_full.mp4",
            "final_video.mp4",
            "combined_video.mp4",
            "pre_caption_render.mp4",
        }:
            continue

        selected.add(resolved)

    missing = [path for path in required if not path.is_file()]

    if missing:
        fail(f"Required package files are missing: {missing}")

    files = sorted(selected, key=lambda item: item.as_posix())

    names = ["output/" + relative_output(path) for path in files]

    if len(names) != len(set(names)):
        fail("Duplicate package paths detected.")

    for path in files:
        if path.suffix.lower() in VIDEO_SUFFIXES:
            if path not in {
                item.resolve() for item in render["part_paths"]
            }:
                fail(f"Non-Part video found in package selection: {path}")

    return files


def validate_zip_structure(archive: zipfile.ZipFile) -> list[str]:
    bad_member = archive.testzip()

    if bad_member:
        fail(f"ZIP CRC verification failed: {bad_member}")

    names = archive.namelist()

    if len(names) != len(set(names)):
        fail("ZIP contains duplicate paths.")

    for name in names:
        pure = PurePosixPath(name)

        if (
            name.startswith("/")
            or "\\" in name
            or ".." in pure.parts
            or not pure.parts
        ):
            fail(f"Unsafe ZIP member: {name}")

    return names


def build_archive(
    archive_path: Path,
    files: list[Path],
    render: dict,
) -> list[dict]:
    temporary = archive_path.with_name(archive_path.name + ".tmp")
    temporary.unlink(missing_ok=True)

    package_names = {
        "output/" + relative_output(path)
        for path in files
    }

    records = []

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=ZIP_COMPRESSION_LEVEL,
            allowZip64=True,
        ) as archive:
            for path in files:
                member = "output/" + relative_output(path)

                if path.resolve() == RENDER.resolve():
                    payload = (
                        json.dumps(
                            portable_render_manifest(
                                render,
                                package_names,
                            ),
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
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
            names = validate_zip_structure(archive)

            if set(names) != package_names:
                fail("ZIP members differ from the package file list.")

            for record in records:
                payload = archive.read(record["path"])

                if len(payload) != record["bytes"]:
                    fail(f"ZIP size mismatch: {record['path']}")

                if sha256_bytes(payload) != record["sha256"]:
                    fail(f"ZIP SHA256 mismatch: {record['path']}")

        os.replace(temporary, archive_path)
        return records

    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def verify_packaged_archive(
    archive_path: Path,
    mode: str,
    part_numbers: list[int],
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    if not archive_path.is_file() or archive_path.stat().st_size < 1000:
        fail("Final ZIP is missing or too small.")

    with zipfile.ZipFile(archive_path, "r") as archive:
        names = validate_zip_structure(archive)
        name_set = set(names)

        missing = REQUIRED_MEMBERS - name_set

        if missing:
            fail(f"Required ZIP files are missing: {sorted(missing)}")

        part_members = [
            item for item in names
            if re.fullmatch(r"output/parts/part_\d+\.mp4", item)
        ]

        expected_part_members = [
            f"output/parts/part_{part:02d}.mp4"
            for part in part_numbers
        ]

        if sorted(part_members) != sorted(expected_part_members):
            fail("ZIP does not contain exactly the selected Part videos.")

        for name in names:
            if PurePosixPath(name).suffix.lower() in VIDEO_SUFFIXES:
                if name not in expected_part_members:
                    fail(f"Non-Part video found in ZIP: {name}")

        for name in names:
            if name.lower().endswith(".zip"):
                fail(f"Nested ZIP is not allowed: {name}")

        manifest = json.loads(
            archive.read("output/scene_renders/render_manifest.json")
            .decode("utf-8")
        )

        if manifest.get("status") != "completed":
            fail("Packaged render manifest is incomplete.")

        if str(manifest.get("format", "")).lower() != mode:
            fail("Packaged render manifest FORMAT mismatch.")

        if manifest.get("part_videos_only") is not True:
            fail("Packaged render manifest does not confirm Part-only output.")

        rows = manifest.get("parts")

        if not isinstance(rows, list) or len(rows) != len(part_numbers):
            fail("Packaged render manifest Part list is invalid.")

        verified_parts = []

        with tempfile.TemporaryDirectory(
            prefix="katha-package-check-"
        ) as temporary_directory:
            for index, row in enumerate(rows):
                expected_part = part_numbers[index]

                if int(row.get("part", -1)) != expected_part:
                    fail("Packaged Part ordering is incorrect.")

                member = row.get("path")

                if member not in name_set:
                    fail(f"Packaged Part reference is missing: {member}")

                suffix = PurePosixPath(member).suffix or ".mp4"

                temporary_path = (
                    Path(temporary_directory)
                    / f"part_{expected_part:02d}{suffix}"
                )

                with archive.open(member) as source:
                    with temporary_path.open("wb") as target:
                        shutil.copyfileobj(source, target)

                if temporary_path.stat().st_size < MIN_VIDEO_BYTES:
                    fail(f"Packaged Part {expected_part} is too small.")

                info = probe_media(
                    temporary_path,
                    expected_size,
                    expected_fps,
                )

                digest = sha256_file(temporary_path)

                if digest != row.get("sha256"):
                    fail(f"Packaged Part {expected_part} hash mismatch.")

                verified_parts.append({
                    "part": expected_part,
                    "path": member,
                    "duration": round(info["duration"], 3),
                    "sha256": digest,
                    "bytes": temporary_path.stat().st_size,
                })

        if "output/katha_lok_ai_final.mp4" in name_set:
            fail("Combined final video must not be packaged.")

        if (
            "output/katha_lok_ai_short.mp4" in name_set
            or "output/katha_lok_ai_full.mp4" in name_set
        ):
            fail("Format alias video must not be packaged.")

        return {
            "status": "valid",
            "crc": "valid",
            "entry_count": len(names),
            "part_count": len(verified_parts),
            "part_videos_only": True,
            "verified_parts": verified_parts,
        }


def verify_package_report(report: dict, archive_path: Path) -> None:
    saved = read_json(REPORT)

    if saved.get("status") != "completed":
        fail("Package report status verification failed.")

    if saved.get("version") != VERSION:
        fail("Package report version verification failed.")

    if saved.get("format") != report.get("format"):
        fail("Package report FORMAT verification failed.")

    if saved.get("archive") != archive_path.name:
        fail("Package report archive name mismatch.")

    if saved.get("archive_sha256") != sha256_file(archive_path):
        fail("Package report archive SHA256 mismatch.")

    if saved.get("archive_bytes") != archive_path.stat().st_size:
        fail("Package report archive size mismatch.")

    if saved.get("zip_integrity") != "valid":
        fail("Package report ZIP integrity is invalid.")

    if saved.get("zip_validation", {}).get("status") != "valid":
        fail("Package ZIP validation report is invalid.")

    if saved.get("part_videos_only") is not True:
        fail("Package report does not confirm Part-only videos.")

    if saved.get("combined_video_included") is not False:
        fail("Package report incorrectly includes a combined video.")

    if saved.get("format_alias_included") is not False:
        fail("Package report incorrectly includes a format alias.")


def main() -> None:
    if not shutil.which("ffprobe"):
        fail("ffprobe is required.")

    if not OUT.is_dir():
        fail("output/ directory is missing.")

    if not INPUT_FILE.is_file():
        fail("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_FILE)

    mode = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    expected_fps = float(get_fps(config))

    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")

    if configured_parts < 1 or scenes < 1:
        fail("PARTS and SCENES must be positive.")

    if (
        not math.isfinite(expected_fps)
        or expected_fps <= 0
        or expected_fps > 60
    ):
        fail("Invalid FPS configuration.")

    expected_size = (
        (720, 1280)
        if mode == "short"
        else (1280, 720)
    )

    log("=" * 65)
    log("KATHA LOK AI — PART-ONLY OUTPUT PACKAGER")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("CONFIGURED PARTS:", configured_parts)
    log("SCENES PER PART:", scenes)
    log("FPS:", expected_fps)

    validation, actual_parts = validate_validation_report(
        mode,
        configured_parts,
        scenes,
    )

    render_source = read_json(RENDER)

    part_numbers = get_part_numbers(
        render_source,
        validation,
        configured_parts,
        actual_parts,
    )

    if len(part_numbers) != actual_parts:
        fail("Selected part numbering does not match actual part count.")

    render = validate_render_manifest(
        mode,
        part_numbers,
        scenes,
        expected_size,
        expected_fps,
    )

    thumbnail, thumbnail_info = validate_thumbnail(
        mode,
        expected_size,
    )

    metadata, metadata_json, tags, metadata_info = validate_metadata(mode)
    captions, caption_info = validate_srt()

    title = read_story_title()
    slug = safe_slug(title)

    archive_path = ROOT / f"katha-lok-{slug}-{mode}-output.zip"

    files = collect_package_files(
        render,
        thumbnail,
        metadata,
        metadata_json,
        tags,
        captions,
    )

    log("STORY TITLE:", title)
    log("ACTUAL PARTS:", actual_parts)
    log("PART NUMBERS:", part_numbers)
    log("MAIN SCENES:", render["main_scene_count"])
    log("EXTRA ROLE SCENES:", render["extra_role_scene_count"])
    log("PART VIDEOS:", len(render["parts"]))
    log("PACKAGE FILES:", len(files))

    # Preserve the previous valid archive until the replacement is verified.
    temporary_archive = archive_path.with_name(
        archive_path.name + ".verified.tmp"
    )

    temporary_archive.unlink(missing_ok=True)

    try:
        records = build_archive(
            temporary_archive,
            files,
            render,
        )

        zip_validation = verify_packaged_archive(
            temporary_archive,
            mode,
            part_numbers,
            expected_size,
            expected_fps,
        )

        os.replace(temporary_archive, archive_path)

    finally:
        temporary_archive.unlink(missing_ok=True)

    archive_hash = sha256_file(archive_path)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "story_title": title,
        "story_slug": slug,
        "parts": actual_parts,
        "part_numbers": part_numbers,
        "configured_parts": configured_parts,
        "dynamic_short_parts": mode == "short",
        "scenes_per_part": scenes,
        "expected_scenes": actual_parts * scenes,
        "main_scene_count": render["main_scene_count"],
        "extra_scene_count": render["extra_role_scene_count"],
        "part_count": len(render["parts"]),
        "part_videos_only": True,
        "combined_video_included": False,
        "format_alias_included": False,
        "video_dimensions": list(expected_size),
        "expected_fps": expected_fps,
        "thumbnail_dimensions": thumbnail_info["dimensions"],
        "thumbnail": thumbnail_info,
        "metadata": {
            "text": relative_output(metadata),
            "json": relative_output(metadata_json),
            "tags": relative_output(tags),
            **metadata_info,
        },
        "captions": caption_info,
        "validation_report": relative_output(VALIDATION),
        "render_manifest": relative_output(RENDER),
        "part_files": render["parts"],
        "part_duration_total": render["part_duration_total"],
        "scene_duration_total": render["scene_duration_total"],
        "archive": archive_path.name,
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": archive_hash,
        "file_count": len(records),
        "files": records,
        "zip_integrity": "valid",
        "manifest_zip_consistency": "valid",
        "zip_validation": zip_validation,
        "validation_checks": validation["checks"],
    }

    write_json(REPORT, report)
    verify_package_report(report, archive_path)

    log("-" * 65)
    log("PACKAGE STATUS: completed")
    log("FORMAT:", mode)
    log("STORY:", title)
    log("PARTS:", actual_parts)
    log("PART VIDEOS ONLY: yes")
    log("COMBINED VIDEO: excluded")
    log("FORMAT ALIAS: excluded")
    log("ZIP:", archive_path.name)
    log("ZIP SIZE:", archive_path.stat().st_size)
    log("FILES:", len(records))
    log("ZIP CRC: valid")
    log("PART VIDEO HASHES: valid")
    log("THUMBNAIL:", thumbnail_info["dimensions"])
    log("REPORT:", REPORT.relative_to(ROOT))
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGE ERROR:", str(exc))
        raise