
#!/usr/bin/env python3
"""Validate final Katha Lok AI photo-motion video deliverables."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.input_config import (
        load_and_validate,
        get_format,
        get_parts,
        get_scenes,
    )
except ImportError:
    from input_config import (
        load_and_validate,
        get_format,
        get_parts,
        get_scenes,
    )


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
VIDEO_ROOT = OUTPUT / "videos"
PART_ROOT = OUTPUT / "parts"
RENDER_MANIFEST = OUTPUT / "scene_renders" / "render_manifest.json"
CAPTION_FILE = OUTPUT / "captions" / "captions.json"
SRT_FILE = OUTPUT / "captions" / "final_captions.srt"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUTPUT / "thumbnail" / "thumbnail_manifest.json"
METADATA_FILE = OUTPUT / "youtube_metadata.txt"

ERRORS: list[str] = []
WARNINGS: list[str] = []


def fail(message: str) -> None:
    ERRORS.append(message)
    print(f"ERROR: {message}")


def warn(message: str) -> None:
    WARNINGS.append(message)
    print(f"WARNING: {message}")


def ok(message: str) -> None:
    print(f"OK: {message}")


def read_json(path: Path, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            fail(f"Required JSON file is missing: {relative(path)}")
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read JSON {relative(path)}: {exc}")
        return None


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def safe_path(value: Any, label: str) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: missing or invalid path")
        return None

    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    try:
        resolved = candidate.resolve()
        resolved.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        fail(f"{label}: path escapes the repository")
        return None

    return resolved


def check_file(value: Any, label: str, min_bytes: int = 1) -> Path | None:
    path = safe_path(value, label)
    if path is None:
        return None

    if not path.is_file():
        fail(f"{label}: file does not exist: {relative(path)}")
        return None

    try:
        size = path.stat().st_size
    except OSError as exc:
        fail(f"{label}: cannot inspect file: {exc}")
        return None

    if size < min_bytes:
        fail(f"{label}: file is unexpectedly small ({size} bytes)")
        return None

    return path


def media_info(path: Path) -> dict[str, Any] | None:
    """Read video/audio streams using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size:"
                "stream=codec_type,width,height,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"ffprobe failed for {relative(path)}: {exc}")
        return None

    if result.returncode != 0:
        fail(
            f"Invalid media file {relative(path)}: "
            f"{result.stderr.strip()[:300]}"
        )
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        fail(f"ffprobe returned invalid JSON for {relative(path)}")
        return None

    streams = data.get("streams", [])
    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]
    audio_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "audio"
    ]

    if not video_streams:
        fail(f"No video stream: {relative(path)}")
        return None

    if not audio_streams:
        fail(f"No audio stream: {relative(path)}")

    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 0:
        fail(f"Invalid media duration: {relative(path)}")

    video = video_streams[0]
    try:
        width = int(video.get("width", 0))
        height = int(video.get("height", 0))
    except (TypeError, ValueError):
        width, height = 0, 0

    if width <= 0 or height <= 0:
        fail(f"Invalid video dimensions: {relative(path)}")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": bool(audio_streams),
        "format": fmt,
    }


def expected_dimensions(mode: str) -> tuple[int, int]:
    if mode == "short":
        return 720, 1280
    return 1280, 720


def check_dimensions(
    path: Path,
    info: dict[str, Any],
    mode: str,
    label: str,
) -> None:
    expected_width, expected_height = expected_dimensions(mode)
    actual_width = info["width"]
    actual_height = info["height"]

    if (actual_width, actual_height) != (
        expected_width,
        expected_height,
    ):
        fail(
            f"{label}: expected {expected_width}x{expected_height}, "
            f"found {actual_width}x{actual_height}"
        )


def parse_srt_timestamp(value: str) -> float | None:
    match = re.fullmatch(
        r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
        value.strip(),
    )
    if not match:
        return None

    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def validate_srt(path: Path, total_duration: float) -> int:
    try:
        content = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        fail(f"Cannot read captions SRT: {exc}")
        return 0

    if not content:
        fail("Caption SRT is empty")
        return 0

    blocks = re.split(r"\n\s*\n", content)
    previous_end = 0.0
    valid_cues = 0

    for block_number, block in enumerate(blocks, start=1):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            fail(f"SRT block {block_number} is incomplete")
            continue

        if not lines[0].strip().isdigit():
            fail(f"SRT block {block_number} has an invalid cue number")
            continue

        timing = re.fullmatch(
            r"\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
            r"(\d{2,}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?",
            lines[1].strip(),
        )
        if not timing:
            fail(f"SRT block {block_number} has invalid timestamps")
            continue

        start = parse_srt_timestamp(timing.group(1))
        end = parse_srt_timestamp(timing.group(2))

        if start is None or end is None or end <= start:
            fail(f"SRT block {block_number} has invalid timing")
            continue

        if start + 0.05 < previous_end:
            fail(f"SRT cues overlap or are out of order at block {block_number}")

        if start > total_duration + 0.5 or end > total_duration + 1.0:
            fail(f"SRT block {block_number} exceeds final video duration")

        caption_text = " ".join(lines[2:]).strip()
        if not caption_text:
            fail(f"SRT block {block_number} has no caption text")

        previous_end = end
        valid_cues += 1

    if valid_cues:
        ok(f"SRT contains {valid_cues} valid caption cues")

    return valid_cues


