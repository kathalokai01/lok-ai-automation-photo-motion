
#!/usr/bin/env python3
"""Validate final photo-motion deliverables without regenerating assets."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUTPUT = ROOT / "output"
ERRORS: list[str] = []
WARNINGS: list[str] = []


def log(message: str) -> None:
    print(message, flush=True)


def error(message: str) -> None:
    ERRORS.append(message)
    log(f"ERROR: {message}")


def warning(message: str) -> None:
    WARNINGS.append(message)
    log(f"WARNING: {message}")


def success(message: str) -> None:
    log(f"OK: {message}")


def read_json(path: Path, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            error(f"Missing file: {path.relative_to(ROOT)}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"Invalid JSON {path.relative_to(ROOT)}: {exc}")
        return None


def safe_file(value: Any, label: str, minimum: int = 1) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        error(f"{label}: missing file path")
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()
        path.relative_to(ROOT.resolve())
    except (OSError, ValueError):
        error(f"{label}: path is outside the repository")
        return None

    if not path.is_file():
        error(f"{label}: file not found ({path})")
        return None

    try:
        size = path.stat().st_size
    except OSError as exc:
        error(f"{label}: cannot read file size: {exc}")
        return None

    if size < minimum:
        error(f"{label}: file is too small ({size} bytes)")
        return None

    return path


def probe_media(path: Path) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "format=duration,size:"
                "stream=codec_type,width,height,codec_name",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        error(f"ffprobe failed for {path.name}: {exc}")
        return None

    if result.returncode != 0:
        error(f"Unreadable media {path.name}: {result.stderr.strip()[:250]}")
        return None

    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        duration = float(data.get("format", {}).get("duration", 0))
        size = int(data.get("format", {}).get("size", 0))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        error(f"Invalid ffprobe output for {path.name}: {exc}")
        return None

    if not videos:
        error(f"No video stream in {path.name}")
        return None

    video = videos[0]
    width = int(video.get("width", 0))
    height = int(video.get("height", 0))

    if width <= 0 or height <= 0:
        error(f"Invalid video dimensions in {path.name}")
    if duration <= 0:
        error(f"Invalid duration in {path.name}")

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "size": size,
        "has_audio": bool(audios),
        "video_codec": video.get("codec_name"),
    }


def check_video(
    value: Any,
    label: str,
    mode: str,
    require_audio: bool = True,
    minimum: int = 1024,
) -> tuple[Path | None, dict[str, Any] | None]:
    path = safe_file(value, label, minimum)
    if path is None:
        return None, None

    info = probe_media(path)
    if info is None:
        return path, None

    expected = (720, 1280) if mode == "short" else (1280, 720)
    actual = (info["width"], info["height"])

    if actual != expected:
        error(
            f"{label}: expected {expected[0]}x{expected[1]}, "
            f"found {actual[0]}x{actual[1]}"
        )

    if require_audio and not info["has_audio"]:
        error(f"{label}: audio stream is missing")

    return path, info


def int_value(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if row.get(key) is not None:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                return None
    return None


def scene_rows(data: Any) -> list[dict[str, Any]] | None:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = None
        for key in ("scenes", "jobs", "visual_jobs", "completed_scenes"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        if rows is None:
            return None
    else:
        return None

    if not all(isinstance(row, dict) for row in rows):
        return None

    return rows


def validate_image_manifest(total: int) -> None:
    jobs_path = OUTPUT / "visuals" / "visual_jobs.json"
    jobs = read_json(jobs_path)
    rows = scene_rows(jobs)

    if rows is None:
        error("Visual jobs manifest has no valid scene list")
    elif len(rows) != total:
        error(f"Visual jobs: expected {total} rows, found {len(rows)}")
    else:
        success(f"Visual jobs: {len(rows)} rows")

    manifest_path = OUTPUT / "visuals" / "image_manifest.json"
    manifest = read_json(manifest_path)
    image_rows = scene_rows(manifest)

    if image_rows is None:
        error("Image manifest has no valid scene list")
        return

    expected_ids = set(range(1, total + 1))
    seen = set()

    for row in image_rows:
        scene = int_value(row, "global_scene", "scene")
        if scene is None:
            error("Image manifest contains a row without a numeric scene ID")
            continue
        if scene in seen:
            error(f"Duplicate image scene ID: {scene}")
        seen.add(scene)

        if row.get("status") not in (None, "completed", "success"):
            error(f"Image scene {scene} is incomplete")

        value = next(
            (
                row.get(key)
                for key in ("image_path", "image", "path")
                if row.get(key)
            ),
            None,
        )
        path = safe_file(value, f"Image scene {scene}", 100)

        if path and path.suffix.lower() not in {
            ".png", ".jpg", ".jpeg", ".webp"
        }:
            error(f"Image scene {scene}: unsupported image format")

    if len(image_rows) != total or seen != expected_ids:
        error("Image manifest does not map exactly to all expected scenes")
    else:
        success(f"Image manifest: {total} unique scenes")


def validate_audio(total: int, parts: int, scenes_per_part: int) -> None:
    path = OUTPUT / "narration" / "audio_jobs.json"
    data = read_json(path)

    if not isinstance(data, dict):
        error("Audio jobs manifest must be a JSON object")
        return

    if data.get("status") != "completed":
        error("Audio jobs manifest status is not completed")

    rows = data.get("jobs", data.get("audio_jobs", []))
    if not isinstance(rows, list) or len(rows) != total:
        error(f"Audio jobs: expected {total} rows")
        return

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    seen = set()

    for row in rows:
        if not isinstance(row, dict):
            error("Audio manifest contains an invalid row")
            continue

        part = int_value(row, "part")
        scene = int_value(row, "scene", "local_scene")

        if part is None or scene is None:
            error("Audio job is missing part/scene mapping")
            continue

        key = (part, scene)
        if key in seen:
            error(f"Duplicate audio job: Part {part}, Scene {scene}")
        seen.add(key)

        if key not in expected:
            error(f"Unexpected audio job mapping: {key}")

        if row.get("status") != "completed":
            error(f"Audio job incomplete: Part {part}, Scene {scene}")

        audio = safe_file(
            row.get("output") or row.get("audio_path") or row.get("path"),
            f"Narration Part {part}, Scene {scene}",
            100,
        )

        if audio:
            try:
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-select_streams", "a:0",
                        "-show_entries", "stream=codec_type",
                        "-of", "json", str(audio),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                parsed = json.loads(probe.stdout or "{}")
                if probe.returncode or not parsed.get("streams"):
                    error(f"No audio stream in {audio.name}")
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                error(f"Could not verify audio {audio.name}: {exc}")

    if seen != expected:
        error("Audio job mapping is incomplete or incorrect")
    else:
        success(f"Audio jobs: {total} unique part/scene mappings")


def validate_captions(total: int, parts: int, scenes_per_part: int) -> None:
    path = OUTPUT / "captions" / "captions.json"
    data = read_json(path)

    if not isinstance(data, dict):
        error("captions.json must be a JSON object")
        return

    if data.get("status") != "completed":
        error("Caption preparation status is not completed")

    rows = data.get("captions", [])
    if not isinstance(rows, list) or len(rows) != total:
        error(f"Caption count must equal {total}")
        return

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    seen = set()

    for row in rows:
        if not isinstance(row, dict):
            error("Invalid caption row")
            continue

        part = int_value(row, "part")
        scene = int_value(row, "scene", "local_scene")

        if part is None or scene is None:
            error("Caption row is missing part/scene IDs")
            continue

        key = (part, scene)
        if key in seen:
            error(f"Duplicate caption mapping: {key}")
        seen.add(key)

        if key not in expected:
            error(f"Unexpected caption mapping: {key}")

        if not str(row.get("text", "")).strip():
            error(f"Empty caption at Part {part}, Scene {scene}")

    if seen != expected:
        error("Caption mapping does not match the requested scene plan")
    else:
        success(f"Captions: {total} unique scene mappings")


def parse_srt_time(value: str) -> float | None:
    match = re.fullmatch(
        r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
        value.strip(),
    )
    if not match:
        return None

    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def validate_srt(total_duration: float) -> None:
    path = OUTPUT / "captions" / "final_captions.srt"

    if not path.is_file():
        error("Final SRT file is missing")
        return

    try:
        content = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        error(f"Cannot read final SRT: {exc}")
        return

    if not content:
        error("Final SRT file is empty")
        return

    blocks = re.split(r"\n\s*\n", content)
    previous_end = 0.0
    valid = 0

    for index, block in enumerate(blocks, 1):
        lines = block.strip().splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            error(f"SRT cue {index} is malformed")
            continue

        match = re.fullmatch(
            r"\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
            r"(\d{2,}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?",
            lines[1].strip(),
        )
        if not match:
            error(f"SRT cue {index} has invalid timestamps")
            continue

        start = parse_srt_time(match.group(1))
        end = parse_srt_time(match.group(2))

        if start is None or end is None or end <= start:
            error(f"SRT cue {index} has invalid duration")
            continue
        if start + 0.05 < previous_end:
            error(f"SRT cues overlap or are out of order at cue {index}")
        if end > total_duration + 1.0:
            error(f"SRT cue {index} exceeds final video duration")
        if not " ".join(lines[2:]).strip():
            error(f"SRT cue {index} has no text")
        if len(lines[2:]) > 2:
            warning(f"SRT cue {index} has more than two text lines")

        previous_end = end
        valid += 1

    if valid:
        success(f"SRT: {valid} readable cues")


def validate_photo_motion(total: int, mode: str) -> None:
    path = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
    data = read_json(path)
    rows = scene_rows(data)

    if rows is None:
        error("Photo-motion manifest has no valid scene list")
        return

    expected = set(range(1, total + 1))
    seen = set()

    for row in rows:
        scene = int_value(row, "global_scene", "scene")
        if scene is None:
            error("Photo-motion row is missing a numeric scene ID")
            continue
        if scene in seen:
            error(f"Duplicate photo-motion scene ID: {scene}")
        seen.add(scene)

        if row.get("status") not in (None, "completed", "success"):
            error(f"Photo-motion scene {scene} is incomplete")

        value = next(
            (
                row.get(key)
                for key in ("video", "output", "video_path", "output_path")
                if row.get(key)
            ),
            None,
        )
        check_video(
            value,
            f"Photo-motion scene {scene}",
            mode,
            require_audio=False,
        )

    if len(rows) != total or seen != expected:
        error("Photo-motion manifest does not map exactly to all scenes")
    else:
        success(f"Photo-motion clips: {total} scenes")


def validate_render(mode: str, parts: int, total: int) -> float:
    path = OUTPUT / "scene_renders" / "render_manifest.json"
    data = read_json(path)

    if not isinstance(data, dict):
        error("Render manifest must be a JSON object")
        return 0.0

    if data.get("status") != "completed":
        error("Render manifest status is not completed")
    if data.get("format") != mode:
        error(f"Render manifest format must be {mode}")

    rows = data.get("completed_scenes", [])
    if not isinstance(rows, list) or len(rows) != total:
        error(f"Rendered scene count should be {total}")
        rows = []

    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Invalid rendered-scene row")
            continue

        scene = int_value(row, "global_scene", "scene")
        if scene is None:
            error("Rendered scene is missing its numeric ID")
            continue
        if scene in seen:
            error(f"Duplicate rendered scene ID: {scene}")
        seen.add(scene)

        if row.get("status") not in (None, "completed", "success"):
            error(f"Rendered scene {scene} is incomplete")

        video_value = row.get("video") or row.get("output")
        video_path, video_info = check_video(
            video_value, f"Rendered scene {scene}", mode
        )

        if video_info and not video_info["has_audio"]:
            error(f"Rendered scene {scene} is missing audio")

        if row.get("captions_burned") is False:
            error(f"Rendered scene {scene} explicitly reports no captions")

    if seen != set(range(1, total + 1)):
        error("Rendered scene IDs do not cover all expected scenes")

    part_rows = data.get("parts", [])
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        error(f"Expected {parts} part videos")
        part_rows = []

    for index, item in enumerate(part_rows, 1):
        if isinstance(item, dict):
            value = item.get("path") or item.get("video") or item.get("output")
        else:
            value = item
        check_video(value, f"Final part {index:02d}", mode)

    final_value = data.get("final_video")
    if not final_value:
        final_value = str(OUTPUT / "videos" / "katha_lok_ai_final.mp4")

    final_path, final_info = check_video(
        final_value, "Final merged video", mode
    )

    alias_value = data.get("alias")
    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if not alias_value:
        alias_value = str(OUTPUT / "videos" / expected_alias)

    alias_path, alias_info = check_video(
        alias_value, "Final format alias", mode
    )

    if alias_path and alias_path.name != expected_alias:
        error(f"Expected final alias filename: {expected_alias}")

    if final_info and alias_info:
        if abs(final_info["duration"] - alias_info["duration"]) > 0.5:
            error("Final video and alias durations differ")

    opposite_name = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite = OUTPUT / "videos" / opposite_name
    if opposite.exists():
        error(f"Unexpected opposite-format output: {opposite}")

    return final_info["duration"] if final_info else 0.0


def validate_thumbnail_and_metadata(mode: str) -> None:
    thumbnail = OUTPUT / "thumbnail.jpg"

    if not thumbnail.is_file():
        error("Thumbnail output/thumbnail.jpg is missing")
    else:
        try:
            from PIL import Image

            with Image.open(thumbnail) as image:
                image.verify()
            with Image.open(thumbnail) as image:
                width, height = image.size

            if width < 320 or height < 320:
                error("Thumbnail dimensions are too small")
            if mode == "short" and height <= width:
                error("Short thumbnail should be portrait")
            if mode == "full" and width <= height:
                error("Full thumbnail should be landscape")

            success(f"Thumbnail valid: {width}x{height}")
        except Exception as exc:
            error(f"Thumbnail is not a valid image: {exc}")

    metadata_path = OUTPUT / "youtube_metadata.txt"
    if not metadata_path.is_file():
        error("YouTube metadata file is missing")
        return

    try:
        content = metadata_path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        error(f"Cannot read YouTube metadata: {exc}")
        return

    for field in ("Title", "Description", "Tags"):
        match = re.search(
            rf"(?ims)^\s*{field}\s*:\s*\n?(.*?)(?=^\s*"
            rf"(?:Title|Description|Tags)\s*:|\Z)",
            content,
        )
        if not match or not match.group(1).strip():
            error(f"Metadata field is empty: {field}")

    if content:
        success("Metadata contains Title, Description and Tags")


def main() -> int:
    try:
        config = load_and_validate(ROOT / "Input" / "topic.txt")
        mode = get_format(config)
        parts = int(get_parts(config))
        scenes_per_part = int(get_scenes(config))
    except Exception as exc:
        error(f"Invalid input configuration: {exc}")
        return 1

    total = parts * scenes_per_part
    log("=" * 60)
    log(
        f"FORMAT={mode} | PARTS={parts} | "
        f"SCENES/PART={scenes_per_part} | TOTAL={total}"
    )
    log("Final-output validation only; no media regeneration.")
    log("=" * 60)

    validate_image_manifest(total)
    validate_audio(total, parts, scenes_per_part)
    validate_photo_motion(total, mode)
    validate_captions(total, parts, scenes_per_part)

    duration = validate_render(mode, parts, total)
    if duration > 0:
        validate_srt(duration)

    validate_thumbnail_and_metadata(mode)

    log("=" * 60)
    log(f"ERRORS={len(ERRORS)} | WARNINGS={len(WARNINGS)}")

    if ERRORS:
        log("FINAL VALIDATION: FAILED")
        for item in ERRORS:
            log(f"- {item}")
        return 1

    log("FINAL VALIDATION: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
