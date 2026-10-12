#!/usr/bin/env python3
"""Strict final-output validation for Katha Lok AI Photo Motion."""

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

from input_config import load_and_validate, get_format, get_parts

OUTPUT = ROOT / "output"
CONFIG = ROOT / "Input" / "topic.txt"

FINAL = OUTPUT / "videos" / "katha_lok_ai_final.mp4"
SHORT = OUTPUT / "videos" / "katha_lok_ai_short.mp4"
FULL = OUTPUT / "videos" / "katha_lok_ai_full.mp4"

JOBS = OUTPUT / "visuals" / "visual_jobs.json"
IMAGES = OUTPUT / "visuals" / "image_manifest.json"
MOTION = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
CAPTIONS = OUTPUT / "captions" / "captions.json"
SRT = OUTPUT / "captions" / "final_captions.srt"
METADATA = OUTPUT / "youtube_metadata.txt"
MODEL = OUTPUT / "config" / "selected_model.json"
PARTS_DIR = OUTPUT / "parts"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def fail(message):
    raise RuntimeError(message)


def log(*values):
    print(*values, flush=True)


def require_file(path, label, minimum=1):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < minimum:
        fail(f"{label} is missing or too small: {path}")
    return path


def read_json(path):
    require_file(path, "JSON file")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        fail(f"Missing path for {label}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        fail(f"{label} points outside the repository: {path}")

    return path


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError):
        fail(f"Invalid {label}: {value!r}")

    if result < 1:
        fail(f"{label} must be greater than zero.")

    return result


def scene_identity(row, index, label):
    if not isinstance(row, dict):
        fail(f"{label} record {index} is not an object.")

    number = positive_int(
        row.get("global_scene", row.get("scene", index)),
        f"{label} global scene",
    )
    part = positive_int(row.get("part", 1), f"{label} part")
    local = positive_int(
        row.get("local_scene", row.get("scene", index)),
        f"{label} local scene",
    )

    return number, part, local


def extract_rows(data, keys, label):
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next(
            (
                data.get(key)
                for key in keys
                if isinstance(data.get(key), list)
            ),
            None,
        )
    else:
        rows = None

    if not isinstance(rows, list) or not rows:
        fail(f"{label} contains no scene records.")

    return rows


def probe_video(path, ffprobe):
    require_file(path, "Video", 1000)

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "format=duration,size:"
            "stream=codec_type,codec_name,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        fail(
            f"ffprobe failed for {path.name}: "
            f"{result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid video metadata for {path.name}: {exc}")

    video = next(
        (
            item for item in streams
            if item.get("codec_type") == "video"
        ),
        None,
    )

    if not video:
        fail(f"No video stream in {path.name}.")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid duration in {path.name}.")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    if width <= 0 or height <= 0:
        fail(f"Invalid video dimensions in {path.name}.")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": any(
            item.get("codec_type") == "audio"
            for item in streams
        ),
        "size": path.stat().st_size,
        "codec": video.get("codec_name", "unknown"),
    }


def validate_visual_jobs():
    data = read_json(JOBS)
    jobs = extract_rows(
        data,
        ("jobs", "visual_jobs", "scenes"),
        "Visual jobs",
    )

    identities = []
    image_paths = set()

    for index, job in enumerate(jobs, 1):
        number, part, local = scene_identity(
            job, index, "Visual job"
        )

        image_value = job.get("image_path") or job.get("image")
        image_path = safe_path(
            image_value, f"image for scene {number}"
        )
        require_file(image_path, f"Scene {number} image", 1000)

        if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            fail(
                f"Unsupported image format for scene {number}: "
                f"{image_path.suffix}"
            )

        try:
            from PIL import Image

            with Image.open(image_path) as image:
                image.verify()

            with Image.open(image_path) as image:
                if image.width < 64 or image.height < 64:
                    fail(
                        f"Scene {number} image dimensions are too small."
                    )
        except Exception as exc:
            fail(f"Invalid image for scene {number}: {exc}")

        identities.append((number, part, local))
        image_paths.add(image_path)

    numbers = [item[0] for item in identities]

    if numbers != list(range(1, len(numbers) + 1)):
        fail("Visual scene numbers must be continuous from 1.")

    if len(set((part, local) for _, part, local in identities)) != len(
        identities
    ):
        fail("Duplicate part/local-scene mapping in visual jobs.")

    image_data = read_json(IMAGES)
    image_rows = extract_rows(
        image_data,
        ("scenes", "images", "items"),
        "Image manifest",
    )

    image_ids = [
        scene_identity(row, index, "Image manifest")[0]
        for index, row in enumerate(image_rows, 1)
    ]

    if image_ids != numbers:
        fail("Image manifest scene order does not match visual jobs.")

    if len(image_rows) != len(jobs):
        fail("Image manifest count does not match visual jobs.")

    log("Visual jobs validated:", len(jobs))
    log("Scene images validated:", len(image_paths))

    return jobs, identities


