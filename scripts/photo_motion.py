#!/usr/bin/env python3
"""Resumable format-aware cinematic photo-motion renderer."""

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

OUT = ROOT / "output"
VISUALS = OUT / "visuals"
JOBS_FILE = VISUALS / "visual_jobs.json"
DEPTH_MANIFEST = VISUALS / "depth_maps" / "depth_manifest.json"
OUTPUT_DIR = OUT / "photo_motion"
MANIFEST_FILE = OUTPUT_DIR / "photo_motion_manifest.json"

VERSION = "photo-motion-v8-depth-guided-checkpoints"
CRF = 20
PRESET = "medium"
MIN_IMAGE_BYTES = 1000
MIN_VIDEO_BYTES = 1000
MIN_DEPTH_BYTES = 100

EFFECTS = (
    "zoom_in",
    "pan_left",
    "cinematic_sweep",
    "zoom_out",
    "pan_right",
    "tilt_up",
    "diagonal_drift",
    "handheld_drift",
    "tilt_down",
    "dutch_angle",
)


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


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


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing path for {label}")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory")

    return path


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if result < 1:
        raise RuntimeError(f"{label} must be positive")
    return result


def run_command(command, timeout=900):
    log("$", " ".join(map(str, command)))
    result = subprocess.run(
        [str(item) for item in command],
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
            "No visual jobs found. Generate visuals and images first."
        )
    return rows


def scene_number(row, index):
    value = row.get("global_scene", row.get("scene", index))
    if isinstance(value, dict):
        value = value.get("global_scene", value.get("number"))
    return positive_int(value, f"global scene number in row {index}")


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
        "Each scene requires a valid duration between 0.5 and 3600 seconds"
    )


def prepare_jobs(rows):
    from PIL import Image

    prepared = []
    global_seen = set()
    local_seen = set()

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Visual job {index} is not an object")

        number = scene_number(row, index)
        part = positive_int(row.get("part", 1), "part number")
        local = positive_int(
            row.get("local_scene", row.get("scene", number)),
            "local scene number",
        )

        if number in global_seen:
            raise RuntimeError(f"Duplicate global scene number: {number}")
        if (part, local) in local_seen:
            raise RuntimeError(
                f"Duplicate part/local scene mapping: {(part, local)}"
            )

        global_seen.add(number)
        local_seen.add((part, local))

        image = safe_path(
            row.get("image_path") or row.get("image"),
            f"scene {number} image",
        )

        if not image.is_file() or image.stat().st_size < MIN_IMAGE_BYTES:
            raise RuntimeError(f"Missing or too-small scene image: {image}")

        try:
            with Image.open(image) as opened:
                opened.verify()
            with Image.open(image) as opened:
                width, height = opened.size
                if width < 64 or height < 64:
                    raise ValueError("Image is smaller than 64x64")
                if opened.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError(
                        f"Unsupported image format: {opened.format}"
                    )
        except Exception as exc:
            raise RuntimeError(
                f"Cannot validate image for scene {number}: {exc}"
            ) from exc

        prepared.append({
            "number": number,
            "part": part,
            "local_scene": local,
            "image": image,
            "image_sha256": sha256_file(image),
            "image_width": width,
            "image_height": height,
            "duration": scene_duration(row),
            "target": OUTPUT_DIR / f"scene_{number:04d}.mp4",
        })

    prepared.sort(key=lambda item: item["number"])
    numbers = [item["number"] for item in prepared]

    if numbers != list(range(1, len(prepared) + 1)):
        raise RuntimeError(
            "Global scene numbers must be continuous from 1; "
            f"found {numbers}"
        )

    expected_pairs = [
        (item["part"], item["local_scene"]) for item in prepared
    ]
    if expected_pairs != sorted(expected_pairs):
        raise RuntimeError("Scene order must be part-then-local-scene order")

    return prepared


def read_depth_maps(prepared):
    data = read_json(DEPTH_MANIFEST)
    if data is None:
        log("NOTICE: No depth manifest; using 2D motion.")
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
        rows = []

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
                "depth manifest scene number",
            )
            path = safe_path(
                row.get("depth_map") or row.get("path") or row.get("file"),
                f"depth map scene {number}",
            )
        except (TypeError, ValueError, RuntimeError):
            continue

        if number not in expected:
            continue

        if not path.is_file() or path.stat().st_size < MIN_DEPTH_BYTES:
            log(f"WARNING: Missing or undersized depth map for scene {number}")
            continue

        try:
            with Image.open(path) as opened:
                opened.verify()
            with Image.open(path) as opened:
                if opened.width < 64 or opened.height < 64:
                    raise ValueError("Depth map is too small")
                if opened.mode not in ("1", "L", "I", "I;16", "F"):
                    opened.convert("L")
        except Exception as exc:
            log(f"WARNING: Invalid depth map for scene {number}: {exc}")
            continue

        source_hash = row.get("image_sha256")
        if source_hash != expected[number]:
            log(f"WARNING: Stale depth map ignored for scene {number}")
            continue

        actual_depth_hash = sha256_file(path)
        expected_depth_hash = row.get("depth_sha256")

        if (
            expected_depth_hash
            and expected_depth_hash != actual_depth_hash
        ):
            log(f"WARNING: Depth checksum mismatch for scene {number}")
            continue

        model = str(row.get("model", "unknown"))
        status = str(row.get("status", "unknown"))

        ai_depth = (
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
            "model": model,
            "status": status,
            "is_ai_depth": ai_depth,
        }

    log("Validated depth maps:", len(result))
    log(
        "AI depth maps:",
        sum(1 for item in result.values() if item["is_ai_depth"]),
    )
    log(
        "Approximate depth maps:",
        sum(1 for item in result.values() if not item["is_ai_depth"]),
    )
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


