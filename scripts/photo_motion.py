#!/usr/bin/env python3
"""Validated, resumable, role-aware cinematic Photo Motion renderer."""

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
JOBS_FILE = VISUALS / "photo_motion_jobs.json"
LEGACY_JOBS_FILE = VISUALS / "visual_jobs.json"
DEPTH_FILE = VISUALS / "depth_maps" / "depth_manifest.json"

RENDER_DIR = OUTPUT / "photo_motion"
MANIFEST_FILE = RENDER_DIR / "photo_motion_manifest.json"
CHECKPOINT_FILE = OUTPUT / "checkpoints" / "photo_motion_progress.json"

VERSION = "photo-motion-v12-validated-role-resume"
CRF = 19
PRESET = "medium"

MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100
MIN_VIDEO_BYTES = 1000
MIN_DURATION = 0.5
MAX_DURATION = 3600

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

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


def digest_data(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


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
    if isinstance(value, bool):
        raise RuntimeError(f"Invalid {label}: {value!r}")

    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if result < 1:
        raise RuntimeError(f"{label} must be at least 1")

    return result


def valid_duration(value):
    if isinstance(value, bool) or value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if (
        not math.isfinite(number)
        or not MIN_DURATION <= number <= MAX_DURATION
    ):
        return None

    return number


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


def normalize_role(row):
    value = str(
        row.get("role")
        or row.get("scene_type")
        or row.get("type")
        or "main"
    ).strip().lower()

    aliases = {
        "opening": "hook",
        "intro": "hook",
        "part_hook": "hook",
        "main_scene": "main",
        "story": "main",
        "cliffhanger": "suspense",
        "ending": "resolution",
        "final": "resolution",
    }

    value = aliases.get(value, value)

    if value not in ROLE_ORDER:
        raise RuntimeError(
            f"Unsupported Photo Motion scene role: {value!r}"
        )

    return value


def load_job_document():
    if JOBS_FILE.is_file():
        source = JOBS_FILE
    elif LEGACY_JOBS_FILE.is_file():
        source = LEGACY_JOBS_FILE
    else:
        raise RuntimeError(
            "Photo Motion jobs are missing. "
            "Expected output/visuals/photo_motion_jobs.json."
        )

    document = read_json(source)

    if isinstance(document, list):
        rows = document
        metadata = {}
    elif isinstance(document, dict):
        rows = (
            document.get("jobs")
            or document.get("photo_motion_jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
            or []
        )
        metadata = document
    else:
        raise RuntimeError(f"{source.name} has invalid structure")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{source.name} contains no jobs")

    return metadata, rows, source


def image_dimensions(path, scene_id):
    from PIL import Image

    if (
        not path.is_file()
        or path.stat().st_size < MIN_IMAGE_BYTES
    ):
        raise RuntimeError(
            f"{scene_id}: image missing or too small: {path}"
        )

    try:
        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            width, height = image.size
            image_format = (image.format or "").upper()

    except Exception as exc:
        raise RuntimeError(
            f"{scene_id}: invalid image: {exc}"
        ) from exc

    if width < 64 or height < 64:
        raise RuntimeError(
            f"{scene_id}: image dimensions below 64x64"
        )

    if image_format not in {"PNG", "JPEG", "WEBP"}:
        raise RuntimeError(
            f"{scene_id}: unsupported image format {image_format}"
        )

    return width, height


def resolve_output_target(row, global_number):
    part = positive_int(row.get("part"), "part")

    scene_id = str(
        row.get("scene_id")
        or row.get("id")
        or f"part_{part:02d}_scene_{global_number:04d}"
    ).strip()

    safe_id = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in scene_id
    )

    if not safe_id:
        safe_id = f"scene_{global_number:04d}"

    folder = RENDER_DIR / f"part_{part:02d}"
    return folder / f"{safe_id}.mp4"


def resolve_duration(row):
    for key in (
        "audio_duration",
        "narration_duration",
        "measured_duration",
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds",
    ):
        value = valid_duration(row.get(key))
        if value is not None:
            return value

    audio = row.get("audio")
    if isinstance(audio, dict):
        for key in ("duration", "duration_seconds", "seconds"):
            value = valid_duration(audio.get(key))
            if value is not None:
                return value

    return None


def prepare_jobs(rows, expected_parts, scenes_per_part):
    prepared = []
    seen_ids = set()
    seen_global = set()
    main_mapping = set()

    role_counts = {
        role: {} for role in ROLE_ORDER
    }

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Photo Motion job {index} is not an object"
            )

        part = positive_int(row.get("part"), "part")

        if part > expected_parts:
            raise RuntimeError(
                f"Job {index}: part {part} exceeds configured parts "
                f"{expected_parts}"
            )

        role = normalize_role(row)
        scene_id = str(
            row.get("scene_id") or row.get("id") or ""
        ).strip()

        if not scene_id:
            raise RuntimeError(f"Job {index}: scene_id is missing")

        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate scene_id: {scene_id}")

        seen_ids.add(scene_id)

        global_number = positive_int(
            row.get(
                "global_scene",
                row.get("global_scene_number", index),
            ),
            "global scene number",
        )

        if global_number in seen_global:
            raise RuntimeError(
                f"Duplicate global job number: {global_number}"
            )

        seen_global.add(global_number)

        local_slot = positive_int(
            row.get("local_scene", row.get("scene", index)),
            "local image slot",
        )

        source_scene = row.get(
            "source_scene_number",
            row.get("source_local_scene"),
        )

        if role == "main":
            source_scene = positive_int(
                source_scene if source_scene is not None else local_slot,
                "main source scene number",
            )

            if source_scene > scenes_per_part:
                raise RuntimeError(
                    f"{scene_id}: main scene number exceeds SCENES"
                )

            pair = (part, source_scene)

            if pair in main_mapping:
                raise RuntimeError(
                    f"Duplicate main scene mapping: {pair}"
                )

            main_mapping.add(pair)

        elif source_scene not in (None, ""):
            source_scene = positive_int(
                source_scene,
                "extra scene source number",
            )
        else:
            source_scene = None

        image = safe_path(
            row.get("image_path")
            or row.get("image")
            or row.get("output_path"),
            f"{scene_id} image",
        )

        width, height = image_dimensions(image, scene_id)
        duration = resolve_duration(row)
        target = resolve_output_target(row, global_number)

        prepared.append({
            "number": global_number,
            "scene_id": scene_id,
            "part": part,
            "role": role,
            "local_scene": local_slot,
            "source_scene_number": source_scene,
            "image": image,
            "image_sha256": sha256_file(image),
            "image_width": width,
            "image_height": height,
            "duration": duration,
            "duration_source": (
                "measured_narration"
                if duration is not None
                else "awaiting_narration_timing"
            ),
            "target": target,
            "source_job": dict(row),
        })

        role_counts[role][part] = (
            role_counts[role].get(part, 0) + 1
        )

    expected_main = {
        (part, scene)
        for part in range(1, expected_parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    if main_mapping != expected_main:
        missing = sorted(expected_main - main_mapping)
        extra = sorted(main_mapping - expected_main)
        raise RuntimeError(
            "Main scene mapping mismatch. "
            f"Missing={missing[:20]}, extra={extra[:20]}"
        )

    for part in range(1, expected_parts + 1):
        if role_counts["hook"].get(part, 0) != 1:
            raise RuntimeError(
                f"Part {part} must have exactly one Hook job"
            )

        if part < expected_parts:
            if role_counts["suspense"].get(part, 0) != 1:
                raise RuntimeError(
                    f"Part {part} must have exactly one Suspense job"
                )

            if role_counts["resolution"].get(part, 0):
                raise RuntimeError(
                    f"Non-final part {part} cannot have Resolution"
                )
        else:
            if role_counts["resolution"].get(part, 0) != 1:
                raise RuntimeError(
                    "Final part must have exactly one Resolution job"
                )

            if role_counts["suspense"].get(part, 0):
                raise RuntimeError(
                    "Final part cannot have Suspense"
                )

    prepared.sort(
        key=lambda item: (
            item["part"],
            ROLE_ORDER[item["role"]],
            item["source_scene_number"]
            if item["source_scene_number"] is not None
            else item["local_scene"],
            item["number"],
        )
    )

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
        raise RuntimeError("Depth manifest has invalid structure")

    if not isinstance(rows, list):
        raise RuntimeError("Depth manifest rows are invalid")

    from PIL import Image

    by_number = {
        item["number"]: item for item in prepared
    }

    by_scene_id = {
        item["scene_id"]: item for item in prepared
    }

    by_image_hash = {}

    for item in prepared:
        by_image_hash.setdefault(
            item["image_sha256"], []
        ).append(item)

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            scene_id = str(row.get("scene_id") or "")
            number = row.get(
                "global_scene",
                row.get("global_scene_number", row.get("scene")),
            )

            item = by_scene_id.get(scene_id) if scene_id else None

            if item is None and number is not None:
                try:
                    item = by_number.get(int(number))
                except (TypeError, ValueError):
                    item = None

            image_hash = str(row.get("image_sha256") or "")

            if item is None and image_hash:
                candidates = by_image_hash.get(image_hash, [])
                if len(candidates) == 1:
                    item = candidates[0]

            if item is None:
                continue

            if image_hash and image_hash != item["image_sha256"]:
                continue

            path = safe_path(
                row.get("depth_map")
                or row.get("depth_path")
                or row.get("path")
                or row.get("file"),
                f"depth map for {item['scene_id']}",
            )

            if path.stat().st_size < MIN_DEPTH_BYTES:
                continue

            with Image.open(path) as image:
                image.verify()

            with Image.open(path) as image:
                if image.width < 64 or image.height < 64:
                    continue

            actual_hash = sha256_file(path)
            declared_hash = row.get("depth_sha256")

            if declared_hash and declared_hash != actual_hash:
                continue

            model = str(row.get("model", "unknown"))
            status = str(row.get("status", "unknown")).lower()

            ai_depth = (
                "depth-anything" in model.lower()
                and "pseudo" not in status
                and "fallback" not in status
            )

            candidate = {
                "path": path,
                "depth_sha256": actual_hash,
                "model": model,
                "status": status,
                "is_ai_depth": ai_depth,
                "image_sha256": item["image_sha256"],
            }

            existing = result.get(item["scene_id"])

            if existing and existing["depth_sha256"] != actual_hash:
                raise RuntimeError(
                    f"Conflicting depth maps for {item['scene_id']}"
                )

            result[item["scene_id"]] = candidate

        except RuntimeError:
            raise
        except Exception as exc:
            log(f"WARNING: rejected depth row: {exc}")

    log("Validated depth maps:", len(result))
    return result


def previous_entries():
    data = read_json(MANIFEST_FILE, {})

    rows = data if isinstance(data, list) else (
        data.get("scenes", []) if isinstance(data, dict) else []
    )

    result = {}

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            key = str(row.get("scene_id") or "")

            if key:
                result[key] = row

    return result


def build_effect_plan(prepared, depth_maps):
    seed = digest_data([
        {
            "scene_id": item["scene_id"],
            "image": item["image_sha256"],
            "duration": item["duration"],
            "role": item["role"],
            "depth": (
                depth_maps[item["scene_id"]]["depth_sha256"]
                if item["scene_id"] in depth_maps else None
            ),
        }
        for item in prepared
    ])

    rng = random.Random(int(seed[:16], 16))
    plan = {}
    recent_by_part = {}

    for item in prepared:
        recent = recent_by_part.setdefault(item["part"], [])
        choices = list(EFFECTS)

        if item["scene_id"] in depth_maps:
            choices.extend(["parallax", "parallax", "parallax"])

        available = [
            effect for effect in choices
            if effect not in recent
        ] or choices

        effect = rng.choice(available)
        plan[item["scene_id"]] = effect
        recent_by_part[item["part"]] = (recent + [effect])[-2:]

    return plan


def make_filter(effect, frames, width, height, fps):
    denominator = max(frames - 1, 1)
    source_width = width * 2
    source_height = height * 2

    zoom = "1.12"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"

    if effect == "zoom_in":
        zoom = f"min(1+0.18*on/{denominator},1.18)"

    elif effect == "zoom_out":
        zoom = f"max(1.18-0.18*on/{denominator},1.0)"

    elif effect in {"pan_left", "pan_right"}:
        zoom = "1.18"
        progress = (
            f"(1-on/{denominator})"
            if effect == "pan_left" else f"on/{denominator}"
        )
        x = f"(iw-iw/zoom)*{progress}"

    elif effect in {"pan_up", "pan_down", "tilt_up", "tilt_down"}:
        zoom = "1.15" if effect.startswith("tilt") else "1.18"
        progress = (
            f"(1-on/{denominator})"
            if effect.endswith("up") else f"on/{denominator}"
        )
        y = f"(ih-ih/zoom)*{progress}"

    elif effect == "diagonal_drift":
        zoom = "1.18"
        x = f"(iw-iw/zoom)*on/{denominator}"
        y = f"(ih-ih/zoom)*(1-on/{denominator})"

    elif effect == "diagonal_push":
        zoom = f"min(1+0.16*on/{denominator},1.16)"
        x = f"(iw-iw/zoom)*(0.18+0.64*on/{denominator})"
        y = f"(ih-ih/zoom)*(0.82-0.64*on/{denominator})"

    elif effect.startswith("zoom_pan_"):
        zoom = f"min(1+0.16*on/{denominator},1.16)"
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
        zoom = "1.14"
        x = "(iw-iw/zoom)/2+5*sin(on*0.11)"
        y = "(ih-ih/zoom)/2+4*sin(on*0.08)"

    elif effect == "cinematic_sweep":
        zoom = f"1.07+0.07*sin(PI*on/{denominator})"
        x = f"(iw-iw/zoom)*(0.5-0.42*cos(PI*on/{denominator}))"
        y = f"(ih-ih/zoom)*(0.5+0.28*sin(PI*on/{denominator}))"

    elif effect == "dutch_angle":
        zoom = "1.14"

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
        raise RuntimeError(f"Rendered clip missing or too small: {path}")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate,avg_frame_rate:"
            "format=duration",
            "-of", "json", str(path),
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
        video = next(
            stream for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
        )
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise RuntimeError(
            f"Invalid media metadata for {path.name}"
        ) from exc

    if (
        int(video.get("width", 0)) != width
        or int(video.get("height", 0)) != height
    ):
        raise RuntimeError(
            f"{path.name}: expected {width}x{height}, "
            f"got {video.get('width')}x{video.get('height')}"
        )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid clip duration: {path.name}")

    tolerance = max(0.12, 2.0 / fps)

    if abs(duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"{path.name}: duration {duration:.3f}s; "
            f"expected {expected_duration:.3f}s"
        )

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or ""

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
        raise RuntimeError(f"Invalid FPS in {path.name}: {rate!r}")

    return duration


def make_signature(
    item, effect, depth, frames, fps, mode, width, height
):
    parallax_script = ROOT / "scripts" / "depth_parallax.py"

    parallax_hash = (
        sha256_file(parallax_script)
        if effect == "parallax" and parallax_script.is_file()
        else None
    )

    return digest_data({
        "version": VERSION,
        "renderer_sha256": sha256_file(Path(__file__)),
        "parallax_sha256": parallax_hash,
        "scene_id": item["scene_id"],
        "role": item["role"],
        "part": item["part"],
        "source_scene_number": item["source_scene_number"],
        "image_sha256": item["image_sha256"],
        "duration": round(item["duration"], 5),
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
    })


def try_reuse(
    item, previous, signature, ffprobe,
    duration, fps, width, height,
):
    if not isinstance(previous, dict):
        return None

    if previous.get("cache_signature") != signature:
        return None

    try:
        path = safe_path(
            previous.get("video"),
            "cached Photo Motion clip",
        )

        if path != item["target"]:
            return None

        actual_hash = sha256_file(path)

        if previous.get("video_sha256") != actual_hash:
            return None

        actual_duration = probe_clip(
            path, ffprobe, width, height, duration, fps
        )

        row = dict(previous)
        row.update({
            "scene_id": item["scene_id"],
            "role": item["role"],
            "scene_type": item["role"],
            "scene": item["number"],
            "global_scene": item["number"],
            "part": item["part"],
            "local_scene": item["local_scene"],
            "source_scene_number": item["source_scene_number"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "video": path.relative_to(ROOT).as_posix(),
            "video_sha256": actual_hash,
            "duration": round(actual_duration, 5),
            "size_bytes": path.stat().st_size,
            "width": width,
            "height": height,
            "fps": fps,
            "status": "completed",
        })

        return row

    except (OSError, ValueError, RuntimeError):
        return None


def render_parallax(
    item, depth, temporary, frames, fps, width, height
):
    script = ROOT / "scripts" / "depth_parallax.py"

    if not script.is_file():
        raise RuntimeError(
            "Depth parallax renderer missing: scripts/depth_parallax.py"
        )

    run_command(
        [
            sys.executable, str(script),
            "--image", str(item["image"]),
            "--depth", str(depth["path"]),
            "--output", str(temporary),
            "--width", str(width),
            "--height", str(height),
            "--fps", str(fps),
            "--frames", str(frames),
            "--strength", "0.026",
        ],
        f"Depth parallax {item['scene_id']}",
        timeout=1800,
    )


def render_2d(
    item, effect, temporary, frames, fps,
    width, height, ffmpeg,
):
    filters = make_filter(effect, frames, width, height, fps)

    run_command(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
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
        f"2D Photo Motion {item['scene_id']}",
        timeout=1800,
    )

def build_manifest(
    entries, mode, width, height, fps,
    prepared, status, error=None,
):
    rows = [
        entries[item["scene_id"]]
        for item in prepared
        if item["scene_id"] in entries
    ]

    document = {
        "status": status,
        "version": VERSION,
        "renderer": "photo_motion.py",
        "format": mode,
        "width": width,
        "height": height,
        "fps": fps,
        "expected_scene_count": len(prepared),
        "completed_count": sum(
            row.get("status") == "completed" for row in rows
        ),
        "total_scenes": len(prepared),
        "scene_role_counts": {
            role: sum(
                item["role"] == role for item in prepared
            )
            for role in ROLE_ORDER
        },
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


def build_checkpoint(entries, prepared, status, error=None):
    rows = []

    for item in prepared:
        row = entries.get(item["scene_id"])

        rows.append({
            "scene_id": item["scene_id"],
            "part": item["part"],
            "role": item["role"],
            "status": (
                row.get("status", "pending")
                if isinstance(row, dict) else "pending"
            ),
            "video": (
                row.get("video")
                if isinstance(row, dict) else None
            ),
        })

    result = {
        "version": VERSION,
        "stage": "photo_motion",
        "status": status,
        "total_jobs": len(prepared),
        "completed_jobs": sum(
            row["status"] == "completed" for row in rows
        ),
        "jobs": rows,
    }

    if error:
        result["last_error"] = str(error)[:2000]

    return result


def resolve_output_dimensions(mode):
    if mode == "short":
        return 720, 1280

    if mode == "full":
        return 1280, 720

    raise RuntimeError(f"Unsupported FORMAT: {mode}")


def validate_job_metadata(metadata, configured_parts, scenes_per_part):
    declared_parts = (
        metadata.get("parts")
        or metadata.get("total_parts")
    )

    if declared_parts is not None:
        if positive_int(declared_parts, "job metadata parts") != configured_parts:
            raise RuntimeError(
                "Photo Motion job PARTS do not match Input/topic.txt"
            )

    declared_scenes = metadata.get("scenes_per_part")

    if declared_scenes is not None:
        if (
            positive_int(declared_scenes, "job metadata scenes_per_part")
            != scenes_per_part
        ):
            raise RuntimeError(
                "Photo Motion job SCENES do not match Input/topic.txt"
            )

    declared_format = metadata.get("format")

    if declared_format is not None:
        if str(declared_format).strip().lower() not in {
            "short", "full"
        }:
            raise RuntimeError(
                f"Invalid job metadata format: {declared_format}"
            )


def validate_final_outputs(
    prepared, entries, ffprobe,
    width, height, fps,
):
    if len(entries) != len(prepared):
        raise RuntimeError(
            f"Photo Motion manifest mismatch: "
            f"{len(entries)}/{len(prepared)}"
        )

    verified = []

    for item in prepared:
        row = entries.get(item["scene_id"])

        if (
            not isinstance(row, dict)
            or row.get("status") != "completed"
        ):
            raise RuntimeError(
                f"{item['scene_id']}: completed manifest row missing"
            )

        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(
                f"{item['scene_id']}: image checksum mismatch"
            )

        if row.get("scene_id") != item["scene_id"]:
            raise RuntimeError(
                f"{item['scene_id']}: manifest scene ID mismatch"
            )

        target = safe_path(
            row.get("video"),
            f"{item['scene_id']} rendered video",
        )

        if target != item["target"]:
            raise RuntimeError(
                f"{item['scene_id']}: unexpected rendered path"
            )

        if sha256_file(target) != row.get("video_sha256"):
            raise RuntimeError(
                f"{item['scene_id']}: video checksum mismatch"
            )

        if item["duration"] is None:
            raise RuntimeError(
                f"{item['scene_id']}: narration duration missing"
            )

        frames = max(1, round(item["duration"] * fps))
        expected_duration = frames / fps

        actual_duration = probe_clip(
            target,
            ffprobe,
            width,
            height,
            expected_duration,
            fps,
        )

        if abs(actual_duration - expected_duration) > max(
            0.12, 2.0 / fps
        ):
            raise RuntimeError(
                f"{item['scene_id']}: final duration mismatch"
            )

        verified.append(row)

    if len(verified) != len(prepared):
        raise RuntimeError("Final Photo Motion clip count mismatch")

    return verified


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    configured_parts = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")
    fps = positive_int(get_fps(config), "FPS")

    if not 1 <= fps <= 60:
        raise RuntimeError(
            f"FPS must be between 1 and 60: {fps}"
        )

    width, height = resolve_output_dimensions(mode)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "FFmpeg and ffprobe are required for Photo Motion"
        )

    metadata, rows, jobs_source = load_job_document()

    validate_job_metadata(
        metadata,
        configured_parts,
        scenes_per_part,
    )

    prepared = prepare_jobs(
        rows,
        configured_parts,
        scenes_per_part,
    )

    if not prepared:
        raise RuntimeError("No valid Photo Motion jobs were prepared")

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    depth_maps = read_depth_maps(prepared)
    effect_plan = build_effect_plan(prepared, depth_maps)
    previous = previous_entries()

    entries = {}

    for item in prepared:
        old = previous.get(item["scene_id"])

        if isinstance(old, dict):
            entries[item["scene_id"]] = old

    def checkpoint(status="in_progress", error=None):
        write_json(
            MANIFEST_FILE,
            build_manifest(
                entries,
                mode,
                width,
                height,
                fps,
                prepared,
                status,
                error,
            ),
        )

        write_json(
            CHECKPOINT_FILE,
            build_checkpoint(
                entries,
                prepared,
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
    log("JOBS SOURCE:", jobs_source.relative_to(ROOT))
    log("FORMAT:", mode)
    log("RESOLUTION:", f"{width}x{height}")
    log("FPS:", fps)
    log("PARTS:", configured_parts)
    log("MAIN SCENES PER PART:", scenes_per_part)
    log("TOTAL CLIPS INCLUDING EXTRAS:", len(prepared))
    log("VALID DEPTH MAPS:", len(depth_maps))
    log("=" * 60)

    try:
        for index, item in enumerate(prepared, 1):
            if item["duration"] is None:
                raise RuntimeError(
                    f"{item['scene_id']}: narration duration is missing. "
                    "The TTS/audio stage must write measured narration "
                    "duration into the Photo Motion job before rendering."
                )

            duration = item["duration"]
            frames = max(1, round(duration * fps))
            expected_duration = frames / fps

            depth = depth_maps.get(item["scene_id"])
            effect = effect_plan[item["scene_id"]]

            if effect == "parallax" and depth is None:
                fallback_index = (
                    int(digest_data(item["scene_id"])[:8], 16)
                    % len(EFFECTS)
                )
                effect = EFFECTS[fallback_index]

            used_depth = depth if effect == "parallax" else None

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
                previous.get(item["scene_id"]),
                signature,
                ffprobe,
                expected_duration,
                fps,
                width,
                height,
            )

            if cached:
                entries[item["scene_id"]] = cached
                reused += 1
                checkpoint()

                log(
                    f"REUSED {index}/{len(prepared)} "
                    f"{item['scene_id']} ({item['role']})"
                )
                continue

            target = item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)

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

                if actual_duration <= 0:
                    raise RuntimeError(
                        f"{item['scene_id']}: invalid rendered duration"
                    )

                if not temporary.is_file() or (
                    temporary.stat().st_size < MIN_VIDEO_BYTES
                ):
                    raise RuntimeError(
                        f"{item['scene_id']}: temporary clip invalid"
                    )

                target.unlink(missing_ok=True)
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

            entries[item["scene_id"]] = {
                "scene_id": item["scene_id"],
                "role": item["role"],
                "scene_type": item["role"],
                "scene": item["number"],
                "global_scene": item["number"],
                "part": item["part"],
                "local_scene": item["local_scene"],
                "source_scene_number": item["source_scene_number"],
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
                "depth_status": (
                    used_depth["status"] if used_depth else None
                ),
                "depth_is_ai_estimated": is_ai_depth,
                "depth_is_approximate_fallback": is_approximate_depth,
                "effect": effect,
                "effect_is_depth_guided": effect == "parallax",
                "effect_uses_ai_depth": is_ai_depth,
                "duration": round(actual_duration, 5),
                "requested_duration": duration,
                "duration_source": item["duration_source"],
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

            generated += 1
            ai_parallax += int(is_ai_depth)
            approximate_parallax += int(is_approximate_depth)
            two_d_motion += int(effect != "parallax")

            checkpoint()

            log(
                f"COMPLETED {index}/{len(prepared)} "
                f"part={item['part']} role={item['role']} "
                f"scene_id={item['scene_id']} effect={effect}"
            )

        validate_final_outputs(
            prepared,
            entries,
            ffprobe,
            width,
            height,
            fps,
        )

        checkpoint(status="completed")

    except Exception as exc:
        checkpoint(status="failed", error=exc)
        raise

    log("=" * 60)
    log("PHOTO MOTION COMPLETE")
    log("FORMAT:", mode)
    log("PARTS:", configured_parts)
    log("MAIN SCENES PER PART:", scenes_per_part)
    log("TOTAL CLIPS:", len(prepared))
    log("NEW CLIPS:", generated)
    log("REUSED CLIPS:", reused)
    log("AI DEPTH PARALLAX:", ai_parallax)
    log("APPROXIMATE DEPTH PARALLAX:", approximate_parallax)
    log("2D MOTION:", two_d_motion)
    log("MANIFEST:", MANIFEST_FILE.relative_to(ROOT))
    log("CHECKPOINT:", CHECKPOINT_FILE.relative_to(ROOT))
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