def validate_motion(jobs, identities, ffprobe, mode):
    rows = read_json(MOTION)

    if not isinstance(rows, list) or not rows:
        fail("Motion manifest must be a non-empty JSON list.")

    if len(rows) != len(jobs):
        fail(
            f"Motion manifest has {len(rows)} scenes; "
            f"visual jobs have {len(jobs)}."
        )

    expected_dimensions = (
        (1280, 720) if mode == "full" else (720, 1280)
    )

    expected_by_key = {
        (part, local): number
        for number, part, local in identities
    }

    seen_keys = set()
    total_duration = 0.0

    for index, row in enumerate(rows, 1):
        number, part, local = scene_identity(
            row, index, "Motion manifest"
        )
        key = (part, local)

        if key in seen_keys:
            fail(f"Duplicate motion mapping: part={part}, scene={local}")

        seen_keys.add(key)

        if expected_by_key.get(key) != number:
            fail(
                f"Visual/motion mapping mismatch at global scene "
                f"{number}, part {part}, local scene {local}."
            )

        video_path = safe_path(
            row.get("video"), f"video for scene {number}"
        )
        info = probe_video(video_path, ffprobe)

        if (info["width"], info["height"]) != expected_dimensions:
            fail(
                f"Scene {number} has resolution "
                f"{info['width']}x{info['height']}; expected "
                f"{expected_dimensions[0]}x{expected_dimensions[1]}."
            )

        if not info["has_audio"]:
            fail(f"Scene {number} has no audio stream.")

        try:
            recorded_duration = float(row.get("duration"))
        except (TypeError, ValueError):
            fail(f"Invalid duration in motion manifest for scene {number}.")

        if (
            not math.isfinite(recorded_duration)
            or recorded_duration <= 0
        ):
            fail(f"Non-positive duration for scene {number}.")

        if abs(info["duration"] - recorded_duration) > 1.0:
            fail(
                f"Scene {number} manifest duration differs from "
                f"the actual video duration."
            )

        total_duration += info["duration"]

    if seen_keys != set(expected_by_key):
        fail("Motion manifest does not cover every visual scene.")

    global_numbers = [
        scene_identity(row, index, "Motion manifest")[0]
        for index, row in enumerate(rows, 1)
    ]

    if global_numbers != list(range(1, len(rows) + 1)):
        fail("Motion scene numbering is not continuous.")

    log("Audio/video scene clips validated:", len(rows))

    return rows, total_duration


def validate_captions(jobs, identities, final_duration):
    data = read_json(CAPTIONS)

    if not isinstance(data, dict):
        fail("Captions JSON must be an object.")

    if data.get("status") != "completed":
        fail("Captions JSON is not marked completed.")

    caption_rows = data.get("captions")

    if not isinstance(caption_rows, list) or not caption_rows:
        fail("Captions JSON contains no captions.")

    caption_map = {}

    for row in caption_rows:
        if not isinstance(row, dict):
            fail("Invalid caption record.")

        key = (
            positive_int(row.get("part"), "caption part"),
            positive_int(row.get("scene"), "caption scene"),
        )
        text = str(row.get("text", "")).strip()

        if not text:
            fail(f"Empty caption for part/scene {key}.")

        if key in caption_map:
            fail(f"Duplicate caption record for part/scene {key}.")

        caption_map[key] = text

    expected_keys = {
        (part, local)
        for _, part, local in identities
    }

    if set(caption_map) != expected_keys:
        missing = sorted(expected_keys - set(caption_map))
        extra = sorted(set(caption_map) - expected_keys)
        fail(
            f"Caption mapping mismatch. Missing={missing}; extra={extra}"
        )

    require_file(SRT, "Final SRT captions", 10)
    srt_text = SRT.read_text(encoding="utf-8-sig").strip()

    if not srt_text:
        fail("Final SRT is empty.")

    blocks = re.split(r"\n\s*\n", srt_text)
    timestamp_pattern = re.compile(
        r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})"
        r"\s*-->\s*"
        r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$"
    )

    previous_start = -1.0
    cue_count = 0

    for block in blocks:
        lines = block.strip().splitlines()

        if len(lines) < 3:
            fail("Malformed SRT cue: index, timestamp or text missing.")

        if not lines[0].strip().isdigit():
            fail("SRT cue index must be numeric.")

        match = timestamp_pattern.match(lines[1].strip())

        if not match:
            fail(f"Invalid SRT timestamp: {lines[1]}")

        values = [int(value) for value in match.groups()]

        start = (
            values[0] * 3600
            + values[1] * 60
            + values[2]
            + values[3] / 1000
        )
        end = (
            values[4] * 3600
            + values[5] * 60
            + values[6]
            + values[7] / 1000
        )

        if end <= start:
            fail("SRT cue end must be after its start.")

        if start < previous_start:
            fail("SRT cues are not in chronological order.")

        if end > final_duration + 1.0:
            fail(
                f"SRT cue extends beyond final video duration: "
                f"{end:.2f}s > {final_duration:.2f}s."
            )

        cue_text = "\n".join(lines[2:]).strip()

        if not cue_text:
            fail("SRT cue has no subtitle text.")

        previous_start = start
        cue_count += 1

    if cue_count == 0:
        fail("No valid SRT cues found.")

    log("Caption scene records validated:", len(caption_map))
    log("SRT cues validated:", cue_count)

    return len(caption_map), cue_count


