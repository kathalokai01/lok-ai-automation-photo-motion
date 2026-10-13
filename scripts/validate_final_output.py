
#!/usr/bin/env python3
"""Validate Katha Lok AI photo-motion outputs without regenerating assets."""

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
VIDEOS = OUTPUT / "videos"
PARTS = OUTPUT / "parts"
RENDER_MANIFEST = OUTPUT / "scene_renders" / "render_manifest.json"
CAPTIONS_JSON = OUTPUT / "captions" / "captions.json"
CAPTIONS_SRT = OUTPUT / "captions" / "final_captions.srt"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUTPUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUTPUT / "youtube_metadata.txt"

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
            error(f"Missing JSON file: {relative(path)}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"Cannot read {relative(path)}: {exc}")
        return None


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def resolve_path(value: Any, label: str) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        error(f"{label}: missing path")
        return None

    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    try:
        resolved = candidate.resolve()
        resolved.relative_to(ROOT.resolve())
    except (OSError, ValueError):
        error(f"{label}: path is outside repository")
        return None

    if not resolved.is_file():
        error(f"{label}: file does not exist: {relative(resolved)}")
        return None
    return resolved


def check_file(value: Any, label: str, minimum: int = 1) -> Path | None:
    path = resolve_path(value, label)
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:
        error(f"{label}: cannot inspect file: {exc}")
        return None
    if size < minimum:
        error(f"{label}: file too small ({size} bytes)")
        return None
    return path


def probe(path: Path) -> dict[str, Any] | None:
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
        error(f"ffprobe failed for {relative(path)}: {exc}")
        return None

    if result.returncode != 0:
        error(
            f"Invalid media {relative(path)}: "
            f"{result.stderr.strip()[:250]}"
        )
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        error(f"Invalid ffprobe response for {relative(path)}")
        return None

    streams = data.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    if not videos:
        error(f"No video stream: {relative(path)}")
        return None
    if not audios:
        error(f"No audio stream: {relative(path)}")

    try:
        width = int(videos[0].get("width", 0))
        height = int(videos[0].get("height", 0))
        duration = float(data.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        width, height, duration = 0, 0, 0.0

    if width <= 0 or height <= 0:
        error(f"Invalid dimensions: {relative(path)}")
    if duration <= 0:
        error(f"Invalid duration: {relative(path)}")

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "has_audio": bool(audios),
    }


def expected_size(mode: str) -> tuple[int, int]:
    return (720, 1280) if mode == "short" else (1280, 720)


def check_media(
    value: Any,
    label: str,
    mode: str,
    minimum: int = 1024,
) -> tuple[Path | None, dict[str, Any] | None]:
    path = check_file(value, label, minimum)
    if path is None:
        return None, None

    info = probe(path)
    if info is None:
        return path, None

    expected = expected_size(mode)
    actual = (info["width"], info["height"])
    if actual != expected:
        error(
            f"{label}: expected {expected[0]}x{expected[1]}, "
            f"found {actual[0]}x{actual[1]}"
        )
    if not info["has_audio"]:
        error(f"{label}: audio stream is missing")
    return path, info


def parse_timestamp(value: str) -> float | None:
    match = re.fullmatch(
        r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})",
        value.strip(),
    )
    if not match:
        return None
    h, m, s, ms = map(int, match.groups())
    return h * 3600 + m * 60 + s + ms / 1000


def validate_srt(total_duration: float) -> int:
    if not CAPTIONS_SRT.is_file():
        error("Final SRT file is missing")
        return 0

    try:
        content = CAPTIONS_SRT.read_text(
            encoding="utf-8-sig"
        ).strip()
    except OSError as exc:
        error(f"Cannot read SRT: {exc}")
        return 0

    if not content:
        error("Final SRT file is empty")
        return 0

    blocks = re.split(r"\n\s*\n", content)
    previous_end = 0.0
    count = 0

    for index, block in enumerate(blocks, 1):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            error(f"SRT cue {index} is incomplete")
            continue
        if not lines[0].strip().isdigit():
            error(f"SRT cue {index} has an invalid index")
            continue

        match = re.fullmatch(
            r"\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
            r"(\d{2,}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?",
            lines[1].strip(),
        )
        if not match:
            error(f"SRT cue {index} has invalid timestamps")
            continue

        start = parse_timestamp(match.group(1))
        end = parse_timestamp(match.group(2))
        if start is None or end is None or end <= start:
            error(f"SRT cue {index} has invalid duration")
            continue

        if start + 0.05 < previous_end:
            error(f"SRT cues overlap or are out of order at cue {index}")
        if start > total_duration + 0.5 or end > total_duration + 1.0:
            error(f"SRT cue {index} exceeds final video duration")
        if not " ".join(lines[2:]).strip():
            error(f"SRT cue {index} has no text")

        if len(lines[2:]) > 2:
            warning(f"SRT cue {index} has more than two text lines")

        previous_end = end
        count += 1

    if count:
        success(f"SRT contains {count} readable cues")
    return count


