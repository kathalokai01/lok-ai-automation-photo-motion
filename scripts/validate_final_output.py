
#!/usr/bin/env python3
"""Schema-aware, cross-manifest final-output validator for Katha Lok AI."""

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
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
CAPTION_MANIFEST = OUT / "captions" / "captions.json"
SOUND_MANIFEST = OUT / "sound_design" / "sound_design_manifest.json"
SFX_MANIFEST = OUT / "sound_design" / "sfx_cues.json"
TIMING_MANIFEST = OUT / "sound_design" / "sfx_timing_validation.json"
RENDER_MANIFEST = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA_FILE = OUT / "youtube_metadata.txt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v23-schema-aware"
WIDTH, HEIGHT = 1280, 720
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_BYTES = 1000


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
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
        value = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError) as exc:
        if optional:
            return None
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Invalid JSON root: {path}")
    return value


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository root")

    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")

    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file too small: {path.name}")

    return path


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


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
            value = data.get(key)
            if isinstance(value, list):
                return value
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
            f"ffprobe failed for {path.name}: {result.stderr[-700:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media information: {path.name}") from exc

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


def require_media(path, label, video=False, audio=False, resolution=None):
    info = media_info(path)

    if video and not info["has_video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream missing")

    if resolution and info["has_video"]:
        actual = (info["width"], info["height"])
        if actual != resolution:
            raise RuntimeError(
                f"{label}: expected {resolution[0]}x{resolution[1]}, "
                f"got {actual[0]}x{actual[1]}"
            )

    return info


def check_hash(row, keys, path, label):
    if isinstance(keys, str):
        keys = (keys,)

    expected = pick(row, *keys)
    if expected is None:
        return None

    expected = expected.lower()
    if len(expected) != 64 or any(
        char not in "0123456789abcdef" for char in expected
    ):
        raise RuntimeError(f"{label}: invalid SHA-256 value")

    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(f"{label}: SHA-256 mismatch")

    return actual


def expected_keys(parts, scenes):
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def scene_mapping(data_rows, parts, scenes, label, global_fallback=False):
    expected = expected_keys(parts, scenes)

    if not isinstance(data_rows, list) or len(data_rows) != len(expected):
        count = len(data_rows) if isinstance(data_rows, list) else 0
        raise RuntimeError(
            f"{label}: expected {len(expected)} rows, found {count}"
        )

    mapping = {}

    for index, row in enumerate(data_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: row {index} is not an object")

        try:
            if global_fallback and "part" not in row:
                global_scene = int(row.get("global_scene", row.get("scene", index)))
                part = (global_scene - 1) // scenes + 1
                scene = (global_scene - 1) % scenes + 1
            else:
                part = int(row["part"])
                scene = int(row.get("local_scene", row.get("scene", 0)))
                global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{label}: invalid scene mapping at row {index}"
            ) from exc

        key = (part, scene)
        if key != expected[index - 1] or global_scene != index:
            raise RuntimeError(
                f"{label}: wrong order at row {index}; "
                f"found part={part}, scene={scene}, global={global_scene}"
            )

        if key in mapping:
            raise RuntimeError(f"{label}: duplicate scene {key}")

        mapping[key] = row

    return mapping


