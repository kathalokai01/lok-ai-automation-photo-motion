
#!/usr/bin/env python3
"""Validate the final photo-motion video and all required deliverables."""

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
THUMBNAIL = OUTPUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUTPUT / "thumbnail" / "thumbnail_manifest.json"
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
        return json.loads(path.read_text(encoding="utf-8-sig"))
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
        number = int(value)
    except (TypeError, ValueError):
        fail(f"Invalid {label}: {value!r}")
    if number < 1:
        fail(f"{label} must be greater than zero.")
    return number


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
            (data.get(key) for key in keys
             if isinstance(data.get(key), list)),
            None,
        )
    else:
        rows = None
    if not isinstance(rows, list) or not rows:
        fail(f"{label} contains no scene records.")
    return rows


def probe_media(path, ffprobe, minimum=1000):
    require_file(path, "Media file", minimum)
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height",
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
        fail(f"Invalid media metadata for {path.name}: {exc}")

    video = next(
        (stream for stream in streams
         if stream.get("codec_type") == "video"),
        None,
    )
    if not video:
        fail(f"No video stream in {path.name}.")
    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid duration in {path.name}.")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        fail(f"Invalid dimensions in {path.name}.")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": any(
            stream.get("codec_type") == "audio" for stream in streams
        ),
        "size": path.stat().st_size,
    }


def validate_images():
    jobs_data = read_json(JOBS)
    jobs = extract_rows(
        jobs_data, ("jobs", "visual_jobs", "scenes"), "Visual jobs"
    )
    identities = []
    for index, job in enumerate(jobs, 1):
        number, part, local = scene_identity(job, index, "Visual job")
        image_value = job.get("image_path") or job.get("image")
        path = safe_path(image_value, f"image for scene {number}")
        require_file(path, f"Scene {number} image", 1000)
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            fail(f"Unsupported image type for scene {number}: {path.suffix}")
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                if image.width < 64 or image.height < 64:
                    fail(f"Scene {number} image is too small.")
        except Exception as exc:
            fail(f"Invalid image for scene {number}: {exc}")
        identities.append((number, part, local))

    numbers = [row[0] for row in identities]
    if numbers != list(range(1, len(numbers) + 1)):
        fail("Visual scene numbering must be continuous from 1.")
    keys = [(part, local) for _, part, local in identities]
    if len(keys) != len(set(keys)):
        fail("Visual jobs contain duplicate part/scene mappings.")

    image_data = read_json(IMAGES)
    image_rows = extract_rows(
        image_data, ("scenes", "images", "items"), "Image manifest"
    )
    image_numbers = [
        scene_identity(row, index, "Image manifest")[0]
        for index, row in enumerate(image_rows, 1)
    ]
    if image_numbers != numbers or len(image_rows) != len(jobs):
        fail("Image manifest does not match the visual jobs.")
    log("Images and visual mappings validated:", len(jobs))
    return jobs, identities


def validate_motion(jobs, identities, ffprobe, mode):
    rows = read_json(MOTION)
    if not isinstance(rows, list) or not rows:
        fail("Motion manifest must be a non-empty JSON list.")
    if len(rows) != len(jobs):
        fail(f"Motion has {len(rows)} scenes; visuals have {len(jobs)}.")

    dimensions = (1280, 720) if mode == "full" else (720, 1280)
    expected = {(part, local): number for number, part, local in identities}
    seen = set()
    total_duration = 0.0

    for index, row in enumerate(rows, 1):
        number, part, local = scene_identity(row, index, "Motion manifest")
        key = (part, local)
        if key in seen:
            fail(f"Duplicate motion mapping: {key}")
        seen.add(key)
        if expected.get(key) != number:
            fail(f"Visual/motion mapping mismatch at scene {number}.")

        path = safe_path(row.get("video"), f"video for scene {number}")
        info = probe_media(path, ffprobe)
        if (info["width"], info["height"]) != dimensions:
            fail(f"Wrong resolution for scene {number}.")
        if not info["has_audio"]:
            fail(f"Scene {number} has no audio stream.")

        try:
            recorded = float(row.get("duration"))
        except (TypeError, ValueError):
            fail(f"Invalid manifest duration for scene {number}.")
        if not math.isfinite(recorded) or recorded <= 0:
            fail(f"Invalid manifest duration for scene {number}.")
        if abs(info["duration"] - recorded) > 1.0:
            fail(f"Recorded duration disagrees with scene {number} video.")
        total_duration += info["duration"]

    if seen != set(expected):
        fail("Motion manifest does not cover every visual scene.")
    if [scene_identity(row, i, "Motion manifest")[0]
            for i, row in enumerate(rows, 1)] != list(range(1, len(rows) + 1)):
        fail("Motion scene numbers are not continuous.")

    log("Motion clips validated:", len(rows))
    return rows, total_duration