def write_manifest(entries, mode, width, height, fps, expected_count, status):
    rows = [entries[number] for number in sorted(entries)]

    atomic_json(
        MANIFEST_FILE,
        {
            "status": status,
            "version": VERSION,
            "format": mode,
            "width": width,
            "height": height,
            "fps": fps,
            "expected_scene_count": expected_count,
            "completed_count": sum(
                row.get("status") == "completed" for row in rows
            ),
            "ai_depth_parallax_count": sum(
                row.get("effect_uses_ai_depth") is True for row in rows
            ),
            "approximate_depth_parallax_count": sum(
                row.get("depth_is_approximate_fallback") is True
                for row in rows
            ),
            "scenes": rows,
        },
    )


def choose_effect(index, depth):
    """
    Use real AI depth more often; do not mislabel pseudo-depth as AI.
    Approximate depth is used more sparingly.
    """
    if depth and depth["is_ai_depth"] and index % 3 == 1:
        return "parallax"

    if depth and not depth["is_ai_depth"] and index % 7 == 4:
        return "parallax"

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
        f"scale={source_width}:{source_height}:force_original_aspect_ratio=increase",
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
        raise RuntimeError(f"Missing or too-small video: {path}")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate:format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-600:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = [
            row for row in data.get("streams", [])
            if row.get("codec_type") == "video"
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid media metadata for {path.name}"
        ) from exc

    if not streams:
        raise RuntimeError(f"No video stream in {path.name}")

    stream = streams[0]
    actual_size = (
        int(stream.get("width", 0)),
        int(stream.get("height", 0)),
    )

    if actual_size != (width, height):
        raise RuntimeError(
            f"{path.name}: expected {width}x{height}, got {actual_size}"
        )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration in {path.name}")

    if abs(duration - expected_duration) > max(0.12, 2 / fps):
        raise RuntimeError(
            f"{path.name}: duration {duration:.3f}s; "
            f"expected {expected_duration:.3f}s"
        )

    rate = stream.get("r_frame_rate", "")
    if isinstance(rate, str) and "/" in rate:
        try:
            numerator, denominator = rate.split("/", 1)
            actual_fps = float(numerator) / float(denominator)
            if (
                not math.isfinite(actual_fps)
                or abs(actual_fps - fps) > 0.5
            ):
                raise RuntimeError(
                    f"{path.name}: FPS {actual_fps}; expected {fps}"
                )
        except (ValueError, ZeroDivisionError):
            raise RuntimeError(f"Invalid frame rate in {path.name}")

    return duration


