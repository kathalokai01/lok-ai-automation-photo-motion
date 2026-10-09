
#!/usr/bin/env python3
"""Katha Lok AI Photo Motion renderer with strict global scene mapping."""

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

EFFECTS = [
    "zoom_in",
    "pan_left",
    "zoom_out",
    "pan_right",
    "subtle",
]


def run(cmd):
    print("\n$", " ".join(map(str, cmd)), flush=True)
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout, flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}"
        )


def load_jobs():
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            f"Missing visual jobs file: {JOBS_FILE}. "
            "Run scripts/generate_visuals.py first."
        )

    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read visual_jobs.json: {exc}") from exc

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
        raise RuntimeError("visual_jobs.json contains no scene jobs.")

    return jobs


def safe_repo_path(value, label):
    if not value:
        raise RuntimeError(f"Scene job is missing {label}.")

    path = (ROOT / str(value)).resolve()

    if not path.is_relative_to(ROOT):
        raise RuntimeError(
            f"Path must stay inside the repository for {label}: {value}"
        )

    return path


def get_scene_number(job, index):
    value = job.get("global_scene", job.get("scene", index))

    if isinstance(value, dict):
        value = value.get("global_scene", value.get("number"))

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid global scene number at job {index}: {value!r}"
        ) from exc

    if number < 1:
        raise RuntimeError(
            f"Scene number must be positive; received {number}"
        )

    return number


def get_duration(job):
    for key in (
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds",
    ):
        value = job.get(key)

        if value is None:
            continue

        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue

        if 0 < seconds <= 3600:
            return seconds

    raise RuntimeError(
        "Scene duration is missing or invalid. "
        "Provide a positive duration in the visual job."
    )


def make_filter(effect, frames, width, height):
    """Create a smooth, deterministic zoom/pan filter."""
    denom = max(frames - 1, 1)
    source_width = width * 2
    source_height = height * 2

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
        zoom = f"1+0.02*sin(on/{max(frames, 1)}*PI)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    return (
        f"scale={source_width}:{source_height}:"
        "force_original_aspect_ratio=increase,"
        f"crop={source_width}:{source_height},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':"
        f"d=1:s={width}x{height}:fps={FPS},"
        "setsar=1,format=yuv420p"
    )


def prepare_jobs(jobs):
    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(
                f"Visual job at position {index} is not an object."
            )

        number = get_scene_number(job, index)

        if number in seen:
            raise RuntimeError(
                f"Duplicate global scene number {number} "
                "in visual_jobs.json."
            )

        seen.add(number)

        image_value = job.get("image_path") or job.get("image")
        image = safe_repo_path(
            image_value,
            f"image_path for scene {number}",
        )

        if not image.is_file():
            raise RuntimeError(
                f"Image for scene {number} does not exist: {image}"
            )

        if image.stat().st_size < 1000:
            raise RuntimeError(
                f"Image for scene {number} is too small: {image}"
            )

        # Standardized output paths keep the renderer and manifest aligned.
        target = OUT / f"scene_{number:04d}.mp4"

        prepared.append({
            "number": number,
            "image": image,
            "duration": get_duration(job),
            "target": target,
        })

    prepared.sort(key=lambda item: item["number"])

    numbers = [item["number"] for item in prepared]
    expected = list(range(1, len(prepared) + 1))

    if numbers != expected:
        raise RuntimeError(
            "Global scene numbers must be contiguous and start at 1. "
            f"Found: {numbers}"
        )

    return prepared


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)

    if mode == "full":
        width, height = 1280, 720
    else:
        width, height = 720, 1280

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed or not on PATH.")

    jobs = load_jobs()
    prepared = prepare_jobs(jobs)

    OUT.mkdir(parents=True, exist_ok=True)

    # Remove only files owned by this renderer to avoid stale clips.
    for pattern in ("scene_*.mp4", "photo_motion_manifest.json"):
        for old in OUT.glob(pattern):
            if old.is_file():
                old.unlink()

    manifest = []

    for index, item in enumerate(prepared):
        number = item["number"]
        image = item["image"]
        seconds = item["duration"]
        target = item["target"]

        frames = max(1, round(seconds * FPS))
        actual_duration = frames / FPS
        effect = EFFECTS[index % len(EFFECTS)]

        print(
            f"\nScene {number}/{len(prepared)}"
            f"\nImage: {image.relative_to(ROOT)}"
            f"\nDuration requested: {seconds:.3f}s"
            f"\nFrames: {frames}"
            f"\nEffect: {effect}"
            f"\nFormat: {mode} ({width}x{height})"
            f"\nOutput: {target.relative_to(ROOT)}",
            flush=True,
        )

        run([
            ffmpeg,
            "-hide_banner",
            "-y",
            "-loop", "1",
            "-framerate", str(FPS),
            "-i", str(image),
            "-vf", make_filter(effect, frames, width, height),
            "-frames:v", str(frames),
            "-an",
            "-c:v", "libx264",
            "-preset", PRESET,
            "-crf", str(CRF),
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            "-movflags", "+faststart",
            str(target),
        ])

        if not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError(
                f"Generated scene video is missing or empty: {target}"
            )

        # Verify the output can be probed and contains a video stream.
        probe = subprocess.run(
            [
                shutil.which("ffprobe") or "ffprobe",
                "-v", "error",
                "-show_entries", "stream=codec_type,width,height",
                "-show_entries", "format=duration",
                "-of", "json",
                str(target),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if probe.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed for scene {number}: {probe.stderr}"
            )

        try:
            details = json.loads(probe.stdout)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid ffprobe output for scene {number}"
            ) from exc

        streams = details.get("streams", [])
        video_streams = [
            stream for stream in streams
            if stream.get("codec_type") == "video"
        ]

        if not video_streams:
            raise RuntimeError(
                f"Scene {number} output has no video stream."
            )

        stream = video_streams[0]

        if (
            stream.get("width") != width
            or stream.get("height") != height
        ):
            raise RuntimeError(
                f"Scene {number} resolution mismatch: "
                f"{stream.get('width')}x{stream.get('height')}; "
                f"expected {width}x{height}."
            )

        manifest.append({
            "scene": number,
            "global_scene": number,
            "image": str(image.relative_to(ROOT)),
            "video": str(target.relative_to(ROOT)),
            "duration": actual_duration,
            "requested_duration": seconds,
            "frames": frames,
            "effect": effect,
            "width": width,
            "height": height,
            "fps": FPS,
            "format": mode,
            "size_bytes": target.stat().st_size,
        })

    if len(manifest) != len(jobs):
        raise RuntimeError(
            f"Scene count mismatch: {len(jobs)} jobs, "
            f"{len(manifest)} clips."
        )

    manifest_path = OUT / "photo_motion_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n" + "=" * 60)
    print("PHOTO MOTION SUCCESS")
    print("Format:", mode)
    print("Resolution:", f"{width}x{height}")
    print("Expected scenes:", len(jobs))
    print("Generated clips:", len(manifest))
    print("Manifest:", manifest_path.relative_to(ROOT))
    print(
        "Total rendered duration:",
        f"{sum(item['duration'] for item in manifest):.2f}s",
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print("PHOTO MOTION FAILED:", exc, file=sys.stderr)
        sys.exit(1)
