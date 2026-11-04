#!/usr/bin/env python3
"""Strict final validation for variable-scene Photo Motion outputs."""

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
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
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

STORY_FILES = (
    OUT / "story" / "story.json",
    OUT / "story" / "ai_story.json",
)
STORY_VERIFICATION = OUT / "story" / "story_verification.json"
PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX = OUT / "sound_design" / "sfx_cues.json"
SFX_VALIDATION = OUT / "sound_design" / "sfx_timing_validation.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
CAPTIONS = OUT / "captions" / "captions.json"
SRT = OUT / "captions" / "final_captions.srt"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA_TEXT = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
METADATA_TAGS = OUT / "publish" / "youtube_tags.txt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v94-variable-scenes-role-aware"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
FPS_TOLERANCE = 0.05
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120

ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}
SUCCESS = {"completed", "complete", "success", "done", "reused", "valid"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


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
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Path escapes repository: {resolved}") from exc


def read_json(path: Path, optional: bool = False) -> Any:
    if not path.is_file():
        if optional:
            return None
        fail(f"Required JSON missing: {relative(path)}")
    if path.stat().st_size <= 0:
        fail(f"Required JSON is empty: {relative(path)}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
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


def sha256_file(path: Path) -> str:
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

    try:
        resolved.relative_to(allowed)
    except ValueError:
        fail(f"{label}: path escapes allowed directory")

    if candidate.is_symlink() or not resolved.is_file():
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
        streams = data["streams"]
    except Exception as exc:
        fail(f"Invalid media probe for {path.name}: {exc}")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {path.name}")
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
    dimensions: tuple[int, int],
    fps: float,
    label: str,
    require_audio: bool = True,
) -> dict:
    info = media_info(path)
    if not info["video"]:
        fail(f"{label}: video stream is missing")
    if require_audio and not info["audio"]:
        fail(f"{label}: audio stream is missing")
    if (info["width"], info["height"]) != dimensions:
        fail(
            f"{label}: expected {dimensions[0]}x{dimensions[1]}, "
            f"found {info['width']}x{info['height']}"
        )
    if info["fps"] is None or abs(info["fps"] - fps) > FPS_TOLERANCE:
        fail(f"{label}: FPS mismatch: {info['fps']}")
    return info


def rows_from(data: Any, *keys: str) -> list:
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


def normalize_role(row: dict) -> str:
    value = clean(
        row.get("role") or row.get("kind")
        or row.get("scene_type") or "main"
    ).lower()

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
    if value not in ROLE_ORDER:
        fail(f"Unknown scene role: {value!r}")
    return value


def scene_key(row: dict, label: str) -> tuple[int, str, int | None]:
    part = positive_int(row.get("part", row.get("part_number")), f"{label} Part")
    role = normalize_role(row)

    if role == "main":
        scene = positive_int(
            row.get("local_scene") or row.get("scene"),
            f"{label} Main scene",
        )
        return part, role, scene

    return part, role, None


def canonical_scene_id(key: tuple[int, str, int | None]) -> str:
    part, role, scene = key
    if role == "main":
        return f"part_{part:02d}_scene_{scene:02d}"
    return f"part_{part:02d}_{role}"


def expected_keys(
    part_numbers: list[int],
    scene_counts: dict[int, int],
    main_only: bool = False,
) -> set:
    result = set()
    for index, part in enumerate(part_numbers):
        if not main_only:
            result.add((part, "hook", None))

        for scene in range(1, scene_counts[part] + 1):
            result.add((part, "main", scene))

        if not main_only:
            if index < len(part_numbers) - 1:
                result.add((part, "suspense", None))
            else:
                result.add((part, "resolution", None))
    return result


def validate_role_rows(
    rows: Any,
    part_numbers: list[int],
    scene_counts: dict[int, int],
    label: str,
    main_only: bool = False,
) -> dict:
    if not isinstance(rows, list):
        fail(f"{label}: scene records are missing")

    expected = expected_keys(part_numbers, scene_counts, main_only)
    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid row {index}")

        key = scene_key(row, label)
        if key not in expected:
            fail(f"{label}: unexpected role or scene number: {key}")
        if key in mapping:
            fail(f"{label}: duplicate scene record: {key}")

        declared_id = clean(row.get("scene_id") or row.get("job_id"))
        expected_id = canonical_scene_id(key)
        if declared_id and declared_id != expected_id:
            fail(f"{label}: scene ID mismatch: {declared_id}")

        mapping[key] = row

    missing = expected - set(mapping)
    if missing:
        fail(f"{label}: missing records: {sorted(missing, key=str)}")

    return mapping


def load_story() -> tuple[dict, Path]:
    for path in STORY_FILES:
        data = read_json(path, optional=True)
        if isinstance(data, dict):
            return data, path
    fail("Completed story JSON is missing")


def nested_story_objects(data: dict) -> list[dict]:
    result = [data]
    for key in ("story", "result", "data"):
        item = data.get(key)
        if isinstance(item, dict) and item not in result:
            result.append(item)
    return result


def story_title(data: dict) -> str:
    for obj in nested_story_objects(data):
        for key in ("final_title", "generated_title", "story_title", "title"):
            value = clean(obj.get(key))
            if value:
                return value
    fail("Story-derived title is missing")


def story_parts(data: dict) -> list:
    value = data.get("parts")
    if not isinstance(value, list):
        nested = data.get("story")
        value = nested.get("parts") if isinstance(nested, dict) else None
    if not isinstance(value, list):
        fail("Story Part records are missing")
    return value


def actual_counts_from_story(
    data: dict,
    part_numbers: list[int],
    minimum: int,
    maximum: int,
) -> dict[int, int]:
    raw = data.get("actual_scene_counts")
    if not isinstance(raw, dict):
        fail("Story actual_scene_counts is missing")

    result = {}
    for part in part_numbers:
        candidates = (str(part), f"part_{part:02d}", part)
        value = next((raw[key] for key in candidates if key in raw), None)
        count = positive_int(value, f"Story Part {part} scene count")
        if not minimum <= count <= maximum:
            fail(
                f"Part {part} has {count} scenes; "
                f"allowed SCENES range is {minimum}-{maximum}"
            )
        result[part] = count
    return result


def validate_story(
    data: dict,
    story_path: Path,
    part_numbers: list[int],
    scene_counts: dict[int, int],
    mode: str,
) -> dict:
    statuses = [
        clean(obj.get("status")).lower()
        for obj in nested_story_objects(data)
        if obj.get("status") is not None
    ]
    if statuses and not any(status in SUCCESS for status in statuses):
        fail(f"Story status is incomplete: {statuses}")

    title = story_title(data)
    parts = story_parts(data)

    if len(parts) != len(part_numbers):
        fail("Story Part count differs from actual selected PARTS")

    words_per_part = []
    all_main_scenes = []
    full_text_parts = []

    for index, (part_number, part) in enumerate(
        zip(part_numbers, parts), 1
    ):
        if not isinstance(part, dict):
            fail(f"Invalid story Part {part_number}")

        actual_part = positive_int(
            part.get("part", part_number),
            f"Story Part {part_number}",
        )
        if actual_part != part_number:
            fail("Story Part order or numbering mismatch")

        narration = clean(part.get("narration") or part.get("text"))
        if not narration:
            fail(f"Story narration missing in Part {part_number}")

        word_count = len(re.findall(r"\S+", narration))
        declared_words = part.get("word_count")
        if declared_words is not None:
            if positive_int(declared_words, "Story word_count") != word_count:
                fail(f"Story word_count mismatch in Part {part_number}")

        if mode == "short":
            final_part = index == len(part_numbers) - 1
            if final_part:
                if not 120 <= word_count <= 239:
                    fail(
                        f"Final SHORT Part has {word_count} words; "
                        "allowed final-part range is 120–239"
                    )
            elif not 120 <= word_count <= 180:
                fail(
                    f"SHORT Part {part_number} has {word_count} words; "
                    "allowed range is 120–180"
                )

        if narration[-1] not in "।॥.!?…,:;—–":
            fail(
                f"Part {part_number} does not end at a complete "
                "sentence/clause boundary"
            )

        scenes = part.get("scenes")
        expected_count = scene_counts[part_number]
        if not isinstance(scenes, list) or len(scenes) != expected_count:
            fail(
                f"Part {part_number} has an incorrect Main scene count"
            )

        scene_text = []
        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                fail(f"Invalid Main scene in Part {part_number}")

            sid = f"part_{part_number:02d}_scene_{scene_number:02d}"
            if clean(scene.get("scene_id")) != sid:
                fail(f"Story scene ID mismatch: {sid}")
            if positive_int(scene.get("part"), "Story scene Part") != part_number:
                fail(f"Story scene Part mismatch: {sid}")
            if positive_int(scene.get("scene"), "Story scene number") != scene_number:
                fail(f"Story scene number mismatch: {sid}")
            if clean(scene.get("role")).lower() != "main":
                fail(f"Story role mismatch: {sid}")
            if not clean(scene.get("narration")):
                fail(f"Scene narration missing: {sid}")
            if not clean(scene.get("visual_prompt") or scene.get("visual")):
                fail(f"Scene visual prompt missing: {sid}")

            scene_text.append(clean(scene["narration"]))
            all_main_scenes.append(scene)

        if clean(" ".join(scene_text)) != narration:
            fail(f"Part {part_number} scene narration differs from Part narration")

        words_per_part.append(word_count)
        full_text_parts.append(narration)

    declared_counts = data.get("actual_scene_counts")
    if not isinstance(declared_counts, dict):
        fail("Story actual_scene_counts is missing")

    if isinstance(data.get("scenes"), list):
        if data["scenes"] != all_main_scenes:
            fail("Top-level story scenes differ from ordered Part scenes")

    total_words = len(re.findall(r"\S+", " ".join(full_text_parts)))
    if data.get("total_parts") is not None:
        if positive_int(data["total_parts"], "Story total_parts") != len(parts):
            fail("Story total_parts mismatch")
    if data.get("total_main_scenes") is not None:
        if positive_int(data["total_main_scenes"], "Story total_main_scenes") != sum(scene_counts.values()):
            fail("Story total_main_scenes mismatch")
    if data.get("total_story_words") is not None:
        if positive_int(data["total_story_words"], "Story total_story_words") != total_words:
            fail("Story total_story_words mismatch")

    verification = read_json(STORY_VERIFICATION, optional=True)
    if verification is not None:
        if clean(verification.get("status")).lower() not in SUCCESS:
            fail("Story verification report is incomplete")
        fmt = clean(verification.get("format")).lower()
        if fmt and fmt != mode:
            fail("Story verification FORMAT mismatch")
        summary = verification.get("summary")
        if isinstance(summary, dict) and summary.get("selected_parts") is not None:
            if positive_int(summary["selected_parts"], "Verified PARTS") != len(parts):
                fail("Story verification PARTS mismatch")

    return {
        "status": "passed",
        "title": title,
        "parts": len(parts),
        "actual_scene_counts": {
            str(part): scene_counts[part] for part in part_numbers
        },
        "total_main_scenes": sum(scene_counts.values()),
        "words_per_part": words_per_part,
        "total_words": total_words,
        "story_file": relative(story_path),
        "story_sha256": sha256_file(story_path),
    }

def validate_artifact_rows(
    data: dict,
    part_numbers: list[int],
    scene_counts: dict[int, int],
    label: str,
    dimensions: tuple[int, int] | None = None,
    fps: float | None = None,
    media_type: str = "video",
    require_audio: bool = False,
) -> dict:
    rows = rows_from(
        data,
        "jobs", "audio_jobs", "scenes", "records", "items",
        "visual_jobs", "photo_motion_jobs",
    )
    mapping = validate_role_rows(
        rows, part_numbers, scene_counts, label
    )

    verified = []
    for key in sorted(
        mapping,
        key=lambda item: (
            item[0], ROLE_ORDER[item[1]], item[2] or 0
        ),
    ):
        row = mapping[key]
        sid = canonical_scene_id(key)

        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"{label} record is not completed: {sid}")

        if media_type == "video":
            path_value = first_path(
                row, "video", "source_video", "output", "path"
            )
            path = safe_file(path_value, f"{label} {sid}", MIN_VIDEO_BYTES)
            info = validate_video(
                path,
                dimensions,
                fps,
                f"{label} {sid}",
                require_audio=require_audio,
            )
        else:
            path_value = first_path(
                row, "audio", "audio_path", "output", "path", "file"
            )
            path = safe_file(path_value, f"{label} {sid}", MIN_AUDIO_BYTES)
            info = media_info(path)
            if not info["audio"]:
                fail(f"{label} {sid}: audio stream is missing")
            if info["duration"] < 0.25:
                fail(f"{label} {sid}: audio duration is too short")

            declared_duration = row.get(
                "actual_audio_duration_seconds",
                row.get("audio_duration", row.get("duration")),
            )
            if declared_duration is not None:
                try:
                    expected_duration = float(declared_duration)
                except (TypeError, ValueError):
                    fail(f"{label} {sid}: invalid declared duration")
                if (
                    not math.isfinite(expected_duration)
                    or abs(expected_duration - info["duration"]) > 0.2
                ):
                    fail(f"{label} {sid}: measured duration mismatch")

        digest = sha256_file(path)
        declared_hash = (
            row.get("sha256")
            or row.get("video_sha256")
            or row.get("audio_sha256")
        )
        if declared_hash and clean(declared_hash).lower() != digest:
            fail(f"{label} {sid}: SHA-256 mismatch")

        duration_source = row.get(
            "duration_source", row.get("audio_duration_source")
        )
        if media_type == "audio" and duration_source:
            if duration_source != "ffprobe_measured":
                fail(f"{label} {sid}: duration is not ffprobe-measured")

        verified.append({
            "scene_id": sid,
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            "path": relative(path),
            "duration": round(info["duration"], 4),
            "sha256": digest,
        })

    return {
        "status": "passed",
        "record_count": len(verified),
        "records": verified,
    }


