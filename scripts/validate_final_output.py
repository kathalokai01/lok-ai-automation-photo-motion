
#!/usr/bin/env python3
"""Cross-manifest final-output validator for Katha Lok AI photo-motion."""

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
REPORT = OUT / "validation_report.json"
VERSION = "final-validator-v21-cross-manifest-checksums"

VIDEO_RESOLUTION = (1280, 720)
THUMBNAIL_RESOLUTION = (1280, 720)
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
EPSILON = 0.05


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_report(data):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temp = REPORT.with_suffix(".json.tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(REPORT)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, optional=False):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if optional:
            return None
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Invalid JSON structure: {path}")

    return value


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing file path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository root")

    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")

    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file is too small: {path.name}")

    return path


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def normalized_path(value, label):
    return relative(safe_file(value, label))


def pick(row, *keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def parse_bool(value, label, default=None):
    if value is None:
        if default is not None:
            return default
        raise RuntimeError(f"{label}: boolean value is missing")

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)

    value = str(value).strip().lower()
    if value in {"true", "yes", "1", "on", "enabled"}:
        return True
    if value in {"false", "no", "0", "off", "disabled"}:
        return False

    raise RuntimeError(f"{label}: invalid boolean value {value!r}")


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
            f"ffprobe failed for {path.name}: {result.stderr[-600:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid media metadata for {path.name}"
        ) from exc

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


def require_media(path, label, video=False, audio=False, resolution=None):
    info = media_info(path)

    if video and not info["has_video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream missing")

    if resolution and (info["width"], info["height"]) != resolution:
        raise RuntimeError(
            f"{label}: expected {resolution[0]}x{resolution[1]}, got "
            f"{info['width']}x{info['height']}"
        )

    return info


def expected_keys(parts, scenes):
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def ordered_scene_map(scene_rows, parts, scenes, label):
    expected = expected_keys(parts, scenes)

    if not isinstance(scene_rows, list) or len(scene_rows) != len(expected):
        count = len(scene_rows) if isinstance(scene_rows, list) else 0
        raise RuntimeError(
            f"{label}: expected {len(expected)} scene rows, found {count}"
        )

    result = {}

    for index, row in enumerate(scene_rows):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: invalid row {index + 1}")

        try:
            part = int(row["part"])
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index + 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{label}: invalid scene mapping at row {index + 1}"
            ) from exc

        key = (part, scene)
        if key != expected[index] or global_scene != index + 1:
            raise RuntimeError(
                f"{label}: scene order mismatch at row {index + 1}; "
                f"got {(part, scene, global_scene)}"
            )

        if key in result:
            raise RuntimeError(f"{label}: duplicate scene {key}")

        result[key] = row

    return result


def check_optional_hash(row, hash_key, path, label):
    expected = row.get(hash_key)
    if expected is None:
        return None

    if not isinstance(expected, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected
    ):
        raise RuntimeError(f"{label}: invalid {hash_key}")

    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise RuntimeError(f"{label}: {hash_key} mismatch")

    return actual


def validate_photo_motion(parts, scenes):
    manifest_path = OUT / "photo_motion" / "photo_motion_manifest.json"
    data = read_json(manifest_path)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest is not completed")

    scene_rows = rows(data, "scenes", "videos", "completed_scenes", "items")
    mapping = ordered_scene_map(scene_rows, parts, scenes, "Photo motion")
    result = {}

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene incomplete: {key}")

        path = safe_file(
            pick(
                row, "video", "source_video", "output",
                "video_path", "path", "file",
            ),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )

        require_media(
            path, f"Photo-motion scene {key}",
            video=True, resolution=VIDEO_RESOLUTION,
        )
        digest = check_optional_hash(
            row, "video_sha256", path, f"Photo-motion scene {key}"
        )

        result[key] = {
            "path": path,
            "relative_path": relative(path),
            "sha256": digest or sha256_file(path),
            "duration": media_info(path)["duration"],
        }

    return {
        "status": "valid",
        "scene_count": len(result),
        "resolution": list(VIDEO_RESOLUTION),
        "mapping": result,
    }


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
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = require_media(audio, f"Narration {key}", audio=True)
        digest = check_optional_hash(
            row, "audio_sha256", audio, f"Narration {key}"
        )

        mapping[key] = {
            "path": audio,
            "relative_path": relative(audio),
            "sha256": digest or sha256_file(audio),
            "duration": info["duration"],
        }

    expected = set(expected_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            "Narration scene mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "total_seconds": round(sum(v["duration"] for v in mapping.values()), 3),
        "mapping": mapping,
    }


