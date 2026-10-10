
#!/usr/bin/env python3
"""Render validated photo-motion clips with resumable scene checkpoints."""

from __future__ import annotations

import hashlib
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
DEPTH_MANIFEST = VISUALS / "depth_maps" / "depth_manifest.json"
OUTPUT_DIR = ROOT / "output" / "photo_motion"
MANIFEST_FILE = OUTPUT_DIR / "photo_motion_manifest.json"

CRF = 20
PRESET = "medium"

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


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT):
        raise RuntimeError(f"{label} must be inside the repository.")

    return path


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def run_command(command):
    log("\n$", " ".join(map(str, command)))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.stdout:
        log(result.stdout.rstrip())

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}."
        )


def read_jobs():
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            "Missing output/visuals/visual_jobs.json. "
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

    raise RuntimeError(
        "Every scene needs a valid duration between "
        "0.5 and 3600 seconds."
    )


def prepare_jobs(jobs):
    from PIL import Image

    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not an object.")

        number = scene_number(job, index)

        if number in seen:
            raise RuntimeError(f"Duplicate scene number: {number}")

        seen.add(number)

        image = safe_path(
            job.get("image_path") or job.get("image"),
            f"image path for scene {number}",
        )

        if not image.is_file() or image.stat().st_size < 1000:
            raise RuntimeError(f"Missing or invalid scene image: {image}")

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
            "image_sha256": sha256_file(image),
            "duration": scene_duration(job),
            "target": OUTPUT_DIR / f"scene_{number:04d}.mp4",
        })

    prepared.sort(key=lambda item: item["number"])

    actual = [item["number"] for item in prepared]
    expected = list(range(1, len(prepared) + 1))

    if actual != expected:
        raise RuntimeError(
            f"Scene numbering must be continuous from 1. Found {actual}"
        )

    return prepared


