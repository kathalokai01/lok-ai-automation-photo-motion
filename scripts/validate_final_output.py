#!/usr/bin/env python3
"""Strict final validation for selected-format Katha Lok AI deliverables."""

from __future__ import annotations

import hashlib
import json
import math
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
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_fps,
)

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"

STORY = OUT / "story" / "story.json"
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

VERSION = "final-validator-v70-role-aware-part-only-integrity"

MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
FPS_TOLERANCE = 0.05
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            import os
            os.fsync(handle.fileno())

        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, optional: bool = False) -> Any:
    if not path.is_file():
        if optional:
            return None
        fail(f"Required JSON missing: {relative(path)}")

    if path.stat().st_size <= 0:
        fail(f"Required JSON is empty: {relative(path)}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Invalid JSON {relative(path)}: {exc}")

    return data


def relative(path: Path) -> str:
    resolved = Path(path).resolve()

    if not resolved.is_relative_to(ROOT.resolve()):
        fail(f"Path escapes repository: {resolved}")

    return resolved.relative_to(ROOT.resolve()).as_posix()


def rows(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def pick(row: Any, *keys: str) -> str | None:
    if not isinstance(row, dict):
        return None

    for key in keys:
        value = row.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def safe_file(
    value: Any,
    label: str,
    minimum: int = 1,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path missing")

    candidate = Path(value.strip())

    if not candidate.is_absolute():
        candidate = ROOT / candidate

    resolved = candidate.resolve()

    if not resolved.is_relative_to(ROOT.resolve()):
        fail(f"{label}: path escapes repository")

    if not resolved.is_file():
        fail(f"{label}: file not found: {relative(resolved)}")

    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small")

    return resolved


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        fail(f"{label}: expected positive integer")

    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(f"{label}: expected positive integer")

    if result < 1:
        fail(f"{label}: value must be positive")

    return result


def parse_fps(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (ValueError, TypeError, ZeroDivisionError):
        fail(f"{label}: invalid FPS {value!r}")

    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS {value!r}")

    return result


def media_info(path: Path) -> dict:
    executable = shutil.which("ffprobe")

    if not executable:
        fail("ffprobe is required but not installed")

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
        fail(f"ffprobe failed for {path.name}: {result.stderr[-500:]}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media probe result for {path.name}: {exc}")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {path.name}")

    streams = data.get("streams", [])

    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
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
        fail(
            f"{label}: expected {expected_fps} FPS, "
            f"found {info['fps']:.4f}"
        )

    return info


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

        fail(f"Invalid boolean configuration value: {key}")

    return True


def expected_main_keys(parts: int, scenes: int) -> list[tuple[int, int]]:
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def scene_key(row: dict, label: str) -> tuple[int, int]:
    try:
        part = int(row["part"])
        scene = int(row.get("local_scene", row.get("scene", 0)))
    except (KeyError, TypeError, ValueError):
        fail(f"{label}: invalid part/scene mapping")

    if part < 1 or scene < 1:
        fail(f"{label}: non-positive part/scene mapping")

    return part, scene


def role_name(row: dict) -> str:
    value = clean(row.get("role") or row.get("kind") or "main").lower()

    if value in {"extra", "extra_scene"}:
        value = clean(row.get("kind")).lower()

    if value not in ROLE_ORDER:
        fail(f"Unknown scene role: {value!r}")

    return value


def role_key(row: dict, label: str) -> tuple[int, str, int | None]:
    try:
        part = positive_int(row.get("part"), f"{label} part")
    except RuntimeError:
        fail(f"{label}: invalid part mapping")

    role = role_name(row)

    if role == "main":
        scene = scene_key(row, label)[1]
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

        if part == parts:
            result.add((part, "resolution", None))

    return result


def validate_role_rows(
    items: Any,
    parts: int,
    scenes: int,
    label: str,
    allow_main_only: bool = False,
) -> dict:
    if not isinstance(items, list):
        fail(f"{label}: scene records missing")

    expected = expected_role_keys(parts, scenes)
    if allow_main_only:
        expected = {
            key for key in expected if key[1] == "main"
        }

    mapping = {}

    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid record {index}")

        key = role_key(row, label)

        if key not in expected:
            fail(f"{label}: unexpected scene role or ID: {key}")

        if key in mapping:
            fail(f"{label}: duplicate scene record: {key}")

        if key[1] == "main":
            expected_id = (
                f"part_{key[0]:02d}_scene_{key[2]:02d}"
            )
        else:
            expected_id = f"part_{key[0]:02d}_{key[1]}"

        declared_id = clean(row.get("scene_id") or row.get("job_id"))

        if declared_id and declared_id != expected_id:
            fail(
                f"{label}: incorrect scene ID for {key}: "
                f"{declared_id}"
            )

        mapping[key] = row

    if set(mapping) != expected:
        fail(
            f"{label}: role mapping incomplete; "
            f"missing={sorted(expected - set(mapping), key=str)}"
        )

    return mapping


def validate_main_scene_rows(
    items: Any,
    parts: int,
    scenes: int,
    label: str,
) -> dict:
    if not isinstance(items, list):
        fail(f"{label}: main scene list missing")

    expected = expected_main_keys(parts, scenes)

    if len(items) != len(expected):
        fail(
            f"{label}: expected {len(expected)} Main scenes, "
            f"found {len(items)}"
        )

    mapping = {}

    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid scene row {index}")

        key = scene_key(row, label)

        if key != expected[index - 1]:
            fail(
                f"{label}: scene order mismatch at row {index}; "
                f"expected {expected[index - 1]}, found {key}"
            )

        if key in mapping:
            fail(f"{label}: duplicate scene {key}")

        global_scene = row.get(
            "global_scene",
            row.get("global_scene_number", index),
        )

        if positive_int(global_scene, f"{label} global scene") != index:
            fail(f"{label}: global scene numbering mismatch at {key}")

        mapping[key] = row

    return mapping


def validate_audio_file(value: Any, label: str) -> dict:
    path = safe_file(value, label, MIN_AUDIO_BYTES)
    info = media_info(path)

    if not info["audio"]:
        fail(f"{label}: audio stream missing")

    if info["duration"] < 0.25:
        fail(f"{label}: audio duration is too short")

    return {
        "path": relative(path),
        "duration": round(info["duration"], 3),
        "sha256": sha256(path),
    }


def verify_declared_hash(row: dict, actual: str, label: str) -> None:
    declared = row.get("sha256")

    if declared and clean(declared).lower() != actual.lower():
        fail(f"{label}: SHA256 mismatch")


def validate_story(parts: int, scenes: int, mode: str) -> dict:
    data = read_json(STORY)

    if data.get("status") != "completed":
        fail("Story status is not completed")

    if clean(data.get("format")).lower() != mode:
        fail("Story format differs from Input/topic.txt")

    title = clean(data.get("title"))

    if not title:
        fail("Story title is missing")

    if positive_int(data.get("total_parts"), "Story total_parts") != parts:
        fail("Story part count differs from selected part count")

    if positive_int(data.get("scenes_per_part"), "Story scenes_per_part") != scenes:
        fail("Story scenes-per-part mismatch")

    if positive_int(data.get("total_scenes"), "Story total_scenes") != parts * scenes:
        fail("Story total scene count mismatch")

    story_parts = data.get("parts")

    if not isinstance(story_parts, list) or len(story_parts) != parts:
        fail("Story parts list is incomplete")

    all_scenes = data.get("scenes")

    if not isinstance(all_scenes, list) or len(all_scenes) != parts * scenes:
        fail("Story Main scene list is incomplete")

    flattened = []
    words = []

    for part_number, part in enumerate(story_parts, 1):
        if not isinstance(part, dict):
            fail(f"Story part {part_number} is invalid")

        if positive_int(part.get("part"), "Story part number") != part_number:
            fail("Story part order is invalid")

        if clean(part.get("part_id")) != f"part_{part_number:02d}":
            fail(f"Story part ID mismatch: {part_number}")

        narration = clean(part.get("narration"))

        if not narration:
            fail(f"Story narration missing in part {part_number}")

        word_count = len(re.findall(r"\S+", narration))
        declared_count = part.get("word_count")

        if declared_count is not None:
            if positive_int(declared_count, "Part word_count") != word_count:
                fail(f"Part word count mismatch: {part_number}")

        if mode == "short" and not 120 <= word_count <= 150:
            fail(
                f"SHORT part {part_number} has {word_count} words; "
                "required range is 120–150"
            )

        if narration[-1] not in (
            "।", "॥", ".", "!", "?", "…", ",", ";", ":", "—", "–"
        ):
            fail(
                f"Part {part_number} does not end at a recognized "
                "sentence or clause boundary"
            )

        scene_rows = part.get("scenes")

        if not isinstance(scene_rows, list) or len(scene_rows) != scenes:
            fail(f"Part {part_number} Main scenes are incomplete")

        scene_text = []

        for scene_number, scene in enumerate(scene_rows, 1):
            if not isinstance(scene, dict):
                fail(f"Invalid scene in part {part_number}")

            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if clean(scene.get("scene_id")) != expected_id:
                fail(f"Story scene ID mismatch: {expected_id}")

            if positive_int(scene.get("part"), "Scene part") != part_number:
                fail(f"Scene part mismatch: {expected_id}")

            if positive_int(scene.get("scene"), "Scene number") != scene_number:
                fail(f"Scene number mismatch: {expected_id}")

            if clean(scene.get("role")) != "main":
                fail(f"Main scene role mismatch: {expected_id}")

            if not clean(scene.get("narration")):
                fail(f"Scene narration missing: {expected_id}")

            if not clean(scene.get("visual_prompt") or scene.get("visual")):
                fail(f"Scene visual prompt missing: {expected_id}")

            scene_text.append(clean(scene.get("narration")))
            flattened.append(scene)

        if clean(" ".join(scene_text)) != narration:
            fail(
                f"Part {part_number} narration differs from "
                "its ordered scene narration"
            )

        words.append(word_count)

    if flattened != all_scenes:
        fail("Top-level Main scenes differ from ordered part scenes")

    combined = clean(" ".join(
        clean(part.get("narration")) for part in story_parts
    ))

    if clean(data.get("story_text")) != combined:
        fail("Story text does not match ordered part narration")

    if positive_int(data.get("total_story_words"), "total_story_words") != len(
        re.findall(r"\S+", combined)
    ):
        fail("Story total word count mismatch")

    verification = read_json(STORY_VERIFICATION, optional=True)

    if verification is not None:
        if verification.get("status") != "completed":
            fail("Story verification report is not completed")

        if verification.get("format") != mode:
            fail("Story verification FORMAT mismatch")

        summary = verification.get("summary", {})

        if summary.get("selected_parts") != parts:
            fail("Story verification selected part count mismatch")

    return {
        "status": "valid",
        "title": title,
        "parts": parts,
        "scenes_per_part": scenes,
        "total_main_scenes": len(all_scenes),
        "words_per_part": words,
        "total_words": len(re.findall(r"\S+", combined)),
        "story_sha256": sha256(STORY),
    }


def validate_photo(parts: int, scenes: int, mode: str) -> dict:
    data = read_json(PHOTO)

    if data.get("status") != "completed":
        fail("Photo-motion manifest is incomplete")

    if clean(data.get("format")).lower() != mode:
        fail("Photo-motion FORMAT mismatch")

    main_rows = rows(data, "scenes", "completed_scenes")

    mapping = validate_main_scene_rows(
        main_rows,
        parts,
        scenes,
        "Photo-motion",
    )

    records = []

    for key, row in mapping.items():
        if row.get("status") != "completed":
            fail(f"Photo-motion scene incomplete: {key}")

        path = safe_file(
            pick(row, "video", "source_video", "output", "path"),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )

        info = media_info(path)

        if not info["video"]:
            fail(f"Photo-motion scene has no video stream: {key}")

        if info["width"] <= 0 or info["height"] <= 0:
            fail(f"Photo-motion dimensions invalid: {key}")

        digest = sha256(path)
        verify_declared_hash(row, digest, f"Photo-motion scene {key}")

        records.append({
            "part": key[0],
            "scene": key[1],
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": digest,
        })

    for field in ("expected_scene_count", "completed_count"):
        if field in data:
            if positive_int(data[field], f"Photo-motion {field}") != parts * scenes:
                fail(f"Photo-motion {field} mismatch")

    return {
        "status": "valid",
        "scene_count": len(records),
        "scenes": records,
    }


def validate_narration(parts: int, scenes: int) -> dict:
    data = read_json(AUDIO)

    if data.get("status") != "completed":
        fail("Narration manifest is incomplete")

    jobs = rows(data, "jobs", "audio_jobs", "scenes")

    if not jobs:
        fail("Narration audio jobs are missing")

    mapping = validate_role_rows(
        jobs,
        parts,
        scenes,
        "Narration",
    )

    records = []
    total_duration = 0.0

    for key in sorted(mapping, key=lambda item: (
        item[0], ROLE_ORDER[item[1]], item[2] or 0
    )):
        row = mapping[key]

        if row.get("status") not in {"completed", "reused"}:
            fail(f"Narration record incomplete: {key}")

        asset = validate_audio_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}",
        )

        verify_declared_hash(row, asset["sha256"], f"Narration {key}")

        if asset["duration"] < 0.25:
            fail(f"Narration duration invalid: {key}")

        total_duration += asset["duration"]

        records.append({
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            **asset,
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
        + int(match.group("ms")) / 1000.0
    )


def validate_srt(path: Path, expected_duration: float | None = None) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read SRT: {exc}")

    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not content:
        fail("SRT file is empty")

    blocks = re.split(r"\n[ \t]*\n", content)
    previous_end = -1.0
    maximum_end = 0.0
    characters = 0

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()

        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index} is incomplete")

        if int(lines[0].strip()) != index:
            fail(f"SRT cue numbering mismatch at {index}")

        if lines[1].count("-->") != 1:
            fail(f"SRT cue {index} has invalid timing")

        start_text, end_text = [
            item.strip() for item in lines[1].split("-->")
        ]

        start = srt_seconds(start_text)
        end = srt_seconds(end_text)

        if end <= start:
            fail(f"SRT cue {index} has invalid duration")

        if start < previous_end - 0.001:
            fail(f"SRT cue {index} overlaps or is out of order")

        caption = "\n".join(lines[2:]).strip()

        if not caption or "-->" in caption:
            fail(f"SRT cue {index} has no caption text")

        tags = re.findall(
            r"</?([A-Za-z]+)(?:\s+[^>]*)?>",
            caption,
        )

        if any(
            tag.lower() not in {"b", "i", "u", "font"}
            for tag in tags
        ):
            fail(f"SRT cue {index} contains unsupported markup")

        previous_end = end
        maximum_end = max(maximum_end, end)
        characters += len(caption)

    if expected_duration is not None:
        if maximum_end > expected_duration + 0.25:
            fail("SRT extends beyond final video duration")

        if maximum_end < expected_duration * 0.50:
            fail("SRT coverage is unexpectedly short")

    return {
        "cue_count": len(blocks),
        "text_characters": characters,
        "last_cue_end": round(maximum_end, 3),
        "overlap_check": "passed",
        "sequence_check": "passed",
        "timestamp_check": "passed",
    }


def validate_captions(
    parts: int,
    scenes: int,
    mode: str,
) -> dict:
    data = read_json(CAPTIONS)

    if data.get("status") != "completed":
        fail("Caption manifest is incomplete")

    if clean(data.get("format")).lower() != mode:
        fail("Caption FORMAT mismatch")

    main_rows = rows(data, "captions", "scenes")

    if not main_rows:
        fail("Caption main scene records are missing")

    main_mapping = validate_main_scene_rows(
        main_rows,
        parts,
        scenes,
        "Captions",
    )

    for key, row in main_mapping.items():
        value = row.get(
            "text",
            row.get("caption", row.get("caption_text")),
        )

        if not isinstance(value, str) or not value.strip():
            fail(f"Caption text missing for Main scene {key}")

    extra_rows = data.get("extra_captions", [])

    if extra_rows:
        expected_extras = set()

        for part in range(1, parts + 1):
            expected_extras.add((part, "hook", None))

            if part < parts:
                expected_extras.add((part, "suspense", None))

            if part == parts:
                expected_extras.add((part, "resolution", None))

        found = set()

        for row in extra_rows:
            if not isinstance(row, dict):
                fail("Invalid extra caption record")

            key = role_key(row, "Extra captions")

            if key not in expected_extras or key in found:
                fail(f"Unexpected or duplicate extra caption: {key}")

            value = row.get(
                "text",
                row.get("caption", row.get("caption_text")),
            )

            if not isinstance(value, str) or not value.strip():
                fail(f"Extra caption text missing: {key}")

            found.add(key)

        if found != expected_extras:
            fail(
                "Extra caption mapping incomplete: "
                f"missing={sorted(expected_extras - found, key=str)}"
            )

    srt_path = safe_file(relative(SRT), "Final SRT", 10)
    srt_report = validate_srt(srt_path)

    return {
        "status": "valid",
        "main_scene_count": len(main_mapping),
        "extra_caption_count": len(extra_rows),
        "srt": relative(srt_path),
        "srt_sha256": sha256(srt_path),
        **srt_report,
    }


def canonical_track_name(name: str) -> str:
    aliases = {
        "ambient": "ambient",
        "ambience": "ambient",
        "environment": "ambient",
        "environmental": "ambient",
        "music": "music",
        "background_music": "music",
        "soundtrack": "music",
        "sfx": "sfx",
        "sound_effect": "sfx",
        "sound_effects": "sfx",
    }

    value = str(name).strip().lower()
    return aliases.get(value, value)


def validate_sound(config: dict) -> dict:
    data = read_json(SOUND)

    if data.get("status") != "completed":
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
        logical = canonical_track_name(name)

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

        if not isinstance(record, dict) or record.get("status") != "completed":
            fail(f"Enabled sound track missing or incomplete: {name}")

        asset = validate_audio_file(
            pick(record, "path", "file", "output", "audio_path"),
            f"Sound track {name}",
        )

        verify_declared_hash(record, asset["sha256"], f"Sound track {name}")

        checked_tracks[name] = {
            "status": "valid",
            **asset,
        }

    sfx_records = []

    if enabled["sfx"]:
        cue_data = read_json(SFX)

        if cue_data.get("status") != "completed":
            fail("SFX cue manifest is incomplete")

        cues = rows(cue_data, "cues", "items")

        if not cues:
            fail("SFX is enabled but no cues exist")

        declared_count = cue_data.get("cue_count")

        if declared_count is not None:
            if positive_int(declared_count, "SFX cue_count") != len(cues):
                fail("SFX cue_count mismatch")

        for index, cue in enumerate(cues, 1):
            if not isinstance(cue, dict):
                fail(f"Invalid SFX cue {index}")

            try:
                part = positive_int(cue.get("part"), "SFX part")
                scene = positive_int(cue.get("scene"), "SFX scene")
                timestamp = float(
                    cue.get("start_seconds", cue.get("time", 0))
                )
                gain = float(cue.get("gain", 0))
            except (TypeError, ValueError):
                fail(f"Invalid SFX timing cue {index}")

            if not math.isfinite(timestamp) or timestamp < 0:
                fail(f"Invalid SFX timestamp {index}")

            if not math.isfinite(gain) or gain < 0:
                fail(f"Invalid SFX gain {index}")

            if part > positive_int(
                cue_data.get("parts", part),
                "SFX part count",
            ):
                fail(f"SFX part outside selected range: {index}")

            asset = validate_audio_file(
                pick(cue, "file", "path", "audio", "output"),
                f"SFX cue {index}",
            )

            scene_duration = cue.get("narration_duration_seconds")

            if scene_duration is not None:
                try:
                    scene_duration = float(scene_duration)
                except (TypeError, ValueError):
                    fail(f"Invalid SFX narration duration: {index}")

                if (
                    not math.isfinite(scene_duration)
                    or scene_duration <= 0
                    or timestamp >= scene_duration
                ):
                    fail(f"SFX cue outside measured narration duration: {index}")

            sfx_records.append({
                "part": part,
                "scene": scene,
                "time": round(timestamp, 4),
                "gain": round(gain, 4),
                **asset,
            })

        timing = read_json(SFX_VALIDATION)

        if timing.get("status") != "completed":
            fail("SFX timing validation is incomplete")

        nested = timing.get("sfx")

        if not isinstance(nested, dict) or nested.get("status") != "valid":
            fail("SFX timing validation did not confirm valid cues")

        if timing.get("cue_count") is not None:
            if positive_int(timing["cue_count"], "SFX validated cue count") != len(cues):
                fail("SFX timing report cue count mismatch")

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

    if data.get("status") != "completed":
        fail("Render manifest is incomplete")

    if clean(data.get("format")).lower() != mode:
        fail("Render FORMAT mismatch")

    if data.get("part_outputs_only") is not True:
        fail("Render manifest does not confirm Part-only outputs")

    try:
        manifest_fps = parse_fps(data.get("fps"), "Render manifest")
        width = positive_int(data.get("width"), "Render width")
        height = positive_int(data.get("height"), "Render height")
    except (TypeError, ValueError):
        fail("Render manifest FPS or dimensions are invalid")

    if abs(manifest_fps - expected_fps) > FPS_TOLERANCE:
        fail("Render manifest FPS mismatch")

    if (width, height) != expected_size:
        fail("Render dimensions differ from selected format")

    all_rows = data.get("all_completed_scenes")

    if not isinstance(all_rows, list):
        fail("Render manifest all_completed_scenes missing")

    role_mapping = validate_role_rows(
        all_rows,
        parts,
        scenes,
        "Final render",
    )

    scene_records = []

    for key in sorted(role_mapping, key=lambda item: (
        item[0], ROLE_ORDER[item[1]], item[2] or 0
    )):
        row = role_mapping[key]

        if row.get("status") != "completed":
            fail(f"Rendered role record incomplete: {key}")

        if row.get("captions_burned") is not True:
            fail(f"Captions not burned into rendered role: {key}")

        path = safe_file(
            pick(row, "video", "output", "rendered_video", "path"),
            f"Rendered role {key}",
            MIN_VIDEO_BYTES,
        )

        info = validate_video(
            path,
            expected_size,
            expected_fps,
            f"Rendered role {key}",
        )

        digest = sha256(path)
        verify_declared_hash(row, digest, f"Rendered role {key}")

        scene_records.append({
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": digest,
        })

    part_rows = data.get("parts")

    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Render manifest must contain exactly {parts} Part videos")

    part_records = []
    total_duration = 0.0
    seen_paths = set()

    for index, row in enumerate(part_rows, 1):
        if not isinstance(row, dict):
            fail(f"Rendered Part {index} record must be an object")

        if positive_int(row.get("part"), "Rendered Part number") != index:
            fail(f"Rendered Part order mismatch: {index}")

        if row.get("status") != "completed":
            fail(f"Rendered Part {index} is incomplete")

        path = safe_file(
            pick(row, "path", "video", "output"),
            f"Rendered Part {index}",
            MIN_VIDEO_BYTES,
        )

        if path.resolve() in seen_paths:
            fail(f"Duplicate rendered Part path: {relative(path)}")

        seen_paths.add(path.resolve())

        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(
                f"Rendered Part must be stored in output/parts/: "
                f"{relative(path)}"
            )

        expected_name = f"part_{index:02d}.mp4"

        if path.name != expected_name:
            fail(
                f"Rendered Part filename mismatch: expected "
                f"{expected_name}, found {path.name}"
            )

        info = validate_video(
            path,
            expected_size,
            expected_fps,
            f"Rendered Part {index}",
        )

        digest = sha256(path)
        verify_declared_hash(row, digest, f"Rendered Part {index}")

        declared_duration = row.get("duration")

        if declared_duration is not None:
            try:
                declared_duration = float(declared_duration)
            except (TypeError, ValueError):
                fail(f"Rendered Part {index} duration metadata invalid")

            if (
                not math.isfinite(declared_duration)
                or abs(declared_duration - info["duration"]) > DURATION_TOLERANCE
            ):
                fail(f"Rendered Part {index} measured duration mismatch")

        total_duration += info["duration"]

        part_records.append({
            "part": index,
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": digest,
            "resolution": [info["width"], info["height"]],
            "fps": round(info["fps"], 4),
        })

    if data.get("final_video") or data.get("alias"):
        fail(
            "Render manifest contains combined/alias video fields; "
            "only individual Part videos are permitted"
        )

    opposite_format = (
        "full" if mode == "short" else "short"
    )

    for path in (
        OUT / "parts" / f"katha_lok_ai_{opposite_format}.mp4",
        OUT / f"katha_lok_ai_{opposite_format}.mp4",
        OUT / "videos" / f"katha_lok_ai_{opposite_format}.mp4",
    ):
        if path.exists():
            fail(f"Opposite-format video exists: {relative(path)}")

    return {
        "status": "valid",
        "format": mode,
        "part_count": parts,
        "role_record_count": len(scene_records),
        "main_scene_count": parts * scenes,
        "part_duration_total": round(total_duration, 3),
        "part_records": part_records,
        "role_records": scene_records,
    }


def validate_thumbnail(mode: str) -> dict:
    path = safe_file(
        relative(THUMBNAIL),
        "Thumbnail",
        MIN_IMAGE_BYTES,
    )

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format

    except Exception as exc:
        fail(f"Invalid thumbnail image: {exc}")

    expected_size = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    if image_format != "JPEG" or dimensions != expected_size:
        fail(
            f"{mode.upper()} thumbnail must be JPEG "
            f"{expected_size[0]}x{expected_size[1]}; "
            f"found {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST)

    if manifest.get("status") != "completed":
        fail("Thumbnail manifest is incomplete")

    if clean(manifest.get("format")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")

    declared = pick(manifest, "thumbnail", "path", "output")

    if not declared:
        fail("Thumbnail manifest main image path missing")

    declared_path = safe_file(
        declared,
        "Thumbnail manifest main image",
        MIN_IMAGE_BYTES,
    )

    if declared_path.resolve() != path.resolve():
        fail("Thumbnail manifest points to a different main image")

    output_rows = manifest.get("thumbnails")

    if not isinstance(output_rows, list) or not output_rows:
        fail("Thumbnail manifest output list missing")

    verified = []
    seen_paths = set()

    for index, item in enumerate(output_rows, 1):
        if not isinstance(item, dict):
            fail(f"Invalid thumbnail output record {index}")

        output = safe_file(
            item.get("file"),
            f"Thumbnail output {index}",
            MIN_IMAGE_BYTES,
        )

        if output.resolve() in seen_paths:
            fail(f"Duplicate thumbnail output path: {relative(output)}")

        seen_paths.add(output.resolve())

        try:
            with Image.open(output) as image:
                image.verify()

            with Image.open(output) as image:
                size = image.size
                fmt = image.format

        except Exception as exc:
            fail(f"Thumbnail output invalid: {output.name}: {exc}")

        expected = (
            positive_int(item.get("width"), "Thumbnail width"),
            positive_int(item.get("height"), "Thumbnail height"),
        )

        if fmt != "JPEG" or size != expected:
            fail(f"Thumbnail output dimensions/format mismatch: {output.name}")

        if item.get("success") is not True:
            fail(f"Thumbnail output not marked successful: {output.name}")

        digest = sha256(output)
        verify_declared_hash(item, digest, f"Thumbnail {output.name}")

        verified.append({
            "path": relative(output),
            "dimensions": list(size),
            "sha256": digest,
        })

    design = manifest.get("design")

    if not isinstance(design, dict):
        fail("Thumbnail story-specific design metadata missing")

    if design.get("story_specific_title") is not True:
        fail("Thumbnail does not confirm story-specific title design")

    return {
        "status": "valid",
        "path": relative(path),
        "sha256": sha256(path),
        "resolution": list(dimensions),
        "verified_outputs": verified,
    }


def parse_metadata_text(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read YouTube metadata text: {exc}")

    if not content:
        fail("YouTube metadata text is empty")

    sections = {}
    current = None

    for line in content.splitlines():
        normalized = (
            line.strip()
            .strip("[]")
            .rstrip(":")
            .strip()
            .upper()
        )

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

    if len(title) > 100:
        fail("Metadata title exceeds 100 characters")

    if len(description) > 5000:
        fail("Metadata description exceeds 5000 characters")

    return {
        "title": title,
        "description": description,
        "tags_text": tags_text,
    }


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

        key = tag.casefold()

        if key not in seen:
            seen.add(key)
            result.append(tag)

    if not 5 <= len(result) <= 20:
        fail(
            f"Metadata requires 5–20 unique tags; found {len(result)}"
        )

    return result


def validate_metadata(mode: str, story_title: str) -> dict:
    text_path = safe_file(
        relative(METADATA),
        "YouTube metadata text",
        10,
    )

    json_path = safe_file(
        relative(METADATA_JSON),
        "Structured YouTube metadata",
        10,
    )

    tags_path = safe_file(
        relative(METADATA_TAGS),
        "Dedicated YouTube tags",
        5,
    )

    parsed = parse_metadata_text(text_path)
    structured = read_json(json_path)

    if structured.get("status") != "completed":
        fail("Structured metadata is incomplete")

    if clean(structured.get("format")).lower() != mode:
        fail("Structured metadata FORMAT mismatch")

    title = structured.get("title")
    description = structured.get("description")

    if not isinstance(title, str) or not title.strip():
        fail("Structured metadata title missing")

    if not isinstance(description, str) or not description.strip():
        fail("Structured metadata description missing")

    if len(title.strip()) > 100 or len(description.strip()) > 5000:
        fail("Structured metadata title or description exceeds limits")

    tags = split_tags(structured.get("tags"))

    if title.strip() != parsed["title"]:
        fail("Metadata text title differs from JSON title")

    if description.strip() != parsed["description"]:
        fail("Metadata text description differs from JSON description")

    combined_tags = split_tags(parsed["tags_text"])

    try:
        dedicated_text = tags_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read dedicated tags file: {exc}")

    dedicated_tags = split_tags(dedicated_text)

    expected = {tag.casefold() for tag in tags}

    if {tag.casefold() for tag in combined_tags} != expected:
        fail("Metadata text tags differ from structured JSON")

    if {tag.casefold() for tag in dedicated_tags} != expected:
        fail("Dedicated tags differ from structured JSON")

    if len(combined_tags) != len(tags) or len(dedicated_tags) != len(tags):
        fail("Metadata tags contain duplicates or missing entries")

    if not story_title:
        fail("Story title is missing")

    if title.strip() == story_title and len(story_title) > 100:
        fail("Story title exceeds YouTube metadata title limit")

    return {
        "status": "valid",
        "path": relative(text_path),
        "sha256": sha256(text_path),
        "json_path": relative(json_path),
        "json_sha256": sha256(json_path),
        "tags_path": relative(tags_path),
        "tags_sha256": sha256(tags_path),
        "title_length": len(title),
        "description_length": len(description),
        "tag_count": len(tags),
        "format": mode,
    }


def validate_unique_output_files() -> dict:
    if not OUT.is_dir():
        fail("output/ directory missing")

    hashes = {}
    duplicate_media = []
    file_count = 0

    for path in sorted(OUT.rglob("*")):
        if path.is_symlink():
            fail(f"Symlink found in output/: {relative(path)}")

        if not path.is_file():
            continue

        file_count += 1

        if path.stat().st_size == 0:
            fail(f"Zero-byte output file: {relative(path)}")

        digest = sha256(path)
        hashes.setdefault(digest, []).append(path)

    for digest, paths in hashes.items():
        media = [
            path for path in paths
            if path.suffix.lower() in (
                VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
            )
        ]

        if len(media) > 1:
            duplicate_media.append({
                "sha256": digest,
                "files": [relative(path) for path in media],
            })

    if duplicate_media:
        fail(
            "Duplicate media files detected: "
            f"{duplicate_media[:10]}"
        )

    return {
        "status": "valid",
        "file_count": file_count,
        "duplicate_media_files": 0,
        "zero_byte_files": 0,
        "symlinks": 0,
    }


def validate_no_opposite_mode_files(mode: str) -> dict:
    opposite = "full" if mode == "short" else "short"

    candidates = [
        OUT / "parts" / f"katha_lok_ai_{opposite}.mp4",
        OUT / f"katha_lok_ai_{opposite}.mp4",
        OUT / "videos" / f"katha_lok_ai_{opposite}.mp4",
        ROOT / f"katha-lok-ai-{opposite}-output.zip",
    ]

    found = [
        relative(path)
        for path in candidates
        if path.exists()
    ]

    if found:
        fail(f"Opposite-format deliverables found: {found}")

    return {
        "status": "valid",
        "selected_format": mode,
        "opposite_format": opposite,
        "opposite_files": [],
    }


def validate_archive_if_present(mode: str, parts: int) -> dict:
    archives = sorted(ROOT.glob("katha-lok-*-output.zip"))

    if not archives:
        return {
            "status": "deferred",
            "reason": "package_output.py creates the story-named ZIP after this validation stage",
        }

    if len(archives) > 1:
        fail(
            "Multiple story output ZIP files exist: "
            f"{[path.name for path in archives]}"
        )

    archive_path = archives[0]

    if archive_path.stat().st_size < MIN_VIDEO_BYTES:
        fail("Existing story ZIP is empty or too small")

    with zipfile.ZipFile(archive_path, "r") as archive:
        bad = archive.testzip()

        if bad:
            fail(f"ZIP CRC verification failed: {bad}")

        names = archive.namelist()

        if not names:
            fail("ZIP archive is empty")

        if len(names) != len(set(names)):
            duplicates = [
                name
                for name, count in Counter(names).items()
                if count > 1
            ]
            fail(f"Duplicate ZIP members: {duplicates[:20]}")

        unsafe = []

        for name in names:
            member = PurePosixPath(name)

            if member.is_absolute() or ".." in member.parts:
                unsafe.append(name)

        if unsafe:
            fail(f"Unsafe ZIP member paths: {unsafe[:20]}")

        video_members = [
            name for name in names
            if PurePosixPath(name).suffix.lower() == ".mp4"
        ]

        if len(video_members) != parts:
            fail(
                f"ZIP must contain exactly {parts} Part videos; "
                f"found {len(video_members)}"
            )

        expected_names = {
            f"part_{number:02d}.mp4"
            for number in range(1, parts + 1)
        }

        found_names = {
            PurePosixPath(name).name
            for name in video_members
        }

        if found_names != expected_names:
            fail("ZIP Part video names or numbering are invalid")

        if any(
            "katha_lok_ai_final.mp4" in name
            or "katha_lok_ai_short.mp4" in name
            or "katha_lok_ai_full.mp4" in name
            for name in names
        ):
            fail("ZIP contains forbidden combined or alias videos")

        return {
            "status": "valid",
            "archive": relative(archive_path),
            "archive_bytes": archive_path.stat().st_size,
            "archive_sha256": sha256(archive_path),
            "entry_count": len(names),
            "part_video_count": len(video_members),
            "crc": "valid",
        }


def verify_report(report: dict, mode: str, parts: int, scenes: int) -> None:
    if report.get("status") != "completed":
        fail("Persisted validation report status mismatch")

    if report.get("format") != mode:
        fail("Persisted validation report FORMAT mismatch")

    if report.get("parts") != parts:
        fail("Persisted validation report PARTS mismatch")

    if report.get("scenes_per_part") != scenes:
        fail("Persisted validation report SCENES mismatch")

    checks = report.get("checks")

    if not isinstance(checks, dict):
        fail("Persisted validation report checks object missing")

    for name in (
        "story",
        "photo_motion",
        "narration",
        "sound_design",
        "render",
        "captions",
        "thumbnail",
        "metadata",
        "unique_output_files",
        "mode_separation",
    ):
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
    configured_parts = positive_int(get_parts(config), "Configured PARTS")
    scenes = positive_int(get_scenes(config), "Configured SCENES")
    expected_fps = parse_fps(get_fps(config), "Configured FPS")

    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")

    if not 0 < expected_fps <= 60:
        fail(f"Unsupported FPS: {expected_fps}")

    if configured_parts > 1000 or scenes > 100:
        fail("Configured PARTS or SCENES exceeds safety limits")

    expected_size = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    log("=" * 68)
    log("KATHA LOK AI — STRICT FINAL OUTPUT VALIDATION")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("SELECTED PARTS:", configured_parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED RESOLUTION:", expected_size)
    log("EXPECTED FPS:", expected_fps)

    story_check = validate_story(
        configured_parts,
        scenes,
        mode,
    )

    photo_check = validate_photo(
        configured_parts,
        scenes,
        mode,
    )

    narration_check = validate_narration(
        configured_parts,
        scenes,
    )

    sound_check = validate_sound(config)

    render_check = validate_render(
        mode,
        configured_parts,
        scenes,
        expected_size,
        expected_fps,
    )

    captions_check = validate_captions(
        configured_parts,
        scenes,
        mode,
    )

    thumbnail_check = validate_thumbnail(mode)

    metadata_check = validate_metadata(
        mode,
        story_check["title"],
    )

    unique_check = validate_unique_output_files()

    mode_check = validate_no_opposite_mode_files(mode)

    archive_check = validate_archive_if_present(
        mode,
        configured_parts,
    )

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
        "parts": configured_parts,
        "configured_parts": configured_parts,
        "scenes_per_part": scenes,
        "expected_scenes": configured_parts * scenes,
        "resolution": list(expected_size),
        "fps": expected_fps,
        "part_outputs_only": True,
        "combined_video_created": False,
        "format_alias_created": False,
        "checks": checks,
        "archive_validation": archive_check,
        "api_calls_made": 0,
        "story_modified": False,
        "limitations": [
            "Media stream presence, dimensions, FPS, duration and SHA256 are checked.",
            "Narration, visual roles, Main scenes and Part numbering are checked.",
            "Caption cue ordering, timing and overlap are checked.",
            "SRT is checked for valid timing and coverage but word-level speech alignment is not independently proven.",
            "Source-story semantic preservation cannot be proven by file-level validation alone.",
            "ZIP validation is deferred when the package stage has not yet created the archive.",
            "External provider availability is not tested by this validator.",
        ],
    }

    write_json(REPORT, report)

    saved = read_json(REPORT)

    if not isinstance(saved, dict):
        fail("Validation report could not be reloaded")

    verify_report(
        saved,
        mode,
        configured_parts,
        scenes,
    )

    log("-" * 68)
    log("FINAL VALIDATION: SUCCESS")
    log("FORMAT:", mode)
    log("TITLE:", story_check["title"])
    log("PARTS:", configured_parts)
    log("SCENES PER PART:", scenes)
    log("MAIN SCENES:", configured_parts * scenes)
    log("NARRATION RECORDS:", narration_check["record_count"])
    log("NARRATION DURATION:", narration_check["total_duration"])
    log("SOUND EFFECT CUES:", sound_check["sfx_cue_count"])
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
        log(
            "FINAL VALIDATION: FAILED",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)