
#!/usr/bin/env python3
"""Strict validation for the completed Katha Lok AI photo-motion pipeline."""

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
RENDER = OUTPUT / "scene_renders" / "render_manifest.json"
CAPTIONS = OUTPUT / "captions" / "captions.json"
SRT = OUTPUT / "captions" / "final_captions.srt"
METADATA = OUTPUT / "youtube_metadata.txt"
MODEL = OUTPUT / "config" / "selected_model.json"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUTPUT / "thumbnail" / "thumbnail_manifest.json"
PARTS_DIR = OUTPUT / "parts"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MIN_MEDIA_BYTES = 1000


def fail(message):
    raise RuntimeError(message)


def log(*values):
    print(*values, flush=True)


def require_file(path, label, minimum=1):
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
        fail(f"{label} points outside the repository.")

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
    part = positive_int(row.get("part"), f"{label} part")
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


def probe_media(path, ffprobe, minimum=MIN_MEDIA_BYTES):
    require_file(path, "Media file", minimum)

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode:
        fail(
            f"ffprobe failed for {path.name}: "
            f"{result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media metadata for {path.name}: {exc}")

    video = next(
        (
            stream for stream in streams
            if stream.get("codec_type") == "video"
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
        fail(f"Invalid dimensions in {path.name}.")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": any(
            stream.get("codec_type") == "audio"
            for stream in streams
        ),
        "size": path.stat().st_size,
    }


def validate_model():
    data = read_json(MODEL)

    if (
        not isinstance(data, dict)
        or data.get("status") != "selected"
        or not str(data.get("model", "")).strip()
    ):
        fail("Selected model metadata is missing or invalid.")

    log("Selected model metadata validated.")


def validate_visuals():
    jobs_data = read_json(JOBS)
    jobs = extract_rows(
        jobs_data,
        ("jobs", "visual_jobs", "scenes"),
        "Visual jobs",
    )

    identities = []
    seen_keys = set()

    try:
        from PIL import Image
    except ImportError as exc:
        fail(f"Pillow is required for image validation: {exc}")

    for index, job in enumerate(jobs, 1):
        number, part, local = scene_identity(
            job, index, "Visual job"
        )

        key = (part, local)
        if key in seen_keys:
            fail(f"Duplicate visual mapping: {key}")
        seen_keys.add(key)

        image_value = job.get("image_path") or job.get("image")
        image_path = safe_path(
            image_value, f"image for scene {number}"
        )
        require_file(image_path, f"Scene {number} image", 1000)

        if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            fail(f"Unsupported image type for scene {number}.")

        try:
            with Image.open(image_path) as image:
                image.verify()

            with Image.open(image_path) as image:
                if image.width < 64 or image.height < 64:
                    fail(f"Scene {number} image is too small.")
        except Exception as exc:
            fail(f"Invalid image for scene {number}: {exc}")

        identities.append((number, part, local))

    identities.sort()

    if [item[0] for item in identities] != list(
        range(1, len(identities) + 1)
    ):
        fail("Visual scene numbers must be continuous from 1.")

    image_data = read_json(IMAGES)
    image_rows = extract_rows(
        image_data,
        ("scenes", "images", "items"),
        "Image manifest",
    )

    image_ids = [
        scene_identity(row, index, "Image manifest")
        for index, row in enumerate(image_rows, 1)
    ]

    if len(image_rows) != len(jobs):
        fail("Image manifest count differs from visual jobs.")

    if sorted(image_ids) != sorted(identities):
        fail("Image manifest mapping differs from visual jobs.")

    log("Visual jobs and images validated:", len(jobs))
    return identities


