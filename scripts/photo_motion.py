#!/usr/bin/env python3
"""Validated, resumable 2D/2.5D Photo Motion renderer."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_selected_parts,
    get_scenes,
    get_fps,
)

OUTPUT = ROOT / "output"
VISUALS = OUTPUT / "visuals"
JOBS_FILE = VISUALS / "photo_motion_jobs.json"
DEPTH_FILE = VISUALS / "depth_maps" / "depth_manifest.json"

RENDER_DIR = OUTPUT / "photo_motion"
MANIFEST_FILE = RENDER_DIR / "photo_motion_manifest.json"
CHECKPOINT_FILE = OUTPUT / "checkpoints" / "photo_motion_progress.json"

VERSION = "photo-motion-v21-verified-role-motion-resume"

MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100
MIN_AUDIO_BYTES = 100
MIN_VIDEO_BYTES = 1000
MIN_DURATION = 0.5
MAX_DURATION = 3600.0
MAX_FRAMES = 216000
MAX_PARALLAX_SECONDS = 600

CRF = 19
PRESET = "medium"

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
    "cinematic_sweep",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()


def digest_data(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)

    if not path.is_file():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_path(
    value: Any,
    label: str,
    must_exist: bool = True,
) -> Path:
    raw = text(value)

    if not raw:
        raise RuntimeError(f"{label}: path is missing.")

    path = Path(raw)

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(
            f"{label}: path outside project directory is forbidden."
        )

    if must_exist and not path.is_file():
        raise RuntimeError(f"{label}: file does not exist: {path}")

    return path


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label}: boolean is not a valid integer.")

    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if result < 1:
        raise RuntimeError(f"{label} must be positive.")

    return result


def valid_duration(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None

    if text(value).lower() in {"", "auto", "unknown", "estimated"}:
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(result):
        return None

    if not MIN_DURATION <= result <= MAX_DURATION:
        return None

    return result


def run_command(
    command: list[Any],
    label: str,
    timeout: int = 1800,
) -> None:
    args = [str(value) for value in command]
    log("$", " ".join(args))

    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label}: command timed out.") from exc

    if result.stdout:
        log(result.stdout[-4000:])

    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}."
        )


def normalize_role(row: dict) -> str:
    raw = text(
        row.get("role")
        or row.get("scene_type")
        or row.get("type")
        or "main"
    ).lower()

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

    role = aliases.get(raw, raw)

    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported scene role: {raw!r}.")

    return role


def load_job_document() -> tuple[dict, list[dict]]:
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            "photo_motion_jobs.json is missing. "
            "Run prepare_visuals.py before Photo Motion."
        )

    document = read_json(JOBS_FILE, required=True) if False else read_json(
        JOBS_FILE
    )

    if isinstance(document, list):
        rows = document
        metadata = {}
    elif isinstance(document, dict):
        rows = (
            document.get("jobs")
            or document.get("photo_motion_jobs")
            or document.get("scenes")
            or []
        )
        metadata = document
    else:
        raise RuntimeError("Invalid Photo Motion jobs document.")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Photo Motion jobs list is empty.")

    if metadata and metadata.get("status") != "completed":
        raise RuntimeError(
            "Photo Motion jobs manifest must have status=completed."
        )

    return metadata, rows


def image_dimensions(path: Path, scene_id: str) -> tuple[int, int]:
    from PIL import Image

    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(f"{scene_id}: image missing or incomplete.")

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

    if not (256 <= width <= 4096 and 256 <= height <= 4096):
        raise RuntimeError(
            f"{scene_id}: invalid image dimensions {width}x{height}."
        )

    if image_format not in {"PNG", "JPEG", "WEBP"}:
        raise RuntimeError(
            f"{scene_id}: unsupported image format {image_format}."
        )

    return width, height


def probe_media(
    path: Path,
    ffprobe: str,
    require_audio: bool = False,
) -> dict:
    command = [
        ffprobe,
        "-v", "error",
        "-show_entries",
        (
            "format=duration:"
            "stream=codec_type,width,height,avg_frame_rate,"
            "r_frame_rate,nb_frames,nb_read_frames"
        ),
        "-of", "json",
        str(path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
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
    except Exception as exc:
        raise RuntimeError(
            f"Invalid media metadata for {path.name}."
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"{path.name}: invalid media duration.")

    audio_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "audio"
    ]
    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]

    if require_audio and not audio_streams:
        raise RuntimeError(f"{path.name}: audio stream is missing.")

    return {
        "duration": duration,
        "audio_streams": audio_streams,
        "video_streams": video_streams,
        "streams": streams,
    }


def resolve_audio(
    row: dict,
    scene_id: str,
    ffprobe: str,
) -> tuple[Path, str, float]:
    value = (
        row.get("audio_path")
        or row.get("narration_audio")
    )

    if isinstance(row.get("audio"), dict):
        value = value or row["audio"].get("path")

    path = safe_path(value, f"{scene_id} narration audio")

    if path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"{scene_id}: narration audio is incomplete.")

    checksum = sha256_file(path)

    declared_hash = text(
        row.get("audio_sha256")
        or (
            row.get("audio", {}).get("sha256")
            if isinstance(row.get("audio"), dict)
            else ""
        )
    ).lower()

    if not declared_hash or declared_hash != checksum:
        raise RuntimeError(
            f"{scene_id}: narration audio SHA256 missing or mismatched."
        )

    media = probe_media(path, ffprobe, require_audio=True)
    measured = media["duration"]

    declared_duration = valid_duration(
        row.get("actual_audio_duration_seconds")
        or row.get("audio_duration")
        or row.get("duration")
    )

    if declared_duration is None:
        raise RuntimeError(
            f"{scene_id}: measured TTS duration is missing. "
            "Estimated or auto duration is not accepted."
        )

    tolerance = max(0.15, 2.0 / 24.0)

    if abs(measured - declared_duration) > tolerance:
        raise RuntimeError(
            f"{scene_id}: declared TTS duration "
            f"{declared_duration:.3f}s differs from ffprobe "
            f"duration {measured:.3f}s."
        )

    duration_source = text(row.get("duration_source")).lower()

    if duration_source and duration_source not in {
        "ffprobe_measured",
        "measured",
        "actual",
    }:
        raise RuntimeError(
            f"{scene_id}: audio duration source is not measured."
        )

    return path, checksum, measured


def resolve_output_target(part: int, scene_id: str) -> Path:
    if not scene_id or not all(
        char.isalnum() or char in "_.-" for char in scene_id
    ):
        raise RuntimeError(f"Invalid scene ID for output: {scene_id!r}.")

    return (
        RENDER_DIR
        / f"part_{part:02d}"
        / f"{scene_id}.mp4"
    )


def prepare_jobs(
    rows: list[dict],
    expected_parts: int,
    scenes_per_part: int,
    mode: str,
    ffprobe: str,
) -> tuple[list[dict], list[int]]:
    prepared = []
    seen_ids = set()
    seen_global = set()
    main_mapping = set()
    role_counts = {role: {} for role in ROLE_ORDER}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Photo Motion job #{index} is not an object.")

        part = positive_int(row.get("part"), "part")
        role = normalize_role(row)

        scene_id = text(row.get("scene_id") or row.get("id"))

        if not scene_id:
            raise RuntimeError(f"Job #{index}: scene_id is missing.")

        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate scene ID: {scene_id}.")

        seen_ids.add(scene_id)

        expected_id = (
            f"part_{part:02d}_scene_{positive_int(row.get('source_scene_number'), scene_id + ' source scene'):02d}"
            if role == "main"
            else f"part_{part:02d}_{role}"
        )

        if scene_id != expected_id:
            raise RuntimeError(
                f"{scene_id}: expected canonical ID {expected_id}."
            )

        global_number = positive_int(
            row.get("global_scene", row.get("global_scene_number", index)),
            "global scene number",
        )

        if global_number in seen_global:
            raise RuntimeError(
                f"Duplicate global scene number: {global_number}."
            )

        seen_global.add(global_number)

        local_slot = positive_int(
            row.get("local_scene", row.get("scene", index)),
            "local Photo Motion scene slot",
        )

        source_scene = row.get("source_scene_number")

        if role == "main":
            source_scene = positive_int(
                source_scene,
                f"{scene_id} source scene number",
            )

            if source_scene > scenes_per_part:
                raise RuntimeError(
                    f"{scene_id}: source scene exceeds SCENES."
                )

            pair = (part, source_scene)

            if pair in main_mapping:
                raise RuntimeError(
                    f"Duplicate main scene mapping: {pair}."
                )

            main_mapping.add(pair)

        elif source_scene not in (None, ""):
            raise RuntimeError(
                f"{scene_id}: extra role must not have a main scene number."
            )
        else:
            source_scene = None

        image_path = safe_path(
            row.get("image_path") or row.get("image") or row.get("output_path"),
            f"{scene_id} image",
        )

        image_width, image_height = image_dimensions(
            image_path,
            scene_id,
        )

        image_hash = sha256_file(image_path)
        declared_image_hash = text(row.get("image_sha256")).lower()

        if not declared_image_hash or declared_image_hash != image_hash:
            raise RuntimeError(
                f"{scene_id}: image SHA256 missing or mismatched."
            )

        audio_path, audio_hash, duration = resolve_audio(
            row,
            scene_id,
            ffprobe,
        )

        prompt = text(row.get("prompt") or row.get("image_prompt"))

        if len(prompt) < 24:
            raise RuntimeError(
                f"{scene_id}: story visual prompt is missing or too short."
            )

        if not text(row.get("source_scene_signature")):
            raise RuntimeError(
                f"{scene_id}: source scene signature is missing."
            )

        if not text(row.get("prompt_signature")):
            raise RuntimeError(
                f"{scene_id}: prompt signature is missing."
            )

        prepared.append({
            "number": global_number,
            "scene_id": scene_id,
            "part": part,
            "role": role,
            "local_scene": local_slot,
            "source_scene_number": source_scene,
            "image": image_path,
            "image_sha256": image_hash,
            "image_width": image_width,
            "image_height": image_height,
            "audio_path": audio_path,
            "audio_sha256": audio_hash,
            "duration": duration,
            "duration_source": "ffprobe_measured",
            "target": resolve_output_target(part, scene_id),
            "source_job": dict(row),
        })

        role_counts[role][part] = role_counts[role].get(part, 0) + 1

    if not prepared:
        raise RuntimeError("No Photo Motion jobs were prepared.")

    part_numbers = sorted({item["part"] for item in prepared})

    if len(part_numbers) != expected_parts:
        raise RuntimeError(
            f"PARTS mismatch: expected {expected_parts}, "
            f"found {part_numbers}."
        )

    if part_numbers != list(
        range(part_numbers[0], part_numbers[0] + expected_parts)
    ):
        raise RuntimeError(
            f"Part numbering must be continuous: {part_numbers}."
        )

    expected_main = {
        (part, scene)
        for part in part_numbers
        for scene in range(1, scenes_per_part + 1)
    }

    if main_mapping != expected_main:
        raise RuntimeError(
            "Main scene mapping mismatch. "
            f"Missing={sorted(expected_main - main_mapping)[:20]}, "
            f"extra={sorted(main_mapping - expected_main)[:20]}."
        )

    for position, part in enumerate(part_numbers):
        counts = {
            role: role_counts[role].get(part, 0)
            for role in ROLE_ORDER
        }

        if counts["hook"] != 1:
            raise RuntimeError(
                f"Part {part} requires exactly one Hook."
            )

        if counts["main"] != scenes_per_part:
            raise RuntimeError(
                f"Part {part} requires exactly {scenes_per_part} Main scenes."
            )

        if position < len(part_numbers) - 1:
            if counts["suspense"] != 1 or counts["resolution"] != 0:
                raise RuntimeError(
                    f"Non-final part {part} requires one Suspense "
                    "and no Resolution."
                )
        else:
            if counts["resolution"] != 1 or counts["suspense"] != 0:
                raise RuntimeError(
                    "Final part requires one Resolution and no Suspense."
                )

    prepared.sort(
        key=lambda item: (
            item["part"],
            ROLE_ORDER[item["role"]],
            item["source_scene_number"] or item["local_scene"],
            item["number"],
        )
    )

    if [item["number"] for item in prepared] != list(
        range(1, len(prepared) + 1)
    ):
        raise RuntimeError(
            "Global Photo Motion scene numbers must be continuous "
            "after role-aware ordering."
        )

    return prepared, part_numbers


def validate_job_metadata(
    metadata: dict,
    parts: int,
    scenes: int,
    mode: str,
    prepared: list[dict],
) -> None:
    if not isinstance(metadata, dict):
        raise RuntimeError("Photo Motion metadata is invalid.")

    if metadata.get("status") != "completed":
        raise RuntimeError(
            "Photo Motion preparation manifest is not completed."
        )

    if text(metadata.get("format")).lower() != mode:
        raise RuntimeError(
            "Photo Motion job FORMAT does not match Input/topic.txt."
        )

    declared_parts = positive_int(
        metadata.get("parts", metadata.get("total_parts")),
        "prepared PARTS",
    )

    declared_scenes = positive_int(
        metadata.get("scenes_per_part"),
        "prepared SCENES",
    )

    if declared_parts != parts or declared_scenes != scenes:
        raise RuntimeError(
            "Prepared PARTS/SCENES do not match Input configuration."
        )

    if positive_int(metadata.get("total_jobs"), "prepared total_jobs") != len(
        prepared
    ):
        raise RuntimeError(
            "Prepared job count does not match actual jobs."
        )

    declared_ids = metadata.get("scene_ids")

    if declared_ids is not None:
        actual_ids = [item["scene_id"] for item in prepared]

        if declared_ids != actual_ids:
            raise RuntimeError(
                "Prepared manifest scene ID order does not match jobs."
            )


def validate_output_dimensions(mode: str) -> tuple[int, int]:
    if mode == "short":
        return 720, 1280

    if mode == "full":
        return 1280, 720

    raise RuntimeError(f"Unsupported FORMAT: {mode!r}.")


def validate_settings(config: dict) -> dict:
    settings = {
        "style": text(config.get("PHOTO_MOTION_STYLE", "dynamic")).lower(),
        "speed": text(config.get("PHOTO_MOTION_SPEED", "fast")).lower(),
        "variation": text(
            config.get("PHOTO_MOTION_VARIATION", "high")
        ).lower(),
        "parallax_enabled": config.get("PHOTO_MOTION_PARALLAX", True),
        "parallax_strength": float(
            config.get("PHOTO_MOTION_PARALLAX_STRENGTH", 0.026)
        ),
        "preserve_subject": config.get(
            "PHOTO_MOTION_PRESERVE_SUBJECT", True
        ),
    }

    if settings["style"] not in {
        "mixed", "zoom", "pan", "parallax", "dynamic", "static"
    }:
        raise RuntimeError("Invalid PHOTO_MOTION_STYLE.")

    if settings["speed"] not in {
        "slow", "normal", "fast", "very_fast"
    }:
        raise RuntimeError("Invalid PHOTO_MOTION_SPEED.")

    if settings["variation"] not in {"low", "medium", "high"}:
        raise RuntimeError("Invalid PHOTO_MOTION_VARIATION.")

    for key in ("parallax_enabled", "preserve_subject"):
        if not isinstance(settings[key], bool):
            raise RuntimeError(f"Invalid Photo Motion setting: {key}.")

    strength = settings["parallax_strength"]

    if not math.isfinite(strength) or not 0.0 <= strength <= 0.04:
        raise RuntimeError(
            "PHOTO_MOTION_PARALLAX_STRENGTH must be between 0 and 0.04."
        )

    return settings


def validate_depth_image(
    path: Path,
    source_dimensions: tuple[int, int],
) -> tuple[int, int]:
    from PIL import Image

    if not path.is_file() or path.stat().st_size < MIN_DEPTH_BYTES:
        raise RuntimeError("Depth map missing or incomplete.")

    try:
        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            image.load()
            width, height = image.size
            image_format = (image.format or "").upper()
            image_mode = image.mode

    except Exception as exc:
        raise RuntimeError(f"Depth map cannot be decoded: {exc}") from exc

    if (width, height) != source_dimensions:
        raise RuntimeError(
            f"Depth map dimensions {width}x{height} do not match "
            f"source image {source_dimensions[0]}x{source_dimensions[1]}."
        )

    if image_format not in {"PNG", "JPEG", "WEBP"}:
        raise RuntimeError("Unsupported depth map image format.")

    if image_mode not in {"L", "I", "I;16", "F", "RGB", "RGBA"}:
        raise RuntimeError(f"Unsupported depth map mode: {image_mode}.")

    return width, height


def read_depth_maps(
    prepared: list[dict],
    enabled: bool,
) -> dict[str, dict]:
    if not enabled:
        log("Depth parallax disabled; all clips use 2D motion.")
        return {}

    document = read_json(DEPTH_FILE)

    if not isinstance(document, dict):
        log("Depth manifest missing or invalid; using 2D motion.")
        return {}

    if document.get("status") != "completed":
        log("Depth manifest is not completed; using 2D motion.")
        return {}

    rows = document.get("scenes")

    if not isinstance(rows, list):
        log("Depth manifest has no scenes list; using 2D motion.")
        return {}

    expected = {item["scene_id"]: item for item in prepared}
    result = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        scene_id = text(row.get("scene_id"))
        item = expected.get(scene_id)

        if item is None:
            continue

        if scene_id in result:
            raise RuntimeError(
                f"Depth manifest contains duplicate scene ID: {scene_id}."
            )

        if text(row.get("image_sha256")).lower() != item["image_sha256"]:
            log("STALE DEPTH — 2D fallback:", scene_id)
            continue

        model = text(row.get("model"))
        status = text(row.get("status")).lower()

        if status not in {
            "generated", "reused", "fallback_pseudo_depth",
            "fallback_reused", "pseudo_depth",
        }:
            log("UNVERIFIED DEPTH — 2D fallback:", scene_id)
            continue

        if not model:
            log("DEPTH MODEL MISSING — 2D fallback:", scene_id)
            continue

        try:
            path = safe_path(
                row.get("depth_map") or row.get("depth_path"),
                f"{scene_id} depth map",
            )

            validate_depth_image(
                path,
                (item["image_width"], item["image_height"]),
            )

            checksum = sha256_file(path)
            declared = text(row.get("depth_sha256")).lower()

            if not declared or checksum != declared:
                raise RuntimeError("Depth SHA256 mismatch.")

            ai_flag = row.get("is_ai_estimated")

            if not isinstance(ai_flag, bool):
                ai_flag = (
                    model != "content-aware-pseudo-depth-v8"
                    and "pseudo" not in model.lower()
                    and "fallback" not in status
                )

            result[scene_id] = {
                "path": path,
                "depth_sha256": checksum,
                "model": model,
                "status": status,
                "is_ai_depth": ai_flag,
                "image_sha256": item["image_sha256"],
            }

        except Exception as exc:
            log("INVALID DEPTH — 2D fallback:", scene_id, str(exc))

    log("Verified depth maps:", len(result))
    log("2D fallback scenes:", len(prepared) - len(result))
    return result

def motion_scale(speed: str) -> float:
    return {
        "slow": 0.85,
        "normal": 1.05,
        "fast": 1.30,
        "very_fast": 1.50,
    }[speed]


def effect_choices(
    role: str,
    style: str,
    variation: str,
    has_depth: bool,
    preserve_subject: bool,
) -> list[str]:
    if style == "static":
        choices = ["zoom_in"]
    elif style == "zoom":
        choices = ["zoom_in", "zoom_out"]
    elif style == "pan":
        choices = [
            "pan_left", "pan_right", "pan_up", "pan_down",
            "zoom_pan_left", "zoom_pan_right",
            "zoom_pan_up", "zoom_pan_down",
        ]
    elif style == "parallax":
        choices = ["parallax"] if has_depth else [
            "zoom_in", "zoom_pan_left", "zoom_pan_right"
        ]
    elif style == "mixed":
        choices = [
            "zoom_in", "zoom_out",
            "pan_left", "pan_right",
            "pan_up", "pan_down",
            "diagonal_drift", "diagonal_push",
            "zoom_pan_left", "zoom_pan_right",
            "cinematic_sweep",
        ]
        if has_depth:
            choices.append("parallax")
    else:
        choices = list(EFFECTS)
        if has_depth:
            choices.append("parallax")

    if role == "hook":
        role_choices = [
            "zoom_in", "zoom_pan_left", "zoom_pan_right",
            "diagonal_push", "cinematic_sweep",
        ]
        if has_depth:
            role_choices.append("parallax")
        choices = [value for value in choices if value in role_choices] or choices

    elif role == "suspense":
        role_choices = [
            "zoom_in", "zoom_pan_left", "zoom_pan_right",
            "diagonal_push", "pan_up", "pan_down",
        ]
        if has_depth:
            role_choices.append("parallax")
        choices = [value for value in choices if value in role_choices] or choices

    elif role == "resolution":
        role_choices = [
            "zoom_out", "zoom_in", "pan_left",
            "pan_right", "cinematic_sweep",
        ]
        if has_depth:
            role_choices.append("parallax")
        choices = [value for value in choices if value in role_choices] or choices

    if preserve_subject:
        unsafe = {
            "dutch_angle", "tilt_up", "tilt_down",
            "handheld_drift",
        }
        choices = [value for value in choices if value not in unsafe]

    if variation == "low":
        allowed = {
            "zoom_in", "zoom_out", "pan_left", "pan_right",
            "zoom_pan_left", "zoom_pan_right", "parallax",
        }
        choices = [value for value in choices if value in allowed]

    elif variation == "medium":
        allowed = {
            "zoom_in", "zoom_out", "pan_left", "pan_right",
            "pan_up", "pan_down", "diagonal_push",
            "zoom_pan_left", "zoom_pan_right",
            "cinematic_sweep", "parallax",
        }
        choices = [value for value in choices if value in allowed]

    return list(dict.fromkeys(choices)) or ["zoom_in"]


def build_effect_plan(
    prepared: list[dict],
    depth_maps: dict[str, dict],
    settings: dict,
) -> dict[str, str]:
    plan = {}
    recent_by_part: dict[int, list[str]] = {}

    for item in prepared:
        scene_id = item["scene_id"]
        depth = depth_maps.get(scene_id)

        if item["duration"] > MAX_PARALLAX_SECONDS:
            depth = None

        choices = effect_choices(
            role=item["role"],
            style=settings["style"],
            variation=settings["variation"],
            has_depth=depth is not None,
            preserve_subject=settings["preserve_subject"],
        )

        if "parallax" in choices and depth is None:
            choices.remove("parallax")

        recent = recent_by_part.setdefault(item["part"], [])
        available = [
            effect for effect in choices
            if effect not in recent
        ] or choices

        seed = digest_data({
            "scene_id": scene_id,
            "image_sha256": item["image_sha256"],
            "audio_sha256": item["audio_sha256"],
            "role": item["role"],
            "settings": settings,
        })

        rng = random.Random(int(seed[:16], 16))
        selected = rng.choice(available)

        plan[scene_id] = selected
        recent_by_part[item["part"]] = (recent + [selected])[-3:]

    return plan


def make_filter(
    effect: str,
    frames: int,
    width: int,
    height: int,
    fps: int,
    settings: dict,
) -> str:
    denominator = max(frames - 1, 1)
    scale = motion_scale(settings["speed"])

    zoom_amount = min(0.12 * scale, 0.16)
    pan_zoom = 1.12

    zoom = "1.05"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"

    progress = f"on/{denominator}"

    if effect == "zoom_in":
        zoom = (
            f"min(1+{zoom_amount:.4f}*{progress},1.12)"
        )

    elif effect == "zoom_out":
        zoom = (
            f"max(1.12-{zoom_amount:.4f}*{progress},1.0)"
        )

    elif effect in {"pan_left", "pan_right"}:
        zoom = str(pan_zoom)
        direction = (
            f"(1-{progress})"
            if effect == "pan_left"
            else progress
        )
        x = f"(iw-iw/zoom)*{direction}"

    elif effect in {"pan_up", "pan_down"}:
        zoom = str(pan_zoom)
        direction = (
            f"(1-{progress})"
            if effect == "pan_up"
            else progress
        )
        y = f"(ih-ih/zoom)*{direction}"

    elif effect == "diagonal_drift":
        zoom = str(pan_zoom)
        x = f"(iw-iw/zoom)*{progress}"
        y = f"(ih-ih/zoom)*(1-{progress})"

    elif effect == "diagonal_push":
        zoom = f"min(1+0.11*{scale:.4f}*{progress},1.12)"
        x = f"(iw-iw/zoom)*(0.12+0.76*{progress})"
        y = f"(ih-ih/zoom)*(0.82-0.64*{progress})"

    elif effect.startswith("zoom_pan_"):
        zoom = f"min(1+0.10*{scale:.4f}*{progress},1.12)"
        direction = effect.rsplit("_", 1)[-1]

        if direction == "left":
            x = f"(iw-iw/zoom)*(1-{progress})"
        elif direction == "right":
            x = f"(iw-iw/zoom)*{progress}"
        elif direction == "up":
            y = f"(ih-ih/zoom)*(1-{progress})"
        elif direction == "down":
            y = f"(ih-ih/zoom)*{progress}"

    elif effect == "cinematic_sweep":
        zoom = (
            f"1.045+0.065*sin(PI*{progress})"
        )
        x = (
            f"(iw-iw/zoom)*(0.5-0.43*cos(PI*{progress}))"
        )
        y = (
            f"(ih-ih/zoom)*(0.5+0.25*sin(PI*{progress}))"
        )

    else:
        raise RuntimeError(f"Unsupported 2D effect: {effect}.")

    source_width = width * 2
    source_height = height * 2

    filters = [
        (
            f"scale={source_width}:{source_height}:"
            "force_original_aspect_ratio=increase"
        ),
        f"crop={source_width}:{source_height}",
        (
            f"zoompan=z='{zoom}':x='{x}':y='{y}':"
            f"d=1:s={width}x{height}:fps={fps}"
        ),
        "setsar=1",
        "format=yuv420p",
    ]

    return ",".join(filters)


def probe_clip(
    path: Path,
    ffprobe: str,
    width: int,
    height: int,
    expected_duration: float,
    expected_frames: int,
    fps: int,
) -> float:
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        raise RuntimeError(f"Rendered clip missing or incomplete: {path}.")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-count_frames",
            "-select_streams", "v:0",
            "-show_entries",
            (
                "stream=codec_type,width,height,avg_frame_rate,"
                "r_frame_rate,nb_frames,nb_read_frames:"
                "format=duration"
            ),
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
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

        if not streams:
            raise ValueError("No video stream.")

        video = streams[0]
        actual_width = int(video.get("width", 0))
        actual_height = int(video.get("height", 0))
    except Exception as exc:
        raise RuntimeError(
            f"{path.name}: invalid video metadata."
        ) from exc

    if (actual_width, actual_height) != (width, height):
        raise RuntimeError(
            f"{path.name}: resolution mismatch "
            f"{actual_width}x{actual_height}; "
            f"expected {width}x{height}."
        )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"{path.name}: invalid duration.")

    tolerance = max(0.15, 2.0 / fps)

    if abs(duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"{path.name}: duration {duration:.3f}s; "
            f"expected {expected_duration:.3f}s."
        )

    raw_frames = video.get("nb_read_frames") or video.get("nb_frames")

    if raw_frames not in (None, "N/A"):
        try:
            actual_frames = int(raw_frames)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{path.name}: invalid frame count."
            ) from exc

        if actual_frames != expected_frames:
            raise RuntimeError(
                f"{path.name}: frame count {actual_frames}; "
                f"expected {expected_frames}."
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
                f"{path.name}: FPS mismatch ({actual_fps})."
            )
    except (ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(
            f"{path.name}: invalid FPS metadata."
        ) from exc

    return duration


def make_signature(
    item: dict,
    effect: str,
    depth: dict | None,
    frames: int,
    fps: int,
    mode: str,
    width: int,
    height: int,
    settings: dict,
) -> str:
    parallax_script = ROOT / "scripts" / "depth_parallax.py"

    return digest_data({
        "version": VERSION,
        "renderer_sha256": sha256_file(Path(__file__)),
        "parallax_sha256": (
            sha256_file(parallax_script)
            if effect == "parallax" and parallax_script.is_file()
            else None
        ),
        "scene_id": item["scene_id"],
        "role": item["role"],
        "part": item["part"],
        "source_scene_number": item["source_scene_number"],
        "source_scene_signature": item["source_job"].get(
            "source_scene_signature"
        ),
        "prompt_signature": item["source_job"].get(
            "prompt_signature"
        ),
        "image_sha256": item["image_sha256"],
        "audio_sha256": item["audio_sha256"],
        "duration": round(item["duration"], 5),
        "frames": frames,
        "effect": effect,
        "depth_sha256": (
            depth["depth_sha256"] if depth else None
        ),
        "width": width,
        "height": height,
        "fps": fps,
        "format": mode,
        "settings": settings,
        "crf": CRF,
        "preset": PRESET,
    })


def try_reuse(
    item: dict,
    previous: dict | None,
    signature: str,
    ffprobe: str,
    expected_duration: float,
    frames: int,
    fps: int,
    width: int,
    height: int,
) -> dict | None:
    if not isinstance(previous, dict):
        return None

    if previous.get("status") != "completed":
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

        checksum = sha256_file(path)

        if checksum != previous.get("video_sha256"):
            return None

        actual_duration = probe_clip(
            path,
            ffprobe,
            width,
            height,
            expected_duration,
            frames,
            fps,
        )

        row = dict(previous)
        row.update({
            "scene_id": item["scene_id"],
            "role": item["role"],
            "scene_type": item["role"],
            "part": item["part"],
            "scene": item["number"],
            "global_scene": item["number"],
            "local_scene": item["local_scene"],
            "source_scene_number": item["source_scene_number"],
            "image": item["image"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "audio_path": item["audio_path"].relative_to(ROOT).as_posix(),
            "audio_sha256": item["audio_sha256"],
            "audio_duration": item["duration"],
            "duration_source": "ffprobe_measured",
            "video": path.relative_to(ROOT).as_posix(),
            "video_sha256": checksum,
            "duration": round(actual_duration, 5),
            "size_bytes": path.stat().st_size,
            "width": width,
            "height": height,
            "fps": fps,
            "frames": frames,
            "status": "completed",
            "cache_reused": True,
        })

        return row

    except (OSError, ValueError, RuntimeError):
        return None


def render_parallax(
    item: dict,
    depth: dict,
    temporary: Path,
    frames: int,
    fps: int,
    width: int,
    height: int,
    strength: float,
) -> None:
    script = ROOT / "scripts" / "depth_parallax.py"

    if not script.is_file():
        raise RuntimeError("scripts/depth_parallax.py is missing.")

    run_command([
        sys.executable,
        script,
        "--image", item["image"],
        "--depth", depth["path"],
        "--output", temporary,
        "--width", width,
        "--height", height,
        "--fps", fps,
        "--frames", frames,
        "--strength", strength,
    ], f"Depth parallax {item['scene_id']}", timeout=1800)


def render_2d(
    item: dict,
    effect: str,
    temporary: Path,
    frames: int,
    fps: int,
    width: int,
    height: int,
    ffmpeg: str,
    settings: dict,
) -> None:
    filters = make_filter(
        effect,
        frames,
        width,
        height,
        fps,
        settings,
    )

    run_command([
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-loop", "1",
        "-framerate", fps,
        "-i", item["image"],
        "-vf", filters,
        "-frames:v", frames,
        "-an",
        "-c:v", "libx264",
        "-preset", PRESET,
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-r", fps,
        "-fps_mode", "cfr",
        "-movflags", "+faststart",
        temporary,
    ], f"2D Photo Motion {item['scene_id']}", timeout=1800)


def build_manifest(
    entries: dict[str, dict],
    prepared: list[dict],
    mode: str,
    width: int,
    height: int,
    fps: int,
    settings: dict,
    status: str,
    error: str | None = None,
) -> dict:
    rows = [
        entries[item["scene_id"]]
        for item in prepared
        if item["scene_id"] in entries
    ]

    result = {
        "version": VERSION,
        "status": status,
        "stage": "photo_motion",
        "renderer": "photo_motion.py",
        "format": mode,
        "width": width,
        "height": height,
        "fps": fps,
        "settings": settings,
        "expected_scene_count": len(prepared),
        "completed_count": sum(
            row.get("status") == "completed"
            for row in rows
        ),
        "total_scenes": len(prepared),
        "scene_role_counts": {
            role: sum(
                item["role"] == role
                for item in prepared
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
        "part_outputs_only": True,
        "combined_video_created": False,
    }

    if error:
        result["last_error"] = str(error)[:2000]

    return result


def build_checkpoint(
    entries: dict[str, dict],
    prepared: list[dict],
    status: str,
    error: str | None = None,
) -> dict:
    rows = []

    for item in prepared:
        row = entries.get(item["scene_id"])

        rows.append({
            "scene_id": item["scene_id"],
            "part": item["part"],
            "role": item["role"],
            "status": (
                row.get("status", "pending")
                if isinstance(row, dict)
                else "pending"
            ),
            "video": (
                row.get("video")
                if isinstance(row, dict)
                else None
            ),
            "video_sha256": (
                row.get("video_sha256")
                if isinstance(row, dict)
                else None
            ),
        })

    result = {
        "version": VERSION,
        "stage": "photo_motion",
        "status": status,
        "total_jobs": len(prepared),
        "completed_jobs": sum(
            row["status"] == "completed"
            for row in rows
        ),
        "jobs": rows,
    }

    if error:
        result["last_error"] = str(error)[:2000]

    return result


def validate_final_outputs(
    prepared: list[dict],
    entries: dict[str, dict],
    ffprobe: str,
    width: int,
    height: int,
    fps: int,
) -> list[dict]:
    if len(entries) != len(prepared):
        raise RuntimeError(
            f"Photo Motion manifest mismatch: "
            f"{len(entries)}/{len(prepared)}."
        )

    verified = []

    for item in prepared:
        scene_id = item["scene_id"]
        row = entries.get(scene_id)

        if not isinstance(row, dict):
            raise RuntimeError(f"{scene_id}: manifest row missing.")

        if row.get("status") != "completed":
            raise RuntimeError(f"{scene_id}: render incomplete.")

        if row.get("part") != item["part"]:
            raise RuntimeError(f"{scene_id}: part mapping mismatch.")

        if row.get("role") != item["role"]:
            raise RuntimeError(f"{scene_id}: role mapping mismatch.")

        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(f"{scene_id}: image checksum mismatch.")

        if row.get("audio_sha256") != item["audio_sha256"]:
            raise RuntimeError(f"{scene_id}: audio checksum mismatch.")

        target = safe_path(
            row.get("video"),
            f"{scene_id} rendered video",
        )

        if target != item["target"]:
            raise RuntimeError(f"{scene_id}: unexpected output path.")

        checksum = sha256_file(target)

        if checksum != row.get("video_sha256"):
            raise RuntimeError(f"{scene_id}: video checksum mismatch.")

        frames = max(1, round(item["duration"] * fps))
        expected_duration = frames / fps

        probe_clip(
            target,
            ffprobe,
            width,
            height,
            expected_duration,
            frames,
            fps,
        )

        verified.append(row)

    if len(verified) != len(prepared):
        raise RuntimeError("Final Photo Motion clip count mismatch.")

    return verified


def main() -> int:
    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = text(get_format(config)).lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode!r}.")

    configured_parts = positive_int(
        get_selected_parts(config, required=True),
        "selected PARTS",
    )

    scenes_per_part = positive_int(
        get_scenes(config),
        "SCENES",
    )

    fps = positive_int(get_fps(config), "FPS")

    if fps > 60:
        raise RuntimeError("FPS cannot exceed 60.")

    settings = validate_settings(config)
    width, height = validate_output_dimensions(mode)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe are required.")

    metadata, rows = load_job_document()

    prepared, part_numbers = prepare_jobs(
        rows=rows,
        expected_parts=configured_parts,
        scenes_per_part=scenes_per_part,
        mode=mode,
        ffprobe=ffprobe,
    )

    validate_job_metadata(
        metadata,
        configured_parts,
        scenes_per_part,
        mode,
        prepared,
    )

    for item in prepared:
        frames = max(1, round(item["duration"] * fps))

        if frames > MAX_FRAMES:
            raise RuntimeError(
                f"{item['scene_id']}: frame count exceeds safety limit."
            )

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    depth_maps = read_depth_maps(
        prepared,
        settings["parallax_enabled"],
    )

    effect_plan = build_effect_plan(
        prepared,
        depth_maps,
        settings,
    )

    previous_document = read_json(MANIFEST_FILE, {})

    if isinstance(previous_document, dict):
        previous_rows = previous_document.get("scenes", [])
    elif isinstance(previous_document, list):
        previous_rows = previous_document
    else:
        previous_rows = []

    if not isinstance(previous_rows, list):
        previous_rows = []

    previous = {}

    for row in previous_rows:
        if not isinstance(row, dict):
            continue

        scene_id = text(row.get("scene_id"))

        if scene_id:
            if scene_id in previous:
                raise RuntimeError(
                    f"Previous manifest has duplicate scene ID: {scene_id}."
                )
            previous[scene_id] = row

    entries = {
        item["scene_id"]: previous[item["scene_id"]]
        for item in prepared
        if item["scene_id"] in previous
    }

    def checkpoint(
        status: str = "in_progress",
        error: str | None = None,
    ) -> None:
        write_json(
            MANIFEST_FILE,
            build_manifest(
                entries,
                prepared,
                mode,
                width,
                height,
                fps,
                settings,
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

    log("=" * 64)
    log("PHOTO MOTION VERSION:", VERSION)
    log("FORMAT:", mode)
    log("RESOLUTION:", f"{width}x{height}")
    log("FPS:", fps)
    log("PARTS:", part_numbers)
    log("MAIN SCENES PER PART:", scenes_per_part)
    log("TOTAL ROLE CLIPS:", len(prepared))
    log("SETTINGS:", settings)
    log("VERIFIED DEPTH MAPS:", len(depth_maps))
    log("=" * 64)

    try:
        for index, item in enumerate(prepared, 1):
            scene_id = item["scene_id"]
            duration = item["duration"]
            frames = max(1, round(duration * fps))
            expected_duration = frames / fps

            depth = depth_maps.get(scene_id)
            effect = effect_plan[scene_id]

            if effect == "parallax" and (
                depth is None or duration > MAX_PARALLAX_SECONDS
            ):
                effect = "zoom_in"
                depth = None

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
                settings,
            )

            cached = try_reuse(
                item=item,
                previous=previous.get(scene_id),
                signature=signature,
                ffprobe=ffprobe,
                expected_duration=expected_duration,
                frames=frames,
                fps=fps,
                width=width,
                height=height,
            )

            if cached:
                entries[scene_id] = cached
                reused += 1
                checkpoint()

                log(
                    f"REUSED {index}/{len(prepared)} "
                    f"{scene_id} ({item['role']})"
                )
                continue

            target = item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)

            temporary = target.with_name(
                target.stem + ".rendering.mp4"
            )

            temporary.unlink(missing_ok=True)

            try:
                if used_depth is not None:
                    render_parallax(
                        item,
                        used_depth,
                        temporary,
                        frames,
                        fps,
                        width,
                        height,
                        settings["parallax_strength"],
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
                        settings,
                    )

                actual_duration = probe_clip(
                    temporary,
                    ffprobe,
                    width,
                    height,
                    expected_duration,
                    frames,
                    fps,
                )

                os.replace(temporary, target)

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

            if used_depth:
                if is_ai_depth:
                    ai_parallax += 1
                else:
                    approximate_parallax += 1
            else:
                two_d_motion += 1

            entries[scene_id] = {
                "scene_id": scene_id,
                "role": item["role"],
                "scene_type": item["role"],
                "scene": item["number"],
                "global_scene": item["number"],
                "part": item["part"],
                "local_scene": item["local_scene"],
                "source_scene_number": item["source_scene_number"],
                "image": item["image"].relative_to(ROOT).as_posix(),
                "image_sha256": item["image_sha256"],
                "audio_path": (
                    item["audio_path"].relative_to(ROOT).as_posix()
                ),
                "audio_sha256": item["audio_sha256"],
                "audio_duration": item["duration"],
                "duration_source": "ffprobe_measured",
                "video": target.relative_to(ROOT).as_posix(),
                "video_sha256": video_hash,
                "size_bytes": target.stat().st_size,
                "depth_map": (
                    used_depth["path"].relative_to(ROOT).as_posix()
                    if used_depth else None
                ),
                "depth_sha256": (
                    used_depth["depth_sha256"]
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
                "effect_is_depth_guided": used_depth is not None,
                "effect_uses_ai_depth": is_ai_depth,
                "duration": round(actual_duration, 5),
                "requested_duration": duration,
                "frames": frames,
                "width": width,
                "height": height,
                "fps": fps,
                "cache_signature": signature,
                "status": "completed",
                "cache_reused": False,
            }

            generated += 1
            checkpoint()

            log(
                f"CHECKPOINT {index}/{len(prepared)} "
                f"part={item['part']} role={item['role']} "
                f"scene={scene_id} effect={effect}"
            )

        verified = validate_final_outputs(
            prepared,
            entries,
            ffprobe,
            width,
            height,
            fps,
        )

        if len(verified) != len(prepared):
            raise RuntimeError("Final Photo Motion count mismatch.")

        final_manifest = build_manifest(
            entries,
            prepared,
            mode,
            width,
            height,
            fps,
            settings,
            "completed",
        )

        final_manifest.update({
            "part_numbers": part_numbers,
            "total_parts": configured_parts,
            "scenes_per_part": scenes_per_part,
            "generated_count": generated,
            "reused_count": reused,
            "ai_depth_parallax_count": final_manifest[
                "ai_depth_parallax_count"
            ],
            "approximate_depth_parallax_count": final_manifest[
                "approximate_depth_parallax_count"
            ],
            "two_d_motion_count": final_manifest["two_d_motion_count"],
            "part_outputs_only": True,
            "combined_video_created": False,
            "scene_ids": [item["scene_id"] for item in prepared],
        })

        write_json(MANIFEST_FILE, final_manifest)

        write_json(
            CHECKPOINT_FILE,
            build_checkpoint(
                entries,
                prepared,
                "completed",
            ),
        )

        saved_manifest = read_json(MANIFEST_FILE)

        if (
            not isinstance(saved_manifest, dict)
            or saved_manifest.get("status") != "completed"
            or saved_manifest.get("completed_count") != len(prepared)
        ):
            raise RuntimeError(
                "Final Photo Motion manifest read-after-write validation failed."
            )

    except Exception as exc:
        checkpoint(status="failed", error=str(exc))
        log("PHOTO MOTION FAILED:", str(exc))
        raise

    log("=" * 64)
    log("PHOTO MOTION COMPLETED")
    log("FORMAT:", mode)
    log("PARTS:", part_numbers)
    log("TOTAL ROLE CLIPS:", len(prepared))
    log("GENERATED:", generated)
    log("REUSED:", reused)
    log("AI DEPTH PARALLAX:", ai_parallax)
    log("APPROXIMATE DEPTH PARALLAX:", approximate_parallax)
    log("2D MOTION:", two_d_motion)
    log("MANIFEST:", MANIFEST_FILE.relative_to(ROOT))
    log("CHECKPOINT:", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 64)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("PHOTO MOTION INTERRUPTED; completed checkpoints retained.")
        sys.exit(130)
    except Exception as exc:
        log("PHOTO MOTION FAILED:", str(exc))
        sys.exit(1)