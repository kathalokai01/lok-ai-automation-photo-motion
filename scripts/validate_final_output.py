
#!/usr/bin/env python3
"""Schema-compatible final validator for Katha Lok AI photo-motion."""

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
OUT = ROOT / "output"
REPORT = OUT / "validation_report.json"
RENDER_MANIFEST = OUT / "scene_renders" / "render_manifest.json"
VERSION = "final-validator-v22-sidecar-schema-compatible"
RESOLUTION = (1280, 720)
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000
EPSILON = 0.10

sys.path.insert(0, str(ROOT / "scripts"))
from input_config import load_and_validate, get_format, get_parts, get_scenes


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def read_json(path, optional=False):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if optional:
            return None
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc

    if not isinstance(data, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path}")
    return data


def rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def pick(row, *keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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
            f"ffprobe failed for {path.name}: {result.stderr[-500:]}"
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


def scene_key(row, index, scenes):
    try:
        global_scene = int(row.get("global_scene", index))
        part = int(row.get("part", (global_scene - 1) // scenes + 1))
        scene = int(
            row.get(
                "local_scene",
                row.get("scene", (global_scene - 1) % scenes + 1),
            )
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

    return part, scene, global_scene


def ordered_mapping(scene_rows, parts, scenes, label):
    expected = expected_keys(parts, scenes)

    if not isinstance(scene_rows, list) or len(scene_rows) != len(expected):
        count = len(scene_rows) if isinstance(scene_rows, list) else 0
        raise RuntimeError(
            f"{label}: expected {len(expected)} scene rows, found {count}"
        )

    result = {}

    for index, row in enumerate(scene_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: invalid row {index}")

        part, scene, global_scene = scene_key(row, index, scenes)
        key = (part, scene)

        if key != expected[index - 1] or global_scene != index:
            raise RuntimeError(
                f"{label}: order mismatch at row {index}; "
                f"got part={part}, scene={scene}, "
                f"global_scene={global_scene}"
            )

        if key in result:
            raise RuntimeError(f"{label}: duplicate scene {key}")

        result[key] = row

    return result


def check_optional_hash(row, keys, path, label):
    declared = pick(row, *keys)
    if declared is None:
        return sha256_file(path)

    if not re.fullmatch(r"[0-9a-fA-F]{64}", declared):
        raise RuntimeError(f"{label}: invalid SHA-256 field")

    actual = sha256_file(path)
    if actual.lower() != declared.lower():
        raise RuntimeError(f"{label}: SHA-256 mismatch")

    return actual


def validate_photo_motion(parts, scenes):
    path = OUT / "photo_motion" / "photo_motion_manifest.json"
    data = read_json(path)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest is not completed")

    mapping = ordered_mapping(
        rows(data, "scenes", "videos", "completed_scenes", "items"),
        parts,
        scenes,
        "Photo motion",
    )

    result = {}

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene incomplete: {key}")

        video = safe_file(
            pick(
                row, "video", "source_video", "output",
                "video_path", "path", "file",
            ),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )

        info = require_media(
            video,
            f"Photo-motion scene {key}",
            video=True,
            resolution=RESOLUTION,
        )

        digest = check_optional_hash(
            row,
            ("video_sha256", "sha256"),
            video,
            f"Photo-motion scene {key}",
        )

        result[key] = {
            "path": video,
            "relative_path": relative(video),
            "sha256": digest,
            "duration": info["duration"],
        }

    return {
        "status": "valid",
        "scene_count": len(result),
        "resolution": list(RESOLUTION),
        "mapping": result,
    }


def validate_narration(parts, scenes):
    data = read_json(OUT / "narration" / "audio_jobs.json")
    mapping = {}

    for row in rows(data, "jobs", "audio_jobs", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid narration row")

        try:
            key = (
                int(row["part"]),
                int(row.get("scene", row.get("local_scene"))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid narration scene mapping") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")

        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration scene is not completed: {key}")

        audio = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )

        info = require_media(audio, f"Narration {key}", audio=True)
        digest = check_optional_hash(
            row, ("audio_sha256", "sha256"), audio,
            f"Narration {key}",
        )

        mapping[key] = {
            "path": audio,
            "relative_path": relative(audio),
            "sha256": digest,
            "duration": info["duration"],
        }

    expected = set(expected_keys(parts, scenes))

    if set(mapping) != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "total_seconds": round(
            sum(item["duration"] for item in mapping.values()), 3
        ),
        "mapping": mapping,
    }


def parse_timestamp(value):
    value = value.strip().replace(",", ".")
    pieces = value.split(":")

    if len(pieces) != 3:
        raise ValueError("Expected HH:MM:SS.mmm")

    hours = int(pieces[0])
    minutes = int(pieces[1])
    seconds = float(pieces[2])

    if minutes >= 60 or seconds < 0 or seconds >= 60:
        raise ValueError("Timestamp component out of range")

    result = hours * 3600 + minutes * 60 + seconds

    if not math.isfinite(result) or result < 0:
        raise ValueError("Invalid timestamp")

    return result


def validate_srt(path, duration):
    content = path.read_text(encoding="utf-8-sig").strip()

    if not content:
        raise RuntimeError("Final SRT is empty")

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", normalized)

    cue_count = 0
    previous_start = -1.0
    previous_index = 0

    for number, block in enumerate(blocks, 1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing = [line for line in lines if "-->" in line]

        if len(timing) != 1:
            raise RuntimeError(f"SRT cue {number}: invalid timing line")

        try:
            start_text, end_text = timing[0].split("-->", 1)
            start = parse_timestamp(start_text)
            end = parse_timestamp(end_text.split()[0])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(
                f"SRT cue {number}: invalid timestamps"
            ) from exc

        if end <= start:
            raise RuntimeError(f"SRT cue {number}: end must follow start")

        if start + EPSILON < previous_start:
            raise RuntimeError(f"SRT cue {number}: timestamps are not ordered")

        if start > duration + 1.0 or end > duration + 1.5:
            raise RuntimeError(f"SRT cue {number}: timestamp exceeds video duration")

        timing_index = lines.index(timing[0])

        if not any(line.strip() for line in lines[timing_index + 1:]):
            raise RuntimeError(f"SRT cue {number}: caption text is empty")

        if lines[0].isdigit():
            cue_index = int(lines[0])
            if cue_index <= previous_index:
                raise RuntimeError(
                    f"SRT cue {number}: cue numbering is not increasing"
                )
            previous_index = cue_index

        previous_start = start
        cue_count += 1

    if cue_count == 0:
        raise RuntimeError("SRT contains no cues")

    return cue_count


def validate_captions(parts, scenes, narration_total):
    path = OUT / "captions" / "captions.json"
    data = read_json(path)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    caption_rows = data.get("captions")

    if not isinstance(caption_rows, list):
        raise RuntimeError("Caption manifest has no captions list")

    mapping = {}

    for row in caption_rows:
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

        mapping[key] = row

    expected = set(expected_keys(parts, scenes))

    if set(mapping) != expected:
        raise RuntimeError(
            "Caption mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    srt = safe_file(
        "output/captions/final_captions.srt",
        "Final SRT",
        10,
    )

    cue_count = validate_srt(srt, narration_total)

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "srt_path": relative(srt),
        "srt_sha256": sha256_file(srt),
        "srt_cue_count": cue_count,
        "word_level_alignment_verified": False,
    }


def validate_sound_design(parts, scenes, narration_mapping):
    base = OUT / "sound_design"

    sound = read_json(base / "sound_design_manifest.json")
    cues = read_json(base / "sfx_cues.json")
    timing = read_json(base / "sfx_timing_validation.json")

    if not isinstance(sound, dict) or sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")

    if not isinstance(cues, dict) or cues.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed")

    if not isinstance(timing, dict) or timing.get("status") != "completed":
        raise RuntimeError("SFX timing report is not completed")

    if timing.get("external_api_calls", 0) not in (0, None):
        raise RuntimeError("SFX timing report declares external API calls")

    if timing.get("paid_services_used") is True:
        raise RuntimeError("SFX timing report declares paid services")

    enabled = sound.get("enabled", {})
    tracks = sound.get("tracks", {})

    if not isinstance(enabled, dict) or not isinstance(tracks, dict):
        raise RuntimeError("Invalid sound-design track structure")

    track_status = {}

    for name in ("music", "ambient"):
        active = enabled.get(name, tracks.get(name) is not None)

        if isinstance(active, str):
            active = active.strip().lower() in {
                "true", "yes", "1", "on", "enabled"
            }

        if not active:
            track_status[name] = "disabled"
            continue

        track = tracks.get(name)

        if not isinstance(track, dict) or track.get("status") != "completed":
            raise RuntimeError(f"Enabled sound track is incomplete: {name}")

        audio = safe_file(
            pick(track, "path", "file", "output"),
            f"Sound track {name}",
            MIN_AUDIO_BYTES,
        )

        require_media(audio, f"Sound track {name}", audio=True)
        check_optional_hash(track, ("sha256",), audio, f"Sound track {name}")
        track_status[name] = "valid"

    cue_rows = rows(cues, "cues", "items")
    sfx_enabled = enabled.get("sfx", bool(cue_rows))

    if isinstance(sfx_enabled, str):
        sfx_enabled = sfx_enabled.strip().lower() in {
            "true", "yes", "1", "on", "enabled"
        }

    if sfx_enabled and not cue_rows:
        raise RuntimeError("SFX is enabled but no cues exist")

    if not sfx_enabled and cue_rows:
        raise RuntimeError("SFX is disabled but cue rows remain")

    for index, cue in enumerate(cue_rows, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        try:
            key = (
                int(cue["part"]),
                int(cue.get("scene", cue.get("local_scene"))),
            )
            timestamp = float(
                cue.get(
                    "time",
                    cue.get("time_seconds", cue.get("start_seconds", -1)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid SFX cue mapping/time: {index}"
            ) from exc

        if key not in narration_mapping:
            raise RuntimeError(f"SFX cue references unknown scene: {key}")

        if not math.isfinite(timestamp) or timestamp < 0:
            raise RuntimeError(f"Invalid SFX cue timestamp: {index}")

        if timestamp >= narration_mapping[key]["duration"]:
            raise RuntimeError(f"SFX cue is outside narration duration: {key}")

        audio = safe_file(
            pick(cue, "file", "path", "audio", "output", "audio_path"),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )

        require_media(audio, f"SFX cue {index}", audio=True)
        check_optional_hash(cue, ("sha256",), audio, f"SFX cue {index}")

    expected_count = parts * scenes

    for field in ("expected_scenes", "narration_scenes_checked"):
        if field in timing:
            try:
                count = int(timing[field])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid SFX timing field: {field}") from exc

            if count != expected_count:
                raise RuntimeError(f"SFX timing count mismatch: {field}")

    sfx_report = timing.get("sfx")

    if isinstance(sfx_report, dict) and sfx_report.get("status") not in (
        None, "valid", "completed"
    ):
        raise RuntimeError("SFX timing report says SFX validation failed")

    return {
        "status": "valid",
        "tracks": track_status,
        "sfx_enabled": bool(sfx_enabled),
        "sfx_cue_count": len(cue_rows),
        "timing_report": "output/sound_design/sfx_timing_validation.json",
    }


def validate_render(mode, parts, scenes, photo_report, narration_mapping):
    data = read_json(RENDER_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")

    if str(data.get("format", "")).lower() != mode:
        raise RuntimeError("Render manifest format differs from input")

    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    mapping = ordered_mapping(
        data.get("completed_scenes"),
        parts,
        scenes,
        "Rendered",
    )

    if int(data.get("completed_count", -1)) != len(mapping):
        raise RuntimeError("Render completed_count mismatch")

    cross_checks = []
    scene_duration_sum = 0.0

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Rendered scene incomplete: {key}")

        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Captions not confirmed for scene {key}")

        photo = photo_report["mapping"].get(key)
        narration = narration_mapping.get(key)

        if photo is None or narration is None:
            raise RuntimeError(f"Missing upstream mapping for scene {key}")

        # burn_captions may replace manifest rows and omit source_video and
        # narration. Read these original values from the render sidecar.
        pre_caption_value = pick(
            row, "source_rendered_video", "pre_caption_video"
        )

        if not pre_caption_value:
            raise RuntimeError(f"Missing pre-caption video path for scene {key}")

        pre_caption = safe_file(
            pre_caption_value,
            f"Pre-caption video {key}",
            MIN_VIDEO_BYTES,
        )

        sidecar_path = pre_caption.with_suffix(".render.json")
        sidecar = read_json(sidecar_path)

        if not isinstance(sidecar, dict) or sidecar.get("status") != "completed":
            raise RuntimeError(
                f"Render sidecar missing/incomplete for scene {key}"
            )

        sidecar_video = pick(sidecar, "video")

        if sidecar_video:
            declared_video = safe_file(
                sidecar_video,
                f"Sidecar video {key}",
                MIN_VIDEO_BYTES,
            )

            if declared_video.resolve() != pre_caption.resolve():
                raise RuntimeError(f"Sidecar video path mismatch for scene {key}")

        source_value = pick(sidecar, "source")
        narration_value = pick(sidecar, "narration")

        if not source_value or not narration_value:
            raise RuntimeError(
                f"Sidecar lacks source/narration for scene {key}"
            )

        source = safe_file(
            source_value,
            f"Original source scene {key}",
            MIN_VIDEO_BYTES,
        )

        audio = safe_file(
            narration_value,
            f"Narration source {key}",
            MIN_AUDIO_BYTES,
        )

        if relative(source) != photo["relative_path"]:
            raise RuntimeError(f"Photo-motion source mismatch for scene {key}")

        if sha256_file(source) != photo["sha256"]:
            raise RuntimeError(
                f"Photo-motion source checksum mismatch for scene {key}"
            )

        if relative(audio) != narration["relative_path"]:
            raise RuntimeError(f"Narration mapping mismatch for scene {key}")

        if sha256_file(audio) != narration["sha256"]:
            raise RuntimeError(
                f"Narration checksum mismatch for scene {key}"
            )

        video = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Captioned scene {key}",
            MIN_VIDEO_BYTES,
        )

        info = require_media(
            video,
            f"Captioned scene {key}",
            video=True,
            audio=True,
            resolution=RESOLUTION,
        )

        declared_duration = row.get("duration")

        if declared_duration is not None:
            try:
                declared_duration = float(declared_duration)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid duration for scene {key}"
                ) from exc

            if not math.isfinite(declared_duration) or declared_duration <= 0:
                raise RuntimeError(f"Invalid duration for scene {key}")

            if abs(info["duration"] - declared_duration) > 1.0:
                raise RuntimeError(f"Duration mismatch for scene {key}")

        check_optional_hash(
            row,
            ("sha256", "video_sha256"),
            video,
            f"Captioned scene {key}",
        )

        scene_duration_sum += info["duration"]

        cross_checks.append({
            "part": key[0],
            "scene": key[1],
            "source_video": photo["relative_path"],
            "narration": narration["relative_path"],
            "pre_caption_video": relative(pre_caption),
            "captioned_video": relative(video),
            "source_mapping_verified": True,
            "narration_mapping_verified": True,
            "sidecar": relative(sidecar_path),
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
                raise RuntimeError(
                    f"Invalid part number at row {index}"
                ) from exc

            if row_part != index:
                raise RuntimeError(f"Part order mismatch at row {index}")

            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part {index} is not completed")

            value = pick(row, "path", "video", "output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)

        info = require_media(
            path,
            f"Part video {index}",
            video=True,
            audio=True,
            resolution=RESOLUTION,
        )

        part_files.append({
            "part": index,
            "path": relative(path),
            "duration": info["duration"],
            "sha256": sha256_file(path),
        })

    final_path = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias_path = safe_file(data.get("alias"), "Format-specific video", MIN_VIDEO_BYTES)

    final_info = require_media(
        final_path,
        "Final video",
        video=True,
        audio=True,
        resolution=RESOLUTION,
    )

    alias_info = require_media(
        alias_path,
        "Format-specific video",
        video=True,
        audio=True,
        resolution=RESOLUTION,
    )

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    if alias_path.name != expected_alias:
        raise RuntimeError(
            f"Expected alias {expected_alias}, got {alias_path.name}"
        )

    if final_path.resolve() == alias_path.resolve():
        raise RuntimeError("Final video and alias must be distinct files")

    if abs(final_info["duration"] - alias_info["duration"]) > 1.0:
        raise RuntimeError("Final video and alias durations differ")

    part_duration_sum = sum(item["duration"] for item in part_files)

    if abs(final_info["duration"] - part_duration_sum) > max(2.0, parts):
        raise RuntimeError(
            "Final duration differs significantly from part durations"
        )

    if abs(final_info["duration"] - scene_duration_sum) > max(
        2.0, len(mapping)
    ):
        raise RuntimeError(
            "Final duration differs significantly from scene durations"
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
        "resolution": list(RESOLUTION),
        "cross_manifest_scene_checks": cross_checks,
        "cross_manifest_scene_checks_count": len(cross_checks),
    }


def validate_thumbnail(mode):
    path = safe_file(
        "output/thumbnail.jpg",
        "Thumbnail",
        MIN_IMAGE_BYTES,
    )

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format

    except Exception as exc:
        raise RuntimeError(f"Thumbnail is invalid: {exc}") from exc

    if dimensions != RESOLUTION:
        raise RuntimeError(
            f"Thumbnail must be 1280x720; got {dimensions}"
        )

    if image_format != "JPEG":
        raise RuntimeError(f"Expected JPEG thumbnail, got {image_format}")

    manifest_path = OUT / "thumbnail" / "thumbnail_manifest.json"
    manifest = read_json(manifest_path, optional=True)

    if isinstance(manifest, dict):
        if manifest.get("status") != "completed":
            raise RuntimeError("Thumbnail manifest is not completed")

        if manifest.get("format") != mode:
            raise RuntimeError("Thumbnail manifest format mismatch")

        declared = pick(manifest, "thumbnail", "path", "output")

        if declared:
            declared_path = safe_file(
                declared,
                "Thumbnail manifest path",
                MIN_IMAGE_BYTES,
            )

            if declared_path.resolve() != path.resolve():
                raise RuntimeError("Thumbnail manifest path mismatch")

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
    path = safe_file(
        "output/youtube_metadata.txt",
        "YouTube metadata",
        10,
    )

    content = path.read_text(encoding="utf-8-sig").strip()

    # Accept common heading styles, including TITLE, Title:, and [TITLE].
    normalized = content.upper()
    required_sections = ("TITLE", "DESCRIPTION", "TAGS")
    missing = [
        label for label in required_sections
        if label not in normalized
    ]

    if missing:
        raise RuntimeError(
            "YouTube metadata is missing sections: "
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

    photo_report = validate_photo_motion(parts, scenes)
    narration_report = validate_narration(parts, scenes)
    narration_mapping = narration_report["mapping"]

    checks = {
        "photo_motion": {
            key: value
            for key, value in photo_report.items()
            if key != "mapping"
        },
        "narration": {
            key: value
            for key, value in narration_report.items()
            if key != "mapping"
        },
        "captions": validate_captions(
            parts,
            scenes,
            narration_report["total_seconds"],
        ),
        "sound_design": validate_sound_design(
            parts,
            scenes,
            narration_mapping,
        ),
        "render": validate_render(
            mode,
            parts,
            scenes,
            photo_report,
            narration_mapping,
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
            "width": RESOLUTION[0],
            "height": RESOLUTION[1],
        },
        "thumbnail_resolution": {
            "width": RESOLUTION[0],
            "height": RESOLUTION[1],
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
            "render_sidecar_used": True,
        },
        "limitations": [
            "Validation cannot assess perceptual video quality.",
            "SFX validation does not prove every effect is audible.",
            "Caption timestamps do not prove word-level speech alignment.",
            "Manifest checks do not perform OCR on caption pixels.",
            "Landscape resolution is checked; vertical-safe framing is not.",
            "YouTube policy compliance is not guaranteed.",
        ],
    }

    write_report(report)
    log("FINAL VALIDATION: PASSED")
    log(
        "CROSS-MANIFEST SCENES:",
        checks["render"]["cross_manifest_scene_checks_count"],
    )
    log("FINAL VIDEO:", checks["render"]["final_video"])
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
