#!/usr/bin/env python3
"""Validate final videos, captions, audio tracks, sound cues and deliverables."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUT = ROOT / "output"
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
CAPTION_MANIFEST = OUT / "captions" / "captions.json"
SOUND_MANIFEST = OUT / "sound_design" / "sound_design_manifest.json"
SFX_CUE_MANIFEST = OUT / "sound_design" / "sfx_cues.json"
RENDER_MANIFEST = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
FINAL_SRT = OUT / "captions" / "final_captions.srt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v8"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
DURATION_TOLERANCE = 0.8


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file too small")

    return path


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def rows_from(data, *keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]

    return []


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    streams = data.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else None,
        "height": int(video.get("height") or 0) if video else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def require_video(path, label, resolution, require_audio=True):
    info = media_info(path)

    if not info["has_video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if require_audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream missing")
    if (info["width"], info["height"]) != resolution:
        raise RuntimeError(
            f"{label}: expected {resolution}, got "
            f"{(info['width'], info['height'])}"
        )

    return info


def validate_scene_order(rows, parts, scenes, label):
    expected_count = parts * scenes

    if len(rows) != expected_count:
        raise RuntimeError(
            f"{label}: expected {expected_count} scenes, found {len(rows)}"
        )

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: row {index} is invalid")

        try:
            part = int(row["part"])
            local = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(
                row.get("global_scene", row.get("scene", index))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{label}: invalid mapping at row {index}"
            ) from exc

        expected_part = (index - 1) // scenes + 1
        expected_local = (index - 1) % scenes + 1

        if (part, local, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(
                f"{label}: incorrect mapping at row {index}: "
                f"{(part, local, global_scene)}"
            )

    return expected_count


def validate_photo_motion(parts, scenes, resolution):
    data = read_json(PHOTO_MANIFEST)

    if isinstance(data, dict) and data.get("status") not in (
        None, "completed"
    ):
        raise RuntimeError("Photo-motion manifest is not completed")

    rows = rows_from(
        data, "scenes", "videos", "completed_scenes", "items"
    )
    count = validate_scene_order(rows, parts, scenes, "Photo-motion")

    for index, row in enumerate(rows, start=1):
        if row.get("status") not in (None, "completed"):
            raise RuntimeError(f"Photo-motion scene {index} is incomplete")

        value = next(
            (
                row.get(key)
                for key in ("video", "output", "video_path")
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        path = safe_file(value, f"Photo-motion scene {index}", MIN_VIDEO_BYTES)
        require_video(
            path, f"Photo-motion scene {index}",
            resolution, require_audio=False,
        )

    return {"status": "valid", "scene_count": count}


def validate_narration(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    rows = rows_from(data, "jobs", "audio_jobs")
    mapping = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        if key in mapping:
            raise RuntimeError(f"Duplicate narration job: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration is not completed: {key}")

        value = next(
            (
                row.get(field)
                for field in ("output", "audio_path", "path", "file", "audio")
                if isinstance(row.get(field), str) and row[field].strip()
            ),
            None,
        )
        path = safe_file(value, f"Narration {key}", MIN_AUDIO_BYTES)
        info = media_info(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {key}")

        mapping[key] = info["duration"]

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    if set(mapping) != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "total_audio_seconds": round(sum(mapping.values()), 3),
    }


def validate_sound_design(parts, scenes):
    manifest = read_json(SOUND_MANIFEST)
    cues_data = read_json(SFX_CUE_MANIFEST)

    if manifest.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")
    if cues_data.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed")

    if manifest.get("external_api_calls") != 0:
        raise RuntimeError("Sound design reports unexpected API calls")
    if manifest.get("paid_services_used") is not False:
        raise RuntimeError("Sound design reports paid-service usage")

    if int(cues_data.get("parts", -1)) != parts:
        raise RuntimeError("SFX cue manifest PARTS mismatch")
    if int(cues_data.get("scenes_per_part", -1)) != scenes:
        raise RuntimeError("SFX cue manifest SCENES mismatch")

    enabled = manifest.get("enabled", {})
    tracks = manifest.get("tracks", {})

    track_checks = {}

    for category in ("music", "ambient", "sfx"):
        if not enabled.get(category, False):
            track_checks[category] = "disabled"
            continue

        record = tracks.get(category)
        if not isinstance(record, dict):
            raise RuntimeError(f"Missing sound-design track record: {category}")
        if record.get("status") != "completed":
            raise RuntimeError(f"Sound-design track incomplete: {category}")

        path = safe_file(
            record.get("path"),
            f"Sound-design track {category}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Track has no audio stream: {category}")

        track_checks[category] = {
            "status": "valid",
            "path": relative(path),
            "duration_seconds": round(info["duration"], 3),
        }

    cues = cues_data.get("cues")
    if not isinstance(cues, list):
        raise RuntimeError("SFX cue manifest has no cues list")

    expected_scene_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }
    cue_count = 0

    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"SFX cue {index} is invalid")

        try:
            part = int(cue["part"])
            scene = int(cue["scene"])
            time = float(cue["time"])
            gain = float(cue.get("gain", 0.075))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"SFX cue {index} has invalid fields") from exc

        if (part, scene) not in expected_scene_keys:
            raise RuntimeError(f"SFX cue {index} maps to an unknown scene")
        if not math.isfinite(time) or time < 0:
            raise RuntimeError(f"SFX cue {index} has invalid timing")
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"SFX cue {index} has invalid gain")

        path = safe_file(cue.get("file"), f"SFX cue {index}", MIN_AUDIO_BYTES)
        info = media_info(path)

        if not info["has_audio"]:
            raise RuntimeError(f"SFX cue {index} has no audio stream")

        cue_count += 1

    if int(cues_data.get("cue_count", -1)) != cue_count:
        raise RuntimeError("SFX cue_count does not match the cue list")
    if not enabled.get("sfx", False) and cue_count:
        raise RuntimeError("SFX cues exist although SFX is disabled")

    return {
        "status": "valid",
        "tracks": track_checks,
        "cue_count": cue_count,
        "external_api_calls": 0,
        "paid_services_used": False,
        "note": (
            "Cues are structurally validated; the sound design currently "
            "uses generic transition whooshes, not story-event-specific SFX."
        ),
    }


def validate_captions(parts, scenes):
    data = read_json(CAPTION_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    rows = data.get("captions")
    if not isinstance(rows, list):
        raise RuntimeError("Caption manifest has no captions list")

    mapping = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        if key in mapping:
            raise RuntimeError(f"Duplicate caption mapping: {key}")
        if not str(row.get("text", "")).strip():
            raise RuntimeError(f"Empty caption: {key}")

        mapping[key] = True

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    if set(mapping) != expected:
        raise RuntimeError("Caption mapping does not match PARTS × SCENES")

    srt = safe_file(relative(FINAL_SRT), "Final SRT", 10)
    content = srt.read_text(encoding="utf-8-sig")

    pattern = re.compile(
        r"(?m)^\d+\s*\n"
        r"(\d{2}:\d{2}:\d{2},\d{3}) --> "
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    )
    cues = list(pattern.finditer(content))

    if len(cues) != parts * scenes:
        raise RuntimeError(
            f"SRT cue count mismatch: expected {parts * scenes}, "
            f"found {len(cues)}"
        )

    def seconds(value):
        hours, minutes, remainder = value.split(":")
        secs, millis = remainder.split(",")
        return (
            int(hours) * 3600 + int(minutes) * 60
            + int(secs) + int(millis) / 1000
        )

    previous_end = -1.0

    for index, cue in enumerate(cues, start=1):
        start = seconds(cue.group(1))
        end = seconds(cue.group(2))

        if end <= start:
            raise RuntimeError(f"SRT cue {index} has invalid timing")
        if start < previous_end:
            raise RuntimeError(f"SRT cue {index} overlaps a previous cue")

        previous_end = end

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "srt_cues": len(cues),
        "last_caption_end_seconds": round(previous_end, 3),
    }


def validate_render(mode, parts, scenes, resolution):
    data = read_json(RENDER_MANIFEST)

    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if data.get("format") != mode:
        raise RuntimeError("Render format differs from Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list):
        raise RuntimeError("Render manifest has no completed_scenes")

    count = validate_scene_order(rows, parts, scenes, "Rendered")

    if int(data.get("completed_count", -1)) != count:
        raise RuntimeError("completed_count does not match scene count")

    durations = []

    for index, row in enumerate(rows, start=1):
        if row.get("status") != "completed":
            raise RuntimeError(f"Rendered scene {index} is incomplete")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} has no burned captions")

        source = safe_file(
            row.get("source_rendered_video"),
            f"Uncaptioned scene {index}",
            MIN_VIDEO_BYTES,
        )
        output = safe_file(
            row.get("video"),
            f"Captioned scene {index}",
            MIN_VIDEO_BYTES,
        )

        if source.resolve() == output.resolve():
            raise RuntimeError(
                f"Scene {index}: original and captioned paths are identical"
            )

        info = require_video(
            output, f"Captioned scene {index}", resolution
        )

        expected_duration = float(row.get("duration", 0))
        if expected_duration <= 0 or not math.isfinite(expected_duration):
            raise RuntimeError(f"Scene {index} has invalid duration")
        if abs(info["duration"] - expected_duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Scene {index} duration mismatch")

        durations.append(info["duration"])

    part_paths = data.get("parts")
    if not isinstance(part_paths, list) or len(part_paths) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_reports = []

    for index, value in enumerate(part_paths, start=1):
        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = require_video(path, f"Part video {index}", resolution)
        part_reports.append({
            "path": relative(path),
            "duration_seconds": round(info["duration"], 3),
            "bytes": path.stat().st_size,
        })

    final_path = safe_file(
        data.get("final_video"), "Final video", MIN_VIDEO_BYTES
    )
    alias_path = safe_file(data.get("alias"), "Format alias", MIN_VIDEO_BYTES)

    final_info = require_video(final_path, "Final video", resolution)
    alias_info = require_video(alias_path, "Format alias", resolution)

    if abs(final_info["duration"] - alias_info["duration"]) > DURATION_TOLERANCE:
        raise RuntimeError("Final video and format alias durations differ")

    manifest_duration = float(data.get("duration", 0))
    if (
        manifest_duration <= 0
        or not math.isfinite(manifest_duration)
        or abs(final_info["duration"] - manifest_duration) > DURATION_TOLERANCE
    ):
        raise RuntimeError("Final duration differs from manifest")

    if abs(final_info["duration"] - sum(durations)) > max(
        DURATION_TOLERANCE * count, 2.0
    ):
        raise RuntimeError("Final duration differs from scene durations")

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias_path.name != expected_alias:
        raise RuntimeError("Unexpected format alias filename")

    opposite_alias = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if (OUT / "videos" / opposite_alias).exists():
        raise RuntimeError("Opposite format alias exists")

    return {
        "status": "valid",
        "scene_count": count,
        "parts": part_reports,
        "final_video": relative(final_path),
        "final_duration_seconds": round(final_info["duration"], 3),
        "final_bytes": final_path.stat().st_size,
        "alias": relative(alias_path),
        "resolution": list(resolution),
    }


def validate_thumbnail(mode):
    path = safe_file(relative(THUMBNAIL), "Thumbnail", MIN_IMAGE_BYTES)

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
    except Exception as exc:
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc

    manifest = read_json(THUMBNAIL_MANIFEST)

    if manifest.get("status") != "completed":
        raise RuntimeError("Thumbnail manifest is not completed")
    if manifest.get("format") != mode:
        raise RuntimeError("Thumbnail format mismatch")
    if not str(manifest.get("title", "")).strip():
        raise RuntimeError("Thumbnail title is empty")

    expected = (1080, 1920) if mode == "short" else (1280, 720)
    if (width, height) != expected:
        raise RuntimeError(
            f"Thumbnail dimensions mismatch: expected {expected}, "
            f"got {(width, height)}"
        )

    if manifest.get("width") != width or manifest.get("height") != height:
        raise RuntimeError("Thumbnail manifest dimensions mismatch")
    if manifest.get("external_api_calls") != 0:
        raise RuntimeError("Thumbnail reports unexpected external API calls")

    declared = manifest.get("thumbnail")
    if declared:
        declared_path = safe_file(
            declared, "Thumbnail manifest image", MIN_IMAGE_BYTES
        )
        if declared_path.resolve() != path.resolve():
            raise RuntimeError("Thumbnail manifest points to another image")

    return {
        "status": "valid",
        "path": relative(path),
        "width": width,
        "height": height,
        "bytes": path.stat().st_size,
        "title": manifest["title"],
    }


def validate_metadata():
    path = safe_file(relative(METADATA), "YouTube metadata", 20)
    content = path.read_text(encoding="utf-8-sig").strip()

    for label in ("Title:", "Description:", "Tags:"):
        if label not in content:
            raise RuntimeError(f"Metadata section missing: {label}")

    try:
        title_section, remainder = content.split("Description:", 1)
        description, tags = remainder.split("Tags:", 1)
    except ValueError as exc:
        raise RuntimeError("Metadata sections are malformed") from exc

    title = title_section.replace("Title:", "", 1).strip()
    description = description.strip()
    tags = tags.strip()

    if not title or not description or not tags:
        raise RuntimeError("Metadata title, description or tags are empty")
    if len(description) > 5000:
        raise RuntimeError("Metadata description is unexpectedly long")

    return {
        "status": "valid",
        "path": relative(path),
        "title_characters": len(title),
        "description_characters": len(description),
        "tag_characters": len(tags),
    }


def main():
    report = {
        "status": "in_progress",
        "validator": VERSION,
        "checks": {},
        "errors": [],
    }

    try:
        if not shutil.which("ffprobe"):
            raise RuntimeError("ffprobe is not installed")

        config = load_and_validate(ROOT / "Input" / "topic.txt")
        mode = get_format(config)
        parts = int(get_parts(config))
        scenes = int(get_scenes(config))

        if mode not in ("short", "full"):
            raise RuntimeError(f"Unsupported format: {mode}")
        if parts < 1 or scenes < 1:
            raise RuntimeError("PARTS and SCENES must be positive integers")

        resolution = (1280, 720) if mode == "full" else (720, 1280)

        report.update({
            "format": mode,
            "expected_scenes": parts * scenes,
            "parts": parts,
            "scenes_per_part": scenes,
            "resolution": list(resolution),
        })

        checks = [
            (
                "photo_motion",
                lambda: validate_photo_motion(parts, scenes, resolution),
            ),
            (
                "narration",
                lambda: validate_narration(parts, scenes),
            ),
            (
                "sound_design",
                lambda: validate_sound_design(parts, scenes),
            ),
            (
                "captions",
                lambda: validate_captions(parts, scenes),
            ),
            (
                "render",
                lambda: validate_render(mode, parts, scenes, resolution),
            ),
            (
                "thumbnail",
                lambda: validate_thumbnail(mode),
            ),
            (
                "metadata",
                validate_metadata,
            ),
        ]

        log("FINAL OUTPUT VALIDATION")
        log(f"FORMAT={mode} PARTS={parts} SCENES_PER_PART={scenes}")

        for name, callback in checks:
            try:
                report["checks"][name] = callback()
                log("PASS:", name)
            except Exception as exc:
                report["errors"].append({
                    "check": name,
                    "error": str(exc),
                })
                log("FAIL:", name, str(exc))

    except Exception as exc:
        report["errors"].append({
            "check": "initialization",
            "error": str(exc),
        })
        log("VALIDATION INITIALIZATION FAILED:", str(exc))

    report["passed_checks"] = len(report["checks"])
    report["failed_checks"] = len(report["errors"])
    report["status"] = "completed" if not report["errors"] else "failed"

    write_json(REPORT, report)

    log("STATUS:", report["status"].upper())
    log("PASSED:", report["passed_checks"])
    log("FAILED:", report["failed_checks"])
    log("REPORT:", relative(REPORT))

    if report["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("VALIDATION PIPELINE FAILED:", str(exc))
        sys.exit(1)