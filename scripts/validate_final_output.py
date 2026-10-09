#!/usr/bin/env python3
"""Validate the generated Photo Motion video and its required deliverables."""

import json
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

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def fail(message):
    raise RuntimeError(message)


def read_json(path):
    if not path.is_file() or path.stat().st_size == 0:
        fail(f"Required JSON file is missing or empty: {path.relative_to(ROOT)}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"Cannot parse {path.relative_to(ROOT)}: {exc}")


def require_nonempty(path, description):
    if not path.is_file() or path.stat().st_size == 0:
        fail(f"{description} is missing or empty: {path.relative_to(ROOT)}")


def probe_video(path, ffprobe):
    require_nonempty(path, "Video")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
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
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid video metadata in {path.name}: {exc}")

    streams = data.get("streams", [])
    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]

    if not video_streams:
        fail(f"{path.name} has no video stream.")

    stream = video_streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)

    if duration <= 0 or width <= 0 or height <= 0:
        fail(f"{path.name} has invalid duration or dimensions.")

    return {
        "duration": duration,
        "size_bytes": path.stat().st_size,
        "width": width,
        "height": height,
        "has_audio": any(
            item.get("codec_type") == "audio" for item in streams
        ),
    }


def extract_jobs(data):
    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = data.get("jobs") or data.get("visual_jobs") or data.get("scenes")
    else:
        jobs = None

    if not isinstance(jobs, list) or not jobs:
        fail("visual_jobs.json has no scene jobs.")

    return jobs


def validate_motion_manifest(data, expected_count):
    if not isinstance(data, list) or not data:
        fail("Photo Motion manifest must be a non-empty JSON list.")

    numbers = []
    total_duration = 0.0

    for row in data:
        if not isinstance(row, dict):
            fail("Photo Motion manifest contains an invalid entry.")

        try:
            number = int(row.get("global_scene", row.get("scene")))
            duration = float(row["duration"])
        except (TypeError, ValueError, KeyError) as exc:
            fail(f"Invalid Photo Motion scene entry: {row} ({exc})")

        if number < 1 or duration <= 0:
            fail(f"Invalid scene number or duration: {row}")

        numbers.append(number)
        total_duration += duration

        video_value = row.get("video")
        if not video_value:
            fail(f"Scene {number} has no video path in the manifest.")

        video_path = (ROOT / str(video_value)).resolve()

        if not video_path.is_relative_to(ROOT):
            fail(f"Scene {number} video path escapes the repository.")

        require_nonempty(video_path, f"Scene {number} video")

    expected_numbers = list(range(1, len(data) + 1))
    if numbers != expected_numbers:
        fail(
            "Photo Motion scene numbers are not continuous from 1. "
            f"Found {numbers}"
        )

    if len(data) != expected_count:
        fail(
            f"Scene count mismatch: visual jobs={expected_count}, "
            f"motion clips={len(data)}."
        )

    return total_duration


def validate_captions():
    data = read_json(CAPTIONS_JSON)

    if not isinstance(data, dict) or data.get("status") != "completed":
        fail("Captions JSON is not marked completed.")

    captions = data.get("captions")
    if not isinstance(captions, list) or not captions:
        fail("Captions JSON contains no captions.")

    seen = set()

    for row in captions:
        if not isinstance(row, dict):
            fail("Captions JSON contains an invalid caption entry.")

        text = str(row.get("text", "")).strip()
        if not text:
            fail("Captions contain an empty text entry.")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            fail("A caption is missing numeric part/scene identifiers.")

        if key[0] < 1 or key[1] < 1 or key in seen:
            fail(f"Invalid or duplicate caption identifiers: {key}")

        seen.add(key)

    require_nonempty(CAPTIONS_SRT, "Generated subtitle SRT")
    srt_text = CAPTIONS_SRT.read_text(encoding="utf-8").strip()

    cue_count = len(re.findall(r"(?m)^\d+\s*$", srt_text))
    if cue_count != len(captions):
        fail(
            f"Caption/SRT count mismatch: JSON={len(captions)}, "
            f"SRT cues={cue_count}."
        )

    if "-->" not in srt_text:
        fail("SRT contains no subtitle timing intervals.")

    return len(captions), data.get("mode", "unknown")


