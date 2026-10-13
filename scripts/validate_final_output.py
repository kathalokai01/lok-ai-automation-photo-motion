#!/usr/bin/env python3
"""Validate Katha Lok AI photo-motion deliverables without regenerating assets."""

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


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def read_json(path: Path, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            error(f"Missing file: {rel(path)}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"Invalid JSON {rel(path)}: {exc}")
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
        error(f"{label}: path is outside repository")
        return None

    if not path.is_file():
        error(f"{label}: file not found ({rel(path)})")
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


def media_info(path: Path) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "format=duration,size:"
                "stream=codec_type,width,height,duration",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        error(f"ffprobe failed for {rel(path)}: {exc}")
        return None

    if result.returncode:
        error(f"Unreadable media {rel(path)}: {result.stderr.strip()[:200]}")
        return None

    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        duration = float(data.get("format", {}).get("duration", 0))
        width = int(videos[0].get("width", 0)) if videos else 0
        height = int(videos[0].get("height", 0)) if videos else 0
    except (ValueError, TypeError, IndexError, json.JSONDecodeError) as exc:
        error(f"Could not interpret media information for {rel(path)}: {exc}")
        return None

    if not videos:
        error(f"No video stream: {rel(path)}")
        return None
    if width <= 0 or height <= 0:
        error(f"Invalid video dimensions: {rel(path)}")
    if duration <= 0:
        error(f"Invalid video duration: {rel(path)}")

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "has_audio": bool(audios),
    }


def check_video(
    value: Any,
    label: str,
    mode: str,
    *,
    require_audio: bool = True,
    minimum: int = 1024,
) -> tuple[Path | None, dict[str, Any] | None]:
    path = safe_file(value, label, minimum)
    if path is None:
        return None, None

    info = media_info(path)
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
        error(f"{label}: narration/audio stream is missing")

    return path, info


def check_scene_manifest(
    path: Path,
    expected_count: int,
    label: str,
    path_keys: tuple[str, ...],
    mode: str | None = None,
    require_audio: bool = True,
) -> None:
    data = read_json(path)
    if not isinstance(data, dict):
        error(f"{label}: manifest must be a JSON object")
        return

    rows = data.get("scenes", data.get("jobs", data.get("completed_scenes", [])))
    if not isinstance(rows, list):
        error(f"{label}: scene rows must be a list")
        return

    if len(rows) != expected_count:
        error(f"{label}: expected {expected_count} rows, found {len(rows)}")

    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            error(f"{label}: invalid scene row")
            continue

        scene = row.get("global_scene", row.get("scene"))
        if not isinstance(scene, int):
            error(f"{label}: row has no numeric scene ID")
            continue
        if scene in seen:
            error(f"{label}: duplicate scene ID {scene}")
        seen.add(scene)

        status = row.get("status")
        if status not in (None, "completed", "success"):
            error(f"{label}: scene {scene} status is {status!r}")

        value = next((row.get(key) for key in path_keys if row.get(key)), None)
        if mode:
            check_video(
                value,
                f"{label} scene {scene}",
                mode,
                require_audio=require_audio,
            )
        else:
            file_path = safe_file(value, f"{label} scene {scene}", 100)
            if file_path and file_path.suffix.lower() not in {
                ".png", ".jpg", ".jpeg", ".webp"
            }:
                error(f"{label}: unsupported image type for scene {scene}")

    if seen != set(range(1, expected_count + 1)):
        error(f"{label}: scene IDs must be sequential from 1 to {expected_count}")
    else:
        success(f"{label}: {len(seen)} scenes accounted for")


def validate_images(total_scenes: int) -> None:
    jobs_path = OUTPUT / "visuals" / "visual_jobs.json"
    jobs = read_json(jobs_path)
    if isinstance(jobs, dict):
        rows = jobs.get("scenes", jobs.get("jobs", []))
        if not isinstance(rows, list) or len(rows) != total_scenes:
            error(f"Visual jobs: expected {total_scenes} scenes")
    elif jobs is not None:
        error("Visual jobs: manifest must be a JSON object")

    check_scene_manifest(
        OUTPUT / "visuals" / "image_manifest.json",
        total_scenes,
        "Image manifest",
        ("image_path", "image", "path"),
    )


def validate_audio(total_scenes: int) -> None:
    path = OUTPUT / "narration" / "audio_jobs.json"
    data = read_json(path)
    if not isinstance(data, dict):
        error("Audio jobs manifest must be a JSON object")
        return

    if data.get("status") != "completed":
        error("Audio jobs manifest status is not completed")

    rows = data.get("jobs", data.get("audio_jobs", []))
    if not isinstance(rows, list) or len(rows) != total_scenes:
        error(f"Audio jobs: expected {total_scenes} rows")
        return

    seen: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Audio jobs: invalid row")
            continue

        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError):
            error("Audio job is missing part/scene IDs")
            continue

        key = (part, scene)
        if key in seen:
            error(f"Duplicate audio job: part {part}, scene {scene}")
        seen.add(key)

        if row.get("status") != "completed":
            error(f"Audio job incomplete: part {part}, scene {scene}")

        audio = safe_file(
            row.get("output") or row.get("audio_path") or row.get("path"),
            f"Narration part {part}, scene {scene}",
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
                    error(f"Narration file contains no audio stream: {rel(audio)}")
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                error(f"Could not verify narration audio {rel(audio)}: {exc}")

    if len(seen) != total_scenes:
        error("Audio job scene mapping is incomplete")


def validate_captions(total_scenes: int) -> None:
    path = OUTPUT / "captions" / "captions.json"
    data = read_json(path)
    if not isinstance(data, dict):
        error("captions.json must be a JSON object")
        return

    if data.get("status") != "completed":
        error("Caption preparation status is not completed")

    rows = data.get("captions", [])
    if not isinstance(rows, list) or len(rows) != total_scenes:
        error(f"Caption count must equal {total_scenes}")
        return

    seen: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Invalid caption row")
            continue
        try:
            key = (
                int(row["part"]),
                int(row.get("scene", row.get("local_scene"))),
            )
        except (KeyError, TypeError, ValueError):
            error("Caption row has missing part/scene IDs")
            continue

        if key in seen:
            error(f"Duplicate caption entry: {key}")
        seen.add(key)

        if not str(row.get("text", "")).strip():
            error(f"Empty caption text for part {key[0]}, scene {key[1]}")


def parse_srt_time(value: str) -> float | None:
    match = re.fullmatch(
        r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
        value.strip(),
    )
    if not match:
        return None
    h, m, s, ms = map(int, match.groups())
    return h * 3600 + m * 60 + s + ms / 1000


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
            error(f"SRT cue {index} has no caption text")
        if len(lines[2:]) > 2:
            warning(f"SRT cue {index} has more than two text lines")

        previous_end = end
        valid += 1

    if valid:
        success(f"SRT: {valid} readable cues")


def validate_render(mode: str, parts_count: int, total_scenes: int) -> float:
    path = OUTPUT / "scene_renders" / "render_manifest.json"
    data = read_json(path)
    if not isinstance(data, dict):
        error("Render manifest must be a JSON object")
        return 0.0

    if data.get("status") != "completed":
        error("Render manifest status is not completed")
    if data.get("format") != mode:
        error(f"Render format should be {mode}")
    if data.get("captions_burned") is not True:
        error("Render manifest does not confirm burned-in captions")
    if data.get("total_scenes") != total_scenes:
        error(f"Render manifest total_scenes should be {total_scenes}")

    rows = data.get("completed_scenes", [])
    if not isinstance(rows, list) or len(rows) != total_scenes:
        error(f"Rendered scene count should be {total_scenes}")
        rows = []

    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Invalid rendered-scene row")
            continue

        scene = row.get("global_scene", row.get("scene"))
        if not isinstance(scene, int):
            error("Rendered scene has no numeric ID")
            continue
        if scene in seen:
            error(f"Duplicate rendered scene ID: {scene}")
        seen.add(scene)

        if row.get("status") != "completed":
            error(f"Rendered scene {scene} is incomplete")
        if row.get("captions_burned") is not True:
            error(f"Rendered scene {scene} has no caption confirmation")
        if row.get("rendered_with_narration") is not True:
            error(f"Rendered scene {scene} is not marked as narration-mixed")

        check_video(
            row.get("source_rendered_video"),
            f"Pre-caption scene {scene}",
            mode,
        )
        check_video(row.get("video"), f"Final scene {scene}", mode)

    if seen != set(range(1, total_scenes + 1)):
        error("Rendered scene IDs are not sequential")

    part_rows = data.get("parts", [])
    if not isinstance(part_rows, list) or len(part_rows) != parts_count:
        error(f"Expected {parts_count} final part videos")
        part_rows = []

    for index, item in enumerate(part_rows, 1):
        value = item.get("path") if isinstance(item, dict) else item
        check_video(value, f"Final part {index:02d}", mode)

    final_path, final_info = check_video(
        data.get("final_video"), "Final merged video", mode
    )
    alias_path, alias_info = check_video(
        data.get("alias"), "Final format alias", mode
    )

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias_path and alias_path.name != expected_alias:
        error(f"Expected final alias filename: {expected_alias}")

    if final_info and alias_info:
        if abs(final_info["duration"] - alias_info["duration"]) > 0.5:
            error("Final video and alias durations differ")

    if final_path and alias_path and final_path.resolve() == alias_path.resolve():
        warning("Final video and alias point to the same file")

    opposite_name = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite = OUTPUT / "videos" / opposite_name
    if opposite.exists():
        error(f"Unexpected opposite-format output: {rel(opposite)}")

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

    thumb_manifest = read_json(
        OUTPUT / "thumbnail" / "thumbnail_manifest.json",
        required=False,
    )
    if isinstance(thumb_manifest, dict):
        if thumb_manifest.get("status") not in (None, "completed"):
            error("Thumbnail manifest status is not completed")

    metadata_path = OUTPUT / "youtube_metadata.txt"
    if not metadata_path.is_file():
        error("YouTube metadata file is missing")
        return

    try:
        content = metadata_path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        error(f"Cannot read YouTube metadata: {exc}")
        return

    # The value may be on the next line, as in:
    # Title:
    # Story title
    for field in ("Title", "Description", "Tags"):
        match = re.search(
            rf"(?ims)^\s*{field}\s*:\s*\n?(.*?)(?=^\s*"
            rf"(?:Title|Description|Tags)\s*:|\Z)",
            content,
        )
        if not match or not match.group(1).strip():
            error(f"Metadata field is empty: {field}")

    if content:
        success("YouTube metadata contains Title, Description and Tags")


def main() -> int:
    try:
        config = load_and_validate(ROOT / "Input" / "topic.txt")
        mode = get_format(config)
        parts_count = get_parts(config)
        scenes_per_part = get_scenes(config)
    except Exception as exc:
        error(f"Invalid input configuration: {exc}")
        return 1

    total_scenes = parts_count * scenes_per_part

    log("=" * 56)
    log("KATHA LOK AI FINAL OUTPUT VALIDATION")
    log(
        f"FORMAT={mode} | PARTS={parts_count} | "
        f"SCENES/PART={scenes_per_part} | TOTAL={total_scenes}"
    )
    log("=" * 56)

    if not (ROOT / "Input" / "topic.txt").is_file():
        error("Input/topic.txt is missing")

    if not (OUTPUT / "config" / "selected_model.json").is_file():
        warning("Selected model manifest is missing")

    validate_images(total_scenes)

    validate_audio(total_scenes)

    # Photo-motion clips can be silent: narration is mixed during video render.
    check_scene_manifest(
        OUTPUT / "photo_motion" / "photo_motion_manifest.json",
        total_scenes,
        "Photo-motion manifest",
        ("video", "output", "video_path", "output_path"),
        mode,
        require_audio=False,
    )

    validate_captions(total_scenes)

    duration = validate_render(mode, parts_count, total_scenes)
    if duration > 0:
        validate_srt(duration)

    validate_thumbnail_and_metadata(mode)

    log("=" * 56)
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