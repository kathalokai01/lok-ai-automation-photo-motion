#!/usr/bin/env python3
"""Validated, resumable cinematic Photo Motion renderer."""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_fps,
)

OUTPUT = ROOT / "output"
VISUALS = OUTPUT / "visuals"
JOBS_FILE = VISUALS / "visual_jobs.json"
DEPTH_FILE = VISUALS / "depth_maps" / "depth_manifest.json"

RENDER_DIR = OUTPUT / "photo_motion"
MANIFEST_FILE = RENDER_DIR / "photo_motion_manifest.json"

VERSION = "photo-motion-v10-exact-mapping-verified-resume"
CRF = 20
PRESET = "medium"
MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100
MIN_VIDEO_BYTES = 1000

EFFECTS = (
    "zoom_in", "zoom_out",
    "pan_left", "pan_right",
    "pan_up", "pan_down",
    "diagonal_drift", "diagonal_push",
    "zoom_pan_left", "zoom_pan_right",
    "zoom_pan_up", "zoom_pan_down",
    "cinematic_sweep", "handheld_drift",
    "tilt_up", "tilt_down", "dutch_angle",
)


def log(*values):
    print(*values, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_data(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return default


def write_json(path, data):
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


def safe_path(value, label, must_exist=True):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path is missing")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory")

    if must_exist and not path.is_file():
        raise RuntimeError(f"{label}: file does not exist: {path}")

    return path


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if result < 1:
        raise RuntimeError(f"{label} must be at least 1")

    return result


def run_command(command, label, timeout=1800):
    command = [str(item) for item in command]
    log("$", " ".join(command))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.stdout:
        log(result.stdout[-5000:])

    if result.returncode:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}"
        )


def load_job_rows():
    data = read_json(JOBS_FILE)

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("jobs")
            or data.get("visual_jobs")
            or data.get("scenes")
            or []
        )
    else:
        rows = []

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            "visual_jobs.json is missing or contains no scene jobs"
        )

    return rows


def scene_duration(row):
    for key in (
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds",
    ):
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            continue

        if math.isfinite(value) and 0.5 <= value <= 3600:
            return value

    raise RuntimeError(
        "Every visual job must contain a valid scene duration"
    )


def validate_image(path, scene_number):
    from PIL import Image

    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(
            f"Scene {scene_number}: image is missing or too small: {path}"
        )

    try:
        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format

        if width < 64 or height < 64:
            raise RuntimeError("Image dimensions are smaller than 64x64")

        if image_format not in {"PNG", "JPEG", "WEBP"}:
            raise RuntimeError(
                f"Unsupported image format: {image_format}"
            )

    except Exception as exc:
        raise RuntimeError(
            f"Scene {scene_number}: invalid image: {exc}"
        ) from exc

    return width, height


