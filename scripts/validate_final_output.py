
#!/usr/bin/env python3
"""Validate scene assets, sound-design files, captions, and final video."""

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
FINAL_SRT = OUT / "captions" / "final_captions.srt"
THUMBNAIL = OUT / "thumbnail.jpg"
METADATA = OUT / "youtube_metadata.txt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v13-audio-assets"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.0


def log(*args):
    print(*args, flush=True)


def read_json(path, required=True):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if required:
            raise RuntimeError(f"Cannot read {path}: {exc}") from exc
        return {}

    if not isinstance(data, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path}")
    return data


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
        raise RuntimeError(f"{label}: file too small: {path.name}")
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
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required")

    result = subprocess.run(
        [
            executable, "-v", "error",
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
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")

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
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_video(path, label, resolution=None, require_audio=True):
    info = media_info(path)
    if not info["has_video"]:
        raise RuntimeError(f"{label}: missing video stream")
    if require_audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: missing audio stream")
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

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1
        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(
                f"{label}: wrong scene order at row {index}: "
                f"{(part, scene, global_scene)}"
            )

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"{label}: duplicate scene {key}")
        mapping[key] = row

    return mapping


def validate_photo_motion(parts, scenes, resolution):
    data = read_json(PHOTO_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Photo-motion manifest must be an object")
    if data.get("status") not in (None, "completed"):
        raise RuntimeError("Photo-motion manifest is incomplete")

    rows = rows_from(
        data, "scenes", "videos", "completed_scenes", "items"
    )
    mapping = check_scene_order(rows, parts, scenes, "Photo motion")

    for key, row in mapping.items():
        path = safe_file(
            first_path(
                row,
                (
                    "source_video", "video", "output",
                    "video_path", "rendered_video", "path", "file",
                ),
            ),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )
        require_video(
            path, f"Photo-motion scene {key}",
            resolution, require_audio=False,
        )

    return {"status": "valid", "scene_count": len(mapping)}


def validate_narration(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Narration manifest must be an object")

    mapping = {}
    for row in rows_from(data, "jobs", "audio_jobs"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid narration job")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid narration mapping") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration incomplete: {key}")

        path = safe_file(
            first_path(
                row, ("output", "audio_path", "path", "file", "audio")
            ),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {key}")
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
            sum(item["duration"] for item in mapping.values()), 3
        ),
    }


def validate_captions(parts, scenes):
    data = read_json(CAPTION_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is incomplete")

    mapping = {}
    for row in data.get("captions", []):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption row")
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption mapping") from exc

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

    srt = safe_file(relative(FINAL_SRT), "Final SRT", 10)
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
            f"SRT cue mismatch: expected {expected_count}, found {len(cues)}"
        )

    def seconds(value):
        hours, minutes, remainder = value.split(":")
        sec, millis = remainder.split(",")
        return (
            int(hours) * 3600
            + int(minutes) * 60
            + int(sec)
            + int(millis) / 1000
        )

    previous_end = -1.0
    for index, cue in enumerate(cues, start=1):
        start, end = (seconds(value) for value in cue.groups())
        if end <= start:
            raise RuntimeError(f"SRT cue {index} has invalid duration")
        if start < previous_end:
            raise RuntimeError(f"SRT cue {index} overlaps prior cue")
        previous_end = end

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "srt_cues": len(cues),
        "last_caption_end_seconds": round(previous_end, 3),
    }


def validate_sound_design():
    if not SOUND_MANIFEST.is_file():
        return {
            "status": "manifest_missing",
            "note": "Sound design cannot be confirmed without its manifest.",
        }

    data = read_json(SOUND_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Sound-design manifest must be an object")
    if data.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is incomplete")
    if data.get("external_api_calls", 0) != 0:
        raise RuntimeError("Sound design reports unexpected API calls")
    if data.get("paid_services_used") is True:
        raise RuntimeError("Sound design reports paid-service usage")

    tracks = data.get("tracks", {})
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks must be an object")

    track_report = {}
    enabled = data.get("enabled", {})
    if not isinstance(enabled, dict):
        enabled = {}

    for name in ("music", "ambient", "sfx"):
        track = tracks.get(name)

        # Some versions use an enabled flag to deliberately disable a track.
        is_enabled = enabled.get(name, True)
        if track is None:
            if is_enabled:
                raise RuntimeError(
                    f"Sound-design track {name!r} is enabled but missing"
                )
            track_report[name] = {"status": "disabled"}
            continue

        if not isinstance(track, dict):
            raise RuntimeError(f"Invalid sound-design track: {name}")

        track_status = str(track.get("status", "")).lower()
        if track_status != "completed":
            raise RuntimeError(
                f"Sound-design track {name!r} is not completed"
            )

        path_value = first_path(track, ("path", "file", "output"))
        if not path_value:
            if name == "sfx" and not is_enabled:
                track_report[name] = {"status": "disabled"}
                continue
            raise RuntimeError(f"Sound-design track path missing: {name}")

        path = safe_file(path_value, f"Sound-design track {name}", MIN_AUDIO_BYTES)
        info = media_info(path)
        if not info["has_audio"]:
            raise RuntimeError(f"Sound-design track has no audio: {name}")

        track_report[name] = {
            "status": "valid",
            "path": relative(path),
            "duration_seconds": round(info["duration"], 3),
            "bytes": path.stat().st_size,
        }

    cue_report = {"status": "not_present", "cue_count": 0}
    if CUE_MANIFEST.is_file():
        cues_data = read_json(CUE_MANIFEST)
        if isinstance(cues_data, dict):
            if cues_data.get("status") not in (None, "completed"):
                raise RuntimeError("SFX cue manifest is incomplete")
            cues = rows_from(cues_data, "cues", "items")
        else:
            cues = rows_from(cues_data)

        valid_cues = []
        for index, cue in enumerate(cues, start=1):
            if not isinstance(cue, dict):
                raise RuntimeError(f"Invalid SFX cue row {index}")

            try:
                part = int(cue["part"])
                scene = int(cue["scene"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid SFX cue mapping at row {index}") from exc

            if part < 1 or scene < 1:
                raise RuntimeError(f"Invalid SFX cue scene at row {index}")

            try:
                timestamp = float(cue.get("time", 0))
                gain = float(cue.get("gain", 1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid SFX cue timing/gain at row {index}") from exc

            if not math.isfinite(timestamp) or timestamp < 0:
                raise RuntimeError(f"Invalid SFX cue timestamp at row {index}")
            if not math.isfinite(gain) or gain < 0:
                raise RuntimeError(f"Invalid SFX cue gain at row {index}")

            path = safe_file(
                first_path(cue, ("file", "path", "audio", "output")),
                f"SFX cue {index}",
                MIN_AUDIO_BYTES,
            )
            audio_info = media_info(path)
            if not audio_info["has_audio"]:
                raise RuntimeError(f"SFX cue file has no audio: {index}")

            valid_cues.append({
                "part": part,
                "scene": scene,
                "time": timestamp,
                "file": relative(path),
            })

        cue_report = {
            "status": "valid",
            "cue_count": len(valid_cues),
            "cues": valid_cues,
        }

    return {
        "status": "valid",
        "external_api_calls": data.get("external_api_calls", 0),
        "paid_services_used": data.get("paid_services_used", False),
        "tracks": track_report,
        "sfx_cues": cue_report,
    }


def validate_render(mode, parts, scenes, resolution):
    data = read_json(RENDER_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest is incomplete")
    if data.get("format") != mode:
        raise RuntimeError("Render format differs from Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Final manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list):
        raise RuntimeError("Rendered scene list missing")

    mapping = check_scene_order(rows, parts, scenes, "Rendered")
    if int(data.get("completed_count", -1)) != len(mapping):
        raise RuntimeError("Render completed_count mismatch")

    scene_durations = []
    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Rendered scene incomplete: {key}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Captions not burned for scene {key}")

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

        info = require_video(output, f"Captioned scene {key}", resolution)
        try:
            declared = float(row.get("duration", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene duration: {key}") from exc

        if not math.isfinite(declared) or declared <= 0:
            raise RuntimeError(f"Invalid declared scene duration: {key}")
        if abs(info["duration"] - declared) > DURATION_TOLERANCE:
            raise RuntimeError(f"Scene duration mismatch: {key}")

        scene_durations.append(info["duration"])

    part_values = data.get("parts")
    if not isinstance(part_values, list) or len(part_values) != parts:
        raise RuntimeError(f"Expected {parts} part videos")

    part_report = []
    for index, value in enumerate(part_values, start=1):
        if isinstance(value, dict):
            value = first_path(value, ("path", "video", "output"))
        path = safe_file(value, f"Part {index}", MIN_VIDEO_BYTES)
        info = require_video(path, f"Part {index}", resolution)
        part_report.append({
            "path": relative(path),
            "duration_seconds": round(info["duration"], 3),
            "bytes": path.stat().st_size,
        })

    final_path = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias_path = safe_file(data.get("alias"), "Final alias", MIN_VIDEO_BYTES)
    final_info = require_video(final_path, "Final video", resolution)
    alias_info = require_video(alias_path, "Final alias", resolution)

    if abs(final_info["duration"] - alias_info["duration"]) > DURATION_TOLERANCE:
        raise RuntimeError("Final video and alias durations differ")

    scene_sum = sum(scene_durations)
    if abs(final_info["duration"] - scene_sum) > max(
        2.0, DURATION_TOLERANCE * len(scene_durations)
    ):
        raise RuntimeError("Final duration differs substantially from scene durations")

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "scene_duration_seconds": round(scene_sum, 3),
        "parts": part_report,
        "final_video": relative(final_path),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": list(resolution),
        "alias": relative(alias_path),
        "final_bytes": final_path.stat().st_size,
    }


def validate_optional_deliverables():
    result = {}

    if THUMBNAIL.is_file():
        path = safe_file(relative(THUMBNAIL), "Thumbnail", 1000)
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format

            if width < 640 or height < 360:
                raise RuntimeError(f"Thumbnail too small: {width}x{height}")

            result["thumbnail"] = {
                "status": "valid",
                "path": relative(path),
                "format": image_format,
                "width": width,
                "height": height,
            }
        except ImportError:
            result["thumbnail"] = {
                "status": "present",
                "path": relative(path),
                "note": "Pillow unavailable; dimensions not checked",
            }
        except Exception as exc:
            raise RuntimeError(f"Invalid thumbnail: {exc}") from exc
    else:
        result["thumbnail"] = {"status": "missing"}

    if METADATA.is_file():
        path = safe_file(relative(METADATA), "Metadata", 10)
        text = path.read_text(encoding="utf-8-sig").strip()
        if len(text) < 10:
            raise RuntimeError("Metadata file is empty")
        result["metadata"] = {
            "status": "valid",
            "path": relative(path),
            "characters": len(text),
        }
    else:
        result["metadata"] = {"status": "missing"}

    return result


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    resolution = (1280, 720) if mode == "full" else (720, 1280)

    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")

    log("FINAL OUTPUT VALIDATION")
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)

    checks = {
        "photo_motion": validate_photo_motion(parts, scenes, resolution),
        "narration": validate_narration(parts, scenes),
        "captions": validate_captions(parts, scenes),
        "sound_design": validate_sound_design(),
        "render": validate_render(mode, parts, scenes, resolution),
        "optional_deliverables": validate_optional_deliverables(),
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
            "Manifest and file validation cannot prove that sound is audible "
            "in the final mix; the final audio must be listened to separately.",
            "A missing sound-design manifest is reported rather than treated "
            "as proof that music, ambience, or SFX were mixed.",
        ],
    }
    write_json(REPORT, report)

    log("=" * 54)
    log("FINAL OUTPUT VALIDATION: PASSED")
    log("FINAL VIDEO:", checks["render"]["final_video"])
    log("DURATION:", checks["render"]["final_duration_seconds"], "seconds")
    log("SOUND DESIGN:", checks["sound_design"]["status"])
    log("REPORT:", relative(REPORT))
    log("=" * 54)


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