def validate_motion(identities, ffprobe, dimensions):
    rows = read_json(MOTION)

    if not isinstance(rows, list) or not rows:
        fail("Photo-motion manifest must be a non-empty list.")

    if len(rows) != len(identities):
        fail("Photo-motion count differs from visual scene count.")

    expected = {
        (part, local): number
        for number, part, local in identities
    }

    seen = set()
    result = {}

    for index, row in enumerate(rows, 1):
        number, part, local = scene_identity(
            row, index, "Motion manifest"
        )
        key = (part, local)

        if key in seen:
            fail(f"Duplicate photo-motion mapping: {key}")

        if expected.get(key) != number:
            fail(f"Photo-motion mapping mismatch at scene {number}.")

        path = safe_path(
            row.get("video"), f"motion video for scene {number}"
        )
        info = probe_media(path, ffprobe)

        if (info["width"], info["height"]) != dimensions:
            fail(f"Wrong resolution in motion scene {number}.")

        try:
            recorded_duration = float(row.get("duration"))
        except (TypeError, ValueError):
            fail(f"Invalid duration in motion scene {number}.")

        if (
            not math.isfinite(recorded_duration)
            or recorded_duration <= 0
            or abs(info["duration"] - recorded_duration) > 1.0
        ):
            fail(f"Duration mismatch in motion scene {number}.")

        seen.add(key)
        result[number] = {
            "part": part,
            "local_scene": local,
            "path": path,
            "info": info,
        }

    if seen != set(expected):
        fail("Photo-motion manifest is missing scenes.")

    log("Photo-motion clips validated:", len(rows))
    return result


def validate_captions():
    data = read_json(CAPTIONS)

    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Captions JSON is missing or incomplete.")

    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        fail("Captions JSON contains no caption records.")

    captions = {}

    for row in rows:
        if not isinstance(row, dict):
            fail("Invalid caption record.")

        key = (
            positive_int(row.get("part"), "caption part"),
            positive_int(row.get("scene"), "caption scene"),
        )
        text = str(row.get("text", "")).strip()

        if not text:
            fail(f"Empty caption for {key}.")

        if key in captions:
            fail(f"Duplicate caption record: {key}")

        captions[key] = text

    return captions


def validate_srt(final_duration):
    require_file(SRT, "Final SRT captions", 10)
    content = SRT.read_text(encoding="utf-8-sig").strip()

    if not content:
        fail("Final SRT is empty.")

    blocks = re.split(r"\n\s*\n", content)
    timestamp = re.compile(
        r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})"
        r"\s*-->\s*"
        r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$"
    )

    previous_start = -1.0
    previous_end = 0.0

    for block in blocks:
        lines = block.strip().splitlines()

        if len(lines) < 3 or not lines[0].strip().isdigit():
            fail("Malformed SRT cue.")

        match = timestamp.match(lines[1].strip())
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
            fail("SRT cues are not chronological.")

        if start < previous_end - 0.05:
            fail("SRT cues overlap unexpectedly.")

        if end > final_duration + 1.0:
            fail("SRT cue extends beyond the final video.")

        if not "\n".join(lines[2:]).strip():
            fail("SRT cue has no text.")

        previous_start = start
        previous_end = end

    log("SRT timestamps validated:", len(blocks))