def validate_model_selection() -> None:
    path = OUTPUT / "config" / "selected_model.json"
    data = read_json(path)

    if not isinstance(data, dict):
        return

    if data.get("status") != "selected":
        fail("Selected-model manifest status is not 'selected'")

    if not data.get("model"):
        fail("Selected-model manifest has no model name")
    else:
        ok("Selected-model manifest is present")


def validate_visual_inputs(expected_scenes: int) -> None:
    jobs_path = OUTPUT / "visuals" / "visual_jobs.json"
    manifest_path = OUTPUT / "visuals" / "image_manifest.json"

    jobs_data = read_json(jobs_path)
    image_data = read_json(manifest_path)

    if not isinstance(jobs_data, dict):
        fail("visual_jobs.json must contain a JSON object")
        return

    if not isinstance(image_data, dict):
        fail("image_manifest.json must contain a JSON object")
        return

    jobs = jobs_data.get("scenes", jobs_data.get("jobs", []))
    images = image_data.get("scenes", [])

    if not isinstance(jobs, list):
        fail("visual_jobs.json has no valid scene list")
        return

    if not isinstance(images, list):
        fail("image_manifest.json has no valid scene list")
        return

    if len(jobs) != expected_scenes:
        fail(
            f"Visual job count mismatch: expected {expected_scenes}, "
            f"found {len(jobs)}"
        )

    if len(images) != expected_scenes:
        fail(
            f"Image manifest count mismatch: expected {expected_scenes}, "
            f"found {len(images)}"
        )

    seen: set[int] = set()

    for row in images:
        if not isinstance(row, dict):
            fail("Image manifest contains an invalid scene entry")
            continue

        scene_id = row.get("global_scene", row.get("scene"))
        if not isinstance(scene_id, int):
            fail("Image manifest contains a scene without a numeric ID")
            continue

        if scene_id in seen:
            fail(f"Duplicate image manifest scene ID: {scene_id}")
        seen.add(scene_id)

        image_value = (
            row.get("image_path")
            or row.get("image")
            or row.get("path")
        )
        image_path = check_file(image_value, f"Scene {scene_id} image")

        if image_path and image_path.suffix.lower() not in {
            ".png", ".jpg", ".jpeg", ".webp"
        }:
            fail(f"Scene {scene_id}: unsupported image format")

    if seen and seen != set(range(1, expected_scenes + 1)):
        fail("Image manifest scene IDs are not sequential from 1")

    if len(images) == expected_scenes:
        ok(f"Image manifest covers {expected_scenes} scenes")


def validate_photo_motion(expected_scenes: int, mode: str) -> None:
    path = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
    data = read_json(path)

    if not isinstance(data, list):
        fail("Photo-motion manifest must be a JSON list")
        return

    if len(data) != expected_scenes:
        fail(
            f"Photo-motion scene count mismatch: expected {expected_scenes}, "
            f"found {len(data)}"
        )

    seen: set[int] = set()

    for row in data:
        if not isinstance(row, dict):
            fail("Photo-motion manifest contains an invalid entry")
            continue

        scene_id = row.get("global_scene", row.get("scene"))
        if not isinstance(scene_id, int):
            fail("Photo-motion entry has no numeric scene ID")
            continue

        if scene_id in seen:
            fail(f"Duplicate photo-motion scene ID: {scene_id}")
        seen.add(scene_id)

        if row.get("status") not in (None, "completed"):
            fail(f"Photo-motion scene {scene_id} is not completed")

        video_value = row.get("video") or row.get("output")
        video_path = check_file(
            video_value, f"Photo-motion scene {scene_id} video", 1024
        )
        if video_path:
            info = media_info(video_path)
            if info:
                check_dimensions(
                    video_path, info, mode,
                    f"Photo-motion scene {scene_id}",
                )

    if seen and seen != set(range(1, expected_scenes + 1)):
        fail("Photo-motion scene IDs are not sequential from 1")


