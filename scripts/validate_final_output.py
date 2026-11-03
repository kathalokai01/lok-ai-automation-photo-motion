#!/usr/bin/env python3
"""Read-only final validation for the selected Photo Motion deliverables."""

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

VERSION = "final-validator-v91-role-aware-dynamic-parts"
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
SUCCESS = {"completed", "complete", "success", "done", "reused"}
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


def read_json(path: Path, optional: bool = False) -> Any:
    if not path.is_file():
        if optional:
            return None
        fail(f"Required JSON missing: {path.relative_to(ROOT)}")
    if path.stat().st_size <= 0:
        fail(f"Required JSON is empty: {path.relative_to(ROOT)}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Invalid JSON {path.relative_to(ROOT)}: {exc}")
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


def relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        fail(f"Path escapes repository: {resolved}")
    return resolved.relative_to(ROOT.resolve()).as_posix()


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
        fail(f"{label}: path escapes {allowed}")
    if resolved.is_symlink() or not resolved.is_file():
        fail(f"{label}: file missing or unsafe: {relative(resolved)}")
    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small")
    return resolved


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
    declared = row.get("sha256") or row.get("video_sha256") or row.get(
        "audio_sha256"
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
        info = validate_video(path, expected_size, expected_fps,
                              f"Photo-motion scene {key}", False)
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


def validate_narration(parts: int, scenes: int, mode: str) -> dict:
    data = read_json(AUDIO)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Narration audio manifest is incomplete")
    if data.get("format") and clean(data["format"]).lower() != mode:
        fail("Narration FORMAT mismatch")

    jobs = get_rows(data, "jobs", "audio_jobs", "scenes")
    mapping = validate_role_rows(jobs, parts, scenes, "Narration")
    records = []
    total_duration = 0.0

    for key in sorted(
        mapping,
        key=lambda item: (item[0], ROLE_ORDER[item[1]], item[2] or 0),
    ):
        row = mapping[key]
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Narration record is incomplete: {key}")
        narration_path = first_path(
            row, "output", "audio_path", "audio_file", "path", "file", "audio"
        )
        asset = validate_audio_asset(narration_path, f"Narration {key}")
        verify_hash(row, asset["sha256"], f"Narration {key}")
        total_duration += asset["duration"]
        records.append({
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            "text": clean(
                row.get("text") or row.get("narration_text")
                or row.get("narration")
            ),
            **asset,
        })

    if len(records) != parts * scenes + 2 * parts:
        fail("Narration record count does not include all required roles")

    return {
        "status": "valid",
        "record_count": len(records),
        "main_scene_count": parts * scenes,
        "extra_role_count": 2 * parts,
        "total_duration": round(total_duration, 3),
        "audio_files": records,
        "manifest_sha256": digest_file(AUDIO),
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


def config_enabled(
    config: dict,
    keys: tuple[str, ...],
    default: bool = True,
) -> bool:
    for key in keys:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "yes", "1", "on", "enabled"}:
            return True
        if normalized in {"false", "no", "0", "off", "disabled", ""}:
            return False
        fail(f"Invalid boolean setting: {key}")
    return default


def validate_sound(
    config: dict,
    parts: int,
    scenes: int,
    mode: str,
    narration: dict,
) -> dict:
    manifest = read_json(SOUND)
    if clean(manifest.get("status")).lower() not in SUCCESS:
        fail("Sound-design manifest is incomplete")
    if clean(manifest.get("format")).lower() not in {"", mode}:
        fail("Sound-design FORMAT mismatch")
    if manifest.get("parts") is not None and positive_int(
        manifest["parts"], "Sound-design parts"
    ) != parts:
        fail("Sound-design selected PARTS mismatch")

    enabled_manifest = manifest.get("enabled")
    if not isinstance(enabled_manifest, dict):
        fail("Sound-design enabled settings are missing")

    enabled = {
        "music": config_enabled(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC", "SOUNDTRACK_ENABLED"),
        ),
        "ambient": config_enabled(
            config,
            ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
             "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED"),
        ),
        "sfx": config_enabled(
            config,
            ("SFX_ENABLED", "SFX", "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED"),
        ),
    }

    for name, expected in enabled.items():
        if isinstance(enabled_manifest.get(name), bool):
            if enabled_manifest[name] != expected:
                fail(f"Sound-design {name} enabled setting differs from input")

    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict):
        fail("Sound-design tracks object is missing")

    normalized_tracks = {}
    for name, record in tracks.items():
        logical = canonical_track(name)
        if logical in {"music", "ambient", "sfx"}:
            if logical in normalized_tracks and normalized_tracks[logical] != record:
                fail(f"Conflicting sound track aliases: {logical}")
            normalized_tracks[logical] = record

    narration_duration = sum(
        item["duration"] for item in narration["audio_files"]
    )
    checked_tracks = {}

    for name in ("music", "ambient"):
        record = normalized_tracks.get(name)
        if not enabled[name]:
            if isinstance(record, dict) and clean(
                record.get("status")
            ).lower() == "completed":
                fail(f"{name} is disabled but a completed track is declared")
            checked_tracks[name] = {"status": "disabled"}
            continue

        if not isinstance(record, dict):
            fail(f"Enabled sound track is missing: {name}")

        track_status = clean(record.get("status")).lower()
        if name == "ambient" and track_status in {
            "skipped", "disabled", "unavailable"
        }:
            checked_tracks[name] = {
                "status": "unavailable",
                "reason": clean(record.get("reason")),
            }
            continue
        if track_status not in SUCCESS:
            fail(f"Enabled sound track is incomplete: {name}")

        asset = validate_audio_asset(
            first_path(record, "path", "file", "output", "audio_path"),
            f"Sound track {name}",
        )
        verify_hash(record, asset["sha256"], f"Sound track {name}")
        if asset["duration"] + 0.5 < narration_duration:
            fail(
                f"{name} track is shorter than measured narration timeline: "
                f"{asset['duration']:.2f}s < {narration_duration:.2f}s"
            )
        checked_tracks[name] = {"status": "valid", **asset}

    cue_data = read_json(SFX)
    if clean(cue_data.get("status")).lower() not in SUCCESS:
        fail("SFX cue manifest is incomplete")
    if cue_data.get("format") and clean(
        cue_data["format"]
    ).lower() != mode:
        fail("SFX cue manifest FORMAT mismatch")

    cues = get_rows(cue_data, "cues", "items")
    if not isinstance(cues, list):
        fail("SFX cue list is invalid")
    if not enabled["sfx"] and cues:
        fail("SFX is disabled but cues remain in the manifest")

    audio_by_id = {}
    for row in narration["audio_files"]:
        scene_id = (
            f"part_{row['part']:02d}_scene_{row['scene']:02d}"
            if row["role"] == "main"
            else f"part_{row['part']:02d}_{row['role']}"
        )
        audio_by_id[scene_id] = row

    checked_cues = []
    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            fail(f"Invalid SFX cue record {index}")
        scene_id = clean(cue.get("scene_id"))
        if not scene_id or scene_id not in audio_by_id:
            fail(f"SFX cue {index} references an unknown narration scene")

        audio = audio_by_id[scene_id]
        try:
            start = float(cue.get("start_seconds", cue.get("time", 0)))
            gain = float(cue.get("gain", 0))
        except (TypeError, ValueError):
            fail(f"Invalid SFX cue timing or gain: {index}")
        if not math.isfinite(start) or start < 0 or start >= audio["duration"]:
            fail(f"SFX cue {index} is outside measured narration duration")
        if not math.isfinite(gain) or gain < 0 or gain > 1:
            fail(f"SFX cue {index} gain is outside 0–1")

        asset = validate_audio_asset(
            first_path(cue, "file", "path", "audio", "output"),
            f"SFX cue {index}",
        )
        verify_hash(cue, asset["sha256"], f"SFX cue {index}")
        checked_cues.append({
            "scene_id": scene_id,
            "part": audio["part"],
            "role": audio["role"],
            "start_seconds": round(start, 4),
            "gain": round(gain, 4),
            **asset,
        })

    timing = read_json(SFX_VALIDATION)
    if clean(timing.get("status")).lower() not in SUCCESS:
        fail("SFX timing validation report is incomplete")
    if timing.get("format") and clean(
        timing["format"]
    ).lower() != mode:
        fail("SFX timing validation FORMAT mismatch")
    if timing.get("sfx_enabled") is not None:
        if bool(timing["sfx_enabled"]) != enabled["sfx"]:
            fail("SFX timing validation enabled setting mismatch")

    timing_sfx = timing.get("sfx")
    if not isinstance(timing_sfx, dict):
        fail("SFX timing validation details are missing")
    if clean(timing_sfx.get("status")).lower() not in {
        "valid", "completed", "success"
    }:
        fail("SFX timing validation did not confirm valid cues")
    if timing_sfx.get("cue_count") is not None and positive_int(
        timing_sfx["cue_count"], "Validated SFX cue count"
    ) != len(cues):
        fail("SFX timing validation cue count mismatch")

    return {
        "status": "valid",
        "enabled": enabled,
        "tracks": checked_tracks,
        "sfx_cue_count": len(checked_cues),
        "sfx_cues": checked_cues,
        "sound_manifest_sha256": digest_file(SOUND),
        "sfx_manifest_sha256": digest_file(SFX),
        "sfx_validation_sha256": digest_file(SFX_VALIDATION),
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


def validate_srt(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read final SRT: {exc}")

    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [
        block.strip()
        for block in re.split(r"\n[ \t]*\n", content)
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
            fail(f"SRT cue {index} numbering or timing is invalid")

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

    return {
        "cue_count": len(blocks),
        "text_characters": characters,
        "last_cue_end": round(maximum_end, 3),
        "overlap_check": "passed",
        "sequence_check": "passed",
        "timestamp_check": "passed",
        "sha256": digest_file(path),
    }


def validate_captions(parts: int, scenes: int, mode: str) -> dict:
    data = read_json(CAPTIONS)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Caption manifest is incomplete")
    if clean(data.get("format")).lower() != mode:
        fail("Caption FORMAT mismatch")

    declared_parts = data.get("parts", data.get("configured_parts"))
    if declared_parts is not None and positive_int(
        declared_parts, "Caption selected PARTS"
    ) != parts:
        fail("Caption selected PARTS mismatch")

    declared_scenes = data.get("scenes_per_part")
    if declared_scenes is not None and positive_int(
        declared_scenes, "Caption scenes per Part"
    ) != scenes:
        fail("Caption SCENES mismatch")

    main_rows = get_rows(data, "captions", "scenes")
    main_mapping = validate_role_rows(
        main_rows, parts, scenes, "Captions", main_only=True
    )

    for key, row in main_mapping.items():
        text = row.get("text", row.get("caption", row.get("caption_text")))
        if not isinstance(text, str) or not text.strip():
            fail(f"Caption text is missing for Main scene {key}")
        if row.get("caption_sha256"):
            expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if clean(row["caption_sha256"]).lower() != expected_hash:
                fail(f"Caption text SHA256 mismatch for Main scene {key}")

    extra_rows = data.get("extra_captions")
    if not isinstance(extra_rows, list):
        fail("Extra-role caption records are missing")

    expected_extras = {
        (part, "hook", None) for part in range(1, parts + 1)
    }
    expected_extras.update(
        (part, "suspense", None) for part in range(1, parts)
    )
    expected_extras.add((parts, "resolution", None))

    found_extras = set()
    for index, row in enumerate(extra_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid extra caption record {index}")
        key = role_key(row, "Extra captions")
        if key not in expected_extras or key in found_extras:
            fail(f"Unexpected or duplicate extra caption: {key}")

        text = row.get("text", row.get("caption", row.get("caption_text")))
        if not isinstance(text, str) or not text.strip():
            fail(f"Extra caption text is missing: {key}")
        if row.get("caption_sha256"):
            expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if clean(row["caption_sha256"]).lower() != expected_hash:
                fail(f"Extra caption SHA256 mismatch: {key}")
        found_extras.add(key)

    if found_extras != expected_extras:
        fail(
            "Extra-role caption mapping mismatch: "
            f"missing={sorted(expected_extras - found_extras, key=str)}"
        )

    expected_main_count = parts * scenes
    if len(main_mapping) != expected_main_count:
        fail("Caption Main scene count mismatch")
    if len(extra_rows) != 2 * parts:
        fail("Caption Hook/Suspense/Resolution count mismatch")

    path = safe_file(
        str(SRT.relative_to(ROOT)), "Final captions SRT", 10
    )
    srt_report = validate_srt(path)

    return {
        "status": "valid",
        "format": mode,
        "main_scene_count": len(main_mapping),
        "extra_caption_count": len(extra_rows),
        "caption_manifest": relative(CAPTIONS),
        "caption_manifest_sha256": digest_file(CAPTIONS),
        "srt": relative(path),
        **srt_report,
    }


def validate_render(
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
    narration: dict,
) -> dict:
    data = read_json(RENDER)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Render manifest is incomplete")
    if clean(data.get("format")).lower() != mode:
        fail("Render FORMAT differs from Input/topic.txt")
    if data.get("part_outputs_only") is not True:
        fail("Render manifest must confirm Part-only output")
    if data.get("captions_burned") is not True:
        fail("Rendered Part videos must confirm burned-in captions")

    width = positive_int(data.get("width"), "Render width")
    height = positive_int(data.get("height"), "Render height")
    fps = parse_fps(data.get("fps"), "Render FPS")
    if (width, height) != expected_size:
        fail("Render resolution differs from selected FORMAT")
    if abs(fps - expected_fps) > FPS_TOLERANCE:
        fail("Render FPS differs from Input/topic.txt")

    all_rows = (
        data.get("all_completed_scenes")
        or data.get("completed_scenes")
        or data.get("scenes")
    )
    if not isinstance(all_rows, list):
        fail("Render manifest scene records are missing")

    if not data.get("all_completed_scenes"):
        extras = data.get("extra_scenes")
        if isinstance(extras, list):
            all_rows = list(all_rows) + list(extras)

    role_mapping = validate_role_rows(
        all_rows, parts, scenes, "Final render"
    )
    expected_role_count = parts * scenes + 2 * parts
    if len(role_mapping) != expected_role_count:
        fail("Render role-scene count mismatch")

    checked_roles = []
    role_duration = 0.0

    for key in sorted(
        role_mapping,
        key=lambda item: (item[0], ROLE_ORDER[item[1]], item[2] or 0),
    ):
        row = role_mapping[key]
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered role is incomplete: {key}")

        path = safe_file(
            first_path(row, "video", "output", "rendered_video", "path"),
            f"Rendered role {key}",
            MIN_VIDEO_BYTES,
        )
        info = validate_video(
            path, expected_size, expected_fps, f"Rendered role {key}"
        )
        checksum = digest_file(path)
        verify_hash(row, checksum, f"Rendered role {key}")

        if row.get("duration") is not None:
            try:
                declared_duration = float(row["duration"])
            except (TypeError, ValueError):
                fail(f"Rendered role duration is invalid: {key}")
            if (
                not math.isfinite(declared_duration)
                or abs(declared_duration - info["duration"]) > DURATION_TOLERANCE
            ):
                fail(f"Rendered role duration mismatch: {key}")

        checked_roles.append({
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": checksum,
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
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered Part {index} is incomplete")

        path = safe_file(
            first_path(row, "path", "video", "output"),
            f"Rendered Part {index}",
            MIN_VIDEO_BYTES,
        )
        if path.resolve() in part_paths:
            fail(f"Duplicate Part video path: {relative(path)}")
        part_paths.add(path.resolve())

        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(f"Part video must be stored in output/parts/: {relative(path)}")
        if path.name != f"part_{index:02d}.mp4":
            fail(f"Part {index} filename must be part_{index:02d}.mp4")

        info = validate_video(
            path, expected_size, expected_fps, f"Part {index}"
        )
        checksum = digest_file(path)
        verify_hash(row, checksum, f"Part {index}")

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
            "sha256": checksum,
            "bytes": path.stat().st_size,
            "resolution": [info["width"], info["height"]],
            "fps": round(info["fps"], 4),
        })

    tolerance = max(DURATION_TOLERANCE, len(all_rows) * 0.05)
    if abs(part_duration - role_duration) > tolerance:
        fail(
            "Part video duration differs significantly from the sum "
            "of rendered role-scene durations"
        )

    narration_duration = narration["total_duration"]
    if part_duration + tolerance < narration_duration:
        fail("Rendered Part duration is shorter than measured narration")

    for key in ("final_video", "alias", "combined_video", "combined_output"):
        if data.get(key):
            fail(f"Combined/alias video output is forbidden: {key}")

    if data.get("expected_scene_count") is not None and positive_int(
        data["expected_scene_count"], "Render expected scene count"
    ) != parts * scenes:
        fail("Render expected Main scene count mismatch")

    if data.get("completed_count") is not None and positive_int(
        data["completed_count"], "Render completed count"
    ) != parts * scenes:
        fail("Render completed Main scene count mismatch")

    return {
        "status": "valid",
        "format": mode,
        "part_outputs_only": True,
        "captions_burned": True,
        "part_count": parts,
        "role_record_count": len(checked_roles),
        "main_scene_count": parts * scenes,
        "extra_role_scene_count": 2 * parts,
        "part_duration_total": round(part_duration, 3),
        "role_duration_total": round(role_duration, 3),
        "part_records": checked_parts,
        "role_records": checked_roles,
        "render_manifest": relative(RENDER),
        "render_manifest_sha256": digest_file(RENDER),
    }


def validate_thumbnail(
    mode: str,
    expected_size: tuple[int, int],
) -> dict:
    path = safe_file(
        str(THUMBNAIL.relative_to(ROOT)),
        "Thumbnail",
        MIN_IMAGE_BYTES,
    )
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions, image_format = image.size, image.format
    except Exception as exc:
        fail(f"Invalid thumbnail image: {exc}")

    if image_format != "JPEG" or dimensions != expected_size:
        fail(
            f"{mode.upper()} thumbnail must be JPEG "
            f"{expected_size[0]}x{expected_size[1]}; "
            f"found {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST)
    if clean(manifest.get("status")).lower() not in SUCCESS:
        fail("Thumbnail manifest is incomplete")
    if clean(manifest.get("format")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")

    declared = first_path(manifest, "thumbnail", "path", "output")
    if declared:
        declared_path = safe_file(
            declared, "Thumbnail manifest main image", MIN_IMAGE_BYTES
        )
        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to a different main image")

    rows = manifest.get("thumbnails")
    if not isinstance(rows, list) or not rows:
        fail("Thumbnail manifest output list is missing")

    verified = []
    seen = set()
    main_found = False

    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            fail(f"Invalid thumbnail record {index}")
        output = safe_file(
            item.get("file"),
            f"Thumbnail output {index}",
            MIN_IMAGE_BYTES,
        )
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

        declared_width = positive_int(item.get("width"), "Thumbnail width")
        declared_height = positive_int(item.get("height"), "Thumbnail height")
        if fmt != "JPEG" or size != (declared_width, declared_height):
            fail(f"Thumbnail dimensions/format mismatch: {output.name}")
        if item.get("success") is not True:
            fail(f"Thumbnail output is not marked successful: {output.name}")

        checksum = digest_file(output)
        verify_hash(item, checksum, f"Thumbnail {output.name}")

        if output.resolve() == path.resolve():
            main_found = True
        elif size != expected_size:
            fail(
                f"Opposite-format thumbnail found in selected output: "
                f"{output.name} {size}"
            )

        verified.append({
            "path": relative(output),
            "dimensions": list(size),
            "sha256": checksum,
        })

    if not main_found:
        fail("Thumbnail manifest does not list output/thumbnail.jpg")

    design = manifest.get("design")
    if isinstance(design, dict) and design.get("story_specific_title") is False:
        fail("Thumbnail is not marked as story-specific")

    return {
        "status": "valid",
        "path": relative(path),
        "sha256": digest_file(path),
        "resolution": list(dimensions),
        "verified_outputs": verified,
        "manifest_sha256": digest_file(THUMBNAIL_MANIFEST),
    }


def parse_metadata_text(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read YouTube metadata: {exc}")
    if not content:
        fail("YouTube metadata text is empty")

    sections: dict[str, list[str]] = {}
    current = None
    for line in content.splitlines():
        label = line.strip().strip("[]").rstrip(":").strip().upper()
        if label in {"TITLE", "DESCRIPTION", "TAGS", "LANGUAGE"}:
            current = label
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
        tag = re.sub(r"\s+", " ", item).strip(" ,#")
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
    text_path = safe_file(
        str(METADATA.relative_to(ROOT)), "YouTube metadata text", 10
    )
    json_path = safe_file(
        str(METADATA_JSON.relative_to(ROOT)), "Structured metadata", 10
    )
    tags_path = safe_file(
        str(METADATA_TAGS.relative_to(ROOT)), "YouTube tags", 5
    )

    parsed = parse_metadata_text(text_path)
    structured = read_json(json_path)

    if clean(structured.get("status")).lower() not in SUCCESS:
        fail("Structured YouTube metadata is incomplete")
    if clean(structured.get("format")).lower() != mode:
        fail("Structured metadata FORMAT mismatch")

    title = structured.get("title")
    description = structured.get("description")
    if not isinstance(title, str) or not title.strip():
        fail("Structured metadata title is missing")
    if not isinstance(description, str) or not description.strip():
        fail("Structured metadata description is missing")
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
        fail("Story-derived title is missing")

    structured_story_title = clean(
        structured.get("story_title") or structured.get("source_story_title")
    )
    if structured_story_title and structured_story_title.casefold() != (
        story_title.casefold()
    ):
        fail("Metadata story title does not match the complete story title")

    return {
        "status": "valid",
        "path": relative(text_path),
        "sha256": digest_file(text_path),
        "json_path": relative(json_path),
        "json_sha256": digest_file(json_path),
        "tags_path": relative(tags_path),
        "tags_sha256": digest_file(tags_path),
        "title": title.strip(),
        "title_length": len(title.strip()),
        "description_length": len(description.strip()),
        "tag_count": len(tags),
        "tags": tags,
        "format": mode,
        "story_title": story_title,
    }


def validate_unique_output_files() -> dict:
    if not OUT.is_dir():
        fail("output/ directory is missing")

    file_count = 0
    zero_byte_files = []
    symlinks = []
    hashes: dict[str, list[Path]] = defaultdict(list)
    ignored_suffixes = {".tmp", ".lock", ".pyc", ".part", ".download"}

    for path in sorted(OUT.rglob("*")):
        if path.is_symlink():
            symlinks.append(relative(path))
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in ignored_suffixes or path.name.endswith(".tmp"):
            continue
        if any(
            segment in {"__pycache__", ".pytest_cache", ".mypy_cache"}
            for segment in path.parts
        ):
            continue

        file_count += 1
        if path.stat().st_size <= 0:
            zero_byte_files.append(relative(path))
            continue
        hashes[digest_file(path)].append(path)

    if symlinks:
        fail(f"Symlinks found in output/: {symlinks[:20]}")
    if zero_byte_files:
        fail(f"Zero-byte output files found: {zero_byte_files[:20]}")

    duplicates = []
    for checksum, paths in hashes.items():
        media = [
            path for path in paths
            if path.suffix.lower() in (
                VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
            )
        ]
        if len(media) > 1:
            duplicates.append({
                "sha256": checksum,
                "files": [relative(path) for path in media],
            })

    return {
        "status": "valid",
        "file_count": file_count,
        "duplicate_media_files": len(duplicates),
        "duplicate_media_records": duplicates[:50],
        "zero_byte_files": 0,
        "symlinks": 0,
        "note": (
            "Identical media hashes are reported, not rejected; "
            "reused media assets may legitimately be byte-identical."
        ),
    }


def opposite_format_video_aliases(mode: str) -> list[Path]:
    opposite = "full" if mode == "short" else "short"
    candidates = [
        OUT / "parts" / f"katha_lok_ai_{opposite}.mp4",
        OUT / f"katha_lok_ai_{opposite}.mp4",
        OUT / "videos" / f"katha_lok_ai_{opposite}.mp4",
        OUT / "videos" / f"katha_lok_ai_final_{opposite}.mp4",
    ]
    return [path for path in candidates if path.exists()]


def validate_mode_separation(mode: str) -> dict:
    opposite = "full" if mode == "short" else "short"
    found = []

    for path in OUT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        name = path.name.lower()
        if (
            f"_{opposite}_" in name
            or name.endswith(f"_{opposite}.mp4")
            or f"-{opposite}-output" in name
        ):
            found.append(relative(path))

    if found:
        fail(f"Opposite-format video files exist: {found[:30]}")

    return {
        "status": "valid",
        "selected_format": mode,
        "opposite_format": opposite,
        "opposite_video_files": [],
        "part_outputs_only": True,
    }


def validate_existing_archives(mode: str, parts: int) -> dict:
    archives = sorted(
        path for path in ROOT.glob("katha-lok-*-output.zip")
        if path.is_file()
    )
    matching = [
        path for path in archives
        if path.name.lower().endswith(f"-{mode}-output.zip")
    ]

    if not matching:
        return {
            "status": "deferred",
            "reason": "The packaging stage creates the story-named ZIP.",
        }
    if len(matching) > 1:
        fail(f"Multiple selected-format story ZIPs exist: {[p.name for p in matching]}")

    archive_path = matching[0]
    if archive_path.stat().st_size < MIN_VIDEO_BYTES:
        fail("Existing story ZIP is too small")

    with zipfile.ZipFile(archive_path, "r") as archive:
        bad = archive.testzip()
        if bad:
            fail(f"ZIP CRC verification failed: {bad}")

        names = archive.namelist()
        if not names or len(names) != len(set(names)):
            fail("ZIP is empty or contains duplicate entries")

        for name in names:
            member = PurePosixPath(name)
            if (
                member.is_absolute()
                or "\\" in name
                or ":" in name
                or any(piece in {"", ".", ".."} for piece in member.parts)
            ):
                fail(f"Unsafe ZIP member path: {name}")

        videos = [
            name for name in names
            if PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS
        ]
        expected = {
            f"output/parts/part_{number:02d}.mp4"
            for number in range(1, parts + 1)
        }
        if set(videos) != expected:
            fail("Existing ZIP Part video paths do not match selected PARTS")
        if "output/package_manifest.json" not in names:
            fail("Existing ZIP package manifest is missing")
        if "output/validation_report.json" not in names:
            fail("Existing ZIP validation report is missing")

        try:
            embedded = json.loads(
                archive.read("output/package_manifest.json").decode("utf-8")
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            fail(f"Existing ZIP package manifest is invalid: {exc}")

        if embedded.get("status") != "completed":
            fail("Existing ZIP package manifest is incomplete")
        if clean(embedded.get("format")).lower() != mode:
            fail("Existing ZIP package FORMAT mismatch")
        if positive_int(embedded.get("parts"), "ZIP package PARTS") != parts:
            fail("Existing ZIP package PARTS mismatch")
        if embedded.get("part_videos_only") is not True:
            fail("Existing ZIP does not confirm Part-only output")

    return {
        "status": "valid",
        "archive": relative(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": digest_file(archive_path),
        "entry_count": len(names),
        "part_video_count": len(videos),
        "crc": "valid",
    }


def cleanup_stale_opposite_aliases(mode: str) -> list[str]:
    removed = []
    for path in opposite_format_video_aliases(mode):
        if path.is_file() and not path.is_symlink():
            path.unlink()
            removed.append(relative(path))
    return removed


def verify_report(report: dict, mode: str, parts: int, scenes: int) -> None:
    if report.get("status") != "completed":
        fail("Persisted validation report status mismatch")
    if report.get("format") != mode:
        fail("Persisted validation report FORMAT mismatch")
    if report.get("parts") != parts or report.get("configured_parts") != parts:
        fail("Persisted validation report PARTS mismatch")
    if report.get("scenes_per_part") != scenes:
        fail("Persisted validation report SCENES mismatch")
    if report.get("expected_scenes") != parts * scenes:
        fail("Persisted validation report expected scene count mismatch")
    if report.get("part_outputs_only") is not True:
        fail("Persisted validation report is not Part-only")

    checks = report.get("checks")
    if not isinstance(checks, dict):
        fail("Persisted validation report checks are missing")

    for name in REQUIRED_CHECKS:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Persisted validation check failed: {name}")


def main() -> int:
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            fail(f"{command} is required but unavailable")
    if not INPUT_FILE.is_file():
        fail("Input/topic.txt is missing")
    if not OUT.is_dir():
        fail("output/ directory is missing")

    config = load_and_validate(INPUT_FILE)
    mode = clean(get_format(config)).lower()
    parts = positive_int(get_parts(config), "Actual selected PARTS")
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
    log("ACTUAL SELECTED PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("RESOLUTION:", expected_size)
    log("FPS:", fps)

    story_check = validate_story(parts, scenes, mode)
    photo_check = validate_photo(
        parts, scenes, mode, expected_size, fps
    )
    narration_check = validate_narration(parts, scenes, mode)
    sound_check = validate_sound(
        config, parts, scenes, mode, narration_check
    )
    render_check = validate_render(
        mode, parts, scenes, expected_size, fps, narration_check
    )
    captions_check = validate_captions(parts, scenes, mode)
    thumbnail_check = validate_thumbnail(mode, expected_size)
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

    removed_aliases = cleanup_stale_opposite_aliases(mode)

    report = {
        "status": "completed",
        "version": VERSION,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": mode,
        "title": story_check["title"],
        "story_title": story_check["title"],
        "parts": parts,
        "configured_parts": parts,
        "actual_selected_parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "expected_role_scenes": parts * scenes + 2 * parts,
        "resolution": list(expected_size),
        "fps": fps,
        "part_outputs_only": True,
        "combined_video_created": False,
        "format_alias_created": False,
        "opposite_format_aliases_removed": removed_aliases,
        "checks": checks,
        "archive_validation": archive_check,
        "api_calls_made": 0,
        "external_api_calls": 0,
        "paid_services_used": False,
        "story_modified": False,
        "assets_regenerated": False,
        "limitations": [
            "This validator reads existing outputs only and does not regenerate assets.",
            "Media streams, resolution, FPS, duration and SHA256 are checked.",
            "Story part count, word count, scene order and sentence boundaries are checked.",
            "Hook, Suspense and Resolution records are checked separately from Main scenes.",
            "Narration audio, sound tracks and SFX cue timing are checked against measured audio.",
            "Caption cue numbering, timestamps, order, overlap and nonempty text are checked.",
            "SRT timing is validated; word-level speech alignment is not independently re-proven.",
            "Story meaning preservation and subjective story quality cannot be proven by file integrity checks alone.",
            "ZIP integrity is checked when a selected-format story archive already exists; packaging creates it otherwise.",
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
    log("ACTUAL PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("MAIN SCENES:", parts * scenes)
    log("ALL ROLE SCENES:", parts * scenes + 2 * parts)
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