def validate_images(total_scenes: int) -> None:
    jobs = read_json(OUTPUT / "visuals" / "visual_jobs.json")
    manifest = read_json(OUTPUT / "visuals" / "image_manifest.json")

    if not isinstance(jobs, dict):
        error("visual_jobs.json must be a JSON object")
        return
    if not isinstance(manifest, dict):
        error("image_manifest.json must be a JSON object")
        return

    job_rows = jobs.get("scenes", jobs.get("jobs", []))
    image_rows = manifest.get("scenes", [])

    if not isinstance(job_rows, list) or len(job_rows) != total_scenes:
        error(f"Visual job count must equal {total_scenes}")
    if not isinstance(image_rows, list) or len(image_rows) != total_scenes:
        error(f"Image manifest count must equal {total_scenes}")
        return

    seen: set[int] = set()
    for row in image_rows:
        if not isinstance(row, dict):
            error("Invalid image manifest row")
            continue
        scene = row.get("global_scene", row.get("scene"))
        if not isinstance(scene, int):
            error("Image row has no numeric scene ID")
            continue
        if scene in seen:
            error(f"Duplicate image scene ID: {scene}")
        seen.add(scene)

        value = row.get("image_path") or row.get("image") or row.get("path")
        image_path = check_file(value, f"Scene {scene} image", 100)
        if image_path and image_path.suffix.lower() not in {
            ".png", ".jpg", ".jpeg", ".webp"
        }:
            error(f"Unsupported image format for scene {scene}")

    if seen != set(range(1, total_scenes + 1)):
        error("Image scene IDs are not sequential")
    else:
        success(f"Image manifest covers {total_scenes} scenes")


def validate_photo_motion(total_scenes: int, mode: str) -> None:
    rows = read_json(
        OUTPUT / "photo_motion" / "photo_motion_manifest.json"
    )
    if not isinstance(rows, list):
        error("Photo-motion manifest must be a list")
        return
    if len(rows) != total_scenes:
        error(
            f"Photo-motion count mismatch: expected {total_scenes}, "
            f"found {len(rows)}"
        )

    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Invalid photo-motion manifest row")
            continue
        scene = row.get("global_scene", row.get("scene"))
        if not isinstance(scene, int):
            error("Photo-motion row has no numeric scene ID")
            continue
        if scene in seen:
            error(f"Duplicate photo-motion scene ID: {scene}")
        seen.add(scene)
        if row.get("status") not in (None, "completed"):
            error(f"Photo-motion scene {scene} is not completed")
        check_media(
            row.get("video") or row.get("output"),
            f"Photo-motion scene {scene}",
            mode,
        )

    if seen != set(range(1, total_scenes + 1)):
        error("Photo-motion scene IDs are not sequential")


def validate_audio_jobs(total_scenes: int) -> None:
    data = read_json(OUTPUT / "narration" / "audio_jobs.json")
    if not isinstance(data, dict):
        error("audio_jobs.json must be a JSON object")
        return
    if data.get("status") != "completed":
        error("Audio jobs manifest status is not completed")

    rows = data.get("jobs", [])
    if not isinstance(rows, list) or len(rows) != total_scenes:
        error(f"Audio job count must equal {total_scenes}")
        return

    seen: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Invalid audio job row")
            continue
        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError):
            error("Audio job is missing part/scene numbers")
            continue

        key = (part, scene)
        if key in seen:
            error(f"Duplicate audio job: part {part}, scene {scene}")
        seen.add(key)

        if row.get("status") != "completed":
            error(f"Audio job part {part}, scene {scene} is incomplete")
        check_file(
            row.get("output"),
            f"Narration part {part}, scene {scene}",
            100,
        )

    if len(seen) != total_scenes:
        error("Audio job scene mapping is incomplete")


def validate_caption_data(total_scenes: int) -> None:
    data = read_json(CAPTIONS_JSON)
    if not isinstance(data, dict):
        error("captions.json must be a JSON object")
        return
    if data.get("status") != "completed":
        error("Caption preparation status is not completed")

    rows = data.get("captions", [])
    if not isinstance(rows, list) or len(rows) != total_scenes:
        error(f"Caption entry count must equal {total_scenes}")
        return

    seen: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Invalid caption entry")
            continue
        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError):
            error("Caption entry has missing part/scene IDs")
            continue
        key = (part, scene)
        if key in seen:
            error(f"Duplicate caption for part {part}, scene {scene}")
        seen.add(key)
        if not str(row.get("text", "")).strip():
            error(f"Empty caption for part {part}, scene {scene}")


