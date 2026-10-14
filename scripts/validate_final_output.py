
#!/usr/bin/env python3
"""Validate final photo-motion video deliverables without regenerating assets."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUTPUT = ROOT / "output"
WARNINGS: list[str] = []
ERRORS: list[str] = []
MIN_MEDIA_BYTES = 1000


def log(*items):
    print(*items, flush=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing file path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path.relative_to(ROOT)}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is too small: {path.relative_to(ROOT)}")

    return path


def find_first(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def rows_from(data, keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def probe_video(path):
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
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path.name}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )

    if video is None or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video stream or duration: {path.name}")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width < 1 or height < 1:
        raise RuntimeError(f"Invalid video dimensions: {path.name}")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": audio is not None,
    }


def validate_video(path, label, resolution=None, require_audio=True):
    info = probe_video(path)

    if require_audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream is missing")

    if resolution and (info["width"], info["height"]) != resolution:
        raise RuntimeError(
            f"{label}: expected {resolution[0]}x{resolution[1]}, "
            f"got {info['width']}x{info['height']}"
        )

    log(
        "OK VIDEO:",
        label,
        f"{info['width']}x{info['height']}",
        f"{info['duration']:.2f}s",
        f"audio={info['has_audio']}",
    )
    return info


def validate_optional_visual_manifest(path, label):
    """Check explicitly named image files without requiring every job to contain one."""
    if not path.is_file():
        WARNINGS.append(f"{label} not found: {path.relative_to(ROOT)}")
        return

    try:
        data = read_json(path)
    except RuntimeError as exc:
        ERRORS.append(str(exc))
        return

    rows = rows_from(
        data,
        ("jobs", "visual_jobs", "images", "completed_images", "items"),
    )
    if not rows:
        WARNINGS.append(
            f"{label} has no recognized rows; image checks were skipped"
        )
        return

    checked = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue

        status = str(row.get("status", "")).lower()
        image_value = find_first(
            row,
            ("image_path", "image", "image_file", "output_image", "path"),
        )

        # Prompt-only jobs are valid before image generation and should not
        # be treated as missing image files by this final-deliverable checker.
        if not image_value:
            if status in ("completed", "success", "done"):
                WARNINGS.append(
                    f"{label} row {index} has no explicit image path"
                )
            continue

        try:
            image_path = safe_file(image_value, f"{label} row {index}", 100)
            if image_path.suffix.lower() not in (
                ".png", ".jpg", ".jpeg", ".webp", ".bmp"
            ):
                WARNINGS.append(
                    f"{label} row {index} uses an unusual image extension"
                )
            checked += 1
        except RuntimeError as exc:
            ERRORS.append(str(exc))

    log(f"OK MANIFEST: {label}; explicit image paths checked: {checked}")


def validate_audio_manifest(path, expected_count):
    if not path.is_file():
        ERRORS.append("Narration manifest is missing")
        return

    try:
        data = read_json(path)
    except RuntimeError as exc:
        ERRORS.append(str(exc))
        return

    jobs = rows_from(data, ("jobs", "audio_jobs"))
    if not jobs:
        ERRORS.append("Narration manifest has no jobs")
        return

    completed = {}
    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            ERRORS.append(f"Invalid narration job row {index}")
            continue

        if str(row.get("status", "")).lower() != "completed":
            ERRORS.append(f"Narration job {index} is not completed")
            continue

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError):
            ERRORS.append(f"Narration job {index} has invalid part/scene")
            continue

        key = (part, scene)
        if key in completed:
            ERRORS.append(f"Duplicate narration mapping: {key}")
            continue

        audio_value = find_first(
            row, ("output", "audio_path", "path", "file", "audio")
        )
        if audio_value:
            try:
                audio_path = safe_file(
                    audio_value, f"Narration part {part} scene {scene}", 100
                )
                if audio_path.suffix.lower() not in (
                    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"
                ):
                    WARNINGS.append(
                        f"Unusual narration extension: {audio_path.name}"
                    )
            except RuntimeError as exc:
                ERRORS.append(str(exc))

        try:
            duration = float(row.get("duration", 0))
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
        except (TypeError, ValueError):
            ERRORS.append(f"Narration job {key} has invalid duration")

        completed[key] = row

    if len(completed) != expected_count:
        ERRORS.append(
            f"Narration mapping count mismatch: expected {expected_count}, "
            f"found {len(completed)}"
        )

    log(f"NARRATION JOBS: {len(completed)} completed mappings")


def validate_photo_motion(path, expected_count, resolution):
    if not path.is_file():
        ERRORS.append("Photo-motion manifest is missing")
        return

    try:
        data = read_json(path)
    except RuntimeError as exc:
        ERRORS.append(str(exc))
        return

    rows = rows_from(data, ("scenes", "videos", "completed_scenes", "items"))
    if not rows:
        ERRORS.append("Photo-motion manifest has no scene rows")
        return

    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            ERRORS.append(f"Invalid photo-motion row {index}")
            continue

        try:
            scene = int(row.get("global_scene", row.get("scene", index)))
            part = int(row["part"])
            local = int(row["local_scene"])
        except (KeyError, TypeError, ValueError):
            ERRORS.append(f"Invalid photo-motion scene mapping at row {index}")
            continue

        if scene in seen:
            ERRORS.append(f"Duplicate photo-motion scene number: {scene}")
        seen.add(scene)

        value = find_first(row, ("video", "output", "video_path"))
        if not value:
            ERRORS.append(f"Photo-motion scene {scene} has no video path")
            continue

        try:
            video = safe_file(value, f"Photo-motion scene {scene}", MIN_MEDIA_BYTES)
            validate_video(
                video, f"photo-motion scene {scene}",
                resolution=resolution, require_audio=False,
            )
        except RuntimeError as exc:
            ERRORS.append(str(exc))

    if len(rows) != expected_count:
        ERRORS.append(
            f"Photo-motion scene count mismatch: expected {expected_count}, "
            f"found {len(rows)}"
        )

    if seen and seen != set(range(1, expected_count + 1)):
        ERRORS.append("Photo-motion global scene numbering is not continuous")


def validate_caption_manifest(path, expected_count):
    if not path.is_file():
        ERRORS.append("Caption manifest is missing")
        return

    try:
        data = read_json(path)
    except RuntimeError as exc:
        ERRORS.append(str(exc))
        return

    if not isinstance(data, dict) or data.get("status") != "completed":
        ERRORS.append("Caption manifest status is not completed")
        return

    rows = data.get("captions")
    if not isinstance(rows, list):
        ERRORS.append("Caption manifest has no captions list")
        return

    keys = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            ERRORS.append(f"Invalid caption row {index}")
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            ERRORS.append(f"Invalid caption mapping at row {index}")
            continue

        if key in keys:
            ERRORS.append(f"Duplicate caption mapping: {key}")
        keys.add(key)

        if not str(row.get("text", "")).strip():
            ERRORS.append(f"Empty caption for {key}")

    if len(keys) != expected_count:
        ERRORS.append(
            f"Caption count mismatch: expected {expected_count}, found {len(keys)}"
        )

    log(f"CAPTIONS: {len(keys)} scene mappings")


def validate_render_manifest(path, expected_count, mode, resolution, parts):
    if not path.is_file():
        ERRORS.append("Render manifest is missing")
        return

    try:
        data = read_json(path)
    except RuntimeError as exc:
        ERRORS.append(str(exc))
        return

    if not isinstance(data, dict):
        ERRORS.append("Render manifest must be a JSON object")
        return

    if data.get("status") != "completed":
        ERRORS.append("Render manifest status is not completed")
    if data.get("format") != mode:
        ERRORS.append("Render manifest FORMAT does not match Input/topic.txt")
    if data.get("captions_burned") is not True:
        ERRORS.append("Render manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    if not isinstance(rows, list) or not rows:
        ERRORS.append("Render manifest has no completed_scenes")
        return

    seen = set()
    part_groups = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            ERRORS.append(f"Invalid render manifest row {index}")
            continue

        try:
            global_scene = int(row.get("global_scene", row.get("scene", index)))
            part = int(row["part"])
            local = int(row["local_scene"])
        except (KeyError, TypeError, ValueError):
            ERRORS.append(f"Invalid render mapping at row {index}")
            continue

        key = (part, local)
        if key in seen:
            ERRORS.append(f"Duplicate render scene mapping: {key}")
        seen.add(key)
        part_groups.setdefault(part, []).append(local)

        if row.get("status") != "completed":
            ERRORS.append(f"Render scene {global_scene} is not completed")
        if row.get("captions_burned") is not True:
            ERRORS.append(f"Render scene {global_scene} lacks caption confirmation")

        video_value = find_first(row, ("video", "output"))
        if not video_value:
            ERRORS.append(f"Render scene {global_scene} has no video path")
            continue

        try:
            video = safe_file(
                video_value, f"Captioned scene {global_scene}", MIN_MEDIA_BYTES
            )
            validate_video(
                video, f"captioned scene {global_scene}",
                resolution=resolution, require_audio=True,
            )
        except RuntimeError as exc:
            ERRORS.append(str(exc))

        source_value = row.get("source_rendered_video")
        if source_value:
            try:
                source = safe_file(
                    source_value, f"Source scene {global_scene}", MIN_MEDIA_BYTES
                )
                if video_value and source.resolve() == (
                    (ROOT / video_value).resolve()
                    if not Path(video_value).is_absolute()
                    else Path(video_value).resolve()
                ):
                    ERRORS.append(
                        f"Scene {global_scene}: source and captioned video paths are identical"
                    )
            except RuntimeError as exc:
                ERRORS.append(str(exc))

    if len(rows) != expected_count:
        ERRORS.append(
            f"Rendered scene count mismatch: expected {expected_count}, found {len(rows)}"
        )

    if sorted(row.get("global_scene", row.get("scene", i))
              for i, row in enumerate(rows, 1)) != list(range(1, expected_count + 1)):
        ERRORS.append("Render global scene numbering is not continuous")

    if set(part_groups) != set(range(1, parts + 1)):
        ERRORS.append("Rendered part numbers do not match configured PARTS")

    for part, local_scenes in part_groups.items():
        if sorted(local_scenes) != list(range(1, len(local_scenes) + 1)):
            ERRORS.append(f"Part {part} has missing or non-continuous scene numbers")

    part_paths = data.get("parts")
    if not isinstance(part_paths, list) or len(part_paths) != parts:
        ERRORS.append(
            f"Render manifest must list exactly {parts} completed part videos"
        )
    else:
        for index, value in enumerate(part_paths, 1):
            try:
                part_path = safe_file(value, f"Final part {index}", MIN_MEDIA_BYTES)
                validate_video(
                    part_path, f"final part {index}",
                    resolution=resolution, require_audio=True,
                )
            except RuntimeError as exc:
                ERRORS.append(str(exc))

    for key in ("final_video", "alias"):
        value = data.get(key)
        if not value:
            ERRORS.append(f"Render manifest missing {key}")
            continue
        try:
            final_path = safe_file(value, key, MIN_MEDIA_BYTES)
            validate_video(
                final_path, key, resolution=resolution, require_audio=True
            )
        except RuntimeError as exc:
            ERRORS.append(str(exc))


def validate_srt(path, expected_count):
    if not path.is_file():
        ERRORS.append("Final SRT file is missing")
        return

    try:
        content = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        ERRORS.append(f"Cannot read SRT: {exc}")
        return

    blocks = [block for block in content.split("\n\n") if block.strip()]
    if not blocks:
        ERRORS.append("SRT contains no caption cues")
        return

    valid = 0
    for block in blocks:
        lines = block.splitlines()
        if (
            len(lines) >= 3
            and lines[0].strip().isdigit()
            and " --> " in lines[1]
            and any(line.strip() for line in lines[2:])
        ):
            valid += 1

    if valid != len(blocks):
        ERRORS.append("SRT contains malformed caption blocks")
    if valid < expected_count:
        WARNINGS.append(
            f"SRT has {valid} cues for {expected_count} scenes; "
            "multiple cues per scene are allowed"
        )

    log(f"SRT CUES: {valid}")


def validate_thumbnail_and_metadata():
    thumbnail = OUTPUT / "thumbnail.jpg"
    metadata = OUTPUT / "youtube_metadata.txt"

    if not thumbnail.is_file() or thumbnail.stat().st_size < MIN_MEDIA_BYTES:
        ERRORS.append("Thumbnail output/thumbnail.jpg is missing or too small")

    if not metadata.is_file() or metadata.stat().st_size < 30:
        ERRORS.append("Metadata output/youtube_metadata.txt is missing or too small")
        return

    text = metadata.read_text(encoding="utf-8-sig", errors="replace")
    lowered = text.lower()
    for label in ("title:", "description:", "tags:"):
        if label not in lowered:
            ERRORS.append(f"Metadata is missing the {label[:-1]} field")

    title_line = next(
        (line.partition(":")[2].strip()
         for line in text.splitlines()
         if line.lower().startswith("title:")),
        "",
    )
    if not title_line:
        ERRORS.append("Metadata title is empty")

    log("THUMBNAIL AND METADATA: checked")


def main():
    log("=" * 58)
    log("KATHA LOK AI — FINAL OUTPUT VALIDATION")
    log("=" * 58)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        ERRORS.append("ffmpeg is not installed")
    if not ffprobe:
        ERRORS.append("ffprobe is not installed")

    try:
        config = load_and_validate(ROOT / "Input" / "topic.txt")
        mode = get_format(config)
        parts = int(get_parts(config))
        scenes_per_part = int(get_scenes(config))
        expected_count = parts * scenes_per_part
        resolution = (1280, 720) if mode == "full" else (720, 1280)

        log(f"FORMAT={mode}")
        log(f"PARTS={parts}")
        log(f"SCENES_PER_PART={scenes_per_part}")
        log(f"EXPECTED_SCENES={expected_count}")

        validate_optional_visual_manifest(
            OUTPUT / "visuals" / "visual_jobs.json",
            "visual_jobs.json",
        )
        validate_optional_visual_manifest(
            OUTPUT / "visuals" / "image_manifest.json",
            "image_manifest.json",
        )

        validate_audio_manifest(
            OUTPUT / "narration" / "audio_jobs.json",
            expected_count,
        )
        validate_photo_motion(
            OUTPUT / "photo_motion" / "photo_motion_manifest.json",
            expected_count,
            resolution,
        )
        validate_caption_manifest(
            OUTPUT / "captions" / "captions.json",
            expected_count,
        )
        validate_render_manifest(
            OUTPUT / "scene_renders" / "render_manifest.json",
            expected_count,
            mode,
            resolution,
            parts,
        )
        validate_srt(
            OUTPUT / "captions" / "final_captions.srt",
            expected_count,
        )
        validate_thumbnail_and_metadata()

    except Exception as exc:
        ERRORS.append(f"Configuration/validation setup failed: {exc}")

    log("-" * 58)
    for warning in WARNINGS:
        log("WARNING:", warning)
    for error in ERRORS:
        log("ERROR:", error)

    if ERRORS:
        log(f"VALIDATION FAILED: {len(ERRORS)} error(s), {len(WARNINGS)} warning(s)")
        sys.exit(1)

    log(f"VALIDATION PASSED: {len(WARNINGS)} warning(s)")
    log("No provider calls were made by this validator.")


if __name__ == "__main__":
    main()