def validate_render_manifest(
    identities,
    captions,
    motion,
    ffprobe,
    dimensions,
    requested_parts,
    mode,
):
    manifest = read_json(RENDER)

    if not isinstance(manifest, dict):
        fail("Render manifest must be a JSON object.")

    if manifest.get("status") != "completed":
        fail("Render manifest is not completed.")

    if manifest.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions.")

    if manifest.get("format") != mode:
        fail("Render manifest format differs from Input/topic.txt.")

    rows = manifest.get("completed_scenes")

    if not isinstance(rows, list) or not rows:
        fail("Render manifest has no completed scenes.")

    if len(rows) != len(identities):
        fail("Rendered scene count differs from visual scene count.")

    expected = {
        (part, local): number
        for number, part, local in identities
    }

    seen_keys = set()
    seen_numbers = set()
    result = []

    for index, row in enumerate(rows, 1):
        number, part, local = scene_identity(
            row, index, "Render manifest"
        )
        key = (part, local)

        if key in seen_keys or number in seen_numbers:
            fail(f"Duplicate rendered scene mapping: {key}")

        if expected.get(key) != number:
            fail(f"Rendered scene mapping mismatch at scene {number}.")

        if key not in captions:
            fail(f"Missing caption for scene {number}.")

        if row.get("captions_burned") is not True:
            fail(f"Scene {number} is not marked captioned.")

        source_path = safe_path(
            row.get("source_rendered_video"),
            f"pre-caption render for scene {number}",
        )
        source_info = probe_media(source_path, ffprobe)

        if not source_info["has_audio"]:
            fail(f"Pre-caption scene {number} has no audio.")

        recorded_motion = row.get("source_photo_motion_video")
        if recorded_motion:
            motion_path = safe_path(
                recorded_motion,
                f"photo-motion source for scene {number}",
            )
            if motion_path != motion[number]["path"]:
                fail(f"Photo-motion source changed at scene {number}.")

        path = safe_path(
            row.get("video"), f"captioned scene {number}"
        )
        info = probe_media(path, ffprobe)

        if not info["has_audio"]:
            fail(f"Captioned scene {number} has no audio.")

        if (info["width"], info["height"]) != dimensions:
            fail(f"Wrong captioned resolution at scene {number}.")

        if abs(info["duration"] - source_info["duration"]) > 1.0:
            fail(f"Captioning changed scene {number} duration.")

        try:
            recorded_duration = float(row.get("duration"))
        except (TypeError, ValueError):
            fail(f"Invalid recorded duration at scene {number}.")

        if (
            not math.isfinite(recorded_duration)
            or recorded_duration <= 0
            or abs(info["duration"] - recorded_duration) > 1.0
        ):
            fail(f"Manifest duration mismatch at scene {number}.")

        seen_keys.add(key)
        seen_numbers.add(number)
        result.append({
            "number": number,
            "part": part,
            "local_scene": local,
            "path": path,
            "info": info,
        })

    if seen_keys != set(expected):
        fail("Render manifest does not cover all visual scenes.")

    if sorted(seen_numbers) != list(range(1, len(rows) + 1)):
        fail("Rendered global scene numbers are not continuous.")

    result.sort(key=lambda item: item["number"])

    expected_parts = set(range(1, requested_parts + 1))
    actual_parts = {item["part"] for item in result}

    if actual_parts != expected_parts:
        fail("Rendered scenes do not cover configured parts.")

    for part in sorted(expected_parts):
        local_numbers = sorted(
            item["local_scene"]
            for item in result
            if item["part"] == part
        )
        if local_numbers != list(range(1, len(local_numbers) + 1)):
            fail(f"Scene numbering is not continuous in part {part}.")

    log("Captioned scene renders validated:", len(result))
    return manifest, result


def validate_final_video(ffprobe, mode, dimensions, scene_rows):
    final_info = probe_media(FINAL, ffprobe)

    if (final_info["width"], final_info["height"]) != dimensions:
        fail("Final video resolution differs from requested format.")

    if not final_info["has_audio"]:
        fail("Final video has no audio.")

    timeline = sum(item["info"]["duration"] for item in scene_rows)
    tolerance = max(2.0, final_info["duration"] * 0.03)

    if abs(timeline - final_info["duration"]) > tolerance:
        fail(
            f"Scene timeline ({timeline:.2f}s) differs from final "
            f"video ({final_info['duration']:.2f}s)."
        )

    selected = FULL if mode == "full" else SHORT
    opposite = SHORT if mode == "full" else FULL

    alias_info = probe_media(selected, ffprobe)

    if (alias_info["width"], alias_info["height"]) != dimensions:
        fail("Selected output alias has incorrect dimensions.")

    if abs(alias_info["duration"] - final_info["duration"]) > 0.10:
        fail("Selected alias duration differs from final video.")

    if not alias_info["has_audio"]:
        fail("Selected output alias has no audio.")

    if opposite.exists():
        fail(f"Unexpected opposite-format alias exists: {opposite.name}")

    manifest = read_json(RENDER)

    if safe_path(
        manifest.get("final_video"), "render manifest final video"
    ) != FINAL.resolve():
        fail("Render manifest points to a different final video.")

    if safe_path(
        manifest.get("alias"), "render manifest format alias"
    ) != selected.resolve():
        fail("Render manifest points to the wrong format alias.")

    log("Final video and format alias validated.")
    return final_info, selected