def validate_photo_motion(
    part_numbers: list[int],
    scene_counts: dict[int, int],
    mode: str,
    dimensions: tuple[int, int],
    fps: float,
) -> dict:
    data = read_json(PHOTO)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Photo Motion manifest is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Photo Motion FORMAT mismatch")

    width = positive_int(data.get("width"), "Photo Motion width")
    height = positive_int(data.get("height"), "Photo Motion height")
    actual_fps = parse_fps(data.get("fps"), "Photo Motion FPS")

    if (width, height) != dimensions:
        fail("Photo Motion dimensions do not match selected FORMAT")
    if abs(actual_fps - fps) > FPS_TOLERANCE:
        fail("Photo Motion FPS does not match Input/topic.txt")

    declared_counts = data.get("actual_scene_counts")
    if not isinstance(declared_counts, dict):
        fail("Photo Motion actual_scene_counts is missing")

    for part in part_numbers:
        value = declared_counts.get(
            str(part),
            declared_counts.get(f"part_{part:02d}", declared_counts.get(part)),
        )
        if positive_int(value, f"Photo Motion Part {part} scene count") != scene_counts[part]:
            fail(f"Photo Motion Part {part} actual scene count mismatch")

    result = validate_artifact_rows(
        data,
        part_numbers,
        scene_counts,
        "Photo Motion",
        dimensions=dimensions,
        fps=fps,
        media_type="video",
        require_audio=False,
    )

    expected_main = sum(scene_counts.values())
    if result["record_count"] != (
        expected_main + len(part_numbers) * 2
    ):
        fail("Photo Motion role clip count mismatch")

    return {
        "status": "passed",
        "format": mode,
        "actual_scene_counts": {
            str(part): scene_counts[part] for part in part_numbers
        },
        "main_scene_count": expected_main,
        "role_clip_count": result["record_count"],
        "records": result["records"],
        "manifest_sha256": sha256_file(PHOTO),
    }