def parse_timestamp(value):
    value = value.strip().replace(",", ".")
    pieces = value.split(":")
    if len(pieces) != 3:
        raise ValueError("timestamp must be HH:MM:SS.mmm")

    hours = int(pieces[0])
    minutes = int(pieces[1])
    seconds = float(pieces[2])

    if minutes >= 60 or seconds < 0 or seconds >= 60:
        raise ValueError("timestamp component outside valid range")

    result = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid timestamp")

    return result


def validate_srt(path, total_duration):
    content = path.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise RuntimeError("Final SRT is empty")

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [
        block.strip()
        for block in re.split(r"\n[ \t]*\n", normalized)
        if block.strip()
    ]

    valid_cues = 0
    previous_start = -1.0
    previous_index = 0

    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        timing_positions = [
            i for i, line in enumerate(lines) if "-->" in line
        ]

        if len(timing_positions) != 1:
            raise RuntimeError(
                f"SRT cue {index}: expected exactly one timing line"
            )

        timing_index = timing_positions[0]
        timing_line = lines[timing_index]

        try:
            start_text, end_text = timing_line.split("-->", 1)
            start = parse_timestamp(start_text)
            end = parse_timestamp(end_text.split()[0])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(
                f"SRT cue {index}: invalid timestamps"
            ) from exc

        if end <= start:
            raise RuntimeError(f"SRT cue {index}: end must follow start")
        if start + EPSILON < previous_start:
            raise RuntimeError(f"SRT cue {index}: cues are not time-ordered")
        if start > total_duration + 1.0:
            raise RuntimeError(f"SRT cue {index}: starts after video end")
        if end > total_duration + 1.5:
            raise RuntimeError(f"SRT cue {index}: ends after video end")

        text_lines = [
            line.strip()
            for line in lines[timing_index + 1:]
            if line.strip()
        ]
        if not text_lines:
            raise RuntimeError(f"SRT cue {index}: caption text is empty")

        # If a cue index is numeric, require it to increase monotonically.
        if lines and lines[0].strip().isdigit():
            cue_number = int(lines[0].strip())
            if cue_number <= previous_index:
                raise RuntimeError(f"SRT cue {index}: cue numbers are not increasing")
            previous_index = cue_number

        previous_start = start
        valid_cues += 1

    if valid_cues == 0:
        raise RuntimeError("SRT contains no valid cues")

    return valid_cues


def validate_captions(parts, scenes, narration_total):
    manifest = read_json(OUT / "captions" / "captions.json")

    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    caption_rows = manifest.get("captions")
    if not isinstance(caption_rows, list):
        raise RuntimeError("Caption manifest has no captions list")

    caption_keys = set()

    for row in caption_rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption row")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption scene mapping") from exc

        if key in caption_keys:
            raise RuntimeError(f"Duplicate caption scene: {key}")
        if not str(row.get("text", "")).strip():
            raise RuntimeError(f"Empty caption: {key}")

        caption_keys.add(key)

    expected = set(expected_keys(parts, scenes))
    if caption_keys != expected:
        raise RuntimeError(
            "Caption scene mismatch: "
            f"missing={sorted(expected - caption_keys)}; "
            f"extra={sorted(caption_keys - expected)}"
        )

    srt = safe_file(
        "output/captions/final_captions.srt",
        "Final SRT",
        10,
    )
    cue_count = validate_srt(srt, narration_total)

    return {
        "status": "valid",
        "scene_count": len(caption_keys),
        "srt_path": relative(srt),
        "srt_sha256": sha256_file(srt),
        "srt_cue_count": cue_count,
        "word_level_alignment_verified": False,
    }