def validate_parts(ffprobe, requested_parts, dimensions, manifest):
    if not PARTS_DIR.is_dir():
        fail("Output parts directory is missing.")

    files = sorted(PARTS_DIR.glob("part_*.mp4"))

    if len(files) != requested_parts:
        fail(
            f"Expected {requested_parts} parts; found {len(files)}."
        )

    for number, path in enumerate(files, 1):
        if path.name != f"part_{number:02d}.mp4":
            fail(f"Unexpected part filename: {path.name}")

        info = probe_media(path, ffprobe)

        if (info["width"], info["height"]) != dimensions:
            fail(f"Wrong resolution in {path.name}.")

        if not info["has_audio"]:
            fail(f"Audio missing from {path.name}.")

    expected_paths = [
        (PARTS_DIR / f"part_{number:02d}.mp4")
        .relative_to(ROOT)
        .as_posix()
        for number in range(1, requested_parts + 1)
    ]

    if manifest.get("parts") != expected_paths:
        fail("Render manifest part list differs from part files.")

    log("Part videos validated:", len(files))


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
            expected_size = (
                (1080, 1920) if mode == "short" else (1280, 720)
            )
            if image.size != expected_size:
                fail(
                    f"Thumbnail is {image.width}x{image.height}; "
                    f"expected {expected_size[0]}x{expected_size[1]}."
                )
    except Exception as exc:
        fail(f"Invalid thumbnail: {exc}")

    manifest = read_json(THUMBNAIL_MANIFEST)

    if not isinstance(manifest, dict):
        fail("Thumbnail manifest must be a JSON object.")

    if manifest.get("status") != "completed":
        fail("Thumbnail manifest is not completed.")

    if manifest.get("format") != mode:
        fail("Thumbnail format differs from requested format.")

    expected_path = THUMBNAIL.relative_to(ROOT).as_posix()
    if manifest.get("path") != expected_path:
        fail("Thumbnail manifest points to a different file.")

    log("Metadata and thumbnail validated.")


def main():
    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        fail("ffprobe is not installed.")

    require_file(CONFIG, "Input configuration")
    config = load_and_validate(CONFIG)

    mode = get_format(config)
    requested_parts = positive_int(get_parts(config), "PARTS")
    dimensions = (1280, 720) if mode == "full" else (720, 1280)

    validate_model()
    identities = validate_visuals()
    motion = validate_motion(identities, ffprobe, dimensions)
    captions = validate_captions()

    expected_caption_keys = {
        (part, local) for _, part, local in identities
    }

    if set(captions) != expected_caption_keys:
        fail("Caption records do not match visual scene mapping.")

    manifest, rendered_scenes = validate_render_manifest(
        identities,
        captions,
        motion,
        ffprobe,
        dimensions,
        requested_parts,
        mode,
    )

    final_info, selected = validate_final_video(
        ffprobe,
        mode,
        dimensions,
        rendered_scenes,
    )

    validate_srt(final_info["duration"])
    validate_parts(
        ffprobe,
        requested_parts,
        dimensions,
        manifest,
    )
    validate_metadata_and_thumbnail(mode)

    log("=" * 56)
    log("FINAL OUTPUT VALIDATION: SUCCESS")
    log("FORMAT:", mode)
    log(
        "Resolution:",
        f"{final_info['width']}x{final_info['height']}",
    )
    log("Duration:", f"{final_info['duration']:.2f}s")
    log("Size:", f"{final_info['size'] / 1048576:.2f} MB")
    log("Visual scenes:", len(identities))
    log("Motion scenes:", len(motion))
    log("Captioned scenes:", len(rendered_scenes))
    log("Parts:", requested_parts)
    log("Selected output:", selected.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"FINAL OUTPUT VALIDATION FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
