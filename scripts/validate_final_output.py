
#!/usr/bin/env python3
"""Strict final-output validation for Katha Lok AI photo-motion videos."""

from __future__ import annotations

import hashlib
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
SFX_REPORT = OUT / "sound_design" / "sfx_timing_validation.json"
RENDER_MANIFEST = OUT / "scene_renders" / "render_manifest.json"
FINAL_SRT = OUT / "captions" / "final_captions.srt"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v17-required-deliverables"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
DURATION_TOLERANCE = 1.0


def log(*values):
    print(*values, flush=True)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, required=True):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if required:
            raise RuntimeError(f"Cannot read {path.relative_to(ROOT)}: {exc}") from exc
        return None

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path.relative_to(ROOT)}")

    return value


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


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


def first_path(row, keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


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
        raise RuntimeError("ffprobe is required but not installed")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-800:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")

    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )

    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_media(path, label, resolution=None, video=False, audio=False):
    info = media_info(path)

    if video and not info["has_video"]:
        raise RuntimeError(f"{label}: video stream is missing")
    if audio and not info["has_audio"]:
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


def check_scene_order(rows, parts, scenes, label):
    expected_count = parts * scenes
    if len(rows) != expected_count:
        raise RuntimeError(
            f"{label}: expected {expected_count} scenes, found {len(rows)}"
        )

    mapping = {}

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: invalid row {index}")

        try:
            part = int(row["part"])
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{label}: invalid scene mapping at row {index}"
            ) from exc

        expected = (
            (index - 1) // scenes + 1,
            (index - 1) % scenes + 1,
            index,
        )
        if (part, scene, global_scene) != expected:
            raise RuntimeError(
                f"{label}: incorrect order at row {index}; "
                f"found {(part, scene, global_scene)}, expected {expected}"
            )

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"{label}: duplicate scene {key}")
        mapping[key] = row

    return mapping


def validate_photo_motion(parts, scenes, resolution):
    data = read_json(PHOTO_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Photo-motion manifest must be a JSON object")
    if data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest is not completed")

    rows = rows_from(data, "scenes", "videos", "completed_scenes", "items")
    mapping = check_scene_order(rows, parts, scenes, "Photo motion")

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene {key} is incomplete")

        path = safe_file(
            first_path(
                row,
                ("video", "source_video", "output", "video_path", "path", "file"),
            ),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )
        require_media(
            path, f"Photo-motion scene {key}",
            resolution=resolution, video=True,
        )

        expected_hash = row.get("video_sha256")
        if expected_hash:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise RuntimeError(f"Photo-motion checksum mismatch: {key}")

    return {"status": "valid", "scene_count": len(mapping)}


def validate_narration(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Narration manifest must be an object")

    mapping = {}

    for row in rows_from(data, "jobs", "audio_jobs", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid narration job")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid narration mapping") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration is incomplete: {key}")

        path = safe_file(
            first_path(row, ("output", "audio_path", "path", "file", "audio")),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = require_media(path, f"Narration {key}", audio=True)

        mapping[key] = {
            "path": relative(path),
            "duration": info["duration"],
        }

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
        "total_audio_seconds": round(
            sum(row["duration"] for row in mapping.values()), 3
        ),
        "scenes": mapping,
    }


def srt_seconds(value):
    hours, minutes, remainder = value.split(":")
    seconds, millis = remainder.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000
    )


def validate_captions(parts, scenes):
    data = read_json(CAPTION_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    mapping = {}
    for row in data.get("captions", []):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption row")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption scene mapping") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate caption: {key}")
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

    srt = safe_file(relative(FINAL_SRT), "Final captions SRT", 10)
    content = srt.read_text(encoding="utf-8-sig")
    pattern = re.compile(
        r"(?m)^\d+\s*\n"
        r"(\d{2}:\d{2}:\d{2},\d{3}) --> "
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    )
    cues = list(pattern.finditer(content))
    expected_count = parts * scenes

    if len(cues) != expected_count:
        raise RuntimeError(
            f"SRT cue count mismatch: expected {expected_count}, "
            f"found {len(cues)}"
        )

    previous_end = -1.0
    for index, cue in enumerate(cues, start=1):
        start, end = (srt_seconds(value) for value in cue.groups())
        if end <= start:
            raise RuntimeError(f"SRT cue {index} has invalid duration")
        if start < previous_end - 0.001:
            raise RuntimeError(f"SRT cue {index} overlaps the previous cue")
        previous_end = end

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "srt_cues": len(cues),
        "last_caption_end_seconds": round(previous_end, 3),
        "word_level_alignment_verified": False,
    }


