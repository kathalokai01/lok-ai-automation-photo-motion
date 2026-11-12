#!/usr/bin/env python3
"""Build and independently verify a story-named, FORMAT-specific ZIP package."""

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
import unicodedata
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
    get_scenes_range,
    get_fps,
)

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"

REPORT = OUT / "package_manifest.json"
ARCHIVE_NAME_FILE = OUT / "package_archive_name.txt"
VALIDATION = OUT / "validation_report.json"
STORY = OUT / "story" / "story.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX_VALIDATION = OUT / "sound_design" / "sfx_timing_validation.json"
CAPTIONS_JSON = OUT / "captions" / "captions.json"
FINAL_SRT = OUT / "captions" / "final_captions.srt"
SFX_CUES = OUT / "sound_design" / "sfx_cues.json"
FLOW_MANIFEST = OUT / "music" / "flow_music_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA_TEXT = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
TAGS = OUT / "publish" / "youtube_tags.txt"

VERSION = "output-packager-v106-narration-manifest-authority"
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
REQUIRED_CHECKS = (
    "story", "photo_motion", "narration", "sound_design",
    "render", "captions", "thumbnail", "metadata",
    "unique_output_files", "mode_separation", "production_artifacts",
)
STORY_VERIFICATION = OUT / "story" / "story_verification.json"
PROJECT_FILES = (
    OUT / "story" / "story.json",
    STORY_VERIFICATION,
    OUT / "story" / "story_plan.json",
    OUT / "story" / "character_bible.json",
    FLOW_MANIFEST,
    OUT / "fonts" / "font_manifest.json",
    OUT / "fonts" / "selected_hindi_caption_font.ttf",
    OUT / "fonts" / "OFL-1.1.txt",
    OUT / "scenes" / "scenes.json",
    OUT / "narration" / "narration.json",
    OUT / "visuals" / "visual_jobs.json",
    OUT / "visuals" / "visual_jobs_manifest.json",
    OUT / "visuals" / "image_manifest.json",
    OUT / "visuals" / "image_generation_manifest.json",
    OUT / "visuals" / "photo_motion_jobs.json",
    OUT / "visuals" / "depth_maps" / "depth_manifest.json",
    OUT / "visuals" / "storyboard.jpg",
    OUT / "visuals" / "storyboard_manifest.json",
    OUT / "quality" / "render_quality_report.json",
    OUT / "production_report.json",
    SFX_CUES,
)
REQUIRED_FILES = (
    VALIDATION, RENDER, PHOTO, AUDIO, SOUND, SFX_VALIDATION,
    CAPTIONS_JSON, FINAL_SRT, THUMBNAIL, THUMBNAIL_MANIFEST,
    METADATA_TEXT, METADATA_JSON, TAGS, *PROJECT_FILES,
)
SUCCESS = {"completed", "complete", "success", "done", "reused", "valid"}
ROLE_ALIASES = {
    "opening": "hook",
    "intro": "hook",
    "part_hook": "hook",
    "story": "main",
    "main_scene": "main",
    "cliffhanger": "suspense",
    "ending": "resolution",
    "final": "resolution",
}


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def clean(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path, optional: bool = False) -> dict | None:
    if not path.is_file():
        if optional:
            return None
        fail(f"Required JSON is missing: {path.relative_to(ROOT)}")
    if path.stat().st_size <= 0:
        fail(f"Required JSON is empty: {path.relative_to(ROOT)}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"Expected JSON object: {path}")
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


def safe_output(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: path is missing")

    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    lexical = Path(os.path.abspath(str(candidate)))
    output_root = Path(os.path.abspath(str(OUT)))

    try:
        relative = lexical.relative_to(output_root)
    except ValueError:
        fail(f"{label}: path must remain inside output/")

    current = output_root
    if current.is_symlink():
        fail("output/ must not be a symlink")

    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            fail(f"{label}: symlink path is forbidden: {current}")

    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(OUT.resolve())
    except ValueError:
        fail(f"{label}: resolved path escapes output/")

    if not resolved.is_file():
        fail(f"{label}: file does not exist: {resolved}")
    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small")
    return resolved


def output_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(OUT.resolve()).as_posix()
    except ValueError:
        fail(f"Package path escapes output/: {path}")


def parse_fps(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (TypeError, ValueError, ZeroDivisionError):
        fail(f"{label}: invalid FPS")
    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS")
    return result


def probe_audio_duration(path: Path) -> float:
    executable = shutil.which("ffprobe")
    if not executable:
        fail("ffprobe is required but unavailable")
    try:
        result = subprocess.run(
            [
                executable, "-v", "error",
                "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        fail(f"ffprobe timed out for audio: {path.name}")
    if result.returncode:
        fail(f"ffprobe failed for audio {path.name}: {(result.stderr or '')[-700:]}")
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data["streams"]
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid audio probe for {path.name}: {exc}")
    if (
        not math.isfinite(duration)
        or duration <= 0
        or not isinstance(streams, list)
        or not any(isinstance(row, dict) and row.get("codec_type") == "audio" for row in streams)
    ):
        fail(f"Invalid or silent narration audio: {path.name}")
    return duration


def probe_video(
    path: Path,
    dimensions: tuple[int, int],
    expected_fps: float,
    require_audio: bool = True,
) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        fail("ffprobe is required but unavailable")

    try:
        result = subprocess.run(
            [
                executable, "-v", "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
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
        fail(f"Invalid duration: {path.name}")

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
        fail(f"{path.name}: video stream is missing")
    if require_audio and audio is None:
        fail(f"{path.name}: audio stream is missing")

    actual_dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )
    rate = video.get("avg_frame_rate")
    if not rate or rate in {"0/0", "N/A"}:
        rate = video.get("r_frame_rate")
    actual_fps = parse_fps(rate, path.name)

    if actual_dimensions != dimensions:
        fail(
            f"{path.name}: expected {dimensions}, found {actual_dimensions}"
        )
    if abs(actual_fps - expected_fps) > FPS_TOLERANCE:
        fail(
            f"{path.name}: expected {expected_fps} FPS, found {actual_fps:.4f}"
        )

    return {
        "duration": duration,
        "width": actual_dimensions[0],
        "height": actual_dimensions[1],
        "fps": actual_fps,
        "has_audio": audio is not None,
    }


def story_title(story: dict) -> str:
    for key in ("final_title", "generated_title", "story_title", "title"):
        value = story.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()

    for key in ("story", "result", "data"):
        nested = story.get(key)
        if isinstance(nested, dict):
            for title_key in (
                "final_title", "generated_title", "story_title", "title"
            ):
                value = nested.get(title_key)
                if isinstance(value, str) and value.strip():
                    return re.sub(r"\s+", " ", value).strip()

    title_file = OUT / "story" / "final_title.txt"
    if title_file.is_file():
        value = title_file.read_text(encoding="utf-8-sig").strip()
        if value:
            return re.sub(r"\s+", " ", value)

    fail("Story-derived title is missing")


def safe_slug(title: str) -> str:
    value = unicodedata.normalize("NFKC", title).strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value, flags=re.UNICODE).strip("-")
    value = value[:100].strip("-")
    if not value or value in {".", ".."}:
        fail("Cannot create a safe story filename from the title")
    return value


def normalize_role(row: dict) -> str:
    value = clean(
        row.get("role") or row.get("scene_type") or row.get("type") or "main"
    ).lower()
    value = ROLE_ALIASES.get(value, value)
    if value not in {"main", "hook", "suspense", "resolution"}:
        fail(f"Unknown scene role: {value}")
    return value


def canonical_scene_key(row: dict) -> tuple[int, str, int | None]:
    try:
        part = int(row.get("part"))
    except (TypeError, ValueError):
        fail(f"Invalid Part number in scene record: {row.get('scene_id')}")

    role = normalize_role(row)
    scene = None
    if role == "main":
        try:
            scene = int(row.get("local_scene") or row.get("scene") or 0)
        except (TypeError, ValueError):
            fail(f"Invalid Main scene number: {row.get('scene_id')}")
        if scene < 1:
            fail(f"Invalid Main scene number: {row.get('scene_id')}")

    return part, role, scene


def validate_validation_report(
    mode: str,
    parts: int,
    scene_counts: dict[int, int],
    title: str,
) -> dict:
    data = read_json(VALIDATION)

    if clean(data.get("status")).lower() != "completed":
        fail("Final validation report is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Validation report FORMAT mismatch")
    if clean(data.get("story_title")) != title:
        fail("Validation report story title mismatch")

    if int(data.get("parts", -1)) != parts:
        fail("Validation report actual PARTS mismatch")
    if int(data.get("configured_parts", -1)) != parts:
        fail("Validation report configured PARTS mismatch")

    expected_counts = {str(part): count for part, count in scene_counts.items()}
    declared_counts = data.get("actual_scene_counts")
    if not isinstance(declared_counts, dict):
        fail("Validation report actual_scene_counts is missing")

    normalized_counts = {}
    for key, value in declared_counts.items():
        try:
            normalized_counts[str(int(key))] = int(value)
        except (TypeError, ValueError):
            fail("Validation report contains an invalid scene count")

    if normalized_counts != expected_counts:
        fail("Validation report actual scene counts differ from render manifest")

    if int(data.get("expected_scenes", -1)) != sum(scene_counts.values()):
        fail("Validation report total Main scene count mismatch")

    checks = data.get("checks")
    if not isinstance(checks, dict):
        fail("Validation report checks are missing")

    for name in REQUIRED_CHECKS:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Final validation check is missing or invalid: {name}")

    return data


def collect_render_rows(data: dict) -> list[dict]:
    rows = data.get("all_completed_scenes")
    if not isinstance(rows, list):
        main = data.get("completed_scenes") or data.get("scenes") or []
        extra = data.get("extra_scenes") or []
        if not isinstance(main, list) or not isinstance(extra, list):
            fail("Render scene records are invalid")
        rows = list(main) + list(extra)

    if not rows:
        fail("Render manifest contains no scene records")

    result = []
    seen_ids = set()
    seen_keys = set()

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render scene record {index}")

        scene_id = clean(row.get("scene_id") or row.get("id"))
        if not scene_id or scene_id in seen_ids:
            fail(f"Missing or duplicate render scene ID at row {index}")
        seen_ids.add(scene_id)

        key = canonical_scene_key(row)
        if key in seen_keys:
            fail(f"Duplicate render scene role/number: {key}")
        seen_keys.add(key)
        result.append(row)

    return result


def validate_render(
    mode: str,
    parts: int,
    scene_counts: dict[int, int],
    dimensions: tuple[int, int],
    fps: float,
) -> dict:
    data = read_json(RENDER)

    if clean(data.get("status")).lower() != "completed":
        fail("Render manifest is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Render manifest FORMAT mismatch")
    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions")
    if data.get("part_outputs_only") is not True:
        fail("Render manifest must confirm Part-only output")

    if (int(data.get("width", 0)), int(data.get("height", 0))) != dimensions:
        fail("Render manifest dimensions mismatch")
    if abs(parse_fps(data.get("fps"), "Render FPS") - fps) > FPS_TOLERANCE:
        fail("Render manifest FPS mismatch")

    for forbidden in ("final_video", "alias", "combined_video", "combined_output"):
        if data.get(forbidden):
            fail(f"Combined or alias video is forbidden: {forbidden}")

    rows = collect_render_rows(data)
    expected_keys = {
        (part, "main", scene)
        for part, count in scene_counts.items()
        for scene in range(1, count + 1)
    }
    expected_keys.update((part, "hook", None) for part in range(1, parts + 1))
    expected_keys.update((part, "suspense", None) for part in range(1, parts))
    expected_keys.add((parts, "resolution", None))

    actual_keys = {canonical_scene_key(row) for row in rows}
    if actual_keys != expected_keys:
        fail(
            "Rendered scene-role mapping mismatch: "
            f"missing={sorted(expected_keys - actual_keys)}, "
            f"extra={sorted(actual_keys - expected_keys)}"
        )

    checked_rows = []
    scene_duration_total = 0.0

    for row in rows:
        scene_id = clean(row.get("scene_id") or row.get("id"))
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered scene is incomplete: {scene_id}")
        if row.get("captions_burned") is not True:
            fail(f"Rendered role captions are not confirmed: {scene_id}")
        if row.get("voiceover_enabled") is not True:
            fail(f"Rendered role voiceover is not enabled: {scene_id}")
        if not clean(row.get("source_audio_sha256")):
            fail(f"Rendered role TTS audio hash is missing: {scene_id}")

        path = safe_output(
            row.get("video") or row.get("output") or row.get("path"),
            f"Rendered scene {scene_id}",
            MIN_VIDEO_BYTES,
        )
        info = probe_video(path, dimensions, fps, require_audio=True)
        digest = sha256_file(path)

        declared_hash = row.get("sha256") or row.get("video_sha256")
        if declared_hash and clean(declared_hash).lower() != digest:
            fail(f"Rendered scene SHA256 mismatch: {scene_id}")

        key = canonical_scene_key(row)
        checked_rows.append({
            "scene_id": scene_id,
            "part": key[0],
            "role": key[1],
            "local_scene": key[2],
            "path": output_relative(path),
            "duration": round(info["duration"], 4),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
        scene_duration_total += info["duration"]

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Render manifest must contain exactly {parts} Part videos")

    checked_parts = []
    part_paths = []
    part_duration_total = 0.0

    for number, row in enumerate(part_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid Part output record: {number}")
        if int(row.get("part", -1)) != number:
            fail(f"Rendered Part ordering mismatch at Part {number}")
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered Part {number} is incomplete")

        path = safe_output(
            row.get("path") or row.get("video") or row.get("output"),
            f"Rendered Part {number}",
            MIN_VIDEO_BYTES,
        )
        if path.suffix.lower() != ".mp4":
            fail(f"Rendered Part {number} must be MP4")
        if path.name != f"part_{number:02d}.mp4":
            fail(f"Rendered Part {number} has an invalid internal filename")
        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(f"Rendered Part {number} must be inside output/parts/")
        if path in part_paths:
            fail(f"Duplicate rendered Part path: {path}")

        info = probe_video(path, dimensions, fps, require_audio=True)
        digest = sha256_file(path)
        declared_hash = row.get("sha256") or row.get("video_sha256")
        if declared_hash and clean(declared_hash).lower() != digest:
            fail(f"Rendered Part {number} SHA256 mismatch")

        if row.get("duration") is not None:
            declared_duration = float(row["duration"])
            if (
                not math.isfinite(declared_duration)
                or abs(declared_duration - info["duration"]) > DURATION_TOLERANCE
            ):
                fail(f"Rendered Part {number} duration mismatch")

        checked_parts.append({
            "part": number,
            "path": output_relative(path),
            "duration": round(info["duration"], 4),
            "width": info["width"],
            "height": info["height"],
            "fps": round(info["fps"], 4),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
        part_paths.append(path)
        part_duration_total += info["duration"]

    tolerance = max(DURATION_TOLERANCE, len(rows) * 0.05)
    if abs(part_duration_total - scene_duration_total) > tolerance:
        fail("Part video duration differs from rendered scene-role clip total")

    return {
        "source": data,
        "scene_rows": rows,
        "checked_scene_rows": checked_rows,
        "parts": checked_parts,
        "part_paths": part_paths,
        "main_scene_count": sum(scene_counts.values()),
        "extra_role_scene_count": parts * 2,
        "part_duration_total": round(part_duration_total, 4),
        "scene_duration_total": round(scene_duration_total, 4),
    }

def validate_thumbnail(
    mode: str,
    dimensions: tuple[int, int],
) -> tuple[Path, dict]:
    path = safe_output("output/thumbnail.jpg", "Thumbnail", MIN_IMAGE_BYTES)

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image_format = image.format
            actual_dimensions = image.size
    except Exception as exc:
        fail(f"Thumbnail image is invalid: {exc}")

    if image_format != "JPEG" or actual_dimensions != dimensions:
        fail(
            f"{mode.upper()} thumbnail must be JPEG {dimensions}; "
            f"found {image_format} {actual_dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST)
    if clean(manifest.get("status")).lower() not in SUCCESS:
        fail("Thumbnail manifest is not completed")
    if clean(manifest.get("format")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")

    declared = (
        manifest.get("thumbnail")
        or manifest.get("path")
        or manifest.get("output")
        or manifest.get("file")
    )
    if declared:
        declared_path = safe_output(
            declared, "Thumbnail manifest path", MIN_IMAGE_BYTES
        )
        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to a different image")

    digest = sha256_file(path)
    return path, {
        "path": "output/thumbnail.jpg",
        "format": image_format,
        "dimensions": list(actual_dimensions),
        "bytes": path.stat().st_size,
        "sha256": digest,
        "manifest_sha256": sha256_file(THUMBNAIL_MANIFEST),
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


def validate_metadata(mode: str, title: str) -> tuple[Path, Path, Path, dict]:
    text_path = safe_output(
        "output/youtube_metadata.txt", "Metadata text", MIN_TEXT_BYTES
    )
    json_path = safe_output(
        "output/publish/youtube_metadata.json",
        "Metadata JSON",
        MIN_TEXT_BYTES,
    )
    tags_path = safe_output(
        "output/publish/youtube_tags.txt", "YouTube tags", MIN_TEXT_BYTES
    )

    try:
        content = text_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read metadata text: {exc}")

    sections: dict[str, list[str]] = {}
    current = None

    for line in content.splitlines():
        label = line.strip().strip("[]").rstrip(":").strip().upper()
        if label in {"TITLE", "DESCRIPTION", "TAGS"}:
            current = label
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.rstrip())

    text_title = " ".join(sections.get("TITLE", [])).strip()
    description = "\n".join(sections.get("DESCRIPTION", [])).strip()
    text_tags = parse_tags(" ".join(sections.get("TAGS", [])))

    if not text_title or not description or not text_tags:
        fail("Metadata text must contain TITLE, DESCRIPTION and TAGS")
    if text_title != title:
        fail("Metadata title does not match the story title")
    if len(text_title) > 100 or len(description) > 5000:
        fail("Metadata exceeds YouTube title/description limits")

    data = read_json(json_path)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Structured metadata is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Structured metadata FORMAT mismatch")

    json_title = data.get("title") or data.get("video_title")
    json_description = data.get("description") or data.get("video_description")
    tags = data.get("tags")

    if not isinstance(json_title, str) or not isinstance(json_description, str):
        fail("Structured metadata title or description is missing")
    if json_title.strip() != text_title:
        fail("Metadata text and JSON titles differ")
    if json_description.strip() != description:
        fail("Metadata text and JSON descriptions differ")
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
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            normalized.append(tag)

    if not 5 <= len(normalized) <= 20:
        fail(f"Metadata requires 5–20 unique tags; found {len(normalized)}")

    expected = {item.casefold() for item in normalized}
    if {item.casefold() for item in text_tags} != expected:
        fail("Metadata text tags differ from structured tags")

    try:
        dedicated = parse_tags(tags_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read dedicated tags file: {exc}")

    if {item.casefold() for item in dedicated} != expected:
        fail("Dedicated tags differ from structured metadata")
    if len(dedicated) != len(normalized) or len(text_tags) != len(normalized):
        fail("Duplicate tags detected in metadata files")

    return text_path, json_path, tags_path, {
        "title": text_title,
        "description_sha256": sha256_bytes(description.encode("utf-8")),
        "tags": normalized,
        "tag_count": len(normalized),
    }


def validate_captions() -> tuple[Path, Path, dict]:
    srt_path = safe_output(
        "output/captions/final_captions.srt", "Final captions SRT", 10
    )
    captions_path = safe_output(
        "output/captions/captions.json", "Caption manifest", 2
    )

    try:
        content = srt_path.read_text(encoding="utf-8-sig")
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
    last_end = 0.0
    characters = 0

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index} is incomplete")
        if int(lines[0].strip()) != index or lines[1].count("-->") != 1:
            fail(f"SRT cue {index} numbering or timing is invalid")

        left, right = [item.strip() for item in lines[1].split("-->")]
        start, end = seconds(left), seconds(right)

        if end <= start or start < previous_end - 0.001:
            fail(f"SRT cue {index} overlaps or has invalid timing")

        text_value = "\n".join(lines[2:]).strip()
        if not text_value:
            fail(f"SRT cue {index} has no caption text")

        previous_end = end
        last_end = max(last_end, end)
        characters += len(text_value)

    caption_data = read_json(captions_path)
    if clean(caption_data.get("status")).lower() not in SUCCESS:
        fail("Caption manifest is not completed")

    caption_rows = (
        caption_data.get("captions")
        or caption_data.get("scenes")
        or caption_data.get("items")
    )
    if not isinstance(caption_rows, list) or not caption_rows:
        fail("Caption manifest contains no caption records")
    extra_caption_rows = caption_data.get("extra_captions")
    if not isinstance(extra_caption_rows, list):
        fail("Caption manifest extra_captions list is missing")
    expected_extra = len(read_json(STORY).get("parts") or []) * 2
    if caption_data.get("total_extra_scenes") != expected_extra or len(extra_caption_rows) != expected_extra:
        fail("Caption manifest does not cover all Hook/Suspense/Resolution roles")
    if any(not isinstance(row, dict) or normalize_role(row) != "main" for row in caption_rows):
        fail("Main caption list contains a non-Main record")
    if any(not isinstance(row, dict) or normalize_role(row) == "main" for row in extra_caption_rows):
        fail("Extra caption list contains a Main record")

    declared_count = (
        caption_data.get("caption_count")
        or caption_data.get("total_captions")
        or caption_data.get("subtitle_count")
    )
    if declared_count is not None and int(declared_count) != len(blocks):
        fail("Caption manifest and SRT cue counts differ")

    return srt_path, captions_path, {
        "path": "output/captions/final_captions.srt",
        "cue_count": len(blocks),
        "text_characters": characters,
        "last_cue_end": round(last_end, 3),
        "sha256": sha256_file(srt_path),
        "manifest_sha256": sha256_file(captions_path),
    }


def validate_sound_and_audio() -> dict:
    audio = read_json(AUDIO)
    if clean(audio.get("status")).lower() not in SUCCESS:
        fail("Narration audio manifest is not completed")

    jobs = audio.get("jobs") or audio.get("audio_jobs") or audio.get("scenes")
    if not isinstance(jobs, list) or not jobs:
        fail("Narration manifest contains no audio jobs")

    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            fail(f"Invalid narration job {index}")
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Narration job {index} is incomplete")
        role = normalize_role(row)
        text_value = clean(row.get("text") or row.get("narration"))
        if not text_value or row.get("text_sha256") != sha256_bytes(text_value.encode("utf-8")):
            fail(f"{role} narration source-text fingerprint mismatch: {row.get('scene_id')}")

    sound = read_json(SOUND)
    if clean(sound.get("status")).lower() not in SUCCESS:
        fail("Sound-design manifest is not completed")

    sfx = read_json(SFX_VALIDATION)
    if clean(sfx.get("status")).lower() not in SUCCESS:
        fail("SFX timing validation is not completed")

    if clean(sound.get("format")).lower() not in {"", clean(
        read_json(VALIDATION).get("format")
    ).lower()}:
        fail("Sound-design FORMAT mismatch")
    render_data = read_json(RENDER)
    parts = len(render_data.get("parts") or [])
    visual_scene_count = len(collect_render_rows(render_data))
    if sound.get("total_visual_scene_records") != visual_scene_count:
        fail("Sound-design visual Scene count mismatch")
    if sfx.get("total_visual_scene_records") != visual_scene_count:
        fail("SFX validation visual Scene count mismatch")
    if sfx.get("duration_source") != "all_role_tts_audio_durations":
        fail("SFX validation duration source is invalid")
    if audio.get("total_jobs") is not None and int(audio["total_jobs"]) != len(jobs):
        fail("TTS manifest total_jobs mismatch")
    expected_audio_ids = {
        clean(row.get("scene_id")) for row in collect_render_rows(render_data)
    }
    if {clean(row.get("scene_id")) for row in jobs} != expected_audio_ids:
        fail("TTS manifest does not cover every rendered role")

    return {
        "narration": {
            "path": "output/narration/audio_jobs.json",
            "main_tts_job_count": len(jobs),
            "sha256": sha256_file(AUDIO),
        },
        "sound_design": {
            "path": "output/sound_design/sound_design_manifest.json",
            "sha256": sha256_file(SOUND),
        },
        "sfx_timing_validation": {
            "path": "output/sound_design/sfx_timing_validation.json",
            "sha256": sha256_file(SFX_VALIDATION),
        },
    }


def validate_photo_manifest(mode: str, dimensions: tuple[int, int], fps: float) -> dict:
    data = read_json(PHOTO)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Photo Motion manifest is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Photo Motion FORMAT mismatch")
    if (int(data.get("width", 0)), int(data.get("height", 0))) != dimensions:
        fail("Photo Motion dimensions mismatch")
    if abs(parse_fps(data.get("fps"), "Photo Motion FPS") - fps) > FPS_TOLERANCE:
        fail("Photo Motion FPS mismatch")
    if data.get("part_outputs_only") is not True:
        fail("Photo Motion manifest must confirm Part-only output")
    if data.get("combined_video_created") is not False:
        fail("Photo Motion manifest must confirm no combined video")

    rows = data.get("scenes")
    if not isinstance(rows, list) or not rows:
        fail("Photo Motion manifest contains no scene records")

    return {
        "path": "output/photo_motion/photo_motion_manifest.json",
        "scene_record_count": len(rows),
        "sha256": sha256_file(PHOTO),
    }


def package_member(root_name: str, relative: str, slug: str) -> str:
    relative = PurePosixPath(relative).as_posix()
    match = re.fullmatch(r"parts/part_(\d+)\.mp4", relative)
    if match:
        return (
            f"{root_name}/parts/"
            f"{slug}-part-{int(match.group(1)):02d}.mp4"
        )
    if relative == "thumbnail.jpg":
        return f"{root_name}/{slug}-thumbnail.jpg"
    return f"{root_name}/{relative}"


def remap_manifest_paths(value: Any, root_name: str, slug: str) -> Any:
    if isinstance(value, dict):
        return {
            key: remap_manifest_paths(item, root_name, slug)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            remap_manifest_paths(item, root_name, slug)
            for item in value
        ]
    if isinstance(value, str) and value.startswith("output/"):
        return package_member(root_name, value[len("output/"):], slug)
    return value


def portable_render_manifest(
    source: dict,
    checked_parts: list[dict],
    root_name: str,
    slug: str,
) -> dict:
    data = json.loads(json.dumps(source))

    for key in (
        "scenes", "completed_scenes", "all_completed_scenes",
        "extra_scenes", "completed_count", "all_completed_scene_count",
        "extra_scene_count", "final_video", "alias",
        "combined_video", "combined_output", "pre_caption_video",
    ):
        data.pop(key, None)

    portable_parts = []
    for item in checked_parts:
        portable_parts.append({
            "part": item["part"],
            "path": package_member(root_name, item["path"], slug),
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
    return remap_manifest_paths(data, root_name, slug)


def resolve_scene_narration(
    photo_row: dict,
    narration_row: dict | None,
    audio_row: dict | None,
    scene_id: str,
    role: str,
) -> str:
    """Resolve spoken text from narration.json, not the visual-only manifest."""
    if narration_row is None:
        fail(f"Narration metadata record is missing: {scene_id}")
    if audio_row is None:
        fail(f"Role TTS audio is missing: {scene_id}")
    if normalize_role(narration_row) != role or normalize_role(audio_row) != role:
        fail(f"Photo Motion/narration/audio role mismatch: {scene_id}")
    if narration_row.get("voiceover_enabled") is not True:
        fail(f"Narration metadata voiceover is disabled: {scene_id}")
    if photo_row.get("voiceover_enabled") is not True:
        fail(f"Photo Motion voiceover is disabled: {scene_id}")

    narration_text = clean(narration_row.get("narration") or narration_row.get("text"))
    audio_text = clean(audio_row.get("text") or audio_row.get("narration"))
    if not narration_text or not audio_text:
        fail(f"Narration text is missing: {scene_id}")
    if narration_text != audio_text:
        fail(f"Narration text differs from TTS source text: {scene_id}")

    expected_source = (
        "ai_generated_role_narration"
        if role in {"hook", "suspense", "resolution"}
        else "locked_part_text"
    )
    narration_source = clean(narration_row.get("narration_source"))
    audio_source = clean(audio_row.get("narration_source"))
    photo_source = clean(photo_row.get("narration_source"))
    if narration_source != expected_source or audio_source != expected_source or photo_source != expected_source:
        fail(f"Narration source mismatch: {scene_id}")

    text_hash = hashlib.sha256(narration_text.encode("utf-8")).hexdigest()
    declared_hash = clean(audio_row.get("text_sha256")).lower()
    if declared_hash != text_hash:
        fail(f"Narration/TTS text SHA-256 mismatch: {scene_id}")
    if clean(photo_row.get("source_text_sha256")).lower() != text_hash:
        fail(f"Photo Motion/narration text SHA-256 mismatch: {scene_id}")
    return narration_text


def selected_package_files(render: dict) -> list[Path]:
    selected = {path.resolve() for path in REQUIRED_FILES}
    selected.update(path.resolve() for path in render["part_paths"])

    photo_data = read_json(PHOTO)
    photo_rows = photo_data.get("scenes")
    if not isinstance(photo_rows, list) or not photo_rows:
        fail("Photo Motion manifest has no scene asset references")

    narration_metadata = read_json(OUT / "narration" / "narration.json")
    narration_by_id = {}
    for field in ("scenes", "extra_scenes"):
        records = narration_metadata.get(field, [])
        if not isinstance(records, list):
            fail(f"Narration metadata {field} must be a list")
        for record in records:
            if not isinstance(record, dict):
                fail(f"Narration metadata {field} contains an invalid record")
            sid = clean(record.get("scene_id"))
            if not sid or sid in narration_by_id:
                fail(f"Narration metadata has a missing or duplicate scene ID: {sid}")
            profile = record.get("voice_profile")
            if not isinstance(profile, dict) or not clean(profile.get("profile_id")):
                fail(f"{sid}: voice profile is missing from package metadata")
            if not re.fullmatch(r"[0-9a-f]{64}", clean(profile.get("fingerprint"))):
                fail(f"{sid}: voice profile fingerprint is invalid")
            narration_by_id[sid] = record

    music_metadata = read_json(FLOW_MANIFEST)
    compositions = music_metadata.get("role_compositions", {})
    if compositions and not isinstance(compositions, dict):
        fail("Music role composition metadata is invalid")
    for role, composition in (compositions.items() if isinstance(compositions, dict) else []):
        if not isinstance(composition, dict):
            fail(f"Music role composition {role} is invalid")
        provider = clean(composition.get("provider") or "lyria").lower()
        if provider not in {"lyria", "eleven_music", "stable_audio", "local"}:
            fail(f"Unsupported music provider in package: {provider}")

    audio_data = read_json(AUDIO)
    audio_rows = (
        audio_data.get("jobs")
        or audio_data.get("audio_jobs")
        or audio_data.get("scenes")
    )
    if not isinstance(audio_rows, list) or not audio_rows:
        fail("Narration manifest has no scene audio references")

    expected_scene_ids = {
        clean(row.get("scene_id"))
        for row in render["scene_rows"]
        if isinstance(row, dict)
    }
    expected_main_ids = {
        clean(row.get("scene_id"))
        for row in render["scene_rows"]
        if isinstance(row, dict)
    }
    photo_by_id = {}
    for row in photo_rows:
        if not isinstance(row, dict):
            fail("Invalid Photo Motion asset record")
        sid = clean(row.get("scene_id"))
        if not sid or sid in photo_by_id:
            fail(f"Missing or duplicate Photo Motion scene ID: {sid}")
        photo_by_id[sid] = row
    audio_by_id = {}
    for row in audio_rows:
        if not isinstance(row, dict):
            fail("Invalid narration audio asset record")
        sid = clean(row.get("scene_id"))
        if not sid or sid in audio_by_id:
            fail(f"Missing or duplicate narration audio scene ID: {sid}")
        audio_by_id[sid] = row
    if set(photo_by_id) != expected_scene_ids or set(audio_by_id) != expected_main_ids:
        fail("Photo Motion and TTS must cover every visual scene role")
    if set(narration_by_id) != expected_scene_ids:
        fail(
            "Narration metadata must cover every visual scene role exactly once: "
            f"missing={sorted(expected_scene_ids - set(narration_by_id))}, "
            f"extra={sorted(set(narration_by_id) - expected_scene_ids)}"
        )

    scene_image_paths = set()
    scene_audio_paths = set()
    flow_audio_paths = set()
    for sid in sorted(expected_scene_ids):
        photo_row = photo_by_id[sid]
        role = normalize_role(photo_row)
        audio_row = audio_by_id.get(sid)
        image_value = (
            photo_row.get("image_path")
            or photo_row.get("image")
            or photo_row.get("output_path")
        )
        image_path = safe_output(image_value, f"Scene image {sid}", MIN_IMAGE_BYTES)
        if image_path.suffix.lower() not in IMAGE_SUFFIXES:
            fail(f"Scene image is not a supported image file: {sid}")
        declared_image_hash = clean(photo_row.get("image_sha256")).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", declared_image_hash) or declared_image_hash != sha256_file(image_path):
            fail(f"Scene image SHA-256 mismatch: {sid}")

        narration_row = narration_by_id.get(sid)
        narration_text = resolve_scene_narration(
            photo_row, narration_row, audio_row, sid, role
        )
        audio_value = (
            audio_row.get("audio_path") or audio_row.get("audio_file")
            or audio_row.get("audio") or audio_row.get("output")
        )
        audio_path = safe_output(audio_value, f"Scene narration audio {sid}", 100)
        if audio_path.suffix.lower() not in AUDIO_SUFFIXES:
            fail(f"Scene narration is not a supported audio file: {sid}")
        declared_audio_hash = clean(
            audio_row.get("audio_sha256") or audio_row.get("sha256")
        ).lower()
        photo_audio_hash = clean(photo_row.get("audio_sha256")).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", declared_audio_hash) or declared_audio_hash != sha256_file(audio_path):
            fail(f"Scene narration SHA-256 mismatch: {sid}")
        if photo_audio_hash != declared_audio_hash:
            fail(f"Photo Motion/narration audio hashes differ: {sid}")
        photo_audio_path = photo_row.get("audio_path") or photo_row.get("narration_audio")
        if not isinstance(photo_audio_path, str) or safe_output(
            photo_audio_path, f"Photo Motion narration path {sid}", 100
        ).resolve() != audio_path.resolve():
            fail(f"Photo Motion/narration audio paths differ: {sid}")
        declared_duration = audio_row.get(
            "actual_audio_duration_seconds",
            audio_row.get("audio_duration", audio_row.get("duration")),
        )
        try:
            declared_duration = float(declared_duration)
        except (TypeError, ValueError):
            fail(f"Measured narration duration is missing: {sid}")
        if not math.isfinite(declared_duration) or declared_duration <= 0:
            fail(f"Measured narration duration is invalid: {sid}")
        if (
            audio_row.get("duration_source") != "ffprobe_measured"
            and audio_row.get("audio_duration_source") != "ffprobe_measured"
        ):
            fail(f"Narration duration is not ffprobe-measured: {sid}")
        if abs(probe_audio_duration(audio_path) - declared_duration) > 0.2:
            fail(f"Scene narration file duration differs from TTS manifest: {sid}")
        if abs(float(photo_row.get("duration", 0)) - declared_duration) > 0.2:
            fail(f"Photo Motion/narration duration mismatch: {sid}")
        if photo_row.get("source_text_sha256") != audio_row.get("text_sha256"):
            fail(f"Photo Motion/TTS source text hash mismatch: {sid}")
        if clean(audio_row.get("text")) != narration_text:
            fail(f"Packager narration read-back mismatch: {sid}")
        scene_audio_paths.add(audio_path.resolve())

        try:
            from PIL import Image
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                if image.size != (
                    int(photo_data.get("width", 0)),
                    int(photo_data.get("height", 0)),
                ):
                    fail(f"Scene image dimensions mismatch: {sid}")
        except Exception as exc:
            fail(f"Scene image integrity check failed: {sid}: {exc}")
        scene_image_paths.add(image_path.resolve())

    if len(scene_image_paths) != len(expected_scene_ids):
        fail("Multiple scenes reference the same source image")
    if len(scene_audio_paths) != len(expected_main_ids):
        fail("Main Scenes do not have unique narration audio files")

    selected.update(scene_image_paths)
    selected.update(scene_audio_paths)

    flow_data = read_json(FLOW_MANIFEST)
    flow_status = clean(flow_data.get("status")).lower()
    if flow_status not in {"completed", "disabled", "unavailable"}:
        fail(f"Flow Music manifest has invalid status: {flow_status!r}")

    flow_segments = flow_data.get("role_segments") or []
    if not isinstance(flow_segments, list):
        fail("Flow Music role-segment inventory is invalid")
    segment_ids = []
    for index, row in enumerate(flow_segments, 1):
        if not isinstance(row, dict):
            fail(f"Invalid Flow Music segment record {index}")
        sid = clean(row.get("scene_id"))
        if sid:
            segment_ids.append(sid)
        value = row.get("path") or row.get("audio_path") or row.get("file")
        if not value:
            if flow_status == "completed":
                fail(f"Flow Music segment path is missing: {sid}")
            continue
        segment_path = safe_output(value, f"Flow Music segment {sid or index}", 100)
        declared = clean(row.get("sha256")).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", declared) or declared != sha256_file(segment_path):
            fail(f"Flow Music segment SHA-256 mismatch: {sid or index}")
        if segment_path.suffix.lower() not in AUDIO_SUFFIXES:
            fail(f"Flow Music segment is not an audio file: {sid or index}")
        try:
            duration = float(row.get("duration_seconds"))
        except (TypeError, ValueError):
            fail(f"Flow Music segment duration is invalid: {sid or index}")
        if not math.isfinite(duration) or duration <= 0 or abs(probe_audio_duration(segment_path) - duration) > 0.65:
            fail(f"Flow Music segment duration mismatch: {sid or index}")
        flow_audio_paths.add(segment_path.resolve())

    if flow_status == "completed":
        if len(segment_ids) != len(expected_scene_ids) or set(segment_ids) != expected_scene_ids or len(segment_ids) != len(set(segment_ids)):
            fail("Completed Flow Music segments do not match every scene exactly once")

    compositions = flow_data.get("role_compositions") or {}
    if not isinstance(compositions, dict):
        fail("Flow Music role-composition inventory is invalid")
    for role, row in compositions.items():
        if not isinstance(row, dict):
            fail(f"Invalid Flow Music composition record: {role}")
        for path_key, hash_key in (("path", "sha256"), ("source_path", "source_sha256")):
            value = row.get(path_key)
            if not value:
                continue
            composition_path = safe_output(value, f"Flow Music {role} {path_key}", 100)
            declared = clean(row.get(hash_key)).lower()
            if not re.fullmatch(r"[a-f0-9]{64}", declared) or declared != sha256_file(composition_path):
                fail(f"Flow Music composition SHA-256 mismatch: {role} {path_key}")
            if composition_path.suffix.lower() not in AUDIO_SUFFIXES:
                fail(f"Flow Music composition is not audio: {role} {path_key}")
            flow_audio_paths.add(composition_path.resolve())

    selected.update(flow_audio_paths)

    depth_data = read_json(OUT / "visuals" / "depth_maps" / "depth_manifest.json")
    depth_rows = depth_data.get("scenes")
    if not isinstance(depth_rows, list) or len(depth_rows) != len(expected_scene_ids):
        fail("Depth manifest does not contain one record for every scene")
    depth_paths = set()
    for row in depth_rows:
        if not isinstance(row, dict):
            fail("Invalid depth-map record")
        sid = clean(row.get("scene_id"))
        if sid not in expected_scene_ids:
            fail(f"Depth map references unknown scene: {sid}")
        value = row.get("depth_map") or row.get("depth_path")
        depth_path = safe_output(value, f"Depth map {sid}", 100)
        declared = clean(row.get("depth_sha256")).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", declared) or declared != sha256_file(depth_path):
            fail(f"Depth map SHA-256 mismatch: {sid}")
        if depth_path.suffix.lower() not in IMAGE_SUFFIXES:
            fail(f"Depth map is not a supported image file: {sid}")
        depth_paths.add(depth_path.resolve())
    if len(depth_paths) != len(expected_scene_ids):
        fail("Depth map files are missing or shared between scenes")
    selected.update(depth_paths)

    sound_data = read_json(SOUND)
    tracks = sound_data.get("tracks", {})
    track_rows = list(tracks.values()) if isinstance(tracks, dict) else tracks if isinstance(tracks, list) else []
    for index, row in enumerate(track_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid sound track record {index}")
        value = row.get("path") or row.get("audio_path") or row.get("audio") or row.get("output") or row.get("file")
        if value:
            track_path = safe_output(value, f"Sound-design track {index}", 100)
            if track_path.suffix.lower() not in AUDIO_SUFFIXES:
                fail(f"Unsupported sound-design track type: {track_path}")
            selected.add(track_path.resolve())

    sfx_data = read_json(SFX_CUES)
    cue_rows = sfx_data.get("cues") or sfx_data.get("sfx_cues") or sfx_data.get("jobs") or []
    if not isinstance(cue_rows, list):
        fail("SFX cue manifest rows are invalid")
    for index, row in enumerate(cue_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid SFX cue record {index}")
        value = row.get("path") or row.get("audio_path") or row.get("audio") or row.get("file") or row.get("output")
        if value:
            cue_path = safe_output(value, f"SFX cue audio {index}", 100)
            if cue_path.suffix.lower() not in AUDIO_SUFFIXES:
                fail(f"Unsupported SFX cue audio type: {cue_path}")
            selected.add(cue_path.resolve())

    for path in selected:
        if not path.is_file():
            fail(f"Required package file is missing: {path}")
        if path.is_symlink():
            fail(f"Symlink is forbidden in package: {path}")
        try:
            path.resolve().relative_to(OUT.resolve())
        except ValueError:
            fail(f"Package file escapes output/: {path}")

        suffix = path.suffix.lower()
        if suffix in VIDEO_SUFFIXES and path not in {
            item.resolve() for item in render["part_paths"]
        }:
            fail(f"Non-Part video is forbidden in package: {path}")
        allowed_audio = set(scene_audio_paths) | set(flow_audio_paths)
        allowed_audio.update(
            safe_output(
                row.get("path") or row.get("audio_path") or row.get("audio") or row.get("output") or row.get("file"),
                "Sound-design track",
                100,
            ).resolve()
            for row in track_rows
            if isinstance(row, dict)
            and (row.get("path") or row.get("audio_path") or row.get("audio") or row.get("output") or row.get("file"))
        )
        allowed_audio.update(
            safe_output(
                row.get("path") or row.get("audio_path") or row.get("audio") or row.get("file") or row.get("output"),
                "SFX cue audio",
                100,
            ).resolve()
            for row in cue_rows
            if isinstance(row, dict)
            and (row.get("path") or row.get("audio_path") or row.get("audio") or row.get("file") or row.get("output"))
        )
        if suffix in IMAGE_SUFFIXES and path not in scene_image_paths and path not in depth_paths and path != THUMBNAIL.resolve():
            fail(f"Unreferenced image is forbidden in package: {path}")
        if suffix in AUDIO_SUFFIXES and path not in allowed_audio:
            fail(f"Unreferenced audio file is forbidden in package: {path}")
        if suffix == ".zip":
            fail(f"Nested ZIP is forbidden: {path}")
        if path.name.endswith(".tmp") or suffix in TEMP_SUFFIXES:
            fail(f"Temporary file is forbidden: {path}")

    return sorted(selected, key=lambda item: output_relative(item))


def validate_zip_member(name: str, root_name: str) -> None:
    member = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or not member.parts
        or member.parts[0] != root_name
        or any(piece in {"", ".", ".."} for piece in member.parts)
    ):
        fail(f"Unsafe ZIP member path: {name!r}")
    if member.suffix.lower() == ".zip":
        fail(f"Nested ZIP is forbidden: {name}")


def validate_zip_structure(archive: zipfile.ZipFile, root_name: str) -> list[str]:
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
        validate_zip_member(info.filename, root_name)
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
    root_name: str,
    archive_name: str,
    part_records: list[dict],
    package_files: list[Path],
    validation: dict,
    thumbnail_info: dict,
    metadata_info: dict,
    caption_info: dict,
    sound_info: dict,
    photo_info: dict,
    scene_counts: dict[int, int],
) -> dict:
    packaged_parts = []
    for item in part_records:
        packaged_parts.append({
            **item,
            "path": package_member(root_name, item["path"], slug),
        })

    return {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "story_title": title,
        "story_slug": slug,
        "package_root": root_name,
        "archive": archive_name,
        "parts": len(packaged_parts),
        "configured_parts": len(packaged_parts),
        "part_numbers": [item["part"] for item in packaged_parts],
        "part_videos_only": True,
        "combined_video_included": False,
        "format_alias_included": False,
        "part_files": packaged_parts,
        "scene_image_members": sorted(
            package_member(root_name, output_relative(path), slug)
            for path in package_files
            if path.suffix.lower() in IMAGE_SUFFIXES
            and path.resolve() != THUMBNAIL.resolve()
            and "visuals/images/" in output_relative(path)
        ),
        "depth_map_members": sorted(
            package_member(root_name, output_relative(path), slug)
            for path in package_files
            if path.suffix.lower() in IMAGE_SUFFIXES
            and "visuals/depth_maps/" in output_relative(path)
        ),
        "scene_audio_members": sorted(
            package_member(root_name, output_relative(path), slug)
            for path in package_files
            if path.suffix.lower() in AUDIO_SUFFIXES
        ),
        "project_files": sorted(
            package_member(root_name, output_relative(path), slug)
            for path in package_files
            if path in {item.resolve() for item in PROJECT_FILES}
        ),
        "thumbnail": package_member(root_name, "thumbnail.jpg", slug),
        "thumbnail_info": thumbnail_info,
        "metadata": metadata_info,
        "captions": caption_info,
        "sound_manifests": sound_info,
        "photo_motion": photo_info,
        "validation_report": package_member(
            root_name, "validation_report.json", slug
        ),
        "render_manifest": package_member(
            root_name, "scene_renders/render_manifest.json", slug
        ),
        "actual_scene_counts": {
            str(part): count for part, count in scene_counts.items()
        },
        "main_scene_count": sum(scene_counts.values()),
        "embedded_manifest_self_hash_omitted": True,
        "archive_sha256_embedded": False,
        "validation_checks": validation["checks"],
        "manifest_zip_consistency": True,
    }


def build_archive(
    archive_path: Path,
    files: list[Path],
    render: dict,
    embedded_manifest: dict,
    root_name: str,
    slug: str,
) -> list[dict]:
    temporary = archive_path.with_name(archive_path.name + ".tmp")
    temporary.unlink(missing_ok=True)

    records = []
    expected_members = {
        package_member(root_name, output_relative(path), slug)
        for path in files
    }
    expected_members.add(f"{root_name}/package_manifest.json")

    portable_render = portable_render_manifest(
        render["source"], render["parts"], root_name, slug
    )

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=ZIP_LEVEL,
            allowZip64=True,
        ) as archive:
            for path in files:
                member = package_member(
                    root_name, output_relative(path), slug
                )

                if path.resolve() == RENDER.resolve():
                    payload = (
                        json.dumps(
                            portable_render,
                            ensure_ascii=False,
                            indent=2,
                        ) + "\n"
                    ).encode("utf-8")
                elif path.suffix.lower() == ".json":
                    try:
                        value = json.loads(
                            path.read_text(encoding="utf-8-sig")
                        )
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        fail(f"Invalid JSON package file {path.name}: {exc}")
                    value = remap_manifest_paths(value, root_name, slug)
                    payload = (
                        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
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

            embedded_payload = (
                json.dumps(
                    embedded_manifest,
                    ensure_ascii=False,
                    indent=2,
                ) + "\n"
            ).encode("utf-8")
            embedded_name = f"{root_name}/package_manifest.json"
            archive.writestr(embedded_name, embedded_payload)
            records.append({
                "path": embedded_name,
                "bytes": len(embedded_payload),
                "sha256": sha256_bytes(embedded_payload),
            })

        with zipfile.ZipFile(temporary, "r") as archive:
            names = validate_zip_structure(archive, root_name)
            if set(names) != expected_members:
                fail("ZIP entries differ from selected package files")

            for item in records:
                payload = archive.read(item["path"])
                if (
                    len(payload) != item["bytes"]
                    or sha256_bytes(payload) != item["sha256"]
                ):
                    fail(f"ZIP member hash/size mismatch: {item['path']}")

        os.replace(temporary, archive_path)
        return records

    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def verify_archive(
    archive_path: Path,
    records: list[dict],
    mode: str,
    title: str,
    slug: str,
    root_name: str,
    parts: int,
    dimensions: tuple[int, int],
    fps: float,
) -> dict:
    if not archive_path.is_file() or archive_path.stat().st_size < MIN_VIDEO_BYTES:
        fail("Final ZIP is missing or too small")

    expected_hashes = {item["path"]: item for item in records}
    expected_videos = {
        f"{root_name}/parts/{slug}-part-{part:02d}.mp4"
        for part in range(1, parts + 1)
    }

    with zipfile.ZipFile(archive_path, "r") as archive:
        names = validate_zip_structure(archive, root_name)
        if set(names) != set(expected_hashes):
            fail("ZIP entries differ from package hash manifest")

        for name, item in expected_hashes.items():
            payload = archive.read(name)
            if (
                len(payload) != item["bytes"]
                or sha256_bytes(payload) != item["sha256"]
            ):
                fail(f"ZIP member hash/size mismatch: {name}")

        videos = {
            name for name in names
            if PurePosixPath(name).suffix.lower() in VIDEO_SUFFIXES
        }
        if videos != expected_videos:
            fail("ZIP contains missing, extra or incorrectly named Part videos")

        thumbnail_member = f"{root_name}/{slug}-thumbnail.jpg"
        if thumbnail_member not in names:
            fail("Story-named thumbnail is missing")

        embedded_name = f"{root_name}/package_manifest.json"
        embedded = json.loads(archive.read(embedded_name).decode("utf-8"))

        expected_embedded = {
            "status": "completed",
            "version": VERSION,
            "format": mode,
            "story_title": title,
            "story_slug": slug,
            "package_root": root_name,
            "parts": parts,
            "configured_parts": parts,
            "part_videos_only": True,
            "combined_video_included": False,
            "format_alias_included": False,
            "embedded_manifest_self_hash_omitted": True,
            "manifest_zip_consistency": True,
        }
        for key, expected in expected_embedded.items():
            if embedded.get(key) != expected:
                fail(f"Embedded package manifest mismatch: {key}")

        if embedded.get("thumbnail") != thumbnail_member:
            fail("Embedded thumbnail path is invalid")
        actual_image_members = sorted(
            name for name in names
            if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES
            and "visuals/images/" in name
        )
        actual_depth_members = sorted(
            name for name in names
            if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES
            and "visuals/depth_maps/" in name
        )
        actual_audio_members = sorted(
            name for name in names
            if PurePosixPath(name).suffix.lower() in AUDIO_SUFFIXES
        )
        if embedded.get("scene_image_members") != actual_image_members:
            fail("ZIP scene image inventory differs from embedded manifest")
        if embedded.get("depth_map_members") != actual_depth_members:
            fail("ZIP depth-map inventory differs from embedded manifest")
        if embedded.get("scene_audio_members") != actual_audio_members:
            fail("ZIP scene audio inventory differs from embedded manifest")
        declared_project_files = embedded.get("project_files")
        if not isinstance(declared_project_files, list) or any(
            name not in names for name in declared_project_files
        ):
            fail("ZIP project-file inventory is incomplete")

        render_member = f"{root_name}/scene_renders/render_manifest.json"
        if render_member not in names:
            fail("Packaged render manifest is missing")

        packaged_render = json.loads(
            archive.read(render_member).decode("utf-8")
        )
        if (
            packaged_render.get("status") != "completed"
            or packaged_render.get("format") != mode
            or packaged_render.get("part_outputs_only") is not True
            or packaged_render.get("captions_burned") is not True
        ):
            fail("Packaged render manifest is invalid")

        packaged_parts = packaged_render.get("parts")
        if not isinstance(packaged_parts, list) or len(packaged_parts) != parts:
            fail("Packaged render Part list is invalid")

        with tempfile.TemporaryDirectory(prefix="katha-package-verify-") as temp:
            for number, row in enumerate(packaged_parts, 1):
                expected_member = (
                    f"{root_name}/parts/{slug}-part-{number:02d}.mp4"
                )
                if (
                    not isinstance(row, dict)
                    or int(row.get("part", -1)) != number
                    or row.get("path") != expected_member
                ):
                    fail(f"Packaged Part {number} reference is invalid")

                extracted = Path(temp) / f"part_{number:02d}.mp4"
                with archive.open(expected_member) as source:
                    with extracted.open("wb") as target:
                        shutil.copyfileobj(source, target)

                info = probe_video(extracted, dimensions, fps, require_audio=True)
                digest = sha256_file(extracted)
                if digest != row.get("sha256"):
                    fail(f"Packaged Part {number} SHA256 mismatch")
                if extracted.stat().st_size != int(row.get("bytes", -1)):
                    fail(f"Packaged Part {number} byte count mismatch")

    return {
        "status": "valid",
        "crc": "valid",
        "entry_count": len(names),
        "part_count": parts,
        "part_videos_only": True,
        "package_root": root_name,
        "thumbnail": thumbnail_member,
        "member_hashes_verified": len(records),
    }


def verify_package_report(archive_path: Path, report: dict) -> None:
    saved = read_json(REPORT)

    for key in (
        "status", "version", "format", "story_title",
        "story_slug", "package_root", "archive",
        "archive_sha256", "archive_bytes", "file_count",
    ):
        if saved.get(key) != report.get(key):
            fail(f"Saved package report mismatch: {key}")

    if saved.get("status") != "completed":
        fail("Package report is not completed")
    if saved.get("archive_sha256") != sha256_file(archive_path):
        fail("Package report archive SHA256 mismatch")
    if saved.get("archive_bytes") != archive_path.stat().st_size:
        fail("Package report archive size mismatch")
    if saved.get("zip_integrity") != "valid":
        fail("Package report ZIP integrity is invalid")
    if saved.get("manifest_zip_consistency") != "valid":
        fail("Package report ZIP consistency is invalid")

    files = saved.get("files")
    if not isinstance(files, list) or len(files) != saved.get("file_count"):
        fail("Package report file list/count mismatch")

    seen = set()
    for item in files:
        if not isinstance(item, dict):
            fail("Invalid package file record")
        name = item.get("path")
        validate_zip_member(name, saved["package_root"])
        if name in seen:
            fail(f"Duplicate package report member: {name}")
        seen.add(name)
        if not isinstance(item.get("bytes"), int) or item["bytes"] <= 0:
            fail(f"Invalid package file size: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            fail(f"Invalid package file SHA256: {name}")


def main() -> int:
    if not shutil.which("ffprobe"):
        fail("ffprobe is required but unavailable")
    if not OUT.is_dir() or not INPUT_FILE.is_file():
        fail("output/ or Input/topic.txt is missing")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")

    selected_parts = get_selected_parts(config, required=True)
    if selected_parts is None:
        fail("Selected PARTS has not been resolved")
    parts = int(selected_parts)
    if not 1 <= parts <= 1000:
        fail("Selected PARTS is outside the permitted range")

    scenes_min, scenes_max = get_scenes_range(config)
    scenes_min, scenes_max = int(scenes_min), int(scenes_max)
    if not 1 <= scenes_min <= scenes_max <= 100:
        fail("SCENES range is invalid")

    fps = parse_fps(get_fps(config), "Configured FPS")
    if not 0 < fps <= 60:
        fail("Configured FPS is outside the permitted range")

    dimensions = (720, 1280) if mode == "short" else (1280, 720)

    story = read_json(STORY)
    title = story_title(story)
    slug = safe_slug(title)

    render_source = read_json(RENDER)
    render_parts = render_source.get("parts")
    if not isinstance(render_parts, list) or len(render_parts) != parts:
        fail("Render manifest Part count does not match selected PARTS")

    # Actual scene counts are read from the validated render manifest.
    render_rows = collect_render_rows(render_source)
    scene_counts = {part: 0 for part in range(1, parts + 1)}
    for row in render_rows:
        part, role, scene = canonical_scene_key(row)
        if not 1 <= part <= parts:
            fail(f"Render record references unselected Part {part}")
        if role == "main":
            scene_counts[part] += 1

    for part, count in scene_counts.items():
        if not scenes_min <= count <= scenes_max:
            fail(
                f"Part {part} actual scene count {count} is outside "
                f"configured SCENES range {scenes_min}-{scenes_max}"
            )

    validation = validate_validation_report(
        mode, parts, scene_counts, title
    )
    render = validate_render(
        mode, parts, scene_counts, dimensions, fps
    )
    photo_info = validate_photo_manifest(mode, dimensions, fps)
    thumbnail, thumbnail_info = validate_thumbnail(mode, dimensions)
    metadata, metadata_json, tags, metadata_info = validate_metadata(mode, title)
    captions, captions_json, caption_info = validate_captions()
    sound_info = validate_sound_and_audio()

    archive_name = f"katha-lok-{slug}-{mode}-output.zip"
    archive_path = ROOT / archive_name
    root_name = f"{slug}-{mode}"

    files = selected_package_files(render)
    embedded_manifest = build_embedded_manifest(
        mode=mode,
        title=title,
        slug=slug,
        root_name=root_name,
        archive_name=archive_name,
        part_records=render["parts"],
        package_files=files,
        validation=validation,
        thumbnail_info=thumbnail_info,
        metadata_info=metadata_info,
        caption_info=caption_info,
        sound_info=sound_info,
        photo_info=photo_info,
        scene_counts=scene_counts,
    )

    log("=" * 68)
    log("KATHA LOK AI — VERIFIED OUTPUT PACKAGER")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("STORY:", title)
    log("ACTUAL PARTS:", parts)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("MAIN SCENES:", sum(scene_counts.values()))
    log("EXTRA ROLE SCENES:", parts * 2)
    log("PART VIDEOS:", len(render["parts"]))
    log("PACKAGE FILES:", len(files) + 1)

    temporary_archive = archive_path.with_name(
        archive_path.name + ".verified.tmp"
    )
    temporary_archive.unlink(missing_ok=True)

    try:
        records = build_archive(
            temporary_archive,
            files,
            render,
            embedded_manifest,
            root_name,
            slug,
        )

        zip_validation = verify_archive(
            temporary_archive,
            records,
            mode,
            title,
            slug,
            root_name,
            parts,
            dimensions,
            fps,
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
        "package_root": root_name,
        "package_layout": "story-root-with-story-named-part-videos-and-thumbnail",
        "parts": parts,
        "configured_parts": parts,
        "part_numbers": list(range(1, parts + 1)),
        "part_videos_only": True,
        "combined_video_included": False,
        "format_alias_included": False,
        "scenes_range": [scenes_min, scenes_max],
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_per_part": scenes_min,
        "actual_scene_counts": {
            str(part): count for part, count in scene_counts.items()
        },
        "expected_scenes": sum(scene_counts.values()),
        "main_scene_count": sum(scene_counts.values()),
        "extra_scene_count": parts * 2,
        "part_count": len(render["parts"]),
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
        "photo_motion": photo_info,
        "validation_report": output_relative(VALIDATION),
        "render_manifest": output_relative(RENDER),
        "part_files": render["parts"],
        "part_duration_total": render["part_duration_total"],
        "scene_duration_total": render["scene_duration_total"],
        "archive": archive_name,
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": archive_hash,
        "file_count": len(records),
        "scene_image_count": sum(
            path.suffix.lower() in IMAGE_SUFFIXES and path.resolve() != THUMBNAIL.resolve()
            for path in files
        ),
        "scene_audio_count": sum(
            path.suffix.lower() in AUDIO_SUFFIXES
            for path in files
        ),
        "project_file_count": sum(
            path.resolve() in {item.resolve() for item in PROJECT_FILES}
            for path in files
        ),
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
            archive.read(f"{root_name}/package_manifest.json").decode("utf-8")
        )
        if (
            embedded.get("archive") != archive_name
            or embedded.get("format") != mode
            or embedded.get("parts") != parts
            or embedded.get("story_title") != title
            or embedded.get("status") != "completed"
        ):
            fail("Final embedded package identity verification failed")

    temporary_name = ARCHIVE_NAME_FILE.with_name(
        ARCHIVE_NAME_FILE.name + ".tmp"
    )
    try:
        with temporary_name.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(archive_name + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, ARCHIVE_NAME_FILE)
    finally:
        temporary_name.unlink(missing_ok=True)

    log("-" * 68)
    log("PACKAGE STATUS: completed")
    log("FORMAT:", mode)
    log("STORY:", title)
    log("PARTS:", parts)
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("PART VIDEOS ONLY: yes")
    log("COMBINED VIDEO: excluded")
    log("FORMAT ALIAS: excluded")
    log("PACKAGE ROOT:", root_name)
    log("THUMBNAIL:", f"{slug}-thumbnail.jpg")
    log("ZIP:", archive_name)
    log("ZIP BYTES:", archive_path.stat().st_size)
    log("ZIP SHA256:", archive_hash)
    log("ZIP CRC: valid")
    log("PACKAGED FILES:", len(records))
    log("MANIFEST:", output_relative(REPORT))
    log("=" * 68)
    return 0




# _PHOTO_MOTION_EVENT_WRAPPED: preserve original stage behavior and record factual outcomes.
_photo_motion_original_main = main
def main():
    import time as _pm_time
    _pm_started = _pm_time.monotonic()
    try:
        try:
            from production_events import record_event as _pm_record
        except ImportError:
            from scripts.production_events import record_event as _pm_record
        _pm_record("package_output", "running")
    except Exception:
        _pm_record = None
    try:
        _pm_result = _photo_motion_original_main()
        if _pm_record:
            _pm_record("package_output", "completed" if _pm_result in (None, 0) else "failed", metadata={"return_code": _pm_result}, started_at=_pm_started)
        return _pm_result
    except BaseException as _pm_exc:
        if _pm_record:
            _pm_record("package_output", "failed", error=f"{type(_pm_exc).__name__}: {_pm_exc}", started_at=_pm_started)
        raise

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