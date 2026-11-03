#!/usr/bin/env python3
"""Read-only final validation for Photo Motion outputs and story packages."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import defaultdict
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

STORY_FILES = (
    OUT / "story" / "story.json",
    OUT / "story" / "ai_story.json",
)
STORY_VERIFICATION = OUT / "story" / "story_verification.json"
PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
CAPTIONS = OUT / "captions" / "captions.json"
SRT = OUT / "captions" / "final_captions.srt"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX = OUT / "sound_design" / "sfx_cues.json"
SFX_VALIDATION = OUT / "sound_design" / "sfx_timing_validation.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
METADATA_TAGS = OUT / "publish" / "youtube_tags.txt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v93-package-v82-compatible"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
FPS_TOLERANCE = 0.05
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120

ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SUCCESS = {"completed", "complete", "success", "done", "reused"}
SRT_TIMESTAMP = re.compile(
    r"^(?P<h>\d{2,}):(?P<m>[0-5]\d):"
    r"(?P<s>[0-5]\d),(?P<ms>\d{3})$"
)
REQUIRED_CHECKS = (
    "story", "photo_motion", "narration", "sound_design", "render",
    "captions", "thumbnail", "metadata", "unique_output_files",
    "mode_separation",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def clean(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        fail(f"Path escapes repository: {resolved}")
    return resolved.relative_to(ROOT.resolve()).as_posix()


def read_json(path: Path, optional: bool = False) -> Any:
    if not path.is_file():
        if optional:
            return None
        fail(f"Required JSON missing: {relative(path)}")
    if path.stat().st_size <= 0:
        fail(f"Required JSON is empty: {relative(path)}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Invalid JSON {relative(path)}: {exc}")
    if not isinstance(value, (dict, list)):
        fail(f"Invalid JSON root in {relative(path)}")
    return value


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        fail(f"{label}: expected a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(f"{label}: expected a positive integer")
    if result < 1:
        fail(f"{label}: value must be positive")
    return result


def parse_fps(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (TypeError, ValueError, ZeroDivisionError):
        fail(f"{label}: invalid FPS")
    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS")
    return result


def safe_file(
    value: Any,
    label: str,
    minimum: int = 1,
    output_only: bool = True,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path is missing")
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    allowed = OUT.resolve() if output_only else ROOT.resolve()
    if not resolved.is_relative_to(allowed):
        fail(f"{label}: path escapes allowed directory")
    if resolved.is_symlink() or not resolved.is_file():
        fail(f"{label}: file missing or unsafe: {resolved}")
    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small")
    return resolved


def media_info(path: Path) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        fail("ffprobe is required but unavailable")
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
    if result.returncode:
        fail(f"ffprobe failed for {path.name}: {(result.stderr or '')[-800:]}")
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams")
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media probe for {path.name}: {exc}")
    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {path.name}")
    if not isinstance(streams, list):
        fail(f"Invalid media stream list: {path.name}")

    video = next(
        (item for item in streams if item.get("codec_type") == "video"), None
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"), None
    )
    rate = None
    if video:
        rate = video.get("avg_frame_rate")
        if not rate or rate in {"0/0", "N/A"}:
            rate = video.get("r_frame_rate")

    return {
        "duration": duration,
        "video": video is not None,
        "audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "fps": parse_fps(rate, path.name) if video else None,
    }


def validate_video(
    path: Path,
    expected_size: tuple[int, int] | None,
    expected_fps: float | None,
    label: str,
    require_audio: bool = True,
) -> dict:
    info = media_info(path)
    if not info["video"]:
        fail(f"{label}: video stream is missing")
    if require_audio and not info["audio"]:
        fail(f"{label}: audio stream is missing")
    if expected_size and (info["width"], info["height"]) != expected_size:
        fail(
            f"{label}: expected {expected_size[0]}x{expected_size[1]}, "
            f"found {info['width']}x{info['height']}"
        )
    if expected_fps is not None and (
        info["fps"] is None
        or abs(info["fps"] - expected_fps) > FPS_TOLERANCE
    ):
        fail(f"{label}: FPS mismatch: {info['fps']}")
    return info


def get_rows(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def first_path(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def verify_hash(row: dict, actual: str, label: str) -> None:
    declared = (
        row.get("sha256")
        or row.get("video_sha256")
        or row.get("audio_sha256")
    )
    if declared and clean(declared).lower() != actual.lower():
        fail(f"{label}: SHA256 mismatch")


def normalize_role(row: dict) -> str:
    value = clean(
        row.get("role") or row.get("kind")
        or row.get("scene_type") or "main"
    ).lower()
    aliases = {
        "opening": "hook", "intro": "hook", "part_hook": "hook",
        "story": "main", "main_scene": "main",
        "cliffhanger": "suspense", "ending": "resolution",
        "final": "resolution",
    }
    value = aliases.get(value, value)
    if value not in ROLE_ORDER:
        fail(f"Unknown scene role: {value!r}")
    return value


def role_key(row: dict, label: str) -> tuple[int, str, int | None]:
    part = positive_int(row.get("part"), f"{label} part")
    role = normalize_role(row)
    if role == "main":
        scene = positive_int(
            row.get("local_scene") or row.get("scene"),
            f"{label} Main scene",
        )
        return part, role, scene
    return part, role, None


def expected_role_keys(parts: int, scenes: int) -> set:
    result = set()
    for part in range(1, parts + 1):
        result.add((part, "hook", None))
        for scene in range(1, scenes + 1):
            result.add((part, "main", scene))
        if part < parts:
            result.add((part, "suspense", None))
        else:
            result.add((part, "resolution", None))
    return result


def validate_role_rows(
    rows: Any,
    parts: int,
    scenes: int,
    label: str,
    main_only: bool = False,
) -> dict:
    if not isinstance(rows, list):
        fail(f"{label}: scene records are missing")
    expected = expected_role_keys(parts, scenes)
    if main_only:
        expected = {key for key in expected if key[1] == "main"}

    mapping = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid row {index}")
        key = role_key(row, label)
        if key not in expected:
            fail(f"{label}: unexpected scene role or number: {key}")
        if key in mapping:
            fail(f"{label}: duplicate scene record: {key}")

        expected_id = (
            f"part_{key[0]:02d}_scene_{key[2]:02d}"
            if key[1] == "main"
            else f"part_{key[0]:02d}_{key[1]}"
        )
        declared_id = clean(row.get("scene_id") or row.get("job_id"))
        if declared_id and declared_id != expected_id:
            fail(f"{label}: scene ID mismatch: {declared_id}")
        mapping[key] = row

    missing = expected - set(mapping)
    if missing:
        fail(f"{label}: missing records {sorted(missing, key=str)}")
    return mapping


def load_story_document() -> tuple[dict, Path]:
    for path in STORY_FILES:
        data = read_json(path, optional=True)
        if isinstance(data, dict):
            return data, path
    fail("Completed story JSON is missing")


def nested_objects(data: dict) -> list[dict]:
    result = [data]
    for key in ("story", "result", "data"):
        nested = data.get(key)
        if isinstance(nested, dict) and nested not in result:
            result.append(nested)
    return result


def extract_story_text(data: dict) -> str:
    for obj in nested_objects(data):
        for key in (
            "story_text", "full_story", "complete_story",
            "original_story", "content", "narration", "script",
        ):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    parts = data.get("parts")
    if not isinstance(parts, list):
        nested = data.get("story")
        parts = nested.get("parts") if isinstance(nested, dict) else None
    if isinstance(parts, list):
        values = []
        for part in parts:
            if isinstance(part, dict):
                value = part.get("narration") or part.get("text")
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
        return "\n".join(values)
    return ""


def validate_story(parts: int, scenes: int, mode: str) -> dict:
    data, story_path = load_story_document()
    nested = nested_objects(data)
    statuses = [
        clean(item.get("status")).lower()
        for item in nested if item.get("status") is not None
    ]
    if statuses and not any(status in SUCCESS for status in statuses):
        fail(f"Story status is incomplete: {statuses}")

    title = ""
    for obj in nested:
        for key in ("final_title", "generated_title", "story_title", "title"):
            if clean(obj.get(key)):
                title = clean(obj[key])
                break
        if title:
            break
    if not title:
        fail("Story-derived title is missing")

    story_parts = data.get("parts")
    if not isinstance(story_parts, list):
        nested_story = data.get("story")
        story_parts = (
            nested_story.get("parts")
            if isinstance(nested_story, dict) else None
        )
    if not isinstance(story_parts, list) or len(story_parts) != parts:
        fail("Story part count differs from actual selected PARTS")

    words_per_part = []
    flattened = []
    combined_parts = []

    for part_number, part in enumerate(story_parts, 1):
        if not isinstance(part, dict):
            fail(f"Invalid story part {part_number}")
        if positive_int(part.get("part", part_number), "Story part") != part_number:
            fail("Story part order mismatch")

        narration = clean(part.get("narration") or part.get("text"))
        if not narration:
            fail(f"Story narration is missing in Part {part_number}")

        count = len(re.findall(r"\S+", narration))
        declared = part.get("word_count")
        if declared is not None and positive_int(
            declared, "Part word count"
        ) != count:
            fail(f"Story word count mismatch in Part {part_number}")

        if mode == "short" and not 120 <= count <= 180:
            fail(
                f"SHORT Part {part_number} has {count} words; "
                "allowed range is 120–180"
            )
        if narration[-1] not in "।॥.!?…,:;—–":
            fail(
                f"Part {part_number} does not end at a sentence/clause boundary"
            )

        scene_rows = part.get("scenes")
        if not isinstance(scene_rows, list) or len(scene_rows) != scenes:
            fail(f"Part {part_number} Main scenes are incomplete")

        scene_text = []
        for scene_number, scene in enumerate(scene_rows, 1):
            if not isinstance(scene, dict):
                fail(f"Invalid scene in Part {part_number}")
            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Story scene ID mismatch: {scene_id}")
            if positive_int(scene.get("part"), "Scene part") != part_number:
                fail(f"Story Part mismatch: {scene_id}")
            if positive_int(scene.get("scene"), "Scene number") != scene_number:
                fail(f"Story scene number mismatch: {scene_id}")
            if clean(scene.get("role")).lower() != "main":
                fail(f"Story Main role mismatch: {scene_id}")
            scene_narration = clean(scene.get("narration"))
            if not scene_narration:
                fail(f"Scene narration is missing: {scene_id}")
            if not clean(scene.get("visual_prompt") or scene.get("visual")):
                fail(f"Scene visual prompt is missing: {scene_id}")
            scene_text.append(scene_narration)
            flattened.append(scene)

        if clean(" ".join(scene_text)) != narration:
            fail(f"Part {part_number} narration differs from its scene narration")

        words_per_part.append(count)
        combined_parts.append(narration)

    if isinstance(data.get("scenes"), list) and data["scenes"] != flattened:
        fail("Top-level story scenes differ from ordered Part scenes")

    combined = clean(" ".join(combined_parts))
    full_text = extract_story_text(data)
    if full_text and clean(full_text) != combined:
        fail("Full story text differs from ordered Part narration")

    total_words = len(re.findall(r"\S+", combined))
    for field, expected in (
        ("total_parts", parts),
        ("scenes_per_part", scenes),
        ("total_scenes", parts * scenes),
        ("total_story_words", total_words),
    ):
        if data.get(field) is not None and positive_int(
            data[field], f"Story {field}"
        ) != expected:
            fail(f"Story {field} mismatch")

    verification = read_json(STORY_VERIFICATION, optional=True)
    if verification is not None:
        if clean(verification.get("status")).lower() not in SUCCESS:
            fail("Story verification report is incomplete")
        verify_format = clean(verification.get("format")).lower()
        if verify_format and verify_format != mode:
            fail("Story verification FORMAT mismatch")
        summary = verification.get("summary")
        if isinstance(summary, dict) and summary.get("selected_parts") is not None:
            if positive_int(
                summary["selected_parts"], "Verified selected PARTS"
            ) != parts:
                fail("Story verification selected PARTS mismatch")

    return {
        "status": "valid",
        "title": title,
        "parts": parts,
        "scenes_per_part": scenes,
        "total_main_scenes": parts * scenes,
        "words_per_part": words_per_part,
        "total_words": total_words,
        "story_file": relative(story_path),
        "story_sha256": digest_file(story_path),
    }


def validate_photo(
    parts: int,
    scenes: int,
    mode: str,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    data = read_json(PHOTO)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Photo-motion manifest is incomplete")
    if clean(data.get("format")).lower() != mode:
        fail("Photo-motion FORMAT mismatch")

    width = positive_int(data.get("width"), "Photo-motion width")
    height = positive_int(data.get("height"), "Photo-motion height")
    fps = parse_fps(data.get("fps"), "Photo-motion FPS")
    if (width, height) != expected_size:
        fail("Photo-motion resolution differs from selected FORMAT")
    if abs(fps - expected_fps) > FPS_TOLERANCE:
        fail("Photo-motion FPS differs from Input/topic.txt")

    rows = data.get("scenes")
    mapping = validate_role_rows(rows, parts, scenes, "Photo Motion")
    expected_count = len(mapping)

    for field in (
        "expected_scene_count", "completed_count",
        "total_scenes", "all_completed_scene_count",
    ):
        if data.get(field) is not None and positive_int(
            data[field], f"Photo-motion {field}"
        ) != expected_count:
            fail(f"Photo-motion {field} mismatch")

    expected_counts = {
        "hook": parts,
        "main": parts * scenes,
        "suspense": max(0, parts - 1),
        "resolution": 1,
    }
    declared_counts = data.get("scene_role_counts")
    if isinstance(declared_counts, dict):
        for role, expected in expected_counts.items():
            if role in declared_counts and positive_int(
                declared_counts[role], f"Photo-motion {role} count"
            ) != expected:
                fail(f"Photo-motion {role} count mismatch")

    records = []
    for key in sorted(
        mapping,
        key=lambda item: (item[0], ROLE_ORDER[item[1]], item[2] or 0),
    ):
        row = mapping[key]
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Photo-motion scene is incomplete: {key}")
        path = safe_file(
            first_path(row, "video", "source_video", "output", "path"),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )
        info = validate_video(
            path, expected_size, expected_fps,
            f"Photo-motion scene {key}", False
        )
        checksum = digest_file(path)
        verify_hash(row, checksum, f"Photo-motion scene {key}")
        records.append({
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": checksum,
        })

    return {
        "status": "valid",
        "format": mode,
        "main_scene_count": parts * scenes,
        "role_scene_count": len(records),
        "role_counts": expected_counts,
        "scenes": records,
        "manifest_sha256": digest_file(PHOTO),
    }


def validate_audio_asset(value: Any, label: str) -> dict:
    path = safe_file(value, label, MIN_AUDIO_BYTES)
    info = media_info(path)
    if not info["audio"]:
        fail(f"{label}: audio stream is missing")
    if info["duration"] < 0.25:
        fail(f"{label}: audio duration is too short")
    return {
        "path": relative(path),
        "duration": round(info["duration"], 4),
        "sha256": digest_file(path),
        "bytes": path.stat().st_size,
    }

def validate_narration(parts: int) -> dict:
    data = read_json(AUDIO)
    rows = get_rows(
        data,
        ("jobs", "audio_jobs", "parts", "narration_jobs"),
    )
    if not rows:
        fail("Narration manifest has no audio records.")

    verified = []
    for part in range(1, parts + 1):
        candidates = [
            row for row in rows
            if isinstance(row, dict)
            and positive_int(
                row.get("part") or row.get("part_number")
            ) == part
        ]
        if not candidates:
            fail(f"Narration audio record missing for Part {part}.")

        row = next(
            (
                item for item in candidates
                if normalize_role(
                    item.get("role") or item.get("kind")
                ) in {"main", "narration", ""}
            ),
            candidates[0],
        )
        path = first_path(
            row,
            ("audio", "audio_path", "path", "file", "output"),
        )
        info = validate_audio_asset(
            path, f"Part {part} narration"
        )
        verify_hash(row, path, f"Part {part} narration")

        verified.append({
            "part": part,
            "path": relative(path),
            "duration": info["duration"],
            "sha256": digest_file(path),
        })

    return {
        "status": "passed",
        "parts": len(verified),
        "verified_audio": verified,
    }


def validate_sound(parts: int) -> dict:
    data = read_json(SOUND)
    if clean(data.get("status")).lower() not in {
        "completed", "success", "valid"
    }:
        fail("Sound-design manifest is not completed.")

    validation = read_json(SFX_VALIDATION)
    if clean(validation.get("status")).lower() not in {
        "completed", "success", "valid", "passed"
    }:
        fail("SFX timing validation is not completed.")

    errors = validation.get("errors")
    if isinstance(errors, list) and errors:
        fail(f"SFX timing validation contains errors: {errors[:5]}")

    for key in (
        "invalid_cues", "out_of_range_cues",
        "timing_errors", "failed_checks",
    ):
        value = validation.get(key)
        if isinstance(value, (int, float)) and value != 0:
            fail(f"SFX timing validation reports {key}={value}.")

    rows = get_rows(
        data,
        ("parts", "tracks", "audio", "assets", "jobs"),
    )
    if not rows:
        fail("Sound-design manifest contains no track records.")

    verified = []
    for part in range(1, parts + 1):
        part_rows = [
            row for row in rows
            if isinstance(row, dict)
            and (
                positive_int(
                    row.get("part") or row.get("part_number")
                ) in {None, part}
            )
        ]

        for row in part_rows:
            category = clean(
                row.get("category")
                or row.get("type")
                or row.get("kind")
                or row.get("role")
            ).lower()

            if category not in {
                "music", "bgm", "background_music",
                "ambient", "ambience", "sfx", "effect",
                "sound_effect",
            }:
                continue

            path_value = first_path(
                row,
                ("audio", "audio_path", "path", "file", "output"),
                required=False,
            )
            if not path_value:
                continue

            info = validate_audio_asset(
                path_value,
                f"Part {part} sound track {category}",
            )
            verify_hash(
                row, path_value,
                f"Part {part} sound track {category}",
            )
            verified.append({
                "part": part,
                "category": (
                    "ambience"
                    if category in {"ambient", "ambience"}
                    else category
                ),
                "path": relative(path_value),
                "duration": info["duration"],
            })

    return {
        "status": "passed",
        "tracks_verified": len(verified),
        "sfx_timing_validation": "passed",
        "tracks": verified,
    }


def parse_srt(path):
    content = path.read_text(
        encoding="utf-8-sig"
    ).replace("\r\n", "\n").replace("\r", "\n")

    blocks = re.split(r"\n\s*\n", content.strip())
    rows = []
    previous_end = -1.0

    def timestamp(value):
        match = re.fullmatch(
            r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})",
            value.strip(),
        )
        if not match:
            fail(f"Invalid SRT timestamp: {value}")
        hours, minutes, seconds, millis = map(
            int, match.groups()
        )
        return (
            hours * 3600
            + minutes * 60
            + seconds
            + millis / 1000
        )

    for expected_index, block in enumerate(blocks, 1):
        lines = [
            line.strip()
            for line in block.split("\n")
            if line.strip()
        ]
        if len(lines) < 3:
            fail(f"SRT block {expected_index} is incomplete.")

        if not lines[0].isdigit() or int(lines[0]) != expected_index:
            fail(f"SRT subtitle index {expected_index} is invalid.")

        match = re.fullmatch(
            r"(.+?)\s*-->\s*(.+?)",
            lines[1],
        )
        if not match:
            fail(f"SRT timing line {expected_index} is invalid.")

        start = timestamp(match.group(1))
        end = timestamp(match.group(2))

        if end <= start:
            fail(f"SRT subtitle {expected_index} has invalid duration.")
        if start < previous_end - 0.02:
            fail(f"SRT subtitle {expected_index} overlaps its predecessor.")

        caption = " ".join(lines[2:]).strip()
        if not caption:
            fail(f"SRT subtitle {expected_index} is empty.")

        rows.append({
            "index": expected_index,
            "start": start,
            "end": end,
            "text": caption,
        })
        previous_end = end

    if not rows:
        fail("Final SRT contains no subtitles.")

    return rows


def validate_captions(parts: int) -> dict:
    data = read_json(CAPTIONS)
    srt_rows = parse_srt(SRT)

    declared_parts = data.get("parts")
    if isinstance(declared_parts, list) and len(declared_parts) != parts:
        fail("Caption manifest Part count mismatch.")

    declared_count = (
        data.get("caption_count")
        or data.get("total_captions")
    )
    if (
        isinstance(declared_count, int)
        and declared_count != len(srt_rows)
    ):
        fail("Caption manifest and SRT subtitle counts differ.")

    return {
        "status": "passed",
        "parts": parts,
        "subtitle_count": len(srt_rows),
        "first_subtitle": srt_rows[0]["start"],
        "last_subtitle": srt_rows[-1]["end"],
    }


def validate_thumbnail(mode: str, dimensions: tuple) -> dict:
    path = safe_file(THUMBNAIL, "Story thumbnail")
    if path.stat().st_size < 1000:
        fail("Thumbnail is too small.")

    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
    except Exception as exc:
        fail(f"Thumbnail image is invalid: {exc}")

    if image_format not in {"JPEG", "JPG"}:
        fail("Thumbnail must be a JPEG image.")

    if (width, height) != dimensions:
        fail(
            f"Thumbnail dimensions mismatch: "
            f"{width}x{height}, expected {dimensions}."
        )

    manifest = read_json(THUMBNAIL_MANIFEST)
    if clean(manifest.get("status")).lower() not in {
        "completed", "success", "valid"
    }:
        fail("Thumbnail manifest is not completed.")

    if manifest.get("format") and clean(
        manifest["format"]
    ).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch.")

    return {
        "status": "passed",
        "path": relative(path),
        "dimensions": [width, height],
        "format": image_format,
    }


def validate_metadata(mode: str, story_title: str) -> dict:
    text_path = safe_file(
        METADATA_TEXT, "YouTube metadata text"
    )
    json_path = safe_file(
        METADATA_JSON, "YouTube metadata JSON"
    )
    tags_path = safe_file(TAGS, "YouTube tags")

    metadata = read_json(json_path)
    text_content = text_path.read_text(
        encoding="utf-8-sig"
    )
    tags_content = tags_path.read_text(
        encoding="utf-8-sig"
    )

    if len(text_content.strip()) < 10:
        fail("YouTube metadata text is empty.")
    if len(tags_content.strip()) < 2:
        fail("YouTube tags are empty.")

    metadata_title = clean(
        metadata.get("title")
        or metadata.get("video_title")
    )
    if metadata_title and metadata_title != story_title:
        fail("YouTube metadata title does not match the story title.")

    metadata_format = clean(
        metadata.get("format")
        or metadata.get("video_mode")
    ).lower()
    if metadata_format and metadata_format != mode:
        fail("YouTube metadata FORMAT mismatch.")

    if story_title.casefold() not in (
        text_content + "\n" + tags_content
    ).casefold():
        fail("YouTube metadata does not identify the story title.")

    return {
        "status": "passed",
        "title": story_title,
        "format": mode,
        "files": [
            relative(text_path),
            relative(json_path),
            relative(tags_path),
        ],
    }


def validate_render(
    parts: int,
    scenes: int,
    mode: str,
    dimensions: tuple,
    fps: float,
) -> dict:
    data = read_json(RENDER)

    if clean(data.get("status")).lower() != "completed":
        fail("Render manifest is not completed.")
    if clean(data.get("format")).lower() != mode:
        fail("Render manifest FORMAT mismatch.")
    if data.get("part_outputs_only") is not True:
        fail("Render manifest does not confirm Part-only output.")
    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions.")

    rows = data.get("parts")
    if not isinstance(rows, list) or len(rows) != parts:
        fail("Render manifest Part list does not match selected PARTS.")

    verified = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid render record for Part {index}.")

        if positive_int(row.get("part")) != index:
            fail(f"Render Part order mismatch at Part {index}.")

        path = first_path(
            row, ("path", "output", "video", "file")
        )
        info = validate_video(
            path, f"Rendered Part {index}",
            dimensions, fps, require_audio=True,
        )
        verify_hash(row, path, f"Rendered Part {index}")

        verified.append({
            "part": index,
            "path": relative(path),
            "duration": info["duration"],
            "sha256": digest_file(path),
        })

    scene_rows = (
        data.get("all_completed_scenes")
        or data.get("completed_scenes")
        or data.get("scenes")
        or []
    )
    if not isinstance(scene_rows, list) or not scene_rows:
        fail("Render manifest has no completed scene records.")

    role_counts = {}
    for row in scene_rows:
        if not isinstance(row, dict):
            continue
        role = normalize_role(row.get("role"))
        if role:
            role_counts[role] = role_counts.get(role, 0) + 1

    expected = expected_role_keys(parts, scenes)
    validate_role_rows(scene_rows, expected)

    return {
        "status": "passed",
        "parts": verified,
        "scene_count": len(scene_rows),
        "role_counts": role_counts,
        "part_outputs_only": True,
        "captions_burned": True,
    }


def validate_package(parts: int, mode: str, title: str) -> dict:
    import zipfile
    from pathlib import PurePosixPath

    archive_name_file = OUT / "package_archive_name.txt"
    report_file = OUT / "package_manifest.json"

    if not archive_name_file.is_file() or not report_file.is_file():
        return {
            "status": "pending",
            "reason": "Package is created by the next workflow step.",
        }

    archive_name = archive_name_file.read_text(
        encoding="utf-8-sig"
    ).strip()
    if not archive_name or Path(archive_name).name != archive_name:
        fail("Package archive filename is unsafe.")

    archive_path = ROOT / archive_name
    if not archive_path.is_file():
        fail("Package archive file is missing.")

    report = read_json(report_file)
    slug = clean(report.get("story_slug"))
    root_name = f"{slug}-{mode}"

    if not slug or report.get("package_root") != root_name:
        fail("Package story root is invalid.")
    if report.get("archive") != archive_name:
        fail("Package archive name mismatch.")
    if archive_name != f"katha-lok-{slug}-{mode}-output.zip":
        fail("Package archive name is not story-specific.")
    if report.get("status") != "completed":
        fail("Package report is incomplete.")
    if report.get("format") != mode or report.get("story_title") != title:
        fail("Package report story identity mismatch.")
    if report.get("parts") != parts:
        fail("Package report actual PARTS mismatch.")
    if report.get("configured_parts") != parts:
        fail("Package configured PARTS mismatch.")
    if report.get("part_videos_only") is not True:
        fail("Package is not Part-video-only.")
    if report.get("combined_video_included") is not False:
        fail("Combined video is included in package report.")
    if report.get("format_alias_included") is not False:
        fail("Opposite format alias is included in package report.")
    if report.get("zip_integrity") != "valid":
        fail("Package ZIP integrity is not valid.")
    if report.get("manifest_zip_consistency") != "valid":
        fail("Package manifest/ZIP consistency is not valid.")
    if report.get("zip_validation", {}).get("status") != "valid":
        fail("Package ZIP validation status is invalid.")

    digest = digest_file(archive_path)
    if digest != report.get("archive_sha256"):
        fail("Package archive SHA256 mismatch.")
    if archive_path.stat().st_size != report.get("archive_bytes"):
        fail("Package archive byte count mismatch.")

    expected_files = report.get("files")
    if not isinstance(expected_files, list):
        fail("Package file hash list is missing.")

    expected_paths = set()
    for item in expected_files:
        if not isinstance(item, dict):
            fail("Invalid package file record.")
        path = item.get("path")
        if not isinstance(path, str) or path in expected_paths:
            fail("Invalid or duplicate package member path.")
        expected_paths.add(path)

    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]

        if not names or len(names) != len(set(names)):
            fail("ZIP is empty or contains duplicate entries.")
        if archive.testzip() is not None:
            fail("ZIP CRC validation failed.")

        prefix = root_name + "/"
        for info in infos:
            name = info.filename
            path = PurePosixPath(name)

            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
                or "\\" in name
                or not name.startswith(prefix)
                or info.flag_bits & 0x1
            ):
                fail(f"Unsafe ZIP member: {name}")

        embedded_path = f"{root_name}/package_manifest.json"
        if embedded_path not in names:
            fail("Embedded package manifest is missing.")

        embedded = json.loads(
            archive.read(embedded_path).decode("utf-8")
        )
        if (
            embedded.get("status") != "completed"
            or embedded.get("format") != mode
            or embedded.get("parts") != parts
            or embedded.get("story_title") != title
            or embedded.get("story_slug") != slug
            or embedded.get("package_root") != root_name
            or embedded.get("manifest_zip_consistency") is not True
            or embedded.get("part_videos_only") is not True
            or embedded.get("combined_video_included") is not False
            or embedded.get("format_alias_included") is not False
        ):
            fail("Embedded package manifest is inconsistent.")

        actual_files = set(names)
        if actual_files != expected_paths | {embedded_path}:
            fail("ZIP member list differs from package hash report.")

        for item in expected_files:
            payload = archive.read(item["path"])
            if (
                len(payload) != item.get("bytes")
                or hashlib.sha256(payload).hexdigest()
                != item.get("sha256")
            ):
                fail(f"ZIP member hash/size mismatch: {item['path']}")

        videos = {
            name for name in names
            if PurePosixPath(name).suffix.lower()
            in {".mp4", ".mov", ".mkv", ".webm"}
        }
        expected_videos = {
            f"{root_name}/parts/{slug}-part-{number:02d}.mp4"
            for number in range(1, parts + 1)
        }
        if videos != expected_videos:
            fail("ZIP has missing, extra or wrongly named videos.")

        thumbnail = f"{root_name}/{slug}-thumbnail.jpg"
        if thumbnail not in names:
            fail("Story-named thumbnail is missing.")

        render_path = f"{root_name}/scene_renders/render_manifest.json"
        if render_path not in names:
            fail("Packaged render manifest is missing.")

        packaged_render = json.loads(
            archive.read(render_path).decode("utf-8")
        )
        if (
            packaged_render.get("status") != "completed"
            or packaged_render.get("format") != mode
            or packaged_render.get("part_outputs_only") is not True
            or packaged_render.get("captions_burned") is not True
            or not isinstance(packaged_render.get("parts"), list)
            or len(packaged_render["parts"]) != parts
        ):
            fail("Packaged render manifest is invalid.")

        for index, row in enumerate(
            packaged_render["parts"], 1
        ):
            expected = (
                f"{root_name}/parts/{slug}-part-{index:02d}.mp4"
            )
            if (
                row.get("part") != index
                or row.get("path") != expected
            ):
                fail(f"Packaged render Part {index} path mismatch.")

    return {
        "status": "passed",
        "archive": archive_name,
        "archive_sha256": digest,
        "archive_bytes": archive_path.stat().st_size,
        "member_count": len(expected_files) + 1,
        "part_videos_only": True,
    }


def main() -> int:
    if not shutil.which("ffprobe"):
        fail("ffprobe is required.")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    parts = get_selected_parts(config, required=True)

    if parts is None:
        fail("Actual selected PARTS are not persisted.")

    parts = int(parts)
    scenes = int(get_scenes(config))
    fps = parse_fps(get_fps(config))

    if mode not in {"short", "full"}:
        fail(f"Invalid FORMAT: {mode}.")
    if parts < 1 or scenes < 1:
        fail("PARTS and SCENES must be positive.")

    dimensions = (
        (720, 1280)
        if mode == "short"
        else (1280, 720)
    )

    story = validate_story(mode, parts, scenes)
    title = clean(
        story.get("title")
        or story.get("story_title")
        or story.get("topic")
    )
    if not title:
        fail("Final story title is missing.")

    checks = {}
    checks["story"] = {
        "status": "passed",
        "title": title,
        "parts": parts,
    }

    checks["photo_motion"] = validate_photo(
        mode, parts, scenes, dimensions, fps
    )
    checks["narration"] = validate_narration(parts)
    checks["sound_design"] = validate_sound(parts)
    checks["captions"] = validate_captions(parts)
    checks["render"] = validate_render(
        parts, scenes, mode, dimensions, fps
    )
    checks["thumbnail"] = validate_thumbnail(
        mode, dimensions
    )
    checks["metadata"] = validate_metadata(mode, title)
    checks["package"] = validate_package(
        parts, mode, title
    )

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "story_title": title,
        "parts": parts,
        "scenes_per_part": scenes,
        "resolution": list(dimensions),
        "fps": fps,
        "checks": checks,
        "failed_checks": [],
        "package_status": checks["package"]["status"],
    }

    write_json(OUT / "validation_report.json", report)

    log("=" * 60)
    log("FINAL OUTPUT VALIDATION PASSED")
    log("FORMAT:", mode)
    log("STORY:", title)
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("RESOLUTION:", dimensions)
    log("FPS:", fps)
    log("PACKAGE:", checks["package"]["status"])
    log("REPORT:", relative(OUT / "validation_report.json"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("FINAL OUTPUT VALIDATION FAILED:", exc)
        raise SystemExit(1)