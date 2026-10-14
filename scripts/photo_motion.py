
#!/usr/bin/env python3
"""Resumable cinematic photo-motion renderer for Katha Lok AI."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_fps

VISUALS = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS / "visual_jobs.json"
DEPTH_MANIFEST = VISUALS / "depth_maps" / "depth_manifest.json"
OUTPUT_DIR = ROOT / "output" / "photo_motion"
MANIFEST_FILE = OUTPUT_DIR / "photo_motion_manifest.json"

CRF = 20
PRESET = "medium"
CACHE_VERSION = "photo-motion-v3"
MIN_IMAGE_BYTES = 1000
MIN_VIDEO_BYTES = 1000

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


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")

    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} must remain inside the repository.")

    return path


def run_command(command, timeout=900):
    log("\n$", " ".join(map(str, command)))

    result = subprocess.run(
        [str(value) for value in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.stdout:
        log(result.stdout.rstrip())

    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command[0]}"
        )


def read_jobs():
    data = read_json(JOBS_FILE)

    if data is None:
        raise RuntimeError(
            "Cannot read output/visuals/visual_jobs.json. "
            "Generate visual jobs first."
        )

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
        raise RuntimeError("No scene jobs found in visual_jobs.json.")

    return jobs


def get_scene_number(job, index):
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


def positive_integer(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if number < 1:
        raise RuntimeError(f"{label} must be positive.")

    return number


def get_duration(job):
    for key in ("duration", "scene_duration", "duration_seconds", "seconds"):
        try:
            value = float(job.get(key))
            if math.isfinite(value) and 0.5 <= value <= 3600:
                return value
        except (TypeError, ValueError):
            pass

    raise RuntimeError(
        "Every visual job must have a duration from 0.5 to 3600 seconds."
    )


def prepare_jobs(jobs):
    from PIL import Image

    prepared = []
    seen_global = set()
    seen_mapping = set()

    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not an object.")

        number = get_scene_number(job, index)

        if number in seen_global:
            raise RuntimeError(f"Duplicate global scene number: {number}")
        seen_global.add(number)

        part = positive_integer(job.get("part", 1), "part number")
        local_scene = positive_integer(
            job.get("local_scene", number), "local scene number"
        )

        mapping = (part, local_scene)
        if mapping in seen_mapping:
            raise RuntimeError(f"Duplicate part/local-scene mapping: {mapping}")
        seen_mapping.add(mapping)

        image = safe_path(
            job.get("image_path") or job.get("image"),
            f"image for scene {number}",
        )

        if not image.is_file() or image.stat().st_size < MIN_IMAGE_BYTES:
            raise RuntimeError(f"Image is missing or too small: {image}")

        try:
            with Image.open(image) as im:
                im.verify()

            with Image.open(image) as im:
                if im.width < 64 or im.height < 64:
                    raise RuntimeError("Image dimensions are too small.")
        except Exception as exc:
            raise RuntimeError(
                f"Cannot validate image for scene {number}: {exc}"
            ) from exc

        prepared.append({
            "number": number,
            "part": part,
            "local_scene": local_scene,
            "image": image,
            "image_sha256": sha256_file(image),
            "duration": get_duration(job),
            "target": OUTPUT_DIR / f"scene_{number:04d}.mp4",
        })

    prepared.sort(key=lambda item: item["number"])
    numbers = [item["number"] for item in prepared]

    if numbers != list(range(1, len(prepared) + 1)):
        raise RuntimeError(
            "Global scene numbers must be continuous from 1. "
            f"Found: {numbers}"
        )

    return prepared


def read_depth_maps():
    data = read_json(DEPTH_MANIFEST)

    if data is None:
        log("NOTICE: No depth manifest; using 2D cinematic motion.")
        return {}

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = (
            data.get("scenes")
            or data.get("depth_maps")
            or data.get("items")
            or []
        )
    else:
        entries = []

    result = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        try:
            number = int(
                entry.get(
                    "global_scene",
                    entry.get("scene", entry.get("number")),
                )
            )

            path = safe_path(
                entry.get("depth_map")
                or entry.get("path")
                or entry.get("file"),
                f"depth map for scene {number}",
            )
        except (TypeError, ValueError, RuntimeError):
            continue

        source_hash = entry.get("image_sha256")

        if (
            number < 1
            or not path.is_file()
            or path.stat().st_size < 100
            or not isinstance(source_hash, str)
            or len(source_hash) != 64
        ):
            continue

        result[number] = {
            "path": path,
            "image_sha256": source_hash,
            "depth_sha256": sha256_file(path),
        }

    log("Validated depth maps:", len(result))
    return result


def read_previous_manifest():
    data = read_json(MANIFEST_FILE, [])

    if not isinstance(data, list):
        return {}

    previous = {}

    for entry in data:
        if not isinstance(entry, dict):
            continue

        try:
            number = int(entry.get("global_scene", entry.get("scene")))
            if number > 0:
                previous[number] = entry
        except (TypeError, ValueError):
            continue

    return previous


def choose_effect(index):
    """Cycle through distinct camera movements in a deterministic order."""
    return EFFECTS[index % len(EFFECTS)]


def make_filter(effect, frames, width, height, fps):
    denominator = max(frames - 1, 1)
    source_width = width * 2
    source_height = height * 2

    zoom = "1.06"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"

    if effect == "zoom_in":
        zoom = f"min(1+0.10*on/{denominator},1.10)"

    elif effect == "zoom_out":
        zoom = f"max(1.10-0.10*on/{denominator},1.0)"

    elif effect == "pan_left":
        x = f"(iw-iw/zoom)*(1-on/{denominator})"

    elif effect == "pan_right":
        x = f"(iw-iw/zoom)*on/{denominator}"

    elif effect == "tilt_up":
        zoom = "1.07"
        y = f"(ih-ih/zoom)*(1-on/{denominator})"

    elif effect == "tilt_down":
        zoom = "1.07"
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
        zoom = "1.055"

    elif effect != "parallax":
        raise RuntimeError(f"Unsupported motion effect: {effect}")

    filters = [
        f"scale={source_width}:{source_height}:"
        "force_original_aspect_ratio=increase",
        f"crop={source_width}:{source_height}",
    ]

    if effect == "dutch_angle":
        filters.append(
            "rotate='0.012*sin(2*PI*t/4)':"
            "ow=rotw(0.012):oh=roth(0.012):c=black"
        )

    filters.extend([
        f"zoompan=z='{zoom}':x='{x}':y='{y}':"
        f"d=1:s={width}x{height}:fps={fps}",
        "setsar=1",
        "format=yuv420p",
    ])

    return ",".join(filters)


def probe_clip(path, ffprobe, width, height, expected_duration):
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        raise RuntimeError(f"Missing or invalid rendered clip: {path}")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height:"
            "format=duration,size",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = [
            stream for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid ffprobe data for {path.name}: {exc}"
        ) from exc

    if not streams:
        raise RuntimeError(f"No video stream found in {path.name}.")

    stream = streams[0]
    actual_size = (
        int(stream.get("width", 0)),
        int(stream.get("height", 0)),
    )

    if actual_size != (width, height):
        raise RuntimeError(
            f"{path.name} resolution is {actual_size}; "
            f"expected {(width, height)}."
        )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration in {path.name}.")

    if abs(duration - expected_duration) > max(0.20, 2 / max(1, 60)):
        raise RuntimeError(
            f"{path.name} duration {duration:.3f}s; "
            f"expected about {expected_duration:.3f}s."
        )

    return duration


def cache_signature(item, effect, depth_info, frames, width, height, fps, mode):
    parallax_script = ROOT / "scripts" / "depth_parallax.py"

    payload = {
        "version": CACHE_VERSION,
        "renderer_sha256": sha256_file(Path(__file__).resolve()),
        "parallax_sha256": (
            sha256_file(parallax_script)
            if effect == "parallax" and parallax_script.is_file()
            else None
        ),
        "image_sha256": item["image_sha256"],
        "effect": effect,
        "depth_sha256": depth_info["depth_sha256"] if depth_info else None,
        "frames": frames,
        "width": width,
        "height": height,
        "fps": fps,
        "format": mode,
        "crf": CRF,
        "preset": PRESET,
    }

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def try_reuse(item, old_entry, signature, ffprobe, width, height, expected):
    if not isinstance(old_entry, dict):
        return None

    if old_entry.get("cache_signature") != signature:
        return None

    try:
        old_path = safe_path(
            old_entry.get("video"),
            f"cached clip for scene {item['number']}",
        )

        if old_path != item["target"] or not old_path.is_file():
            return None

        duration = probe_clip(
            old_path, ffprobe, width, height, expected
        )

        entry = dict(old_entry)
        entry.update({
            "scene": item["number"],
            "global_scene": item["number"],
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "video": old_path.relative_to(ROOT).as_posix(),
            "duration": duration,
            "size_bytes": old_path.stat().st_size,
            "status": "reused",
        })

        return entry

    except (OSError, ValueError, RuntimeError):
        return None


def render_parallax(item, depth_path, target, frames, width, height, fps):
    script = ROOT / "scripts" / "depth_parallax.py"

    if not script.is_file():
        raise RuntimeError(f"Missing parallax renderer: {script}")

    run_command([
        sys.executable, str(script),
        "--image", str(item["image"]),
        "--depth", str(depth_path),
        "--output", str(target),
        "--width", str(width),
        "--height", str(height),
        "--fps", str(fps),
        "--frames", str(frames),
        "--strength", "0.018",
    ])


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    fps = get_fps(config)

    if not 1 <= fps <= 60:
        raise RuntimeError(f"FPS must be from 1 to 60; got {fps}.")

    width, height = (
        (1280, 720) if mode == "full" else (720, 1280)
    )

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe must be installed.")

    prepared = prepare_jobs(read_jobs())
    depth_maps = read_depth_maps()
    previous = read_previous_manifest()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    current_numbers = {item["number"] for item in prepared}
    manifest = {
        number: dict(entry)
        for number, entry in previous.items()
        if number in current_numbers
    }

    def checkpoint():
        atomic_json(
            MANIFEST_FILE,
            [manifest[number] for number in sorted(manifest)],
        )

    checkpoint()

    generated = 0
    reused = 0
    parallax_count = 0

    log("=" * 58)
    log("KATHA LOK AI PHOTO MOTION")
    log("Format:", mode)
    log("Resolution:", f"{width}x{height}")
    log("FPS:", fps)
    log("Scene count:", len(prepared))
    log("Resume/checkpoint: enabled")
    log("=" * 58)

    for index, item in enumerate(prepared):
        number = item["number"]
        frames = max(1, round(item["duration"] * fps))
        expected_duration = frames / fps
        effect = choose_effect(index)

        depth_info = depth_maps.get(number)

        if depth_info and (
            depth_info["image_sha256"] != item["image_sha256"]
        ):
            log(f"Scene {number}: stale depth map ignored.")
            depth_info = None

        true_parallax = effect == "parallax" and depth_info is not None

        if effect == "parallax" and not true_parallax:
            effect = "cinematic_sweep"

        used_depth = depth_info if true_parallax else None
        signature = cache_signature(
            item, effect, used_depth,
            frames, width, height, fps, mode,
        )

        log(f"\nScene {number}/{len(prepared)} | effect={effect}")

        cached = try_reuse(
            item, previous.get(number), signature,
            ffprobe, width, height, expected_duration,
        )

        if cached:
            log("Reusing validated clip:", cached["video"])
            manifest[number] = cached
            reused += 1
            checkpoint()
            continue

        temp = item["target"].with_name(
            item["target"].stem + ".rendering.mp4"
        )
        temp.unlink(missing_ok=True)

        try:
            if true_parallax:
                render_parallax(
                    item, depth_info["path"], temp,
                    frames, width, height, fps,
                )
            else:
                run_command([
                    ffmpeg,
                    "-hide_banner", "-loglevel", "error", "-y",
                    "-loop", "1",
                    "-framerate", str(fps),
                    "-i", str(item["image"]),
                    "-vf", make_filter(
                        effect, frames, width, height, fps
                    ),
                    "-frames:v", str(frames),
                    "-an",
                    "-c:v", "libx264",
                    "-preset", PRESET,
                    "-crf", str(CRF),
                    "-pix_fmt", "yuv420p",
                    "-r", str(fps),
                    "-movflags", "+faststart",
                    str(temp),
                ])

            actual_duration = probe_clip(
                temp, ffprobe, width, height, expected_duration
            )

            # Replace final clip only after successful validation.
            temp.replace(item["target"])

        except Exception:
            temp.unlink(missing_ok=True)
            raise

        entry = {
            "scene": number,
            "global_scene": number,
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "video": item["target"].relative_to(ROOT).as_posix(),
            "depth_map": (
                depth_info["path"].relative_to(ROOT).as_posix()
                if true_parallax else None
            ),
            "depth_map_available": true_parallax,
            "effect": effect,
            "effect_is_true_parallax": true_parallax,
            "duration": actual_duration,
            "requested_duration": item["duration"],
            "frames": frames,
            "width": width,
            "height": height,
            "fps": fps,
            "format": mode,
            "size_bytes": item["target"].stat().st_size,
            "cache_signature": signature,
            "status": "generated",
        }

        manifest[number] = entry
        generated += 1

        if true_parallax:
            parallax_count += 1

        checkpoint()
        log("Checkpoint saved:", entry["video"])

    # Validate all scenes before marking this stage complete.
    for item in prepared:
        number = item["number"]
        entry = manifest.get(number)

        if not isinstance(entry, dict):
            raise RuntimeError(f"Missing manifest entry for scene {number}.")

        if entry.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(f"Image hash mismatch for scene {number}.")

        if not entry.get("cache_signature"):
            raise RuntimeError(f"Missing cache signature for scene {number}.")

        frames = max(1, round(item["duration"] * fps))
        probe_clip(
            item["target"], ffprobe, width, height, frames / fps
        )

    checkpoint()

    log("=" * 58)
    log("PHOTO MOTION GENERATION SUCCESS")
    log("Scenes:", len(prepared))
    log("Generated:", generated)
    log("Reused:", reused)
    log("True depth-parallax clips generated:", parallax_count)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("PHOTO MOTION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"PHOTO MOTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