def read_depth_maps():
    if not DEPTH_MANIFEST.is_file():
        log("NOTICE: Depth manifest missing; using 2D motion effects.")
        return {}

    try:
        data = json.loads(DEPTH_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log("WARNING: Cannot read depth manifest:", exc)
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

    if not isinstance(entries, list):
        log("WARNING: Depth manifest entries are not a list.")
        return {}

    result = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        try:
            raw_number = entry.get(
                "global_scene",
                entry.get("scene", entry.get("number")),
            )
            number = int(raw_number)

            path = safe_path(
                entry.get("depth_map") or entry.get("path") or entry.get("file"),
                f"depth map for scene {number}",
            )
        except (TypeError, ValueError, RuntimeError):
            continue

        if number < 1 or not path.is_file() or path.stat().st_size < 100:
            continue

        # A depth map must be traceable to its source image.
        source_hash = entry.get("image_sha256")
        if not isinstance(source_hash, str) or len(source_hash) != 64:
            log(
                f"NOTICE: Scene {number} depth map has no source hash; "
                "it will not be used for parallax."
            )
            continue

        result[number] = {
            "path": path,
            "image_sha256": source_hash,
            "depth_sha256": sha256_file(path),
        }

    log("Source-validated depth maps:", len(result))
    return result


def read_previous_manifest():
    if not MANIFEST_FILE.is_file():
        return {}

    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log("WARNING: Previous clip manifest cannot be read:", exc)
        return {}

    if not isinstance(data, list):
        return {}

    previous = {}

    for entry in data:
        if not isinstance(entry, dict):
            continue

        try:
            number = int(entry.get("global_scene", entry.get("scene")))
        except (TypeError, ValueError):
            continue

        if number > 0:
            previous[number] = entry

    return previous


def make_filter(effect, frames, width, height, fps):
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
        zoom = "1.055"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    else:
        raise RuntimeError(f"Unsupported FFmpeg effect: {effect}")

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


def create_parallax_clip(item, depth_path, frames, width, height, fps):
    script = ROOT / "scripts" / "depth_parallax.py"

    if not script.is_file():
        raise RuntimeError(f"Parallax compositor is missing: {script}")

    run_command([
        sys.executable,
        str(script),
        "--image", str(item["image"]),
        "--depth", str(depth_path),
        "--output", str(item["target"]),
        "--width", str(width),
        "--height", str(height),
        "--fps", str(fps),
        "--frames", str(frames),
        "--strength", "0.018",
    ])


def probe_clip(path, ffprobe, width, height, expected_duration):
    if not path.is_file() or path.stat().st_size < 1000:
        raise RuntimeError(f"Scene video is missing or too small: {path}")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height,nb_frames:"
            "format=duration,size",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        actual_duration = float(data["format"]["duration"])
        streams = [
            stream for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid ffprobe output for {path.name}: {exc}"
        ) from exc

    if not streams:
        raise RuntimeError(f"{path.name} has no video stream.")

    stream = streams[0]
    actual_width = int(stream.get("width", 0))
    actual_height = int(stream.get("height", 0))

    if (actual_width, actual_height) != (width, height):
        raise RuntimeError(
            f"Wrong resolution for {path.name}: "
            f"{actual_width}x{actual_height}; expected {width}x{height}"
        )

    if not math.isfinite(actual_duration) or actual_duration <= 0:
        raise RuntimeError(f"Invalid video duration for {path.name}.")

    tolerance = max(0.15, 2.0 / max(1, 24))

    if abs(actual_duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"Unexpected duration for {path.name}: {actual_duration:.3f}s; "
            f"expected about {expected_duration:.3f}s"
        )

    raw_frames = stream.get("nb_frames")

    if raw_frames not in (None, "N/A"):
        try:
            if int(raw_frames) < 1:
                raise RuntimeError(f"{path.name} contains no video frames.")
        except ValueError:
            pass

    return actual_duration


def write_manifest(entries):
    atomic_json(MANIFEST_FILE, entries)


def cache_signature(item, effect, depth_info, frames, width, height, fps, mode):
    payload = {
        "image_sha256": item["image_sha256"],
        "effect": effect,
        "depth_sha256": (
            depth_info["depth_sha256"] if depth_info else None
        ),
        "frames": frames,
        "width": width,
        "height": height,
        "fps": fps,
        "format": mode,
        "crf": CRF,
        "preset": PRESET,
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(serialized).hexdigest()


def try_reuse_clip(item, old_entry, signature, ffprobe, width, height, expected):
    if not isinstance(old_entry, dict):
        return None

    if old_entry.get("cache_signature") != signature:
        return None

    try:
        old_path = safe_path(
            old_entry.get("video"),
            f"cached video for scene {item['number']}",
        )

        if old_path != item["target"] or not old_path.is_file():
            return None

        actual_duration = probe_clip(
            old_path, ffprobe, width, height, expected
        )

        entry = dict(old_entry)
        entry.update({
            "scene": item["number"],
            "global_scene": item["number"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "video": old_path.relative_to(ROOT).as_posix(),
            "duration": actual_duration,
            "size_bytes": old_path.stat().st_size,
            "status": "reused",
        })

        return entry

    except (OSError, ValueError, RuntimeError):
        return None


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    fps = get_fps(config)

    if not 1 <= fps <= 60:
        raise RuntimeError(f"FPS must be between 1 and 60; got {fps}.")

    width, height = (1280, 720) if mode == "full" else (720, 1280)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe must both be installed.")

    prepared = prepare_jobs(read_jobs())
    depth_maps = read_depth_maps()
    previous = read_previous_manifest()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = []
    generated_count = 0
    reused_count = 0
    parallax_count = 0

    log("=" * 60)
    log("KATHA LOK AI PHOTO MOTION")
    log("Format:", mode)
    log("Resolution:", f"{width}x{height}")
    log("FPS:", fps)
    log("Scenes:", len(prepared))
    log("Resume enabled: yes")
    log("=" * 60)

    for index, item in enumerate(prepared):
        number = item["number"]
        frames = max(1, round(item["duration"] * fps))
        expected_duration = frames / fps

        selected_effect = EFFECTS[index % len(EFFECTS)]
        depth_info = depth_maps.get(number)

        # A depth map is only safe when it belongs to this exact image.
        if (
            depth_info is not None
            and depth_info["image_sha256"] != item["image_sha256"]
        ):
            log(
                f"NOTICE: Scene {number} depth map is stale; "
                "using 2D motion for this scene."
            )
            depth_info = None

        true_parallax = (
            selected_effect == "parallax" and depth_info is not None
        )

        effect = selected_effect

        if selected_effect == "parallax" and not true_parallax:
            effect = "cinematic_sweep"

        depth_path = depth_info["path"] if true_parallax else None

        signature = cache_signature(
            item,
            effect,
            depth_info if true_parallax else None,
            frames,
            width,
            height,
            fps,
            mode,
        )

        log("\n" + "=" * 55)
        log(f"Scene: {number}/{len(prepared)}")
        log("Motion:", effect)
        log("True depth parallax:", true_parallax)

        cached = try_reuse_clip(
            item,
            previous.get(number),
            signature,
            ffprobe,
            width,
            height,
            expected_duration,
        )

        if cached is not None:
            log("Reusing validated scene clip:", cached["video"])
            manifest.append(cached)
            reused_count += 1
            write_manifest(manifest)
            continue

        temporary_target = item["target"].with_name(
            item["target"].stem + ".rendering.mp4"
        )
        temporary_target.unlink(missing_ok=True)

        render_item = dict(item)
        render_item["target"] = temporary_target

        try:
            if true_parallax:
                create_parallax_clip(
                    render_item,
                    depth_path,
                    frames,
                    width,
                    height,
                    fps,
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
                    str(temporary_target),
                ])

            actual_duration = probe_clip(
                temporary_target,
                ffprobe,
                width,
                height,
                expected_duration,
            )

            # Only replace the final clip after validation succeeds.
            temporary_target.replace(item["target"])

        except Exception:
            temporary_target.unlink(missing_ok=True)
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
                depth_path.relative_to(ROOT).as_posix()
                if depth_path else None
            ),
            "depth_map_available": depth_path is not None,
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

        manifest.append(entry)
        generated_count += 1

        if true_parallax:
            parallax_count += 1

        # Keep completed scene metadata after every successful scene.
        write_manifest(manifest)

        log(
            "Saved:",
            entry["video"],
            "| bytes:",
            entry["size_bytes"],
        )

    if len(manifest) != len(prepared):
        raise RuntimeError(
            f"Clip count mismatch: jobs={len(prepared)}, clips={len(manifest)}"
        )

    log("=" * 60)
    log("PHOTO MOTION GENERATION SUCCESS")
    log("Format:", mode)
    log("Resolution:", f"{width}x{height}")
    log("FPS:", fps)
    log("Total scenes:", len(manifest))
    log("New clips:", generated_count)
    log("Reused clips:", reused_count)
    log("New true-parallax clips:", parallax_count)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("PHOTO MOTION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"PHOTO MOTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
