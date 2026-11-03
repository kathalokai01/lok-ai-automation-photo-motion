#!/usr/bin/env python3
"""Create and verify a story-specific, selected-format Photo Motion ZIP."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from input_config import (
    load_and_validate,
    get_format,
    get_selected_parts,
    get_scenes,
    get_fps,
)

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"
REPORT = OUT / "package_manifest.json"
VALIDATION = OUT / "validation_report.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX_VALIDATION = OUT / "sound_design" / "sfx_timing_validation.json"
CAPTIONS_JSON = OUT / "captions" / "captions.json"
FINAL_SRT = OUT / "captions" / "final_captions.srt"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA_TEXT = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
TAGS = OUT / "publish" / "youtube_tags.txt"

VERSION = "output-packager-v80-selected-format-portable-verified"
MIN_VIDEO_BYTES = 1000
MIN_IMAGE_BYTES = 1000
MIN_TEXT_BYTES = 5
FPS_TOLERANCE = 0.05
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120
ZIP_LEVEL = 6

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
TEMP_SUFFIXES = {
    ".tmp", ".lock", ".pyc", ".part", ".download",
    ".rendering", ".joining", ".building",
}
EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "checkpoints", "cache",
    "temporary", "temp",
}
REQUIRED_CHECKS = (
    "story", "photo_motion", "narration", "sound_design", "render",
    "captions", "thumbnail", "metadata", "unique_output_files",
    "mode_separation",
)
REQUIRED_MEMBERS = {
    "output/thumbnail.jpg",
    "output/thumbnail/thumbnail_manifest.json",
    "output/youtube_metadata.txt",
    "output/publish/youtube_metadata.json",
    "output/publish/youtube_tags.txt",
    "output/captions/final_captions.srt",
    "output/captions/captions.json",
    "output/validation_report.json",
    "output/scene_renders/render_manifest.json",
    "output/photo_motion/photo_motion_manifest.json",
    "output/narration/audio_jobs.json",
    "output/sound_design/sound_design_manifest.json",
    "output/sound_design/sfx_timing_validation.json",
    "output/package_manifest.json",
}


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def clean(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def read_json(path: Path, optional: bool = False) -> dict | None:
    if not path.is_file():
        if optional:
            return None
        fail(f"Required JSON is missing: {path}")
    if path.stat().st_size <= 0:
        fail(f"Required JSON is empty: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def output_path(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: path is missing")
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(OUT.resolve()):
        fail(f"{label}: path must remain inside output/")
    if not resolved.is_file():
        fail(f"{label}: file not found: {resolved}")
    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small")
    return resolved


def output_relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(OUT.resolve()):
        fail(f"Path escapes output/: {resolved}")
    return resolved.relative_to(OUT.resolve()).as_posix()


def parse_fps(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (ValueError, TypeError, ZeroDivisionError):
        fail(f"{label}: invalid FPS")
    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS")
    return result


def probe_video(
    path: Path,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        fail("ffprobe is required but unavailable")
    try:
        result = subprocess.run(
            [
                executable, "-v", "error", "-show_entries",
                "format=duration:"
                "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True,
            timeout=PROBE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        fail(f"ffprobe timed out: {path.name}")
    if result.returncode:
        fail(f"ffprobe failed for {path.name}: {(result.stderr or '')[-700:]}")
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
        (item for item in streams if item.get("codec_type") == "video"), None
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"), None
    )
    if video is None or audio is None:
        fail(f"{path.name}: both video and audio streams are required")
    dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )
    rate = video.get("avg_frame_rate")
    if not rate or rate in {"0/0", "N/A"}:
        rate = video.get("r_frame_rate")
    fps = parse_fps(rate, path.name)
    if dimensions != expected_size:
        fail(f"{path.name}: expected {expected_size}, found {dimensions}")
    if abs(fps - expected_fps) > FPS_TOLERANCE:
        fail(f"{path.name}: expected {expected_fps} FPS, found {fps:.4f}")
    return {
        "duration": duration,
        "width": dimensions[0],
        "height": dimensions[1],
        "fps": fps,
        "has_video": True,
        "has_audio": True,
    }


def normalize_role(row: dict) -> str:
    value = clean(
        row.get("role") or row.get("scene_type") or row.get("type") or "main"
    ).lower()
    aliases = {
        "opening": "hook", "intro": "hook", "part_hook": "hook",
        "story": "main", "main_scene": "main",
        "cliffhanger": "suspense",
        "ending": "resolution", "final": "resolution",
    }
    value = aliases.get(value, value)
    if value not in {"main", "hook", "suspense", "resolution"}:
        fail(f"Unknown scene role: {value}")
    return value


def validate_validation_report(mode: str, parts: int, scenes: int) -> dict:
    data = read_json(VALIDATION)
    if clean(data.get("status")).lower() != "completed":
        fail("Final validation report is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Validation report FORMAT mismatch")
    for key, expected in (
        ("parts", parts),
        ("configured_parts", parts),
        ("scenes_per_part", scenes),
        ("expected_scenes", parts * scenes),
    ):
        try:
            actual = int(data.get(key, -1))
        except (TypeError, ValueError):
            fail(f"Validation report has invalid {key}")
        if actual != expected:
            fail(f"Validation report {key} mismatch")
    checks = data.get("checks")
    if not isinstance(checks, dict):
        fail("Validation report checks are missing")
    for name in REQUIRED_CHECKS:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Validation check missing or invalid: {name}")
    return data


def render_rows(data: dict) -> list[dict]:
    rows = data.get("all_completed_scenes")
    if not isinstance(rows, list):
        main = data.get("completed_scenes") or data.get("scenes") or []
        extra = data.get("extra_scenes") or []
        if not isinstance(main, list) or not isinstance(extra, list):
            fail("Render scene records are invalid")
        rows = list(main) + list(extra)
    if not rows:
        fail("Render manifest contains no scene records")
    seen = set()
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render scene row {index}")
        scene_id = clean(row.get("scene_id") or row.get("id"))
        if not scene_id or scene_id in seen:
            fail(f"Missing or duplicate render scene ID at row {index}")
        seen.add(scene_id)
        result.append(row)
    return result


def validate_role_records(rows: list[dict], parts: int, scenes: int) -> dict:
    expected_main = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }
    actual_main = set()
    role_counts: dict[str, dict[int, int]] = {}
    for row in rows:
        role = normalize_role(row)
        try:
            part = int(row.get("part"))
        except (TypeError, ValueError):
            fail(f"Invalid part number in scene {row.get('scene_id')}")
        if not 1 <= part <= parts:
            fail(f"Scene belongs to an unselected Part: {part}")
        if role == "main":
            try:
                scene = int(row.get("local_scene") or row.get("scene") or 0)
            except (TypeError, ValueError):
                fail(f"Invalid Main scene number: {row.get('scene_id')}")
            key = (part, scene)
            if key in actual_main:
                fail(f"Duplicate Main scene: {key}")
            actual_main.add(key)
        else:
            per_part = role_counts.setdefault(role, {})
            per_part[part] = per_part.get(part, 0) + 1

    if actual_main != expected_main:
        fail(
            "Main scene mapping mismatch: "
            f"missing={sorted(expected_main - actual_main)}, "
            f"extra={sorted(actual_main - expected_main)}"
        )
    for part in range(1, parts + 1):
        if role_counts.get("hook", {}).get(part, 0) != 1:
            fail(f"Part {part} must contain exactly one Hook")
        expected_suspense = 1 if part < parts else 0
        if role_counts.get("suspense", {}).get(part, 0) != expected_suspense:
            fail(f"Part {part} has an invalid Suspense count")
        expected_resolution = 1 if part == parts else 0
        if role_counts.get("resolution", {}).get(part, 0) != expected_resolution:
            fail(f"Part {part} has an invalid Resolution count")

    extra_count = sum(
        count for per_part in role_counts.values() for count in per_part.values()
    )
    if extra_count != parts * 2:
        fail(f"Hook/Suspense/Resolution count mismatch: {extra_count}")
    return {
        "main_scene_count": len(actual_main),
        "extra_role_scene_count": extra_count,
        "role_counts": role_counts,
    }


def validate_render(
    mode: str,
    parts: int,
    scenes: int,
    dimensions: tuple[int, int],
    fps: float,
) -> dict:
    data = read_json(RENDER)
    if clean(data.get("status")).lower() != "completed":
        fail("Render manifest is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Render manifest FORMAT mismatch")
    if data.get("part_outputs_only") is not True:
        fail("Render manifest must confirm Part-only output")
    try:
        width, height = int(data.get("width")), int(data.get("height"))
    except (TypeError, ValueError):
        fail("Render manifest dimensions are invalid")
    if (width, height) != dimensions:
        fail("Render manifest dimensions mismatch")
    manifest_fps = parse_fps(data.get("fps"), "Render manifest")
    if abs(manifest_fps - fps) > FPS_TOLERANCE:
        fail("Render manifest FPS mismatch")
    if data.get("captions_burned") is not True:
        fail("Rendered Part videos must confirm burned-in captions")

    rows = render_rows(data)
    role_report = validate_role_records(rows, parts, scenes)
    scene_records = []
    scene_duration = 0.0
    for row in rows:
        if clean(row.get("status")).lower() not in {
            "completed", "success", "reused"
        }:
            fail(f"Rendered scene is incomplete: {row.get('scene_id')}")
        path = output_path(
            row.get("video") or row.get("output") or row.get("path"),
            f"Rendered scene {row.get('scene_id')}", MIN_VIDEO_BYTES,
        )
        info = probe_video(path, dimensions, fps)
        digest = sha256_file(path)
        declared = row.get("sha256") or row.get("video_sha256")
        if declared and clean(declared).lower() != digest:
            fail(f"Rendered scene SHA256 mismatch: {row.get('scene_id')}")
        role = normalize_role(row)
        scene_records.append({
            "scene_id": clean(row.get("scene_id") or row.get("id")),
            "role": role,
            "part": int(row["part"]),
            "local_scene": (
                int(row.get("local_scene") or row.get("scene") or 0)
                if role == "main" else None
            ),
            "path": output_relative(path),
            "duration": round(info["duration"], 3),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
        scene_duration += info["duration"]

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Render manifest must contain exactly {parts} Part videos")
    checked_parts = []
    part_paths = []
    part_duration = 0.0
    for number, row in enumerate(part_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid rendered Part record: {number}")
        try:
            actual_number = int(row.get("part", -1))
        except (TypeError, ValueError):
            fail(f"Invalid rendered Part number: {number}")
        if actual_number != number:
            fail(f"Rendered Part ordering mismatch at Part {number}")
        if clean(row.get("status")).lower() not in {
            "completed", "success", "reused"
        }:
            fail(f"Rendered Part {number} is incomplete")
        path = output_path(
            row.get("path") or row.get("video") or row.get("output"),
            f"Rendered Part {number}", MIN_VIDEO_BYTES,
        )
        if path.suffix.lower() != ".mp4" or path.name != f"part_{number:02d}.mp4":
            fail(f"Part {number} must be named part_{number:02d}.mp4")
        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(f"Part {number} must be stored in output/parts/")
        if path in part_paths:
            fail(f"Duplicate rendered Part path: {path}")
        info = probe_video(path, dimensions, fps)
        digest = sha256_file(path)
        if row.get("sha256") and clean(row["sha256"]).lower() != digest:
            fail(f"Part {number} SHA256 mismatch")
        if row.get("duration") is not None:
            try:
                declared_duration = float(row["duration"])
            except (TypeError, ValueError):
                fail(f"Part {number} duration is invalid")
            if (
                not math.isfinite(declared_duration)
                or abs(declared_duration - info["duration"]) > DURATION_TOLERANCE
            ):
                fail(f"Part {number} duration mismatch")
        checked_parts.append({
            "part": number,
            "path": output_relative(path),
            "duration": round(info["duration"], 3),
            "width": info["width"],
            "height": info["height"],
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
        part_paths.append(path)
        part_duration += info["duration"]

    tolerance = max(DURATION_TOLERANCE, len(rows) * 0.05)
    if abs(part_duration - scene_duration) > tolerance:
        fail("Part video duration differs from rendered scene duration total")

    for forbidden_key in (
        "final_video", "alias", "combined_video", "combined_output"
    ):
        if data.get(forbidden_key):
            fail(f"Combined/alias video field is forbidden: {forbidden_key}")

    return {
        "source": data,
        "scene_rows": rows,
        "checked_scene_rows": scene_records,
        "parts": checked_parts,
        "part_paths": part_paths,
        "part_duration_total": round(part_duration, 3),
        "scene_duration_total": round(scene_duration, 3),
        **role_report,
    }

def validate_thumbnail(mode: str, dimensions: tuple[int, int]) -> tuple[Path, dict]:
    path = output_path("output/thumbnail.jpg", "Thumbnail", MIN_IMAGE_BYTES)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image_format, actual_dimensions = image.format, image.size
    except Exception as exc:
        fail(f"Thumbnail image is invalid: {exc}")
    if image_format != "JPEG" or actual_dimensions != dimensions:
        fail(
            f"{mode.upper()} thumbnail must be JPEG {dimensions}; "
            f"found {image_format} {actual_dimensions}"
        )
    manifest = read_json(THUMBNAIL_MANIFEST)
    if clean(manifest.get("status")).lower() != "completed":
        fail("Thumbnail manifest is not completed")
    if clean(manifest.get("format")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")
    declared = (
        manifest.get("thumbnail")
        or manifest.get("path")
        or manifest.get("output")
    )
    if declared:
        declared_path = output_path(
            declared, "Thumbnail manifest path", MIN_IMAGE_BYTES
        )
        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to a different image")
    digest = sha256_file(path)
    entries = manifest.get("thumbnails")
    if not isinstance(entries, list) or not entries:
        fail("Thumbnail manifest output list is missing")
    matched = False
    for item in entries:
        if not isinstance(item, dict):
            fail("Invalid thumbnail manifest record")
        file_value = item.get("file")
        if file_value and Path(str(file_value)).name == path.name:
            matched = True
            if item.get("success") is False:
                fail("Main thumbnail is marked unsuccessful")
            if item.get("sha256") and clean(item["sha256"]).lower() != digest:
                fail("Thumbnail SHA256 mismatch")
    if not matched:
        fail("Thumbnail manifest does not list output/thumbnail.jpg")
    return path, {
        "path": "output/thumbnail.jpg",
        "format": image_format,
        "dimensions": list(actual_dimensions),
        "bytes": path.stat().st_size,
        "sha256": digest,
    }


def parse_tags(value: str) -> list[str]:
    raw = value.strip()
    if not raw:
        return []
    pieces = (
        raw.replace("\r", "\n").split("\n")
        if "\n" in raw else raw.split(",")
    )
    result = []
    seen = set()
    for item in pieces:
        tag = re.sub(r"\s+", " ", item).strip(" ,#")
        if not tag:
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def validate_metadata(mode: str) -> tuple[Path, Path, Path, dict]:
    text_path = output_path(
        "output/youtube_metadata.txt", "YouTube metadata text", MIN_TEXT_BYTES
    )
    json_path = output_path(
        "output/publish/youtube_metadata.json",
        "YouTube metadata JSON", MIN_TEXT_BYTES,
    )
    tags_path = output_path(
        "output/publish/youtube_tags.txt",
        "YouTube tags", MIN_TEXT_BYTES,
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
    text_tags = parse_tags(" ".join(sections.get("TAGS", [])))
    if not title or not description or not text_tags:
        fail("Metadata text must contain TITLE, DESCRIPTION and TAGS")
    if len(title) > 100 or len(description) > 5000:
        fail("Metadata exceeds YouTube title/description limits")

    data = read_json(json_path)
    if clean(data.get("status")).lower() != "completed":
        fail("Structured metadata is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Structured metadata FORMAT mismatch")
    json_title = data.get("title")
    json_description = data.get("description")
    tags = data.get("tags")
    if not isinstance(json_title, str) or not isinstance(json_description, str):
        fail("Structured metadata title or description is missing")
    if json_title.strip() != title or json_description.strip() != description:
        fail("Metadata text and JSON content differ")
    if len(json_title.strip()) > 100 or len(json_description.strip()) > 5000:
        fail("Structured metadata exceeds YouTube limits")
    if not isinstance(tags, list):
        fail("Structured metadata tags must be a list")

    normalized = []
    seen = set()
    for item in tags:
        if not isinstance(item, str):
            fail("Metadata contains a non-text tag")
        tag = re.sub(r"\s+", " ", item).strip(" ,#")
        if not tag or len(tag) > 100:
            fail("Metadata contains an invalid tag")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(tag)
    if not 5 <= len(normalized) <= 20:
        fail(f"Metadata requires 5–20 unique tags; found {len(normalized)}")

    expected = {item.casefold() for item in normalized}
    if {item.casefold() for item in text_tags} != expected:
        fail("Metadata text tags differ from structured tags")
    try:
        dedicated_text = tags_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read dedicated YouTube tags: {exc}")
    dedicated = parse_tags(dedicated_text)
    if {item.casefold() for item in dedicated} != expected:
        fail("Dedicated tags differ from structured metadata")
    if len(text_tags) != len(normalized) or len(dedicated) != len(normalized):
        fail("Duplicate tags detected in metadata files")

    return text_path, json_path, tags_path, {
        "title": title,
        "description_sha256": sha256_bytes(description.encode("utf-8")),
        "tags": normalized,
        "tag_count": len(normalized),
    }


def validate_srt() -> tuple[Path, dict]:
    path = output_path(
        "output/captions/final_captions.srt", "Final captions SRT", 10
    )
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read final SRT: {exc}")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [
        item.strip()
        for item in re.split(r"\n[ \t]*\n", content)
        if item.strip()
    ]
    if not blocks:
        fail("Final SRT contains no caption cues")

    def seconds(value: str) -> float:
        match = re.fullmatch(
            r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
            value.strip(),
        )
        if not match:
            fail(f"Invalid SRT timestamp: {value}")
        hours, minutes, secs, millis = map(int, match.groups())
        return hours * 3600 + minutes * 60 + secs + millis / 1000

    previous_end = -1.0
    maximum_end = 0.0
    character_count = 0
    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index} is incomplete")
        if int(lines[0].strip()) != index or lines[1].count("-->") != 1:
            fail(f"SRT cue {index} numbering or timing is invalid")
        left, right = [value.strip() for value in lines[1].split("-->")]
        start, end = seconds(left), seconds(right)
        if end <= start or start < previous_end - 0.001:
            fail(f"SRT cue {index} overlaps or has invalid timing")
        text = "\n".join(lines[2:]).strip()
        if not text:
            fail(f"SRT cue {index} has no caption text")
        tags = re.findall(r"</?([A-Za-z]+)(?:\s+[^>]*)?>", text)
        if any(tag.lower() not in {"b", "i", "u", "font"} for tag in tags):
            fail(f"SRT cue {index} contains unsupported markup")
        previous_end = end
        maximum_end = max(maximum_end, end)
        character_count += len(text)

    captions = read_json(CAPTIONS_JSON)
    if clean(captions.get("status")).lower() != "completed":
        fail("Caption JSON manifest is not completed")
    main_rows = captions.get("captions") or captions.get("scenes")
    if not isinstance(main_rows, list) or not main_rows:
        fail("Caption JSON contains no Main scene captions")
    return path, {
        "path": "output/captions/final_captions.srt",
        "cue_count": len(blocks),
        "text_characters": character_count,
        "last_cue_end": round(maximum_end, 3),
        "sha256": sha256_file(path),
        "caption_manifest": "output/captions/captions.json",
    }


def read_story_title() -> str:
    candidates = (
        OUT / "story" / "story.json",
        OUT / "story" / "ai_story.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        data = read_json(path)
        objects = [data]
        for key in ("story", "result", "data"):
            nested = data.get(key)
            if isinstance(nested, dict):
                objects.append(nested)
        for obj in objects:
            for key in (
                "final_title", "generated_title", "story_title", "title"
            ):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return re.sub(r"\s+", " ", value).strip()
    title_file = OUT / "story" / "final_title.txt"
    if title_file.is_file():
        value = title_file.read_text(encoding="utf-8-sig").strip()
        if value:
            return re.sub(r"\s+", " ", value)
    fail("Story-derived title is missing; generic ZIP names are prohibited")


def safe_slug(title: str) -> str:
    slug = re.sub(r"[^\w]+", "-", title, flags=re.UNICODE).strip("-_")
    slug = slug[:90].strip("-_")
    if not slug or slug in {".", ".."}:
        fail("Story title cannot form a safe ZIP filename")
    return slug


def validate_sound_manifests() -> dict:
    sound = read_json(SOUND)
    if clean(sound.get("status")).lower() != "completed":
        fail("Sound-design manifest is not completed")
    sfx = read_json(SFX_VALIDATION)
    if clean(sfx.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("SFX timing validation manifest is not completed")
    return {
        "sound_design": {
            "path": "output/sound_design/sound_design_manifest.json",
            "sha256": sha256_file(SOUND),
        },
        "sfx_timing_validation": {
            "path": "output/sound_design/sfx_timing_validation.json",
            "sha256": sha256_file(SFX_VALIDATION),
        },
    }


def validate_narration_manifest() -> dict:
    data = read_json(AUDIO)
    if clean(data.get("status")).lower() != "completed":
        fail("Narration audio manifest is not completed")
    jobs = data.get("jobs") or data.get("audio_jobs") or data.get("scenes")
    if not isinstance(jobs, list) or not jobs:
        fail("Narration audio manifest contains no audio jobs")
    incomplete = [
        index for index, item in enumerate(jobs, 1)
        if not isinstance(item, dict)
        or clean(item.get("status")).lower() not in {
            "completed", "success", "reused"
        }
    ]
    if incomplete:
        fail(f"Narration jobs are incomplete: {incomplete[:20]}")
    return {
        "path": "output/narration/audio_jobs.json",
        "job_count": len(jobs),
        "sha256": sha256_file(AUDIO),
    }


def portable_render_manifest(
    source: dict,
    checked_parts: list[dict],
    package_members: set[str],
) -> dict:
    data = json.loads(json.dumps(source))
    for key in (
        "scenes", "completed_scenes", "all_completed_scenes",
        "extra_scenes", "completed_count", "all_completed_scene_count",
        "extra_scene_count", "final_video", "alias", "combined_video",
        "combined_output", "pre_caption_video",
    ):
        data.pop(key, None)

    portable_parts = []
    for item in checked_parts:
        member = "output/" + item["path"]
        if member not in package_members:
            fail(f"Rendered Part missing from archive selection: {member}")
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
    data["part_outputs_only"] = True
    data["part_videos_only"] = True
    data["combined_video_included"] = False
    data["format_alias_included"] = False
    data["package_manifest_view"] = True
    data["package_manifest_version"] = VERSION
    return data


def package_file_list(
    render: dict,
    required_paths: list[Path],
) -> list[Path]:
    part_paths = {path.resolve() for path in render["part_paths"]}
    selected = {path.resolve() for path in required_paths}
    selected.update(part_paths)
    for path in selected:
        if not path.is_file():
            fail(f"Required package file is missing: {path}")
        if path.is_symlink():
            fail(f"Symlink is forbidden in package: {path}")
        if not path.resolve().is_relative_to(OUT.resolve()):
            fail(f"Package file escapes output/: {path}")
        if path.suffix.lower() in VIDEO_SUFFIXES and path.resolve() not in part_paths:
            fail(f"Non-Part video is forbidden: {path}")
        if path.suffix.lower() in IMAGE_SUFFIXES and path.resolve() != (
            THUMBNAIL.resolve()
        ):
            fail(f"Unrequested image asset is forbidden: {path}")
        if path.suffix.lower() in AUDIO_SUFFIXES:
            fail(f"Unrequested audio asset is forbidden: {path}")
        if path.suffix.lower() == ".zip":
            fail(f"Nested ZIP is forbidden: {path}")
        if path.name.endswith(".tmp") or path.suffix.lower() in TEMP_SUFFIXES:
            fail(f"Temporary file is forbidden: {path}")

    names = ["output/" + output_relative(path) for path in selected]
    if len(names) != len(set(names)):
        fail("Duplicate package paths detected")
    return sorted(selected, key=lambda path: output_relative(path))


def validate_zip_member(name: str) -> None:
    member = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or not member.parts
        or member.parts[0] != "output"
        or any(piece in {"", ".", ".."} for piece in member.parts)
    ):
        fail(f"Unsafe ZIP member path: {name!r}")
    if member.suffix.lower() == ".zip":
        fail(f"Nested ZIP is forbidden: {name}")


def validate_zip_structure(archive: zipfile.ZipFile) -> list[str]:
    bad = archive.testzip()
    if bad:
        fail(f"ZIP CRC verification failed: {bad}")
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if not names:
        fail("ZIP archive is empty")
    if len(names) != len(set(names)):
        fail("ZIP contains duplicate entries")
    for info in infos:
        validate_zip_member(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            fail(f"ZIP symlink is forbidden: {info.filename}")
        if info.flag_bits & 0x1:
            fail(f"Encrypted ZIP member is forbidden: {info.filename}")
        if info.file_size <= 0:
            fail(f"Empty ZIP member is forbidden: {info.filename}")
    return names

def build_embedded_manifest(
    mode: str,
    title: str,
    slug: str,
    archive_name: str,
    part_records: list[dict],
    file_records: list[dict],
    validation: dict,
    thumbnail_info: dict,
    metadata_info: dict,
    caption_info: dict,
    sound_info: dict,
    narration_info: dict,
) -> dict:
    return {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "story_title": title,
        "story_slug": slug,
        "archive": archive_name,
        "parts": len(part_records),
        "part_numbers": [item["part"] for item in part_records],
        "part_videos_only": True,
        "combined_video_included": False,
        "format_alias_included": False,
        "part_files": part_records,
        "thumbnail": thumbnail_info,
        "metadata": metadata_info,
        "captions": caption_info,
        "sound_manifests": sound_info,
        "narration": narration_info,
        "validation_report": "output/validation_report.json",
        "render_manifest": "output/scene_renders/render_manifest.json",
        "embedded_manifest_self_hash_omitted": True,
        "archive_sha256_embedded": False,
        "package_files": [
            item for item in file_records
            if item["path"] != "output/package_manifest.json"
        ],
        "validation_checks": validation["checks"],
        "manifest_zip_consistency": "valid",
    }


def build_archive(
    archive_path: Path,
    files: list[Path],
    render: dict,
    embedded_manifest: dict,
) -> list[dict]:
    temporary = archive_path.with_name(archive_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    package_members = {
        "output/" + output_relative(path) for path in files
    }
    package_members.add("output/package_manifest.json")
    portable_render = portable_render_manifest(
        render["source"], render["parts"], package_members
    )
    records = []

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=ZIP_LEVEL,
            allowZip64=True,
        ) as archive:
            for path in files:
                member = "output/" + output_relative(path)
                if path.resolve() == RENDER.resolve():
                    payload = (
                        json.dumps(
                            portable_render,
                            ensure_ascii=False,
                            indent=2,
                        ) + "\n"
                    ).encode("utf-8")
                else:
                    payload = path.read_bytes()
                if not payload:
                    fail(f"Refusing empty package file: {member}")
                archive.writestr(member, payload)
                records.append({
                    "path": member,
                    "bytes": len(payload),
                    "sha256": sha256_bytes(payload),
                })

            manifest_payload = (
                json.dumps(
                    embedded_manifest,
                    ensure_ascii=False,
                    indent=2,
                ) + "\n"
            ).encode("utf-8")
            archive.writestr(
                "output/package_manifest.json", manifest_payload
            )
            records.append({
                "path": "output/package_manifest.json",
                "bytes": len(manifest_payload),
                "sha256": sha256_bytes(manifest_payload),
            })

        with zipfile.ZipFile(temporary, "r") as archive:
            names = validate_zip_structure(archive)
            if set(names) != package_members:
                fail("ZIP entries differ from selected package files")
            for record in records:
                payload = archive.read(record["path"])
                if len(payload) != record["bytes"]:
                    fail(f"ZIP member byte count mismatch: {record['path']}")
                if sha256_bytes(payload) != record["sha256"]:
                    fail(f"ZIP member SHA256 mismatch: {record['path']}")

        os.replace(temporary, archive_path)
        return records
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def verify_packaged_archive(
    archive_path: Path,
    mode: str,
    parts: int,
    dimensions: tuple[int, int],
    fps: float,
    records: list[dict],
) -> dict:
    if not archive_path.is_file() or archive_path.stat().st_size < MIN_VIDEO_BYTES:
        fail("Final ZIP is missing or too small")
    expected_part_members = [
        f"output/parts/part_{number:02d}.mp4"
        for number in range(1, parts + 1)
    ]
    expected_hashes = {item["path"]: item for item in records}

    with zipfile.ZipFile(archive_path, "r") as archive:
        names = validate_zip_structure(archive)
        name_set = set(names)
        missing = REQUIRED_MEMBERS - name_set
        if missing:
            fail(f"Required ZIP files are missing: {sorted(missing)}")
        if name_set != set(expected_hashes):
            fail("ZIP entries differ from package hash manifest")

        video_members = [
            name for name in names
            if PurePosixPath(name).suffix.lower() in VIDEO_SUFFIXES
        ]
        if set(video_members) != set(expected_part_members):
            fail("ZIP contains missing, extra or incorrectly named Part videos")

        for name, record in expected_hashes.items():
            payload = archive.read(name)
            if len(payload) != record["bytes"]:
                fail(f"ZIP member size mismatch: {name}")
            if sha256_bytes(payload) != record["sha256"]:
                fail(f"ZIP member hash mismatch: {name}")

        embedded = json.loads(
            archive.read("output/package_manifest.json").decode("utf-8")
        )
        if embedded.get("status") != "completed":
            fail("Embedded package manifest is incomplete")
        if embedded.get("version") != VERSION or embedded.get("format") != mode:
            fail("Embedded package manifest version or FORMAT mismatch")
        if embedded.get("part_videos_only") is not True:
            fail("Embedded manifest does not confirm Part-only videos")
        if embedded.get("combined_video_included") is not False:
            fail("Combined video must not be included")
        if embedded.get("format_alias_included") is not False:
            fail("Format alias must not be included")
        if embedded.get("embedded_manifest_self_hash_omitted") is not True:
            fail("Embedded manifest self-hash handling is invalid")
        if embedded.get("parts") != parts:
            fail("Embedded manifest Part count mismatch")

        render_data = json.loads(
            archive.read("output/scene_renders/render_manifest.json").decode("utf-8")
        )
        if clean(render_data.get("status")).lower() != "completed":
            fail("Packaged render manifest is incomplete")
        if clean(render_data.get("format")).lower() != mode:
            fail("Packaged render manifest FORMAT mismatch")
        if render_data.get("part_outputs_only") is not True:
            fail("Packaged render manifest is not Part-only")
        if render_data.get("captions_burned") is not True:
            fail("Packaged render manifest does not confirm burned captions")

        part_rows = render_data.get("parts")
        if not isinstance(part_rows, list) or len(part_rows) != parts:
            fail("Packaged render manifest Part list is invalid")

        verified_parts = []
        with tempfile.TemporaryDirectory(prefix="katha-package-check-") as temp:
            for index, row in enumerate(part_rows, 1):
                if not isinstance(row, dict):
                    fail(f"Invalid packaged Part record: {index}")
                try:
                    part_number = int(row.get("part", -1))
                except (TypeError, ValueError):
                    fail(f"Invalid packaged Part number: {index}")
                if part_number != index:
                    fail(f"Packaged Part order is incorrect at Part {index}")
                member = row.get("path")
                if member != expected_part_members[index - 1] or member not in name_set:
                    fail(f"Packaged Part reference is invalid: {member}")
                temp_path = Path(temp) / f"part_{index:02d}.mp4"
                with archive.open(member) as source, temp_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                info = probe_video(temp_path, dimensions, fps)
                digest = sha256_file(temp_path)
                if digest != row.get("sha256"):
                    fail(f"Packaged Part {index} SHA256 mismatch")
                if temp_path.stat().st_size != int(row.get("bytes", -1)):
                    fail(f"Packaged Part {index} byte count mismatch")
                verified_parts.append({
                    "part": index,
                    "path": member,
                    "duration": round(info["duration"], 3),
                    "sha256": digest,
                    "bytes": temp_path.stat().st_size,
                })

        if any(
            name in name_set for name in (
                "output/katha_lok_ai_final.mp4",
                "output/katha_lok_ai_short.mp4",
                "output/katha_lok_ai_full.mp4",
            )
        ):
            fail("Combined or format-alias video found in ZIP")

        return {
            "status": "valid",
            "crc": "valid",
            "entry_count": len(names),
            "part_count": len(verified_parts),
            "part_videos_only": True,
            "verified_parts": verified_parts,
            "member_hashes_verified": len(records),
        }


def verify_package_report(
    archive_path: Path,
    expected_report: dict,
) -> None:
    saved = read_json(REPORT)
    if saved.get("status") != "completed":
        fail("Package report status verification failed")
    if saved.get("version") != VERSION:
        fail("Package report version verification failed")
    if saved.get("format") != expected_report.get("format"):
        fail("Package report FORMAT verification failed")
    if saved.get("archive") != archive_path.name:
        fail("Package report archive name mismatch")
    if saved.get("archive_sha256") != sha256_file(archive_path):
        fail("Package report archive SHA256 mismatch")
    if saved.get("archive_bytes") != archive_path.stat().st_size:
        fail("Package report archive size mismatch")
    if saved.get("zip_integrity") != "valid":
        fail("Package report ZIP integrity is invalid")
    if saved.get("manifest_zip_consistency") != "valid":
        fail("Package manifest ZIP consistency is invalid")
    if saved.get("zip_validation", {}).get("status") != "valid":
        fail("Package ZIP validation report is invalid")
    if saved.get("part_videos_only") is not True:
        fail("Package report does not confirm Part-only videos")
    if saved.get("combined_video_included") is not False:
        fail("Package report incorrectly includes combined video")
    if saved.get("format_alias_included") is not False:
        fail("Package report incorrectly includes format alias")
    files = saved.get("files")
    if not isinstance(files, list) or saved.get("file_count") != len(files):
        fail("Package report file count mismatch")
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            fail("Package file record is invalid")
        name = item.get("path")
        validate_zip_member(name)
        if name in seen:
            fail(f"Duplicate package report path: {name}")
        seen.add(name)
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", item["sha256"]
        ):
            fail(f"Package file hash is invalid: {name}")
        if not isinstance(item.get("bytes"), int) or item["bytes"] <= 0:
            fail(f"Package file size is invalid: {name}")


def reject_opposite_format_files(mode: str) -> None:
    opposite = "full" if mode == "short" else "short"
    candidates = [
        OUT / "parts" / f"katha_lok_ai_{opposite}.mp4",
        OUT / f"katha_lok_ai_{opposite}.mp4",
        OUT / "videos" / f"katha_lok_ai_{opposite}.mp4",
    ]
    found = [str(path.relative_to(ROOT)) for path in candidates if path.exists()]
    if found:
        fail(f"Opposite-format videos are present: {found}")

    for path in ROOT.glob(f"katha-lok-*-{opposite}-output.zip"):
        if path.is_file():
            fail(f"Opposite-format ZIP is present: {path.name}")


def main() -> int:
    if not shutil.which("ffprobe"):
        fail("ffprobe is required but unavailable")
    if not OUT.is_dir() or not INPUT_FILE.is_file():
        fail("output/ or Input/topic.txt is missing")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    selected_parts = get_selected_parts(config, required=True)
    if selected_parts is None:
        fail("PARTS range has not been resolved and persisted")
    parts = int(selected_parts)
    scenes = int(get_scenes(config))
    fps = parse_fps(get_fps(config), "Configured FPS")

    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")
    if not 1 <= parts <= 1000 or not 1 <= scenes <= 100:
        fail("Selected PARTS or SCENES exceeds safety limits")
    if not math.isfinite(fps) or not 0 < fps <= 60:
        fail("Invalid FPS configuration")

    dimensions = (720, 1280) if mode == "short" else (1280, 720)
    log("=" * 68)
    log("KATHA LOK AI — VERIFIED OUTPUT PACKAGER")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("SELECTED PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("RESOLUTION:", dimensions)
    log("FPS:", fps)

    reject_opposite_format_files(mode)
    validation = validate_validation_report(mode, parts, scenes)
    render = validate_render(mode, parts, scenes, dimensions, fps)
    thumbnail, thumbnail_info = validate_thumbnail(mode, dimensions)
    metadata, metadata_json, tags, metadata_info = validate_metadata(mode)
    captions, caption_info = validate_srt()
    sound_info = validate_sound_manifests()
    narration_info = validate_narration_manifest()

    title = read_story_title()
    slug = safe_slug(title)
    archive_path = ROOT / f"katha-lok-{slug}-{mode}-output.zip"

    required_paths = [
        RENDER, VALIDATION, PHOTO, AUDIO, SOUND, SFX_VALIDATION,
        CAPTIONS_JSON, FINAL_SRT, THUMBNAIL, THUMBNAIL_MANIFEST,
        METADATA_TEXT, METADATA_JSON, TAGS,
    ]
    files = package_file_list(render, required_paths)
    package_members = {
        "output/" + output_relative(path) for path in files
    }
    package_members.add("output/package_manifest.json")

    part_records = render["parts"]
    embedded_manifest = build_embedded_manifest(
        mode=mode,
        title=title,
        slug=slug,
        archive_name=archive_path.name,
        part_records=part_records,
        file_records=[],
        validation=validation,
        thumbnail_info=thumbnail_info,
        metadata_info=metadata_info,
        caption_info=caption_info,
        sound_info=sound_info,
        narration_info=narration_info,
    )

    log("STORY TITLE:", title)
    log("ACTUAL PARTS:", parts)
    log("MAIN SCENES:", render["main_scene_count"])
    log("EXTRA ROLE SCENES:", render["extra_role_scene_count"])
    log("PART VIDEOS:", len(part_records))
    log("PACKAGE FILES:", len(files) + 1)

    temporary_archive = archive_path.with_name(
        archive_path.name + ".verified.tmp"
    )
    temporary_archive.unlink(missing_ok=True)
    try:
        records = build_archive(
            temporary_archive, files, render, embedded_manifest
        )
        zip_validation = verify_packaged_archive(
            temporary_archive,
            mode,
            parts,
            dimensions,
            fps,
            records,
        )
        os.replace(temporary_archive, archive_path)
    finally:
        temporary_archive.unlink(missing_ok=True)

    archive_hash = sha256_file(archive_path)
    report = {
        "status": "completed",
        "version": VERSION,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": mode,
        "story_title": title,
        "story_slug": slug,
        "parts": parts,
        "configured_parts": parts,
        "part_numbers": list(range(1, parts + 1)),
        "dynamic_short_parts": mode == "short",
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "main_scene_count": render["main_scene_count"],
        "extra_scene_count": render["extra_role_scene_count"],
        "part_count": len(part_records),
        "part_videos_only": True,
        "combined_video_included": False,
        "format_alias_included": False,
        "video_dimensions": list(dimensions),
        "expected_fps": fps,
        "thumbnail_dimensions": thumbnail_info["dimensions"],
        "thumbnail": thumbnail_info,
        "metadata": {
            "text": output_relative(metadata),
            "json": output_relative(metadata_json),
            "tags": output_relative(tags),
            **metadata_info,
        },
        "captions": caption_info,
        "sound_manifests": sound_info,
        "narration": narration_info,
        "validation_report": output_relative(VALIDATION),
        "render_manifest": output_relative(RENDER),
        "part_files": part_records,
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
        "embedded_manifest_self_hash_omitted": True,
        "part_video_paths_only": True,
    }

    write_json(REPORT, report)
    verify_package_report(archive_path, report)

    with zipfile.ZipFile(archive_path, "r") as archive:
        embedded = json.loads(
            archive.read("output/package_manifest.json").decode("utf-8")
        )
        if embedded.get("archive") != archive_path.name:
            fail("Embedded manifest archive name mismatch")
        if embedded.get("format") != mode:
            fail("Embedded manifest FORMAT mismatch")
        if embedded.get("parts") != parts:
            fail("Embedded manifest PARTS mismatch")
        if embedded.get("status") != "completed":
            fail("Embedded manifest status mismatch")
        if embedded.get("story_title") != title:
            fail("Embedded manifest story title mismatch")

    archive_name_file = OUT / "package_archive_name.txt"
    archive_name_file.write_text(
        archive_path.name + "\n", encoding="utf-8"
    )

    log("-" * 68)
    log("PACKAGE STATUS: completed")
    log("FORMAT:", mode)
    log("STORY:", title)
    log("PARTS:", parts)
    log("PART VIDEOS ONLY: yes")
    log("COMBINED VIDEO: excluded")
    log("FORMAT ALIAS: excluded")
    log("ZIP:", archive_path.name)
    log("ZIP BYTES:", archive_path.stat().st_size)
    log("ZIP SHA256:", archive_hash)
    log("ZIP CRC: valid")
    log("PACKAGED FILES:", len(records))
    log("MANIFEST:", output_relative(REPORT))
    log("=" * 68)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("PACKAGE: CANCELLED")
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        log("PACKAGE: FAILED", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)