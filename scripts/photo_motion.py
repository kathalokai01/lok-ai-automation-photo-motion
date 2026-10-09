#!/usr/bin/env python3
"""Katha Lok AI Photo Motion engine using globally numbered visual jobs."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format

VISUALS = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS / "visual_jobs.json"
OUT = ROOT / "output" / "photo_motion"

FPS = 24
CRF = 20
PRESET = "medium"
EFFECTS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "subtle"]


def run(cmd):
    print("\n$", " ".join(map(str, cmd)), flush=True)
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout, flush=True)
    if result.returncode:
        raise RuntimeError(f"Command failed with exit code {result.returncode}")


def load_jobs():
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            f"Visual jobs file missing: {JOBS_FILE}. "
            "Run scripts/generate_visuals.py first."
        )

    data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))

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
        raise RuntimeError("visual_jobs.json contains no jobs.")

    return jobs


def safe_repo_path(value, label):
    if not value:
        raise RuntimeError(f"Scene job is missing {label}.")

    path = (ROOT / str(value)).resolve()

    if not path.is_relative_to(ROOT):
        raise RuntimeError(f"Unsafe path outside repository for {label}: {value}")

    return path


def get_scene_number(job, index):
    value = job.get("global_scene", job.get("scene", index))
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"Invalid global scene number: {value}")

    if number < 1:
        raise RuntimeError(f"Scene number must be positive: {number}")

    return number


def get_duration(job):
    for key in ("duration", "scene_duration", "duration_seconds", "seconds"):
        try:
            value = float(job.get(key))
            if 0 < value <= 3600:
                return value
        except (TypeError, ValueError):
            pass

    return 5.0


def make_filter(effect, frames, width, height):
    denom = max(frames - 1, 1)

    if effect == "zoom_in":
        zoom = f"min(1+0.10*on/{denom},1.10)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif effect == "zoom_out":
        zoom = f"max(1.10-0.10*on/{denom},1.0)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif effect == "pan_left":
        zoom = "1.06"
        x = f"(iw-iw/zoom)*(1-on/{denom})"
        y = "(ih-ih/zoom)/2"
    elif effect == "pan_right":
        zoom = "1.06"
        x = f"(iw-iw/zoom)*on/{denom}"
        y = "(ih-ih/zoom)/2"
    else:
        zoom = f"1+0.025*sin(on/{max(frames, 1)}*PI)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    return (
        f"scale={width * 2}:{height * 2}:"
        "force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':"
        f"d=1:s={width}x{height}:fps={FPS},"
        "format=yuv420p"
    )


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    width, height = (1280, 720) if mode == "full" else (720, 1280)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found.")

    jobs = load_jobs()
    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Invalid visual job at position {index}")

        number = get_scene_number(job, index)

        if number in seen:
            raise RuntimeError(
                f"Duplicate global scene number {number} in visual_jobs.json"
            )
        seen.add(number)

        image = safe_repo_path(
            job.get("image_path") or job.get("image"),
            f"image_path for scene {number}",
        )

        if not image.is_file() or image.stat().st_size < 5000:
            raise RuntimeError(
                f"Scene {number} image missing or too small: {image}"
            )

        output_value = job.get("output_video")
        target = (
            safe_repo_path(output_value, f"output_video for scene {number}")
            if output_value
            else OUT / f"scene_{number:02d}.mp4"
        )

        if target.suffix.lower() != ".mp4":
            raise RuntimeError(f"Scene {number} output must be an MP4: {target}")

        prepared.append({
            "number": number,
            "image": image,
            "duration": get_duration(job),
            "target": target,
        })

    # Ensure deterministic order and prevent stale clips from entering the render.
    prepared.sort(key=lambda item: item["number"])
    OUT.mkdir(parents=True, exist_ok=True)

    for old in OUT.glob("scene_*.mp4"):
        old.unlink()

    manifest = []

    for index, item in enumerate(prepared):
        number = item["number"]
        image = item["image"]
        seconds = item["duration"]
        target = item["target"]
        frames = max(1, round(seconds * FPS))
        effect = EFFECTS[index % len(EFFECTS)]

        target.parent.mkdir(parents=True, exist_ok=True)

        print(
            f"\nScene {number}: {image.name}; "
            f"{seconds:.2f}s; {effect}; FORMAT={mode}; "
            f"output={target.relative_to(ROOT)}",
            flush=True,
        )

        run([
            ffmpeg, "-y",
            "-loop", "1",
            "-i", str(image),
            "-vf", make_filter(effect, frames, width, height),
            "-frames:v", str(frames),
            "-an",
            "-c:v", "libx264",
            "-preset", PRESET,
            "-crf", str(CRF),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(target),
        ])

        if not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError(f"Scene {number} video missing or empty: {target}")

        manifest.append({
            "scene": number,
            "image": str(image.relative_to(ROOT)),
            "video": str(target.relative_to(ROOT)),
            "duration": seconds,
            "effect": effect,
            "width": width,
            "height": height,
            "fps": FPS,
            "format": mode,
        })

    manifest_path = OUT / "photo_motion_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=" * 60)
    print("PHOTO MOTION SUCCESS")
    print("Format:", mode)
    print("Resolution:", f"{width}x{height}")
    print("Expected scenes:", len(jobs))
    print("Generated clips:", len(manifest))
    print("Manifest:", manifest_path)

    if len(manifest) != len(jobs):
        raise RuntimeError(
            f"Scene count mismatch: expected {len(jobs)}, generated {len(manifest)}"
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print("PHOTO MOTION FAILED:", exc, file=sys.stderr)
        sys.exit(1)

Commit message:

"Fix global scene mapping in photo motion generation"