def validate_narration(
    part_numbers: list[int],
    scene_counts: dict[int, int],
) -> dict:
    data = read_json(AUDIO)
    if isinstance(data, dict) and data.get("status") is not None:
        if clean(data["status"]).lower() not in SUCCESS:
            fail("Narration manifest is not completed")

    result = validate_artifact_rows(
        data,
        part_numbers,
        scene_counts,
        "Narration",
        media_type="audio",
    )

    expected_count = (
        sum(scene_counts.values()) + len(part_numbers) * 2
    )
    if result["record_count"] != expected_count:
        fail(
            f"Narration record count mismatch: "
            f"{result['record_count']} != {expected_count}"
        )

    return {
        "status": "passed",
        "record_count": result["record_count"],
        "records": result["records"],
        "manifest_sha256": sha256_file(AUDIO),
    }


def validate_sound_design(
    part_numbers: list[int],
    scene_counts: dict[int, int],
    mode: str,
) -> dict:
    data = read_json(SOUND)
    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Sound-design manifest is not completed")

    if clean(data.get("format")).lower() != mode:
        fail("Sound-design FORMAT mismatch")

    validation = read_json(SFX_VALIDATION)
    if clean(validation.get("status")).lower() not in SUCCESS:
        fail("SFX timing validation is not completed")

    for key in (
        "errors", "failed_checks", "invalid_cues",
        "out_of_range_cues", "timing_errors",
    ):
        value = validation.get(key)
        if isinstance(value, list) and value:
            fail(f"SFX timing validation contains {key}: {value[:5]}")
        if isinstance(value, (int, float)) and value != 0:
            fail(f"SFX timing validation reports {key}={value}")

    if clean(validation.get("format")).lower() not in {"", mode}:
        fail("SFX timing validation FORMAT mismatch")

    tracks = data.get("tracks", {})
    if tracks is not None and not isinstance(tracks, (dict, list)):
        fail("Sound-design tracks must be an object or list")

    verified_tracks = []
    track_rows = []

    if isinstance(tracks, dict):
        for category, record in tracks.items():
            if record is None:
                continue
            if not isinstance(record, dict):
                fail(f"Invalid sound track record: {category}")
            status = clean(record.get("status")).lower()
            if status and status not in SUCCESS:
                fail(f"Sound track is incomplete: {category}")

            value = first_path(
                record, "path", "audio_path", "audio", "output", "file"
            )
            if not value:
                continue

            path = safe_file(value, f"Sound track {category}", MIN_AUDIO_BYTES)
            info = media_info(path)
            if not info["audio"]:
                fail(f"Sound track {category} has no audio stream")

            digest = sha256_file(path)
            declared = record.get("sha256") or record.get("audio_sha256")
            if declared and clean(declared).lower() != digest:
                fail(f"Sound track {category} SHA-256 mismatch")

            verified_tracks.append({
                "category": category,
                "path": relative(path),
                "duration": round(info["duration"], 4),
                "sha256": digest,
            })

    elif isinstance(tracks, list):
        track_rows = tracks

    if track_rows:
        for index, row in enumerate(track_rows, 1):
            if not isinstance(row, dict):
                fail(f"Invalid sound track row {index}")
            value = first_path(
                row, "path", "audio_path", "audio", "output", "file"
            )
            if not value:
                continue
            path = safe_file(
                value, f"Sound track {index}", MIN_AUDIO_BYTES
            )
            info = media_info(path)
            if not info["audio"]:
                fail(f"Sound track {index} has no audio stream")
            digest = sha256_file(path)
            declared = row.get("sha256") or row.get("audio_sha256")
            if declared and clean(declared).lower() != digest:
                fail(f"Sound track {index} SHA-256 mismatch")
            verified_tracks.append({
                "category": clean(
                    row.get("category") or row.get("role") or row.get("type")
                ),
                "path": relative(path),
                "duration": round(info["duration"], 4),
                "sha256": digest,
            })

    cue_rows = rows_from(
        validation, "cues", "validated_cues", "sfx_cues", "records"
    )
    if not cue_rows:
        cue_data = read_json(SFX, optional=True)
        if isinstance(cue_data, dict):
            cue_rows = rows_from(
                cue_data, "cues", "sfx_cues", "jobs", "records"
            )
        elif isinstance(cue_data, list):
            cue_rows = cue_data

    cue_count = 0
    for index, cue in enumerate(cue_rows, 1):
        if not isinstance(cue, dict):
            fail(f"Invalid SFX cue {index}")

        sid = clean(cue.get("scene_id"))
        if sid:
            valid_ids = {
                canonical_scene_id(key)
                for key in expected_keys(part_numbers, scene_counts)
            }
            if sid not in valid_ids:
                fail(f"SFX cue references unknown scene: {sid}")

        value = first_path(
            cue, "path", "audio_path", "audio", "file", "output"
        )
        if value:
            path = safe_file(
                value, f"SFX cue {index}", MIN_AUDIO_BYTES
            )
            info = media_info(path)
            if not info["audio"]:
                fail(f"SFX cue {index} has no audio stream")
            digest = sha256_file(path)
            declared = cue.get("sha256") or cue.get("audio_sha256")
            if declared and clean(declared).lower() != digest:
                fail(f"SFX cue {index} SHA-256 mismatch")

        cue_count += 1

    return {
        "status": "passed",
        "tracks_verified": len(verified_tracks),
        "sfx_cues_verified": cue_count,
        "sfx_timing_validation": "passed",
        "tracks": verified_tracks,
        "sound_manifest_sha256": sha256_file(SOUND),
        "sfx_validation_sha256": sha256_file(SFX_VALIDATION),
    }