def validate_sound_design(parts, scenes, narration):
    sound = read_json(SOUND_MANIFEST)
    cues_data = read_json(CUE_MANIFEST)
    timing = read_json(SFX_REPORT)

    if not isinstance(sound, dict) or sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")
    if sound.get("external_api_calls", 0) != 0:
        raise RuntimeError("Sound-design manifest reports external API calls")
    if sound.get("paid_services_used") is True:
        raise RuntimeError("Sound design reports paid-service usage")

    if not isinstance(cues_data, dict) or cues_data.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed")
    if not isinstance(timing, dict) or timing.get("status") != "completed":
        raise RuntimeError("SFX timing validation report is not completed")
    if timing.get("external_api_calls", 0) != 0:
        raise RuntimeError("SFX timing report declares external API calls")
    if timing.get("paid_services_used") is True:
        raise RuntimeError("SFX timing report declares paid-service usage")

    sfx_result = timing.get("sfx")
    if not isinstance(sfx_result, dict) or sfx_result.get("status") != "valid":
        raise RuntimeError("SFX timing report does not confirm valid status")
    if int(timing.get("expected_scenes", -1)) != parts * scenes:
        raise RuntimeError("SFX report scene count mismatch")
    if int(timing.get("narration_scenes_checked", -1)) != parts * scenes:
        raise RuntimeError("SFX report did not check every narration scene")

    enabled = sound.get("enabled", {})
    tracks = sound.get("tracks", {})
    if not isinstance(enabled, dict) or not isinstance(tracks, dict):
        raise RuntimeError("Invalid sound-design track structure")

    track_report = {}
    for name in ("music", "ambient", "sfx"):
        track = tracks.get(name)
        is_enabled = bool(enabled.get(name, track is not None))

        if not is_enabled:
            track_report[name] = "disabled"
            continue

        if not isinstance(track, dict) or track.get("status") != "completed":
            raise RuntimeError(f"Enabled sound-design track is incomplete: {name}")

        path = safe_file(
            first_path(track, ("path", "file", "output")),
            f"Sound-design track {name}",
            MIN_AUDIO_BYTES,
        )
        require_media(path, f"Sound-design track {name}", audio=True)
        track_report[name] = "valid"

    cue_rows = rows_from(cues_data, "cues", "items")
    expected = expected_keys(parts, scenes)

    for index, cue in enumerate(cue_rows, start=1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        try:
            key = (int(cue["part"]), int(cue["scene"]))
            timestamp = float(next(
                cue[k] for k in (
                    "time", "time_seconds", "timestamp", "start_seconds", "start"
                ) if k in cue and cue[k] is not None
            ))
            gain = float(cue.get("gain", cue.get("volume", 0.06)))
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX cue fields at row {index}") from exc

        if key not in expected:
            raise RuntimeError(f"SFX cue references unknown scene: {key}")
        if not math.isfinite(timestamp) or timestamp < 0:
            raise RuntimeError(f"Invalid SFX timestamp at row {index}")
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"Invalid SFX gain at row {index}")
        if timestamp >= narration[key]["duration"]:
            raise RuntimeError(f"SFX starts after narration ends: {key}")

        path = safe_file(
            first_path(cue, ("file", "path", "audio", "output", "audio_path")),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )
        require_media(path, f"SFX cue {index}", audio=True)

    return {
        "status": "valid",
        "tracks": track_report,
        "sfx_cue_count": len(cue_rows),
        "timing_report": relative(SFX_REPORT),
    }