def validate_parts(ffprobe, mode, requested_parts):
    if not PARTS_DIR.is_dir():
        fail("Output parts directory is missing.")

    part_files = sorted(PARTS_DIR.glob("part_*.mp4"))

    if len(part_files) != requested_parts:
        fail(
            f"Expected exactly PARTS={requested_parts} part videos, "
            f"found {len(part_files)}."
        )

    dimensions = (
        (1280, 720) if mode == "full" else (720, 1280)
    )

    for expected_number, path in enumerate(part_files, 1):
        expected_name = f"part_{expected_number:02d}.mp4"

        if path.name != expected_name:
            fail(
                f"Unexpected part filename {path.name}; "
                f"expected {expected_name}."
            )

        info = probe_video(path, ffprobe)

        if (info["width"], info["height"]) != dimensions:
            fail(f"Wrong resolution in {path.name}.")

        if not info["has_audio"]:
            fail(f"Audio is missing from {path.name}.")

    log("Requested part videos validated:", len(part_files))


def validate_metadata():
    require_file(METADATA, "YouTube metadata", 50)
    metadata = METADATA.read_text(encoding="utf-8-sig").strip()

    for label in ("Title:", "Description:", "Tags:"):
        if label.lower() not in metadata.lower():
            fail(f"YouTube metadata is missing {label}")

    return len(metadata)


def main():
    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        fail("ffprobe is not installed.")

    require_file(CONFIG, "Input configuration")

    config = load_and_validate(CONFIG)
    mode = get_format(config)
    requested_parts = get_parts(config)

    dimensions = (
        (1280, 720) if mode == "full" else (720, 1280)
    )

    model = read_json(MODEL)

    if (
        not isinstance(model, dict)
        or model.get("status") != "selected"
        or not str(model.get("model", "")).strip()
    ):
        fail("Selected Gemini model record is missing or invalid.")

    jobs, identities = validate_visual_jobs()

    motion_rows, scene_duration = validate_motion(
        jobs, identities, ffprobe, mode
    )

    final_info = probe_video(FINAL, ffprobe)

    if (final_info["width"], final_info["height"]) != dimensions:
        fail(
            f"Final video dimensions are "
            f"{final_info['width']}x{final_info['height']}; "
            f"expected {dimensions[0]}x{dimensions[1]}."
        )

    if not final_info["has_audio"]:
        fail("Final video has no audio stream.")

    duration_tolerance = max(2.0, final_info["duration"] * 0.03)

    if abs(scene_duration - final_info["duration"]) > duration_tolerance:
        fail(
            f"Scene timeline ({scene_duration:.2f}s) differs from "
            f"final video ({final_info['duration']:.2f}s)."
        )

    selected_alias = FULL if mode == "full" else SHORT
    opposite_alias = SHORT if mode == "full" else FULL

    alias_info = probe_video(selected_alias, ffprobe)

    if (alias_info["width"], alias_info["height"]) != dimensions:
        fail("Selected format alias has incorrect dimensions.")

    if abs(alias_info["duration"] - final_info["duration"]) > 0.10:
        fail("Selected format alias duration differs from final video.")

    if alias_info["has_audio"] != final_info["has_audio"]:
        fail("Selected format alias audio differs from final video.")

    if opposite_alias.exists():
        fail(
            f"Wrong-format output alias also exists: {opposite_alias.name}. "
            "Only the requested FORMAT alias should remain."
        )

    caption_count, srt_cues = validate_captions(
        jobs, identities, final_info["duration"]
    )

    metadata_characters = validate_metadata()

    validate_parts(ffprobe, mode, requested_parts)

    log("=" * 60)
    log("FINAL OUTPUT VALIDATION: SUCCESS")
    log("Gemini model:", model["model"])
    log("FORMAT:", mode)
    log("Resolution:", f"{final_info['width']}x{final_info['height']}")
    log("Final duration:", f"{final_info['duration']:.2f}s")
    log("Final file size:", f"{final_info['size'] / 1048576:.2f} MB")
    log("Final audio:", "present")
    log("Visual scenes:", len(jobs))
    log("Motion scene clips:", len(motion_rows))
    log("Caption scene records:", caption_count)
    log("SRT cues:", srt_cues)
    log("Requested parts:", requested_parts)
    log("Metadata characters:", metadata_characters)
    log("Selected final output:", selected_alias.relative_to(ROOT))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"FINAL OUTPUT VALIDATION FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)