def validate_captions(identities, duration):
    data = read_json(CAPTIONS)
    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Captions JSON is missing or not completed.")
    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        fail("Captions JSON has no caption records.")

    actual = {}
    for row in rows:
        if not isinstance(row, dict):
            fail("Invalid caption record.")
        key = (
            positive_int(row.get("part"), "caption part"),
            positive_int(row.get("scene"), "caption scene"),
        )
        if not str(row.get("text", "")).strip():
            fail(f"Empty caption for {key}.")
        if key in actual:
            fail(f"Duplicate caption record: {key}")
        actual[key] = str(row["text"]).strip()

    expected = {(part, local) for _, part, local in identities}
    if set(actual) != expected:
        fail(
            f"Caption mapping mismatch: missing={sorted(expected-set(actual))}; "
            f"extra={sorted(set(actual)-expected)}"
        )

    require_file(SRT, "Final SRT captions", 10)
    srt = SRT.read_text(encoding="utf-8-sig").strip()
    if not srt:
        fail("Final SRT is empty.")

    blocks = re.split(r"\n\s*\n", srt)
    pattern = re.compile(
        r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})"
        r"\s*-->\s*"
        r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$"
    )
    previous_start = -1.0
    cue_count = 0

    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail("Malformed SRT cue.")
        match = pattern.match(lines[1].strip())
        if not match:
            fail(f"Invalid SRT timestamp: {lines[1]}")
        values = [int(value) for value in match.groups()]
        start = values[0]*3600 + values[1]*60 + values[2] + values[3]/1000
        end = values[4]*3600 + values[5]*60 + values[6] + values[7]/1000
        if end <= start:
            fail("SRT cue end must be after its start.")
        if start < previous_start:
            fail("SRT cues are not in chronological order.")
        if end > duration + 1.0:
            fail("SRT cue extends beyond the final video.")
        if not "\n".join(lines[2:]).strip():
            fail("SRT cue has no text.")
        previous_start = start
        cue_count += 1

    if cue_count == 0:
        fail("No valid SRT cues.")
    log("Caption scenes and SRT cues validated:", len(actual), cue_count)


def validate_metadata_and_thumbnail(mode):
    require_file(METADATA, "YouTube metadata", 50)
    metadata = METADATA.read_text(encoding="utf-8-sig").strip()
    for label in ("Title:", "Description:", "Tags:"):
        if label.lower() not in metadata.lower():
            fail(f"YouTube metadata is missing {label}")

    require_file(THUMBNAIL, "Thumbnail", 1000)
    try:
        from PIL import Image
        with Image.open(THUMBNAIL) as image:
            image.verify()
        with Image.open(THUMBNAIL) as image:
            expected = (1080, 1920) if mode == "short" else (1280, 720)
            if image.size != expected:
                fail(
                    f"Thumbnail size is {image.width}x{image.height}; "
                    f"expected {expected[0]}x{expected[1]}."
                )
    except Exception as exc:
        fail(f"Invalid thumbnail: {exc}")

    manifest = read_json(THUMBNAIL_MANIFEST)
    if not isinstance(manifest, dict):
        fail("Thumbnail manifest must be a JSON object.")
    log("Metadata and thumbnail validated.")


def validate_parts(ffprobe, mode, requested_parts):
    if not PARTS_DIR.is_dir():
        fail("Output parts directory is missing.")
    files = sorted(PARTS_DIR.glob("part_*.mp4"))
    if len(files) != requested_parts:
        fail(f"Expected {requested_parts} parts; found {len(files)}.")
    dimensions = (1280, 720) if mode == "full" else (720, 1280)
    for number, path in enumerate(files, 1):
        if path.name != f"part_{number:02d}.mp4":
            fail(f"Unexpected part filename: {path.name}")
        info = probe_media(path, ffprobe)
        if (info["width"], info["height"]) != dimensions:
            fail(f"Wrong resolution in {path.name}.")
        if not info["has_audio"]:
            fail(f"Audio missing from {path.name}.")
    log("Part videos validated:", len(files))


def main():
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        fail("ffprobe is not installed.")
    require_file(CONFIG, "Input configuration")

    config = load_and_validate(CONFIG)
    mode = get_format(config)
    requested_parts = get_parts(config)
    dimensions = (1280, 720) if mode == "full" else (720, 1280)

    model = read_json(MODEL)
    if (
        not isinstance(model, dict)
        or model.get("status") != "selected"
        or not str(model.get("model", "")).strip()
    ):
        fail("Selected Gemini model metadata is missing or invalid.")

    jobs, identities = validate_images()
    motion_rows, scene_duration = validate_motion(
        jobs, identities, ffprobe, mode
    )

    final_info = probe_media(FINAL, ffprobe)
    if (final_info["width"], final_info["height"]) != dimensions:
        fail("Final video resolution does not match FORMAT.")
    if not final_info["has_audio"]:
        fail("Final video has no audio stream.")

    tolerance = max(2.0, final_info["duration"] * 0.03)
    if abs(scene_duration - final_info["duration"]) > tolerance:
        fail(
            f"Scene timeline ({scene_duration:.2f}s) differs from final "
            f"video ({final_info['duration']:.2f}s)."
        )

    selected = FULL if mode == "full" else SHORT
    opposite = SHORT if mode == "full" else FULL
    alias_info = probe_media(selected, ffprobe)
    if (alias_info["width"], alias_info["height"]) != dimensions:
        fail("Selected format alias has incorrect dimensions.")
    if abs(alias_info["duration"] - final_info["duration"]) > 0.10:
        fail("Selected alias duration differs from the final video.")
    if alias_info["has_audio"] != final_info["has_audio"]:
        fail("Selected alias audio differs from the final video.")
    if opposite.exists():
        fail(f"Unexpected opposite-format alias exists: {opposite.name}")

    validate_captions(identities, final_info["duration"])
    validate_metadata_and_thumbnail(mode)
    validate_parts(ffprobe, mode, requested_parts)

    log("=" * 56)
    log("FINAL OUTPUT VALIDATION: SUCCESS")
    log("FORMAT:", mode)
    log("Resolution:", f"{final_info['width']}x{final_info['height']}")
    log("Duration:", f"{final_info['duration']:.2f}s")
    log("Size:", f"{final_info['size'] / 1048576:.2f} MB")
    log("Visual scenes:", len(jobs))
    log("Motion scenes:", len(motion_rows))
    log("Parts:", requested_parts)
    log("Selected output:", selected.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FINAL OUTPUT VALIDATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
