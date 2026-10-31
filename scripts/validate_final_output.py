#!/usr/bin/env python3
"""Strict final deliverable validator for Katha Lok AI Photo Motion."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
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

PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
CAPTIONS = OUT / "captions" / "captions.json"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX = OUT / "sound_design" / "sfx_cues.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
SRT = OUT / "captions" / "final_captions.srt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v40-complete-deliverable-audit"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MAX_FFPROBE_SECONDS = 90
SRT_TIMESTAMP = re.compile(
    r"^(?P<h>\d{2,}):(?P<m>[0-5]\d):"
    r"(?P<s>[0-5]\d),(?P<ms>\d{3})$"
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path, optional: bool = False) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if optional:
            return None
        fail(f"Required JSON missing: {path.relative_to(ROOT)}")
    except (OSError, UnicodeError, ValueError) as exc:
        fail(f"Cannot read JSON {path.relative_to(ROOT)}: {exc}")


def write_report(data: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(REPORT)
    finally:
        temporary.unlink(missing_ok=True)


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


def safe_file(value: Any, label: str, minimum: int = 1) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        fail(f"{label}: path escapes repository")

    if not path.is_file():
        fail(f"{label}: file not found: {path}")

    if path.stat().st_size < minimum:
        fail(f"{label}: file is empty or too small: {path.name}")

    return path


def cleanup_opposite_format_alias(mode: str) -> None:
    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT for alias cleanup: {mode}")

    opposite = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    for candidate in (OUT / "videos" / opposite, OUT / opposite):
        if not candidate.exists():
            continue

        resolved = candidate.resolve()
        if not resolved.is_relative_to(OUT.resolve()):
            fail(f"Unsafe opposite-format alias: {candidate}")
        if not resolved.is_file():
            fail(f"Opposite-format alias is not a regular file: {candidate}")

        resolved.unlink()
        log("REMOVED STALE OPPOSITE-FORMAT ALIAS:", candidate)


def media_info(path: Path) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        fail("ffprobe is not installed")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=MAX_FFPROBE_SECONDS,
        check=False,
    )

    if result.returncode != 0:
        fail(
            f"ffprobe failed for {path.name}: "
            f"{result.stderr[-600:]}"
        )

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

    return {
        "duration": duration,
        "video": video is not None,
        "audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def validate_video(path: Path, expected_size: tuple, label: str) -> dict:
    info = media_info(path)
    if not info["video"] or not info["audio"]:
        fail(f"{label}: audio or video stream missing")

    actual = (info["width"], info["height"])
    if actual != expected_size:
        fail(
            f"{label}: expected {expected_size[0]}x{expected_size[1]}, "
            f"got {actual[0]}x{actual[1]}"
        )
    return info


def expected_scene_keys(parts: int, scenes: int) -> list:
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def validate_scene_rows(
    items: Any, parts: int, scenes: int, label: str
) -> dict:
    expected = expected_scene_keys(parts, scenes)
    if not isinstance(items, list):
        fail(f"{label}: scene list missing")
    if len(items) != len(expected):
        fail(
            f"{label}: expected {len(expected)} scenes, "
            f"found {len(items)}"
        )

    mapping = {}
    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid scene row {index}")

        try:
            part = int(row["part"])
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError):
            fail(f"{label}: invalid scene mapping at row {index}")

        wanted = expected[index - 1]
        if (part, scene) != wanted:
            fail(
                f"{label}: wrong scene order at row {index}; "
                f"expected {wanted}, found {(part, scene)}"
            )
        if global_scene != index:
            fail(f"{label}: global scene number mismatch at row {index}")

        key = (part, scene)
        if key in mapping:
            fail(f"{label}: duplicate scene mapping {key}")
        mapping[key] = row

    return mapping


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


def validate_srt(path: Path, expected_duration: float) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read SRT: {exc}")

    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        fail("Final SRT is empty")

    blocks = re.split(r"\n[ \t]*\n", content)
    previous_end = -1.0
    maximum_end = 0.0
    total_chars = 0

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index}: invalid cue structure")
        if int(lines[0].strip()) != index:
            fail(f"SRT cue sequence error at cue {index}")
        if lines[1].count("-->") != 1:
            fail(f"SRT cue {index}: invalid timing line")

        start_text, end_text = [
            item.strip() for item in lines[1].split("-->")
        ]
        start = srt_seconds(start_text)
        end = srt_seconds(end_text)

        if end <= start:
            fail(f"SRT cue {index}: end must be after start")
        if start < previous_end - 0.001:
            fail(f"SRT cue {index}: overlapping or out-of-order timing")

        caption = "\n".join(lines[2:]).strip()
        if not caption:
            fail(f"SRT cue {index}: caption text missing")
        if "-->" in caption:
            fail(f"SRT cue {index}: timing delimiter found in caption text")

        tags = re.findall(r"</?([A-Za-z]+)(?:\s+[^>]*)?>", caption)
        if any(tag.lower() not in {"b", "i", "u", "font"} for tag in tags):
            fail(f"SRT cue {index}: unsupported markup")

        previous_end = end
        maximum_end = max(maximum_end, end)
        total_chars += len(caption)

    if maximum_end > expected_duration + 0.25:
        fail(
            f"SRT ends at {maximum_end:.3f}s, "
            f"after video duration {expected_duration:.3f}s"
        )

    if maximum_end < expected_duration * 0.50:
        fail(
            "SRT coverage is unexpectedly short compared with final video; "
            "check caption timing and missing cues."
        )

    return {
        "cue_count": len(blocks),
        "text_characters": total_chars,
        "last_cue_end": round(maximum_end, 3),
        "overlap_check": "passed",
        "sequence_check": "passed",
        "timestamp_check": "passed",
        "duration_check": "passed",
    }


def declared_part_count(path: Path, label: str) -> int | None:
    data = read_json(path, optional=True)
    if data is None:
        return None
    if not isinstance(data, dict):
        fail(f"{label}: manifest must be a JSON object")

    for field in ("total_parts", "actual_parts", "part_count", "parts_count"):
        value = data.get(field)
        if value is not None:
            try:
                count = int(value)
            except (TypeError, ValueError):
                fail(f"{label}: invalid {field}")
            if count < 1:
                fail(f"{label}: {field} must be positive")
            return count

    for field in (
        "scenes", "completed_scenes", "jobs", "audio_jobs",
        "captions", "items",
    ):
        items = data.get(field)
        if not isinstance(items, list) or not items:
            continue

        numbers = set()
        for index, row in enumerate(items, 1):
            if not isinstance(row, dict) or row.get("part") is None:
                numbers = set()
                break
            try:
                number = int(row["part"])
            except (TypeError, ValueError):
                fail(f"{label}: invalid part number in {field}[{index - 1}]")
            if number < 1:
                fail(f"{label}: part number must be positive")
            numbers.add(number)

        if numbers:
            if numbers != set(range(1, max(numbers) + 1)):
                fail(f"{label}: part numbering is not continuous")
            return len(numbers)

    part_rows = data.get("parts")
    if isinstance(part_rows, list) and part_rows:
        return len(part_rows)

    return None


def resolve_actual_parts(mode: str, configured_parts: int) -> int:
    specs = (
        (AUDIO, "Narration"),
        (CAPTIONS, "Captions"),
        (PHOTO, "Photo-motion"),
        (RENDER, "Render"),
    )
    discovered = [
        (label, count)
        for path, label in specs
        if (count := declared_part_count(path, label)) is not None
    ]

    if mode == "full":
        for label, count in discovered:
            if count != configured_parts:
                fail(
                    f"FULL {label} manifest has {count} parts; "
                    f"input requires {configured_parts}"
                )
        return configured_parts

    if not discovered:
        fail(
            "SHORT part count cannot be determined from upstream manifests."
        )

    counts = {count for _, count in discovered}
    if len(counts) != 1:
        detail = ", ".join(f"{label}={count}" for label, count in discovered)
        fail(f"SHORT part-count mismatch: {detail}")

    actual = counts.pop()
    log("SHORT configured PARTS (ignored):", configured_parts)
    log("SHORT actual parts:", actual)
    return actual

def validate_photo(mode: str, parts: int, scenes: int) -> dict:
    data = read_json(PHOTO)
    if not isinstance(data, dict):
        fail("Photo-motion manifest must be a JSON object")
    if data.get("status") != "completed":
        fail("Photo-motion manifest is not completed")
    if str(data.get("format", "")).lower() != mode:
        fail("Photo-motion FORMAT mismatch")

    expected_count = parts * scenes
    for field in ("expected_scene_count", "completed_count"):
        try:
            actual = int(data.get(field, -1))
        except (TypeError, ValueError):
            actual = -1
        if actual != expected_count:
            fail(
                f"Photo-motion {field}: expected {expected_count}, "
                f"found {actual}"
            )

    mapping = validate_scene_rows(
        rows(data, "scenes", "completed_scenes"),
        parts, scenes, "Photo-motion",
    )

    checked = 0
    for key, row in mapping.items():
        if row.get("status") != "completed":
            fail(f"Photo-motion scene incomplete: {key}")

        path = safe_file(
            pick(row, "video", "source_video", "output", "path"),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )
        if not media_info(path)["video"]:
            fail(f"Photo-motion scene has no video stream: {key}")

        declared_hash = row.get("video_sha256") or row.get("sha256")
        if declared_hash and declared_hash != sha256(path):
            fail(f"Photo-motion hash mismatch: {key}")
        checked += 1

    return {"status": "valid", "scene_count": checked}


def validate_audio(parts: int, scenes: int) -> dict:
    data = read_json(AUDIO)
    if not isinstance(data, dict):
        fail("Narration audio manifest must be a JSON object")
    if data.get("status") != "completed":
        fail("Narration audio manifest is not completed")

    jobs = rows(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        fail("Narration audio jobs are missing")

    expected = set(expected_scene_keys(parts, scenes))
    found = set()

    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            fail(f"Invalid narration job {index}")

        try:
            key = (
                int(row["part"]),
                int(row.get("scene", row.get("local_scene", 0))),
            )
        except (KeyError, TypeError, ValueError):
            fail(f"Invalid narration scene mapping at row {index}")

        if key in found:
            fail(f"Duplicate narration scene: {key}")
        if key not in expected:
            fail(f"Unexpected narration scene: {key}")
        if row.get("status") != "completed":
            fail(f"Narration scene incomplete: {key}")

        path = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        if not media_info(path)["audio"]:
            fail(f"Narration file has no audio stream: {key}")

        declared_hash = row.get("sha256")
        if declared_hash and declared_hash != sha256(path):
            fail(f"Narration hash mismatch: {key}")

        found.add(key)

    if found != expected:
        fail(
            "Narration scene mapping mismatch; "
            f"missing={sorted(expected - found)}"
        )

    return {"status": "valid", "scene_count": len(found)}


def validate_captions(
    mode: str,
    parts: int,
    scenes: int,
    final_duration: float,
) -> dict:
    data = read_json(CAPTIONS)
    if not isinstance(data, dict):
        fail("Caption manifest must be a JSON object")
    if data.get("status") != "completed":
        fail("Caption manifest is not completed")
    if str(data.get("format", "")).lower() != mode:
        fail("Caption FORMAT mismatch")

    caption_rows = rows(data, "captions")
    if not caption_rows:
        fail("Caption rows are missing")

    expected = set(expected_scene_keys(parts, scenes))
    found = set()

    for index, row in enumerate(caption_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid caption row {index}")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            fail(f"Invalid caption mapping at row {index}")

        if key in found:
            fail(f"Duplicate caption scene: {key}")

        caption_text = row.get("text")
        if not isinstance(caption_text, str) or not caption_text.strip():
            fail(f"Caption text missing: {key}")

        found.add(key)

    if found != expected:
        fail(
            "Caption scene mapping mismatch; "
            f"missing={sorted(expected - found)}"
        )

    srt = safe_file(
        SRT.relative_to(ROOT).as_posix(),
        "Final SRT",
        10,
    )
    srt_result = validate_srt(srt, final_duration)

    return {
        "status": "valid",
        "scene_count": len(found),
        "srt": srt.relative_to(ROOT).as_posix(),
        "srt_sha256": sha256(srt),
        **srt_result,
    }


def validate_sound(config: dict) -> dict:
    data = read_json(SOUND)
    if not isinstance(data, dict):
        fail("Sound-design manifest must be a JSON object")
    if data.get("status") != "completed":
        fail("Sound-design manifest is not completed")

    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        fail("Sound-design tracks object missing")

    enabled_tracks = {
        "music": bool(config.get("MUSIC", True)),
        "ambience": bool(config.get("AMBIENT_SOUND", True)),
    }
    checked_tracks = {}

    for name, record in tracks.items():
        normalized = str(name).strip().lower()

        if record is None:
            checked_tracks[name] = "disabled"
            if enabled_tracks.get(normalized, False):
                fail(f"Sound track {name} is enabled in input but disabled")
            continue

        if not isinstance(record, dict):
            fail(f"Invalid sound track record: {name}")
        if record.get("status") != "completed":
            fail(f"Sound track incomplete: {name}")

        path = safe_file(
            pick(record, "path", "file", "output", "audio_path"),
            f"Sound track {name}",
            MIN_AUDIO_BYTES,
        )
        if not media_info(path)["audio"]:
            fail(f"Sound track has no audio stream: {name}")

        declared_hash = record.get("sha256")
        if declared_hash and declared_hash != sha256(path):
            fail(f"Sound track hash mismatch: {name}")

        checked_tracks[name] = path.relative_to(ROOT).as_posix()

    for name, enabled in enabled_tracks.items():
        if enabled and name not in {
            str(key).strip().lower() for key in tracks
        }:
            fail(f"Enabled sound track missing from manifest: {name}")

    cue_data = read_json(SFX, optional=True)
    cue_count = 0

    if cue_data is not None:
        if not isinstance(cue_data, dict):
            fail("SFX cue manifest must be a JSON object")
        if cue_data.get("status") != "completed":
            fail("SFX cue manifest is not completed")

        for index, cue in enumerate(rows(cue_data, "cues", "items"), 1):
            if not isinstance(cue, dict):
                fail(f"Invalid SFX cue row {index}")

            try:
                part = int(cue["part"])
                scene = int(cue["scene"])
                timestamp = float(cue.get("time", 0))
                gain = float(cue.get("gain", 0))
            except (KeyError, TypeError, ValueError):
                fail(f"Invalid SFX timing row {index}")

            if part < 1 or scene < 1:
                fail(f"Invalid SFX scene mapping at row {index}")
            if not math.isfinite(timestamp) or timestamp < 0:
                fail(f"Invalid SFX timestamp at row {index}")
            if not math.isfinite(gain) or gain < 0:
                fail(f"Invalid SFX gain at row {index}")

            path = safe_file(
                pick(cue, "file", "path", "audio", "output"),
                f"SFX cue {index}",
                MIN_AUDIO_BYTES,
            )
            if not media_info(path)["audio"]:
                fail(f"SFX cue has no audio stream: {index}")
            cue_count += 1

    elif bool(config.get("SFX", True)):
        log("WARNING: SFX is enabled but no separate SFX cue manifest exists.")

    return {
        "status": "valid",
        "tracks": checked_tracks,
        "sfx_cue_count": cue_count,
    }


def validate_render(
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple,
    expected_fps: float,
) -> dict:
    data = read_json(RENDER)
    if not isinstance(data, dict):
        fail("Render manifest must be a JSON object")
    if data.get("status") != "completed":
        fail("Render manifest is not completed")
    if str(data.get("format", "")).lower() != mode:
        fail("Render manifest FORMAT mismatch")
    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions")

    try:
        manifest_fps = float(data.get("fps"))
    except (TypeError, ValueError):
        fail("Render manifest FPS is missing or invalid")

    if not math.isfinite(manifest_fps) or abs(manifest_fps - expected_fps) > 0.05:
        fail(
            f"Render FPS mismatch: expected {expected_fps}, "
            f"found {manifest_fps}"
        )

    expected_count = parts * scenes
    try:
        completed_count = int(data.get("completed_count", -1))
    except (TypeError, ValueError):
        completed_count = -1

    if completed_count != expected_count:
        fail(
            f"Render completed_count mismatch: "
            f"{completed_count}/{expected_count}"
        )

    mapping = validate_scene_rows(
        rows(data, "completed_scenes", "scenes"),
        parts, scenes, "Final render",
    )

    scene_total = 0.0
    scene_records = []

    for key, row in mapping.items():
        if row.get("status") != "completed":
            fail(f"Rendered scene incomplete: {key}")
        if row.get("captions_burned") is not True:
            fail(f"Captions not burned into scene: {key}")

        path = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Rendered scene {key}",
            MIN_VIDEO_BYTES,
        )
        info = validate_video(path, expected_size, f"Rendered scene {key}")
        scene_total += info["duration"]

        declared_hash = row.get("sha256")
        actual_hash = sha256(path)
        if declared_hash and declared_hash != actual_hash:
            fail(f"Rendered scene hash mismatch: {key}")

        scene_records.append({
            "part": key[0],
            "scene": key[1],
            "duration": round(info["duration"], 3),
            "sha256": actual_hash,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Expected exactly {parts} part videos")

    part_total = 0.0
    part_records = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                part_number = int(row.get("part", index))
            except (TypeError, ValueError):
                fail(f"Invalid part number: {index}")
            if part_number != index:
                fail(f"Part video order mismatch: {index}")
            if row.get("status") not in (None, "completed"):
                fail(f"Part video incomplete: {index}")
            value = pick(row, "path", "video", "output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = validate_video(path, expected_size, f"Part video {index}")
        part_total += info["duration"]

        part_records.append({
            "part": index,
            "duration": round(info["duration"], 3),
            "sha256": sha256(path),
        })

    final = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias = safe_file(data.get("alias"), "Format alias", MIN_VIDEO_BYTES)

    final_info = validate_video(final, expected_size, "Final video")
    alias_info = validate_video(alias, expected_size, "Format alias")

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        fail(f"Wrong selected-format alias: {alias.name}")
    if final.resolve() == alias.resolve():
        fail("Final video and alias must be separate files")

    final_hash = sha256(final)
    alias_hash = sha256(alias)
    if final_hash != alias_hash:
        fail("Final video and format alias content hashes differ")
    if abs(final_info["duration"] - alias_info["duration"]) > 1:
        fail("Final video and alias durations differ")

    duration_tolerance = max(2.0, parts * 1.0)
    if abs(final_info["duration"] - part_total) > duration_tolerance:
        fail("Final video duration does not match part durations")
    if abs(final_info["duration"] - scene_total) > max(2.0, expected_count):
        fail("Final video duration does not match scene durations")

    return {
        "status": "valid",
        "scene_count": expected_count,
        "part_count": parts,
        "final_video": final.relative_to(ROOT).as_posix(),
        "final_video_sha256": final_hash,
        "alias": alias.relative_to(ROOT).as_posix(),
        "duration": round(final_info["duration"], 3),
        "resolution": list(expected_size),
        "scene_records": scene_records,
        "part_records": part_records,
        "final_duration": final_info["duration"],
    }


def validate_thumbnail(mode: str) -> dict:
    path = safe_file(
        THUMBNAIL.relative_to(ROOT).as_posix(),
        "Thumbnail",
        MIN_VIDEO_BYTES,
    )

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format
    except Exception as exc:
        fail(f"Invalid thumbnail: {exc}")

    if image_format != "JPEG" or dimensions != (1280, 720):
        fail(
            f"Thumbnail must be 1280x720 JPEG; "
            f"got {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST, optional=True)
    if manifest is not None:
        if not isinstance(manifest, dict):
            fail("Thumbnail manifest must be a JSON object")
        if manifest.get("status") != "completed":
            fail("Thumbnail manifest is not completed")
        if str(manifest.get("format", "")).lower() != mode:
            fail("Thumbnail manifest FORMAT mismatch")

        declared = pick(manifest, "thumbnail", "path", "output")
        if declared:
            declared_path = safe_file(declared, "Thumbnail manifest path")
            if declared_path.resolve() != path.resolve():
                fail("Thumbnail manifest points to a different file")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "resolution": list(dimensions),
    }


def parse_metadata_text(path: Path) -> dict:
    content = path.read_text(encoding="utf-8-sig").strip()
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
            sections[current].append(line.strip())

    title = " ".join(sections.get("TITLE", [])).strip()
    description = " ".join(sections.get("DESCRIPTION", [])).strip()
    tags_text = " ".join(sections.get("TAGS", [])).strip()

    if not title:
        fail("Metadata TITLE is empty")
    if not description:
        fail("Metadata DESCRIPTION is empty")
    if not tags_text:
        fail("Metadata TAGS are empty")
    if len(title) > 100:
        fail(f"Metadata title exceeds 100 characters: {len(title)}")

    return {
        "title": title,
        "description": description,
        "tags_text": tags_text,
    }


def validate_metadata(mode: str) -> dict:
    path = safe_file(
        METADATA.relative_to(ROOT).as_posix(),
        "YouTube metadata",
        10,
    )
    parsed = parse_metadata_text(path)

    structured = read_json(METADATA_JSON, optional=True)
    if structured is not None:
        if not isinstance(structured, dict):
            fail("Structured YouTube metadata must be a JSON object")
        if structured.get("status") != "completed":
            fail("Structured YouTube metadata is not completed")
        if str(structured.get("format", "")).lower() != mode:
            fail("Structured metadata FORMAT mismatch")

        title = structured.get("title")
        description = structured.get("description")
        tags = structured.get("tags")

        if not isinstance(title, str) or not title.strip():
            fail("Structured metadata title is missing")
        if not isinstance(description, str) or not description.strip():
            fail("Structured metadata description is missing")
        if not isinstance(tags, list) or not tags:
            fail("Structured metadata tags must be a non-empty list")
        if len(title) > 100:
            fail("Structured metadata title exceeds 100 characters")
        if len(tags) > 20:
            fail("Structured metadata has more than 20 tags")

        normalized_tags = []
        seen = set()
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip():
                fail("Structured metadata contains an invalid tag")
            normalized = re.sub(r"\s+", " ", tag).strip()
            if len(normalized) > 100:
                fail("Structured metadata tag exceeds 100 characters")
            key = normalized.casefold()
            if key not in seen:
                seen.add(key)
                normalized_tags.append(normalized)

        if len(normalized_tags) < 8:
            fail("Structured metadata has fewer than 8 unique tags")

        if title.strip() != parsed["title"]:
            fail("Metadata text title differs from structured JSON")
        if description.strip() != parsed["description"]:
            fail("Metadata text description differs from structured JSON")

        text_tags = {
            item.strip().lstrip("#").casefold()
            for item in re.split(r"[,;\n]", parsed["tags_text"])
            if item.strip()
        }
        json_tags = {item.casefold() for item in normalized_tags}
        if text_tags != json_tags:
            fail("Metadata text tags differ from structured JSON")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "title_length": len(parsed["title"]),
        "description_length": len(parsed["description"]),
        "tags_length": len(parsed["tags_text"]),
        "structured_json": (
            METADATA_JSON.relative_to(ROOT).as_posix()
            if structured is not None else None
        ),
    }


def main() -> None:
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            fail(f"{command} is required")

    if not OUT.is_dir():
        fail("output/ directory missing")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    expected_fps = float(get_fps(config))

    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")
    if configured_parts < 1 or scenes < 1:
        fail("PARTS and SCENES must be positive integers")
    if not math.isfinite(expected_fps) or expected_fps <= 0:
        fail(f"Invalid FPS: {expected_fps}")

    expected_size = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    log("VALIDATION VERSION:", VERSION)
    log("FORMAT:", mode)
    log("CONFIGURED PARTS:", configured_parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED FPS:", expected_fps)

    parts = resolve_actual_parts(mode, configured_parts)
    log("ACTUAL PARTS:", parts)
    log("EXPECTED SCENES:", parts * scenes)

    cleanup_opposite_format_alias(mode)

    checks = {}
    checks["photo_motion"] = validate_photo(mode, parts, scenes)
    checks["narration"] = validate_audio(parts, scenes)
    checks["sound_design"] = validate_sound(config)

    checks["render"] = validate_render(
        mode, parts, scenes, expected_size, expected_fps
    )
    final_duration = checks["render"]["final_duration"]

    checks["captions"] = validate_captions(
        mode, parts, scenes, final_duration
    )
    checks["thumbnail"] = validate_thumbnail(mode)
    checks["metadata"] = validate_metadata(mode)

    required_checks = (
        "photo_motion",
        "narration",
        "sound_design",
        "render",
        "captions",
        "thumbnail",
        "metadata",
    )

    for name in required_checks:
        if checks[name].get("status") != "valid":
            fail(f"Validation check did not pass: {name}")

    checks["render"].pop("final_duration", None)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "configured_parts": configured_parts,
        "dynamic_short_parts": mode == "short",
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "resolution": list(expected_size),
        "checks": checks,
        "limitations": [
            "Media streams, resolution, duration, hashes and scene order are checked.",
            "SRT timestamps, sequence, overlaps and final-video duration are checked.",
            "SRT validation cannot prove word-level speech alignment.",
            "Thumbnail validation checks file integrity and dimensions, not artistic quality.",
            "Metadata validation checks structure and file consistency, not search ranking.",
            "Manifest validation cannot judge subjective story quality or visual realism.",
        ],
    }

    write_report(report)

    log("=" * 60)
    log("FINAL VALIDATION: PASSED")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES:", parts * scenes)
    log("CHECKS:", ", ".join(required_checks))
    log("REPORT:", REPORT.relative_to(ROOT))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FINAL VALIDATION FAILED:", str(exc))
        sys.exit(1)