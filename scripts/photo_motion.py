#!/usr/bin/env python3
"""Resumable cinematic photo-motion renderer with validated cache entries."""

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

VERSION = "photo-motion-v4"
CRF = 20
PRESET = "medium"
MIN_IMAGE_BYTES = 1000
MIN_VIDEO_BYTES = 1000
MIN_DEPTH_BYTES = 100

EFFECTS = (
    "zoom_in",
    "pan_left",
    "parallax",
    "tilt_up",
    "zoom_out",
    "pan_right",
    "tilt_down",
    "diagonal_drift",
    "handheld_drift",
    "cinematic_sweep",
    "dutch_angle",
)


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
    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing path for {label}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} must remain inside the project.")

    return path


def run_command(command, timeout=900):
    log("$", " ".join(map(str, command)))

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


def positive_integer(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if result < 1:
        raise RuntimeError(f"{label} must be positive.")

    return result


def read_jobs():
    document = read_json(JOBS_FILE)

    if isinstance(document, list):
        jobs = document
    elif isinstance(document, dict):
        jobs = (
            document.get("jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
            or []
        )
    else:
        jobs = []

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "No visual jobs found. Run generate_visuals.py and "
            "free_image_generator.py first."
        )

    return jobs


def get_scene_number(job, index):
    value = job.get("global_scene", job.get("scene", index))

    if isinstance(value, dict):
        value = value.get("global_scene", value.get("number"))

    return positive_integer(value, f"global scene number in job {index}")


def get_duration(job):
    for key in ("duration", "scene_duration", "duration_seconds", "seconds"):
        try:
            value = float(job.get(key))
            if math.isfinite(value) and 0.5 <= value <= 3600:
                return value
        except (TypeError, ValueError):
            continue

    raise RuntimeError(
        "Each visual job must provide a valid duration between "
        "0.5 and 3600 seconds."
    )


def prepare_jobs(jobs):
    from PIL import Image

    prepared = []
    seen_global = set()
    seen_local = set()

    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Job {index} is not a JSON object.")

        number = get_scene_number(job, index)
        if number in seen_global:
            raise RuntimeError(f"Duplicate global scene number: {number}")
        seen_global.add(number)

        part = positive_integer(job.get("part", 1), "part number")
        local_scene = positive_integer(
            job.get("local_scene", job.get("scene", number)),
            "local scene number",
        )

        mapping = (part, local_scene)
        if mapping in seen_local:
            raise RuntimeError(f"Duplicate part/local-scene mapping: {mapping}")
        seen_local.add(mapping)

        image = safe_path(
            job.get("image_path") or job.get("image"),
            f"image for scene {number}",
        )

        if not image.is_file() or image.stat().st_size < MIN_IMAGE_BYTES:
            raise RuntimeError(f"Missing or too-small image: {image}")

        try:
            with Image.open(image) as opened:
                opened.verify()

            with Image.open(image) as opened:
                width, height = opened.size
                if width < 64 or height < 64:
                    raise ValueError("Image dimensions are below 64 pixels.")
        except Exception as exc:
            raise RuntimeError(
                f"Cannot validate scene {number} image: {exc}"
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

    prepared.sort(key=lambda row: row["number"])
    numbers = [row["number"] for row in prepared]

    if numbers != list(range(1, len(prepared) + 1)):
        raise RuntimeError(
            "Global scene numbers must be continuous from 1. "
            f"Found: {numbers}"
        )

    return prepared


def read_depth_maps():
    document = read_json(DEPTH_MANIFEST, None)

    if document is None:
        log("NOTICE: No depth manifest; 2D cinematic motion will be used.")
        return {}

    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        rows = (
            document.get("scenes")
            or document.get("depth_maps")
            or document.get("items")
            or []
        )
    else:
        rows = []

    if not isinstance(rows, list):
        log("WARNING: Depth manifest has an invalid scene list.")
        return {}

    from PIL import Image

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            number = int(row.get("global_scene", row.get("scene")))
            path = safe_path(
                row.get("depth_map") or row.get("path") or row.get("file"),
                f"depth map for scene {number}",
            )
        except (TypeError, ValueError, RuntimeError):
            continue

        if number < 1 or not path.is_file():
            continue

        if path.stat().st_size < MIN_DEPTH_BYTES:
            continue

        try:
            with Image.open(path) as depth:
                depth.verify()
            with Image.open(path) as depth:
                depth.load()
                if depth.width < 64 or depth.height < 64:
                    continue
        except Exception:
            log(f"WARNING: Invalid depth map for scene {number}; ignoring.")
            continue

        source_hash = row.get("image_sha256")
        if not isinstance(source_hash, str) or len(source_hash) != 64:
            continue

        expected_depth_hash = row.get("depth_sha256")
        actual_depth_hash = sha256_file(path)

        if expected_depth_hash and actual_depth_hash != expected_depth_hash:
            log(f"WARNING: Depth checksum mismatch for scene {number}.")
            continue

        model = str(row.get("model", "")).strip()
        status = str(row.get("status", "")).strip()

        is_ai_depth = (
            model == "depth-anything/Depth-Anything-V2-Small-hf"
            and status not in {
                "fallback_pseudo_depth",
                "pseudo_depth",
                "fallback_reused",
            }
        )

        result[number] = {
            "path": path,
            "image_sha256": source_hash,
            "depth_sha256": actual_depth_hash,
            "model": model or "unknown",
            "status": status or "unknown",
            "is_ai_depth": is_ai_depth,
        }

    log("Validated depth maps:", len(result))
    log("AI-estimated depth maps:", sum(
        item["is_ai_depth"] for item in result.values()
    ))
    return result


def read_previous_manifest():
    document = read_json(MANIFEST_FILE, [])

    if isinstance(document, dict):
        rows = document.get("scenes", [])
    elif isinstance(document, list):
        rows = document
    else:
        rows = []

    previous = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                number = int(row.get("global_scene", row.get("scene")))
                if number > 0:
                    previous[number] = row
            except (TypeError, ValueError):
                continue

    return previous


def choose_effect(index):
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
        raise RuntimeError(f"Unsupported effect: {effect}")

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


def probe_clip(path, ffprobe, width, height, expected_duration, fps):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        raise RuntimeError(f"Missing or too-small rendered clip: {path}")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate:"
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
        document = json.loads(result.stdout)
        duration = float(document["format"]["duration"])
        streams = [
            item for item in document.get("streams", [])
            if item.get("codec_type") == "video"
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid ffprobe response for {path.name}: {exc}"
        ) from exc

    if not streams:
        raise RuntimeError(f"No video stream in {path.name}.")

    stream = streams[0]
    actual_size = (
        int(stream.get("width", 0)),
        int(stream.get("height", 0)),
    )
    if actual_size != (width, height):
        raise RuntimeError(
            f"{path.name}: resolution {actual_size}, expected {(width, height)}."
        )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"{path.name}: invalid duration.")

    # Permit small container rounding differences only.
    tolerance = max(0.12, 2 / fps)
    if abs(duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"{path.name}: duration {duration:.3f}s; "
            f"expected {expected_duration:.3f}s."
        )

    rate = stream.get("r_frame_rate", "")
    if isinstance(rate, str) and "/" in rate:
        try:
            numerator, denominator = rate.split("/", 1)
            actual_fps = float(numerator) / float(denominator)
            if not math.isfinite(actual_fps) or abs(actual_fps - fps) > 0.5:
                raise RuntimeError(
                    f"{path.name}: FPS {actual_fps}; expected {fps}."
                )
        except (ValueError, ZeroDivisionError):
            raise RuntimeError(f"{path.name}: invalid frame rate metadata.")

    return duration


def cache_signature(item, effect, depth_info, frames, width, height, fps, mode):
    renderer = Path(__file__).resolve()
    parallax_script = ROOT / "scripts" / "depth_parallax.py"

    payload = {
        "version": VERSION,
        "renderer_sha256": sha256_file(renderer),
        "parallax_script_sha256": (
            sha256_file(parallax_script)
            if effect == "parallax" and parallax_script.is_file()
            else None
        ),
        "image_sha256": item["image_sha256"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "duration_frames": frames,
        "effect": effect,
        "depth_sha256": depth_info["depth_sha256"] if depth_info else None,
        "depth_model": depth_info["model"] if depth_info else None,
        "depth_status": depth_info["status"] if depth_info else None,
        "width": width,
        "height": height,
        "fps": fps,
        "format": mode,
        "crf": CRF,
        "preset": PRESET,
    }

    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def try_reuse(
    item, old_entry, signature, ffprobe,
    width, height, expected_duration, fps,
):
    if not isinstance(old_entry, dict):
        return None

    if old_entry.get("cache_signature") != signature:
        return None

    try:
        old_path = safe_path(
            old_entry.get("video"),
            f"cached video for scene {item['number']}",
        )

        if old_path != item["target"]:
            return None

        if not old_path.is_file():
            return None

        actual_hash = sha256_file(old_path)
        expected_hash = old_entry.get("video_sha256")
        if expected_hash and actual_hash != expected_hash:
            return None

        duration = probe_clip(
            old_path, ffprobe, width, height, expected_duration, fps
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
            "video_sha256": actual_hash,
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
        raise RuntimeError(f"Missing renderer: {script}")

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

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported video format: {mode!r}")
    if not 1 <= fps <= 60:
        raise RuntimeError(f"FPS must be from 1 to 60; got {fps}.")

    width, height = (1280, 720) if mode == "full" else (720, 1280)

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
    true_parallax_count = 0
    approximate_parallax_count = 0

    log("=" * 58)
    log("KATHA LOK AI PHOTO MOTION")
    log("Version:", VERSION)
    log("Format:", mode)
    log("Resolution:", f"{width}x{height}")
    log("FPS:", fps)
    log("Scenes:", len(prepared))
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

        # The parallax script can consume either depth type, but the
        # manifest distinguishes AI-estimated depth from radial fallback.
        has_depth = depth_info is not None
        if effect == "parallax" and not has_depth:
            effect = "cinematic_sweep"

        used_depth = depth_info if effect == "parallax" else None
        signature = cache_signature(
            item, effect, used_depth,
            frames, width, height, fps, mode,
        )

        log(f"\nScene {number}/{len(prepared)} | effect={effect}")

        cached = try_reuse(
            item, previous.get(number), signature, ffprobe,
            width, height, expected_duration, fps,
        )

        if cached:
            log("Reusing validated clip:", cached["video"])
            manifest[number] = cached
            reused += 1
            checkpoint()
            continue

        target = item["target"]
        temporary = target.with_name(target.stem + ".rendering.mp4")
        temporary.unlink(missing_ok=True)

        try:
            if effect == "parallax" and used_depth:
                render_parallax(
                    item, used_depth["path"], temporary,
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
                    str(temporary),
                ])

            actual_duration = probe_clip(
                temporary, ffprobe, width, height,
                expected_duration, fps,
            )

            # Final output is replaced only after the temporary clip passes.
            temporary.replace(target)

        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        video_hash = sha256_file(target)
        depth_is_ai = bool(
            used_depth and used_depth.get("is_ai_depth")
        )
        depth_is_approximate = bool(
            used_depth and not used_depth.get("is_ai_depth")
        )

        entry = {
            "scene": number,
            "global_scene": number,
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "video": target.relative_to(ROOT).as_posix(),
            "video_sha256": video_hash,
            "depth_map": (
                used_depth["path"].relative_to(ROOT).as_posix()
                if used_depth else None
            ),
            "depth_map_available": has_depth,
            "depth_model": (
                used_depth["model"] if used_depth else None
            ),
            "depth_is_ai_estimated": depth_is_ai,
            "depth_is_approximate_fallback": depth_is_approximate,
            "effect": effect,
            "effect_is_depth_guided": effect == "parallax" and has_depth,
            "effect_uses_ai_depth": effect == "parallax" and depth_is_ai,
            "duration": actual_duration,
            "requested_duration": item["duration"],
            "frames": frames,
            "width": width,
            "height": height,
            "fps": fps,
            "format": mode,
            "size_bytes": target.stat().st_size,
            "cache_signature": signature,
            "renderer_version": VERSION,
            "status": "generated",
        }

        manifest[number] = entry
        generated += 1

        if effect == "parallax" and depth_is_ai:
            true_parallax_count += 1
        elif effect == "parallax" and depth_is_approximate:
            approximate_parallax_count += 1

        checkpoint()
        log("Checkpoint saved:", entry["video"])
        log("Effect:", effect)
        log("AI depth:", depth_is_ai)
        log("Approximate depth:", depth_is_approximate)

    # Validate every clip, including clips reused from earlier runs.
    for item in prepared:
        number = item["number"]
        entry = manifest.get(number)

        if not isinstance(entry, dict):
            raise RuntimeError(f"Missing manifest entry for scene {number}.")

        if entry.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(f"Image checksum mismatch for scene {number}.")

        if not entry.get("cache_signature"):
            raise RuntimeError(f"Missing cache signature for scene {number}.")

        target = safe_path(entry.get("video"), f"video for scene {number}")
        if target != item["target"]:
            raise RuntimeError(
                f"Unexpected output path for scene {number}: {target}"
            )

        frames = max(1, round(item["duration"] * fps))
        probe_clip(
            target, ffprobe, width, height, frames / fps, fps
        )

        actual_hash = sha256_file(target)
        if entry.get("video_sha256") and entry["video_sha256"] != actual_hash:
            raise RuntimeError(f"Video checksum mismatch for scene {number}.")

    checkpoint()

    log("=" * 58)
    log("PHOTO MOTION COMPLETE")
    log("Scenes:", len(prepared))
    log("New clips:", generated)
    log("Reused clips:", reused)
    log("AI-depth parallax clips generated:", true_parallax_count)
    log("Fallback-depth parallax clips generated:", approximate_parallax_count)
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