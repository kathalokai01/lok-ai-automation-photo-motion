#!/usr/bin/env python3
"""Validate Photo Motion pipeline outputs and final deliverables."""

import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"

FINAL_VIDEO = OUTPUT / "videos" / "katha_lok_ai_final.mp4"
SHORT_VIDEO = OUTPUT / "videos" / "katha_lok_ai_short.mp4"
FULL_VIDEO = OUTPUT / "videos" / "katha_lok_ai_full.mp4"

VISUAL_JOBS = OUTPUT / "visuals" / "visual_jobs.json"
IMAGE_MANIFEST = OUTPUT / "visuals" / "image_manifest.json"
MOTION_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
CAPTIONS_JSON = OUTPUT / "captions" / "captions.json"
CAPTIONS_SRT = OUTPUT / "captions" / "final_captions.srt"
METADATA = OUTPUT / "youtube_metadata.txt"
MODEL_FILE = OUTPUT / "config" / "selected_model.json"
CONFIG_FILE = ROOT / "Input" / "topic.txt"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def fail(message):
    raise RuntimeError(message)


def require_nonempty(path, description):
    if not path.is_file() or path.stat().st_size == 0:
        fail(f"{description} is missing or empty: {path}")


def read_json(path):
    require_nonempty(path, "JSON file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"Cannot parse {path}: {exc}")


def safe_repo_path(value, description):
    if not isinstance(value, str) or not value.strip():
        fail(f"Missing path for {description}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        fail(f"{description} path escapes the repository: {value}")

    return path


def positive_int(value, description):
    try:
        number = int(value)
    except (TypeError, ValueError):
        fail(f"Invalid {description}: {value!r}")

    if number < 1:
        fail(f"{description} must be positive.")

    return number


def scene_ids(row, index):
    if not isinstance(row, dict):
        fail(f"Scene entry {index} is not an object.")

    global_scene = positive_int(
        row.get("global_scene", row.get("scene", index)),
        "global scene number",
    )
    part = positive_int(row.get("part", 1), "part number")
    local_scene = positive_int(
        row.get("local_scene", row.get("scene", index)),
        "local scene number",
    )

    return global_scene, part, local_scene


def extract_jobs(data):
    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = next(
            (
                data.get(key)
                for key in ("jobs", "visual_jobs", "scenes")
                if isinstance(data.get(key), list)
            ),
            None,
        )
    else:
        jobs = None

    if not isinstance(jobs, list) or not jobs:
        fail("visual_jobs.json contains no scene jobs.")

    return jobs


def load_format():
    require_nonempty(CONFIG_FILE, "Input configuration")
    config = {}

    for raw in CONFIG_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip().strip("'\"")
        config[key.strip().upper()] = value

    value = config.get("FORMAT", "short").strip().lower()

    if value in {"full", "long", "landscape", "youtube",
                 "youtube_full", "youtube-long"}:
        return "full"

    if value in {"short", "shorts", "vertical", "reels",
                 "reel", "youtube_short", "youtube-shorts"}:
        return "short"

    fail(f"Unsupported FORMAT in Input/topic.txt: {value!r}")


def probe_video(path, ffprobe):
    require_nonempty(path, "Video")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        fail(f"ffprobe could not read {path.name}: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media information in {path.name}: {exc}")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"{path.name} has an invalid duration.")

    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]
    if not video_streams:
        fail(f"{path.name} has no video stream.")

    stream = video_streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)

    if width <= 0 or height <= 0:
        fail(f"{path.name} has invalid dimensions.")

    return {
        "duration": duration,
        "size_bytes": path.stat().st_size,
        "width": width,
        "height": height,
        "video_codec": stream.get("codec_name", "unknown"),
        "has_audio": any(
            item.get("codec_type") == "audio"
            for item in streams
        ),
    }


def validate_image_manifest(data, jobs):
    if not isinstance(data, dict):
        fail("image_manifest.json must be a JSON object.")

    rows = data.get("scenes")
    if not isinstance(rows, list) or len(rows) != len(jobs):
        fail("Image manifest scene count does not match visual jobs.")

    job_numbers = []
    for index, job in enumerate(jobs, 1):
        number, _, _ = scene_ids(job, index)
        job_numbers.append(number)

    manifest_numbers = []
    for index, row in enumerate(rows, 1):
        number, _, _ = scene_ids(row, index)
        manifest_numbers.append(number)

    if job_numbers != manifest_numbers:
        fail("Image manifest scene numbering differs from visual jobs.")

    return len(rows)