def prepare_jobs(rows, expected_parts, scenes_per_part):
    prepared = []
    seen = set()

    expected_count = expected_parts * scenes_per_part

    if len(rows) != expected_count:
        raise RuntimeError(
            "Visual job count does not match Input/topic.txt: "
            f"expected {expected_count}, found {len(rows)}"
        )

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Visual job {index} is not an object")

        try:
            part = positive_int(row.get("part"), "part number")
            local_scene = positive_int(
                row.get("local_scene", row.get("scene")),
                "local scene number",
            )
            global_scene = positive_int(
                row.get("global_scene", row.get("global_scene_number", index)),
                "global scene number",
            )
        except RuntimeError as exc:
            raise RuntimeError(f"Visual job {index}: {exc}") from exc

        expected_part = (index - 1) // scenes_per_part + 1
        expected_scene = (index - 1) % scenes_per_part + 1

        if (part, local_scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(
                f"Scene mapping error at row {index}: "
                f"found part={part}, scene={local_scene}, "
                f"global={global_scene}; expected "
                f"part={expected_part}, scene={expected_scene}, "
                f"global={index}"
            )

        key = (part, local_scene)
        if key in seen:
            raise RuntimeError(f"Duplicate scene mapping: {key}")
        seen.add(key)

        image_value = row.get("image_path") or row.get("image")
        image = safe_path(
            image_value,
            f"scene {global_scene} image",
        )

        image_width, image_height = validate_image(
            image,
            global_scene,
        )

        prepared.append({
            "number": global_scene,
            "part": part,
            "local_scene": local_scene,
            "image": image,
            "image_sha256": sha256_file(image),
            "image_width": image_width,
            "image_height": image_height,
            "duration": scene_duration(row),
            "target": RENDER_DIR / f"scene_{global_scene:04d}.mp4",
        })

    if len(seen) != expected_count:
        raise RuntimeError("Some configured scenes are missing")

    return prepared


def read_depth_maps(prepared):
    data = read_json(DEPTH_FILE)

    if data is None:
        log("Depth manifest unavailable; 2D motion will be used.")
        return {}

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("scenes")
            or data.get("depth_maps")
            or data.get("items")
            or []
        )
    else:
        raise RuntimeError("Depth manifest has an invalid structure")

    if not isinstance(rows, list):
        raise RuntimeError("Depth manifest scene list is invalid")

    expected = {
        item["number"]: item["image_sha256"]
        for item in prepared
    }

    result = {}
    from PIL import Image

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            number = positive_int(
                row.get("global_scene", row.get("scene")),
                "depth scene number",
            )
            path = safe_path(
                row.get("depth_map")
                or row.get("depth_path")
                or row.get("path")
                or row.get("file"),
                f"scene {number} depth map",
            )
        except RuntimeError:
            continue

        if number not in expected:
            continue

        if path.stat().st_size < MIN_DEPTH_BYTES:
            log(f"WARNING: Scene {number} depth map is too small")
            continue

        if row.get("image_sha256") != expected[number]:
            log(f"WARNING: Scene {number} depth map is stale")
            continue

        try:
            with Image.open(path) as image:
                image.verify()

            with Image.open(path) as image:
                if image.width < 64 or image.height < 64:
                    raise ValueError("Depth map is smaller than 64x64")
                if image.mode not in {"1", "L", "I", "I;16", "F"}:
                    image.convert("L")

        except Exception as exc:
            log(f"WARNING: Scene {number} depth map rejected: {exc}")
            continue

        actual_hash = sha256_file(path)
        declared_hash = row.get("depth_sha256")

        if declared_hash and declared_hash != actual_hash:
            log(f"WARNING: Scene {number} depth checksum mismatch")
            continue

        model = str(row.get("model", "unknown"))
        status = str(row.get("status", "unknown")).lower()

        is_ai_depth = (
            "depth-anything" in model.lower()
            and "pseudo" not in status
            and "fallback" not in status
        )

        result[number] = {
            "path": path,
            "image_sha256": expected[number],
            "depth_sha256": actual_hash,
            "model": model,
            "status": status,
            "is_ai_depth": is_ai_depth,
        }

    log("Validated depth maps:", len(result))
    log("AI depth maps:", sum(
        1 for item in result.values() if item["is_ai_depth"]
    ))
    log("Approximate depth maps:", sum(
        1 for item in result.values() if not item["is_ai_depth"]
    ))

    return result


def previous_entries():
    data = read_json(MANIFEST_FILE, {})

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("scenes", [])
    else:
        rows = []

    result = {}

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                number = int(row.get("global_scene", row.get("scene")))
                if number > 0:
                    result[number] = row
            except (TypeError, ValueError):
                continue

    return result


def build_effect_plan(prepared, depth_maps):
    seed = digest_data([
        {
            "scene": item["number"],
            "image": item["image_sha256"],
            "duration": round(item["duration"], 4),
        }
        for item in prepared
    ])

    rng = random.Random(int(seed[:16], 16))
    plan = {}
    recent = []

    for item in prepared:
        choices = list(EFFECTS)
        depth = depth_maps.get(item["number"])

        if depth:
            choices.extend(["parallax", "parallax"])

        available = [
            effect for effect in choices
            if effect not in recent
        ]

        if not available:
            available = choices

        effect = rng.choice(available)
        plan[item["number"]] = effect
        recent = (recent + [effect])[-2:]

    return plan


