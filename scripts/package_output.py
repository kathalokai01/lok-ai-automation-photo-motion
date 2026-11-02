#!/usr/bin/env python3
"""Validate and package the selected-format Katha Lok AI Photo Motion output."""

from __future__ import annotations

import hashlib
import json
import math
import os
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
ARCHIVE = ROOT / "katha-lok-ai-photo-motion-output.zip"
REPORT = OUT / "package_manifest.json"
VALIDATION = OUT / "validation_report.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
INPUT_FILE = ROOT / "Input" / "topic.txt"

VERSION = "output-packager-v60-role-aware-portable-integrity"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
FPS_TOLERANCE = 0.03
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120
ZIP_COMPRESSION_LEVEL = 6

EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules",
}

EXCLUDED_SUFFIXES = {
    ".zip", ".tmp", ".lock", ".pyc", ".part",
    ".download", ".rendering", ".joining", ".building",
}

# These source-only fields are deliberately removed from the portable
# render manifest. The final captioned video and scene clips remain.
SOURCE_ONLY_PATH_FIELDS = {
    "source_rendered_video",
    "source_photo_motion_video",
}

PATH_FIELDS = {
    "video", "output", "path", "final_video", "alias",
    "pre_caption_video", "caption_manifest", "final_srt",
    "source_video", "source_photo_motion_video",
    "source_rendered_video", "source_audio",
    "narration", "audio", "image", "image_path",
    "depth_map", "depth_map_path", "thumbnail",
    "thumbnail_path", "source_photo_manifest",
    "source_audio_manifest", "source_sound_manifest",
    "source_sfx_validation", "file", "audio_path",
    "source_image", "source_manifest", "video_path",
    "audio_file", "image_file", "font_path",
    "caption_font_file", "srt", "manifest",
}