def validate_render(mode, parts, scenes, resolution):
    data = read_json(RENDER_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if data.get("format") != mode:
        raise RuntimeError("Render format differs from Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list):
        raise RuntimeError("Rendered scene list is missing")

    mapping = check_scene_order(rows, parts, scenes, "Rendered")
    if int(data.get("completed_count", -1)) != len(mapping):
        raise RuntimeError("Render completed_count mismatch")

    scene_duration_sum = 0.0
    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Rendered scene is incomplete: {key}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Captions are not confirmed for scene {key}")

        source = safe_file(
            first_path(row, ("source_rendered_video", "source_video", "source")),
            f"Original scene {key}",
            MIN_VIDEO_BYTES,
        )
        output = safe_file(
            first_path(row, ("video", "output", "rendered_video")),
            f"Captioned scene {key}",
            MIN_VIDEO_BYTES,
        )
        if source.resolve() == output.resolve():
            raise RuntimeError(f"Original and captioned paths match: {key}")

        info = require_media(
            output, f"Captioned scene {key}",
            resolution=resolution, video=True, audio=True,
        )

        try:
            declared = float(row.get("duration", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid declared duration: {key}") from exc

        if not math.isfinite(declared) or declared <= 0:
            raise RuntimeError(f"Invalid declared duration: {key}")
        if abs(info["duration"] - declared) > DURATION_TOLERANCE:
            raise RuntimeError(f"Scene duration mismatch: {key}")

        scene_duration_sum += info["duration"]

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_report = []
    for index, row in enumerate(part_rows, start=1):
        value = (
            first_path(row, ("path", "video", "output"))
            if isinstance(row, dict)
            else row
        )
        path = safe_file(value, f"Part {index}", MIN_VIDEO_BYTES)
        info = require_media(
            path, f"Part {index}", resolution=resolution, video=True, audio=True
        )
        part_report.append({
            "path": relative(path),
            "duration_seconds": round(info["duration"], 3),
        })

    final_path = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias_path = safe_file(data.get("alias"), "Format-specific video", MIN_VIDEO_BYTES)
    final_info = require_media(
        final_path, "Final video", resolution=resolution, video=True, audio=True
    )
    alias_info = require_media(
        alias_path, "Format-specific video",
        resolution=resolution, video=True, audio=True,
    )

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias_path.name != expected_alias:
        raise RuntimeError(f"Expected alias {expected_alias}, got {alias_path.name}")
    if abs(final_info["duration"] - alias_info["duration"]) > DURATION_TOLERANCE:
        raise RuntimeError("Final video and alias durations differ")
    if abs(final_info["duration"] - scene_duration_sum) > max(
        2.0, DURATION_TOLERANCE * len(mapping)
    ):
        raise RuntimeError("Final duration differs substantially from scene durations")

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "scene_duration_seconds": round(scene_duration_sum, 3),
        "parts": part_report,
        "final_video": relative(final_path),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": list(resolution),
        "alias": relative(alias_path),
        "final_bytes": final_path.stat().st_size,
    }


def validate_thumbnail(mode):
    path = safe_file(relative(THUMBNAIL), "Thumbnail", MIN_IMAGE_BYTES)

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
    except ImportError as exc:
        raise RuntimeError("Pillow is required to validate the thumbnail") from exc
    except Exception as exc:
        raise RuntimeError(f"Thumbnail image is invalid: {exc}") from exc

    expected = (1080, 1920) if mode == "short" else (1280, 720)
    if (width, height) != expected:
        raise RuntimeError(
            f"Thumbnail dimensions do not match format: "
            f"expected {expected}, got {(width, height)}"
        )
    if image_format not in {"JPEG", "PNG", "WEBP"}:
        raise RuntimeError(f"Unsupported thumbnail format: {image_format}")

    manifest = read_json(THUMBNAIL_MANIFEST, required=False)
    if isinstance(manifest, dict):
        if manifest.get("status") not in (None, "completed"):
            raise RuntimeError("Thumbnail manifest reports failure")
        if manifest.get("format") and manifest["format"] != mode:
            raise RuntimeError("Thumbnail manifest format mismatch")

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
    content = path.read_text(encoding="utf-8-sig").strip()

    if len(content) < 10:
        raise RuntimeError("YouTube metadata is empty")

    return {
        "status": "valid",
        "path": relative(path),
        "characters": len(content),
        "bytes": path.stat().st_size,
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    resolution = (1280, 720) if mode == "full" else (720, 1280)

    log("FINAL OUTPUT VALIDATION")
    log("VERSION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)

    narration_report = validate_narration(parts, scenes)
    narration = narration_report.pop("scenes")

    # These deliverables are mandatory because package_output.py requires them.
    checks = {
        "photo_motion": validate_photo_motion(parts, scenes, resolution),
        "narration": narration_report,
        "captions": validate_captions(parts, scenes),
        "sound_design": validate_sound_design(parts, scenes, narration),
        "render": validate_render(mode, parts, scenes, resolution),
        "thumbnail": validate_thumbnail(mode),
        "metadata": validate_metadata(),
    }

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "resolution": {"width": resolution[0], "height": resolution[1]},
        "validator_external_api_calls": 0,
        "checks": checks,
        "limitations": [
            "File validation does not assess perceptual video quality.",
            "SFX timing checks do not prove that every effect is audible.",
            "SRT structural validation does not prove word-level alignment.",
            "Successful validation does not guarantee YouTube policy compliance.",
        ],
    }

    write_json(REPORT, report)

    log("=" * 54)
    log("FINAL OUTPUT VALIDATION: PASSED")
    log("FINAL VIDEO:", checks["render"]["final_video"])
    log("DURATION:", checks["render"]["final_duration_seconds"], "seconds")
    log("THUMBNAIL:", checks["thumbnail"]["width"], "x", checks["thumbnail"]["height"])
    log("SFX CUES:", checks["sound_design"]["sfx_cue_count"])
    log("REPORT:", relative(REPORT))
    log("=" * 54)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            write_json(REPORT, {
                "status": "failed",
                "version": VERSION,
                "error": str(exc),
            })
        except Exception:
            pass

        log("FINAL OUTPUT VALIDATION: FAILED:", str(exc))
        sys.exit(1)
