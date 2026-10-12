
#!/usr/bin/env python3
"""Strict validation of Katha Lok AI final deliverables."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def fail(message):
    raise RuntimeError(message)


def require_file(path, label, minimum=1):
    if not path.is_file() or path.stat().st_size < minimum:
        fail(f"{label} missing or too small: {path}")


def read_json(path):
    require_file(path, "JSON file")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        fail(f"Missing path: {label}")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        fail(f"Path escapes repository: {label}")

    return path


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError):
        fail(f"Invalid {label}: {value!r}")

    if result < 1:
        fail(f"{label} must be positive.")

    return result


def scene_identity(row, index):
    if not isinstance(row, dict):
        fail(f"Scene record {index} is not an object.")

    number = positive_int(
        row.get("global_scene", row.get("scene", index)),
        "global scene",
    )
    part = positive_int(row.get("part", 1), "part")
    local = positive_int(
        row.get("local_scene", row.get("scene", index)),
        "local scene",
    )
    return number, part, local


def extract_rows(data, keys, label):
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next(
            (
                data.get(key) for key in keys
                if isinstance(data.get(key), list)
            ),
            None,
        )
    else:
        rows = None

    if not isinstance(rows, list) or not rows:
        fail(f"{label} has no scene entries.")

    return rows


def load_config():
    require_file(CONFIG, "Input configuration")
    values = {}

    for raw in CONFIG.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip().upper()] = (
            value.split("#", 1)[0].strip().strip("'\"")
        )

    fmt = values.get("FORMAT", "short").lower()

    if fmt in {
        "full", "long", "landscape", "youtube",
        "youtube_full", "youtube-long",
    }:
        mode = "full"
    elif fmt in {
        "short", "shorts", "vertical", "reels",
        "reel", "youtube_short", "youtube-shorts",
    }:
        mode = "short"
    else:
        fail(f"Unsupported FORMAT: {fmt}")

    try:
        requested_parts = int(values.get("PARTS", "1"))
    except ValueError:
        fail("PARTS must be a positive integer.")

    if requested_parts < 1:
        fail("PARTS must be positive.")

    return mode, requested_parts


def probe_video(path, ffprobe):
    require_file(path, "Video", 1000)

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration,size:"
            "stream=codec_type,codec_name,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
        fail(f"ffprobe failed for {path.name}: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media data for {path.name}: {exc}")

    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )

    if not video or not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid video stream or duration: {path.name}")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    if width <= 0 or height <= 0:
        fail(f"Invalid dimensions: {path.name}")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": any(
            item.get("codec_type") == "audio" for item in streams
        ),
        "size": path.stat().st_size,
        "codec": video.get("codec_name", "unknown"),
    }


def validate_images(jobs, manifest):
    if not isinstance(manifest, dict):
        fail("Image manifest must be a JSON object.")

    manifest_rows = manifest.get("scenes")
    if not isinstance(manifest_rows, list):
        fail("Image manifest has no scenes list.")

    if len(manifest_rows) != len(jobs):
        fail("Image manifest count differs from visual jobs.")

    job_ids = []
    for index, job in enumerate(jobs, 1):
        number, _, _ = scene_identity(job, index)
        job_ids.append(number)

        raw = job.get("image_path") or job.get("image")
        path = safe_path(raw, f"scene {number} image")

        require_file(path, f"Scene {number} image", 1000)

        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            fail(f"Unsupported image format: {path.name}")

        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            fail(f"Invalid image for scene {number}: {exc}")

    manifest_ids = [
        scene_identity(row, index)[0]
        for index, row in enumerate(manifest_rows, 1)
    ]

    if job_ids != manifest_ids:
        fail("Image manifest scene order differs from visual jobs.")

    if len(set(job_ids)) != len(job_ids):
        fail("Duplicate visual scene numbers.")

    return len(jobs)


def validate_motion(jobs, rows, ffprobe, mode):
    if not isinstance(rows, list) or not rows:
        fail("Motion manifest must be a non-empty list.")

    expected_dimensions = (
        (1280, 720) if mode == "full" else (720, 1280)
    )

    job_map = {}
    for index, job in enumerate(jobs, 1):
        number, part, local = scene_identity(job, index)
        key = (part, local)

        if key in job_map:
            fail(f"Duplicate visual scene key: {key}")

        job_map[key] = number

    motion_map = {}
    total_duration = 0.0

    for index, row in enumerate(rows, 1):
        number, part, local = scene_identity(row, index)
        key = (part, local)

        if key in motion_map:
            fail(f"Duplicate motion scene key: {key}")

        if job_map.get(key) != number:
            fail(f"Motion/visual scene mismatch: {key}")

        path = safe_path(row.get("video"), f"scene {number} video")
        info = probe_video(path, ffprobe)

        if (info["width"], info["height"]) != expected_dimensions:
            fail(
                f"Scene {number} has wrong resolution: "
                f"{info['width']}x{info['height']}."
            )

        if not info["has_audio"]:
            fail(f"Scene {number} has no audio stream.")

        try:
            duration = float(row.get("duration"))
        except (TypeError, ValueError):
            fail(f"Invalid duration for scene {number}.")

        if not math.isfinite(duration) or duration <= 0:
            fail(f"Non-positive duration for scene {number}.")

        if abs(info["duration"] - duration) > 1.0:
            fail(f"Manifest/video duration mismatch at scene {number}.")

        motion_map[key] = row
        total_duration += info["duration"]

    if set(motion_map) != set(job_map):
        fail("Motion manifest does not cover every visual scene.")

    numbers = [
        scene_identity(row, index)[0]
        for index, row in enumerate(rows, 1)
    ]

    if numbers != list(range(1, len(rows) + 1)):
        fail("Motion scene numbering is not continuous.")

    if len(rows) != len(jobs):
        fail("Motion scene count differs from visual jobs.")

    return total_duration


def validate_captions(jobs):
    data = read_json(CAPTIONS)

    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Captions JSON is not marked completed.")

    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        fail("Captions JSON is empty.")

    caption_map = {}
    for row in rows:
        if not isinstance(row, dict):
            fail("Invalid caption record.")

        key = (
            positive_int(row.get("part"), "caption part"),
            positive_int(row.get("scene"), "caption scene"),
        )
        text = str(row.get("text", "")).strip()

        if not text:
            fail(f"Empty caption at {key}.")
        if key in caption_map:
            fail(f"Duplicate caption at {key}.")

        caption_map[key] = text

    expected = set()
    for index, job in enumerate(jobs, 1):
        _, part, local = scene_identity(job, index)
        expected.add((part, local))

    if set(caption_map) != expected:
        fail("Caption records do not match visual scenes.")

    require_file(SRT, "Final SRT", 10)
    text = SRT.read_text(encoding="utf-8-sig").strip()

    # Validate SRT blocks and timestamps, not a false one-cue-per-scene rule.
    blocks = re.split(r"\n\s*\n", text)
    cue_count = 0
    previous_start = -1.0

    timestamp = re.compile(
        r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})"
        r"\s*-->\s*"
        r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$"
    )

    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            fail("Malformed SRT cue: missing index, timestamp or text.")

        if not lines[0].strip().isdigit():
            fail("SRT cue index is invalid.")

        match = timestamp.match(lines[1].strip())
        if not match:
            fail(f"Invalid SRT timestamp: {lines[1]}")

        values = [int(value) for value in match.groups()]
        start = (
            values[0] * 3600 + values[1] * 60
            + values[2] + values[3] / 1000
        )
        end = (
            values[4] * 3600 + values[5] * 60
            + values[6] + values[7] / 1000
        )

        if end <= start:
            fail("SRT cue end must be after its start.")
        if start < previous_start:
            fail("SRT cues are not ordered by start time.")
        if not any(line.strip() for line in lines[2:]):
            fail("SRT cue has no subtitle text.")

        previous_start = start
        cue_count += 1

    if cue_count == 0:
        fail("SRT contains no valid cues.")

    # Multiple SRT cues per scene are valid.
    print(
        f"Captions validated: {len(caption_map)} scene records, "
        f"{cue_count} SRT cues."
    )
    return len(caption_map), cue_count


def main():
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        fail("ffprobe is not installed.")

    mode, requested_parts = load_config()
    dimensions = (1280, 720) if mode == "full" else (720, 1280)

    model = read_json(MODEL)
    if (
        not isinstance(model, dict)
        or model.get("status") != "selected"
        or not str(model.get("model", "")).strip()
    ):
        fail("Selected Gemini model record is invalid.")

    jobs_data = read_json(JOBS)
    jobs = extract_rows(
        jobs_data, ("jobs", "visual_jobs", "scenes"), "Visual jobs"
    )

    image_count = validate_images(jobs, read_json(IMAGES))
    motion_rows = read_json(MOTION)
    scene_duration = validate_motion(
        jobs, motion_rows, ffprobe, mode
    )

    final_info = probe_video(FINAL, ffprobe)
    if (final_info["width"], final_info["height"]) != dimensions:
        fail("Final video resolution does not match FORMAT.")

    if not final_info["has_audio"]:
        fail("Final video has no audio stream.")

    tolerance = max(2.0, final_info["duration"] * 0.03)
    if abs(scene_duration - final_info["duration"]) > tolerance:
        fail(
            f"Timeline duration mismatch: scenes={scene_duration:.2f}s, "
            f"final={final_info['duration']:.2f}s."
        )

    alias = FULL if mode == "full" else SHORT
    alias_info = probe_video(alias, ffprobe)

    if (alias_info["width"], alias_info["height"]) != dimensions:
        fail("Selected format alias has wrong dimensions.")

    if abs(alias_info["duration"] - final_info["duration"]) > 0.10:
        fail("Selected format alias duration differs from final video.")

    if alias_info["has_audio"] != final_info["has_audio"]:
        fail("Selected format alias audio differs from final video.")

    caption_count, srt_cues = validate_captions(jobs)

    require_file(METADATA, "YouTube metadata", 50)
    metadata = METADATA.read_text(encoding="utf-8-sig")
    for label in ("Title:", "Description:", "Tags:"):
        if label.lower() not in metadata.lower():
            fail(f"YouTube metadata missing {label}")

    part_dir = OUTPUT / "parts"
    parts = sorted(part_dir.glob("part_*.mp4")) if part_dir.is_dir() else []

    if not parts:
        fail("No rendered parts found.")
    if len(parts) > requested_parts:
        fail(f"Rendered parts exceed PARTS={requested_parts}.")

    for path in parts:
        info = probe_video(path, ffprobe)
        if (info["width"], info["height"]) != dimensions:
            fail(f"Wrong part dimensions: {path.name}")
        if not info["has_audio"]:
            fail(f"Part has no audio: {path.name}")

    print("=" * 60)
    print("FINAL OUTPUT VALIDATION: SUCCESS")
    print("Gemini model:", model["model"])
    print("Format:", mode)
    print("Resolution:", f"{final_info['width']}x{final_info['height']}")
    print("Final duration:", f"{final_info['duration']:.2f}s")
    print("Final audio:", "present")
    print("Visual scenes:", len(jobs))
    print("Validated images:", image_count)
    print("Motion clips:", len(motion_rows))
    print("Captioned scene records:", caption_count)
    print("SRT cues:", srt_cues)
    print("Rendered parts:", len(parts))
    print("Metadata characters:", len(metadata))
    print("Selected output:", alias.relative_to(ROOT))
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FINAL OUTPUT VALIDATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
