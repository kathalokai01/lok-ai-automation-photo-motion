
#!/usr/bin/env python3
"""Validate final photo-motion video deliverables without external API calls."""

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
CUE_MANIFEST = OUT / "sound_design" / "sfx_cues.json"
RENDER_MANIFEST = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
FINAL_SRT = OUT / "captions" / "final_captions.srt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v11"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
DURATION_TOLERANCE = 1.0


def log(*args):
    print(*args, flush=True)


def read_json(path, required=True):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if required:
            raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc
        return {}

    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return value


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
        raise RuntimeError(f"{label}: file path is missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is too small: {path.name}")

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


def first_path(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


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
        raise RuntimeError(f"Invalid media duration: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else None,
        "height": int(video.get("height") or 0) if video else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def require_video(path, label, resolution=None, require_audio=True):
    info = media_info(path)

    if not info["has_video"]:
        raise RuntimeError(f"{label}: video stream is missing")
    if require_audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream is missing")
    if resolution and (info["width"], info["height"]) != resolution:
        raise RuntimeError(
            f"{label}: expected {resolution}, got "
            f"{(info['width'], info['height'])}"
        )

    return info


def expected_keys(parts, scenes):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }


def validate_scene_order(rows, parts, scenes, label):
    expected_count = parts * scenes

    if len(rows) != expected_count:
        raise RuntimeError(
            f"{label}: expected {expected_count} scenes, found {len(rows)}"
        )

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: invalid row {index}")

        expected_part = (index - 1) // scenes + 1
        expected_local = (index - 1) % scenes + 1

        try:
            part = int(row["part"])
            local = int(row.get("local_scene", row.get("scene", expected_local)))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{label}: invalid mapping at row {index}"
            ) from exc

        if (part, local, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(
                f"{label}: incorrect order at row {index}; "
                f"got {(part, local, global_scene)}, expected "
                f"{(expected_part, expected_local, index)}"
            )

    return expected_count


def validate_photo_motion(parts, scenes, resolution):
    data = read_json(PHOTO_MANIFEST)

    if data.get("status") not in (None, "completed"):
        raise RuntimeError("Photo-motion manifest is not completed")

    rows = rows_from(
        data, "scenes", "videos", "completed_scenes", "items"
    )
    count = validate_scene_order(rows, parts, scenes, "Photo motion")

    for index, row in enumerate(rows, start=1):
        if row.get("status") not in (None, "completed"):
            raise RuntimeError(f"Photo-motion scene {index} is incomplete")

        path = safe_file(
            first_path(row, ("video", "output", "video_path")),
            f"Photo-motion scene {index}",
            MIN_VIDEO_BYTES,
        )
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
            raise RuntimeError("Invalid narration job row")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid narration scene mapping") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration job is incomplete: {key}")

        path = safe_file(
            first_path(row, ("output", "audio_path", "path", "file", "audio")),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {key}")

        mapping[key] = info["duration"]

    expected = expected_keys(parts, scenes)
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
    cue_data = read_json(CUE_MANIFEST)

    if manifest.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is incomplete")
    if cue_data.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is incomplete")
    if manifest.get("external_api_calls") != 0:
        raise RuntimeError("Sound design reports unexpected API calls")
    if manifest.get("paid_services_used") is not False:
        raise RuntimeError("Sound design reports paid-service usage")

    if int(cue_data.get("parts", -1)) != parts:
        raise RuntimeError("SFX cue PARTS differs from input")
    if int(cue_data.get("scenes_per_part", -1)) != scenes:
        raise RuntimeError("SFX cue SCENES differs from input")

    enabled = manifest.get("enabled", {})
    tracks = manifest.get("tracks", {})

    if not isinstance(enabled, dict) or not isinstance(tracks, dict):
        raise RuntimeError("Invalid sound-design enabled/tracks fields")

    track_report = {}
    for category in ("music", "ambient", "sfx"):
        if not enabled.get(category, False):
            track_report[category] = {"status": "disabled"}
            continue

        record = tracks.get(category)
        if not isinstance(record, dict) or record.get("status") != "completed":
            raise RuntimeError(f"Enabled sound track is incomplete: {category}")

        path = safe_file(
            record.get("path"),
            f"Sound track {category}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Sound track has no audio stream: {category}")

        track_report[category] = {
            "status": "valid",
            "path": relative(path),
            "duration_seconds": round(info["duration"], 3),
        }

    cues = cue_data.get("cues")
    if not isinstance(cues, list):
        raise RuntimeError("SFX cue list is missing")

    known = expected_keys(parts, scenes)
    cue_count = 0

    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue row {index}")

        try:
            key = (int(cue["part"]), int(cue["scene"]))
            cue_time = float(cue["time"])
            gain = float(cue.get("gain", 0.075))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX cue fields: {index}") from exc

        if key not in known:
            raise RuntimeError(f"SFX cue maps to unknown scene: {index}")
        if not math.isfinite(cue_time) or cue_time < 0:
            raise RuntimeError(f"Invalid SFX cue time: {index}")
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"Invalid SFX cue gain: {index}")
        if not enabled.get("sfx", False):
            raise RuntimeError("SFX cues exist while SFX is disabled")

        path = safe_file(
            cue.get("file"), f"SFX cue {index}", MIN_AUDIO_BYTES
        )
        if not media_info(path)["has_audio"]:
            raise RuntimeError(f"SFX cue has no audio stream: {index}")

        cue_count += 1

    if int(cue_data.get("cue_count", -1)) != cue_count:
        raise RuntimeError("SFX cue_count differs from cue list")

    return {
        "status": "valid",
        "tracks": track_report,
        "cue_count": cue_count,
        "external_api_calls": 0,
        "paid_services_used": False,
    }