def parse_srt(path: Path) -> list[dict]:
    content = path.read_text(encoding="utf-8-sig")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())
    rows = []
    previous_end = -1.0

    def timestamp(value: str) -> float:
        match = re.fullmatch(
            r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})",
            value.strip(),
        )
        if not match:
            fail(f"Invalid SRT timestamp: {value}")
        hours, minutes, seconds, millis = map(int, match.groups())
        return hours * 3600 + minutes * 60 + seconds + millis / 1000

    for index, block in enumerate(blocks, 1):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 3:
            fail(f"SRT block {index} is incomplete")
        if not lines[0].isdigit() or int(lines[0]) != index:
            fail(f"SRT subtitle index {index} is invalid")

        timing = re.fullmatch(r"(.+?)\s*-->\s*(.+?)", lines[1])
        if not timing:
            fail(f"SRT timing line {index} is invalid")

        start = timestamp(timing.group(1))
        end = timestamp(timing.group(2))
        if end <= start:
            fail(f"SRT subtitle {index} has invalid duration")
        if start < previous_end - 0.02:
            fail(f"SRT subtitle {index} overlaps its predecessor")

        caption = " ".join(lines[2:]).strip()
        if not caption:
            fail(f"SRT subtitle {index} is empty")

        rows.append({
            "index": index,
            "start": start,
            "end": end,
            "text": caption,
        })
        previous_end = end

    if not rows:
        fail("Final SRT contains no subtitles")
    return rows