def validate_render_manifest(
    mode: str,
    expected_parts: int,
    expected_scenes: int,
) -> tuple[dict[str, Any] | None, float]:
    manifest = read_json(RENDER_MANIFEST)

    if not isinstance(manifest, dict):
        fail("Render manifest must be a JSON object")
        return None, 0.0

    if manifest.get("status") != "completed":
        fail("Render manifest status is not 'completed'")

    if manifest.get("format") != mode:
        fail(
            f"Render manifest format mismatch: expected {mode}, "
            f"found {manifest.get('format')!r}"
        )

    if manifest.get("captions_burned") is not True:
        fail("Final render manifest does not confirm burned-in captions")

    rows = manifest.get("completed_scenes", [])
    if not isinstance(rows, list):
        fail("Render manifest completed_scenes is not a list")
        rows = []

    if len(rows) != expected_scenes:
        fail(
            f"Rendered scene count mismatch: expected {expected_scenes}, "
            f"found {len(rows)}"
        )

    seen: set[int] = set()

    for row in rows:
        if not isinstance(row, dict):
            fail("Render manifest contains an invalid scene entry")
            continue

        scene_id = row.get("global_scene")
        if not isinstance(scene_id, int):
            fail("Rendered scene has no numeric global_scene")
            continue

        if scene_id in seen:
            fail(f"Duplicate rendered scene ID: {scene_id}")
        seen.add(scene_id)

        if row.get("status") not in (None, "completed"):
            fail(f"Rendered scene {scene_id} is not completed")

        if row.get("captions_burned") is not True:
            fail(f"Rendered scene {scene_id} is not marked captioned")

        for key in ("source_rendered_video", "video"):
            value = row.get(key)
            scene_path = check_file(
                value, f"Rendered scene {scene_id} {key}", 1024
            )
            if scene_path:
                info = media_info(scene_path)
                if info:
                    check_dimensions(
                        scene_path, info, mode,
                        f"Rendered scene {scene_id} {key}",
                    )

        audio_value = row.get("audio")
        if audio_value:
            check_file(audio_value, f"Rendered scene {scene_id} audio")

    if seen and seen != set(range(1, expected_scenes + 1)):
        fail("Rendered scene IDs are not sequential from 1")

    parts = manifest.get("parts", [])
    if not isinstance(parts, list):
        fail("Render manifest parts field is not a list")
        parts = []

    if len(parts) != expected_parts:
        fail(
            f"Part manifest count mismatch: expected {expected_parts}, "
            f"found {len(parts)}"
        )

    for index, item in enumerate(parts, start=1):
        value = item.get("path") if isinstance(item, dict) else item
        part_path = check_file(value, f"Part {index:02d} video", 1024)
        if part_path:
            info = media_info(part_path)
            if info:
                check_dimensions(
                    part_path, info, mode, f"Part {index:02d}"
                )

    final_value = manifest.get("final_video")
    final_path = check_file(final_value, "Manifest final_video", 1024)

    final_duration = 0.0
    if final_path:
        info = media_info(final_path)
        if info:
            check_dimensions(final_path, info, mode, "Final video")
            final_duration = info["duration"]

    alias_value = manifest.get("alias")
    alias_path = check_file(alias_value, "Manifest format alias", 1024)

    if final_path and alias_path and final_path.resolve() == alias_path.resolve():
        warn("Final video and alias point to the same file")

    return manifest, final_duration


def validate_captions(expected_scenes: int, total_duration: float) -> None:
    data = read_json(CAPTION_FILE)

    if not isinstance(data, dict):
        fail("Caption manifest must be a JSON object")
        return

    if data.get("status") != "completed":
        fail("Caption manifest status is not 'completed'")

    rows = data.get("captions", [])
    if not isinstance(rows, list):
        fail("Caption manifest captions field is not a list")
        rows = []

    if len(rows) != expected_scenes:
        fail(
            f"Caption scene count mismatch: expected {expected_scenes}, "
            f"found {len(rows)}"
        )

    seen: set[tuple[int, int]] = set()

    for row in rows:
        if not isinstance(row, dict):
            fail("Caption manifest contains an invalid entry")
            continue

        part = row.get("part")
        scene = row.get("scene", row.get("local_scene"))
        if not isinstance(part, int) or not isinstance(scene, int):
            fail("Caption entry is missing numeric part/scene IDs")
            continue

        key = (part, scene)
        if key in seen:
            fail(f"Duplicate caption entry for part {part}, scene {scene}")
        seen.add(key)

        if not str(row.get("text", "")).strip():
            fail(f"Caption text is empty for part {part}, scene {scene}")

    if SRT_FILE.is_file():
        validate_srt(SRT_FILE, total_duration)
    else:
        fail("Final SRT file is missing")


