#!/usr/bin/env python3
"""Strict final-output validation for Katha Lok AI Photo Motion."""

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

PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
CAPTIONS = OUT / "captions" / "captions.json"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX = OUT / "sound_design" / "sfx_cues.json"
SFX_VALIDATION = OUT / "sound_design" / "sfx_timing_validation.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
METADATA_JSON = OUT / "publish" / "youtube_metadata.json"
METADATA_TAGS = OUT / "publish" / "youtube_tags.txt"
SRT = OUT / "captions" / "final_captions.srt"
REPORT = OUT / "validation_report.json"
ARCHIVE = ROOT / "katha-lok-ai-photo-motion-output.zip"

VERSION = "final-validator-v60-role-aware-strict-deliverables"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
FPS_TOLERANCE = 0.05
DURATION_TOLERANCE = 2.0
PROBE_TIMEOUT = 120

REQUIRED_CHECKS = (
    "photo_motion",
    "narration",
    "sound_design",
    "render",
    "captions",
    "thumbnail",
    "metadata",
)

SRT_TIMESTAMP = re.compile(
    r"^(?P<h>\d{2,}):(?P<m>[0-5]\d):"
    r"(?P<s>[0-5]\d),(?P<ms>\d{3})$"
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


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


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, optional: bool = False) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if optional:
            return None
        fail(f"Required JSON missing: {relative(path)}")
    except (OSError, UnicodeError, ValueError) as exc:
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


def safe_file(value: Any, label: str, minimum: int = 1) -> Path:
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


def parse_fps(value: Any, label: str) -> float:
    try:
        result = float(Fraction(str(value)))
    except (ValueError, TypeError, ZeroDivisionError):
        fail(f"{label}: invalid FPS value {value!r}")
    if not math.isfinite(result) or result <= 0:
        fail(f"{label}: invalid FPS value {value!r}")
    return result


def media_info(path: Path) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        fail("ffprobe is required but not installed")

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
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
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


def expected_scene_keys(parts: int, scenes: int) -> list:
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def scene_key(row: dict, label: str) -> tuple[int, int]:
    try:
        return (
            int(row["part"]),
            int(row.get("local_scene", row.get("scene", 0))),
        )
    except (KeyError, TypeError, ValueError):
        fail(f"{label}: invalid part/scene mapping")


def validate_scene_rows(
    items: Any,
    parts: int,
    scenes: int,
    label: str,
) -> dict:
    expected = expected_scene_keys(parts, scenes)
    if not isinstance(items, list):
        fail(f"{label}: scene list missing")
    if len(items) != len(expected):
        fail(
            f"{label}: expected {len(expected)} scenes, found {len(items)}"
        )

    mapping = {}
    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid scene row {index}")
        key = scene_key(row, label)
        try:
            global_scene = int(row.get("global_scene", index))
        except (TypeError, ValueError):
            fail(f"{label}: invalid global scene number at row {index}")
        if key != expected[index - 1]:
            fail(
                f"{label}: wrong scene order at row {index}; "
                f"expected {expected[index - 1]}, got {key}"
            )
        if global_scene != index:
            fail(f"{label}: global scene mismatch at row {index}")
        if key in mapping:
            fail(f"{label}: duplicate scene {key}")
        mapping[key] = row
    return mapping


def declared_part_count(path: Path, label: str) -> int | None:
    data = read_json(path, optional=True)
    if data is None:
        return None
    if not isinstance(data, dict):
        fail(f"{label}: manifest must be a JSON object")

    for field in (
        "total_parts", "actual_parts", "part_count", "parts_count"
    ):
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
        "scenes", "completed_scenes", "all_completed_scenes",
        "jobs", "audio_jobs", "captions", "items",
    ):
        items = data.get(field)
        if not isinstance(items, list) or not items:
            continue
        numbers = set()
        for index, row in enumerate(items):
            if not isinstance(row, dict) or row.get("part") is None:
                numbers = set()
                break
            try:
                number = int(row["part"])
            except (TypeError, ValueError):
                fail(f"{label}: invalid part in {field}[{index}]")
            if number < 1:
                fail(f"{label}: non-positive part number")
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
    sources = (
        (AUDIO, "Narration"),
        (CAPTIONS, "Captions"),
        (PHOTO, "Photo-motion"),
        (RENDER, "Render"),
    )
    discovered = []
    for path, label in sources:
        count = declared_part_count(path, label)
        if count is not None:
            discovered.append((label, count))

    if mode == "full":
        for label, count in discovered:
            if count != configured_parts:
                fail(
                    f"FULL {label} manifest has {count} parts; "
                    f"configured PARTS={configured_parts}"
                )
        return configured_parts

    if not discovered:
        fail("SHORT part count cannot be determined from manifests")
    counts = {count for _, count in discovered}
    if len(counts) != 1:
        detail = ", ".join(f"{label}={count}" for label, count in discovered)
        fail(f"SHORT part-count mismatch: {detail}")
    actual = counts.pop()
    log("SHORT configured PARTS:", configured_parts)
    log("SHORT actual parts:", actual)
    return actual


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


