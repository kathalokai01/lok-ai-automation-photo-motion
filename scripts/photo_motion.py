#!/usr/bin/env python3
"""Create validated cinematic photo-motion clips, including depth parallax."""

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
DEPTH_DIR = VISUALS / "depth_maps"
DEPTH_MANIFEST = DEPTH_DIR / "depth_manifest.json"

OUTPUT_DIR = ROOT / "output" / "photo_motion"
MANIFEST_FILE = OUTPUT_DIR / "photo_motion_manifest.json"

CRF = 20
PRESET = "medium"

# Parallax requires a matching generated depth map.
# Other effects work without depth estimation.
EFFECTS = [
    "zoom_in",
    "parallax",
    "pan_left",
    "tilt_up",
    "zoom_out",
    "pan_right",
    "tilt_down",
    "diagonal_drift",
    "handheld_drift",
    "cinematic_sweep",
    "dutch_angle",
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
    if result.returncode:
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
        raise RuntimeError(f"Cannot read visual jobs: {exc}") from exc

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
        raise RuntimeError(f"Scene number must be positive: {number}")
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

    raise RuntimeError("Every scene needs a valid duration.")


def prepare_jobs(jobs):
    from PIL import Image

    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not a JSON object.")

        number = scene_number(job, index)
        if number in seen:
            raise RuntimeError(f"Duplicate scene number: {number}")
        seen.add(number)

        image = safe_path(
            job.get("image_path") or job.get("image"),
            f"image path for scene {number}",
        )

        if not image.is_file() or image.stat().st_size < 1000:
            raise RuntimeError(
                f"Scene {number} image is missing or too small: {image}"
            )

        try:
            with Image.open(image) as source:
                source.verify()
            with Image.open(image) as source:
                if source.width < 64 or source.height < 64:
                    raise RuntimeError("Image dimensions are too small.")
        except Exception as exc:
            raise RuntimeError(
                f"Invalid image for scene {number}: {exc}"
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

    if actual != list(range(1, len(prepared) + 1)):
        raise RuntimeError(
            "Global scene numbering must be continuous from 1. "
            f"Found: {actual}"
        )

    return prepared


def read_depth_maps():
    """Return scene number -> depth-map path, if a valid manifest exists."""
    if not DEPTH_MANIFEST.is_file():
        log(
            "NOTICE: Depth manifest is absent. "
            "Parallax will be replaced by a normal cinematic move."
        )
        return {}

    try:
        data = json.loads(
            DEPTH_MANIFEST.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        log("WARNING: Cannot read depth manifest:", exc)
        return {}

    if isinstance(data, dict):
        entries = (
            data.get("scenes")
            or data.get("depth_maps")
            or data.get("items")
            or []
        )
    elif isinstance(data, list):
        entries = data
    else:
        entries = []

    result = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        raw_number = entry.get(
            "global_scene",
            entry.get("scene", entry.get("number")),
        )
        raw_path = (
            entry.get("depth_map")
            or entry.get("path")
            or entry.get("file")
        )

        try:
            number = int(raw_number)
            path = safe_path(raw_path, f"depth map for scene {number}")
        except (TypeError, ValueError, RuntimeError):
            continue

        if number > 0 and path.is_file() and path.stat().st_size > 100:
            result[number] = path

    log("Valid depth maps:", len(result))
    return result


def make_filter(effect, frames, width, height, fps):
    """Build a restrained FFmpeg camera movement from a still image."""

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

    elif effect == "tilt_up":
        zoom = "1.07"
        x = "(iw-iw/zoom)/2"
        y = f"(ih-ih/zoom)*(1-on/{denominator})"

    elif effect == "tilt_down":
        zoom = "1.07"
        x = "(iw-iw/zoom)/2"
        y = f"(ih-ih/zoom)*on/{denominator}"

    elif effect == "diagonal_drift":
        zoom = "1.09"
        x = f"(iw-iw/zoom)*on/{denominator}"
        y = f"(ih-ih/zoom)*(1-on/{denominator})"

    elif effect == "handheld_drift":
        zoom = "1.055"
        x = "(iw-iw/zoom)/2+2*sin(on*0.16)"
        y = "(ih-ih/zoom)/2+2*sin(on*0.11)"

    elif effect == "cinematic_sweep":
        zoom = f"1.035+0.025*sin(PI*on/{denominator})"
        x = f"(iw-iw/zoom)*(0.5-0.35*cos(PI*on/{denominator}))"
        y = f"(ih-ih/zoom)*(0.5+0.20*sin(PI*on/{denominator}))"

    elif effect == "dutch_angle":
        # Small rotation gives a cinematic angled framing.
        zoom = "1.055"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    elif effect == "parallax":
        # This is only a restrained camera-like move over a depth-guided
        # scene. Actual layered displacement is performed in a separate
        # depth-aware compositor; FFmpeg zoompan alone cannot create it.
        zoom = f"1.04+0.025*on/{denominator}"
        x = f"(iw-iw/zoom)*(0.42+0.16*on/{denominator})"
        y = f"(ih-ih/zoom)*(0.52-0.08*on/{denominator})"

    else:
        raise RuntimeError(f"Unsupported motion effect: {effect}")

    filters = [
        f"scale={source_width}:{source_height}:"
        "force_original_aspect_ratio=increase",
        f"crop={source_width}:{source_height}",
    ]

    if effect == "dutch_angle":
        filters.append(
            "rotate='0.012*sin(2*PI*t/4)':"
            "ow=rotw(0.012):oh=roth(0.012):"
            "c=black"
        )

    filters.extend([
        f"zoompan=z='{zoom}':x='{x}':y='{y}':"
        f"d=1:s={width}x{height}:fps={fps}",
        "setsar=1",
        "format=yuv420p",
    ])

    return ",".join(filters)


def probe_clip(path, ffprobe, width, height, expected_duration):
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"Scene video is missing or empty: {path}")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height:format=duration,size",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
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

    streams = [
        stream for stream in data.get("streams", [])
        if stream.get("codec_type") == "video"
    ]
    if not streams:
        raise RuntimeError(f"{path.name} has no video stream.")

    stream = streams[0]
    actual_width = int(stream.get("width", 0))
    actual_height = int(stream.get("height", 0))

    if (actual_width, actual_height) != (width, height):
        raise RuntimeError(
            f"Wrong resolution for {path.name}: "
            f"{actual_width}x{actual_height}"
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
        raise RuntimeError(f"FPS must be between 1 and 60; got {fps}.")

    width, height = (1280, 720) if mode == "full" else (720, 1280)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed.")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    jobs = read_jobs()
    prepared = prepare_jobs(jobs)
    depth_maps = read_depth_maps()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for old_file in OUTPUT_DIR.glob("scene_*.mp4"):
        if old_file.is_file():
            old_file.unlink()

    manifest = []

    for index, item in enumerate(prepared):
        number = item["number"]
        image = item["image"]
        requested_seconds = item["duration"]
        target = item["target"]

        frames = max(1, round(requested_seconds * fps))
        actual_duration = frames / fps
        selected_effect = EFFECTS[index % len(EFFECTS)]
        depth_path = depth_maps.get(number)

        # Do not label a clip as parallax if it has no matching depth map.
        effect = selected_effect
        if effect == "parallax" and depth_path is None:
            effect = "cinematic_sweep"

        log("\n" + "=" * 55)
        log(f"Scene: {number}/{len(prepared)}")
        log(f"Image: {image.relative_to(ROOT)}")
        log(f"Motion: {effect}")
        log(f"Depth map: {depth_path.relative_to(ROOT) if depth_path else 'not used'}")
        log(f"Duration: {actual_duration:.3f}s")
        log(f"Resolution: {width}x{height}, FPS: {fps}")

        video_filter = make_filter(
            effect, frames, width, height, fps
        )

        run_command([
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
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
            target, ffprobe, width, height, actual_duration
        )

        manifest.append({
            "scene": number,
            "global_scene": number,
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image": str(image.relative_to(ROOT)),
            "video": str(target.relative_to(ROOT)),
            "depth_map": (
                str(depth_path.relative_to(ROOT))
                if depth_path else None
            ),
            "depth_map_available": depth_path is not None,
            "duration": actual,
            "requested_duration": requested_seconds,
            "frames": frames,
            "effect": effect,
            "effect_is_true_parallax": False,
            "width": width,
            "height": height,
            "fps": fps,
            "format": mode,
            "size_bytes": target.stat().st_size,
        })

        write_manifest(manifest)

    if len(manifest) != len(jobs):
        raise RuntimeError(
            f"Clip count mismatch: jobs={len(jobs)}, clips={len(manifest)}"
        )

    log("\nPHOTO MOTION GENERATION SUCCESS")
    log("Format:", mode)
    log("Resolution:", f"{width}x{height}")
    log("FPS:", fps)
    log("Scenes:", len(manifest))
    log(
        "Total duration:",
        f"{sum(row['duration'] for row in manifest):.2f}s",
    )
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log(
        "NOTE: The parallax-labelled move is still a 2D camera move. "
        "True foreground/background displacement needs a depth-aware compositor."
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"PHOTO MOTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)