def validate_images(expected_count):
    visual_dir = OUTPUT / "visuals"
    if not visual_dir.is_dir():
        fail("Visuals directory is missing.")

    jobs = extract_jobs(read_json(VISUAL_JOBS))
    images_by_scene = {}

    for index, job in enumerate(jobs, 1):
        number, _, _ = scene_ids(job, index)
        image_value = job.get("image_path") or job.get("image")

        image_path = safe_repo_path(
            image_value,
            f"image for scene {number}",
        )
        require_nonempty(image_path, f"Image for scene {number}")

        if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            fail(f"Unsupported image extension: {image_path.name}")

        if image_path.stat().st_size < 1000:
            fail(f"Scene image is suspiciously small: {image_path.name}")

        if number in images_by_scene:
            fail(f"Duplicate image job for scene {number}.")

        images_by_scene[number] = image_path

    if len(images_by_scene) != expected_count:
        fail("Scene image count does not match visual jobs.")

    return len(images_by_scene)


def validate_motion_manifest(data, jobs, ffprobe):
    if not isinstance(data, list) or not data:
        fail("Photo Motion manifest must be a non-empty JSON list.")

    job_map = {}
    for index, job in enumerate(jobs, 1):
        number, part, local = scene_ids(job, index)
        key = (part, local)

        if key in job_map:
            fail(f"Duplicate visual scene key: {key}")

        job_map[key] = number

    motion_map = {}
    total_duration = 0.0
    numbers = []

    for index, row in enumerate(data, 1):
        number, part, local = scene_ids(row, index)
        key = (part, local)

        if key in motion_map:
            fail(f"Duplicate motion scene key: {key}")

        if key not in job_map:
            fail(f"Motion clip has no matching visual job: {key}")

        if job_map[key] != number:
            fail(f"Scene numbering mismatch for Part {part}, Scene {local}.")

        try:
            duration = float(row["duration"])
        except (KeyError, TypeError, ValueError):
            fail(f"Invalid duration for motion scene {number}.")

        if not math.isfinite(duration) or duration <= 0:
            fail(f"Non-positive duration for motion scene {number}.")

        video_path = safe_repo_path(
            row.get("video"),
            f"motion video for scene {number}",
        )
        info = probe_video(video_path, ffprobe)

        tolerance = max(1.0, 2.0 / max(int(row.get("fps", 24)), 1))
        if abs(info["duration"] - duration) > tolerance:
            fail(
                f"Manifest duration differs from clip for scene {number}: "
                f"manifest={duration:.2f}s, clip={info['duration']:.2f}s."
            )

        expected_size = (
            (1280, 720)
            if str(row.get("format", "")).lower() == "full"
            else (720, 1280)
        )
        if (info["width"], info["height"]) != expected_size:
            fail(
                f"Wrong dimensions for scene {number}: "
                f"{info['width']}x{info['height']}."
            )

        numbers.append(number)
        total_duration += duration
        motion_map[key] = row

    if numbers != list(range(1, len(data) + 1)):
        fail(f"Motion scene numbers are not continuous: {numbers}")

    if set(motion_map) != set(job_map):
        fail("Motion clips and visual jobs do not contain the same scenes.")

    if len(data) != len(jobs):
        fail(
            f"Scene count mismatch: jobs={len(jobs)}, clips={len(data)}."
        )

    return total_duration


def validate_captions(jobs):
    data = read_json(CAPTIONS_JSON)

    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Captions JSON is not marked completed.")

    captions = data.get("captions")
    if not isinstance(captions, list) or not captions:
        fail("Captions JSON contains no captions.")

    caption_map = {}
    for row in captions:
        if not isinstance(row, dict):
            fail("Invalid caption entry.")

        part = positive_int(row.get("part"), "caption part")
        scene = positive_int(row.get("scene"), "caption scene")
        text = str(row.get("text", "")).strip()

        if not text:
            fail(f"Empty caption at Part {part}, Scene {scene}.")

        key = (part, scene)
        if key in caption_map:
            fail(f"Duplicate caption: {key}")

        caption_map[key] = text

    expected = set()
    for index, job in enumerate(jobs, 1):
        _, part, local = scene_ids(job, index)
        expected.add((part, local))

    if set(caption_map) != expected:
        fail(
            "Caption scene keys do not match visual jobs. "
            f"Missing={sorted(expected - set(caption_map))}; "
            f"extra={sorted(set(caption_map) - expected)}"
        )

    require_nonempty(CAPTIONS_SRT, "Subtitle SRT")
    srt_text = CAPTIONS_SRT.read_text(encoding="utf-8-sig").strip()

    if "-->" not in srt_text:
        fail("SRT has no subtitle timing intervals.")

    cue_count = len(re.findall(r"(?m)^\d+\s*$", srt_text))
    if cue_count != len(caption_map):
        fail(
            f"Caption/SRT count mismatch: JSON={len(caption_map)}, "
            f"SRT={cue_count}."
        )

    return len(caption_map), data.get("mode", "unknown")


def validate_metadata():
    require_nonempty(METADATA, "YouTube metadata")
    content = METADATA.read_text(encoding="utf-8").strip()

    if len(content) < 50:
        fail("YouTube metadata is suspiciously short.")

    for field in ("Title:", "Description:", "Tags:"):
        if field.lower() not in content.lower():
            fail(f"YouTube metadata is missing the {field} section.")

    return len(content)