def validate_photo(mode: str, parts: int, scenes: int) -> dict:
    data = read_json(PHOTO)
    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Photo-motion manifest is missing or incomplete")
    if str(data.get("format", "")).lower() != mode:
        fail("Photo-motion FORMAT mismatch")

    expected_count = parts * scenes
    mapping = validate_scene_rows(
        rows(data, "scenes", "completed_scenes"),
        parts, scenes, "Photo-motion",
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
            fail(f"Photo-motion scene dimensions invalid: {key}")
        digest = sha256(path)
        declared = row.get("video_sha256") or row.get("sha256")
        if declared and declared != digest:
            fail(f"Photo-motion SHA256 mismatch: {key}")
        records.append({
            "part": key[0], "scene": key[1],
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": digest,
        })

    for field in ("expected_scene_count", "completed_count"):
        if field in data and int(data[field]) != expected_count:
            fail(f"Photo-motion {field} mismatch")

    return {"status": "valid", "scene_count": expected_count, "scenes": records}


def validate_narration(parts: int, scenes: int) -> dict:
    data = read_json(AUDIO)
    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Narration manifest is missing or incomplete")

    jobs = rows(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        fail("Narration audio jobs are missing")

    expected = set(expected_scene_keys(parts, scenes))
    found = set()
    records = []
    total_duration = 0.0

    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            fail(f"Invalid narration job {index}")
        key = scene_key(row, "Narration")
        if key not in expected or key in found:
            fail(f"Unexpected or duplicate narration scene: {key}")
        if row.get("status") not in {"completed", "reused"}:
            fail(f"Narration scene incomplete: {key}")

        asset = validate_audio_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration scene {key}",
        )
        declared = row.get("sha256")
        if declared and declared != asset["sha256"]:
            fail(f"Narration SHA256 mismatch: {key}")

        duration = asset["duration"]
        total_duration += duration
        found.add(key)
        records.append({
            "part": key[0], "scene": key[1], **asset,
        })

    if found != expected:
        fail(
            "Narration scene mapping incomplete; "
            f"missing={sorted(expected - found)}"
        )

    # Reject obviously truncated or empty narration. The per-scene
    # duration floor also prevents tiny placeholder audio from passing.
    short_scenes = [
        item for item in records if item["duration"] < 0.75
    ]
    if short_scenes:
        fail(
            "Narration contains suspiciously short scene audio: "
            + str([(x["part"], x["scene"], x["duration"])
                   for x in short_scenes])
        )

    return {
        "status": "valid",
        "scene_count": len(found),
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


def validate_srt(path: Path, expected_duration: float) -> dict:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read final SRT: {exc}")

    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        fail("Final SRT is empty")

    blocks = re.split(r"\n[ \t]*\n", content)
    previous_end = -1.0
    maximum_end = 0.0
    characters = 0

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail(f"SRT cue {index}: incomplete block")
        if int(lines[0].strip()) != index:
            fail(f"SRT cue {index}: sequence is not continuous")
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
            fail(f"SRT cue {index}: overlapping or unordered timing")

        caption = "\n".join(lines[2:]).strip()
        if not caption or "-->" in caption:
            fail(f"SRT cue {index}: caption text missing")

        tags = re.findall(r"</?([A-Za-z]+)(?:\s+[^>]*)?>", caption)
        if any(tag.lower() not in {"b", "i", "u", "font"} for tag in tags):
            fail(f"SRT cue {index}: unsupported markup")

        previous_end = end
        maximum_end = max(maximum_end, end)
        characters += len(caption)

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
        "duration_check": "passed",
    }


def validate_captions(
    mode: str,
    parts: int,
    scenes: int,
    final_duration: float,
) -> dict:
    data = read_json(CAPTIONS)
    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Caption manifest is missing or incomplete")
    if str(data.get("format", "")).lower() != mode:
        fail("Caption FORMAT mismatch")

    caption_rows = rows(data, "captions", "scenes")
    if not caption_rows:
        fail("Caption rows are missing")

    expected = set(expected_scene_keys(parts, scenes))
    found = set()
    for index, row in enumerate(caption_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid caption row {index}")
        key = scene_key(row, "Captions")
        if key not in expected or key in found:
            fail(f"Unexpected or duplicate caption scene: {key}")
        value = row.get("text", row.get("caption", row.get("caption_text")))
        if not isinstance(value, str) or not value.strip():
            fail(f"Caption text missing or empty: {key}")
        found.add(key)

    if found != expected:
        fail(
            "Caption mapping incomplete; "
            f"missing={sorted(expected - found)}"
        )

    srt = safe_file(relative(SRT), "Final SRT", 10)
    srt_report = validate_srt(srt, final_duration)
    return {
        "status": "valid",
        "scene_count": len(found),
        "srt": relative(srt),
        "srt_sha256": sha256(srt),
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


def validate_sound(config: dict) -> dict:
    data = read_json(SOUND)
    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Sound-design manifest is missing or incomplete")

    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        fail("Sound-design tracks object missing")

    enabled = {
        "music": config_enabled(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC",
             "SOUNDTRACK_ENABLED"),
        ),
        "ambient": config_enabled(
            config,
            ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
             "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED"),
        ),
        "sfx": config_enabled(
            config,
            ("SFX_ENABLED", "SFX", "SOUND_EFFECTS",
             "SOUND_EFFECTS_ENABLED"),
        ),
    }

    normalized = {}
    for name, record in tracks.items():
        logical = canonical_track_name(name)
        if logical in {"music", "ambient", "sfx"}:
            if logical in normalized and normalized[logical] != record:
                fail(f"Conflicting sound track aliases: {logical}")
            normalized[logical] = record

    checked = {}
    for name in ("music", "ambient"):
        if not enabled[name]:
            checked[name] = {"status": "disabled"}
            continue
        record = normalized.get(name)
        if not isinstance(record, dict) or record.get("status") != "completed":
            fail(f"Enabled sound track missing or incomplete: {name}")
        asset = validate_audio_file(
            pick(record, "path", "file", "output", "audio_path"),
            f"Sound track {name}",
        )
        declared = record.get("sha256")
        if declared and declared != asset["sha256"]:
            fail(f"Sound track SHA256 mismatch: {name}")
        checked[name] = {"status": "valid", **asset}

    sfx_rows = []
    sfx_count = 0
    if enabled["sfx"]:
        cue_data = read_json(SFX)
        if not isinstance(cue_data, dict) or cue_data.get("status") != "completed":
            fail("SFX is enabled but its cue manifest is missing or incomplete")

        cues = rows(cue_data, "cues", "items")
        try:
            declared_count = int(cue_data.get("cue_count", len(cues)))
        except (TypeError, ValueError):
            fail("Invalid SFX cue_count")
        if declared_count != len(cues) or not cues:
            fail("SFX cue count mismatch or no SFX cues were generated")

        for index, cue in enumerate(cues, 1):
            if not isinstance(cue, dict):
                fail(f"Invalid SFX cue row {index}")
            try:
                part = int(cue["part"])
                scene = int(cue["scene"])
                timestamp = float(cue.get("start_seconds", cue.get("time", 0)))
                gain = float(cue.get("gain", 0))
            except (KeyError, TypeError, ValueError):
                fail(f"Invalid SFX timing row {index}")
            if part < 1 or scene < 1:
                fail(f"Invalid SFX scene mapping: cue {index}")
            if not math.isfinite(timestamp) or timestamp < 0:
                fail(f"Invalid SFX timestamp: cue {index}")
            if not math.isfinite(gain) or gain < 0:
                fail(f"Invalid SFX gain: cue {index}")

            asset = validate_audio_file(
                pick(cue, "file", "path", "audio", "output"),
                f"SFX cue {index}",
            )
            scene_duration = cue.get("narration_duration_seconds")
            if scene_duration is not None:
                try:
                    scene_duration = float(scene_duration)
                except (TypeError, ValueError):
                    fail(f"Invalid narration duration in SFX cue {index}")
                if (
                    not math.isfinite(scene_duration)
                    or scene_duration <= 0
                    or timestamp >= scene_duration
                ):
                    fail(f"SFX cue falls outside narration scene: {index}")

            sfx_rows.append({
                "part": part,
                "scene": scene,
                "time": round(timestamp, 4),
                "gain": round(gain, 4),
                **asset,
            })

        sfx_count = len(cues)
        timing = read_json(SFX_VALIDATION)
        if timing.get("status") != "completed":
            fail("SFX timing validation is not completed")
        nested = timing.get("sfx")
        if not isinstance(nested, dict) or nested.get("status") != "valid":
            fail("SFX timing validation did not confirm valid cues")
        if timing.get("cue_count") is not None:
            if int(timing["cue_count"]) != sfx_count:
                fail("SFX timing report cue count mismatch")

    return {
        "status": "valid",
        "enabled": enabled,
        "tracks": checked,
        "sfx_cue_count": sfx_count,
        "sfx_cues": sfx_rows,
        "sfx_timing_validation": "valid" if enabled["sfx"] else "disabled",
    }


def validate_render(
    mode: str,
    parts: int,
    scenes: int,
    expected_size: tuple[int, int],
    expected_fps: float,
) -> dict:
    data = read_json(RENDER)
    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Render manifest is missing or incomplete")
    if str(data.get("format", "")).lower() != mode:
        fail("Render manifest FORMAT mismatch")
    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions")

    try:
        manifest_fps = float(data["fps"])
        width = int(data.get("width", expected_size[0]))
        height = int(data.get("height", expected_size[1]))
    except (KeyError, TypeError, ValueError):
        fail("Render manifest FPS or dimensions are invalid")

    if not math.isfinite(manifest_fps) or abs(manifest_fps - expected_fps) > FPS_TOLERANCE:
        fail("Render manifest FPS mismatch")
    if (width, height) != expected_size:
        fail("Render manifest dimensions differ from selected format")

    expected_count = parts * scenes
    declared_count = data.get("completed_count")
    if declared_count is not None and int(declared_count) != expected_count:
        fail(
            f"Render completed_count mismatch: "
            f"{declared_count}/{expected_count}"
        )

    mapping = validate_scene_rows(
        rows(data, "completed_scenes", "scenes"),
        parts, scenes, "Final render",
    )

    scene_records = []
    scene_total = 0.0
    for key, row in mapping.items():
        if row.get("status") != "completed":
            fail(f"Rendered scene incomplete: {key}")
        if row.get("captions_burned") is not True:
            fail(f"Captions not burned into rendered scene: {key}")

        path = safe_file(
            pick(row, "video", "output", "source_rendered_video",
                 "rendered_video", "path"),
            f"Rendered scene {key}",
            MIN_VIDEO_BYTES,
        )
        info = validate_video(
            path, expected_size, expected_fps,
            f"Rendered scene {key}",
        )
        digest = sha256(path)
        declared = row.get("sha256")
        if declared and declared != digest:
            fail(f"Rendered scene SHA256 mismatch: {key}")

        scene_total += info["duration"]
        scene_records.append({
            "part": key[0],
            "scene": key[1],
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": digest,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Expected exactly {parts} rendered part videos")

    part_records = []
    part_total = 0.0
    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                number = int(row.get("part", index))
            except (TypeError, ValueError):
                fail(f"Invalid rendered part number: {index}")
            if number != index:
                fail(f"Rendered part order mismatch: {index}")
            if row.get("status") not in (None, "completed"):
                fail(f"Rendered part incomplete: {index}")
            value = pick(row, "path", "video", "output")
            declared = row.get("sha256")
        else:
            value = row
            declared = None

        path = safe_file(value, f"Rendered part {index}", MIN_VIDEO_BYTES)
        info = validate_video(
            path, expected_size, expected_fps,
            f"Rendered part {index}",
        )
        digest = sha256(path)
        if declared and declared != digest:
            fail(f"Rendered part SHA256 mismatch: {index}")
        part_total += info["duration"]
        part_records.append({
            "part": index,
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": digest,
        })

    final = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias = safe_file(data.get("alias"), "Selected-format alias", MIN_VIDEO_BYTES)
    final_info = validate_video(
        final, expected_size, expected_fps, "Final video"
    )
    alias_info = validate_video(
        alias, expected_size, expected_fps, "Selected-format alias"
    )

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        fail(f"Wrong selected-format alias: {alias.name}")
    if final.resolve() == alias.resolve():
        fail("Final video and format alias must be separate files")

    final_hash = sha256(final)
    if final_hash != sha256(alias):
        fail("Final video and selected-format alias content differ")
    if abs(final_info["duration"] - alias_info["duration"]) > 0.25:
        fail("Final video and alias durations differ")
    if abs(final_info["duration"] - part_total) > max(DURATION_TOLERANCE, parts):
        fail("Final video duration differs from part durations")
    if abs(final_info["duration"] - scene_total) > max(
        DURATION_TOLERANCE, expected_count
    ):
        fail("Final video duration differs from scene durations")

    opposite_name = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite_candidates = (
        OUT / "videos" / opposite_name,
        OUT / opposite_name,
    )
    existing_opposite = [
        relative(path) for path in opposite_candidates if path.exists()
    ]
    if existing_opposite:
        fail(f"Opposite-format video exists: {existing_opposite}")

    return {
        "status": "valid",
        "scene_count": expected_count,
        "part_count": parts,
        "final_video": relative(final),
        "final_video_sha256": final_hash,
        "alias": relative(alias),
        "alias_sha256": sha256(alias),
        "duration": round(final_info["duration"], 3),
        "resolution": list(expected_size),
        "fps": round(final_info["fps"], 4),
        "scene_records": scene_records,
        "part_records": part_records,
        "final_duration": final_info["duration"],
    }


def validate_thumbnail(mode: str) -> dict:
    path = safe_file(relative(THUMBNAIL), "Thumbnail", MIN_IMAGE_BYTES)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format
    except Exception as exc:
        fail(f"Invalid thumbnail image: {exc}")

    if image_format != "JPEG" or dimensions != (1280, 720):
        fail(
            "Thumbnail must be a 1280x720 JPEG; "
            f"found {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        fail("Thumbnail manifest is missing or incomplete")
    if str(manifest.get("format", "")).lower() != mode:
        fail("Thumbnail manifest FORMAT mismatch")

    declared = pick(manifest, "thumbnail", "path", "output")
    if not declared:
        fail("Thumbnail manifest does not declare its main image")
    declared_path = safe_file(declared, "Thumbnail manifest main image", MIN_IMAGE_BYTES)
    if declared_path.resolve() != path.resolve():
        fail("Thumbnail manifest points to a different main image")

    output_rows = manifest.get("thumbnails")
    if not isinstance(output_rows, list) or not output_rows:
        fail("Thumbnail manifest output list is missing")

    verified = []
    seen_paths = set()
    for index, item in enumerate(output_rows, 1):
        if not isinstance(item, dict):
            fail(f"Invalid thumbnail output record: {index}")
        output = safe_file(item.get("file"), f"Thumbnail output {index}", MIN_IMAGE_BYTES)
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

        expected = (int(item.get("width", 0)), int(item.get("height", 0)))
        if fmt != "JPEG" or size != expected:
            fail(f"Thumbnail output dimensions/format mismatch: {output.name}")
        if item.get("success") is not True:
            fail(f"Thumbnail output is not marked successful: {output.name}")
        declared_hash = item.get("sha256")
        digest = sha256(output)
        if declared_hash and declared_hash != digest:
            fail(f"Thumbnail output SHA256 mismatch: {output.name}")

        verified.append({
            "path": relative(output),
            "dimensions": list(size),
            "sha256": digest,
        })

    return {
        "status": "valid",
        "path": relative(path),
        "sha256": sha256(path),
        "resolution": list(dimensions),
        "verified_outputs": verified,
        "story_specific_title": bool(
            manifest.get("design", {}).get("story_specific_title")
        ),
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
        fail("Metadata text must contain TITLE, DESCRIPTION and TAGS")
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
        fail(f"Metadata requires 5–20 unique tags; found {len(result)}")
    return result


def validate_metadata(mode: str) -> dict:
    text_path = safe_file(relative(METADATA), "YouTube metadata text", 10)
    json_path = safe_file(relative(METADATA_JSON), "Structured YouTube metadata", 10)
    tags_path = safe_file(relative(METADATA_TAGS), "Dedicated YouTube tags", 5)

    parsed = parse_metadata_text(text_path)
    structured = read_json(json_path)
    if not isinstance(structured, dict) or structured.get("status") != "completed":
        fail("Structured YouTube metadata is missing or incomplete")
    if str(structured.get("format", "")).lower() != mode:
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
        dedicated = tags_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read dedicated tags file: {exc}")
    dedicated_tags = split_tags(dedicated)

    expected = {tag.casefold() for tag in tags}
    if {tag.casefold() for tag in combined_tags} != expected:
        fail("Metadata text tags differ from structured JSON")
    if {tag.casefold() for tag in dedicated_tags} != expected:
        fail("Dedicated tags differ from structured JSON")
    if len(combined_tags) != len(tags) or len(dedicated_tags) != len(tags):
        fail("Metadata tags contain duplicates or missing entries")

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
    """Reject duplicate file contents and unsafe/ambiguous deliverable names."""
    if not OUT.is_dir():
        fail("output/ directory missing")

    hashes = {}
    duplicate_content = []
    files = []

    for path in sorted(OUT.rglob("*")):
        if path.is_symlink():
            fail(f"Symlink found inside output/: {relative(path)}")
        if not path.is_file():
            continue

        rel = relative(path)
        files.append(rel)

        if path.stat().st_size == 0:
            fail(f"Zero-byte output file: {rel}")

        digest = sha256(path)
        hashes.setdefault(digest, []).append(rel)

    for digest, names in hashes.items():
        if len(names) > 1:
            # Exact duplicates in generated media are rejected, but
            # harmless duplicate JSON/text manifests are reported only.
            media_names = [
                name for name in names
                if Path(name).suffix.lower() in (
                    VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
                )
            ]
            if len(media_names) > 1:
                duplicate_content.append({
                    "sha256": digest,
                    "files": media_names,
                })

    if duplicate_content:
        fail(f"Duplicate media files detected: {duplicate_content[:10]}")

    return {
        "status": "valid",
        "file_count": len(files),
        "duplicate_media_files": 0,
        "zero_byte_files": 0,
        "symlinks": 0,
    }


def validate_archive_if_present(mode: str) -> dict:
    """Validate ZIP contents when present; packaging stage creates it afterward."""
    if not ARCHIVE.is_file():
        return {
            "status": "deferred",
            "reason": "package_output.py creates and verifies the ZIP after this validator runs",
        }

    if ARCHIVE.stat().st_size < MIN_VIDEO_BYTES:
        fail("Existing output ZIP is missing or too small")

    required = {
        "output/thumbnail.jpg",
        "output/youtube_metadata.txt",
        "output/publish/youtube_metadata.json",
        "output/publish/youtube_tags.txt",
        "output/captions/final_captions.srt",
        "output/validation_report.json",
        "output/scene_renders/render_manifest.json",
    }

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        bad = archive.testzip()
        if bad:
            fail(f"ZIP CRC verification failed: {bad}")

        names = archive.namelist()
        if not names:
            fail("ZIP archive is empty")
        if len(names) != len(set(names)):
            duplicates = [
                name for name, count in Counter(names).items()
                if count > 1
            ]
            fail(f"Duplicate ZIP entries detected: {duplicates[:20]}")

        unsafe = []
        for name in names:
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts:
                unsafe.append(name)
        if unsafe:
            fail(f"Unsafe ZIP member paths: {unsafe[:20]}")

        missing = required - set(names)
        if missing:
            fail(f"Required files missing from ZIP: {sorted(missing)}")

        opposite = (
            "katha_lok_ai_full.mp4"
            if mode == "short"
            else "katha_lok_ai_short.mp4"
        )
        if any(PurePosixPath(name).name == opposite for name in names):
            fail(f"Opposite-format video included in ZIP: {opposite}")

        return {
            "status": "valid",
            "archive": relative(ARCHIVE),
            "archive_bytes": ARCHIVE.stat().st_size,
            "archive_sha256": sha256(ARCHIVE),
            "entry_count": len(names),
            "crc": "valid",
            "required_files": sorted(required),
        }


def main() -> None:
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            fail(f"{command} is required but unavailable")
    if not INPUT_FILE.is_file():
        fail("Input/topic.txt is missing")
    if not OUT.is_dir():
        fail("output/ directory missing")

    config = load_and_validate(INPUT_FILE)
    mode = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    expected_fps = float(get_fps(config))

    if mode not in {"short", "full"}:
        fail(f"Unsupported FORMAT: {mode}")
    if configured_parts < 1 or scenes < 1:
        fail("PARTS and SCENES must be positive integers")
    if not math.isfinite(expected_fps) or not 0 < expected_fps <= 60:
        fail(f"Invalid FPS: {expected_fps}")

    expected_size = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    log("=" * 65)
    log("KATHA LOK AI — FINAL OUTPUT VALIDATION")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("CONFIGURED PARTS:", configured_parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED FPS:", expected_fps)

    parts = resolve_actual_parts(mode, configured_parts)
    expected_count = parts * scenes
    log("ACTUAL PARTS:", parts)
    log("EXPECTED MAIN SCENES:", expected_count)

    checks = {}
    checks["photo_motion"] = validate_photo(mode, parts, scenes)
    checks["narration"] = validate_narration(parts, scenes)
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
    checks["unique_output_files"] = validate_unique_output_files()

    for name in REQUIRED_CHECKS:
        item = checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Required validation failed: {name}")

    archive_check = validate_archive_if_present(mode)
    render_report = dict(checks["render"])
    render_report.pop("final_duration", None)
    checks["render"] = render_report

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "configured_parts": configured_parts,
        "dynamic_short_parts": mode == "short",
        "scenes_per_part": scenes,
        "expected_scenes": expected_count,
        "resolution": list(expected_size),
        "fps": expected_fps,
        "checks": checks,
        "archive_validation": archive_check,
        "limitations": [
            "Media streams, dimensions, FPS, duration, hashes and scene order are checked.",
            "Narration audio streams, scene mapping and minimum scene duration are checked.",
            "SRT cue sequence, timestamps, overlaps and duration are checked.",
            "This validator does not independently prove word-level speech alignment.",
            "ZIP creation and final ZIP verification are performed by package_output.py after this step.",
            "External provider availability is not tested by final-output validation.",
        ],
    }

    write_json(REPORT, report)

    saved = read_json(REPORT)
    if not isinstance(saved, dict):
        fail("Validation report could not be reloaded")
    if saved.get("status") != "completed":
        fail("Persisted validation report status mismatch")
    if saved.get("format") != mode:
        fail("Persisted validation report FORMAT mismatch")
    if saved.get("parts") != parts:
        fail("Persisted validation report part count mismatch")
    if saved.get("scenes_per_part") != scenes:
        fail("Persisted validation report scene count mismatch")
    if saved.get("expected_scenes") != expected_count:
        fail("Persisted validation report expected scene count mismatch")
    if saved.get("dynamic_short_parts") is not (mode == "short"):
        fail("Persisted validation report dynamic_short_parts mismatch")

    saved_checks = saved.get("checks")
    if not isinstance(saved_checks, dict):
        fail("Persisted validation report checks object missing")
    for name in REQUIRED_CHECKS:
        item = saved_checks.get(name)
        if not isinstance(item, dict) or item.get("status") != "valid":
            fail(f"Persisted validation check failed: {name}")

    log("-" * 65)
    log("FINAL VALIDATION: completed")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("TOTAL MAIN SCENES:", expected_count)
    log("NARRATION DURATION:", checks["narration"]["total_duration"])
    log("SFX CUES:", checks["sound_design"]["sfx_cue_count"])
    log("FINAL VIDEO:", checks["render"]["final_video"])
    log("THUMBNAIL:", checks["thumbnail"]["path"])
    log("METADATA:", checks["metadata"]["path"])
    log("ZIP VALIDATION:", archive_check["status"])
    log("REPORT:", relative(REPORT))
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("FINAL VALIDATION CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("FINAL VALIDATION FAILED:", str(exc))
        raise