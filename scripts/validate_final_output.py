#!/usr/bin/env python3
"""Validate all selected-format photo-motion outputs before packaging."""

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
from collections import Counter
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
    get_parts,
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

VERSION = "final-validator-v80-dynamic-format-part-only-integrity"
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
SRT_TIMESTAMP = re.compile(
    r"^(?P<h>\d{2,}):(?P<m>[0-5]\d):"
    r"(?P<s>[0-5]\d),(?P<ms>\d{3})$"
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def clean(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def relative(path: Path) -> str:
    resolved = Path(path).resolve()
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
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Invalid JSON {relative(path)}: {exc}")


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


def safe_file(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path missing")
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(OUT.resolve()):
        fail(f"{label}: path must remain inside output/")
    if not resolved.is_file():
        fail(f"{label}: file not found: {relative(resolved)}")
    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small")
    return resolved


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
    except (ValueError, TypeError, ZeroDivisionError):
        fail(f"{label}: invalid FPS")
    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS")
    return result


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
            capture_output=True, text=True,
            timeout=PROBE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        fail(f"ffprobe timed out: {path.name}")
    if result.returncode:
        fail(f"ffprobe failed for {path.name}: {(result.stderr or '')[-600:]}")
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media probe for {path.name}: {exc}")
    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {path.name}")
    streams = data.get("streams")
    if not isinstance(streams, list):
        fail(f"Invalid stream list: {path.name}")
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
    expected_size: tuple[int, int],
    expected_fps: float,
    label: str,
    require_audio: bool = True,
) -> dict:
    info = media_info(path)
    if not info["video"]:
        fail(f"{label}: video stream missing")
    if require_audio and not info["audio"]:
        fail(f"{label}: audio stream missing")
    if (info["width"], info["height"]) != expected_size:
        fail(
            f"{label}: expected {expected_size[0]}x{expected_size[1]}, "
            f"found {info['width']}x{info['height']}"
        )
    if abs(info["fps"] - expected_fps) > FPS_TOLERANCE:
        fail(f"{label}: FPS mismatch: {info['fps']:.4f}")
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
    declared = row.get("sha256") or row.get("video_sha256")
    if declared and clean(declared).lower() != actual.lower():
        fail(f"{label}: SHA256 mismatch")


def role_name(row: dict) -> str:
    value = clean(
        row.get("role") or row.get("kind") or row.get("scene_type") or "main"
    ).lower()
    aliases = {
        "extra": clean(row.get("kind")).lower(),
        "extra_scene": clean(row.get("kind")).lower(),
        "opening": "hook",
        "intro": "hook",
        "cliffhanger": "suspense",
        "ending": "resolution",
        "final": "resolution",
        "story": "main",
        "main_scene": "main",
    }
    value = aliases.get(value, value)
    if value not in ROLE_ORDER:
        fail(f"Unknown scene role: {value!r}")
    return value


def role_key(row: dict, label: str) -> tuple[int, str, int | None]:
    try:
        part = positive_int(row.get("part"), f"{label} part")
    except RuntimeError:
        fail(f"{label}: invalid part number")
    role = role_name(row)
    if role == "main":
        try:
            scene = positive_int(
                row.get("local_scene", row.get("scene")),
                f"{label} scene",
            )
        except RuntimeError:
            fail(f"{label}: invalid Main scene number")
        return part, role, scene
    return part, role, None


def expected_main_keys(parts: int, scenes: int) -> list[tuple[int, int]]:
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def expected_roles(parts: int, scenes: int) -> set:
    result = set()
    for part in range(1, parts + 1):
        result.add((part, "hook", None))
        for scene in range(1, scenes + 1):
            result.add((part, "main", scene))
        if part < parts:
            result.add((part, "suspense", None))
        if part == parts:
            result.add((part, "resolution", None))
    return result


def validate_role_rows(
    items: Any, parts: int, scenes: int, label: str,
    main_only: bool = False,
) -> dict:
    if not isinstance(items, list):
        fail(f"{label}: scene records missing")
    expected = expected_roles(parts, scenes)
    if main_only:
        expected = {item for item in expected if item[1] == "main"}
    mapping = {}
    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid row {index}")
        key = role_key(row, label)
        if key not in expected:
            fail(f"{label}: unexpected role or scene: {key}")
        if key in mapping:
            fail(f"{label}: duplicate role or scene: {key}")
        expected_id = (
            f"part_{key[0]:02d}_scene_{key[2]:02d}"
            if key[1] == "main"
            else f"part_{key[0]:02d}_{key[1]}"
        )
        declared_id = clean(row.get("scene_id") or row.get("job_id"))
        if declared_id and declared_id != expected_id:
            fail(f"{label}: scene ID mismatch: {declared_id}")
        mapping[key] = row
    if set(mapping) != expected:
        fail(
            f"{label}: missing records "
            f"{sorted(expected - set(mapping), key=str)}"
        )
    return mapping


def validate_main_rows(items: Any, parts: int, scenes: int, label: str) -> dict:
    if not isinstance(items, list):
        fail(f"{label}: Main scene list missing")
    expected = expected_main_keys(parts, scenes)
    if len(items) != len(expected):
        fail(f"{label}: expected {len(expected)} scenes, found {len(items)}")
    mapping = {}
    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid scene row {index}")
        key = role_key({**row, "role": "main"}, label)
        expected_key = expected[index - 1]
        if key[0] != expected_key[0] or key[2] != expected_key[1]:
            fail(f"{label}: scene order mismatch at row {index}")
        global_number = row.get(
            "global_scene", row.get("global_scene_number", index)
        )
        if positive_int(global_number, f"{label} global scene") != index:
            fail(f"{label}: global scene numbering mismatch")
        mapping[(key[0], key[2])] = row
    return mapping


def config_enabled(config: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1", "on", "enabled"}:
                return True
            if normalized in {"false", "no", "0", "off", "disabled"}:
                return False
        fail(f"Invalid boolean setting: {key}")
    return True


def load_story_document() -> tuple[dict, Path]:
    for path in STORY_FILES:
        data = read_json(path, optional=True)
        if isinstance(data, dict):
            return data, path
    fail("Completed story JSON is missing")


def nested_objects(data: dict) -> list[dict]:
    result = [data]
    for key in ("story", "result", "data"):
        value = data.get(key)
        if isinstance(value, dict) and value not in result:
            result.append(value)
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
        sections = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            value = part.get("narration") or part.get("text")
            if isinstance(value, str) and value.strip():
                sections.append(value.strip())
        return "\n".join(sections)
    return ""


def validate_story(parts: int, scenes: int, mode: str) -> dict:
    data, story_path = load_story_document()
    nested = nested_objects(data)
    statuses = [
        clean(obj.get("status")).lower()
        for obj in nested if obj.get("status") is not None
    ]
    if statuses and not any(
        item in {"completed", "complete", "success", "done"} for item in statuses
    ):
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
        fail("Story title is missing")

    story_parts = data.get("parts")
    if not isinstance(story_parts, list):
        nested_story = data.get("story")
        story_parts = nested_story.get("parts") if isinstance(nested_story, dict) else None
    if not isinstance(story_parts, list) or len(story_parts) != parts:
        fail("Story part count differs from selected PARTS")

    words_per_part = []
    flattened = []
    combined_parts = []
    for part_number, part in enumerate(story_parts, 1):
        if not isinstance(part, dict):
            fail(f"Invalid story part {part_number}")
        actual_part = positive_int(part.get("part", part_number), "Story part")
        if actual_part != part_number:
            fail("Story part order mismatch")
        narration = clean(part.get("narration") or part.get("text"))
        if not narration:
            fail(f"Story narration missing in part {part_number}")
        count = len(re.findall(r"\S+", narration))
        declared = part.get("word_count")
        if declared is not None and positive_int(declared, "Part word count") != count:
            fail(f"Story part word count mismatch: {part_number}")
        if mode == "short" and not 120 <= count <= 180:
            fail(
                f"SHORT part {part_number} has {count} words; "
                "allowed range is 120–180"
            )
        if narration[-1] not in "।॥.!?…,:;—–":
            fail(f"Part {part_number} does not end at a sentence/clause boundary")
        scene_rows = part.get("scenes")
        if not isinstance(scene_rows, list) or len(scene_rows) != scenes:
            fail(f"Part {part_number} Main scenes are incomplete")
        scene_text = []
        for scene_number, scene in enumerate(scene_rows, 1):
            if not isinstance(scene, dict):
                fail(f"Invalid scene in part {part_number}")
            expected_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            if clean(scene.get("scene_id")) != expected_id:
                fail(f"Story scene ID mismatch: {expected_id}")
            if positive_int(scene.get("part"), "Scene part") != part_number:
                fail(f"Story part mismatch: {expected_id}")
            if positive_int(scene.get("scene"), "Scene number") != scene_number:
                fail(f"Story scene number mismatch: {expected_id}")
            if clean(scene.get("role")).lower() != "main":
                fail(f"Story Main role mismatch: {expected_id}")
            narration_text = clean(scene.get("narration"))
            if not narration_text:
                fail(f"Scene narration missing: {expected_id}")
            if not clean(scene.get("visual_prompt") or scene.get("visual")):
                fail(f"Scene visual prompt missing: {expected_id}")
            scene_text.append(narration_text)
            flattened.append(scene)
        if clean(" ".join(scene_text)) != narration:
            fail(f"Part {part_number} narration differs from scene narration")
        words_per_part.append(count)
        combined_parts.append(narration)

    all_scenes = data.get("scenes")
    if isinstance(all_scenes, list) and all_scenes != flattened:
        fail("Top-level story scenes differ from ordered part scenes")
    combined = clean(" ".join(combined_parts))
    full_text = extract_story_text(data)
    if full_text and clean(full_text) != combined:
        fail("Full story text differs from ordered part narration")

    total_words = len(re.findall(r"\S+", combined))
    declared_parts = data.get("total_parts")
    if declared_parts is not None and positive_int(declared_parts, "Story total_parts") != parts:
        fail("Story total_parts mismatch")
    declared_scenes = data.get("scenes_per_part")
    if declared_scenes is not None and positive_int(declared_scenes, "Story scenes_per_part") != scenes:
        fail("Story scenes_per_part mismatch")
    declared_total = data.get("total_scenes")
    if declared_total is not None and positive_int(declared_total, "Story total_scenes") != parts * scenes:
        fail("Story total_scenes mismatch")
    declared_words = data.get("total_story_words")
    if declared_words is not None and positive_int(declared_words, "Story total_story_words") != total_words:
        fail("Story total word count mismatch")

    verification = read_json(STORY_VERIFICATION, optional=True)
    if verification is not None:
        if clean(verification.get("status")).lower() not in {
            "completed", "complete", "success", "done"
        }:
            fail("Story verification report is incomplete")
        verify_format = clean(verification.get("format")).lower()
        if verify_format and verify_format != mode:
            fail("Story verification FORMAT mismatch")
        summary = verification.get("summary")
        if isinstance(summary, dict) and summary.get("selected_parts") is not None:
            if positive_int(summary["selected_parts"], "Verified selected parts") != parts:
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


def validate_photo(parts: int, scenes: int, mode: str) -> dict:
    data = read_json(PHOTO)
    if clean(data.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("Photo-motion manifest is incomplete")
    if clean(data.get("format")).lower() != mode:
        fail("Photo-motion FORMAT mismatch")
    scene_rows = get_rows(data, "scenes", "completed_scenes")
    mapping = validate_main_rows(scene_rows, parts, scenes, "Photo-motion")
    records = []
    for key, row in mapping.items():
        if clean(row.get("status")).lower() not in {
            "completed", "complete", "success", "done"
        }:
            fail(f"Photo-motion scene incomplete: {key}")
        path = safe_file(
            first_path(row, "video", "source_video", "output", "path"),
            f"Photo-motion scene {key}", MIN_VIDEO_BYTES,
        )
        info = media_info(path)
        if not info["video"]:
            fail(f"Photo-motion scene has no video stream: {key}")
        actual_hash = digest_file(path)
        verify_hash(row, actual_hash, f"Photo-motion scene {key}")
        records.append({
            "part": key[0], "scene": key[1],
            "path": relative(path), "duration": round(info["duration"], 3),
            "sha256": actual_hash,
        })
    for field in ("expected_scene_count", "completed_count"):
        if data.get(field) is not None:
            if positive_int(data[field], f"Photo-motion {field}") != parts * scenes:
                fail(f"Photo-motion {field} mismatch")
    return {"status": "valid", "scene_count": len(records), "scenes": records}


def validate_audio_asset(value: Any, label: str) -> dict:
    path = safe_file(value, label, MIN_AUDIO_BYTES)
    info = media_info(path)
    if not info["audio"]:
        fail(f"{label}: audio stream missing")
    if info["duration"] < 0.25:
        fail(f"{label}: audio duration too short")
    return {
        "path": relative(path),
        "duration": round(info["duration"], 3),
        "sha256": digest_file(path),
    }


def validate_narration(parts: int, scenes: int) -> dict:
    data = read_json(AUDIO)
    if clean(data.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("Narration manifest is incomplete")
    jobs = get_rows(data, "jobs", "audio_jobs", "scenes")
    mapping = validate_role_rows(jobs, parts, scenes, "Narration")
    records = []
    total_duration = 0.0
    for key in sorted(mapping, key=lambda item: (
        item[0], ROLE_ORDER[item[1]], item[2] or 0
    )):
        row = mapping[key]
        if clean(row.get("status")).lower() not in {"completed", "reused", "success"}:
            fail(f"Narration record incomplete: {key}")
        asset = validate_audio_asset(
            first_path(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}",
        )
        verify_hash(row, asset["sha256"], f"Narration {key}")
        total_duration += asset["duration"]
        records.append({
            "part": key[0], "role": key[1], "scene": key[2], **asset
        })
    return {
        "status": "valid",
        "record_count": len(records),
        "main_scene_count": parts * scenes,
        "total_duration": round(total_duration, 3),
        "audio_files": records,
    }


def srt_seconds(value: str) -> float:
    match = SRT_TIMESTAMP.fullmatch(value.strip())
    if not match:
        fail(f"Invalid SRT timestamp: {value!r}")
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(match.group("ms")) / 1000
    )


def validate_srt(path: Path, expected_duration: float | None = None) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read final SRT: {exc}")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [
        block.strip() for block in re.split(r"\n[ \t]*\n", content)
        if block.strip()
    ]
    if not blocks:
        fail("Final SRT contains no caption cues")
    previous_end = -1.0
    maximum_end = 0.0
    characters = 0
    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index} is incomplete")
        if int(lines[0].strip()) != index or lines[1].count("-->") != 1:
            fail(f"SRT cue {index} numbering/timing is invalid")
        left, right = [value.strip() for value in lines[1].split("-->")]
        start, end = srt_seconds(left), srt_seconds(right)
        if end <= start or start < previous_end - 0.001:
            fail(f"SRT cue {index} overlaps or has invalid timing")
        caption = "\n".join(lines[2:]).strip()
        if not caption:
            fail(f"SRT cue {index} has no caption text")
        tags = re.findall(r"</?([A-Za-z]+)(?:\s+[^>]*)?>", caption)
        if any(tag.lower() not in {"b", "i", "u", "font"} for tag in tags):
            fail(f"SRT cue {index} contains unsupported markup")
        previous_end = end
        maximum_end = max(maximum_end, end)
        characters += len(caption)
    if expected_duration is not None:
        if maximum_end > expected_duration + 0.25:
            fail("SRT extends beyond video duration")
        if maximum_end < expected_duration * 0.50:
            fail("SRT timing coverage is unexpectedly short")
    return {
        "cue_count": len(blocks),
        "text_characters": characters,
        "last_cue_end": round(maximum_end, 3),
        "overlap_check": "passed",
        "sequence_check": "passed",
        "timestamp_check": "passed",
    }


def validate_captions(parts: int, scenes: int, mode: str) -> dict:
    data = read_json(CAPTIONS)
    if clean(data.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("Caption manifest is incomplete")
    if clean(data.get("format")).lower() != mode:
        fail("Caption FORMAT mismatch")
    main_rows = get_rows(data, "captions", "scenes")
    main_mapping = validate_main_rows(main_rows, parts, scenes, "Captions")
    for key, row in main_mapping.items():
        text = row.get("text", row.get("caption", row.get("caption_text")))
        if not isinstance(text, str) or not text.strip():
            fail(f"Caption text missing for Main scene {key}")

    extra_rows = data.get("extra_captions")
    if extra_rows is not None:
        expected = {
            (part, "hook", None) for part in range(1, parts + 1)
        }
        expected.update(
            (part, "suspense", None) for part in range(1, parts)
        )
        expected.add((parts, "resolution", None))
        found = set()
        if not isinstance(extra_rows, list):
            fail("Extra caption records are invalid")
        for row in extra_rows:
            if not isinstance(row, dict):
                fail("Invalid extra caption record")
            key = role_key(row, "Extra captions")
            if key not in expected or key in found:
                fail(f"Unexpected or duplicate extra caption: {key}")
            text = row.get("text", row.get("caption", row.get("caption_text")))
            if not isinstance(text, str) or not text.strip():
                fail(f"Extra caption text missing: {key}")
            found.add(key)
        if found != expected:
            fail(f"Extra captions incomplete: {sorted(expected - found, key=str)}")

    srt_path = safe_file(relative(SRT), "Final SRT", 10)
    srt_report = validate_srt(srt_path)
    return {
        "status": "valid",
        "main_scene_count": len(main_mapping),
        "extra_caption_count": len(extra_rows or []),
        "srt": relative(srt_path),
        "srt_sha256": digest_file(srt_path),
        **srt_report,
    }

def canonical_track(name: str) -> str:
    aliases = {
        "ambience": "ambient",
        "environment": "ambient",
        "environmental": "ambient",
        "background_music": "music",
        "soundtrack": "music",
        "sound_effect": "sfx",
        "sound_effects": "sfx",
    }
    value = str(name).strip().lower()
    return aliases.get(value, value)


def validate_sound(config: dict, parts: int, scenes: int) -> dict:
    data = read_json(SOUND)
    if clean(data.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("Sound-design manifest is incomplete")
    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        fail("Sound-design tracks object missing")

    enabled = {
        "music": config_enabled(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC", "SOUNDTRACK_ENABLED"),
        ),
        "ambient": config_enabled(
            config,
            ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND", "ENVIRONMENT_SOUND"),
        ),
        "sfx": config_enabled(
            config,
            ("SFX_ENABLED", "SFX", "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED"),
        ),
    }

    normalized = {}
    for name, record in tracks.items():
        logical = canonical_track(name)
        if logical in {"music", "ambient", "sfx"}:
            if logical in normalized and normalized[logical] != record:
                fail(f"Conflicting sound track aliases: {logical}")
            normalized[logical] = record

    checked_tracks = {}
    for name in ("music", "ambient"):
        if not enabled[name]:
            checked_tracks[name] = {"status": "disabled"}
            continue
        record = normalized.get(name)
        if not isinstance(record, dict) or clean(record.get("status")).lower() not in {
            "completed", "complete", "success", "done"
        }:
            fail(f"Enabled sound track missing or incomplete: {name}")
        asset = validate_audio_asset(
            first_path(record, "path", "file", "output", "audio_path"),
            f"Sound track {name}",
        )
        verify_hash(record, asset["sha256"], f"Sound track {name}")
        checked_tracks[name] = {"status": "valid", **asset}

    sfx_records = []
    if enabled["sfx"]:
        cue_data = read_json(SFX)
        if clean(cue_data.get("status")).lower() not in {
            "completed", "complete", "success", "done"
        }:
            fail("SFX cue manifest is incomplete")
        cues = get_rows(cue_data, "cues", "items")
        if not cues:
            fail("SFX is enabled but no cues exist")
        if cue_data.get("cue_count") is not None:
            if positive_int(cue_data["cue_count"], "SFX cue_count") != len(cues):
                fail("SFX cue_count mismatch")
        for index, cue in enumerate(cues, 1):
            if not isinstance(cue, dict):
                fail(f"Invalid SFX cue {index}")
            part = positive_int(cue.get("part"), "SFX part")
            scene = positive_int(cue.get("scene"), "SFX scene")
            if part > parts or scene > scenes:
                fail(f"SFX cue outside selected story range: {index}")
            try:
                timestamp = float(cue.get("start_seconds", cue.get("time", 0)))
                gain = float(cue.get("gain", 0))
            except (TypeError, ValueError):
                fail(f"Invalid SFX timing/gain: {index}")
            if not math.isfinite(timestamp) or timestamp < 0:
                fail(f"Invalid SFX timestamp: {index}")
            if not math.isfinite(gain) or gain < 0:
                fail(f"Invalid SFX gain: {index}")
            asset = validate_audio_asset(
                first_path(cue, "file", "path", "audio", "output"),
                f"SFX cue {index}",
            )
            narration_duration = cue.get("narration_duration_seconds")
            if narration_duration is not None:
                try:
                    duration = float(narration_duration)
                except (TypeError, ValueError):
                    fail(f"Invalid measured narration duration: SFX cue {index}")
                if (
                    not math.isfinite(duration)
                    or duration <= 0
                    or timestamp >= duration
                ):
                    fail(f"SFX cue is outside measured narration: {index}")
            sfx_records.append({
                "part": part, "scene": scene,
                "time": round(timestamp, 4),
                "gain": round(gain, 4), **asset,
            })

        timing = read_json(SFX_VALIDATION)
        if clean(timing.get("status")).lower() not in {
            "completed", "complete", "success", "done"
        }:
            fail("SFX timing validation is incomplete")
        nested = timing.get("sfx")
        if isinstance(nested, dict):
            if clean(nested.get("status")).lower() not in {"valid", "completed"}:
                fail("SFX timing validation did not confirm valid cues")
        elif clean(timing.get("validation_status")).lower() != "valid":
            fail("SFX timing validation did not confirm valid cues")
        if timing.get("cue_count") is not None:
            if positive_int(timing["cue_count"], "Validated SFX cue count") != len(cues):
                fail("SFX validation cue count mismatch")

    return {
        "status": "valid",
        "enabled": enabled,
        "tracks": checked_tracks,
        "sfx_cue_count": len(sfx_records),
        "sfx_cues": sfx_records,
    }


def validate_render(
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    data = read_json(RENDER)
    if clean(data.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("Render manifest is incomplete")
    if clean(data.get("format")).lower() != mode:
        fail("Render FORMAT differs from Input/topic.txt")
    if data.get("part_outputs_only") is not True:
        fail("Render manifest must declare Part-only output")

    width = positive_int(data.get("width"), "Render width")
    height = positive_int(data.get("height"), "Render height")
    fps = parse_fps(data.get("fps"), "Render FPS")
    if (width, height) != expected_size:
        fail("Render resolution differs from selected FORMAT")
    if abs(fps - expected_fps) > FPS_TOLERANCE:
        fail("Render FPS differs from Input/topic.txt")

    all_rows = data.get("all_completed_scenes")
    if not isinstance(all_rows, list):
        fail("Render manifest all_completed_scenes is missing")
    role_mapping = validate_role_rows(all_rows, parts, scenes, "Final render")

    checked_roles = []
    role_duration = 0.0
    for key in sorted(role_mapping, key=lambda item: (
        item[0], ROLE_ORDER[item[1]], item[2] or 0
    )):
        row = role_mapping[key]
        if clean(row.get("status")).lower() not in {
            "completed", "complete", "success", "done"
        }:
            fail(f"Rendered role incomplete: {key}")
        value = first_path(row, "video", "output", "rendered_video", "path")
        path = safe_file(value, f"Rendered role {key}", MIN_VIDEO_BYTES)
        info = validate_video(path, expected_size, expected_fps, f"Rendered role {key}")
        actual_hash = digest_file(path)
        verify_hash(row, actual_hash, f"Rendered role {key}")
        checked_roles.append({
            "part": key[0], "role": key[1], "scene": key[2],
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": actual_hash,
        })
        role_duration += info["duration"]

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Render manifest must contain exactly {parts} Part videos")
    checked_parts = []
    part_paths = set()
    part_duration = 0.0
    for index, row in enumerate(part_rows, 1):
        if not isinstance(row, dict):
            fail(f"Rendered Part {index} record is invalid")
        if positive_int(row.get("part"), "Rendered Part number") != index:
            fail(f"Rendered Part order mismatch at {index}")
        if clean(row.get("status")).lower() not in {
            "completed", "complete", "success", "done"
        }:
            fail(f"Rendered Part {index} is incomplete")
        path = safe_file(
            first_path(row, "path", "video", "output"),
            f"Rendered Part {index}", MIN_VIDEO_BYTES,
        )
        if path.resolve() in part_paths:
            fail(f"Duplicate Part video path: {relative(path)}")
        part_paths.add(path.resolve())
        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(f"Part video must be stored in output/parts/: {relative(path)}")
        expected_name = f"part_{index:02d}.mp4"
        if path.name != expected_name:
            fail(f"Part {index} filename must be {expected_name}")
        info = validate_video(path, expected_size, expected_fps, f"Part {index}")
        actual_hash = digest_file(path)
        verify_hash(row, actual_hash, f"Part {index}")
        if row.get("duration") is not None:
            try:
                declared_duration = float(row["duration"])
            except (TypeError, ValueError):
                fail(f"Part {index} declared duration is invalid")
            if (
                not math.isfinite(declared_duration)
                or abs(declared_duration - info["duration"]) > DURATION_TOLERANCE
            ):
                fail(f"Part {index} measured duration mismatch")
        part_duration += info["duration"]
        checked_parts.append({
            "part": index,
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": actual_hash,
            "resolution": [info["width"], info["height"]],
            "fps": round(info["fps"], 4),
        })

    if abs(part_duration - role_duration) > max(
        DURATION_TOLERANCE, len(all_rows) * 0.05
    ):
        fail("Part duration differs significantly from rendered role durations")

    if data.get("final_video") or data.get("alias") or data.get("combined_video"):
        fail("Combined or alias video fields are not allowed in Part-only output")

    opposite = "full" if mode == "short" else "short"
    forbidden = [
        OUT / "parts" / f"katha_lok_ai_{opposite}.mp4",
        OUT / f"katha_lok_ai_{opposite}.mp4",
        OUT / "videos" / f"katha_lok_ai_{opposite}.mp4",
    ]
    for path in forbidden:
        if path.exists():
            fail(f"Opposite-format video exists: {relative(path)}")

    return {
        "status": "valid",
        "format": mode,
        "part_count": parts,
        "role_record_count": len(checked_roles),
        "main_scene_count": parts * scenes,
        "part_duration_total": round(part_duration, 3),
        "role_duration_total": round(role_duration, 3),
        "part_records": checked_parts,
        "role_records": checked_roles,
        "captions_burned": bool(data.get("captions_burned", False)),
    }


def validate_thumbnail(mode: str) -> dict:
    path = safe_file(relative(THUMBNAIL), "Thumbnail", MIN_IMAGE_BYTES)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions, image_format = image.size, image.format
    except Exception as exc:
        fail(f"Invalid thumbnail image: {exc}")
    expected_size = (720, 1280) if mode == "short" else (1280, 720)
    if image_format != "JPEG" or dimensions != expected_size:
        fail(
            f"{mode.upper()} thumbnail must be JPEG "
            f"{expected_size[0]}x{expected_size[1]}; found {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST)
    if clean(manifest.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("Thumbnail manifest is incomplete")
    if clean(manifest.get("format")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")
    declared = first_path(manifest, "thumbnail", "path", "output")
    if declared:
        declared_path = safe_file(declared, "Thumbnail manifest main image", MIN_IMAGE_BYTES)
        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to a different main image")
    rows = manifest.get("thumbnails")
    if not isinstance(rows, list) or not rows:
        fail("Thumbnail manifest output list missing")

    verified = []
    seen = set()
    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            fail(f"Invalid thumbnail record {index}")
        output = safe_file(item.get("file"), f"Thumbnail output {index}", MIN_IMAGE_BYTES)
        if output.resolve() in seen:
            fail(f"Duplicate thumbnail output path: {relative(output)}")
        seen.add(output.resolve())
        try:
            with Image.open(output) as image:
                image.verify()
            with Image.open(output) as image:
                size, fmt = image.size, image.format
        except Exception as exc:
            fail(f"Thumbnail output invalid: {output.name}: {exc}")
        expected = (
            positive_int(item.get("width"), "Thumbnail width"),
            positive_int(item.get("height"), "Thumbnail height"),
        )
        if fmt != "JPEG" or size != expected:
            fail(f"Thumbnail dimensions/format mismatch: {output.name}")
        if item.get("success") is not True:
            fail(f"Thumbnail output is not marked successful: {output.name}")
        actual_hash = digest_file(output)
        verify_hash(item, actual_hash, f"Thumbnail {output.name}")
        verified.append({
            "path": relative(output),
            "dimensions": list(size),
            "sha256": actual_hash,
        })

    design = manifest.get("design")
    if isinstance(design, dict) and design.get("story_specific_title") is False:
        fail("Thumbnail design is not story-specific")
    return {
        "status": "valid",
        "path": relative(path),
        "sha256": digest_file(path),
        "resolution": list(dimensions),
        "verified_outputs": verified,
    }


def parse_metadata_text(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read YouTube metadata: {exc}")
    if not content:
        fail("YouTube metadata text is empty")
    sections = {}
    current = None
    for line in content.splitlines():
        normalized = line.strip().strip("[]").rstrip(":").strip().upper()
        if normalized in {"TITLE", "DESCRIPTION", "TAGS", "LANGUAGE"}:
            current = normalized
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.rstrip())
    title = " ".join(sections.get("TITLE", [])).strip()
    description = "\n".join(sections.get("DESCRIPTION", [])).strip()
    tags_text = " ".join(sections.get("TAGS", [])).strip()
    if not title or not description or not tags_text:
        fail("Metadata must contain TITLE, DESCRIPTION and TAGS")
    if len(title) > 100 or len(description) > 5000:
        fail("Metadata title/description exceeds YouTube limits")
    return {"title": title, "description": description, "tags_text": tags_text}


def split_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = re.split(r"[,;\n]", value)
    elif isinstance(value, list):
        raw = value
    else:
        fail("Metadata tags must be a list or text")
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            fail("Metadata contains a non-text tag")
        tag = re.sub(r"\s+", " ", item).strip().strip(" ,#")
        if not tag:
            continue
        if len(tag) > 100:
            fail("Metadata tag exceeds 100 characters")
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            result.append(tag)
    if not 5 <= len(result) <= 20:
        fail(f"Metadata requires 5–20 unique tags; found {len(result)}")
    return result


def validate_metadata(mode: str, story_title: str) -> dict:
    text_path = safe_file(relative(METADATA), "YouTube metadata text", 10)
    json_path = safe_file(relative(METADATA_JSON), "Structured metadata", 10)
    tags_path = safe_file(relative(METADATA_TAGS), "YouTube tags", 5)
    parsed = parse_metadata_text(text_path)
    structured = read_json(json_path)
    if clean(structured.get("status")).lower() not in {
        "completed", "complete", "success", "done"
    }:
        fail("Structured YouTube metadata is incomplete")
    if clean(structured.get("format")).lower() != mode:
        fail("Structured metadata FORMAT mismatch")
    title = structured.get("title")
    description = structured.get("description")
    if not isinstance(title, str) or not title.strip():
        fail("Structured metadata title missing")
    if not isinstance(description, str) or not description.strip():
        fail("Structured metadata description missing")
    if len(title.strip()) > 100 or len(description.strip()) > 5000:
        fail("Structured metadata exceeds YouTube limits")
    tags = split_tags(structured.get("tags"))
    text_tags = split_tags(parsed["tags_text"])
    dedicated = split_tags(tags_path.read_text(encoding="utf-8-sig"))
    if title.strip() != parsed["title"]:
        fail("Metadata text and JSON titles differ")
    if description.strip() != parsed["description"]:
        fail("Metadata text and JSON descriptions differ")
    expected = {item.casefold() for item in tags}
    if {item.casefold() for item in text_tags} != expected:
        fail("Metadata text tags differ from JSON tags")
    if {item.casefold() for item in dedicated} != expected:
        fail("Dedicated tags differ from JSON tags")
    if len(text_tags) != len(tags) or len(dedicated) != len(tags):
        fail("Metadata tags contain duplicates")
    if not story_title:
        fail("Story title is missing")
    if not title.strip():
        fail("Metadata title is empty")
    return {
        "status": "valid",
        "path": relative(text_path),
        "sha256": digest_file(text_path),
        "json_path": relative(json_path),
        "json_sha256": digest_file(json_path),
        "tags_path": relative(tags_path),
        "tags_sha256": digest_file(tags_path),
        "title_length": len(title),
        "description_length": len(description),
        "tag_count": len(tags),
        "format": mode,
    }


def validate_unique_output_files() -> dict:
    if not OUT.is_dir():
        fail("output/ directory missing")
    file_count = 0
    hashes = {}
    for path in sorted(OUT.rglob("*")):
        if path.is_symlink():
            fail(f"Symlink found in output/: {relative(path)}")
        if not path.is_file():
            continue
        file_count += 1
        if path.stat().st_size <= 0:
            fail(f"Zero-byte output file: {relative(path)}")
        value = digest_file(path)
        hashes.setdefault(value, []).append(path)
    duplicates = []
    for value, paths in hashes.items():
        media = [
            path for path in paths
            if path.suffix.lower() in (
                VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
            )
        ]
        if len(media) > 1:
            duplicates.append({
                "sha256": value,
                "files": [relative(path) for path in media],
            })
    if duplicates:
        fail(f"Duplicate media files detected: {duplicates[:10]}")
    return {
        "status": "valid",
        "file_count": file_count,
        "duplicate_media_files": 0,
        "zero_byte_files": 0,
        "symlinks": 0,
    }


def validate_mode_separation(mode: str) -> dict:
    opposite = "full" if mode == "short" else "short"
    candidates = [
        OUT / "parts" / f"katha_lok_ai_{opposite}.mp4",
        OUT / f"katha_lok_ai_{opposite}.mp4",
        OUT / "videos" / f"katha_lok_ai_{opposite}.mp4",
        ROOT / f"katha-lok-ai-{opposite}-output.zip",
    ]
    found = [relative(path) for path in candidates if path.exists()]
    if found:
        fail(f"Opposite-format deliverables exist: {found}")
    return {
        "status": "valid",
        "selected_format": mode,
        "opposite_format": opposite,
        "opposite_files": [],
    }


def validate_existing_archives(mode: str, parts: int) -> dict:
    archives = sorted(ROOT.glob("katha-lok-*-output.zip"))
    if not archives:
        return {
            "status": "deferred",
            "reason": "The packaging stage creates the story-named ZIP after validation.",
        }
    if len(archives) > 1:
        fail(f"Multiple story output ZIPs exist: {[p.name for p in archives]}")
    archive_path = archives[0]
    if archive_path.stat().st_size < MIN_VIDEO_BYTES:
        fail("Existing story ZIP is too small")
    with zipfile.ZipFile(archive_path, "r") as archive:
        bad = archive.testzip()
        if bad:
            fail(f"ZIP CRC verification failed: {bad}")
        names = archive.namelist()
        if not names:
            fail("ZIP archive is empty")
        if len(names) != len(set(names)):
            duplicates = [
                name for name, count in Counter(names).items() if count > 1
            ]
            fail(f"Duplicate ZIP members: {duplicates[:20]}")
        for name in names:
            member = PurePosixPath(name)
            if (
                member.is_absolute()
                or "\\" in name
                or any(piece in {"", ".", ".."} for piece in member.parts)
            ):
                fail(f"Unsafe ZIP member path: {name}")
        video_members = [
            name for name in names
            if PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS
        ]
        expected = {
            f"output/parts/part_{number:02d}.mp4"
            for number in range(1, parts + 1)
        }
        if set(video_members) != expected:
            fail("Existing ZIP Part video paths/numbering do not match selection")
        if any(
            "katha_lok_ai_final.mp4" in name
            or "katha_lok_ai_short.mp4" in name
            or "katha_lok_ai_full.mp4" in name
            for name in names
        ):
            fail("Existing ZIP contains combined or alias videos")
        return {
            "status": "valid",
            "archive": relative(archive_path),
            "archive_bytes": archive_path.stat().st_size,
            "archive_sha256": digest_file(archive_path),
            "entry_count": len(names),
            "part_video_count": len(video_members),
            "crc": "valid",
        }


def verify_report(report: dict, mode: str, parts: int, scenes: int) -> None:
    if report.get("status") != "completed":
        fail("Persisted validation report status mismatch")
    if report.get("format") != mode:
        fail("Persisted validation report FORMAT mismatch")
    if report.get("parts") != parts or report.get("configured_parts") != parts:
        fail("Persisted validation report PARTS mismatch")
    if report.get("scenes_per_part") != scenes:
        fail("Persisted validation report SCENES mismatch")
    checks = report.get("checks")
    if not isinstance(checks, dict):
        fail("Persisted validation report checks missing")
    required = (
        "story", "photo_motion", "narration", "sound_design", "render",
        "captions", "thumbnail", "metadata", "unique_output_files",
        "mode_separation",
    )
    for name in required:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Persisted validation check failed: {name}")


def main() -> int:
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            fail(f"{command} is required but unavailable")
    if not INPUT_FILE.is_file():
        fail("Input/topic.txt is missing")
    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    parts = positive_int(get_parts(config), "Selected PARTS")
    scenes = positive_int(get_scenes(config), "Selected SCENES")
    fps = parse_fps(get_fps(config), "Configured FPS")
    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")
    if parts > 1000 or scenes > 100 or not 0 < fps <= 60:
        fail("Selected PARTS, SCENES or FPS exceeds allowed limits")
    expected_size = (720, 1280) if mode == "short" else (1280, 720)

    log("=" * 68)
    log("KATHA LOK AI — FINAL OUTPUT VALIDATION")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("SELECTED PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("RESOLUTION:", expected_size)
    log("FPS:", fps)

    story_check = validate_story(parts, scenes, mode)
    photo_check = validate_photo(parts, scenes, mode)
    narration_check = validate_narration(parts, scenes)
    sound_check = validate_sound(config, parts, scenes)
    render_check = validate_render(
        mode, parts, scenes, expected_size, fps
    )
    captions_check = validate_captions(parts, scenes, mode)
    thumbnail_check = validate_thumbnail(mode)
    metadata_check = validate_metadata(mode, story_check["title"])
    unique_check = validate_unique_output_files()
    mode_check = validate_mode_separation(mode)
    archive_check = validate_existing_archives(mode, parts)

    checks = {
        "story": story_check,
        "photo_motion": photo_check,
        "narration": narration_check,
        "sound_design": sound_check,
        "render": render_check,
        "captions": captions_check,
        "thumbnail": thumbnail_check,
        "metadata": metadata_check,
        "unique_output_files": unique_check,
        "mode_separation": mode_check,
    }
    for name, item in checks.items():
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Required validation failed: {name}")

    report = {
        "status": "completed",
        "version": VERSION,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": mode,
        "title": story_check["title"],
        "parts": parts,
        "configured_parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "resolution": list(expected_size),
        "fps": fps,
        "part_outputs_only": True,
        "combined_video_created": False,
        "format_alias_created": False,
        "checks": checks,
        "archive_validation": archive_check,
        "api_calls_made": 0,
        "story_modified": False,
        "limitations": [
            "Media streams, resolution, FPS, duration and file hashes are checked.",
            "Story parts, scene order, role records and selected part count are checked.",
            "Narration audio, sound tracks and SFX cue timing are checked.",
            "Caption cue numbering, timestamps, order and overlap are checked.",
            "SRT timing is checked; word-level speech alignment is not independently proven.",
            "Story meaning preservation cannot be proven by file integrity checks alone.",
            "ZIP verification is deferred until the packaging stage creates the archive.",
            "This validator does not call external providers or regenerate assets.",
        ],
    }
    write_json(REPORT, report)
    saved = read_json(REPORT)
    if not isinstance(saved, dict):
        fail("Validation report could not be reloaded")
    verify_report(saved, mode, parts, scenes)

    log("-" * 68)
    log("FINAL VALIDATION: SUCCESS")
    log("FORMAT:", mode)
    log("TITLE:", story_check["title"])
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("MAIN SCENES:", parts * scenes)
    log("NARRATION RECORDS:", narration_check["record_count"])
    log("NARRATION DURATION:", narration_check["total_duration"])
    log("SFX CUES:", sound_check["sfx_cue_count"])
    log("RENDERED PART VIDEOS:", len(render_check["part_records"]))
    log("THUMBNAIL:", thumbnail_check["path"])
    log("METADATA:", metadata_check["path"])
    log("ZIP VALIDATION:", archive_check["status"])
    log("REPORT:", relative(REPORT))
    log("=" * 68)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("FINAL VALIDATION: CANCELLED")
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        log("FINAL VALIDATION: FAILED", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)