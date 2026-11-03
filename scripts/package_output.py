#!/usr/bin/env python3
"""Verify selected-format deliverables and create a portable story ZIP."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPTS))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_fps,
)

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"
REPORT = OUT / "package_manifest.json"
VALIDATION = OUT / "validation_report.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA_TEXT = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
TAGS = OUT / "publish" / "youtube_tags.txt"
FINAL_SRT = OUT / "captions" / "final_captions.srt"

VERSION = "output-packager-v70-dynamic-parts-verified-portable-zip"
MIN_VIDEO_BYTES = 1000
MIN_IMAGE_BYTES = 1000
MIN_TEXT_BYTES = 5
FPS_TOLERANCE = 0.05
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120
ZIP_LEVEL = 6

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "checkpoints", "cache",
    "temporary", "temp",
}
EXCLUDED_SUFFIXES = {
    ".zip", ".tmp", ".lock", ".pyc", ".part", ".download",
    ".rendering", ".joining", ".building",
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
REQUIRED_CHECKS = (
    "story", "photo_motion", "narration", "sound_design", "render",
    "captions", "thumbnail", "metadata", "unique_output_files",
    "mode_separation",
)


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


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path) -> dict:
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


def safe_output_path(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path is missing.")
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(OUT.resolve()):
        fail(f"{label}: path escapes output/.")
    if not resolved.is_file():
        fail(f"{label}: file not found: {resolved}")
    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small.")
    return resolved


def relative_output(path: Path) -> str:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(OUT.resolve()):
        fail(f"Path is outside output/: {resolved}")
    return resolved.relative_to(OUT.resolve()).as_posix()


def parse_rate(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (ValueError, TypeError, ZeroDivisionError):
        fail(f"{label}: invalid FPS.")
    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS.")
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
                executable, "-v", "error", "-show_entries",
                "format=duration:"
                "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            check=False,
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
    if video is None:
        fail(f"Video stream missing: {path.name}")
    if require_audio and audio is None:
        fail(f"Audio stream missing: {path.name}")
    dimensions = (
        int(video.get("width") or 0), int(video.get("height") or 0)
    )
    rate = video.get("avg_frame_rate")
    if not rate or rate in {"0/0", "N/A"}:
        rate = video.get("r_frame_rate")
    fps = parse_rate(rate, path.name)
    if expected_size and dimensions != expected_size:
        fail(f"{path.name}: expected {expected_size}, found {dimensions}.")
    if expected_fps is not None and abs(fps - expected_fps) > FPS_TOLERANCE:
        fail(f"{path.name}: expected {expected_fps} FPS, found {fps:.4f}.")
    return {
        "duration": duration, "width": dimensions[0], "height": dimensions[1],
        "fps": fps, "has_video": True, "has_audio": audio is not None,
    }


def normalize_role(row: dict) -> str:
    value = str(
        row.get("role") or row.get("scene_type") or row.get("type") or "main"
    ).strip().lower()
    aliases = {
        "opening": "hook", "intro": "hook", "part_hook": "hook",
        "story": "main", "main_scene": "main",
        "cliffhanger": "suspense", "ending": "resolution",
        "final": "resolution",
    }
    value = aliases.get(value, value)
    if value not in {"main", "hook", "suspense", "resolution"}:
        fail(f"Unknown scene role: {value!r}")
    return value


def validate_validation_report(
    mode: str, parts: int, scenes: int
) -> dict:
    data = read_json(VALIDATION)
    if data.get("status") != "completed":
        fail("Final validation report is not completed.")
    if str(data.get("format", "")).strip().lower() != mode:
        fail("Validation report FORMAT mismatch.")
    try:
        reported_parts = int(data.get("parts", -1))
        reported_scenes = int(data.get("scenes_per_part", -1))
        expected_scenes = int(data.get("expected_scenes", -1))
        configured = int(data.get("configured_parts", -1))
    except (TypeError, ValueError):
        fail("Validation report contains invalid part/scene counts.")
    if reported_parts != parts or configured != parts:
        fail("Validation report PARTS differs from selected PARTS.")
    if reported_scenes != scenes or expected_scenes != parts * scenes:
        fail("Validation report SCENES count mismatch.")
    checks = data.get("checks")
    if not isinstance(checks, dict):
        fail("Validation report checks are missing.")
    for name in REQUIRED_CHECKS:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Validation check missing or invalid: {name}")
    if mode == "short" and reported_parts < 1:
        fail("SHORT dynamic part selection is invalid.")
    return data


def normalize_render_rows(data: dict) -> list[dict]:
    rows = data.get("all_completed_scenes")
    if not isinstance(rows, list):
        main = data.get("completed_scenes") or data.get("scenes") or []
        extra = data.get("extra_scenes") or []
        if not isinstance(main, list) or not isinstance(extra, list):
            fail("Render scene records are invalid.")
        rows = list(main) + list(extra)
    if not rows:
        fail("Render manifest contains no scene records.")
    result = []
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render scene row: {index}")
        scene_id = str(row.get("scene_id") or row.get("id") or "").strip()
        if not scene_id or scene_id in seen:
            fail(f"Missing or duplicate render scene ID at row {index}.")
        seen.add(scene_id)
        result.append(row)
    return result


def validate_role_records(
    rows: list[dict], parts: int, scenes: int
) -> dict:
    expected_main = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }
    actual_main = set()
    counts: dict[str, dict[int, int]] = {}
    for row in rows:
        role = normalize_role(row)
        try:
            part = int(row.get("part"))
        except (TypeError, ValueError):
            fail(f"Invalid part number in {row.get('scene_id')}.")
        if not 1 <= part <= parts:
            fail(f"Scene belongs to an unselected part: {part}")
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
            counts.setdefault(role, {})
            counts[role][part] = counts[role].get(part, 0) + 1
    if actual_main != expected_main:
        fail(
            "Main scene mapping mismatch: "
            f"missing={sorted(expected_main - actual_main)}, "
            f"extra={sorted(actual_main - expected_main)}"
        )
    for part in range(1, parts + 1):
        if counts.get("hook", {}).get(part, 0) != 1:
            fail(f"Part {part} must contain exactly one Hook.")
    for part in range(1, parts):
        if counts.get("suspense", {}).get(part, 0) != 1:
            fail(f"Non-final part {part} must contain exactly one Suspense.")
    if counts.get("suspense", {}).get(parts, 0):
        fail("Final part must not contain a Suspense scene.")
    if counts.get("resolution", {}).get(parts, 0) != 1:
        fail("Final part must contain exactly one Resolution.")
    for role, per_part in counts.items():
        for part, count in per_part.items():
            if count != 1:
                fail(f"Duplicate {role} record in part {part}.")
    extras = sum(1 for row in rows if normalize_role(row) != "main")
    if extras != parts * 2:
        fail(f"Hook/Suspense/Resolution count mismatch: {extras}/{parts * 2}.")
    return {
        "main_scene_count": len(actual_main),
        "extra_role_scene_count": extras,
        "role_counts": counts,
    }


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
    try:
        width = int(data.get("width"))
        height = int(data.get("height"))
        fps = float(data.get("fps"))
    except (TypeError, ValueError):
        fail("Render dimensions or FPS are invalid.")
    if (width, height) != expected_size:
        fail("Render dimensions do not match selected format.")
    if not math.isfinite(fps) or abs(fps - expected_fps) > FPS_TOLERANCE:
        fail("Render FPS does not match Input/topic.txt.")
    rows = normalize_render_rows(data)
    role_report = validate_role_records(rows, parts, scenes)
    checked_scenes = []
    scene_duration = 0.0
    for row in rows:
        if row.get("status") != "completed":
            fail(f"Render scene is incomplete: {row.get('scene_id')}")
        value = row.get("video") or row.get("output") or row.get("path")
        path = safe_output_path(value, f"Scene {row.get('scene_id')}", MIN_VIDEO_BYTES)
        info = probe_media(path, expected_size, expected_fps)
        digest = sha256_file(path)
        declared = row.get("sha256") or row.get("video_sha256")
        if declared and declared != digest:
            fail(f"Scene SHA256 mismatch: {row.get('scene_id')}")
        checked_scenes.append({
            "scene_id": row.get("scene_id"),
            "role": normalize_role(row),
            "part": int(row["part"]),
            "local_scene": (
                int(row.get("local_scene") or row.get("scene") or 0)
                if normalize_role(row) == "main" else None
            ),
            "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
        scene_duration += info["duration"]

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail("Render manifest must contain exactly the selected Part videos.")
    checked_parts = []
    part_paths = []
    part_duration = 0.0
    for index, row in enumerate(part_rows, 1):
        expected_name = f"part_{index:02d}.mp4"
        if not isinstance(row, dict):
            fail(f"Rendered Part {index} record is invalid.")
        if int(row.get("part", -1)) != index:
            fail(f"Rendered Part order mismatch at Part {index}.")
        if row.get("status") != "completed":
            fail(f"Rendered Part {index} is incomplete.")
        path = safe_output_path(
            row.get("path") or row.get("video") or row.get("output"),
            f"Rendered Part {index}", MIN_VIDEO_BYTES,
        )
        if path.suffix.lower() != ".mp4" or path.name != expected_name:
            fail(f"Part {index} filename must be {expected_name}.")
        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(f"Part {index} must be stored in output/parts/.")
        info = probe_media(path, expected_size, expected_fps)
        digest = sha256_file(path)
        if row.get("sha256") and row["sha256"] != digest:
            fail(f"Part {index} SHA256 mismatch.")
        if row.get("duration") is not None:
            if abs(float(row["duration"]) - info["duration"]) > DURATION_TOLERANCE:
                fail(f"Part {index} duration metadata mismatch.")
        checked_parts.append({
            "part": index, "path": relative_output(path),
            "duration": round(info["duration"], 3),
            "width": info["width"], "height": info["height"],
            "fps": round(info["fps"], 4), "bytes": path.stat().st_size,
            "sha256": digest,
        })
        part_paths.append(path)
        part_duration += info["duration"]
    if abs(part_duration - scene_duration) > max(
        DURATION_TOLERANCE, len(rows) * 0.05
    ):
        fail("Part duration differs significantly from rendered scene durations.")
    return {
        "source": data, "scene_rows": rows,
        "checked_scene_rows": checked_scenes,
        "parts": checked_parts, "part_paths": part_paths,
        "part_duration_total": round(part_duration, 3),
        "scene_duration_total": round(scene_duration, 3),
        **role_report,
    }


def validate_thumbnail(
    mode: str, expected_size: tuple[int, int]
) -> tuple[Path, dict]:
    path = safe_output_path("output/thumbnail.jpg", "Thumbnail", MIN_IMAGE_BYTES)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image_format, dimensions = image.format, image.size
    except Exception as exc:
        fail(f"Thumbnail image is invalid: {exc}")
    if image_format != "JPEG" or dimensions != expected_size:
        fail(
            f"{mode.upper()} thumbnail must be JPEG {expected_size}; "
            f"found {image_format} {dimensions}."
        )
    manifest = read_json(THUMBNAIL_MANIFEST)
    if manifest.get("status") != "completed":
        fail("Thumbnail manifest is not completed.")
    if str(manifest.get("format", "")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch.")
    declared = manifest.get("thumbnail") or manifest.get("path") or manifest.get("output")
    if declared:
        declared_path = safe_output_path(declared, "Thumbnail manifest image", MIN_IMAGE_BYTES)
        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to another image.")
    digest = sha256_file(path)
    for item in manifest.get("thumbnails", []):
        if isinstance(item, dict) and item.get("file") in {
            "output/thumbnail.jpg", "thumbnail.jpg"
        }:
            if item.get("sha256") and item["sha256"] != digest:
                fail("Thumbnail SHA256 mismatch.")
    return path, {
        "path": relative_output(path), "format": image_format,
        "dimensions": list(dimensions), "bytes": path.stat().st_size,
        "sha256": digest,
    }


def parse_tags(value: str) -> list[str]:
    raw = value.strip()
    if not raw:
        return []
    pieces = raw.replace("\r", "\n").split("\n") if "\n" in raw else raw.split(",")
    result, seen = [], set()
    for item in pieces:
        tag = " ".join(item.split()).strip(" ,#")
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def validate_metadata(mode: str) -> tuple[Path, Path, Path, dict]:
    text_path = safe_output_path("output/youtube_metadata.txt", "Metadata text", MIN_TEXT_BYTES)
    json_path = safe_output_path(
        "output/publish/youtube_metadata.json", "Metadata JSON", MIN_TEXT_BYTES
    )
    tags_path = safe_output_path("output/publish/youtube_tags.txt", "Metadata tags", MIN_TEXT_BYTES)
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
    text_tags = parse_tags(" ".join(sections.get("TAGS", [])))
    if not title or not description or not text_tags:
        fail("Metadata text must contain TITLE, DESCRIPTION and TAGS.")
    if len(title) > 100 or len(description) > 5000:
        fail("Metadata text exceeds YouTube title/description limits.")
    data = read_json(json_path)
    if data.get("status") != "completed":
        fail("Structured metadata is not completed.")
    if str(data.get("format", "")).lower() != mode:
        fail("Metadata FORMAT mismatch.")
    json_title, json_description, tags = (
        data.get("title"), data.get("description"), data.get("tags")
    )
    if not isinstance(json_title, str) or not isinstance(json_description, str):
        fail("Structured metadata title or description is missing.")
    if json_title.strip() != title or json_description.strip() != description:
        fail("Metadata text and JSON content differ.")
    if len(json_title) > 100 or len(json_description) > 5000:
        fail("Structured metadata exceeds YouTube limits.")
    if not isinstance(tags, list):
        fail("Structured metadata tags are invalid.")
    normalized = []
    seen = set()
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            fail("Metadata contains an invalid tag.")
        value = " ".join(tag.split()).strip(" ,#")
        if not value or len(value) > 100:
            fail("Metadata contains an invalid tag.")
        if value.casefold() not in seen:
            seen.add(value.casefold())
            normalized.append(value)
    if not 5 <= len(normalized) <= 20:
        fail("Metadata must contain 5–20 unique tags.")
    expected = {item.casefold() for item in normalized}
    if {item.casefold() for item in text_tags} != expected:
        fail("Metadata text tags differ from structured tags.")
    dedicated = parse_tags(tags_path.read_text(encoding="utf-8-sig"))
    if {item.casefold() for item in dedicated} != expected:
        fail("Dedicated tags differ from structured tags.")
    return text_path, json_path, tags_path, {
        "title": title,
        "description_sha256": sha256_bytes(description.encode("utf-8")),
        "tags": normalized,
    }


def validate_srt() -> tuple[Path, dict]:
    path = safe_output_path("output/captions/final_captions.srt", "Final captions SRT", 10)
    content = path.read_text(encoding="utf-8-sig")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [
        item.strip() for item in re.split(r"\n[ \t]*\n", content)
        if item.strip()
    ]
    if not blocks:
        fail("Final SRT contains no caption cues.")
    previous_end = -1.0
    maximum_end = 0.0

    def seconds(value: str) -> float:
        match = re.fullmatch(
            r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
            value.strip(),
        )
        if not match:
            fail(f"Invalid SRT timestamp: {value}")
        h, m, s, ms = map(int, match.groups())
        return h * 3600 + m * 60 + s + ms / 1000

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index} is incomplete.")
        if int(lines[0].strip()) != index or lines[1].count("-->") != 1:
            fail(f"SRT cue {index} numbering or timing is invalid.")
        left, right = [value.strip() for value in lines[1].split("-->")]
        start, end = seconds(left), seconds(right)
        if start < 0 or end <= start or start < previous_end - 0.001:
            fail(f"SRT cue {index} overlaps or has invalid timing.")
        if not "\n".join(lines[2:]).strip():
            fail(f"SRT cue {index} has no caption text.")
        previous_end = end
        maximum_end = max(maximum_end, end)
    return path, {
        "path": relative_output(path), "cue_count": len(blocks),
        "last_cue_end": round(maximum_end, 3),
        "sha256": sha256_file(path),
    }

def read_story_title() -> str:
    for path in (
        OUT / "story" / "story.json",
        OUT / "story" / "ai_story.json",
    ):
        if not path.is_file():
            continue
        data = read_json(path)
        for obj in (
            data,
            data.get("story") if isinstance(data.get("story"), dict) else {},
        ):
            for key in ("final_title", "generated_title", "story_title", "title"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    title_file = OUT / "story" / "final_title.txt"
    if title_file.is_file():
        value = title_file.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    fail("Story title is missing; generic ZIP names are prohibited.")


def safe_slug(title: str) -> str:
    slug = re.sub(r"[^\w]+", "-", title, flags=re.UNICODE).strip("-_")
    slug = slug[:90].strip("-_")
    if not slug:
        fail("Story title cannot form a safe ZIP filename.")
    return slug


def portable_render_manifest(
    render: dict, package_members: set[str]
) -> dict:
    data = json.loads(json.dumps(render["source"]))
    for key in (
        "scenes", "completed_scenes", "all_completed_scenes",
        "extra_scenes", "completed_count", "all_completed_scene_count",
        "extra_scene_count", "final_video", "alias", "combined_video",
        "combined_output", "pre_caption_video",
    ):
        data.pop(key, None)

    parts = []
    for item in render["parts"]:
        member = "output/" + item["path"]
        if member not in package_members:
            fail(f"Part video missing from package: {member}")
        parts.append({
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
    data["parts"] = parts
    data["part_numbers"] = [item["part"] for item in parts]
    data["part_videos_only"] = True
    data["combined_video_included"] = False
    data["format_alias_included"] = False
    data["package_manifest_view"] = True
    data["package_manifest_version"] = VERSION
    data["package_integrity"] = {
        "part_count": len(parts),
        "part_sha256": {
            str(item["part"]): item["sha256"] for item in parts
        },
    }
    return data


def should_exclude_file(path: Path) -> bool:
    if path.is_symlink():
        fail(f"Output symlink is not allowed: {path}")
    if not path.is_file():
        return True
    if not path.resolve().is_relative_to(OUT.resolve()):
        fail(f"Output path escapes output/: {path}")
    pieces = path.relative_to(OUT).parts
    if any(piece in EXCLUDED_DIRS for piece in pieces):
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if path.name.startswith(".resume-") or path.name.endswith(".tmp"):
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
    part_paths = {item.resolve() for item in render["part_paths"]}
    required = {
        RENDER.resolve(), VALIDATION.resolve(), thumbnail.resolve(),
        metadata.resolve(), metadata_json.resolve(), tags.resolve(),
        captions.resolve(), *part_paths,
    }
    selected = set(required)

    for path in OUT.rglob("*"):
        if should_exclude_file(path):
            continue
        resolved = path.resolve()
        if resolved == REPORT.resolve():
            continue
        if path.suffix.lower() in VIDEO_SUFFIXES or path.suffix.lower() == ".zip":
            continue
        selected.add(resolved)

    missing = [path for path in required if not path.is_file()]
    if missing:
        fail(f"Required package files are missing: {missing}")

    for path in selected:
        if path.suffix.lower() in VIDEO_SUFFIXES and path not in part_paths:
            fail(f"Non-Part video found in package selection: {path}")

    names = ["output/" + relative_output(path) for path in selected]
    if len(names) != len(set(names)):
        fail("Duplicate package paths detected.")
    return sorted(selected, key=lambda item: item.as_posix())


def validate_zip_member_name(name: str) -> None:
    member = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or any(piece in {"", ".", ".."} for piece in member.parts)
        or not member.parts
        or member.parts[0] != "output"
    ):
        fail(f"Unsafe ZIP member path: {name!r}")


def validate_zip_structure(archive: zipfile.ZipFile) -> list[str]:
    bad_member = archive.testzip()
    if bad_member:
        fail(f"ZIP CRC verification failed: {bad_member}")
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if not names:
        fail("ZIP archive is empty.")
    if len(names) != len(set(names)):
        fail("ZIP contains duplicate paths.")
    for info in infos:
        validate_zip_member_name(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            fail(f"Symlink is forbidden in ZIP: {info.filename}")
        if info.flag_bits & 0x1:
            fail(f"Encrypted ZIP member is not allowed: {info.filename}")
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
        "output/" + relative_output(path) for path in files
    }
    package_members.add("output/package_manifest.json")
    portable_render = portable_render_manifest(render, package_members)
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
                member = "output/" + relative_output(path)
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
                fail("ZIP members differ from the selected package file list.")
            for record in records:
                payload = archive.read(record["path"])
                if len(payload) != record["bytes"]:
                    fail(f"ZIP byte count mismatch: {record['path']}")
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
    parts: int,
    expected_size: tuple[int, int],
    expected_fps: float,
    records: list[dict],
) -> dict:
    if not archive_path.is_file() or archive_path.stat().st_size < MIN_VIDEO_BYTES:
        fail("Final ZIP is missing or too small.")

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
            fail("ZIP entries do not match the package hash manifest.")

        for name in names:
            if PurePosixPath(name).suffix.lower() in VIDEO_SUFFIXES:
                if name not in expected_part_members:
                    fail(f"Non-Part video found in ZIP: {name}")
            if name.lower().endswith(".zip"):
                fail(f"Nested ZIP is not allowed: {name}")

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
            fail("Embedded package manifest is incomplete.")
        if embedded.get("version") != VERSION or embedded.get("format") != mode:
            fail("Embedded package manifest version or FORMAT mismatch.")
        if embedded.get("part_videos_only") is not True:
            fail("Embedded package manifest does not confirm Part-only output.")
        if embedded.get("combined_video_included") is not False:
            fail("Combined video must not be included.")
        if embedded.get("format_alias_included") is not False:
            fail("Format alias must not be included.")
        if embedded.get("embedded_manifest_self_hash_omitted") is not True:
            fail("Embedded manifest self-hash handling is invalid.")

        render_data = json.loads(
            archive.read("output/scene_renders/render_manifest.json").decode("utf-8")
        )
        if render_data.get("status") != "completed":
            fail("Packaged render manifest is incomplete.")
        if str(render_data.get("format", "")).lower() != mode:
            fail("Packaged render manifest FORMAT mismatch.")
        if render_data.get("part_videos_only") is not True:
            fail("Packaged render manifest is not Part-only.")
        part_rows = render_data.get("parts")
        if not isinstance(part_rows, list) or len(part_rows) != parts:
            fail("Packaged render manifest Part list is invalid.")

        verified_parts = []
        with tempfile.TemporaryDirectory(prefix="katha-package-check-") as temp:
            for index, row in enumerate(part_rows, 1):
                if int(row.get("part", -1)) != index:
                    fail("Packaged Part ordering is incorrect.")
                member = row.get("path")
                if member != expected_part_members[index - 1] or member not in name_set:
                    fail(f"Packaged Part reference is invalid: {member}")
                temp_path = Path(temp) / f"part_{index:02d}.mp4"
                with archive.open(member) as source, temp_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                info = probe_media(temp_path, expected_size, expected_fps)
                digest = sha256_file(temp_path)
                if digest != row.get("sha256"):
                    fail(f"Packaged Part {index} SHA256 mismatch.")
                if int(row.get("bytes", -1)) != temp_path.stat().st_size:
                    fail(f"Packaged Part {index} byte count mismatch.")
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
            fail("Combined or format-alias video found in ZIP.")

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
    report: dict, archive_path: Path
) -> None:
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
    if saved.get("manifest_zip_consistency") != "valid":
        fail("Package manifest ZIP consistency is invalid.")
    if saved.get("zip_validation", {}).get("status") != "valid":
        fail("Package ZIP validation report is invalid.")
    if saved.get("part_videos_only") is not True:
        fail("Package report does not confirm Part-only videos.")
    if saved.get("combined_video_included") is not False:
        fail("Package report incorrectly includes a combined video.")
    if saved.get("format_alias_included") is not False:
        fail("Package report incorrectly includes a format alias.")
    if saved.get("file_count") != len(saved.get("files", [])):
        fail("Package report file count mismatch.")
    for item in saved.get("files", []):
        if not isinstance(item, dict):
            fail("Package file record is invalid.")
        path = item.get("path")
        validate_zip_member_name(path)
        if path == "output/package_manifest.json":
            continue
        if not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64:
            fail(f"Package file hash is invalid: {path}")


def main() -> int:
    if not shutil.which("ffprobe"):
        fail("ffprobe is required.")
    if not OUT.is_dir() or not INPUT_FILE.is_file():
        fail("output/ or Input/topic.txt is missing.")

    config = load_and_validate(str(INPUT_FILE))
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    fps = float(get_fps(config))

    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")
    if not 1 <= parts <= 1000 or not 1 <= scenes <= 100:
        fail("Selected PARTS or SCENES exceeds safety limits.")
    if not math.isfinite(fps) or not 0 < fps <= 60:
        fail("Invalid FPS configuration.")

    expected_size = (720, 1280) if mode == "short" else (1280, 720)
    log("=" * 65)
    log("KATHA LOK AI — VERIFIED OUTPUT PACKAGER")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("SELECTED PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("FPS:", fps)

    validation = validate_validation_report(mode, parts, scenes)
    render = validate_render_manifest(
        mode, parts, scenes, expected_size, fps
    )
    thumbnail, thumbnail_info = validate_thumbnail(mode, expected_size)
    metadata, metadata_json, tags, metadata_info = validate_metadata(mode)
    captions, caption_info = validate_srt()

    title = read_story_title()
    slug = safe_slug(title)
    archive_path = ROOT / f"katha-lok-{slug}-{mode}-output.zip"

    files = collect_package_files(
        render, thumbnail, metadata, metadata_json, tags, captions
    )
    package_file_names = {
        "output/" + relative_output(path) for path in files
    }

    part_records = render["parts"]
    embedded_manifest = build_embedded_manifest(
        mode, title, slug, archive_path.name, part_records, [],
        validation, thumbnail_info, metadata_info, caption_info,
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
            temporary_archive, mode, parts, expected_size, fps, records
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
        "video_dimensions": list(expected_size),
        "expected_fps": fps,
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
    }

    write_json(REPORT, report)
    verify_package_report(report, archive_path)

    with zipfile.ZipFile(archive_path, "r") as archive:
        embedded = json.loads(
            archive.read("output/package_manifest.json").decode("utf-8")
        )
        if embedded.get("archive") != archive_path.name:
            fail("Embedded manifest archive name mismatch.")
        if embedded.get("format") != mode:
            fail("Embedded manifest FORMAT mismatch.")
        if embedded.get("parts") != parts:
            fail("Embedded manifest PARTS mismatch.")
        if embedded.get("status") != "completed":
            fail("Embedded manifest status mismatch.")

    archive_name_file = OUT / "package_archive_name.txt"
    archive_name_file.write_text(archive_path.name + "\n", encoding="utf-8")

    log("-" * 65)
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
    log("MANIFEST:", relative_output(REPORT))
    log("=" * 65)
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