def validate_captions(part_numbers: list[int]) -> dict:
    data = read_json(CAPTIONS)
    srt_path = safe_file(SRT, "Final captions SRT", 10)
    srt_rows = parse_srt(srt_path)

    if isinstance(data, dict):
        status = clean(data.get("status")).lower()
        if status and status not in SUCCESS:
            fail("Caption manifest is not completed")

        declared_parts = data.get("parts")
        if isinstance(declared_parts, list):
            if len(declared_parts) != len(part_numbers):
                fail("Caption manifest Part count mismatch")

        declared_count = (
            data.get("caption_count")
            or data.get("total_captions")
            or data.get("subtitle_count")
        )
        if declared_count is not None:
            if positive_int(declared_count, "Caption count") != len(srt_rows):
                fail("Caption manifest and SRT subtitle counts differ")

    return {
        "status": "passed",
        "parts": len(part_numbers),
        "subtitle_count": len(srt_rows),
        "first_subtitle": srt_rows[0]["start"],
        "last_subtitle": srt_rows[-1]["end"],
        "srt_sha256": sha256_file(srt_path),
        "manifest_sha256": sha256_file(CAPTIONS),
    }


def validate_render(
    part_numbers: list[int],
    scene_counts: dict[int, int],
    mode: str,
    dimensions: tuple[int, int],
    fps: float,
) -> dict:
    data = read_json(RENDER)

    if clean(data.get("status")).lower() not in SUCCESS:
        fail("Render manifest is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Render manifest FORMAT mismatch")
    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions")

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != len(part_numbers):
        fail("Render manifest Part output count mismatch")

    verified_parts = []
    for index, (part, row) in enumerate(
        zip(part_numbers, part_rows), 1
    ):
        if not isinstance(row, dict):
            fail(f"Invalid rendered Part record {index}")
        if positive_int(row.get("part"), "Rendered Part") != part:
            fail("Rendered Part order mismatch")
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered Part {part} is incomplete")

        value = first_path(row, "path", "output", "video", "file")
        path = safe_file(value, f"Rendered Part {part}", MIN_VIDEO_BYTES)
        info = validate_video(
            path, dimensions, fps, f"Rendered Part {part}", True
        )
        digest = sha256_file(path)
        declared = row.get("sha256") or row.get("video_sha256")
        if declared and clean(declared).lower() != digest:
            fail(f"Rendered Part {part} SHA-256 mismatch")

        verified_parts.append({
            "part": part,
            "path": relative(path),
            "duration": round(info["duration"], 4),
            "sha256": digest,
        })

    scene_rows = (
        data.get("all_completed_scenes")
        or data.get("completed_scenes")
        or data.get("scenes")
    )
    mapping = validate_role_rows(
        scene_rows,
        part_numbers,
        scene_counts,
        "Rendered scene",
    )

    for key, row in mapping.items():
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered scene is incomplete: {canonical_scene_id(key)}")
        value = first_path(row, "video", "output", "path")
        path = safe_file(
            value, f"Rendered scene {canonical_scene_id(key)}",
            MIN_VIDEO_BYTES,
        )
        info = validate_video(
            path, dimensions, fps,
            f"Rendered scene {canonical_scene_id(key)}",
            require_audio=True,
        )
        digest = sha256_file(path)
        declared = row.get("sha256") or row.get("video_sha256")
        if declared and clean(declared).lower() != digest:
            fail(f"Rendered scene SHA-256 mismatch: {canonical_scene_id(key)}")

        declared_duration = row.get("duration")
        if declared_duration is not None:
            try:
                expected_duration = float(declared_duration)
            except (TypeError, ValueError):
                fail(f"Invalid duration: {canonical_scene_id(key)}")
            if abs(expected_duration - info["duration"]) > DURATION_TOLERANCE:
                fail(f"Rendered scene duration mismatch: {canonical_scene_id(key)}")

    role_counts = {
        role: sum(1 for key in mapping if key[1] == role)
        for role in ROLE_ORDER
    }

    expected_main = sum(scene_counts.values())
    if role_counts["main"] != expected_main:
        fail("Rendered Main scene count mismatch")
    if role_counts["hook"] != len(part_numbers):
        fail("Rendered Hook count mismatch")
    if role_counts["suspense"] != max(0, len(part_numbers) - 1):
        fail("Rendered Suspense count mismatch")
    if role_counts["resolution"] != 1:
        fail("Rendered Resolution count mismatch")

    return {
        "status": "passed",
        "parts": verified_parts,
        "main_scene_count": role_counts["main"],
        "role_counts": role_counts,
        "all_role_clip_count": len(mapping),
        "captions_burned": True,
        "part_outputs_only": data.get("part_outputs_only"),
        "manifest_sha256": sha256_file(RENDER),
    }


