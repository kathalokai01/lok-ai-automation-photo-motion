#!/usr/bin/env python3
"""Strict final validation for story-specific, variable-scene Photo Motion output."""

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
from story_length_policy import verify_revision_evidence

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"

STORY = OUT / "story" / "story.json"
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
PACKAGE_REPORT = OUT / "package_manifest.json"
PACKAGE_NAME = OUT / "package_archive_name.txt"

VERSION = "final-validator-v101-strict-manifest-audio-caption-path-integrity"
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
SUCCESS = {
    "completed", "complete", "success", "done", "reused", "valid",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


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


def digest_json(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        fail(f"Artifact path escapes repository: {path}")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, optional: bool = False) -> Any:
    if not path.is_file():
        if optional:
            return None
        fail(f"Required file is missing: {relative(path)}")
    if path.stat().st_size <= 0:
        fail(f"Required file is empty: {relative(path)}")
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except Exception as exc:
        fail(f"Invalid JSON {relative(path)}: {exc}")


def positive_int(value: Any, label: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        fail(f"{label}: invalid integer")
    try:
        result = int(value)
        if isinstance(value, float) and (not math.isfinite(value) or value != result):
            fail(f"{label}: fractional integer is not permitted")
        if isinstance(value, str) and str(result) != value.strip():
            fail(f"{label}: non-canonical integer is not permitted")
    except (TypeError, ValueError, OverflowError):
        fail(f"{label}: invalid integer")
    if result < (0 if allow_zero else 1):
        fail(f"{label}: value is outside the permitted range")
    return result


def parse_fps(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (TypeError, ValueError, ZeroDivisionError):
        fail(f"{label}: invalid FPS")
    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS")
    return result


def safe_output(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: path is missing")

    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    lexical = Path(os.path.abspath(str(candidate)))
    output_root = Path(os.path.abspath(str(OUT)))

    try:
        lexical.relative_to(output_root)
    except ValueError:
        fail(f"{label}: path must remain inside output/")

    current = output_root
    if current.is_symlink():
        fail("output/ must not be a symlink")

    for component in lexical.relative_to(output_root).parts:
        current = current / component
        if current.is_symlink():
            fail(f"{label}: symlink path is forbidden: {current}")

    resolved = lexical.resolve()
    try:
        resolved.relative_to(OUT.resolve())
    except ValueError:
        fail(f"{label}: resolved path escapes output/")

    if not resolved.is_file():
        fail(f"{label}: file does not exist: {resolved}")
    if resolved.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small")
    return resolved


def first_path(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def rows_from(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


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
        fail(
            f"ffprobe failed for {path.name}: "
            f"{(result.stderr or '')[-1000:]}"
        )

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
    if any(not isinstance(item, dict) for item in streams):
        fail(f"Invalid media stream record: {path.name}")

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
        if int(video.get("width") or 0) < 1 or int(video.get("height") or 0) < 1:
            fail(f"Invalid video dimensions: {path.name}")
        rate = video.get("avg_frame_rate")
        if not rate or rate in {"0/0", "N/A"}:
            rate = video.get("r_frame_rate")

    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
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

    if not info["has_video"]:
        fail(f"{label}: video stream is missing")
    if require_audio and not info["has_audio"]:
        fail(f"{label}: audio stream is missing")
    if (info["width"], info["height"]) != dimensions:
        fail(
            f"{label}: expected {dimensions[0]}x{dimensions[1]}, "
            f"found {info['width']}x{info['height']}"
        )
    if info["fps"] is None or abs(info["fps"] - fps) > FPS_TOLERANCE:
        fail(f"{label}: FPS mismatch: {info['fps']}")
    return info


def normalize_role(row: dict) -> str:
    value = clean(
        row.get("role")
        or row.get("scene_type")
        or row.get("kind")
        or row.get("type")
        or "main"
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


def canonical_id(part: int, role: str, scene: int | None = None) -> str:
    if role == "main":
        if scene is None:
            fail("Main scene number is missing")
        return f"part_{part:02d}_scene_{scene:02d}"
    return f"part_{part:02d}_{role}"


def scene_key(row: dict, label: str) -> tuple[int, str, int | None]:
    part = positive_int(
        row.get("part", row.get("part_number")),
        f"{label} Part",
    )
    role = normalize_role(row)

    if role == "main":
        scene = positive_int(
            row.get("local_scene") or row.get("scene")
            or row.get("source_scene_number"),
            f"{label} Main scene",
        )
        return part, role, scene

    return part, role, None


def expected_scene_keys(
    part_numbers: list[int],
    scene_counts: dict[int, int],
) -> set[tuple[int, str, int | None]]:
    expected = set()
    for index, part in enumerate(part_numbers):
        expected.add((part, "hook", None))
        for scene in range(1, scene_counts[part] + 1):
            expected.add((part, "main", scene))
        if index < len(part_numbers) - 1:
            expected.add((part, "suspense", None))
        else:
            expected.add((part, "resolution", None))
    return expected


def validate_role_rows(
    rows: Any,
    part_numbers: list[int],
    scene_counts: dict[int, int],
    label: str,
) -> dict:
    if not isinstance(rows, list):
        fail(f"{label}: scene rows are missing")

    expected = expected_scene_keys(part_numbers, scene_counts)
    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid row {index}")

        key = scene_key(row, label)
        if key not in expected:
            fail(f"{label}: unexpected role or scene: {key}")
        if key in mapping:
            fail(f"{label}: duplicate scene record: {key}")

        expected_id = canonical_id(*key)
        declared_id = clean(row.get("scene_id") or row.get("job_id"))
        if declared_id and declared_id != expected_id:
            fail(f"{label}: scene ID mismatch: {declared_id}")

        mapping[key] = row

    missing = expected - set(mapping)
    if missing:
        fail(f"{label}: missing scene records: {sorted(missing, key=str)}")
    return mapping


def validate_story(
    story: dict,
    part_numbers: list[int],
    scenes_min: int,
    scenes_max: int,
    mode: str,
) -> tuple[dict[int, int], dict]:
    if clean(story.get("status")).lower() != "completed":
        fail("story.json status is not completed")
    if clean(story.get("format")).lower() != mode:
        fail("Story FORMAT differs from Input/topic.txt")

    title = clean(story.get("title") or story.get("final_title"))
    if not title:
        fail("Story-derived title is missing")
    if clean(story.get("final_title")) != title:
        fail("Story title and final_title differ")

    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != len(part_numbers):
        fail("Story Part count differs from selected PARTS")

    raw_counts = story.get("actual_scene_counts")
    if not isinstance(raw_counts, list) or len(raw_counts) != len(parts):
        fail("Story actual_scene_counts must be an ordered list")

    scene_counts = {}
    word_counts = []
    flattened = []
    merged_parts = []

    for index, (part_number, part) in enumerate(
        zip(part_numbers, parts)
    ):
        if not isinstance(part, dict):
            fail(f"Invalid story Part {part_number}")

        if positive_int(part.get("part"), "Story Part number") != part_number:
            fail("Story Part order or numbering mismatch")
        if clean(part.get("part_id")) != f"part_{part_number:02d}":
            fail(f"Story Part ID mismatch: {part_number}")

        narration = clean(part.get("narration"))
        if not narration:
            fail(f"Story narration missing in Part {part_number}")

        words = len(re.findall(r"\S+", narration))
        if positive_int(part.get("word_count"), "Story word_count") != words:
            fail(f"Story word_count metadata mismatch in Part {part_number}")

        if mode == "short":
            merged = part.get("final_part_merged") is True
            final_part = index == len(parts) - 1

            if merged and not final_part:
                fail("Only the final SHORT Part may use the merge exception")
            if merged:
                merged_parts.append(part_number)

            maximum = 239 if merged else 180
            if not 120 <= words <= maximum:
                fail(
                    f"SHORT Part {part_number} has {words} words; "
                    f"permitted range is 120-{maximum}"
                )

            if words > 150 and not merged:
                if part.get("revision_attempted") is not True:
                    fail(
                        f"SHORT Part {part_number} exceeds 150 words "
                        "without a recorded revision"
                    )
                evidence = part.get("revision_evidence")
                try:
                    valid = verify_revision_evidence(
                        evidence=evidence,
                        revised_text=narration,
                    )
                except TypeError:
                    valid = verify_revision_evidence(evidence, narration)
                except Exception as exc:
                    fail(
                        f"SHORT Part {part_number} revision evidence "
                        f"cannot be verified: {exc}"
                    )
                if valid is not True:
                    fail(
                        f"SHORT Part {part_number} revision evidence is invalid"
                    )

        if narration[-1] not in (
            "।", "॥", ".", "!", "?", "…", "...",
            ",", "，", ";", "؛", ":", "—", "–",
        ):
            fail(f"Part {part_number} ends mid-sentence or mid-clause")

        scenes = part.get("scenes")
        if not isinstance(scenes, list):
            fail(f"Part {part_number} Main scenes are missing")

        actual_count = positive_int(
            raw_counts[index],
            f"Story actual_scene_counts[{index}]",
        )
        if not scenes_min <= actual_count <= scenes_max:
            fail(
                f"Part {part_number} has {actual_count} scenes; "
                f"SCENES range is {scenes_min}-{scenes_max}"
            )
        if len(scenes) != actual_count:
            fail(f"Part {part_number} actual scene count mismatch")

        scene_counts[part_number] = actual_count
        scene_narrations = []

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                fail(f"Invalid Main scene in Part {part_number}")

            sid = canonical_id(part_number, "main", scene_number)
            expected_values = {
                "scene_id": sid,
                "part": part_number,
                "scene": scene_number,
                "role": "main",
            }
            for field, expected in expected_values.items():
                if scene.get(field) != expected:
                    fail(f"Story {field} mismatch: {sid}")

            if not clean(scene.get("narration")):
                fail(f"Scene narration missing: {sid}")
            if not clean(scene.get("visual")):
                fail(f"Scene visual missing: {sid}")
            if clean(scene.get("visual")) != clean(scene.get("visual_prompt")):
                fail(f"Scene visual/visual_prompt mismatch: {sid}")

            scene_narrations.append(clean(scene["narration"]))
            flattened.append(scene)

        if clean(" ".join(scene_narrations)) != narration:
            fail(
                f"Part {part_number}: ordered Main scene narration "
                "does not reconstruct the Part narration"
            )

        if part.get("main_scene_count") != actual_count:
            fail(f"Part {part_number} main_scene_count mismatch")
        if part.get("actual_scene_count") != actual_count:
            fail(f"Part {part_number} actual_scene_count mismatch")

        word_counts.append(words)

    if story.get("scenes") != flattened:
        fail("Top-level story scenes differ from ordered Part scenes")

    if story.get("total_parts") != len(parts):
        fail("Story total_parts mismatch")
    if story.get("parts_count") != len(parts):
        fail("Story parts_count mismatch")
    if story.get("total_scenes") != sum(scene_counts.values()):
        fail("Story total_scenes mismatch")
    if story.get("total_main_scenes") != sum(scene_counts.values()):
        fail("Story total_main_scenes mismatch")

    full_text = clean(" ".join(part["narration"] for part in parts))
    if clean(story.get("story_text")) != full_text:
        fail("Story story_text differs from ordered Part narration")
    if clean(story.get("corrected_complete_story")) != full_text:
        fail("Story corrected_complete_story differs from Parts")

    if positive_int(story.get("total_story_words"), "Story total_story_words") != len(
        re.findall(r"\S+", full_text)
    ):
        fail("Story total_story_words mismatch")

    policy = story.get("story_length_policy")
    if not isinstance(policy, dict):
        fail("Story story_length_policy is missing")
    if policy.get("actual_scene_counts") != raw_counts:
        fail("Story length policy scene counts mismatch")
    if policy.get("actual_parts") != len(parts):
        fail("Story length policy actual_parts mismatch")
    if policy.get("final_part_merged") is not bool(merged_parts):
        fail("Story final merge policy does not match Part records")

    return scene_counts, {
        "status": "valid",
        "title": title,
        "parts": len(parts),
        "actual_scene_counts": {
            str(part): scene_counts[part] for part in part_numbers
        },
        "words_per_part": word_counts,
        "total_words": len(re.findall(r"\S+", full_text)),
        "total_main_scenes": sum(scene_counts.values()),
        "final_part_merged": bool(merged_parts),
        "story_sha256": digest_file(STORY),
        "story_file": relative(STORY),
    }


def validate_story_verification(story: dict, mode: str, parts: int) -> dict:
    data = read_json(STORY_VERIFICATION)
    if clean(data.get("status")).lower() != "completed":
        fail("Story verification status is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Story verification FORMAT mismatch")
    if data.get("story_fingerprint") != digest_json(story):
        fail("Story verification fingerprint does not match story.json")

    summary = data.get("summary")
    if not isinstance(summary, dict):
        fail("Story verification summary is missing")
    if positive_int(summary.get("selected_parts"), "Verified selected PARTS") != parts:
        fail("Story verification selected PARTS mismatch")
    if summary.get("actual_scene_counts") != story.get("actual_scene_counts"):
        fail("Story verification scene counts mismatch")

    return {
        "status": "valid",
        "verification_sha256": digest_file(STORY_VERIFICATION),
        "summary": summary,
    }


def validate_extra_role_records(
    story: dict,
    part_numbers: list[int],
) -> dict:
    hooks = story.get("hook_scenes")
    suspenses = story.get("suspense_scenes")
    resolutions = story.get("resolution_scenes")

    if not isinstance(hooks, list) or len(hooks) != len(part_numbers):
        fail("Story Hook records do not match selected Parts")
    if not isinstance(suspenses, list) or len(suspenses) != len(part_numbers) - 1:
        fail("Story Suspense records do not match non-final Parts")
    if not isinstance(resolutions, list) or len(resolutions) != 1:
        fail("Story must contain exactly one final Resolution")

    for part_number, part in enumerate(story["parts"], 1):
        final_part = part_number == len(part_numbers)

        for role, field, required in (
            ("hook", "hook_scene", True),
            ("suspense", "suspense_scene", not final_part),
            ("resolution", "resolution_scene", final_part),
        ):
            row = part.get(field)
            if not required:
                if row is not None:
                    fail(f"Part {part_number} has unexpected {role} record")
                continue

            if not isinstance(row, dict):
                fail(f"Part {part_number} is missing {role} record")

            expected_id = canonical_id(part_number, role)
            if row.get("scene_id") != expected_id:
                fail(f"Story {role} ID mismatch: {expected_id}")
            if row.get("part") != part_number or row.get("role") != role:
                fail(f"Story {role} Part/role mismatch: {expected_id}")
            if row.get("separate_from_main_scenes") is not True:
                fail(f"Story {role} is not a separate scene: {expected_id}")
            if row.get("main_scene_numbering_affected") is not False:
                fail(f"Story {role} affects Main scene numbering: {expected_id}")
            if row.get("scene") is not None:
                fail(f"Story {role} must not have a Main scene number: {expected_id}")

            for key in (
                "story_anchor", "purpose", "instruction",
                "continuity_rule", "invention_policy", "visual_policy",
            ):
                if not clean(row.get(key)):
                    fail(f"Story {role} is missing {key}: {expected_id}")

            main_scenes = part.get("scenes", [])
            anchor = (
                clean(main_scenes[0].get("story_anchor"))
                if role == "hook"
                else clean(main_scenes[-1].get("story_anchor"))
            )
            if clean(row.get("story_anchor")) != anchor:
                fail(f"Story {role} anchor mismatch: {expected_id}")

        expected_extras = [
            row for row in (
                part.get("hook_scene"),
                part.get("suspense_scene"),
                part.get("resolution_scene"),
            )
            if isinstance(row, dict)
        ]
        if part.get("extra_scenes") != expected_extras:
            fail(f"Part {part_number} extra_scenes mismatch")

    return {
        "status": "valid",
        "hook_count": len(hooks),
        "suspense_count": len(suspenses),
        "resolution_count": len(resolutions),
    }

def validate_media_rows(
    manifest_path: Path,
    part_numbers: list[int],
    scene_counts: dict[int, int],
    label: str,
    dimensions: tuple[int, int] | None = None,
    fps: float | None = None,
    media_type: str = "video",
    require_audio: bool = False,
) -> dict:
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        fail(f"{label} manifest must be a JSON object")

    status = clean(data.get("status")).lower()
    if status and status not in SUCCESS:
        fail(f"{label} manifest is not completed: {status}")

    rows = rows_from(
        data,
        "jobs", "audio_jobs", "scenes", "records", "items",
        "visual_jobs", "photo_motion_jobs",
    )
    mapping = validate_role_rows(
        rows, part_numbers, scene_counts, label
    )

    verified = []
    seen_media_paths = set()
    for key in sorted(
        mapping,
        key=lambda item: (
            item[0], ROLE_ORDER[item[1]], item[2] or 0
        ),
    ):
        row = mapping[key]
        sid = canonical_id(*key)

        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"{label} record is incomplete: {sid}")

        if media_type == "video":
            value = first_path(
                row, "video", "output", "path", "source_video"
            )
            path = safe_output(value, f"{label} {sid}", MIN_VIDEO_BYTES)
            info = validate_video(
                path,
                dimensions,
                fps,
                f"{label} {sid}",
                require_audio=require_audio,
            )
            declared_hash = (
                row.get("video_sha256")
                or row.get("sha256")
            )
        else:
            value = first_path(
                row,
                "audio", "audio_path", "audio_file",
                "output", "path", "file",
            )
            path = safe_output(value, f"{label} {sid}", MIN_AUDIO_BYTES)
            info = media_info(path)
            if not info["has_audio"]:
                fail(f"{label} {sid}: audio stream is missing")
            declared_duration = row.get(
                "actual_audio_duration_seconds",
                row.get("audio_duration", row.get("duration")),
            )
            if declared_duration is None:
                fail(f"{label} {sid}: ffprobe-measured audio duration is missing")
            try:
                expected_duration = float(declared_duration)
            except (TypeError, ValueError):
                fail(f"{label} {sid}: invalid audio duration")
            if (
                not math.isfinite(expected_duration)
                or expected_duration <= 0
                or abs(expected_duration - info["duration"]) > 0.2
            ):
                fail(f"{label} {sid}: measured audio duration mismatch")

            source = (
                row.get("duration_source")
                or row.get("audio_duration_source")
            )
            if source != "ffprobe_measured":
                fail(f"{label} {sid}: duration is not ffprobe-measured")
            declared_hash = (
                row.get("audio_sha256")
                or row.get("sha256")
            )

        checksum = digest_file(path)
        if not isinstance(declared_hash, str) or not re.fullmatch(
            r"[a-f0-9]{64}", declared_hash.strip().lower()
        ):
            fail(f"{label} {sid}: manifest SHA-256 is missing or invalid")
        if declared_hash.strip().lower() != checksum:
            fail(f"{label} {sid}: SHA-256 mismatch")
        resolved_media = path.resolve()
        if resolved_media in seen_media_paths:
            fail(f"{label}: duplicate media file is referenced by multiple scenes: {relative(path)}")
        seen_media_paths.add(resolved_media)

        verified.append({
            "scene_id": sid,
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            "path": relative(path),
            "duration": round(info["duration"], 4),
            "sha256": checksum,
        })

    if not verified:
        fail(f"{label}: no verified media records")
    return {
        "status": "valid",
        "record_count": len(verified),
        "records": verified,
        "manifest_sha256": digest_file(manifest_path),
    }


def validate_photo_motion(
    part_numbers: list[int],
    scene_counts: dict[int, int],
    mode: str,
    dimensions: tuple[int, int],
    fps: float,
) -> dict:
    data = read_json(PHOTO)

    if clean(data.get("status")).lower() != "completed":
        fail("Photo Motion manifest is not completed")
    if clean(data.get("format")).lower() != mode:
        fail("Photo Motion FORMAT mismatch")

    if (
        positive_int(data.get("width"), "Photo Motion width") != dimensions[0]
        or positive_int(data.get("height"), "Photo Motion height") != dimensions[1]
    ):
        fail("Photo Motion dimensions mismatch")

    if abs(parse_fps(data.get("fps"), "Photo Motion FPS") - fps) > FPS_TOLERANCE:
        fail("Photo Motion FPS mismatch")

    rows = data.get("scenes")
    mapping = validate_role_rows(
        rows, part_numbers, scene_counts, "Photo Motion"
    )

    expected_count = sum(scene_counts.values()) + len(part_numbers) * 2
    if len(mapping) != expected_count:
        fail("Photo Motion total role-clip count mismatch")

    for field in ("expected_scene_count", "completed_count", "total_scenes"):
        declared = data.get(field)
        if declared is not None and positive_int(
            declared, f"Photo Motion {field}", allow_zero=True
        ) != expected_count:
            fail(f"Photo Motion {field} mismatch")

    if data.get("part_outputs_only") is not True:
        fail("Photo Motion manifest must confirm Part-only output")
    if data.get("combined_video_created") is not False:
        fail("Photo Motion must not create a combined video")

    result = validate_media_rows(
        PHOTO,
        part_numbers,
        scene_counts,
        "Photo Motion",
        dimensions=dimensions,
        fps=fps,
        media_type="video",
        require_audio=False,
    )

    return {
        "status": "valid",
        "format": mode,
        "main_scene_count": sum(scene_counts.values()),
        "role_clip_count": result["record_count"],
        "records": result["records"],
        "manifest_sha256": result["manifest_sha256"],
    }


def validate_narration(
    part_numbers: list[int],
    scene_counts: dict[int, int],
) -> dict:
    data = read_json(AUDIO)
    if clean(data.get("status")).lower() != "completed":
        fail("Narration audio manifest is not completed")

    result = validate_media_rows(
        AUDIO,
        part_numbers,
        scene_counts,
        "Narration",
        media_type="audio",
    )

    expected = sum(scene_counts.values()) + len(part_numbers) * 2
    if result["record_count"] != expected:
        fail(
            f"Narration record count mismatch: "
            f"{result['record_count']} != {expected}"
        )

    return result


def validate_sound_design(
    part_numbers: list[int],
    scene_counts: dict[int, int],
    mode: str,
) -> dict:
    sound = read_json(SOUND)
    validation = read_json(SFX_VALIDATION)

    if clean(sound.get("status")).lower() not in SUCCESS:
        fail("Sound-design manifest is not completed")
    if clean(sound.get("format")).lower() != mode:
        fail("Sound-design FORMAT mismatch")
    if clean(validation.get("status")).lower() not in SUCCESS:
        fail("SFX timing validation is not completed")
    if clean(validation.get("format")).lower() not in {"", mode}:
        fail("SFX timing validation FORMAT mismatch")

    for key in (
        "errors", "failed_checks", "invalid_cues",
        "out_of_range_cues", "timing_errors",
    ):
        value = validation.get(key)
        if isinstance(value, list) and value:
            fail(f"SFX timing validation contains {key}: {value[:5]}")
        if isinstance(value, (int, float)) and value != 0:
            fail(f"SFX timing validation reports {key}={value}")

    tracks = sound.get("tracks", {})
    if tracks is not None and not isinstance(tracks, (dict, list)):
        fail("Sound-design tracks must be an object or list")

    verified_tracks = []
    track_rows = (
        list(tracks.values()) if isinstance(tracks, dict)
        else tracks if isinstance(tracks, list)
        else []
    )

    for index, record in enumerate(track_rows, 1):
        if record is None:
            continue
        if not isinstance(record, dict):
            fail(f"Invalid sound-design track record {index}")

        status = clean(record.get("status")).lower()
        if status and status not in SUCCESS:
            fail(f"Sound-design track {index} is incomplete")

        value = first_path(
            record, "path", "audio_path", "audio", "output", "file"
        )
        if not value:
            continue

        path = safe_output(value, f"Sound-design track {index}", MIN_AUDIO_BYTES)
        info = media_info(path)
        if not info["has_audio"]:
            fail(f"Sound-design track {index} has no audio stream")

        checksum = digest_file(path)
        declared = record.get("sha256") or record.get("audio_sha256")
        if declared and clean(declared).lower() != checksum:
            fail(f"Sound-design track {index} SHA-256 mismatch")

        verified_tracks.append({
            "category": clean(
                record.get("category") or record.get("role")
                or record.get("type")
            ),
            "path": relative(path),
            "duration": round(info["duration"], 4),
            "sha256": checksum,
        })

    cue_rows = rows_from(
        validation, "cues", "validated_cues", "sfx_cues", "records"
    )
    if not cue_rows:
        cue_data = read_json(SFX, optional=True)
        cue_rows = rows_from(
            cue_data, "cues", "sfx_cues", "jobs", "records"
        )

    valid_ids = {
        canonical_id(*key)
        for key in expected_scene_keys(part_numbers, scene_counts)
    }
    verified_cues = []

    for index, cue in enumerate(cue_rows, 1):
        if not isinstance(cue, dict):
            fail(f"Invalid SFX cue {index}")

        sid = clean(cue.get("scene_id"))
        if sid and sid not in valid_ids:
            fail(f"SFX cue {index} references unknown scene {sid}")

        value = first_path(
            cue, "path", "audio_path", "audio", "file", "output"
        )
        if value:
            path = safe_output(value, f"SFX cue {index}", MIN_AUDIO_BYTES)
            info = media_info(path)
            if not info["has_audio"]:
                fail(f"SFX cue {index} has no audio stream")
            checksum = digest_file(path)
            declared = cue.get("sha256") or cue.get("audio_sha256")
            if declared and clean(declared).lower() != checksum:
                fail(f"SFX cue {index} SHA-256 mismatch")
            verified_cues.append({
                "scene_id": sid or None,
                "path": relative(path),
                "duration": round(info["duration"], 4),
                "sha256": checksum,
            })
        else:
            verified_cues.append({
                "scene_id": sid or None,
                "cue": clean(cue.get("kind") or cue.get("type")),
            })

    return {
        "status": "valid",
        "tracks_verified": len(verified_tracks),
        "sfx_cues_verified": len(verified_cues),
        "tracks": verified_tracks,
        "cues": verified_cues,
        "sound_manifest_sha256": digest_file(SOUND),
        "sfx_validation_sha256": digest_file(SFX_VALIDATION),
    }


def parse_srt(path: Path) -> list[dict]:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read SRT: {exc}")

    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [
        item.strip()
        for item in re.split(r"\n[ \t]*\n", content)
        if item.strip()
    ]

    def timestamp(value: str) -> float:
        match = re.fullmatch(
            r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
            value.strip(),
        )
        if not match:
            fail(f"Invalid SRT timestamp: {value}")
        hours, minutes, seconds, millis = map(int, match.groups())
        return hours * 3600 + minutes * 60 + seconds + millis / 1000

    rows = []
    previous_end = -1.0

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index} is incomplete")
        if int(lines[0].strip()) != index or lines[1].count("-->") != 1:
            fail(f"SRT cue {index} numbering/timing is invalid")

        left, right = [value.strip() for value in lines[1].split("-->")]
        start, end = timestamp(left), timestamp(right)

        if end <= start or start < previous_end - 0.001:
            fail(f"SRT cue {index} overlaps or has invalid timing")

        text_value = "\n".join(lines[2:]).strip()
        if not text_value:
            fail(f"SRT cue {index} has no caption text")

        previous_end = end
        rows.append({
            "index": index,
            "start": start,
            "end": end,
            "text": text_value,
        })

    if not rows:
        fail("Final SRT contains no caption cues")
    return rows


def validate_captions(
    part_numbers: list[int],
    total_duration: float,
    render_data: dict,
) -> dict:
    captions = read_json(CAPTIONS)
    srt_path = safe_output(SRT, "Final captions SRT", 10)
    srt_rows = parse_srt(srt_path)

    if isinstance(captions, dict):
        status = clean(captions.get("status")).lower()
        if status and status not in SUCCESS:
            fail("Caption manifest is not completed")

        declared_parts = captions.get("part_numbers")
        if isinstance(declared_parts, list):
            if declared_parts != part_numbers:
                fail("Caption manifest Part numbers mismatch")

        declared_count = (
            captions.get("caption_count")
            or captions.get("total_captions")
            or captions.get("subtitle_count")
        )
        if declared_count is not None:
            if positive_int(declared_count, "Caption count") != len(srt_rows):
                fail("Caption manifest and SRT cue counts differ")

    if srt_rows[-1]["end"] > total_duration + DURATION_TOLERANCE:
        fail("SRT contains captions beyond the final video duration")
    declared_srt_hash = clean(render_data.get("final_srt_sha256")).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", declared_srt_hash):
        fail("Render manifest is missing the final SRT SHA-256")
    if declared_srt_hash != digest_file(srt_path):
        fail("Final SRT SHA-256 differs from the burned-caption render manifest")
    declared_caption_manifest = clean(render_data.get("caption_manifest"))
    if declared_caption_manifest and declared_caption_manifest != relative(CAPTIONS):
        fail("Render manifest caption-manifest path mismatch")
    timeline = render_data.get("caption_timeline")
    if not isinstance(timeline, dict) or any(
        timeline.get(key) is not True
        for key in ("includes_hook", "includes_main", "includes_suspense", "includes_resolution", "global_srt")
    ):
        fail("Burned-caption timeline does not cover every required scene role")

    return {
        "status": "valid",
        "parts": len(part_numbers),
        "subtitle_count": len(srt_rows),
        "first_subtitle": srt_rows[0]["start"],
        "last_subtitle": srt_rows[-1]["end"],
        "srt_sha256": digest_file(srt_path),
        "manifest_sha256": digest_file(CAPTIONS),
    }


def validate_render(
    part_numbers: list[int],
    scene_counts: dict[int, int],
    mode: str,
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
    if data.get("final_video") not in (None, ""):
        fail("Combined final video is forbidden")
    if data.get("alias") not in (None, ""):
        fail("Opposite-format alias is forbidden")

    if positive_int(data.get("width"), "Render width") != dimensions[0]:
        fail("Render width mismatch")
    if positive_int(data.get("height"), "Render height") != dimensions[1]:
        fail("Render height mismatch")
    if abs(parse_fps(data.get("fps"), "Render FPS") - fps) > FPS_TOLERANCE:
        fail("Render FPS mismatch")

    rows = (
        data.get("all_completed_scenes")
        or (
            list(data.get("completed_scenes") or data.get("scenes") or [])
            + list(data.get("extra_scenes") or [])
        )
    )
    mapping = validate_role_rows(
        rows, part_numbers, scene_counts, "Rendered scene"
    )

    for field, manifest_path in (
        ("source_photo_manifest_sha256", PHOTO),
        ("source_audio_manifest_sha256", AUDIO),
        ("source_sound_manifest_sha256", SOUND),
        ("source_sfx_validation_sha256", SFX_VALIDATION),
    ):
        declared_source_hash = clean(data.get(field)).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", declared_source_hash):
            fail(f"Render manifest source provenance is missing: {field}")
        if declared_source_hash != digest_file(manifest_path):
            fail(f"Render manifest source provenance mismatch: {field}")

    verified_scenes = []
    seen_render_paths = set()
    scene_duration_total = 0.0

    for key in sorted(
        mapping,
        key=lambda item: (
            item[0], ROLE_ORDER[item[1]], item[2] or 0
        ),
    ):
        row = mapping[key]
        sid = canonical_id(*key)

        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered scene is incomplete: {sid}")
        if row.get("captions_burned") is not True:
            fail(f"Rendered scene does not confirm burned captions: {sid}")

        value = first_path(row, "video", "output", "path")
        path = safe_output(value, f"Rendered scene {sid}", MIN_VIDEO_BYTES)
        info = validate_video(
            path, dimensions, fps, f"Rendered scene {sid}", True
        )
        checksum = digest_file(path)

        declared = row.get("sha256") or row.get("video_sha256")
        if not isinstance(declared, str) or not re.fullmatch(r"[a-f0-9]{64}", declared.strip().lower()):
            fail(f"Rendered scene manifest SHA-256 is missing or invalid: {sid}")
        if declared.strip().lower() != checksum:
            fail(f"Rendered scene SHA-256 mismatch: {sid}")
        if path.suffix.lower() != ".mp4":
            fail(f"Rendered scene must be MP4: {sid}")
        if path.resolve() in seen_render_paths:
            fail(f"Multiple rendered scene records reference the same MP4: {sid}")
        seen_render_paths.add(path.resolve())

        declared_duration = row.get("duration")
        if declared_duration is None:
            fail(f"Rendered scene duration is missing: {sid}")
        try:
            expected_duration = float(declared_duration)
        except (TypeError, ValueError):
            fail(f"Invalid rendered duration: {sid}")
        if (
            not math.isfinite(expected_duration)
            or expected_duration <= 0
            or abs(expected_duration - info["duration"]) > DURATION_TOLERANCE
        ):
            fail(f"Rendered scene duration mismatch: {sid}")

        scene_duration_total += info["duration"]
        verified_scenes.append({
            "scene_id": sid,
            "part": key[0],
            "role": key[1],
            "scene": key[2],
            "path": relative(path),
            "duration": round(info["duration"], 4),
            "sha256": checksum,
        })

    expected_main = sum(scene_counts.values())
    role_counts = {
        role: sum(key[1] == role for key in mapping)
        for role in ROLE_ORDER
    }
    if role_counts["main"] != expected_main:
        fail("Rendered Main scene count mismatch")
    if role_counts["hook"] != len(part_numbers):
        fail("Rendered Hook count mismatch")
    if role_counts["suspense"] != len(part_numbers) - 1:
        fail("Rendered Suspense count mismatch")
    if role_counts["resolution"] != 1:
        fail("Rendered Resolution count mismatch")

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != len(part_numbers):
        fail("Render manifest Part output count mismatch")

    verified_parts = []
    part_duration_total = 0.0
    for part_number, row in zip(part_numbers, part_rows):
        if not isinstance(row, dict):
            fail(f"Invalid rendered Part record: {part_number}")
        if positive_int(row.get("part"), "Rendered Part") != part_number:
            fail("Rendered Part order mismatch")
        if clean(row.get("status")).lower() not in SUCCESS:
            fail(f"Rendered Part {part_number} is incomplete")

        path = safe_output(
            first_path(row, "path", "output", "video"),
            f"Rendered Part {part_number}",
            MIN_VIDEO_BYTES,
        )
        if path.suffix.lower() != ".mp4":
            fail(f"Rendered Part {part_number} must be MP4")
        if path.parent.resolve() != (OUT / "parts").resolve():
            fail(f"Rendered Part {part_number} must be stored in output/parts/")

        info = validate_video(
            path, dimensions, fps, f"Rendered Part {part_number}", True
        )
        checksum = digest_file(path)
        declared = row.get("sha256") or row.get("video_sha256")
        if not isinstance(declared, str) or not re.fullmatch(r"[a-f0-9]{64}", declared.strip().lower()):
            fail(f"Rendered Part {part_number} manifest SHA-256 is missing or invalid")
        if declared.strip().lower() != checksum:
            fail(f"Rendered Part {part_number} SHA-256 mismatch")

        part_duration_total += info["duration")
        verified_parts.append({
            "part": part_number,
            "path": relative(path),
            "duration": round(info["duration"], 4),
            "sha256": checksum,
            "bytes": path.stat().st_size,
        })

    if abs(part_duration_total - scene_duration_total) > max(
        DURATION_TOLERANCE, len(mapping) * 0.05
    ):
        fail("Part video duration differs from the sum of rendered role clips")

    for field, expected in (
        ("all_completed_scene_count", len(mapping)),
        ("extra_scene_count", len(part_numbers) * 2),
        ("hook_count", len(part_numbers)),
        ("suspense_count", len(part_numbers) - 1),
        ("resolution_count", 1),
    ):
        declared = data.get(field)
        if declared is not None and positive_int(
            declared, f"Render {field}", allow_zero=True
        ) != expected:
            fail(f"Render manifest {field} mismatch")

    return {
        "status": "valid",
        "parts": verified_parts,
        "scenes": verified_scenes,
        "part_count": len(verified_parts),
        "main_scene_count": expected_main,
        "role_counts": role_counts,
        "all_role_clip_count": len(mapping),
        "part_duration_total": round(part_duration_total, 4),
        "scene_duration_total": round(scene_duration_total, 4),
        "captions_burned": True,
        "part_outputs_only": True,
        "manifest_sha256": digest_file(RENDER),
    }


def validate_thumbnail(
    mode: str,
    dimensions: tuple[int, int],
) -> dict:
    path = safe_output(THUMBNAIL, "Story thumbnail", MIN_IMAGE_BYTES)

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
    if clean(manifest.get("format")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")

    declared = first_path(
        manifest, "thumbnail", "path", "output", "file"
    )
    if declared:
        declared_path = safe_output(
            declared, "Thumbnail manifest path", MIN_IMAGE_BYTES
        )
        if declared_path.resolve() != path.resolve():
            fail("Thumbnail manifest points to a different image")
    thumbnail_rows = manifest.get("thumbnails")
    if not isinstance(thumbnail_rows, list) or len(thumbnail_rows) != 1:
        fail("Thumbnail manifest must contain exactly one output record")
    thumbnail_row = thumbnail_rows[0]
    if not isinstance(thumbnail_row, dict):
        fail("Thumbnail output record is invalid")
    if thumbnail_row.get("sha256") != digest_file(path):
        fail("Thumbnail output SHA-256 differs from its manifest")
    if thumbnail_row.get("bytes") != path.stat().st_size:
        fail("Thumbnail output byte count differs from its manifest")
    if thumbnail_row.get("format") != "JPEG":
        fail("Thumbnail manifest does not identify JPEG output")
    if thumbnail_row.get("width") != width or thumbnail_row.get("height") != height:
        fail("Thumbnail manifest dimensions differ from the image")

    return {
        "status": "valid",
        "path": relative(path),
        "dimensions": [width, height],
        "format": image_format,
        "bytes": path.stat().st_size,
        "sha256": digest_file(path),
        "manifest_sha256": digest_file(THUMBNAIL_MANIFEST),
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
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            result.append(tag)
    return result


def validate_metadata(mode: str, title: str) -> dict:
    text_path = safe_output(METADATA_TEXT, "YouTube metadata text", 10)
    json_path = safe_output(METADATA_JSON, "YouTube metadata JSON", 2)
    tags_path = safe_output(METADATA_TAGS, "YouTube tags", 2)

    metadata = read_json(json_path)
    if clean(metadata.get("status")).lower() not in SUCCESS:
        fail("Structured YouTube metadata is not completed")

    json_title = clean(metadata.get("title") or metadata.get("video_title"))
    if json_title != title:
        fail("YouTube metadata title does not match the story title")

    json_format = clean(
        metadata.get("format") or metadata.get("video_mode")
    ).lower()
    if json_format and json_format != mode:
        fail("YouTube metadata FORMAT mismatch")

    description = clean(
        metadata.get("description") or metadata.get("video_description")
    )
    if not description:
        fail("YouTube metadata description is missing")
    if len(json_title) > 100 or len(description) > 5000:
        fail("YouTube metadata exceeds title/description limits")

    tags = metadata.get("tags")
    if not isinstance(tags, list):
        fail("YouTube metadata tags must be a list")

    normalized_tags = []
    seen = set()
    for item in tags:
        if not isinstance(item, str):
            fail("YouTube metadata contains a non-text tag")
        tag = re.sub(r"\s+", " ", item).strip(" ,#")
        if not tag or len(tag) > 100:
            fail("YouTube metadata contains an invalid tag")
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            normalized_tags.append(tag)

    if not 5 <= len(normalized_tags) <= 20:
        fail(
            f"YouTube metadata requires 5-20 unique tags; "
            f"found {len(normalized_tags)}"
        )

    text_content = text_path.read_text(encoding="utf-8-sig")
    sections = {}
    current = None
    for line in text_content.splitlines():
        label = line.strip().strip("[]").rstrip(":").strip().upper()
        if label in {"TITLE", "DESCRIPTION", "TAGS"}:
            current = label
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.rstrip())

    text_title = " ".join(sections.get("TITLE", [])).strip()
    text_description = "\n".join(sections.get("DESCRIPTION", [])).strip()
    text_tags = parse_tags(" ".join(sections.get("TAGS", [])))
    dedicated_tags = parse_tags(
        tags_path.read_text(encoding="utf-8-sig")
    )

    if text_title != json_title:
        fail("Metadata text and JSON titles differ")
    if clean(text_description) != description:
        fail("Metadata text and JSON descriptions differ")

    expected_tags = {tag.casefold() for tag in normalized_tags}
    if {tag.casefold() for tag in text_tags} != expected_tags:
        fail("Metadata text tags differ from structured metadata")
    if {tag.casefold() for tag in dedicated_tags} != expected_tags:
        fail("Dedicated tags differ from structured metadata")

    return {
        "status": "valid",
        "title": json_title,
        "description_sha256": hashlib.sha256(
            description.encode("utf-8")
        ).hexdigest(),
        "tag_count": len(normalized_tags),
        "files": [
            relative(text_path),
            relative(json_path),
            relative(tags_path),
        ],
    }


def validate_mode_separation(
    mode: str,
    dimensions: tuple[int, int],
    fps: float,
) -> dict:
    checked = []
    roots = (
        OUT / "parts",
        OUT / "scene_renders",
        OUT / "videos",
    )

    for base in roots:
        if not base.exists():
            continue
        if base.is_symlink():
            fail(f"Output video directory is a symlink: {relative(base)}")

        for path in base.rglob("*"):
            if path.is_symlink():
                fail(f"Symlink in video output: {relative(path)}")
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue

            if path.name.endswith((
                ".tmp.mp4", ".rendering.mp4", ".joining.mp4",
            )):
                fail(f"Temporary video remains: {relative(path)}")

            info = validate_video(
                path,
                dimensions,
                fps,
                f"Mode separation {relative(path)}",
                require_audio=(path.parent.name != "photo_motion"),
            )
            checked.append({
                "path": relative(path),
                "dimensions": [info["width"], info["height"]],
                "fps": info["fps"],
                "duration": round(info["duration"], 4),
            })

    if not checked:
        fail("No video artifacts found for mode separation")

    return {
        "status": "valid",
        "format": mode,
        "dimensions": list(dimensions),
        "fps": fps,
        "video_files_checked": len(checked),
        "videos": checked,
    }


def validate_unique_output_files() -> dict:
    checked = []
    seen_paths = set()

    for base in (
        OUT / "parts",
        OUT / "scene_renders",
        OUT / "photo_motion",
    ):
        if not base.exists():
            continue
        if base.is_symlink():
            fail(f"Output directory is a symlink: {relative(base)}")

        for path in base.rglob("*"):
            if path.is_symlink():
                fail(f"Symlink found in output: {relative(path)}")
            if not path.is_file():
                continue

            resolved = path.resolve()
            if resolved in seen_paths:
                fail(f"Duplicate output file reference: {relative(path)}")
            seen_paths.add(resolved)

            if path.name.endswith((
                ".tmp", ".rendering.mp4", ".joining.mp4",
                ".building", ".part",
            )):
                fail(f"Temporary output file remains: {relative(path)}")

            checked.append(relative(path))

    return {
        "status": "valid",
        "files_checked": len(checked),
    }


def validate_package(
    part_numbers: list[int],
    mode: str,
    title: str,
) -> dict:
    if not PACKAGE_REPORT.is_file() or not PACKAGE_NAME.is_file():
        return {
            "status": "pending",
            "reason": "Package is created by the following packaging step.",
        }

    archive_name = PACKAGE_NAME.read_text(
        encoding="utf-8-sig"
    ).strip()
    if not archive_name or Path(archive_name).name != archive_name:
        fail("Package archive name is unsafe")

    report = read_json(PACKAGE_REPORT)
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
    if positive_int(
        report.get("configured_parts"), "Package configured PARTS"
    ) != len(part_numbers):
        fail("Package configured PARTS mismatch")
    if report.get("part_videos_only") is not True:
        fail("Package does not confirm Part-video-only output")
    if report.get("combined_video_included") is not False:
        fail("Package includes a combined video")
    if report.get("format_alias_included") is not False:
        fail("Package includes the opposite-format alias")

    expected_archive = f"katha-lok-{slug}-{mode}-output.zip"
    if archive_name != expected_archive or report.get("archive") != archive_name:
        fail("Package archive filename is not story-specific")

    archive_path = ROOT / archive_name
    if not archive_path.is_file() or archive_path.is_symlink():
        fail("Package archive is missing or unsafe")

    archive_hash = digest_file(archive_path)
    if report.get("archive_sha256") != archive_hash:
        fail("Package archive SHA-256 mismatch")
    if report.get("archive_bytes") != archive_path.stat().st_size:
        fail("Package archive byte count mismatch")

    file_rows = report.get("files")
    if not isinstance(file_rows, list) or not file_rows:
        fail("Package file hash list is missing")

    expected_hashes = {}
    for item in file_rows:
        if not isinstance(item, dict):
            fail("Invalid package file record")
        name = item.get("path")
        if not isinstance(name, str) or name in expected_hashes:
            fail("Invalid or duplicate package member")
        expected_hashes[name] = item

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
            member = PurePosixPath(name)
            if (
                member.is_absolute()
                or not member.parts
                or any(piece in {"", ".", ".."} for piece in member.parts)
                or "\\" in name
                or ":" in name
                or not name.startswith(prefix)
                or info.flag_bits & 0x1
                or info.file_size <= 0
            ):
                fail(f"Unsafe ZIP member: {name}")

        embedded_path = f"{root_name}/package_manifest.json"
        if embedded_path not in names:
            fail("Embedded package manifest is missing")
        if set(names) != set(expected_hashes):
            fail("ZIP members differ from package hash manifest")

        for name, item in expected_hashes.items():
            payload = archive.read(name)
            if (
                len(payload) != item.get("bytes")
                or hashlib.sha256(payload).hexdigest() != item.get("sha256")
            ):
                fail(f"ZIP member hash/size mismatch: {name}")

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

        expected_videos = {
            f"{root_name}/parts/{slug}-part-{part:02d}.mp4"
            for part in part_numbers
        }
        actual_videos = {
            name for name in names
            if PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS
        }
        if actual_videos != expected_videos:
            fail("ZIP contains missing, extra or incorrectly named videos")

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
            expected_path = (
                f"{root_name}/parts/{slug}-part-{part:02d}.mp4"
            )
            if (
                positive_int(row.get("part"), "Packaged Part") != part
                or row.get("path") != expected_path
            ):
                fail(f"Packaged Part {part} path mismatch")

    return {
        "status": "valid",
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

    selected = get_selected_parts(config, required=True)
    parts = positive_int(selected, "Selected PARTS")

    scenes_min, scenes_max = get_scenes_range(config)
    fps = parse_fps(get_fps(config), "Input FPS")
    dimensions = (720, 1280) if mode == "short" else (1280, 720)

    story = read_json(STORY)
    part_numbers = list(range(1, parts + 1))

    scene_counts, story_check = validate_story(
        story,
        part_numbers,
        scenes_min,
        scenes_max,
        mode,
    )
    verification_check = validate_story_verification(
        story, mode, parts
    )
    role_check = validate_extra_role_records(story, part_numbers)

    photo_check = validate_photo_motion(
        part_numbers, scene_counts, mode, dimensions, fps
    )
    narration_check = validate_narration(part_numbers, scene_counts)
    sound_check = validate_sound_design(
        part_numbers, scene_counts, mode
    )
    render_check = validate_render(
        part_numbers, scene_counts, mode, dimensions, fps
    )

    total_duration = sum(
        row["duration"] for row in render_check["parts"]
    )
    caption_check = validate_captions(
        part_numbers,
        total_duration,
        read_json(RENDER),
    )
    thumbnail_check = validate_thumbnail(mode, dimensions)
    metadata_check = validate_metadata(mode, story_check["title"])
    mode_check = validate_mode_separation(mode, dimensions, fps)
    unique_check = validate_unique_output_files()
    package_check = validate_package(
        part_numbers, mode, story_check["title"]
    )

    checks = {
        "story": {"status": "valid", **story_check},
        "story_verification": {"status": "valid", **verification_check},
        "story_roles": {"status": "valid", **role_check},
        "photo_motion": {"status": "valid", **photo_check},
        "narration": {"status": "valid", **narration_check},
        "sound_design": {"status": "valid", **sound_check},
        "render": {"status": "valid", **render_check},
        "captions": {"status": "valid", **caption_check},
        "thumbnail": {"status": "valid", **thumbnail_check},
        "metadata": {"status": "valid", **metadata_check},
        "mode_separation": {"status": "valid", **mode_check},
        "unique_output_files": {"status": "valid", **unique_check},
    }

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "story_title": story_check["title"],
        "parts": parts,
        "configured_parts": parts,
        "part_numbers": part_numbers,
        "scenes_range": [scenes_min, scenes_max],
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_per_part": scenes_min,
        "actual_scene_counts": {
            str(part): scene_counts[part] for part in part_numbers
        },
        "expected_scenes": sum(scene_counts.values()),
        "total_main_scenes": sum(scene_counts.values()),
        "total_role_clips": (
            sum(scene_counts.values()) + parts * 2
        ),
        "resolution": list(dimensions),
        "fps": fps,
        "words_per_part": story_check["words_per_part"],
        "total_story_words": story_check["total_words"],
        "final_part_merged": story_check["final_part_merged"],
        "checks": checks,
        "failed_checks": [],
        "package_status": package_check["status"],
        "package": package_check,
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    write_json(REPORT, report)
    saved_report = read_json(REPORT)
    if not isinstance(saved_report, dict):
        fail("Final validation report read-back failed")
    if (
        saved_report.get("status") != "completed"
        or saved_report.get("format") != mode
        or saved_report.get("story_title") != story_check["title"]
        or saved_report.get("checks") != checks
    ):
        fail("Final validation report read-back integrity mismatch")

    log("=" * 64)
    log("FINAL OUTPUT VALIDATION PASSED")
    log("FORMAT:", mode)
    log("STORY:", story_check["title"])
    log("PARTS:", part_numbers)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("MAIN SCENES:", sum(scene_counts.values()))
    log("ALL ROLE CLIPS:", report["total_role_clips"])
    log("RESOLUTION:", dimensions)
    log("FPS:", fps)
    log("PACKAGE:", package_check["status"])
    log("REPORT:", relative(REPORT))
    log("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("FINAL OUTPUT VALIDATION FAILED:", exc)
        raise SystemExit(1)