def validate_images(expected_count):
    if not OUTPUT.joinpath("visuals").is_dir():
        fail("Visuals directory is missing.")

    images = [
        path for path in OUTPUT.joinpath("visuals").rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]

    if len(images) < expected_count:
        fail(
            f"Not enough generated scene images: expected at least "
            f"{expected_count}, found {len(images)}."
        )

    for path in images:
        if path.stat().st_size == 0:
            fail(f"An image file is empty: {path.relative_to(ROOT)}")

    return len(images)


def main():
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        fail("ffprobe is not installed.")

    require_nonempty(MODEL_FILE, "Selected Gemini model file")
    model_data = read_json(MODEL_FILE)

    if model_data.get("status") != "selected" or not model_data.get("model"):
        fail("Selected Gemini model file is invalid.")

    jobs_data = read_json(VISUAL_JOBS)
    jobs = extract_jobs(jobs_data)

    if not IMAGE_MANIFEST.is_file():
        fail("Image manifest is missing.")

    image_data = read_json(IMAGE_MANIFEST)
    if not isinstance(image_data, dict) or not isinstance(
        image_data.get("scenes"), list
    ):
        fail("Image manifest has an unexpected structure.")

    if len(image_data["scenes"]) != len(jobs):
        fail(
            f"Image manifest/job count mismatch: "
            f"{len(image_data['scenes'])} vs {len(jobs)}."
        )

    image_count = validate_images(len(jobs))

    motion_data = read_json(MOTION_MANIFEST)
    motion_duration = validate_motion_manifest(motion_data, len(jobs))

    video_info = probe_video(FINAL_VIDEO, ffprobe)

    # The scene timeline and final video should be reasonably aligned.
    tolerance = max(2.0, video_info["duration"] * 0.03)
    if abs(motion_duration - video_info["duration"]) > tolerance:
        fail(
            "Scene timeline and final MP4 duration differ too much: "
            f"scenes={motion_duration:.2f}s, "
            f"video={video_info['duration']:.2f}s."
        )

    # Require the appropriate format alias if it exists as a render target.
    config_path = ROOT / "Input" / "topic.txt"
    config_text = config_path.read_text(encoding="utf-8-sig").lower()
    full_mode = any(
        re.search(rf"(?m)^\s*format\s*=\s*['\"]?{mode}\b", config_text)
        for mode in ("full", "long", "landscape", "youtube")
    )
    alias = FULL_VIDEO if full_mode else SHORT_VIDEO
    require_nonempty(alias, "Format-specific video output")
    alias_info = probe_video(alias, ffprobe)

    if abs(alias_info["duration"] - video_info["duration"]) > tolerance:
        fail("Format-specific video duration does not match final MP4.")

    caption_count, caption_mode = validate_captions()

    require_nonempty(METADATA, "YouTube metadata")
    metadata_text = METADATA.read_text(encoding="utf-8").strip()

    for field in ("Title:", "Description:", "Tags:"):
        if field not in metadata_text:
            fail(f"YouTube metadata is missing the {field} section.")

    if len(metadata_text) < 50:
        fail("YouTube metadata is suspiciously short.")

    part_files = sorted((OUTPUT / "parts").glob("part_*.mp4"))
    if not part_files:
        fail("No rendered part MP4 files were found.")

    for path in part_files:
        probe_video(path, ffprobe)

    narration_dir = OUTPUT / "narration"
    narration_files = [
        path for path in narration_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and path.name.lower() not in {
            "combined_narration.m4a",
            "audio_concat.txt",
        }
    ] if narration_dir.is_dir() else []

    if narration_files and not video_info["has_audio"]:
        fail(
            "Narration files exist, but the final MP4 has no audio stream."
        )

    print("=" * 60)
    print("FINAL OUTPUT VALIDATION: SUCCESS")
    print("Selected Gemini model:", model_data["model"])
    print("Visual jobs:", len(jobs))
    print("Scene images:", image_count)
    print("Motion clips:", len(motion_data))
    print("Scene timeline:", f"{motion_duration:.2f}s")
    print("Final video:", FINAL_VIDEO)
    print("Video duration:", f"{video_info['duration']:.2f}s")
    print("Resolution:", f"{video_info['width']}x{video_info['height']}")
    print("Audio stream:", "present" if video_info["has_audio"] else "not present")
    print("Captions:", caption_count, f"({caption_mode})")
    print("Parts:", len(part_files))
    print("Metadata:", METADATA)
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FINAL OUTPUT VALIDATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)