def validate_render(mode: str, parts_count: int, total_scenes: int):
    manifest = read_json(RENDER_MANIFEST)
    if not isinstance(manifest, dict):
        error("render_manifest.json must be a JSON object")
        return 0.0

    if manifest.get("status") != "completed":
        error("Final render manifest status is not completed")
    if manifest.get("format") != mode:
        error(f"Render format mismatch: expected {mode}")
    if manifest.get("captions_burned") is not True:
        error("Manifest does not confirm burned-in captions")
    if manifest.get("total_scenes") != total_scenes:
        error(f"Manifest total_scenes must equal {total_scenes}")

    rows = manifest.get("completed_scenes", [])
    if not isinstance(rows, list) or len(rows) != total_scenes:
        error(f"Rendered scene count must equal {total_scenes}")
        rows = []

    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            error("Invalid rendered scene row")
            continue
        scene = row.get("global_scene", row.get("scene"))
        if not isinstance(scene, int):
            error("Rendered scene has no numeric ID")
            continue
        if scene in seen:
            error(f"Duplicate rendered scene ID: {scene}")
        seen.add(scene)

        if row.get("status") != "completed":
            error(f"Rendered scene {scene} is not completed")
        if row.get("captions_burned") is not True:
            error(f"Rendered scene {scene} has no caption confirmation")

        check_media(
            row.get("source_rendered_video"),
            f"Scene {scene} pre-caption render",
            mode,
        )
        check_media(
            row.get("video"),
            f"Scene {scene} final captioned render",
            mode,
        )
        if row.get("rendered_with_narration") is not True:
            error(f"Scene {scene} is not marked as narration-mixed")

    if seen != set(range(1, total_scenes + 1)):
        error("Rendered scene IDs are not sequential")

    part_rows = manifest.get("parts", [])
    if not isinstance(part_rows, list) or len(part_rows) != parts_count:
        error(f"Final part count must equal {parts_count}")
        part_rows = []

    for index, item in enumerate(part_rows, 1):
        value = item.get("path") if isinstance(item, dict) else item
        check_media(value, f"Final part {index:02d}", mode)

    final_path, final_info = check_media(
        manifest.get("final_video"), "Final merged video", mode
    )
    alias_path, alias_info = check_media(
        manifest.get("alias"), "Final format alias", mode
    )

    if final_path and alias_path:
        if final_path.resolve() == alias_path.resolve():
            warning("Final video and format alias resolve to the same path")
        if final_info and alias_info:
            if abs(final_info["duration"] - alias_info["duration"]) > 0.5:
                error("Final video and alias durations differ")

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if not alias_path or alias_path.name != expected_alias:
        error(f"Expected format alias filename: {expected_alias}")

    opposite = VIDEOS / (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if opposite.exists():
        error(f"Opposite-format video should not exist: {relative(opposite)}")

    if final_path and final_path != VIDEOS / "katha_lok_ai_final.mp4":
        warning("Final video path differs from the standard output path")

    return final_info["duration"] if final_info else 0.0


def validate_thumbnail_and_metadata(mode: str) -> None:
    if not THUMBNAIL.is_file():
        error("Thumbnail is missing")
    else:
        try:
            from PIL import Image

            with Image.open(THUMBNAIL) as image:
                image.verify()
            with Image.open(THUMBNAIL) as image:
                width, height = image.size

            if width < 320 or height < 320:
                error("Thumbnail dimensions are too small")
            if mode == "short" and height <= width:
                error("Short thumbnail should be portrait")
            if mode == "full" and width <= height:
                error("Full thumbnail should be landscape")
            if width >= 320 and height >= 320:
                success(f"Thumbnail is valid ({width}x{height})")
        except Exception as exc:
            error(f"Thumbnail is not a valid image: {exc}")

    thumb_data = read_json(THUMBNAIL_MANIFEST)
    if isinstance(thumb_data, dict):
        if thumb_data.get("status") != "completed":
            error("Thumbnail manifest status is not completed")
        if thumb_data.get("path") and thumb_data["path"] != "output/thumbnail.jpg":
            warning("Thumbnail manifest path is not the standard path")

    if not METADATA.is_file():
        error("YouTube metadata file is missing")
        return

    try:
        content = METADATA.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        error(f"Cannot read YouTube metadata: {exc}")
        return

    for field in ("Title:", "Description:", "Tags:"):
        if not re.search(
            rf"(?im)^\s*{re.escape(field)}\s*\S",
            content,
        ):
            error(f"Metadata is missing a non-empty {field} field")

    if content:
        success("YouTube metadata exists")


def main() -> int:
    try:
        config = load_and_validate(ROOT / "Input" / "topic.txt")
        mode = get_format(config)
        parts_count = get_parts(config)
        scenes_per_part = get_scenes(config)
    except Exception as exc:
        error(f"Input configuration is invalid: {exc}")
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

    if not (ROOT / "output" / "config" / "selected_model.json").is_file():
        warning("Selected model manifest is missing")

    validate_images(total_scenes)
    validate_audio_jobs(total_scenes)
    validate_photo_motion(total_scenes, mode)
    validate_caption_data(total_scenes)

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