def validate_parts(ffprobe):
    part_dir = OUTPUT / "parts"
    parts = sorted(part_dir.glob("part_*.mp4")) if part_dir.is_dir() else []

    if not parts:
        fail("No rendered part MP4 files were found.")

    for path in parts:
        probe_video(path, ffprobe)

    return len(parts)


def validate_narration_audio(final_info):
    narration_dir = OUTPUT / "narration"
    if not narration_dir.is_dir():
        return 0

    audio_files = [
        path for path in narration_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and path.name.lower() not in {
            "combined_narration.m4a",
            "audio_concat.txt",
        }
    ]

    if audio_files and not final_info["has_audio"]:
        fail("Narration audio exists, but the final MP4 has no audio stream.")

    for path in audio_files:
        if path.stat().st_size == 0:
            fail(f"Empty narration audio file: {path.name}")

    return len(audio_files)


def compare_videos(final_info, alias_info, alias_path):
    if (final_info["width"], final_info["height"]) != (
        alias_info["width"], alias_info["height"]
    ):
        fail(f"Format alias dimensions differ from final video: {alias_path.name}")

    if abs(final_info["duration"] - alias_info["duration"]) > 0.10:
        fail(f"Format alias duration differs from final video: {alias_path.name}")

    if final_info["has_audio"] != alias_info["has_audio"]:
        fail(f"Format alias audio presence differs: {alias_path.name}")

    # If the files are byte-for-byte copies, this also confirms they are identical.
    if final_info["size_bytes"] == alias_info["size_bytes"]:
        digest_a = hashlib.sha256(FINAL_VIDEO.read_bytes()).hexdigest()
        digest_b = hashlib.sha256(alias_path.read_bytes()).hexdigest()
        if digest_a != digest_b:
            print(
                "NOTICE: Final video and alias have equal size but different "
                "contents; duration, dimensions and audio were checked."
            )


def main():
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        fail("ffprobe is not installed.")

    require_nonempty(MODEL_FILE, "Selected Gemini model file")
    model_data = read_json(MODEL_FILE)

    if (
        not isinstance(model_data, dict)
        or model_data.get("status") != "selected"
        or not str(model_data.get("model", "")).strip()
    ):
        fail("Selected Gemini model file is invalid.")

    mode = load_format()
    expected_dimensions = (1280, 720) if mode == "full" else (720, 1280)

    jobs = extract_jobs(read_json(VISUAL_JOBS))
    validate_image_manifest(read_json(IMAGE_MANIFEST), jobs)
    image_count = validate_images(len(jobs))

    motion_duration = validate_motion_manifest(
        read_json(MOTION_MANIFEST), jobs, ffprobe
    )

    final_info = probe_video(FINAL_VIDEO, ffprobe)
    if (final_info["width"], final_info["height"]) != expected_dimensions:
        fail(
            f"Final video dimensions are {final_info['width']}x"
            f"{final_info['height']}; expected "
            f"{expected_dimensions[0]}x{expected_dimensions[1]} for {mode}."
        )

    tolerance = max(2.0, final_info["duration"] * 0.03)
    if abs(motion_duration - final_info["duration"]) > tolerance:
        fail(
            "Scene timeline and final video duration differ too much: "
            f"scenes={motion_duration:.2f}s, "
            f"video={final_info['duration']:.2f}s."
        )

    alias_path = FULL_VIDEO if mode == "full" else SHORT_VIDEO
    alias_info = probe_video(alias_path, ffprobe)
    compare_videos(final_info, alias_info, alias_path)

    caption_count, caption_mode = validate_captions(jobs)
    metadata_length = validate_metadata()
    part_count = validate_parts(ffprobe)
    narration_count = validate_narration_audio(final_info)

    print("=" * 60)
    print("FINAL OUTPUT VALIDATION: SUCCESS")
    print("Selected Gemini model:", model_data["model"])
    print("Format:", mode)
    print("Visual jobs:", len(jobs))
    print("Scene images:", image_count)
    print("Motion clips:", len(read_json(MOTION_MANIFEST)))
    print("Scene timeline:", f"{motion_duration:.2f}s")
    print("Final video:", FINAL_VIDEO.relative_to(ROOT))
    print("Video duration:", f"{final_info['duration']:.2f}s")
    print("Resolution:", f"{final_info['width']}x{final_info['height']}")
    print("Video codec:", final_info["video_codec"])
    print("Audio stream:", "present" if final_info["has_audio"] else "not present")
    print("Narration audio files:", narration_count)
    print("Captions:", caption_count, f"({caption_mode})")
    print("Parts:", part_count)
    print("Metadata characters:", metadata_length)
    print("Format alias:", alias_path.relative_to(ROOT))
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FINAL OUTPUT VALIDATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)