def validate_sound_design(parts, scenes, narration_mapping):
    sound = read_json(OUT / "sound_design" / "sound_design_manifest.json")
    cues = read_json(OUT / "sound_design" / "sfx_cues.json")
    timing = read_json(OUT / "sound_design" / "sfx_timing_validation.json")

    if not isinstance(sound, dict) or sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")
    if not isinstance(cues, dict) or cues.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed")
    if not isinstance(timing, dict) or timing.get("status") != "completed":
        raise RuntimeError("SFX timing report is not completed")

    if timing.get("external_api_calls", 0) != 0:
        raise RuntimeError("SFX report declares external API calls")
    if timing.get("paid_services_used") is True:
        raise RuntimeError("SFX report declares paid-service usage")

    sfx_report = timing.get("sfx")
    if not isinstance(sfx_report, dict) or sfx_report.get("status") != "valid":
        raise RuntimeError("SFX timing report does not confirm valid status")

    expected_count = parts * scenes
    for field in ("expected_scenes", "narration_scenes_checked"):
        try:
            actual = int(timing.get(field, -1))
        except (TypeError, ValueError):
            actual = -1
        if actual != expected_count:
            raise RuntimeError(
                f"SFX report {field} mismatch: expected {expected_count}, "
                f"got {actual}"
            )

    enabled = sound.get("enabled", {})
    tracks = sound.get("tracks", {})

    if not isinstance(enabled, dict) or not isinstance(tracks, dict):
        raise RuntimeError("Invalid sound-design tracks structure")

    track_status = {}

    for name in ("music", "ambient", "sfx"):
        track = tracks.get(name)
        is_enabled = parse_bool(
            enabled.get(name),
            f"sound_design.enabled.{name}",
            default=(track is not None),
        )

        if not is_enabled:
            track_status[name] = "disabled"
            continue

        if not isinstance(track, dict) or track.get("status") != "completed":
            raise RuntimeError(f"Enabled sound track incomplete: {name}")

        audio = safe_file(
            pick(track, "path", "file", "output"),
            f"Sound track {name}",
            MIN_AUDIO_BYTES,
        )
        require_media(audio, f"Sound track {name}", audio=True)
        check_optional_hash(
            track, "sha256", audio, f"Sound track {name}"
        )
        track_status[name] = "valid"

    cue_rows = rows(cues, "cues", "items")
    sfx_enabled = parse_bool(
        enabled.get("sfx"),
        "sound_design.enabled.sfx",
        default=bool(cue_rows),
    )

    if sfx_enabled and not cue_rows:
        raise RuntimeError("SFX is enabled but the cue list is empty")
    if not sfx_enabled and cue_rows:
        raise RuntimeError("SFX is disabled but cues remain in the manifest")

    for index, cue in enumerate(cue_rows, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        try:
            key = (int(cue["part"]), int(cue["scene"]))
            timestamp = float(next(
                cue[name]
                for name in (
                    "time", "time_seconds", "timestamp",
                    "start_seconds", "start",
                )
                if cue.get(name) is not None
            ))
            gain = float(cue.get("gain", cue.get("volume", 0.06)))
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX cue fields: {index}") from exc

        if key not in narration_mapping:
            raise RuntimeError(f"SFX cue references unknown scene: {key}")
        if not math.isfinite(timestamp) or timestamp < 0:
            raise RuntimeError(f"Invalid SFX timestamp: {index}")
        if timestamp >= narration_mapping[key]["duration"]:
            raise RuntimeError(f"SFX occurs after narration ends: {key}")
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"Invalid SFX gain: {index}")

        audio = safe_file(
            pick(cue, "file", "path", "audio", "output", "audio_path"),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )
        require_media(audio, f"SFX cue {index}", audio=True)
        check_optional_hash(cue, "sha256", audio, f"SFX cue {index}")

    return {
        "status": "valid",
        "tracks": track_status,
        "sfx_enabled": sfx_enabled,
        "sfx_cue_count": len(cue_rows),
        "timing_report": "output/sound_design/sfx_timing_validation.json",
    }