def validate_metadata_and_thumbnail(mode: str) -> None:
    if not METADATA_FILE.is_file():
        fail("YouTube metadata file is missing")
    else:
        try:
            metadata = METADATA_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            fail(f"Cannot read metadata file: {exc}")
            metadata = ""

        for field in ("Title:", "Description:", "Tags:"):
            if not re.search(
                rf"(?im)^\s*{re.escape(field)}\s*\S",
                metadata,
            ):
                fail(f"Metadata is missing a non-empty {field} field")

        if metadata:
            ok("YouTube metadata file exists")

    if not THUMBNAIL.is_file():
        fail("Thumbnail image is missing")
    else:
        try:
            from PIL import Image

            with Image.open(THUMBNAIL) as image:
                image.verify()

            with Image.open(THUMBNAIL) as image:
                width, height = image.size

            if width <= 0 or height <= 0:
                fail("Thumbnail has invalid dimensions")
            elif mode == "short" and height <= width:
                fail("Short thumbnail should be portrait")
            elif mode == "full" and width <= height:
                fail("Full-video thumbnail should be landscape")
            else:
                ok(f"Thumbnail is valid ({width}x{height})")
        except Exception as exc:
            fail(f"Thumbnail is unreadable: {exc}")

    thumbnail_manifest = read_json(THUMBNAIL_MANIFEST)
    if isinstance(thumbnail_manifest, dict):
        if thumbnail_manifest.get("status") != "completed":
            fail("Thumbnail manifest status is not 'completed'")

        recorded_path = thumbnail_manifest.get("path")
        if recorded_path:
            resolved = safe_path(recorded_path, "Thumbnail manifest path")
            if resolved and resolved.resolve() != THUMBNAIL.resolve():
                warn("Thumbnail manifest points to a different image path")


def main() -> int:
    print("=" * 64)
    print("KATHA LOK AI — FINAL OUTPUT VALIDATION")
    print("=" * 64)

    try:
        config = load_and_validate()
        mode = str(get_format(config)).strip().lower()
        parts = int(get_parts(config))
        scenes_per_part = int(get_scenes(config))
    except Exception as exc:
        fail(f"Input configuration could not be loaded: {exc}")
        return 1

    if mode not in {"short", "full"}:
        fail(f"Unsupported output format: {mode!r}")
        return 1

    if parts < 1 or scenes_per_part < 1:
        fail("PARTS and SCENES must both be positive integers")
        return 1

    expected_scenes = parts * scenes_per_part

    print(f"Format: {mode}")
    print(f"Configured parts: {parts}")
    print(f"Scenes per part: {scenes_per_part}")
    print(f"Expected scenes: {expected_scenes}")
    print("-" * 64)

    validate_model_selection()
    validate_visual_inputs(expected_scenes)
    validate_photo_motion(expected_scenes, mode)

    render_manifest, final_duration = validate_render_manifest(
        mode, parts, expected_scenes
    )

    if render_manifest is not None:
        validate_captions(expected_scenes, final_duration)

    validate_metadata_and_thumbnail(mode)

    final_video = VIDEO_ROOT / "katha_lok_ai_final.mp4"
    alias_name = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    expected_alias = VIDEO_ROOT / alias_name
    opposite_alias = VIDEO_ROOT / (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    if not final_video.is_file():
        fail(f"Expected final video is missing: {relative(final_video)}")

    if not expected_alias.is_file():
        fail(f"Expected format alias is missing: {relative(expected_alias)}")

    if opposite_alias.exists():
        fail(
            f"Wrong-format alias also exists: {relative(opposite_alias)}. "
            "Only the selected format should be delivered."
        )

    if final_video.is_file() and expected_alias.is_file():
        try:
            if final_video.stat().st_size != expected_alias.stat().st_size:
                warn("Final video and format alias have different file sizes")
        except OSError:
            pass

    for part_number in range(1, parts + 1):
        part_path = PART_ROOT / f"part_{part_number:02d}.mp4"
        if not part_path.is_file():
            fail(f"Missing expected part video: {relative(part_path)}")

    print("-" * 64)
    print(f"Warnings: {len(WARNINGS)}")
    print(f"Errors: {len(ERRORS)}")

    if ERRORS:
        print("FINAL VALIDATION: FAILED")
        for number, message in enumerate(ERRORS, start=1):
            print(f"{number}. {message}")
        return 1

    print("FINAL VALIDATION: PASSED")
    print("Selected format and required deliverables passed validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
