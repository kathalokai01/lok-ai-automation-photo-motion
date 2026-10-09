#!/usr/bin/env python3
"""Generate validated Photo Motion clips from scene images."""

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_fps

VISUALS = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS / "visual_jobs.json"
OUTPUT_DIR = ROOT / "output" / "photo_motion"
MANIFEST_FILE = OUTPUT_DIR / "photo_motion_manifest.json"

CRF = 20
PRESET = "medium"

EFFECTS = [
    "zoom_in",
    "pan_left",
    "zoom_out",
    "pan_right",
    "subtle",
]


def log(*items):
    print(*items, flush=True)


def run_command(command):
    log("\n$", " ".join(map(str, command)))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.stdout:
        log(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}."
        )


def read_jobs():
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            "visual_jobs.json is missing. "
            "Run scripts/generate_visuals.py first."
        )

    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read visual jobs: {exc}"
        ) from exc

    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = (
            data.get("jobs")
            or data.get("visual_jobs")
            or data.get("scenes")
            or []
        )
    else:
        jobs = []

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "visual_jobs.json contains no valid scene jobs."
        )

    return jobs


def safe_path(value, label):
    if not value:
        raise RuntimeError(f"Missing {label}.")

    path = (ROOT / str(value)).resolve()

    if not path.is_relative_to(ROOT):
        raise RuntimeError(
            f"{label} must remain inside the repository: {value}"
        )

    return path


def scene_number(job, index):
    value = job.get("global_scene", job.get("scene", index))

    if isinstance(value, dict):
        value = value.get("global_scene", value.get("number"))

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid scene number in job {index}: {value!r}"
        ) from exc

    if number < 1:
        raise RuntimeError(
            f"Scene number must be positive: {number}"
        )

    return number


def scene_duration(job):
    for key in (
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds",
    ):
        try:
            value = float(job.get(key))

            if math.isfinite(value) and 0.5 <= value <= 3600:
                return value
        except (TypeError, ValueError):
            continue

    raise RuntimeError(
        "Every scene needs a valid positive duration."
    )


def prepare_jobs(jobs):
    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(
                f"Scene job {index} is not a JSON object."
            )

        number = scene_number(job, index)

        if number in seen:
            raise RuntimeError(
                f"Duplicate global scene number: {number}"
            )

        seen.add(number)

        image_value = job.get("image_path") or job.get("image")
        image = safe_path(
            image_value,
            f"image path for scene {number}",
        )

        if not image.is_file() or image.stat().st_size < 1000:
            raise RuntimeError(
                f"Scene {number} image is missing or too small: {image}"
            )

        # Validate the actual image rather than only its filename.
        try:
            from PIL import Image

            with Image.open(image) as source:
                source.verify()

            with Image.open(image) as source:
                if source.width < 64 or source.height < 64:
                    raise RuntimeError(
                        f"Scene {number} image dimensions are too small."
                    )
        except Exception as exc:
            raise RuntimeError(
                f"Scene {number} image is invalid: {image}: {exc}"
            ) from exc

        prepared.append({
            "number": number,
            "part": job.get("part", 1),
            "local_scene": job.get("local_scene", number),
            "image": image,
            "duration": scene_duration(job),
            "target": OUTPUT_DIR / f"scene_{number:04d}.mp4",
        })

    prepared.sort(key=lambda item: item["number"])

    actual = [item["number"] for item in prepared]
    expected = list(range(1, len(prepared) + 1))

    if actual != expected:
        raise RuntimeError(
            "Global scene numbering must be continuous from 1. "
            f"Found: {actual}"
        )

    return prepared


def make_filter(effect, frames, width, height, fps):
    """Create a smooth pan/zoom filter for a still image."""

    denominator = max(frames - 1, 1)
    source_width = width * 2
    source_height = height * 2

    if effect == "zoom_in":
        zoom = f"min(1+0.10*on/{denominator},1.10)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    elif effect == "zoom_out":
        zoom = f"max(1.10-0.10*on/{denominator},1.0)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    elif effect == "pan_left":
        zoom = "1.06"
        x = f"(iw-iw/zoom)*(1-on/{denominator})"
        y = "(ih-ih/zoom)/2"

    elif effect == "pan_right":
        zoom = "1.06"
        x = f"(iw-iw/zoom)*on/{denominator}"
        y = "(ih-ih/zoom)/2"

    else:
        zoom = f"1+0.015*sin(on/{max(frames, 1)}*PI)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    return (
        f"scale={source_width}:{source_height}:"
        "force_original_aspect_ratio=increase,"
        f"crop={source_width}:{source_height},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':"
        f"d=1:s={width}x{height}:fps={fps},"
        "setsar=1,format=yuv420p"
    )


