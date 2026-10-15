
#!/usr/bin/env python3
"""Validate all final deliverables before packaging."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUT = ROOT / "output"
REPORT = OUT / "validation_report.json"
VERSION = "final-validator-v18-youtube-16x9"
MIN_VIDEO = 1000
MIN_AUDIO = 100
MIN_IMAGE = 1000


def log(*args):
    print(*args, flush=True)


def read_json(path, optional=False):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if optional:
            return None
        raise RuntimeError(f"Cannot read {path.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Invalid JSON structure: {path.relative_to(ROOT)}")
    return value


def write_report(data):
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


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing file path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file is too small: {path.name}")
    return path


def pick(row, *keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def rows(data, *keys):
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
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr[-500:]}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_media(path, label, video=False, audio=False, resolution=None):
    info = media_info(path)
    if video and not info["has_video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream missing")
    if resolution and (info["width"], info["height"]) != resolution:
        raise RuntimeError(
            f"{label}: expected {resolution}, got "
            f"{(info['width'], info['height'])}"
        )
    return info


def expected_scene_keys(parts, scenes):
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def check_order(scene_rows, parts, scenes, label):
    expected = expected_scene_keys(parts, scenes)
    if len(scene_rows) != len(expected):
        raise RuntimeError(
            f"{label}: expected {len(expected)} scenes, found {len(scene_rows)}"
        )

    mapping = {}
    for index, row in enumerate(scene_rows):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: invalid scene row {index + 1}")
        try:
            key = (
                int(row["part"]),
                int(row.get("local_scene", row.get("scene", 0))),
            )
            global_scene = int(row.get("global_scene", index + 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"{label}: invalid scene mapping") from exc

        if key != expected[index] or global_scene != index + 1:
            raise RuntimeError(
                f"{label}: scene order mismatch at row {index + 1}"
            )
        if key in mapping:
            raise RuntimeError(f"{label}: duplicate scene {key}")
        mapping[key] = row

    return mapping


def validate_photo_motion(parts, scenes, resolution):
    path = OUT / "photo_motion" / "photo_motion_manifest.json"
    data = read_json(path)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest is not completed")

    mapping = check_order(
        rows(data, "scenes", "videos", "completed_scenes", "items"),
        parts, scenes, "Photo motion",
    )

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene incomplete: {key}")
        video = safe_file(
            pick(row, "video", "source_video", "output", "video_path", "path", "file"),
            f"Photo-motion scene {key}", MIN_VIDEO,
        )
        require_media(video, f"Photo-motion scene {key}", video=True,
                      resolution=resolution)

        expected_hash = row.get("video_sha256")
        if expected_hash:
            digest = hashlib.sha256(video.read_bytes()).hexdigest()
            if digest != expected_hash:
                raise RuntimeError(f"Photo-motion hash mismatch: {key}")

    return {"status": "valid", "scene_count": len(mapping)}


def validate_narration(parts, scenes):
    data = read_json(OUT / "narration" / "audio_jobs.json")
    if not isinstance(data, dict):
        raise RuntimeError("Narration manifest must be an object")

    mapping = {}
    for row in rows(data, "jobs", "audio_jobs", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid narration row")
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid narration scene mapping") from exc
        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration incomplete: {key}")

        audio = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}", MIN_AUDIO,
        )
        info = require_media(audio, f"Narration {key}", audio=True)
        mapping[key] = info["duration"]

    expected = set(expected_scene_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            f"Narration scene mismatch: missing={sorted(expected - set(mapping))}"
        )

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "total_seconds": round(sum(mapping.values()), 3),
        "durations": mapping,
    }


def validate_captions(parts, scenes):
    manifest = read_json(OUT / "captions" / "captions.json")
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    caption_rows = manifest.get("captions", [])
    caption_map = set()
    for row in caption_rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption row")
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption mapping") from exc
        if key in caption_map:
            raise RuntimeError(f"Duplicate caption: {key}")
        if not str(row.get("text", "")).strip():
            raise RuntimeError(f"Empty caption: {key}")
        caption_map.add(key)

    expected = set(expected_scene_keys(parts, scenes))
    if caption_map != expected:
        raise RuntimeError(
            f"Caption scene mismatch: missing={sorted(expected - caption_map)}"
        )

    srt = safe_file(
        "output/captions/final_captions.srt", "Final SRT", 10
    )
    content = srt.read_text(encoding="utf-8-sig")
    if "-->" not in content:
        raise RuntimeError("SRT contains no timing cues")

    return {
        "status": "valid",
        "scene_count": len(caption_map),
        "srt_path": srt.relative_to(ROOT).as_posix(),
        "word_level_alignment_verified": False,
    }


def validate_sound_design(parts, scenes, narration_durations):
    sound = read_json(OUT / "sound_design" / "sound_design_manifest.json")
    cues = read_json(OUT / "sound_design" / "sfx_cues.json")
    timing = read_json(OUT / "sound_design" / "sfx_timing_validation.json")

    if not isinstance(sound, dict) or sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")
    if not isinstance(cues, dict) or cues.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed")
    if not isinstance(timing, dict) or timing.get("status") != "completed":
        raise RuntimeError("SFX timing report is not completed")
    if timing.get("sfx", {}).get("status") != "valid":
        raise RuntimeError("SFX timing report does not confirm valid status")

    expected_count = parts * scenes
    if int(timing.get("expected_scenes", -1)) != expected_count:
        raise RuntimeError("SFX expected scene count mismatch")
    if int(timing.get("narration_scenes_checked", -1)) != expected_count:
        raise RuntimeError("SFX report did not check all narration scenes")

    enabled = sound.get("enabled", {})
    tracks = sound.get("tracks", {})
    if not isinstance(enabled, dict) or not isinstance(tracks, dict):
        raise RuntimeError("Invalid sound-design tracks structure")

    track_status = {}
    for name in ("music", "ambient", "sfx"):
        track = tracks.get(name)
        is_enabled = bool(enabled.get(name, track is not None))
        if not is_enabled:
            track_status[name] = "disabled"
            continue
        if not isinstance(track, dict) or track.get("status") != "completed":
            raise RuntimeError(f"Enabled sound track incomplete: {name}")
        audio = safe_file(
            pick(track, "path", "file", "output"),
            f"Sound track {name}", MIN_AUDIO,
        )
        require_media(audio, f"Sound track {name}", audio=True)
        track_status[name] = "valid"

    cue_rows = rows(cues, "cues", "items")
    for index, cue in enumerate(cue_rows, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")
        try:
            key = (int(cue["part"]), int(cue["scene"]))
            timestamp = float(next(
                cue[k] for k in (
                    "time", "time_seconds", "timestamp", "start_seconds", "start"
                ) if cue.get(k) is not None
            ))
            gain = float(cue.get("gain", cue.get("volume", 0.06)))
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX cue fields: {index}") from exc

        if key not in narration_durations:
            raise RuntimeError(f"SFX cue references unknown scene: {key}")
        if not math.isfinite(timestamp) or timestamp < 0:
            raise RuntimeError(f"Invalid SFX timestamp: {index}")
        if timestamp >= narration_durations[key]:
            raise RuntimeError(f"SFX occurs after narration ends: {key}")
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"Invalid SFX gain: {index}")

        audio = safe_file(
            pick(cue, "file", "path", "audio", "output", "audio_path"),
            f"SFX cue {index}", MIN_AUDIO,
        )
        require_media(audio, f"SFX cue {index}", audio=True)

    return {
        "status": "valid",
        "tracks": track_status,
        "sfx_cue_count": len(cue_rows),
        "timing_report": "output/sound_design/sfx_timing_validation.json",
    }


def validate_render(mode, parts, scenes, resolution):
    data = read_json(OUT / "scene_renders" / "render_manifest.json")
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if data.get("format") != mode:
        raise RuntimeError("Render format differs from input")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    scene_rows = data.get("completed_scenes")
    if not isinstance(scene_rows, list):
        raise RuntimeError("Rendered scene rows are missing")
    mapping = check_order(scene_rows, parts, scenes, "Rendered")

    if int(data.get("completed_count", -1)) != len(mapping):
        raise RuntimeError("Render completed_count mismatch")

    duration_sum = 0.0
    for key, row in mapping.items():
        if row.get("status") != "completed" or row.get("captions_burned") is not True:
            raise RuntimeError(f"Rendered scene or captions incomplete: {key}")

        source = safe_file(
            pick(row, "source_rendered_video", "source_video", "source"),
            f"Original scene {key}", MIN_VIDEO,
        )
        video = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Captioned scene {key}", MIN_VIDEO,
        )
        if source.resolve() == video.resolve():
            raise RuntimeError(f"Original and captioned paths match: {key}")

        info = require_media(
            video, f"Captioned scene {key}", video=True, audio=True,
            resolution=resolution,
        )
        try:
            declared = float(row.get("duration", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene duration: {key}") from exc
        if not math.isfinite(declared) or declared <= 0:
            raise RuntimeError(f"Invalid scene duration: {key}")
        if abs(info["duration"] - declared) > 1.0:
            raise RuntimeError(f"Scene duration mismatch: {key}")
        duration_sum += info["duration"]

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected {parts} part videos")

    part_files = []
    for index, row in enumerate(part_rows, 1):
        value = pick(row, "path", "video", "output") if isinstance(row, dict) else row
        video = safe_file(value, f"Part video {index}", MIN_VIDEO)
        require_media(video, f"Part video {index}", video=True, audio=True,
                      resolution=resolution)
        part_files.append(video.relative_to(ROOT).as_posix())

    final_path = safe_file(data.get("final_video"), "Final video", MIN_VIDEO)
    alias_path = safe_file(data.get("alias"), "Format-specific video", MIN_VIDEO)
    final_info = require_media(
        final_path, "Final video", video=True, audio=True, resolution=resolution
    )
    alias_info = require_media(
        alias_path, "Format-specific video", video=True, audio=True,
        resolution=resolution,
    )

    expected_alias = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias_path.name != expected_alias:
        raise RuntimeError(f"Expected {expected_alias}, got {alias_path.name}")
    if abs(final_info["duration"] - alias_info["duration"]) > 1.0:
        raise RuntimeError("Final video and alias durations differ")
    if abs(final_info["duration"] - duration_sum) > max(2.0, len(mapping)):
        raise RuntimeError("Final video duration differs from scene durations")

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "parts": part_files,
        "final_video": final_path.relative_to(ROOT).as_posix(),
        "alias": alias_path.relative_to(ROOT).as_posix(),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": list(resolution),
    }


def validate_thumbnail(mode):
    path = safe_file("output/thumbnail.jpg", "Thumbnail", MIN_IMAGE)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format
    except Exception as exc:
        raise RuntimeError(f"Thumbnail is invalid: {exc}") from exc

    # YouTube video thumbnails use 16:9 for both Shorts and full videos.
    expected = (1280, 720)
    if dimensions != expected:
        raise RuntimeError(
            f"Thumbnail must be 1280x720 for {mode}; got {dimensions}"
        )
    if image_format != "JPEG":
        raise RuntimeError(f"Expected JPEG thumbnail, got {image_format}")

    manifest = read_json(OUT / "thumbnail" / "thumbnail_manifest.json",
                         optional=True)
    if isinstance(manifest, dict):
        if manifest.get("status") != "completed":
            raise RuntimeError("Thumbnail manifest is not completed")
        if manifest.get("format") != mode:
            raise RuntimeError("Thumbnail manifest format mismatch")
        if manifest.get("width") != 1280 or manifest.get("height") != 720:
            raise RuntimeError("Thumbnail manifest dimensions mismatch")

    return {
        "status": "valid",
        "path": "output/thumbnail.jpg",
        "width": dimensions[0],
        "height": dimensions[1],
        "format": image_format,
        "bytes": path.stat().st_size,
    }


def validate_metadata():
    path = safe_file("output/youtube_metadata.txt", "YouTube metadata", 10)
    content = path.read_text(encoding="utf-8-sig").strip()
    if len(content) < 10:
        raise RuntimeError("YouTube metadata is empty")
    return {
        "status": "valid",
        "path": "output/youtube_metadata.txt",
        "characters": len(content),
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
    log("FINAL VALIDATION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)

    narration_report = validate_narration(parts, scenes)
    narration_durations = narration_report.pop("durations")

    checks = {
        "photo_motion": validate_photo_motion(parts, scenes, resolution),
        "narration": narration_report,
        "captions": validate_captions(parts, scenes),
        "sound_design": validate_sound_design(parts, scenes, narration_durations),
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
            "File validation cannot assess perceptual video quality.",
            "SFX validation does not prove that each effect is audible.",
            "SRT presence does not prove word-level speech alignment.",
            "Validation does not guarantee YouTube policy compliance.",
        ],
    }
    write_report(report)

    log("FINAL VALIDATION: PASSED")
    log("FINAL VIDEO:", checks["render"]["final_video"])
    log("THUMBNAIL: 1280x720")
    log("REPORT: output/validation_report.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            write_report({
                "status": "failed",
                "version": VERSION,
                "error": str(exc),
            })
        except Exception:
            pass
        log("FINAL VALIDATION: FAILED:", str(exc))
        sys.exit(1)