def validate_captions(parts, scenes):
    data = read_json(CAPTION_MANIFEST)

    if data.get("status") != "completed":
        raise RuntimeError("Caption manifest is incomplete")

    rows = data.get("captions")
    if not isinstance(rows, list):
        raise RuntimeError("Caption list is missing")

    mapping = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption row")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption mapping") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate caption mapping: {key}")
        if not str(row.get("text", "")).strip():
            raise RuntimeError(f"Empty caption: {key}")

        mapping[key] = True

    expected = expected_keys(parts, scenes)
    if set(mapping) != expected:
        raise RuntimeError(
            "Caption mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

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

    def to_seconds(value):
        hours, minutes, remainder = value.split(":")
        seconds, millis = remainder.split(",")
        return (
            int(hours) * 3600 + int(minutes) * 60
            + int(seconds) + int(millis) / 1000
        )

    previous_end = -1.0
    for index, cue in enumerate(cues, start=1):
        start, end = map(to_seconds, cue.groups())

        if end <= start:
            raise RuntimeError(f"SRT cue {index} has invalid timing")
        if start < previous_end:
            raise RuntimeError(f"SRT cue {index} overlaps previous cue")

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
        raise RuntimeError("Render manifest is incomplete")
    if data.get("format") != mode:
        raise RuntimeError("Render format differs from input")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list):
        raise RuntimeError("Render completed_scenes list is missing")

    count = validate_scene_order(rows, parts, scenes, "Rendered")
    if int(data.get("completed_count", -1)) != count:
        raise RuntimeError("Render completed_count mismatch")

    durations = []
    for index, row in enumerate(rows, start=1):
        if row.get("status") != "completed":
            raise RuntimeError(f"Rendered scene {index} is incomplete")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Rendered scene {index} lacks burned captions")

        source = safe_file(
            row.get("source_rendered_video"),
            f"Original scene {index}",
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

        info = require_video(output, f"Captioned scene {index}", resolution)

        try:
            declared = float(row.get("duration", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid declared duration for scene {index}") from exc

        if not math.isfinite(declared) or declared <= 0:
            raise RuntimeError(f"Invalid declared duration for scene {index}")
        if abs(info["duration"] - declared) > DURATION_TOLERANCE:
            raise RuntimeError(f"Scene {index} duration mismatch")

        durations.append(info["duration"])

    part_values = data.get("parts")
    if not isinstance(part_values, list) or len(part_values) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_report = []
    for index, value in enumerate(part_values, start=1):
        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = require_video(path, f"Part video {index}", resolution)
        part_report.append({
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
        raise RuntimeError("Final video and alias durations differ")

    try:
        declared_final = float(data.get("duration", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid final duration in render manifest") from exc

    if (
        not math.isfinite(declared_final)
        or declared_final <= 0
        or abs(final_info["duration"] - declared_final) > DURATION_TOLERANCE
    ):
        raise RuntimeError("Final duration differs from render manifest")

    if abs(final_info["duration"] - sum(durations)) > max(
        DURATION_TOLERANCE * count, 2.0
    ):
        raise RuntimeError("Final duration differs from scene durations")

    return {
        "status": "valid",
        "scene_count": count,
        "scene_duration_seconds": round(sum(durations), 3),
        "parts": part_report,
        "final_video": relative(final_path),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": list(resolution),
        "alias": relative(alias_path),
        "final_bytes": final_path.stat().st_size,
    }


def validate_thumbnail():
    path = safe_file(
        relative(THUMBNAIL), "Thumbnail", MIN_IMAGE_BYTES
    )

    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
    except Exception as exc:
        raise RuntimeError(f"Thumbnail is not a valid image: {exc}") from exc

    if width < 640 or height < 360:
        raise RuntimeError(
            f"Thumbnail dimensions too small: {width}x{height}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST, required=False)
    if manifest:
        if manifest.get("status") not in (None, "completed"):
            raise RuntimeError("Thumbnail manifest is not completed")

        declared_path = first_path(
            manifest, ("thumbnail", "output", "path", "file")
        )
        if declared_path:
            declared = safe_file(declared_path, "Manifest thumbnail", 1)
            if declared.resolve() != path.resolve():
                log(
                    "WARNING: thumbnail manifest references a different "
                    "file:", relative(declared)
                )

    return {
        "status": "valid",
        "path": relative(path),
        "format": image_format,
        "width": width,
        "height": height,
        "bytes": path.stat().st_size,
    }


def validate_metadata():
    path = safe_file(relative(METADATA), "YouTube metadata", 10)
    text = path.read_text(encoding="utf-8-sig").strip()

    if len(text) < 10:
        raise RuntimeError("YouTube metadata is empty or too short")

    return {
        "status": "valid",
        "path": relative(path),
        "characters": len(text),
    }


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    resolution = (1280, 720) if mode == "full" else (720, 1280)

    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required for final validation")

    log("FINAL VALIDATION")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED SCENES:", parts * scenes)

    checks = {}
    checks["photo_motion"] = validate_photo_motion(
        parts, scenes, resolution
    )
    checks["narration"] = validate_narration(parts, scenes)
    checks["sound_design"] = validate_sound_design(parts, scenes)
    checks["captions"] = validate_captions(parts, scenes)
    checks["render"] = validate_render(mode, parts, scenes, resolution)
    checks["thumbnail"] = validate_thumbnail()
    checks["metadata"] = validate_metadata()

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "resolution": {
            "width": resolution[0],
            "height": resolution[1],
        },
        "external_api_calls_by_validator": 0,
        "checks": checks,
    }
    write_json(REPORT, report)

    log("=" * 58)
    log("FINAL OUTPUT VALIDATION: PASSED")
    log("FORMAT:", mode)
    log("SCENES:", parts * scenes)
    log("FINAL VIDEO:", checks["render"]["final_video"])
    log("DURATION:", checks["render"]["final_duration_seconds"])
    log("THUMBNAIL:", checks["thumbnail"]["path"])
    log("REPORT:", relative(REPORT))
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        failure = {
            "status": "failed",
            "version": VERSION,
            "error": str(exc),
        }
        try:
            write_json(REPORT, failure)
        except Exception:
            pass
        log("FINAL OUTPUT VALIDATION: FAILED:", str(exc))
        sys.exit(1)