def probe_clip(path, ffprobe, width, height, expected_duration):
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(
            f"Scene video is missing or empty: {path}"
        )

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height:format=duration,size",
            "-of", "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        actual_duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid ffprobe output for {path.name}: {exc}"
        ) from exc

    streams = data.get("streams", [])
    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]

    if not video_streams:
        raise RuntimeError(
            f"{path.name} has no video stream."
        )

    stream = video_streams[0]

    if (
        int(stream.get("width", 0)) != width
        or int(stream.get("height", 0)) != height
    ):
        raise RuntimeError(
            f"Wrong resolution for {path.name}: "
            f"{stream.get('width')}x{stream.get('height')}"
        )

    if abs(actual_duration - expected_duration) > 1.0:
        raise RuntimeError(
            f"Unexpected duration for {path.name}: "
            f"{actual_duration:.3f}s; expected about "
            f"{expected_duration:.3f}s"
        )

    return actual_duration


def write_manifest(entries):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    temporary = MANIFEST_FILE.with_suffix(".json.tmp")

    temporary.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    temporary.replace(MANIFEST_FILE)


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    fps = get_fps(config)

    if not 1 <= fps <= 60:
        raise RuntimeError(
            f"FPS must be between 1 and 60 for this workflow; got {fps}."
        )

    if mode == "full":
        width, height = 1280, 720
    else:
        width, height = 720, 1280

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed.")

    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    jobs = read_jobs()
    prepared = prepare_jobs(jobs)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Remove only this renderer's old outputs.
    for pattern in ("scene_*.mp4",):
        for old_file in OUTPUT_DIR.glob(pattern):
            if old_file.is_file():
                old_file.unlink()

    manifest = []

    for index, item in enumerate(prepared):
        number = item["number"]
        image = item["image"]
        seconds = item["duration"]
        target = item["target"]

        frames = max(1, round(seconds * fps))
        actual_duration = frames / fps
        effect = EFFECTS[index % len(EFFECTS)]

        log("\n" + "=" * 55)
        log(f"Scene: {number}/{len(prepared)}")
        log(f"Image: {image.relative_to(ROOT)}")
        log(f"Effect: {effect}")
        log(f"Duration: {actual_duration:.3f}s")
        log(f"FPS: {fps}")
        log(f"Resolution: {width}x{height}")
        log(f"Output: {target.relative_to(ROOT)}")

        video_filter = make_filter(
            effect,
            frames,
            width,
            height,
            fps,
        )

        run_command([
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-loop", "1",
            "-framerate", str(fps),
            "-i", str(image),
            "-vf", video_filter,
            "-frames:v", str(frames),
            "-an",
            "-c:v", "libx264",
            "-preset", PRESET,
            "-crf", str(CRF),
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-movflags", "+faststart",
            str(target),
        ])

        actual = probe_clip(
            target,
            ffprobe,
            width,
            height,
            actual_duration,
        )

        manifest.append({
            "scene": number,
            "global_scene": number,
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image": str(image.relative_to(ROOT)),
            "video": str(target.relative_to(ROOT)),
            "duration": actual,
            "requested_duration": seconds,
            "frames": frames,
            "effect": effect,
            "width": width,
            "height": height,
            "fps": fps,
            "format": mode,
            "size_bytes": target.stat().st_size,
        })

        # Save progress after every scene, so completed clips
        # remain represented in the manifest if a later scene fails.
        write_manifest(manifest)

    if len(manifest) != len(jobs):
        raise RuntimeError(
            f"Clip count mismatch: jobs={len(jobs)}, "
            f"clips={len(manifest)}"
        )

    log("\n" + "=" * 55)
    log("PHOTO MOTION GENERATION SUCCESS")
    log("Format:", mode)
    log("Resolution:", f"{width}x{height}")
    log("FPS:", fps)
    log("Scenes:", len(manifest))
    log(
        "Total duration:",
        f"{sum(row['duration'] for row in manifest):.2f}s",
    )
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(
            f"PHOTO MOTION FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)