REQUIRED_PACKAGE_MEMBERS = {
    "output/thumbnail.jpg",
    "output/youtube_metadata.txt",
    "output/publish/youtube_metadata.json",
    "output/publish/youtube_tags.txt",
    "output/captions/final_captions.srt",
    "output/validation_report.json",
    "output/scene_renders/render_manifest.json",
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


def inside_output(path: Path) -> Path:
    resolved = Path(path).resolve()

    try:
        resolved.relative_to(OUT.resolve())
    except ValueError:
        fail(f"Path escapes output directory: {resolved}")

    return resolved


def output_path(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path is missing.")

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
        fail("ffprobe is required but was not found.")

    try:
        result = subprocess.run(
            [
                executable, "-v", "error",
                "-show_entries",
                "format=duration:"
                "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
                "-of", "json", str(path),
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
            f"{result.stderr[-700:]}"
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

    if video is None or audio is None:
        fail(f"Audio or video stream missing: {path.name}")

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
        "has_audio": True,
    }


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
        reported_parts = int(data["parts"])
    except (KeyError, TypeError, ValueError):
        fail("Validation report part count is invalid.")

    if reported_parts < 1:
        fail("Validation report part count must be positive.")

    if mode == "short":
        declared = data.get("configured_parts")

        if declared is not None:
            try:
                declared = int(declared)
            except (TypeError, ValueError):
                fail("Validation report configured_parts is invalid.")

            if declared != configured_parts:
                fail("Validation report configured_parts mismatch.")

        if data.get("dynamic_short_parts") is not True:
            fail("SHORT report does not confirm dynamic part count.")

        actual_parts = reported_parts
    else:
        if reported_parts != configured_parts:
            fail("FULL validation report part count mismatch.")
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
                f"expected {expected}, found {actual}"
            )

    checks = data.get("checks")
    if not isinstance(checks, dict):
        fail("Validation report checks are missing.")

    required = (
        "photo_motion",
        "narration",
        "sound_design",
        "render",
        "captions",
        "thumbnail",
        "metadata",
    )

    for name in required:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Validation check missing or invalid: {name}")

    return data, actual_parts


def normalize_render_rows(data: dict) -> list[dict]:
    rows = data.get("all_completed_scenes")

    if not isinstance(rows, list):
        main_rows = data.get("completed_scenes", [])
        extra_rows = data.get("extra_scenes", [])

        if not isinstance(main_rows, list):
            fail("Render completed_scenes must be a list.")
        if not isinstance(extra_rows, list):
            fail("Render extra_scenes must be a list.")

        rows = list(main_rows) + list(extra_rows)

    if not rows:
        fail("Render manifest contains no completed scene rows.")

    result = []
    seen_ids = set()

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render scene row: {index}")

        scene_id = str(row.get("scene_id", "")).strip()
        if not scene_id:
            fail(f"Render scene {index} has no scene_id.")

        if scene_id in seen_ids:
            fail(f"Duplicate render scene_id: {scene_id}")

        seen_ids.add(scene_id)
        result.append(row)

    return result


def validate_render_manifest(
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    data = read_json(RENDER)

    if data.get("status") != "completed":
        fail("Render manifest is not completed.")

    if str(data.get("format", "")).strip().lower() != mode:
        fail("Render manifest FORMAT mismatch.")

    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions.")

    try:
        manifest_fps = float(data.get("fps"))
    except (TypeError, ValueError):
        fail("Render manifest FPS is missing or invalid.")

    if (
        not math.isfinite(manifest_fps)
        or abs(manifest_fps - expected_fps) > FPS_TOLERANCE
    ):
        fail("Render manifest FPS does not match Input/topic.txt.")

    all_rows = normalize_render_rows(data)
    main_rows = [row for row in all_rows if row.get("role") == "main"]
    extra_rows = [row for row in all_rows if row.get("role") != "main"]

    expected_main_count = parts * scenes
    if len(main_rows) != expected_main_count:
        fail(
            f"Main scene count mismatch: "
            f"{len(main_rows)}/{expected_main_count}"
        )

    declared_count = data.get("completed_count")
    if declared_count is not None:
        try:
            if int(declared_count) != expected_main_count:
                fail("Render completed_count does not match main scenes.")
        except (TypeError, ValueError):
            fail("Render completed_count is invalid.")

    declared_all_count = data.get("all_completed_scene_count")
    if declared_all_count is not None:
        try:
            if int(declared_all_count) != len(all_rows):
                fail("Render all_completed_scene_count mismatch.")
        except (TypeError, ValueError):
            fail("Render all_completed_scene_count is invalid.")

    scene_files = []
    scene_records = []
    extra_records = []
    scene_duration_total = 0.0

    for index, row in enumerate(main_rows, 1):
        expected_part = (index - 1) // scenes + 1
        expected_local = (index - 1) % scenes + 1

        try:
            mapping = (
                int(row.get("part", 0)),
                int(row.get("local_scene", row.get("scene", 0))),
                int(row.get("global_scene", index)),
            )
        except (TypeError, ValueError):
            fail(f"Invalid main scene mapping: {index}")

        if mapping != (expected_part, expected_local, index):
            fail(f"Main scene order mismatch: {index}")

    role_counts: dict[str, dict[int, int]] = {}

    for index, row in enumerate(all_rows, 1):
        role = str(row.get("role") or row.get("scene_type") or "").lower()
        if role not in {"main", "hook", "suspense", "resolution"}:
            fail(f"Unknown scene role in render manifest: {role!r}")

        try:
            part_number = int(row.get("part", 0))
        except (TypeError, ValueError):
            fail(f"Invalid part number in render row {index}.")

        if not 1 <= part_number <= parts:
            fail(f"Scene role part is outside selected range: {part_number}")

        if role != "main":
            role_counts.setdefault(role, {})
            role_counts[role][part_number] = (
                role_counts[role].get(part_number, 0) + 1
            )

        if row.get("status") != "completed":
            fail(f"Render scene is not completed: {row.get('scene_id')}")

        if row.get("captions_burned") is not True:
            fail(
                f"Captions are not confirmed for scene "
                f"{row.get('scene_id')}."
            )

        video_value = row.get("video") or row.get("output")
        path = output_path(
            video_value,
            f"Rendered scene {row.get('scene_id')}",
            MIN_VIDEO_BYTES,
        )
        info = probe_media(path, expected_size, expected_fps)
        checksum = sha256_file(path)

        declared_hash = row.get("sha256") or row.get("video_sha256")
        if declared_hash and declared_hash != checksum:
            fail(f"Rendered scene SHA256 mismatch: {row.get('scene_id')}")

        record = {
            "scene_id": row["scene_id"],
            "role": role,
            "part": part_number,
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": checksum,
        }

        if role == "main":
            scene_files.append(path)
            scene_records.append(record)
        else:
            extra_records.append(record)

        scene_duration_total += info["duration"]

    for part_number in range(1, parts + 1):
        if role_counts.get("hook", {}).get(part_number, 0) != 1:
            fail(f"Part {part_number} must have exactly one Hook scene.")

    for part_number in range(1, parts):
        if role_counts.get("suspense", {}).get(part_number, 0) != 1:
            fail(
                f"Non-final part {part_number} must have exactly one "
                "Suspense scene."
            )

    if any(role_counts.get("suspense", {}).get(parts, 0) for _ in [0]):
        fail("The final part must not contain a Suspense scene.")

    if role_counts.get("resolution", {}).get(parts, 0) != 1:
        fail("The final part must have exactly one Resolution scene.")

    for role, counts in role_counts.items():
        for part_number, count in counts.items():
            if role in {"hook", "resolution"} and count != 1:
                fail(f"Duplicate {role} scene in part {part_number}.")
            if role == "suspense" and count != 1:
                fail(f"Duplicate Suspense scene in part {part_number}.")

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Expected exactly {parts} rendered part videos.")

    part_files = []
    part_records = []
    part_duration_total = 0.0

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                part_number = int(row.get("part", index))
            except (TypeError, ValueError):
                fail(f"Invalid rendered part number: {index}")

            if part_number != index:
                fail(f"Rendered part order mismatch: {index}")

            if row.get("status") not in (None, "completed"):
                fail(f"Rendered part is incomplete: {index}")

            value = row.get("path") or row.get("video") or row.get("output")
            declared_hash = row.get("sha256")
        else:
            value = row
            declared_hash = None

        path = output_path(value, f"Rendered part {index}", MIN_VIDEO_BYTES)
        info = probe_media(path, expected_size, expected_fps)
        checksum = sha256_file(path)

        if declared_hash and declared_hash != checksum:
            fail(f"Rendered part SHA256 mismatch: {index}")

        part_files.append(path)
        part_duration_total += info["duration"]
        part_records.append({
            "part": index,
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": checksum,
        })

    final_video = output_path(
        data.get("final_video"), "Final video", MIN_VIDEO_BYTES
    )
    alias = output_path(data.get("alias"), "Format alias", MIN_VIDEO_BYTES)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    if alias.name != expected_alias:
        fail(f"Wrong format alias: expected {expected_alias}")

    if final_video.resolve() == alias.resolve():
        fail("Final video and format alias must be separate files.")

    final_info = probe_media(final_video, expected_size, expected_fps)
    alias_info = probe_media(alias, expected_size, expected_fps)
    final_hash = sha256_file(final_video)
    alias_hash = sha256_file(alias)

    if final_hash != alias_hash:
        fail("Final video and format alias contents differ.")

    if abs(final_info["duration"] - alias_info["duration"]) > 0.25:
        fail("Final video and alias durations differ.")

    if abs(final_info["duration"] - part_duration_total) > max(
        DURATION_TOLERANCE, float(parts)
    ):
        fail("Final video duration differs from combined part duration.")

    if abs(final_info["duration"] - scene_duration_total) > max(
        DURATION_TOLERANCE, float(len(all_rows))
    ):
        fail("Final video duration differs from all rendered scene durations.")

    return {
        "data": data,
        "all_rows": all_rows,
        "scene_files": scene_files,
        "scene_records": scene_records,
        "extra_records": extra_records,
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
        "output/thumbnail.jpg", "Thumbnail", MIN_IMAGE_BYTES
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
            f"Thumbnail must be a 1280x720 JPEG; "
            f"found {image_format} {dimensions}"
        )

    manifest_path = OUT / "thumbnail" / "thumbnail_manifest.json"
    if not manifest_path.is_file():
        fail("Thumbnail manifest is missing.")

    manifest = read_json(manifest_path)

    if manifest.get("status") != "completed":
        fail("Thumbnail manifest is not completed.")

    if str(manifest.get("format", "")).strip().lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch.")

    declared = (
        manifest.get("thumbnail")
        or manifest.get("path")
        or manifest.get("output")
    )

    if declared:
        declared_path = output_path(
            declared, "Thumbnail manifest output", MIN_IMAGE_BYTES
        )
        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to a different thumbnail.")

    declared_hash = None
    items = manifest.get("thumbnails", [])
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("file") in {
                "output/thumbnail.jpg", "thumbnail.jpg"
            }:
                declared_hash = item.get("sha256")
                break

    actual_hash = sha256_file(path)
    if declared_hash and declared_hash != actual_hash:
        fail("Thumbnail SHA256 does not match its manifest.")

    return path


def parse_text_tags(value: str) -> list[str]:
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


def validate_metadata(mode: str) -> tuple[Path, Path, Path]:
    text_path = output_path(
        "output/youtube_metadata.txt", "YouTube metadata text", 10
    )
    json_path = output_path(
        "output/publish/youtube_metadata.json",
        "YouTube metadata JSON", 10,
    )
    tags_path = output_path(
        "output/publish/youtube_tags.txt", "YouTube tags text", 5
    )

    try:
        content = text_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read YouTube metadata text: {exc}")

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
    tags_text = " ".join(sections.get("TAGS", [])).strip()

    if not title or not description or not tags_text:
        fail("Metadata text must contain TITLE, DESCRIPTION and TAGS.")

    if len(title) > 100:
        fail("YouTube title exceeds 100 characters.")

    if len(description) > 5000:
        fail("YouTube description exceeds 5000 characters.")

    metadata = read_json(json_path)

    if metadata.get("status") != "completed":
        fail("Structured metadata status is not completed.")

    if str(metadata.get("format", "")).strip().lower() != mode:
        fail("Structured metadata FORMAT mismatch.")

    json_title = metadata.get("title")
    json_description = metadata.get("description")
    tags = metadata.get("tags")

    if not isinstance(json_title, str) or not json_title.strip():
        fail("Structured metadata title is missing.")

    if not isinstance(json_description, str) or not json_description.strip():
        fail("Structured metadata description is missing.")

    if len(json_title) > 100 or len(json_description) > 5000:
        fail("Structured metadata exceeds YouTube text limits.")

    if not isinstance(tags, list) or not 8 <= len(tags) <= 20:
        fail("Metadata must contain 8–20 tags.")

    normalized_tags = []
    seen = set()

    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            fail("Metadata contains an invalid tag.")

        normalized = " ".join(tag.split()).strip(" ,#")
        if not normalized:
            fail("Metadata contains an empty tag.")

        if len(normalized) > 100:
            fail("Metadata tag exceeds 100 characters.")

        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            normalized_tags.append(normalized)

    if not 8 <= len(normalized_tags) <= 20:
        fail("Metadata must contain 8–20 unique tags.")

    if json_title.strip() != title:
        fail("Metadata text and JSON titles differ.")

    if json_description.strip() != description:
        fail("Metadata text and JSON descriptions differ.")

    combined_tags = parse_text_tags(tags_text)
    dedicated_tags = parse_text_tags(
        tags_path.read_text(encoding="utf-8-sig")
    )
    expected_tags = {item.casefold() for item in normalized_tags}

    if {item.casefold() for item in combined_tags} != expected_tags:
        fail("Combined metadata text tags and JSON tags differ.")

    if {item.casefold() for item in dedicated_tags} != expected_tags:
        fail("Dedicated tags file and JSON tags differ.")

    if len(combined_tags) != len(normalized_tags):
        fail("Combined metadata contains duplicate or missing tags.")

    if len(dedicated_tags) != len(normalized_tags):
        fail("Dedicated tags file contains duplicate or missing tags.")

    return text_path, json_path, tags_path


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
        fail("Final SRT contains no timing cues.")

    blocks = [
        block.strip()
        for block in content.replace("\r\n", "\n").split("\n\n")
        if block.strip()
    ]

    cue_count = 0
    previous_start = -1.0

    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            continue

        left, right = [item.strip() for item in lines[1].split("-->", 1)]

        def to_seconds(value: str) -> float:
            value = value.replace(",", ".")
            hh, mm, ss = value.split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(ss)

        try:
            start = to_seconds(left)
            end = to_seconds(right)
        except (ValueError, TypeError):
            fail("Final SRT contains an invalid timestamp.")

        if start < 0 or end <= start:
            fail("Final SRT contains an invalid cue duration.")

        if start + 0.001 < previous_start:
            fail("Final SRT cues are not in chronological order.")

        if not any(line.strip() for line in lines[2:]):
            fail("Final SRT contains an empty caption cue.")

        previous_start = start
        cue_count += 1

    if cue_count < 1:
        fail("Final SRT contains no valid caption cues.")

    return path


def collect_output_files(
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
        render["final_video"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(),
        metadata.resolve(),
        metadata_json.resolve(),
        tags.resolve(),
        captions.resolve(),
        *(path.resolve() for path in render["scene_files"]),
        *(path.resolve() for path in render["part_files"]),
    }

    for row in render["extra_records"]:
        required.add((OUT / row["path"]).resolve())

    selected = set(required)
    allowed_videos = {
        path.resolve()
        for path in (
            render["scene_files"]
            + render["part_files"]
            + [render["final_video"], render["alias"]]
        )
    }
    expected_alias = (
        "katha_lok_ai_short.mp4"
        if render["data"]["format"] == "short"
        else "katha_lok_ai_full.mp4"
    )
    opposite_alias = (
        "katha_lok_ai_full.mp4"
        if render["data"]["format"] == "short"
        else "katha_lok_ai_short.mp4"
    )

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

        # Do not ship stale or wrong-format video files.
        if resolved.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
            if resolved not in allowed_videos:
                continue

        if resolved.name == opposite_alias:
            continue

        selected.add(resolved)

    missing = [path for path in required if not path.is_file()]
    if missing:
        fail(f"Required package files are missing: {missing}")

    files = sorted(selected, key=lambda item: item.as_posix())
    names = [archive_name(path) for path in files]

    if len(names) != len(set(names)):
        fail("Duplicate paths detected while collecting package files.")

    return files


def remap_manifest_paths(
    value: Any,
    packaged_names: set[str],
    context: str,
) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in SOURCE_ONLY_PATH_FIELDS:
                value.pop(key, None)
                continue

            if key in PATH_FIELDS and isinstance(item, str):
                if item.startswith(("http://", "https://")):
                    continue

                if not item.strip():
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

                if not resolved.exists():
                    fail(f"{context}: referenced file is missing: {item}")

                if not resolved.is_file():
                    fail(f"{context}: referenced path is not a file: {item}")

                member = archive_name(resolved)
                if member not in packaged_names:
                    fail(f"{context}: referenced file not included: {member}")

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

    remap_manifest_paths(data, packaged_names, "render manifest")

    final_member = archive_name(render["final_video"])
    alias_member = archive_name(render["alias"])

    if final_member not in packaged_names:
        fail("Final video is not included in ZIP.")

    if alias_member not in packaged_names:
        fail("Format alias is not included in ZIP.")

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


def validate_zip_structure(archive: zipfile.ZipFile) -> list[str]:
    bad_member = archive.testzip()
    if bad_member:
        fail(f"ZIP CRC verification failed: {bad_member}")

    members = archive.namelist()
    if len(members) != len(set(members)):
        fail("ZIP contains duplicate file paths.")

    for name in members:
        pure = PurePosixPath(name)
        if (
            name.startswith("/")
            or "\\" in name
            or ".." in pure.parts
            or not pure.parts
        ):
            fail(f"ZIP contains an unsafe path: {name}")

    return members


def build_archive(files: list[Path], render: dict) -> list[dict]:
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)

    packaged_names = {archive_name(path) for path in files}
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
                member = archive_name(path)

                if path.resolve() == RENDER.resolve():
                    payload = (
                        json.dumps(
                            portable_render_manifest(render, packaged_names),
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
            validate_zip_structure(archive)

            for record in records:
                payload = archive.read(record["path"])
                if len(payload) != record["bytes"]:
                    fail(f"ZIP size verification failed: {record['path']}")
                if sha256_bytes(payload) != record["sha256"]:
                    fail(f"ZIP hash verification failed: {record['path']}")

        return records

    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
            fail(f"{label}: packaged video is too small.")

        info = probe_media(temporary_path, expected_size, expected_fps)

        return {
            "duration": round(info["duration"], 3),
            "fps": round(info["fps"], 4),
            "bytes": temporary_path.stat().st_size,
            "sha256": sha256_file(temporary_path),
        }
    finally:
        temporary_path.unlink(missing_ok=True)


def verify_packaged_archive(
    archive_path: Path,
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    if (
        not archive_path.is_file()
        or archive_path.stat().st_size < MIN_VIDEO_BYTES
    ):
        fail("Final ZIP is missing or too small.")

    with zipfile.ZipFile(archive_path, "r") as archive:
        members = validate_zip_structure(archive)
        names = set(members)

        missing = REQUIRED_PACKAGE_MEMBERS - names
        if missing:
            fail(f"Required deliverables missing from ZIP: {sorted(missing)}")

        render_member = "output/scene_renders/render_manifest.json"

        try:
            data = json.loads(archive.read(render_member).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            fail(f"Packaged render manifest is invalid: {exc}")

        if not isinstance(data, dict):
            fail("Packaged render manifest must be a JSON object.")

        if data.get("status") != "completed":
            fail("Packaged render status mismatch.")

        if str(data.get("format", "")).lower() != mode:
            fail("Packaged render FORMAT mismatch.")

        if data.get("captions_burned") is not True:
            fail("Packaged render manifest does not confirm captions.")

        try:
            manifest_fps = float(data.get("fps"))
        except (TypeError, ValueError):
            fail("Packaged render FPS is missing.")

        if (
            not math.isfinite(manifest_fps)
            or abs(manifest_fps - expected_fps) > FPS_TOLERANCE
        ):
            fail("Packaged render FPS mismatch.")

        rows = normalize_render_rows(data)
        main_rows = [row for row in rows if row.get("role") == "main"]
        extra_rows = [row for row in rows if row.get("role") != "main"]

        expected_count = parts * scenes
        if len(main_rows) != expected_count:
            fail("Packaged main scene count mismatch.")

        if len(extra_rows) != (2 * parts):
            fail("Packaged Hook/Suspense/Resolution scene count mismatch.")

        with tempfile.TemporaryDirectory(
            prefix="katha-lok-package-verify-"
        ) as temporary_directory:
            all_scene_durations = []
            role_counts: dict[str, dict[int, int]] = {}

            for index, row in enumerate(rows, 1):
                role = str(row.get("role") or row.get("scene_type") or "").lower()

                if role not in {"main", "hook", "suspense", "resolution"}:
                    fail(f"Unknown packaged scene role: {role!r}")

                try:
                    part_number = int(row.get("part", 0))
                except (TypeError, ValueError):
                    fail(f"Invalid packaged scene part: {index}")

                if not 1 <= part_number <= parts:
                    fail(f"Packaged scene part out of range: {index}")

                if role != "main":
                    role_counts.setdefault(role, {})
                    role_counts[role][part_number] = (
                        role_counts[role].get(part_number, 0) + 1
                    )

                if row.get("status") != "completed":
                    fail(f"Packaged scene is incomplete: {index}")

                if row.get("captions_burned") is not True:
                    fail(f"Packaged scene captions missing: {index}")

                member = row.get("video") or row.get("output")
                info = verify_zip_video(
                    archive,
                    member,
                    f"Packaged scene {index}",
                    expected_size,
                    expected_fps,
                    temporary_directory,
                )

                declared_hash = row.get("sha256")
                if declared_hash and declared_hash != info["sha256"]:
                    fail(f"Packaged scene SHA256 mismatch: {index}")

                all_scene_durations.append(info["duration"])

            for part_number in range(1, parts + 1):
                if role_counts.get("hook", {}).get(part_number, 0) != 1:
                    fail(f"Packaged part {part_number} Hook scene missing.")

            for part_number in range(1, parts):
                if role_counts.get("suspense", {}).get(part_number, 0) != 1:
                    fail(
                        f"Packaged part {part_number} Suspense scene missing."
                    )

            if role_counts.get("suspense", {}).get(parts, 0):
                fail("Final part contains a packaged Suspense scene.")

            if role_counts.get("resolution", {}).get(parts, 0) != 1:
                fail("Final part Resolution scene missing.")

            part_rows = data.get("parts")
            if not isinstance(part_rows, list) or len(part_rows) != parts:
                fail("Packaged part count mismatch.")

            part_durations = []

            for index, row in enumerate(part_rows, 1):
                if isinstance(row, dict):
                    try:
                        part_number = int(row.get("part", index))
                    except (TypeError, ValueError):
                        fail(f"Invalid packaged part number: {index}")

                    if part_number != index:
                        fail(f"Packaged part order mismatch: {index}")

                    if row.get("status") not in (None, "completed"):
                        fail(f"Packaged part incomplete: {index}")

                    member = (
                        row.get("path")
                        or row.get("video")
                        or row.get("output")
                    )
                    declared_hash = row.get("sha256")
                else:
                    member = row
                    declared_hash = None

                info = verify_zip_video(
                    archive,
                    member,
                    f"Packaged part {index}",
                    expected_size,
                    expected_fps,
                    temporary_directory,
                )

                if declared_hash and declared_hash != info["sha256"]:
                    fail(f"Packaged part SHA256 mismatch: {index}")

                part_durations.append(info["duration"])

            final_member = data.get("final_video")
            alias_member = data.get("alias")

            if final_member == alias_member:
                fail("Packaged final video and alias must be separate ZIP entries.")

            final_info = verify_zip_video(
                archive, final_member, "Packaged final video",
                expected_size, expected_fps, temporary_directory,
            )
            alias_info = verify_zip_video(
                archive, alias_member, "Packaged format alias",
                expected_size, expected_fps, temporary_directory,
            )

            expected_alias = (
                "katha_lok_ai_short.mp4"
                if mode == "short"
                else "katha_lok_ai_full.mp4"
            )

            if PurePosixPath(alias_member).name != expected_alias:
                fail(f"Packaged alias mismatch: expected {expected_alias}")

            if final_info["sha256"] != alias_info["sha256"]:
                fail("Packaged final video and alias content differ.")

            if abs(final_info["duration"] - alias_info["duration"]) > 0.25:
                fail("Packaged final video and alias durations differ.")

            if abs(
                final_info["duration"] - sum(part_durations)
            ) > max(DURATION_TOLERANCE, float(parts)):
                fail("Packaged final duration differs from part durations.")

            if abs(
                final_info["duration"] - sum(all_scene_durations)
            ) > max(DURATION_TOLERANCE, float(len(rows))):
                fail("Packaged final duration differs from scene durations.")

        opposite = (
            "katha_lok_ai_full.mp4"
            if mode == "short"
            else "katha_lok_ai_short.mp4"
        )

        if any(PurePosixPath(name).name == opposite for name in names):
            fail(f"Opposite-format video included in ZIP: {opposite}")

        if any(name.lower().endswith(".zip") for name in names):
            fail("Nested ZIP files are not allowed.")

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

        return {
            "status": "valid",
            "entry_count": len(names),
            "crc": "valid",
            "manifest_references": "valid",
            "scene_videos": len(main_rows),
            "extra_scene_videos": len(extra_rows),
            "part_videos": parts,
            "final_video": final_member,
            "alias": alias_member,
            "final_video_sha256": final_info["sha256"],
            "alias_sha256": alias_info["sha256"],
            "final_video_duration": final_info["duration"],
            "part_duration_total": round(sum(part_durations), 3),
            "all_scene_duration_total": round(sum(all_scene_durations), 3),
        }


def verify_package_report(report: dict, archive: Path) -> None:
    saved = read_json(REPORT)

    if saved.get("status") != "completed":
        fail("Package report status verification failed.")

    if saved.get("version") != VERSION:
        fail("Package report version verification failed.")

    if saved.get("format") != report.get("format"):
        fail("Package report FORMAT verification failed.")

    if int(saved.get("file_count", 0)) != int(report.get("file_count", -1)):
        fail("Package report file_count verification failed.")

    if saved.get("zip_integrity") != "valid":
        fail("Package report ZIP integrity verification failed.")

    if saved.get("archive_sha256") != sha256_file(archive):
        fail("Package report archive hash verification failed.")

    if saved.get("archive_bytes") != archive.stat().st_size:
        fail("Package report archive size verification failed.")

    if saved.get("zip_validation", {}).get("status") != "valid":
        fail("Package report ZIP validation status is invalid.")

    for key in (
        "parts", "configured_parts", "scenes_per_part",
        "expected_scenes", "scene_count", "extra_scene_count",
        "part_count",
    ):
        if saved.get(key) != report.get(key):
            fail(f"Package report field mismatch: {key}")


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
        fail("Invalid expected FPS.")

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
        mode, configured_parts, scenes
    )

    expected_count = actual_parts * scenes

    log("ACTUAL PARTS:", actual_parts)
    log("EXPECTED MAIN SCENES:", expected_count)

    render = validate_render_manifest(
        mode, actual_parts, scenes, expected_size, expected_fps
    )

    thumbnail = validate_thumbnail(mode)
    metadata, metadata_json, tags = validate_metadata(mode)
    captions = validate_captions()

    files = collect_output_files(
        render, thumbnail, metadata, metadata_json, tags, captions
    )

    log("FILES TO PACKAGE:", len(files))

    temporary_archive = ARCHIVE.with_name(
        ARCHIVE.name + ".verified.tmp"
    )
    temporary_archive.unlink(missing_ok=True)

    try:
        records = build_archive(files, render)

        generated_archive = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
        if not generated_archive.is_file():
            fail("Temporary ZIP was not created.")

        os.replace(generated_archive, temporary_archive)

        zip_validation = verify_packaged_archive(
            temporary_archive,
            mode,
            actual_parts,
            scenes,
            expected_size,
            expected_fps,
        )

        # Keep the previous successful ZIP until the new ZIP passes
        # CRC, file hash, media stream, FPS, role, and reference checks.
        os.replace(temporary_archive, ARCHIVE)

    finally:
        temporary_archive.unlink(missing_ok=True)
        ARCHIVE.with_name(ARCHIVE.name + ".tmp").unlink(missing_ok=True)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": actual_parts,
        "configured_parts": configured_parts,
        "dynamic_short_parts": mode == "short",
        "scenes_per_part": scenes,
        "expected_scenes": expected_count,
        "extra_scene_count": len(render["extra_records"]),
        "all_scene_count": (
            len(render["scene_records"]) + len(render["extra_records"])
        ),
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
        "tags": relative_output(tags),
        "captions": relative_output(captions),
        "validation_report": relative_output(VALIDATION),
        "render_manifest": relative_output(RENDER),
        "scene_files": render["scene_records"],
        "extra_scene_files": render["extra_records"],
        "part_files": render["part_records"],
        "files": records,
        "zip_integrity": "valid",
        "manifest_zip_consistency": "valid",
        "zip_video_fps_validation": "valid",
        "zip_validation": zip_validation,
        "validation_checks": validation["checks"],
    }

    write_json(REPORT, report)
    verify_package_report(report, ARCHIVE)

    log("-" * 65)
    log("PACKAGE STATUS: completed")
    log("FORMAT:", mode)
    log("PARTS:", actual_parts)
    log("MAIN SCENES:", expected_count)
    log("EXTRA ROLE SCENES:", len(render["extra_records"]))
    log("ZIP:", ARCHIVE.name)
    log("ZIP SIZE:", ARCHIVE.stat().st_size)
    log("FILES:", len(records))
    log("ZIP CRC: valid")
    log("VIDEO FPS: valid")
    log("MANIFEST REFERENCES: valid")
    log("PACKAGED VIDEO HASHES: valid")
    log("REPORT:", REPORT.relative_to(ROOT))
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGE ERROR:", str(exc))
        raise