def validate_render(mode, parts, scenes, photo_report, narration_mapping):
    data = read_json(OUT / "scene_renders" / "render_manifest.json")

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if data.get("format") != mode:
        raise RuntimeError("Render manifest format differs from input")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    mapping = ordered_scene_map(
        data.get("completed_scenes"), parts, scenes, "Rendered"
    )

    if int(data.get("completed_count", -1)) != len(mapping):
        raise RuntimeError("Render completed_count mismatch")

    duration_sum = 0.0
    cross_checks = []

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Rendered scene incomplete: {key}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Captions not confirmed for scene {key}")

        # Reconcile the source scene video with the matching photo-motion row.
        photo = photo_report["mapping"].get(key)
        if photo is None:
            raise RuntimeError(f"Photo-motion source missing for render scene {key}")

        source_value = pick(row, "source_video", "source", "photo_motion_video")
        if source_value:
            source = safe_file(
                source_value, f"Original scene {key}", MIN_VIDEO_BYTES
            )
            if relative(source) != photo["relative_path"]:
                raise RuntimeError(
                    f"Cross-manifest source mismatch for scene {key}: "
                    f"render={relative(source)}, photo_motion="
                    f"{photo['relative_path']}"
                )
            if sha256_file(source) != photo["sha256"]:
                raise RuntimeError(
                    f"Photo-motion source checksum mismatch for scene {key}"
                )
        else:
            raise RuntimeError(
                f"Render manifest lacks source_video for scene {key}"
            )

        # Reconcile narration paths so scene audio cannot silently shift.
        narration_value = pick(row, "narration", "narration_audio", "audio_source")
        if narration_value:
            narration_path = safe_file(
                narration_value, f"Narration source {key}", MIN_AUDIO_BYTES
            )
            expected_narration = narration_mapping[key]
            if relative(narration_path) != expected_narration["relative_path"]:
                raise RuntimeError(
                    f"Cross-manifest narration mismatch for scene {key}: "
                    f"render={relative(narration_path)}, narration="
                    f"{expected_narration['relative_path']}"
                )
            if sha256_file(narration_path) != expected_narration["sha256"]:
                raise RuntimeError(
                    f"Narration checksum mismatch for render scene {key}"
                )
        else:
            raise RuntimeError(
                f"Render manifest lacks narration path for scene {key}"
            )

        source_rendered_value = pick(
            row, "source_rendered_video", "pre_caption_video"
        )
        if source_rendered_value:
            source_rendered = safe_file(
                source_rendered_value,
                f"Pre-caption scene {key}",
                MIN_VIDEO_BYTES,
            )
            if source_rendered.resolve() == photo["path"].resolve():
                raise RuntimeError(
                    f"Pre-caption video unexpectedly equals photo-motion source: {key}"
                )

        video = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Captioned scene {key}",
            MIN_VIDEO_BYTES,
        )

        if video.resolve() == photo["path"].resolve():
            raise RuntimeError(
                f"Captioned video unexpectedly equals photo-motion source: {key}"
            )

        info = require_media(
            video, f"Captioned scene {key}",
            video=True, audio=True, resolution=VIDEO_RESOLUTION,
        )

        try:
            declared = float(row.get("duration", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene duration: {key}") from exc

        if not math.isfinite(declared) or declared <= 0:
            raise RuntimeError(f"Invalid scene duration: {key}")
        if abs(info["duration"] - declared) > 1.0:
            raise RuntimeError(f"Scene duration mismatch: {key}")

        check_optional_hash(row, "video_sha256", video, f"Rendered scene {key}")

        duration_sum += info["duration"]
        cross_checks.append({
            "part": key[0],
            "scene": key[1],
            "source_video": relative(photo["path"]),
            "narration": narration_mapping[key]["relative_path"],
            "rendered_video": relative(video),
            "source_mapping_verified": True,
            "narration_mapping_verified": True,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected {parts} part videos")

    part_files = []
    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                row_part = int(row.get("part", index))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid part number at row {index}") from exc

            if row_part != index:
                raise RuntimeError(f"Part order mismatch at row {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part {index} is not completed")

            value = pick(row, "path", "video", "output")
        else:
            value = row

        video = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = require_media(
            video, f"Part video {index}",
            video=True, audio=True, resolution=VIDEO_RESOLUTION,
        )
        part_files.append({
            "part": index,
            "path": relative(video),
            "duration": info["duration"],
            "sha256": sha256_file(video),
        })

    final_path = safe_file(
        data.get("final_video"), "Final video", MIN_VIDEO_BYTES
    )
    alias_path = safe_file(
        data.get("alias"), "Format-specific video", MIN_VIDEO_BYTES
    )

    final_info = require_media(
        final_path, "Final video",
        video=True, audio=True, resolution=VIDEO_RESOLUTION,
    )
    alias_info = require_media(
        alias_path, "Format-specific video",
        video=True, audio=True, resolution=VIDEO_RESOLUTION,
    )

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short" else "katha_lok_ai_full.mp4"
    )
    if alias_path.name != expected_alias:
        raise RuntimeError(
            f"Expected {expected_alias}, got {alias_path.name}"
        )
    if final_path.resolve() == alias_path.resolve():
        raise RuntimeError("Final video and alias must be distinct files")
    if abs(final_info["duration"] - alias_info["duration"]) > 1.0:
        raise RuntimeError("Final video and alias durations differ")
    if abs(final_info["duration"] - duration_sum) > max(2.0, len(mapping)):
        raise RuntimeError("Final duration differs from scene durations")

    part_duration_sum = sum(item["duration"] for item in part_files)
    if abs(final_info["duration"] - part_duration_sum) > max(2.0, parts):
        raise RuntimeError(
            "Final duration differs significantly from part durations"
        )

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "parts": part_files,
        "final_video": relative(final_path),
        "final_video_sha256": sha256_file(final_path),
        "alias": relative(alias_path),
        "alias_sha256": sha256_file(alias_path),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": list(VIDEO_RESOLUTION),
        "cross_manifest_scene_checks": cross_checks,
        "cross_manifest_scene_checks_count": len(cross_checks),
    }


def validate_thumbnail(mode):
    path = safe_file("output/thumbnail.jpg", "Thumbnail", MIN_IMAGE_BYTES)

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format
    except Exception as exc:
        raise RuntimeError(f"Thumbnail is invalid: {exc}") from exc

    if dimensions != THUMBNAIL_RESOLUTION:
        raise RuntimeError(
            f"Thumbnail must be {THUMBNAIL_RESOLUTION[0]}x"
            f"{THUMBNAIL_RESOLUTION[1]} for {mode}; got {dimensions}"
        )
    if image_format != "JPEG":
        raise RuntimeError(f"Expected JPEG thumbnail, got {image_format}")

    manifest = read_json(
        OUT / "thumbnail" / "thumbnail_manifest.json", optional=True
    )

    if isinstance(manifest, dict):
        if manifest.get("status") != "completed":
            raise RuntimeError("Thumbnail manifest is not completed")
        if manifest.get("format") != mode:
            raise RuntimeError("Thumbnail manifest format mismatch")
        if (
            manifest.get("width") != THUMBNAIL_RESOLUTION[0]
            or manifest.get("height") != THUMBNAIL_RESOLUTION[1]
        ):
            raise RuntimeError("Thumbnail manifest dimensions mismatch")

        manifest_path = pick(manifest, "thumbnail", "path", "output")
        if manifest_path:
            declared = safe_file(manifest_path, "Thumbnail manifest path")
            if declared.resolve() != path.resolve():
                raise RuntimeError("Thumbnail manifest path mismatch")

        expected_hash = manifest.get("thumbnail_sha256")
        if expected_hash and expected_hash != sha256_file(path):
            raise RuntimeError("Thumbnail checksum mismatch")

    return {
        "status": "valid",
        "path": relative(path),
        "sha256": sha256_file(path),
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

    required_labels = ("Title:", "Description:", "Tags:")
    missing = [label for label in required_labels if label not in content]
    if missing:
        raise RuntimeError(
            "YouTube metadata missing required sections: "
            + ", ".join(missing)
        )

    return {
        "status": "valid",
        "path": relative(path),
        "sha256": sha256_file(path),
        "characters": len(content),
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    log("FINAL VALIDATION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)
    log("EXPECTED VIDEO RESOLUTION: 1280x720")

    photo_report = validate_photo_motion(parts, scenes)
    narration_report = validate_narration(parts, scenes)
    narration_mapping = narration_report["mapping"]
    narration_total = narration_report["total_seconds"]

    checks = {
        "photo_motion": {
            key: value for key, value in photo_report.items()
            if key != "mapping"
        },
        "narration": {
            key: value for key, value in narration_report.items()
            if key != "mapping"
        },
        "captions": validate_captions(parts, scenes, narration_total),
        "sound_design": validate_sound_design(
            parts, scenes, narration_mapping
        ),
        "render": validate_render(
            mode, parts, scenes, photo_report, narration_mapping
        ),
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
        "resolution": {
            "width": VIDEO_RESOLUTION[0],
            "height": VIDEO_RESOLUTION[1],
        },
        "thumbnail_resolution": {
            "width": THUMBNAIL_RESOLUTION[0],
            "height": THUMBNAIL_RESOLUTION[1],
        },
        "validator_external_api_calls": 0,
        "checks": checks,
        "cross_manifest_validation": {
            "status": "valid",
            "scene_source_mapping_verified": True,
            "scene_narration_mapping_verified": True,
            "verified_scene_count": checks["render"][
                "cross_manifest_scene_checks_count"
            ],
        },
        "limitations": [
            "Validation cannot assess perceptual video quality.",
            "SFX validation does not prove each effect is audible.",
            "SRT timing checks do not prove word-level speech alignment.",
            "Validation does not guarantee YouTube policy compliance.",
            "Landscape resolution is checked; vertical-safe framing is not.",
            "Caption burn status is checked from manifests and media paths; "
            "pixel-level OCR verification is not performed.",
        ],
    }

    write_report(report)
    log("FINAL VALIDATION: PASSED")
    log("CROSS-MANIFEST SCENES:", checks["render"]["cross_manifest_scene_checks_count"])
    log("FINAL VIDEO:", checks["render"]["final_video"])
    log("VIDEO RESOLUTION: 1280x720")
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