def make_filter(effect, frames, width, height, fps):
    denominator = max(frames - 1, 1)
    source_width = width * 2
    source_height = height * 2

    zoom = "1.12"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"

    if effect == "zoom_in":
        zoom = f"min(1+0.16*on/{denominator},1.16)"

    elif effect == "zoom_out":
        zoom = f"max(1.16-0.16*on/{denominator},1.0)"

    elif effect in {"pan_left", "pan_right"}:
        zoom = "1.16"
        progress = (
            f"(1-on/{denominator})"
            if effect == "pan_left"
            else f"on/{denominator}"
        )
        x = f"(iw-iw/zoom)*{progress}"

    elif effect in {"pan_up", "pan_down"}:
        zoom = "1.16"
        progress = (
            f"(1-on/{denominator})"
            if effect == "pan_up"
            else f"on/{denominator}"
        )
        y = f"(ih-ih/zoom)*{progress}"

    elif effect in {"tilt_up", "tilt_down"}:
        zoom = "1.13"
        progress = (
            f"(1-on/{denominator})"
            if effect == "tilt_up"
            else f"on/{denominator}"
        )
        y = f"(ih-ih/zoom)*{progress}"

    elif effect == "diagonal_drift":
        zoom = "1.16"
        x = f"(iw-iw/zoom)*on/{denominator}"
        y = f"(ih-ih/zoom)*(1-on/{denominator})"

    elif effect == "diagonal_push":
        zoom = f"min(1+0.13*on/{denominator},1.13)"
        x = f"(iw-iw/zoom)*(0.18+0.64*on/{denominator})"
        y = f"(ih-ih/zoom)*(0.82-0.64*on/{denominator})"

    elif effect.startswith("zoom_pan_"):
        zoom = f"min(1+0.14*on/{denominator},1.14)"
        direction = effect.rsplit("_", 1)[-1]

        if direction == "left":
            x = f"(iw-iw/zoom)*(1-on/{denominator})"
        elif direction == "right":
            x = f"(iw-iw/zoom)*on/{denominator}"
        elif direction == "up":
            y = f"(ih-ih/zoom)*(1-on/{denominator})"
        elif direction == "down":
            y = f"(ih-ih/zoom)*on/{denominator}"

    elif effect == "handheld_drift":
        zoom = "1.12"
        x = "(iw-iw/zoom)/2+4*sin(on*0.11)"
        y = "(ih-ih/zoom)/2+3*sin(on*0.08)"

    elif effect == "cinematic_sweep":
        zoom = f"1.06+0.06*sin(PI*on/{denominator})"
        x = f"(iw-iw/zoom)*(0.5-0.42*cos(PI*on/{denominator}))"
        y = f"(ih-ih/zoom)*(0.5+0.28*sin(PI*on/{denominator}))"

    elif effect == "dutch_angle":
        zoom = "1.12"

    else:
        raise RuntimeError(f"Unsupported 2D motion effect: {effect}")

    filters = [
        f"scale={source_width}:{source_height}:force_original_aspect_ratio=increase",
        f"crop={source_width}:{source_height}",
    ]

    if effect == "dutch_angle":
        filters.append(
            "rotate='0.014*sin(2*PI*t/5)':"
            "ow=rotw(0.014):oh=roth(0.014):c=black"
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
        raise RuntimeError(f"Rendered video is missing or too small: {path}")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate,avg_frame_rate:"
            "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])

        video = next(
            (
                stream for stream in streams
                if stream.get("codec_type") == "video"
            ),
            None,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid media metadata for {path.name}"
        ) from exc

    if video is None:
        raise RuntimeError(f"No video stream in {path.name}")

    actual_width = int(video.get("width", 0))
    actual_height = int(video.get("height", 0))

    if (actual_width, actual_height) != (width, height):
        raise RuntimeError(
            f"{path.name}: expected {width}x{height}, "
            f"got {actual_width}x{actual_height}"
        )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration in {path.name}")

    tolerance = max(0.12, 2.0 / fps)

    if abs(duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"{path.name}: duration {duration:.3f}s; "
            f"expected {expected_duration:.3f}s"
        )

    rate = (
        video.get("avg_frame_rate")
        or video.get("r_frame_rate")
        or ""
    )

    try:
        if "/" in rate:
            numerator, denominator = rate.split("/", 1)
            actual_fps = float(numerator) / float(denominator)
        else:
            actual_fps = float(rate)

        if (
            not math.isfinite(actual_fps)
            or abs(actual_fps - fps) > 0.5
        ):
            raise RuntimeError(
                f"{path.name}: FPS {actual_fps}; expected {fps}"
            )
    except (ValueError, ZeroDivisionError):
        raise RuntimeError(
            f"Invalid frame rate in {path.name}: {rate!r}"
        )

    return duration


def make_signature(
    item,
    effect,
    depth,
    frames,
    fps,
    mode,
    width,
    height,
):
    renderer_hash = sha256_file(Path(__file__))
    parallax_script = ROOT / "scripts" / "depth_parallax.py"

    parallax_hash = (
        sha256_file(parallax_script)
        if effect == "parallax" and parallax_script.is_file()
        else None
    )

    return digest_data({
        "version": VERSION,
        "renderer_sha256": renderer_hash,
        "parallax_sha256": parallax_hash,
        "image_sha256": item["image_sha256"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "duration": round(item["duration"], 5),
        "frames": frames,
        "effect": effect,
        "depth_sha256": (
            depth["depth_sha256"] if depth else None
        ),
        "depth_model": depth["model"] if depth else None,
        "width": width,
        "height": height,
        "fps": fps,
        "format": mode,
        "crf": CRF,
        "preset": PRESET,
    })


def try_reuse(
    item,
    previous,
    signature,
    ffprobe,
    expected_duration,
    fps,
    width,
    height,
):
    if not isinstance(previous, dict):
        return None

    if previous.get("cache_signature") != signature:
        return None

    try:
        path = safe_path(
            previous.get("video"),
            "cached scene video",
        )

        if path != item["target"]:
            return None

        actual_hash = sha256_file(path)

        if previous.get("video_sha256") != actual_hash:
            return None

        actual_duration = probe_clip(
            path,
            ffprobe,
            width,
            height,
            expected_duration,
            fps,
        )

        row = dict(previous)
        row.update({
            "scene": item["number"],
            "global_scene": item["number"],
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "video": path.relative_to(ROOT).as_posix(),
            "video_sha256": actual_hash,
            "duration": round(actual_duration, 5),
            "size_bytes": path.stat().st_size,
            "width": width,
            "height": height,
            "fps": fps,
            "format": row.get("format"),
            "status": "completed",
        })

        return row

    except (OSError, ValueError, RuntimeError):
        return None


def render_parallax(
    item,
    depth,
    temporary,
    frames,
    fps,
    width,
    height,
):
    script = ROOT / "scripts" / "depth_parallax.py"

    if not script.is_file():
        raise RuntimeError(
            "Depth parallax renderer is missing: scripts/depth_parallax.py"
        )

    run_command(
        [
            sys.executable,
            str(script),
            "--image", str(item["image"]),
            "--depth", str(depth["path"]),
            "--output", str(temporary),
            "--width", str(width),
            "--height", str(height),
            "--fps", str(fps),
            "--frames", str(frames),
            "--strength", "0.026",
        ],
        f"Depth parallax scene {item['number']}",
        timeout=1800,
    )


def render_2d(
    item,
    effect,
    temporary,
    frames,
    fps,
    width,
    height,
    ffmpeg,
):
    filters = make_filter(
        effect,
        frames,
        width,
        height,
        fps,
    )

    run_command(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-loop", "1",
            "-framerate", str(fps),
            "-i", str(item["image"]),
            "-vf", filters,
            "-frames:v", str(frames),
            "-an",
            "-c:v", "libx264",
            "-preset", PRESET,
            "-crf", str(CRF),
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-fps_mode", "cfr",
            "-movflags", "+faststart",
            str(temporary),
        ],
        f"2D Photo Motion scene {item['number']}",
        timeout=1800,
    )


def build_manifest(
    entries,
    mode,
    width,
    height,
    fps,
    expected_count,
    status,
    error=None,
):
    rows = [
        entries[number]
        for number in sorted(entries)
    ]

    completed = sum(
        row.get("status") == "completed"
        for row in rows
    )

    document = {
        "status": status,
        "version": VERSION,
        "renderer": "photo_motion.py",
        "format": mode,
        "width": width,
        "height": height,
        "fps": fps,
        "expected_scene_count": expected_count,
        "completed_count": completed,
        "total_scenes": expected_count,
        "scenes": rows,
        "depth_parallax_count": sum(
            row.get("effect_is_depth_guided") is True
            for row in rows
        ),
        "ai_depth_parallax_count": sum(
            row.get("effect_uses_ai_depth") is True
            for row in rows
        ),
        "approximate_depth_parallax_count": sum(
            row.get("depth_is_approximate_fallback") is True
            for row in rows
        ),
        "two_d_motion_count": sum(
            row.get("effect_is_depth_guided") is False
            for row in rows
        ),
    }

    if error:
        document["last_error"] = str(error)[:2000]

    return document


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    configured_parts = positive_int(
        get_parts(config),
        "PARTS",
    )
    scenes_per_part = positive_int(
        get_scenes(config),
        "SCENES",
    )
    fps = positive_int(
        get_fps(config),
        "FPS",
    )

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    if not 1 <= fps <= 60:
        raise RuntimeError(f"FPS must be between 1 and 60: {fps}")

    if mode == "short":
        width, height = 720, 1280
    else:
        width, height = 1280, 720

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe are required")

    RENDER_DIR.mkdir(parents=True, exist_ok=True)

    expected_count = configured_parts * scenes_per_part

    prepared = prepare_jobs(
        load_job_rows(),
        configured_parts,
        scenes_per_part,
    )

    depth_maps = read_depth_maps(prepared)
    effect_plan = build_effect_plan(prepared, depth_maps)
    previous = previous_entries()

    expected_numbers = {
        item["number"] for item in prepared
    }

    entries = {
        number: row
        for number, row in previous.items()
        if number in expected_numbers
    }

    def checkpoint(status="in_progress", error=None):
        write_json(
            MANIFEST_FILE,
            build_manifest(
                entries,
                mode,
                width,
                height,
                fps,
                expected_count,
                status,
                error,
            ),
        )

    checkpoint()

    generated = 0
    reused = 0
    ai_parallax = 0
    approximate_parallax = 0
    two_d_motion = 0

    log("=" * 60)
    log("PHOTO MOTION VERSION:", VERSION)
    log("FORMAT:", mode)
    log("RESOLUTION:", f"{width}x{height}")
    log("FPS:", fps)
    log("PARTS:", configured_parts)
    log("SCENES PER PART:", scenes_per_part)
    log("TOTAL SCENES:", expected_count)
    log("VALID DEPTH MAPS:", len(depth_maps))
    log("=" * 60)

    try:
        for index, item in enumerate(prepared, 1):
            number = item["number"]
            frames = max(1, round(item["duration"] * fps))
            expected_duration = frames / fps

            depth = depth_maps.get(number)
            effect = effect_plan[number]

            if effect == "parallax" and depth is None:
                effect = EFFECTS[(index - 1) % len(EFFECTS)]

            used_depth = (
                depth if effect == "parallax" else None
            )

            signature = make_signature(
                item,
                effect,
                used_depth,
                frames,
                fps,
                mode,
                width,
                height,
            )

            cached = try_reuse(
                item,
                previous.get(number),
                signature,
                ffprobe,
                expected_duration,
                fps,
                width,
                height,
            )

            if cached:
                entries[number] = cached
                reused += 1
                checkpoint()
                log(
                    f"REUSED {index}/{expected_count}: "
                    f"scene {number}"
                )
                continue

            target = item["target"]
            temporary = target.with_name(
                target.stem + ".rendering.mp4"
            )

            temporary.unlink(missing_ok=True)

            try:
                if effect == "parallax" and used_depth:
                    render_parallax(
                        item,
                        used_depth,
                        temporary,
                        frames,
                        fps,
                        width,
                        height,
                    )
                else:
                    render_2d(
                        item,
                        effect,
                        temporary,
                        frames,
                        fps,
                        width,
                        height,
                        ffmpeg,
                    )

                actual_duration = probe_clip(
                    temporary,
                    ffprobe,
                    width,
                    height,
                    expected_duration,
                    fps,
                )

                if target.exists():
                    target.unlink()

                temporary.replace(target)

            except Exception:
                temporary.unlink(missing_ok=True)
                raise

            video_hash = sha256_file(target)
            is_ai_depth = bool(
                used_depth and used_depth["is_ai_depth"]
            )
            is_approximate_depth = bool(
                used_depth and not used_depth["is_ai_depth"]
            )

            row = {
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
                "depth_map_available": depth is not None,
                "depth_model": (
                    used_depth["model"] if used_depth else None
                ),
                "depth_is_ai_estimated": is_ai_depth,
                "depth_is_approximate_fallback": is_approximate_depth,
                "effect": effect,
                "effect_is_depth_guided": effect == "parallax",
                "effect_uses_ai_depth": is_ai_depth,
                "duration": round(actual_duration, 5),
                "requested_duration": item["duration"],
                "frames": frames,
                "width": width,
                "height": height,
                "fps": fps,
                "format": mode,
                "size_bytes": target.stat().st_size,
                "cache_signature": signature,
                "renderer_version": VERSION,
                "status": "completed",
            }

            entries[number] = row
            generated += 1

            ai_parallax += int(is_ai_depth)
            approximate_parallax += int(is_approximate_depth)
            two_d_motion += int(effect != "parallax")

            checkpoint()

            log(
                f"COMPLETED {index}/{expected_count}: "
                f"part={item['part']} scene={item['local_scene']} "
                f"effect={effect} "
                f"depth={'AI' if is_ai_depth else 'approximate' if is_approximate_depth else 'not used'}"
            )

        if set(entries) != expected_numbers:
            missing = sorted(expected_numbers - set(entries))
            raise RuntimeError(
                f"Photo Motion output is missing scenes: {missing}"
            )

        verified_rows = []

        for item in prepared:
            number = item["number"]
            row = entries.get(number)

            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Scene {number} manifest row is missing"
                )

            if row.get("status") != "completed":
                raise RuntimeError(
                    f"Scene {number} is not marked completed"
                )

            if row.get("image_sha256") != item["image_sha256"]:
                raise RuntimeError(
                    f"Scene {number} image checksum mismatch"
                )

            target = safe_path(
                row.get("video"),
                f"scene {number} rendered video",
            )

            if target != item["target"]:
                raise RuntimeError(
                    f"Scene {number} has an unexpected video path"
                )

            frames = max(1, round(item["duration"] * fps))
            expected_duration = frames / fps

            probe_clip(
                target,
                ffprobe,
                width,
                height,
                expected_duration,
                fps,
            )

            actual_hash = sha256_file(target)

            if row.get("video_sha256") != actual_hash:
                raise RuntimeError(
                    f"Scene {number} video checksum mismatch"
                )

            verified_rows.append(row)

        if len(verified_rows) != expected_count:
            raise RuntimeError(
                "Final Photo Motion scene count validation failed"
            )

        checkpoint(status="completed")

    except Exception as exc:
        checkpoint(status="failed", error=exc)
        raise

    log("=" * 60)
    log("PHOTO MOTION COMPLETE")
    log("FORMAT:", mode)
    log("PARTS:", configured_parts)
    log("SCENES PER PART:", scenes_per_part)
    log("TOTAL SCENES:", expected_count)
    log("NEW CLIPS:", generated)
    log("REUSED CLIPS:", reused)
    log("AI DEPTH PARALLAX:", ai_parallax)
    log("APPROXIMATE DEPTH PARALLAX:", approximate_parallax)
    log("2D MOTION:", two_d_motion)
    log("MANIFEST:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("PHOTO MOTION CANCELLED")
        sys.exit(130)
    except Exception as exc:
        log("PHOTO MOTION FAILED:", str(exc))
        sys.exit(1)