def validate_thumbnail(
    mode: str,
    dimensions: tuple[int, int],
) -> dict:
    path = safe_file(THUMBNAIL, "Story thumbnail", MIN_IMAGE_BYTES)

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
        fail("Thumbnail must be JPEG")
    if (width, height) != dimensions:
        fail(
            f"Thumbnail dimensions mismatch: {width}x{height}; "
            f"expected {dimensions[0]}x{dimensions[1]}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST)
    if clean(manifest.get("status")).lower() not in SUCCESS:
        fail("Thumbnail manifest is not completed")
    if clean(manifest.get("format")).lower() not in {"", mode}:
        fail("Thumbnail manifest FORMAT mismatch")

    declared_path = first_path(
        manifest, "path", "output", "thumbnail", "file"
    )
    if declared_path:
        declared = safe_file(declared_path, "Thumbnail manifest path")
        if declared != path:
            fail("Thumbnail manifest points to a different thumbnail")

    return {
        "status": "passed",
        "path": relative(path),
        "dimensions": [width, height],
        "format": image_format,
        "sha256": sha256_file(path),
    }


def validate_metadata(mode: str, title: str) -> dict:
    text_path = safe_file(METADATA_TEXT, "YouTube metadata text", 10)
    json_path = safe_file(METADATA_JSON, "YouTube metadata JSON", 2)
    tags_path = safe_file(METADATA_TAGS, "YouTube tags", 2)

    metadata = read_json(json_path)
    text_content = text_path.read_text(encoding="utf-8-sig")
    tags_content = tags_path.read_text(encoding="utf-8-sig")

    metadata_title = clean(
        metadata.get("title") or metadata.get("video_title")
    )
    if not metadata_title:
        fail("YouTube metadata title is missing")
    if metadata_title != title:
        fail("YouTube metadata title does not match story title")

    metadata_format = clean(
        metadata.get("format") or metadata.get("video_mode")
    ).lower()
    if metadata_format and metadata_format != mode:
        fail("YouTube metadata FORMAT mismatch")

    if title.casefold() not in (
        text_content + "\n" + tags_content
    ).casefold():
        fail("YouTube metadata text/tags do not identify the story title")

    description = clean(
        metadata.get("description") or metadata.get("video_description")
    )
    if not description and len(text_content.strip()) < 20:
        fail("YouTube description/metadata text is too short")

    return {
        "status": "passed",
        "title": title,
        "format": mode,
        "files": [
            relative(text_path),
            relative(json_path),
            relative(tags_path),
        ],
    }


def validate_mode_separation(
    mode: str,
    dimensions: tuple[int, int],
) -> dict:
    checked = []
    allowed_roots = (
        OUT / "parts",
        OUT / "scene_renders",
        OUT / "videos",
    )

    for base in allowed_roots:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.name.endswith((".tmp.mp4", ".rendering.mp4", ".joining.mp4")):
                fail(f"Temporary video remains in output: {relative(path)}")

            info = validate_video(
                path,
                dimensions,
                None,
                f"Mode separation {relative(path)}",
                require_audio=True,
            )
            checked.append({
                "path": relative(path),
                "dimensions": [info["width"], info["height"]],
                "duration": round(info["duration"], 4),
            })

    if not checked:
        fail("No final/scene video files found for mode validation")

    return {
        "status": "passed",
        "format": mode,
        "dimensions": list(dimensions),
        "video_files_checked": len(checked),
        "videos": checked,
    }


def validate_unique_output_files() -> dict:
    checked = []
    for base in (OUT / "parts", OUT / "scene_renders"):
        if not base.exists():
            continue
        seen = set()
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                fail(f"Duplicate output file reference: {relative(path)}")
            seen.add(resolved)
            if path.is_symlink():
                fail(f"Symlink found in output artifacts: {relative(path)}")
            checked.append(relative(path))

    return {
        "status": "passed",
        "files_checked": len(checked),
    }


def validate_package(
    part_numbers: list[int],
    mode: str,
    title: str,
) -> dict:
    archive_name_file = OUT / "package_archive_name.txt"
    report_file = OUT / "package_manifest.json"

    if not archive_name_file.is_file() or not report_file.is_file():
        return {
            "status": "pending",
            "reason": "Package is generated by the next workflow step.",
        }

    archive_name = archive_name_file.read_text(
        encoding="utf-8-sig"
    ).strip()
    if not archive_name or Path(archive_name).name != archive_name:
        fail("Package archive filename is unsafe")

    archive_path = ROOT / archive_name
    if not archive_path.is_file() or archive_path.is_symlink():
        fail("Package archive file is missing or unsafe")

    report = read_json(report_file)
    slug = clean(report.get("story_slug"))
    root_name = f"{slug}-{mode}"

    if not slug or report.get("package_root") != root_name:
        fail("Package story root mismatch")
    if report.get("status") != "completed":
        fail("Package report is not completed")
    if report.get("format") != mode or report.get("story_title") != title:
        fail("Package story identity mismatch")
    if positive_int(report.get("parts"), "Package PARTS") != len(part_numbers):
        fail("Package PARTS mismatch")
    if positive_int(report.get("configured_parts"), "Package configured PARTS") != len(part_numbers):
        fail("Package configured PARTS mismatch")
    if report.get("part_videos_only") is not True:
        fail("Package does not confirm Part-video-only output")
    if report.get("combined_video_included") is not False:
        fail("Package includes a combined video")
    if report.get("format_alias_included") is not False:
        fail("Package includes the opposite format alias")

    if archive_name != f"katha-lok-{slug}-{mode}-output.zip":
        fail("Package archive filename is not story-specific")

    archive_hash = sha256_file(archive_path)
    if report.get("archive_sha256") != archive_hash:
        fail("Package archive SHA-256 mismatch")
    if report.get("archive_bytes") != archive_path.stat().st_size:
        fail("Package archive byte count mismatch")

    expected_files = report.get("files")
    if not isinstance(expected_files, list):
        fail("Package file hash list is missing")

    expected_paths = set()
    for item in expected_files:
        if not isinstance(item, dict):
            fail("Invalid package file record")
        member = item.get("path")
        if not isinstance(member, str) or member in expected_paths:
            fail("Invalid or duplicate package member path")
        expected_paths.add(member)

    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if not names or len(names) != len(set(names)):
            fail("ZIP is empty or contains duplicate entries")
        if archive.testzip() is not None:
            fail("ZIP CRC validation failed")

        prefix = root_name + "/"
        for info in infos:
            name = info.filename
            member_path = PurePosixPath(name)
            if (
                member_path.is_absolute()
                or not member_path.parts
                or any(part in {"", ".", ".."} for part in member_path.parts)
                or "\\" in name
                or not name.startswith(prefix)
                or info.flag_bits & 0x1
                or info.file_size <= 0
            ):
                fail(f"Unsafe ZIP member: {name}")

        embedded_path = f"{root_name}/package_manifest.json"
        if embedded_path not in names:
            fail("Embedded package manifest is missing")

        embedded = json.loads(
            archive.read(embedded_path).decode("utf-8")
        )
        if (
            embedded.get("status") != "completed"
            or embedded.get("format") != mode
            or embedded.get("story_title") != title
            or embedded.get("story_slug") != slug
            or embedded.get("package_root") != root_name
            or embedded.get("part_videos_only") is not True
            or embedded.get("combined_video_included") is not False
            or embedded.get("format_alias_included") is not False
            or embedded.get("manifest_zip_consistency") is not True
        ):
            fail("Embedded package manifest is inconsistent")

        if set(names) != expected_paths | {embedded_path}:
            fail("ZIP members differ from package file hash list")

        for item in expected_files:
            payload = archive.read(item["path"])
            if (
                len(payload) != item.get("bytes")
                or hashlib.sha256(payload).hexdigest() != item.get("sha256")
            ):
                fail(f"ZIP member hash/size mismatch: {item['path']}")

        expected_videos = {
            f"{root_name}/parts/{slug}-part-{number:02d}.mp4"
            for number in part_numbers
        }
        actual_videos = {
            name for name in names
            if PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS
        }
        if actual_videos != expected_videos:
            fail("ZIP has missing, extra or incorrectly named videos")

        thumbnail_member = f"{root_name}/{slug}-thumbnail.jpg"
        if thumbnail_member not in names:
            fail("Story-named thumbnail is missing")

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
            or not isinstance(packaged_render.get("parts"), list)
            or len(packaged_render["parts"]) != len(part_numbers)
        ):
            fail("Packaged render manifest is invalid")

        for part, row in zip(part_numbers, packaged_render["parts"]):
            expected = (
                f"{root_name}/parts/{slug}-part-{part:02d}.mp4"
            )
            if (
                positive_int(row.get("part"), "Packaged render Part") != part
                or row.get("path") != expected
            ):
                fail(f"Packaged render Part {part} path mismatch")

    return {
        "status": "passed",
        "archive": archive_name,
        "archive_sha256": archive_hash,
        "archive_bytes": archive_path.stat().st_size,
        "member_count": len(names),
        "part_videos_only": True,
    }