def signature(item, effect, depth, frames, fps, mode, width, height):
    payload = {
        "version": VERSION,
        "renderer_sha256": sha256_file(Path(__file__)),
        "parallax_sha256": (
            sha256_file(ROOT / "scripts" / "depth_parallax.py")
            if effect == "parallax"
            and (ROOT / "scripts" / "depth_parallax.py").is_file()
            else None
        ),
        "image_sha256": item["image_sha256"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "frames": frames,
        "effect": effect,
        "depth_sha256": depth["depth_sha256"] if depth else None,
        "depth_model": depth["model"] if depth else None,
        "width": width,
        "height": height,
        "fps": fps,
        "format": mode,
        "crf": CRF,
        "preset": PRESET,
    }

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def try_reuse(
    item,
    old,
    sig,
    ffprobe,
    duration,
    fps,
    width,
    height,
):
    if not isinstance(old, dict):
        return None

    if old.get("cache_signature") != sig:
        return None

    try:
        path = safe_path(old.get("video"), "cached scene video")

        if path != item["target"] or not path.is_file():
            return None

        actual_hash = sha256_file(path)
        if old.get("video_sha256") != actual_hash:
            return None

        actual_duration = probe_clip(
            path,
            ffprobe,
            width,
            height,
            duration,
            fps,
        )

        row = dict(old)
        row.update({
            "scene": item["number"],
            "global_scene": item["number"],
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "video": path.relative_to(ROOT).as_posix(),
            "video_sha256": actual_hash,
            "duration": actual_duration,
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


def render_parallax(item, depth_path, target, frames, fps, width, height):
    script = ROOT / "scripts" / "depth_parallax.py"

    if not script.is_file():
        raise RuntimeError(f"Missing parallax renderer: {script}")

    run_command([
        sys.executable,
        str(script),
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
    mode = str(get_format(config)).strip().lower()
    fps = positive_int(get_fps(config), "FPS")

    if mode == "short":
        width, height = 720, 1280
    elif mode == "full":
        width, height = 1280, 720
    else:
        raise RuntimeError(f"Unsupported video format: {mode}")

    if not 1 <= fps <= 60:
        raise RuntimeError(f"FPS must be between 1 and 60, got {fps}")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe are required")

    prepared = prepare_jobs(read_jobs())
    depth_maps = read_depth_maps(prepared)
    previous = previous_entries()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    expected_numbers = {item["number"] for item in prepared}
    entries = {
        number: row
        for number, row in previous.items()
        if number in expected_numbers
    }

    def checkpoint(status="in_progress"):
        write_manifest(
            entries,
            mode,
            width,
            height,
            fps,
            len(prepared),
            status,
        )

    checkpoint()
    generated = 0
    reused = 0
    ai_parallax = 0
    approximate_parallax = 0
    two_d_motion = 0

    log("PHOTO MOTION:", VERSION)
    log("FORMAT:", mode, "| SIZE:", f"{width}x{height}", "| FPS:", fps)
    log("SCENES:", len(prepared))
    log("DEPTH MAPS:", len(depth_maps))

    for index, item in enumerate(prepared):
        number = item["number"]
        frames = max(1, round(item["duration"] * fps))
        duration = frames / fps
        depth = depth_maps.get(number)

        effect = choose_effect(index, depth)

        if effect == "parallax" and depth is None:
            effect = EFFECTS[index % len(EFFECTS)]

        used_depth = depth if effect == "parallax" else None

        sig = signature(
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
            sig,
            ffprobe,
            duration,
            fps,
            width,
            height,
        )

        if cached:
            entries[number] = cached
            reused += 1
            checkpoint()
            log(f"Scene {number}: reused verified clip")
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
                    used_depth["path"],
                    temporary,
                    frames,
                    fps,
                    width,
                    height,
                )
            else:
                run_command([
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-loop", "1",
                    "-framerate", str(fps),
                    "-i", str(item["image"]),
                    "-vf", make_filter(
                        effect,
                        frames,
                        width,
                        height,
                        fps,
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
                temporary,
                ffprobe,
                width,
                height,
                duration,
                fps,
            )
            temporary.replace(target)

        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        video_hash = sha256_file(target)
        ai_depth = bool(
            used_depth and used_depth["is_ai_depth"]
        )
        fallback_depth = bool(
            used_depth and not used_depth["is_ai_depth"]
        )

        entries[number] = {
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
            "depth_is_ai_estimated": ai_depth,
            "depth_is_approximate_fallback": fallback_depth,
            "effect": effect,
            "effect_is_depth_guided": effect == "parallax",
            "effect_uses_ai_depth": ai_depth,
            "duration": actual_duration,
            "requested_duration": item["duration"],
            "frames": frames,
            "width": width,
            "height": height,
            "fps": fps,
            "format": mode,
            "size_bytes": target.stat().st_size,
            "cache_signature": sig,
            "renderer_version": VERSION,
            "status": "completed",
        }

        generated += 1
        ai_parallax += int(ai_depth)
        approximate_parallax += int(fallback_depth)
        two_d_motion += int(effect != "parallax")

        checkpoint()
        log(
            f"Scene {number}: checkpoint saved; "
            f"effect={effect}; "
            f"depth={'AI' if ai_depth else 'approximate' if fallback_depth else 'unused'}"
        )

    if set(entries) != expected_numbers:
        missing = sorted(expected_numbers - set(entries))
        raise RuntimeError(f"Scene manifest is missing scenes: {missing}")

    for item in prepared:
        number = item["number"]
        row = entries.get(number)

        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Scene {number} is not complete")

        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(f"Scene {number} image checksum mismatch")

        target = safe_path(
            row.get("video"),
            f"scene {number} output",
        )

        if target != item["target"]:
            raise RuntimeError(
                f"Scene {number} has an unexpected output path"
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

        if row.get("video_sha256") != sha256_file(target):
            raise RuntimeError(
                f"Scene {number} video checksum mismatch"
            )

    checkpoint(status="completed")

    log("=" * 55)
    log("PHOTO MOTION COMPLETE")
    log("SCENES:", len(prepared))
    log("NEW CLIPS:", generated)
    log("REUSED CLIPS:", reused)
    log("AI-DEPTH PARALLAX CLIPS:", ai_parallax)
    log("APPROXIMATE-DEPTH PARALLAX CLIPS:", approximate_parallax)
    log("2D MOTION CLIPS:", two_d_motion)
    log("MANIFEST:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("PHOTO MOTION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"PHOTO MOTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)