def validate_photo_motion(parts, scenes):
    data = read_json(PHOTO_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest is not completed")

    if str(data.get("format", "")).lower() not in ("short", "full"):
        raise RuntimeError("Photo-motion manifest has invalid format")

    mapping = scene_mapping(
        rows(data, "scenes", "videos", "items"),
        parts, scenes, "Photo motion",
        global_fallback=True,
    )

    result = {}

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene incomplete: {key}")

        path = safe_file(
            pick(row, "video", "video_path", "output", "path", "file"),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )

        info = require_media(
            path, f"Photo-motion scene {key}",
            video=True, resolution=(WIDTH, HEIGHT),
        )

        check_hash(row, "video_sha256", path, f"Photo-motion scene {key}")

        result[key] = {
            "path": path,
            "relative_path": relative(path),
            "sha256": sha256_file(path),
            "duration": info["duration"],
        }

    if int(data.get("completed_count", -1)) != len(result):
        raise RuntimeError("Photo-motion completed_count mismatch")

    return {
        "status": "valid",
        "scene_count": len(result),
        "mapping": result,
    }


def validate_narration(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
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

        path = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = require_media(path, f"Narration {key}", audio=True)

        check_hash(row, "audio_sha256", path, f"Narration {key}")

        mapping[key] = {
            "path": path,
            "relative_path": relative(path),
            "sha256": sha256_file(path),
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
        "total_seconds": round(sum(x["duration"] for x in mapping.values()), 3),
        "mapping": mapping,
    }


def validate_captions(parts, scenes):
    data = read_json(CAPTION_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    mapping = {}

    for row in rows(data, "captions", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption row")

        try:
            key = (int(row["part"]), int(row.get("scene", row.get("local_scene", 0))))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption scene mapping") from exc

        text = row.get("text", row.get("caption", row.get("caption_text", "")))
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Empty caption text: {key}")
        if key in mapping:
            raise RuntimeError(f"Duplicate caption: {key}")

        mapping[key] = text.strip()

    expected = set(expected_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            "Caption mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "nonempty_text_count": len(mapping),
    }


def validate_sound_design(parts, scenes, narration_mapping):
    sound = read_json(SOUND_MANIFEST)
    cues = read_json(SFX_MANIFEST, optional=True)
    timing = read_json(TIMING_MANIFEST, optional=True)

    if not isinstance(sound, dict) or sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")

    tracks = sound.get("tracks")
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks must be an object")

    track_results = {}

    for name, row in tracks.items():
        if row is None:
            track_results[name] = {"status": "not_generated"}
            continue

        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid sound track record: {name}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Sound track incomplete: {name}")

        path = safe_file(
            pick(row, "path", "file", "output", "audio_path"),
            f"Sound track {name}",
            MIN_AUDIO_BYTES,
        )
        info = require_media(path, f"Sound track {name}", audio=True)
        track_results[name] = {
            "status": "valid",
            "path": relative(path),
            "sha256": sha256_file(path),
            "duration": round(info["duration"], 3),
        }

    cue_count = 0

    if cues is not None:
        if not isinstance(cues, dict) or cues.get("status") != "completed":
            raise RuntimeError("SFX cue manifest is incomplete")

        cue_rows = cues.get("cues", [])
        if not isinstance(cue_rows, list):
            raise RuntimeError("SFX cues must be a list")

        for index, cue in enumerate(cue_rows, 1):
            if not isinstance(cue, dict):
                raise RuntimeError(f"Invalid SFX cue row {index}")

            try:
                key = (int(cue["part"]), int(cue["scene"]))
                timestamp = float(cue["time"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid SFX cue row {index}") from exc

            if key not in narration_mapping:
                raise RuntimeError(f"SFX cue references unknown scene: {key}")

            if not math.isfinite(timestamp) or timestamp < 0:
                raise RuntimeError(f"Invalid SFX timestamp: {key}")

            if timestamp >= narration_mapping[key]["duration"]:
                raise RuntimeError(f"SFX cue outside narration duration: {key}")

            path = safe_file(
                pick(cue, "file", "path", "audio", "output", "audio_path"),
                f"SFX cue {index}",
                MIN_AUDIO_BYTES,
            )
            require_media(path, f"SFX cue {index}", audio=True)
            cue_count += 1

        declared_count = cues.get("cue_count")
        if declared_count is not None and int(declared_count) != cue_count:
            raise RuntimeError("SFX cue_count mismatch")

    if timing is not None:
        if not isinstance(timing, dict):
            raise RuntimeError("SFX timing report must be an object")
        if timing.get("status") not in (None, "", "completed", "valid"):
            raise RuntimeError("SFX timing report indicates failure")

    return {
        "status": "valid",
        "track_count": len(track_results),
        "tracks": track_results,
        "sfx_cue_count": cue_count,
        "sfx_timing_report_present": timing is not None,
    }


def validate_render(mode, parts, scenes, photo, narration):
    data = read_json(RENDER_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Final render manifest is not completed")

    if str(data.get("format", "")).lower() != mode:
        raise RuntimeError("Render format differs from input format")

    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    mapping = scene_mapping(
        rows(data, "completed_scenes", "scenes"),
        parts, scenes, "Captioned render",
    )

    if int(data.get("completed_count", -1)) != len(mapping):
        raise RuntimeError("Render completed_count mismatch")

    scene_checks = []
    scene_duration_sum = 0.0

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Rendered scene incomplete: {key}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Caption burn not confirmed for scene {key}")

        pre_value = pick(row, "source_rendered_video", "pre_caption_video")
        pre_caption = safe_file(pre_value, f"Pre-caption video {key}", MIN_VIDEO_BYTES)
        sidecar_path = pre_caption.with_suffix(".render.json")
        sidecar = read_json(sidecar_path)

        if not isinstance(sidecar, dict) or sidecar.get("status") != "completed":
            raise RuntimeError(f"Render sidecar missing/incomplete: {key}")

        sidecar_video = pick(sidecar, "video")
        if sidecar_video:
            declared = safe_file(sidecar_video, f"Sidecar video {key}", MIN_VIDEO_BYTES)
            if declared.resolve() != pre_caption.resolve():
                raise RuntimeError(f"Sidecar video mismatch: {key}")

        source_value = pick(sidecar, "source")
        audio_value = pick(sidecar, "narration")
        source = safe_file(source_value, f"Original visual {key}", MIN_VIDEO_BYTES)
        audio = safe_file(audio_value, f"Narration source {key}", MIN_AUDIO_BYTES)

        if relative(source) != photo["mapping"][key]["relative_path"]:
            raise RuntimeError(f"Visual source mapping mismatch: {key}")
        if sha256_file(source) != photo["mapping"][key]["sha256"]:
            raise RuntimeError(f"Visual source checksum mismatch: {key}")

        if relative(audio) != narration["mapping"][key]["relative_path"]:
            raise RuntimeError(f"Narration mapping mismatch: {key}")
        if sha256_file(audio) != narration["mapping"][key]["sha256"]:
            raise RuntimeError(f"Narration checksum mismatch: {key}")

        video = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Captioned scene {key}",
            MIN_VIDEO_BYTES,
        )
        info = require_media(
            video, f"Captioned scene {key}",
            video=True, audio=True, resolution=(WIDTH, HEIGHT),
        )

        declared_duration = row.get("duration")
        if declared_duration is not None:
            try:
                declared_duration = float(declared_duration)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid scene duration: {key}") from exc
            if not math.isfinite(declared_duration) or declared_duration <= 0:
                raise RuntimeError(f"Invalid scene duration: {key}")
            if abs(info["duration"] - declared_duration) > 1.0:
                raise RuntimeError(f"Scene duration mismatch: {key}")

        check_hash(row, ("sha256", "video_sha256"), video, f"Captioned scene {key}")
        scene_duration_sum += info["duration"]

        scene_checks.append({
            "part": key[0],
            "scene": key[1],
            "visual_source": relative(source),
            "narration_source": relative(audio),
            "pre_caption_video": relative(pre_caption),
            "captioned_video": relative(video),
            "sidecar": relative(sidecar_path),
            "source_mapping_verified": True,
            "narration_mapping_verified": True,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_results = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                part_number = int(row.get("part", index))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid part number: {index}") from exc

            if part_number != index:
                raise RuntimeError(f"Part order mismatch: {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part is incomplete: {index}")

            value = pick(row, "path", "video", "output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = require_media(
            path, f"Part video {index}",
            video=True, audio=True, resolution=(WIDTH, HEIGHT),
        )
        part_results.append({
            "part": index,
            "path": relative(path),
            "duration": round(info["duration"], 3),
            "sha256": sha256_file(path),
        })

    final_path = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias_path = safe_file(data.get("alias"), "Format alias", MIN_VIDEO_BYTES)

    final_info = require_media(
        final_path, "Final video",
        video=True, audio=True, resolution=(WIDTH, HEIGHT),
    )
    alias_info = require_media(
        alias_path, "Format alias",
        video=True, audio=True, resolution=(WIDTH, HEIGHT),
    )

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias_path.name != expected_alias:
        raise RuntimeError(
            f"Expected alias {expected_alias}; found {alias_path.name}"
        )

    if final_path.resolve() == alias_path.resolve():
        raise RuntimeError("Final video and alias must be separate files")

    if abs(final_info["duration"] - alias_info["duration"]) > 1.0:
        raise RuntimeError("Final video and alias duration mismatch")

    part_total = sum(item["duration"] for item in part_results)
    if abs(final_info["duration"] - part_total) > max(2.0, parts):
        raise RuntimeError("Final duration differs from sum of part durations")

    if abs(final_info["duration"] - scene_duration_sum) > max(2.0, len(mapping)):
        raise RuntimeError("Final duration differs significantly from scene durations")

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "parts": part_results,
        "final_video": relative(final_path),
        "final_video_sha256": sha256_file(final_path),
        "alias": relative(alias_path),
        "alias_sha256": sha256_file(alias_path),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": [WIDTH, HEIGHT],
        "cross_manifest_scene_checks": scene_checks,
        "cross_manifest_scene_checks_count": len(scene_checks),
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
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc

    if dimensions != (WIDTH, HEIGHT):
        raise RuntimeError(f"Thumbnail must be 1280x720; found {dimensions}")
    if image_format != "JPEG":
        raise RuntimeError(f"Thumbnail must be JPEG; found {image_format}")

    manifest = read_json(THUMBNAIL_MANIFEST, optional=True)
    if isinstance(manifest, dict):
        if manifest.get("status") != "completed":
            raise RuntimeError("Thumbnail manifest is not completed")
        if str(manifest.get("format", "")).lower() != mode:
            raise RuntimeError("Thumbnail manifest format mismatch")

        declared = pick(manifest, "thumbnail", "path", "output")
        if declared:
            declared_path = safe_file(declared, "Thumbnail manifest path", MIN_IMAGE_BYTES)
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
    path = safe_file(METADATA_FILE.relative_to(ROOT).as_posix(), "YouTube metadata", 10)
    content = path.read_text(encoding="utf-8-sig").strip()

    sections = {}
    current = None

    for line in content.splitlines():
        normalized = line.strip().strip("[]").rstrip(":").strip().upper()
        if normalized in {"TITLE", "DESCRIPTION", "TAGS", "LANGUAGE"}:
            current = normalized
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.strip())

    missing = [
        key for key in ("TITLE", "DESCRIPTION", "TAGS")
        if not sections.get(key) or not any(sections[key])
    ]
    if missing:
        raise RuntimeError(
            "YouTube metadata has missing/empty sections: " + ", ".join(missing)
        )

    title = " ".join(sections["TITLE"]).strip()
    description = "\n".join(sections["DESCRIPTION"]).strip()
    tags = " ".join(sections["TAGS"]).strip()

    if not title or not description or not tags:
        raise RuntimeError("Metadata title, description, and tags must be nonempty")

    return {
        "status": "valid",
        "path": relative(path),
        "sha256": sha256_file(path),
        "characters": len(content),
        "title_characters": len(title),
        "description_characters": len(description),
        "tags_characters": len(tags),
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required but not installed")

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

    photo = validate_photo_motion(parts, scenes)
    narration = validate_narration(parts, scenes)

    checks = {
        "photo_motion": {
            key: value for key, value in photo.items() if key != "mapping"
        },
        "narration": {
            key: value for key, value in narration.items() if key != "mapping"
        },
        "captions": validate_captions(parts, scenes),
        "sound_design": validate_sound_design(parts, scenes, narration["mapping"]),
        "render": validate_render(mode, parts, scenes, photo, narration),
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
        "resolution": {"width": WIDTH, "height": HEIGHT},
        "validator_external_api_calls": 0,
        "checks": checks,
        "cross_manifest_validation": {
            "status": "valid",
            "scene_source_mapping_verified": True,
            "scene_narration_mapping_verified": True,
            "verified_scene_count": checks["render"]["cross_manifest_scene_checks_count"],
            "render_sidecar_used": True,
        },
        "limitations": [
            "Does not assess perceptual video quality.",
            "SFX file validation does not prove that each effect is audible in the final mix.",
            "Caption phrase timing is estimated, not word-level forced alignment.",
            "Manifest validation does not perform OCR on burned caption pixels.",
            "Landscape resolution is checked; vertical-safe framing is not.",
            "YouTube policy compliance is not guaranteed.",
        ],
    }

    write_report(report)
    log("FINAL VALIDATION: PASSED")
    log("VERIFIED SCENES:", checks["render"]["cross_manifest_scene_checks_count"])
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