def main() -> int:
    if not shutil.which("ffprobe"):
        fail("ffprobe is required")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    if mode not in {"short", "full"}:
        fail(f"Invalid FORMAT: {mode}")

    selected_parts = get_selected_parts(config, required=True)
    if selected_parts is None:
        fail("Actual selected PARTS are not persisted")
    parts = positive_int(selected_parts, "Selected PARTS")

    scene_minimum, scene_maximum = get_scenes_range(config)
    fps = parse_fps(get_fps(config), "Input FPS")

    dimensions = (
        (720, 1280)
        if mode == "short"
        else (1280, 720)
    )

    story_data, story_path = load_story()
    part_numbers = list(range(1, parts + 1))
    scene_counts = actual_counts_from_story(
        story_data,
        part_numbers,
        scene_minimum,
        scene_maximum,
    )

    story_check = validate_story(
        story_data,
        story_path,
        part_numbers,
        scene_counts,
        mode,
    )

    photo_check = validate_photo_motion(
        part_numbers,
        scene_counts,
        mode,
        dimensions,
        fps,
    )
    narration_check = validate_narration(
        part_numbers,
        scene_counts,
    )
    sound_check = validate_sound_design(
        part_numbers,
        scene_counts,
        mode,
    )
    render_check = validate_render(
        part_numbers,
        scene_counts,
        mode,
        dimensions,
        fps,
    )
    caption_check = validate_captions(part_numbers)
    thumbnail_check = validate_thumbnail(mode, dimensions)
    metadata_check = validate_metadata(
        mode,
        story_check["title"],
    )
    mode_check = validate_mode_separation(mode, dimensions)
    unique_check = validate_unique_output_files()
    package_check = validate_package(
        part_numbers,
        mode,
        story_check["title"],
    )

    checks = {
        "story": story_check,
        "photo_motion": photo_check,
        "narration": narration_check,
        "sound_design": sound_check,
        "render": render_check,
        "captions": caption_check,
        "thumbnail": thumbnail_check,
        "metadata": metadata_check,
        "unique_output_files": unique_check,
        "mode_separation": mode_check,
        "package": package_check,
    }

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "story_title": story_check["title"],
        "parts": parts,
        "part_numbers": part_numbers,
        "scenes_range": [scene_minimum, scene_maximum],
        "actual_scene_counts": {
            str(part): scene_counts[part] for part in part_numbers
        },
        "total_main_scenes": sum(scene_counts.values()),
        "total_role_clips": (
            sum(scene_counts.values()) + parts * 2
        ),
        "resolution": list(dimensions),
        "fps": fps,
        "checks": checks,
        "failed_checks": [],
        "package_status": package_check["status"],
        "validated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }

    write_json(REPORT, report)

    log("=" * 60)
    log("FINAL OUTPUT VALIDATION PASSED")
    log("FORMAT:", mode)
    log("STORY:", story_check["title"])
    log("PARTS:", part_numbers)
    log("SCENE RANGE:", f"{scene_minimum}-{scene_maximum}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("TOTAL MAIN SCENES:", sum(scene_counts.values()))
    log("TOTAL ROLE CLIPS:", report["total_role_clips"])
    log("RESOLUTION:", dimensions)
    log("FPS:", fps)
    log("PACKAGE:", package_check["status"])
    log("REPORT:", relative(REPORT))
    log("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("FINAL OUTPUT VALIDATION FAILED:", exc)
        raise SystemExit(1)