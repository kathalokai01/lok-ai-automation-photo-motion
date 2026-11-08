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
    get_scenes_range,
    get_fps,
)

OUTPUT = ROOT / "output"
VISUALS = OUTPUT / "visuals"
JOBS_FILE = VISUALS / "photo_motion_jobs.json"
DEPTH_FILE = VISUALS / "depth_maps" / "depth_manifest.json"
VISUAL_JOBS_MANIFEST = VISUALS / "visual_jobs_manifest.json"
IMAGE_MANIFEST = VISUALS / "image_manifest.json"

RENDER_DIR = OUTPUT / "photo_motion"
MANIFEST_FILE = RENDER_DIR / "photo_motion_manifest.json"
CHECKPOINT_FILE = OUTPUT / "checkpoints" / "photo_motion_progress.json"

VERSION = "photo-motion-v23-stronger-parallax-visual-only-scenes"

MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100
MIN_AUDIO_BYTES = 100
MIN_VIDEO_BYTES = 1000
MIN_DURATION = 0.5
MAX_DURATION = 3600.0
DEFAULT_VISUAL_SCENE_SECONDS = 6.0
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
    "cinematic_sweep", "parallax",
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

    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant is forbidden: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
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

    document = read_json(JOBS_FILE)

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
    scenes_minimum: int,
    scenes_maximum: int,
    mode: str,
    ffprobe: str,
    metadata: dict,
) -> tuple[list[dict], list[int], dict[int, int]]:
    if not isinstance(metadata, dict) or metadata.get("status") != "completed":
        raise RuntimeError("Photo Motion jobs metadata is missing or incomplete.")
    if text(metadata.get("stage")) != "photo_motion_jobs_prepared":
        raise RuntimeError("Photo Motion jobs metadata stage is invalid.")
    if text(metadata.get("format")).lower() != mode:
        raise RuntimeError("Photo Motion jobs FORMAT does not match Input.")
    if metadata.get("scenes_min") != scenes_minimum or metadata.get("scenes_max") != scenes_maximum:
        raise RuntimeError("Prepared SCENES range does not match Input/topic.txt.")

    raw_parts = metadata.get("part_numbers")
    raw_counts = metadata.get("actual_scene_counts")
    if not isinstance(raw_parts, list) or len(raw_parts) != expected_parts:
        raise RuntimeError("Photo Motion part_numbers must match selected PARTS.")
    if not isinstance(raw_counts, dict):
        raise RuntimeError("Photo Motion actual_scene_counts is missing.")
    part_numbers = [positive_int(value, "prepared Part number") for value in raw_parts]
    if len(set(part_numbers)) != len(part_numbers) or part_numbers != sorted(part_numbers):
        raise RuntimeError("Photo Motion Part numbers must be unique and ascending.")
    if part_numbers != list(range(part_numbers[0], part_numbers[0] + expected_parts)):
        raise RuntimeError(f"Part numbering must be continuous: {part_numbers}.")

    scene_counts = {}
    for part in part_numbers:
        raw_count = raw_counts.get(str(part), raw_counts.get(part))
        count = positive_int(raw_count, f"Part {part} actual Scene count")
        if not scenes_minimum <= count <= scenes_maximum:
            raise RuntimeError(
                f"Part {part}: actual Scene count {count} is outside "
                f"{scenes_minimum}-{scenes_maximum}."
            )
        scene_counts[part] = count
    if set(str(key) for key in raw_counts) != {str(part) for part in part_numbers}:
        raise RuntimeError("actual_scene_counts contains missing or unexpected Parts.")

    prepared = []
    seen_ids = set()
    seen_global = set()
    seen_local_slots = set()
    main_mapping = set()
    role_counts = {role: {} for role in ROLE_ORDER}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Photo Motion job #{index} is not an object.")
        part = positive_int(row.get("part"), "part")
        if part not in scene_counts:
            raise RuntimeError(f"Job #{index}: unexpected Part {part}.")
        role = normalize_role(row)
        scene_id = text(row.get("scene_id") or row.get("id"))
        if not scene_id or not all(char.isalnum() or char in "_.-" for char in scene_id):
            raise RuntimeError(f"Job #{index}: invalid scene_id {scene_id!r}.")
        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate scene ID: {scene_id}.")
        seen_ids.add(scene_id)

        source_scene = row.get("source_scene_number")
        if role == "main":
            source_scene = positive_int(source_scene, f"{scene_id} source scene number")
            if source_scene > scene_counts[part]:
                raise RuntimeError(f"{scene_id}: source Scene exceeds Part {part} actual count.")
            pair = (part, source_scene)
            if pair in main_mapping:
                raise RuntimeError(f"Duplicate main Scene mapping: {pair}.")
            main_mapping.add(pair)
            expected_id = f"part_{part:02d}_scene_{source_scene:02d}"
        else:
            if source_scene not in (None, ""):
                raise RuntimeError(f"{scene_id}: extra role must not have a main Scene number.")
            source_scene = None
            expected_id = f"part_{part:02d}_{role}"
        if scene_id != expected_id:
            raise RuntimeError(f"{scene_id}: expected canonical ID {expected_id}.")

        global_number = positive_int(
            row.get("global_scene", row.get("global_scene_number", index)),
            "global scene number",
        )
        if global_number in seen_global:
            raise RuntimeError(f"Duplicate global scene number: {global_number}.")
        seen_global.add(global_number)

        local_slot = positive_int(
            row.get("local_scene", row.get("scene", index)),
            "local Photo Motion scene slot",
        )
        if (part, local_slot) in seen_local_slots:
            raise RuntimeError(f"{scene_id}: duplicate local Photo Motion scene slot.")
        seen_local_slots.add((part, local_slot))

        image_path = safe_path(
            row.get("image_path") or row.get("image") or row.get("output_path"),
            f"{scene_id} image",
        )
        image_width, image_height = image_dimensions(image_path, scene_id)
        expected_dimensions = (720, 1280) if mode == "short" else (1280, 720)
        if (image_width, image_height) != expected_dimensions:
            raise RuntimeError(
                f"{scene_id}: image dimensions {image_width}x{image_height} "
                f"do not match {mode.upper()} {expected_dimensions[0]}x{expected_dimensions[1]}."
            )
        image_hash = sha256_file(image_path)
        declared_image_hash = text(row.get("image_sha256")).lower()
        if not declared_image_hash or declared_image_hash != image_hash:
            raise RuntimeError(f"{scene_id}: image SHA256 missing or mismatched.")

        if role == "main":
            if row.get("voiceover_enabled") is not True or row.get("narration_source") != "locked_part_text":
                raise RuntimeError(f"{scene_id}: Main narration source is not locked.")
            audio_path, audio_hash, duration = resolve_audio(row, scene_id, ffprobe)
            duration_source = "ffprobe_measured"
        else:
            if row.get("voiceover_enabled") is not False or text(row.get("narration")):
                raise RuntimeError(f"{scene_id}: visual-only role cannot contain narration.")
            if text(row.get("narration_source")) != "visual_only":
                raise RuntimeError(f"{scene_id}: extra role is not marked visual_only.")
            if text(row.get("audio_sha256")) or row.get("audio_path") not in (None, ""):
                raise RuntimeError(f"{scene_id}: visual-only role has unexpected TTS audio.")
            duration = (
                valid_duration(row.get("scene_duration"))
                or valid_duration(row.get("duration"))
                or DEFAULT_VISUAL_SCENE_SECONDS
            )
            audio_path, audio_hash = None, ""
            duration_source = "visual_scene_duration"
        prompt = text(row.get("prompt") or row.get("image_prompt"))
        if len(prompt) < 24:
            raise RuntimeError(f"{scene_id}: story visual prompt is missing or too short.")
        if not text(row.get("source_scene_signature")) or not text(row.get("prompt_signature")):
            raise RuntimeError(f"{scene_id}: source/prompt signatures are missing.")

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
            "duration_source": duration_source,
            "voiceover_enabled": role == "main",
            "narration_source": "locked_part_text" if role == "main" else "visual_only",
            "source_text_sha256": text(row.get("source_text_sha256")) if role == "main" else "",
            "target": resolve_output_target(part, scene_id),
            "source_job": dict(row),
        })
        role_counts[role][part] = role_counts[role].get(part, 0) + 1

    if len(prepared) != positive_int(metadata.get("total_jobs"), "prepared total_jobs"):
        raise RuntimeError("Prepared job count does not match actual jobs.")
    if set(item["scene_id"] for item in prepared) != set(
        row.get("scene_id") for row in metadata.get("jobs", []) if isinstance(row, dict)
    ):
        raise RuntimeError("Prepared job IDs do not match the metadata's embedded jobs.")
    expected_global = list(range(1, len(prepared) + 1))
    if sorted(seen_global) != expected_global:
        raise RuntimeError("Global Photo Motion Scene numbers must be continuous from 1.")

    expected_main = {
        (part, scene)
        for part, count in scene_counts.items()
        for scene in range(1, count + 1)
    }
    if main_mapping != expected_main:
        raise RuntimeError(
            "Main Scene mapping mismatch. "
            f"Missing={sorted(expected_main-main_mapping)[:20]}, "
            f"extra={sorted(main_mapping-expected_main)[:20]}."
        )

    for position, part in enumerate(part_numbers):
        counts = {role: role_counts[role].get(part, 0) for role in ROLE_ORDER}
        if counts["hook"] != 1 or counts["main"] != scene_counts[part]:
            raise RuntimeError(f"Part {part}: Hook/Main role counts are invalid.")
        if position < len(part_numbers) - 1:
            if counts["suspense"] != 1 or counts["resolution"] != 0:
                raise RuntimeError(f"Part {part}: requires one Suspense and no Resolution.")
        elif counts["resolution"] != 1 or counts["suspense"] != 0:
            raise RuntimeError(f"Final Part {part}: requires one Resolution and no Suspense.")

    prepared.sort(key=lambda item: item["number"])
    role_sorted = sorted(
        prepared,
        key=lambda item: (
            item["part"],
            ROLE_ORDER[item["role"]],
            item["source_scene_number"] or item["local_scene"],
            item["number"],
        ),
    )
    if prepared != role_sorted:
        raise RuntimeError("Photo Motion jobs are not in canonical Part/role/Scene order.")
    return prepared, part_numbers, scene_counts
def validate_job_metadata(
    metadata: dict,
    parts: int,
    scenes_minimum: int,
    scenes_maximum: int,
    mode: str,
    prepared: list[dict],
    part_numbers: list[int],
    scene_counts: dict[int, int],
) -> None:
    if not isinstance(metadata, dict) or metadata.get("status") != "completed":
        raise RuntimeError("Photo Motion preparation manifest is not completed.")
    if text(metadata.get("format")).lower() != mode:
        raise RuntimeError("Photo Motion job FORMAT does not match Input/topic.txt.")
    if positive_int(metadata.get("parts", metadata.get("total_parts")), "prepared PARTS") != parts:
        raise RuntimeError("Prepared PARTS do not match Input configuration.")
    if metadata.get("part_numbers") != part_numbers:
        raise RuntimeError("Prepared Part numbers do not match selected PARTS.")
    if metadata.get("scenes_min") != scenes_minimum or metadata.get("scenes_max") != scenes_maximum:
        raise RuntimeError("Prepared SCENES range does not match Input configuration.")
    expected_counts = {str(part): count for part, count in scene_counts.items()}
    if metadata.get("actual_scene_counts") != expected_counts:
        raise RuntimeError("Prepared actual_scene_counts do not match the jobs.")
    if positive_int(metadata.get("total_jobs"), "prepared total_jobs") != len(prepared):
        raise RuntimeError("Prepared job count does not match actual jobs.")
    signature = text(metadata.get("config_signature")).lower()
    if len(signature) != 64 or any(char not in "0123456789abcdef" for char in signature):
        raise RuntimeError("Photo Motion config_signature is missing or invalid.")
    declared_jobs = metadata.get("jobs")
    if not isinstance(declared_jobs, list) or len(declared_jobs) != len(prepared):
        raise RuntimeError("Photo Motion metadata embedded jobs are missing or incomplete.")
    declared_ids = [text(row.get("scene_id")) for row in declared_jobs if isinstance(row, dict)]
    actual_ids = [item["scene_id"] for item in prepared]
    if declared_ids != actual_ids:
        raise RuntimeError("Prepared manifest Scene ID order does not match jobs.")
    main_count = sum(item["role"] == "main" for item in prepared)
    visual_only_count = len(prepared) - main_count
    if metadata.get("pending_audio_measurement_count") != 0 or metadata.get("measured_audio_count") != main_count:
        raise RuntimeError("Photo Motion preparation metadata does not confirm measured Main TTS.")
    if metadata.get("visual_only_scene_count") != visual_only_count:
        raise RuntimeError("Photo Motion visual-only Scene count mismatch.")
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
            config.get("PHOTO_MOTION_PARALLAX_STRENGTH", 0.032)
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

    if not math.isfinite(strength) or not 0.0 <= strength <= 0.055:
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
            if (image.format or "").upper() != "PNG":
                raise RuntimeError("Depth map must be a PNG file.")
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            gray = image.convert("L")
            extrema = gray.getextrema()
            if not extrema or extrema[1] - extrema[0] < 2:
                raise RuntimeError("Depth map is uniform and cannot drive parallax.")
    except Exception as exc:
        raise RuntimeError(f"Depth map cannot be decoded or is invalid: {exc}") from exc
    if (width, height) != source_dimensions:
        raise RuntimeError(
            f"Depth map dimensions {width}x{height} do not match "
            f"source image {source_dimensions[0]}x{source_dimensions[1]}."
        )
    return width, height
def read_depth_maps(
    prepared: list[dict],
    enabled: bool,
    mode: str,
    part_numbers: list[int],
    scene_counts: dict[int, int],
    scenes_minimum: int,
    scenes_maximum: int,
) -> dict[str, dict]:
    if not enabled:
        log("Depth parallax disabled by configuration; 2D motion only.")
        return {}

    document = read_json(DEPTH_FILE)
    if not isinstance(document, dict):
        raise RuntimeError("Depth manifest is missing or invalid; cannot silently downgrade to 2D.")
    if document.get("status") != "completed" or document.get("stage") != "depth_maps":
        raise RuntimeError("Depth manifest is not a completed depth_maps artifact.")
    if document.get("version") != "photo-motion-depth-v9-manifest-bound-resume-integrity":
        raise RuntimeError("Depth manifest version is incompatible; regenerate depth maps.")
    if document.get("format") != mode:
        raise RuntimeError("Depth manifest FORMAT does not match Photo Motion FORMAT.")
    if document.get("total_scenes") != len(prepared) or document.get("completed_scenes") != len(prepared):
        raise RuntimeError("Depth manifest does not contain a completed map for every Photo Motion job.")
    if document.get("scenes_min") != scenes_minimum or document.get("scenes_max") != scenes_maximum:
        raise RuntimeError("Depth manifest SCENES range does not match prepared jobs.")
    expected_counts = {str(part): count for part, count in scene_counts.items()}
    if document.get("actual_scene_counts") != expected_counts:
        raise RuntimeError("Depth manifest actual_scene_counts do not match prepared jobs.")

    visual_manifest = read_json(VISUAL_JOBS_MANIFEST)
    image_manifest = read_json(IMAGE_MANIFEST)
    if not isinstance(visual_manifest, dict) or visual_manifest.get("status") != "completed":
        raise RuntimeError("Visual jobs manifest is missing or incomplete.")
    if not isinstance(image_manifest, dict) or image_manifest.get("status") != "completed":
        raise RuntimeError("Image manifest is missing or incomplete.")
    for key in ("source_fingerprint", "config_signature", "format"):
        expected_value = visual_manifest.get(key)
        if not expected_value or document.get(key) != expected_value or image_manifest.get(key) != expected_value:
            raise RuntimeError(f"Depth/image manifest {key} provenance mismatch.")
    model_signature = text(document.get("model_signature")).lower()
    if len(model_signature) != 64 or any(char not in "0123456789abcdef" for char in model_signature):
        raise RuntimeError("Depth manifest model_signature is missing or invalid.")
    if image_manifest.get("total_scenes") != len(prepared) or image_manifest.get("images_ready") != len(prepared):
        raise RuntimeError("Image manifest does not confirm all prepared source images.")
    for manifest in (visual_manifest, image_manifest):
        if manifest.get("scenes_min") != scenes_minimum or manifest.get("scenes_max") != scenes_maximum:
            raise RuntimeError("Upstream image/visual SCENES range does not match Photo Motion.")
        if manifest.get("actual_scene_counts") != expected_counts:
            raise RuntimeError("Upstream image/visual actual_scene_counts do not match Photo Motion.")

    rows = document.get("scenes")
    if not isinstance(rows, list) or len(rows) != len(prepared):
        raise RuntimeError("Depth manifest scene rows are missing or incomplete.")
    expected = {item["scene_id"]: item for item in prepared}
    result = {}
    depth_root = (VISUALS / "depth_maps").resolve()

    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Depth manifest contains a non-object Scene record.")
        scene_id = text(row.get("scene_id"))
        item = expected.get(scene_id)
        if item is None or scene_id in result:
            raise RuntimeError(f"Depth manifest contains unexpected or duplicate Scene ID: {scene_id!r}.")
        for key, expected_value in (
            ("image_sha256", item["image_sha256"]),
            ("part", item["part"]),
            ("role", item["role"]),
            ("local_scene", item["local_scene"]),
            ("global_scene", item["number"]),
            ("source_fingerprint", visual_manifest.get("source_fingerprint")),
            ("config_signature", visual_manifest.get("config_signature")),
            ("model_signature", model_signature),
            ("format", mode),
        ):
            if row.get(key) != expected_value:
                raise RuntimeError(f"{scene_id}: depth manifest {key} mismatch.")
        status = text(row.get("status")).lower()
        if status not in {"generated", "reused", "fallback_pseudo_depth", "fallback_reused", "pseudo_depth"}:
            raise RuntimeError(f"{scene_id}: depth status is not completed: {status!r}.")
        model = text(row.get("model"))
        if not model:
            raise RuntimeError(f"{scene_id}: depth model is missing.")
        ai_flag = row.get("is_ai_estimated")
        if not isinstance(ai_flag, bool):
            raise RuntimeError(f"{scene_id}: is_ai_estimated must be an explicit boolean.")
        if ai_flag:
            if model == "content-aware-pseudo-depth-v9" or status in {"fallback_pseudo_depth", "fallback_reused", "pseudo_depth"}:
                raise RuntimeError(f"{scene_id}: AI depth flag conflicts with fallback model/status.")
        else:
            if model != "content-aware-pseudo-depth-v9" or status not in {"fallback_pseudo_depth", "fallback_reused", "pseudo_depth"}:
                raise RuntimeError(f"{scene_id}: approximate depth flag conflicts with model/status.")
            if document.get("ai_required") is True or document.get("fallback_allowed") is not True:
                raise RuntimeError(f"{scene_id}: pseudo-depth fallback is forbidden by the depth manifest.")
        try:
            path = safe_path(row.get("depth_map") or row.get("depth_path"), f"{scene_id} depth map")
            expected_path = (depth_root / f"depth_{item['number']:04d}.png").resolve()
            if path.resolve() != expected_path:
                raise RuntimeError(f"{scene_id}: depth-map path is not canonical.")
            validate_depth_image(path, (item["image_width"], item["image_height"]))
            checksum = sha256_file(path)
            if checksum != text(row.get("depth_sha256")).lower():
                raise RuntimeError(f"{scene_id}: depth SHA256 missing or mismatched.")
            if row.get("size_bytes") != path.stat().st_size:
                raise RuntimeError(f"{scene_id}: depth file size mismatch.")
            if row.get("width") != item["image_width"] or row.get("height") != item["image_height"]:
                raise RuntimeError(f"{scene_id}: depth map dimensions do not match source image.")
        except Exception as exc:
            raise RuntimeError(f"{scene_id}: depth map failed integrity validation: {exc}") from exc

        if ai_flag and status not in {"generated", "reused"}:
            raise RuntimeError(f"{scene_id}: AI depth status is invalid.")
        result[scene_id] = {
            "path": path,
            "depth_sha256": checksum,
            "model": model,
            "status": status,
            "is_ai_depth": ai_flag,
            "image_sha256": item["image_sha256"],
            "source_fingerprint": visual_manifest["source_fingerprint"],
            "config_signature": visual_manifest["config_signature"],
            "model_signature": model_signature,
        }
    if set(result) != set(expected):
        missing = sorted(set(expected) - set(result))
        raise RuntimeError(f"Depth manifest is missing Scene maps: {missing[:20]}.")
    log("Verified depth maps:", len(result))
    log("2.5D Parallax available for every scene.")
    return result
def motion_scale(speed: str) -> float:
    return {
        "slow": 1.0,
        "normal": 1.35,
        "fast": 1.75,
        "very_fast": 2.15,
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
        recent_effects = set(recent[-4:])
        available = [effect for effect in choices if effect not in recent_effects]
        if not available and len(choices) > 1 and recent:
            available = [effect for effect in choices if effect != recent[-1]]
        if not available:
            available = choices
        seed = digest_data({
            "scene_id": scene_id,
            "part": item["part"],
            "local_scene": item["local_scene"],
            "image_sha256": item["image_sha256"],
            "audio_sha256": item["audio_sha256"],
            "role": item["role"],
            "settings": settings,
            "renderer_version": VERSION,
        })
        rng = random.Random(int(seed[:16], 16))
        selected = rng.choice(available)
        plan[scene_id] = selected
        recent_by_part[item["part"]] = (recent + [selected])[-4:]
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
    zoom_amount = min(0.15 * scale, 0.27)
    zoom_out_amount = min(0.14 * scale, 0.24)
    pan_zoom = 1.24
    zoom = "1.06"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"
    progress = f"on/{denominator}"

    if effect == "zoom_in":
        zoom = f"min(1+{zoom_amount:.4f}*{progress},1.28)"
    elif effect == "zoom_out":
        zoom = f"max(1.24-{zoom_out_amount:.4f}*{progress},1.0)"
    elif effect in {"pan_left", "pan_right"}:
        zoom = str(pan_zoom)
        direction = f"(1-{progress})" if effect == "pan_left" else progress
        x = f"(iw-iw/zoom)*{direction}"
    elif effect in {"pan_up", "pan_down"}:
        zoom = str(pan_zoom)
        direction = f"(1-{progress})" if effect == "pan_up" else progress
        y = f"(ih-ih/zoom)*{direction}"
    elif effect == "diagonal_drift":
        zoom = str(pan_zoom)
        x = f"(iw-iw/zoom)*{progress}"
        y = f"(ih-ih/zoom)*(1-{progress})"
    elif effect == "diagonal_push":
        zoom = f"min(1+0.12*{scale:.4f}*{progress},1.24)"
        x = f"(iw-iw/zoom)*(0.08+0.84*{progress})"
        y = f"(ih-ih/zoom)*(0.88-0.76*{progress})"
    elif effect.startswith("zoom_pan_"):
        zoom = f"min(1+0.12*{scale:.4f}*{progress},1.24)"
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
        zoom = f"1.08+0.12*sin(PI*{progress})"
        x = f"(iw-iw/zoom)*(0.5-0.45*cos(PI*{progress}))"
        y = f"(ih-ih/zoom)*(0.5+0.32*sin(PI*{progress}))"
    else:
        raise RuntimeError(f"Unsupported 2D effect: {effect}.")

    source_width = width * 2
    source_height = height * 2
    filters = [
        f"scale={source_width}:{source_height}:force_original_aspect_ratio=increase",
        f"crop={source_width}:{source_height}",
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={fps}",
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
            "audio_path": (
                item["audio_path"].relative_to(ROOT).as_posix()
                if item["audio_path"] is not None else None
            ),
            "audio_sha256": item["audio_sha256"],
            "audio_duration": item["duration"] if item["role"] == "main" else None,
            "duration_source": item["duration_source"],
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
    rows = [entries[item["scene_id"]] for item in prepared if item["scene_id"] in entries]
    main_count = sum(item["role"] == "main" for item in prepared)
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
        "scenes_min": settings["scenes_min"],
        "scenes_max": settings["scenes_max"],
        "actual_scene_counts": settings["actual_scene_counts"],
        "expected_scene_count": len(prepared),
        "measured_audio_count": main_count,
        "visual_only_scene_count": len(prepared) - main_count,
        "completed_count": sum(row.get("status") == "completed" for row in rows),
        "total_scenes": len(prepared),
        "scene_role_counts": {
            role: sum(item["role"] == role for item in prepared)
            for role in ROLE_ORDER
        },
        "scenes": rows,
        "depth_parallax_count": sum(row.get("effect_is_depth_guided") is True for row in rows),
        "ai_depth_parallax_count": sum(row.get("effect_uses_ai_depth") is True for row in rows),
        "approximate_depth_parallax_count": sum(row.get("depth_is_approximate_fallback") is True for row in rows),
        "two_d_motion_count": sum(row.get("effect_is_depth_guided") is False for row in rows),
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
            "global_scene": item["number"],
            "image_sha256": item["image_sha256"],
            "audio_sha256": item["audio_sha256"],
            "cache_signature": row.get("cache_signature") if isinstance(row, dict) else None,
            "status": row.get("status", "pending") if isinstance(row, dict) else "pending",
            "video": row.get("video") if isinstance(row, dict) else None,
            "video_sha256": row.get("video_sha256") if isinstance(row, dict) else None,
        })
    result = {
        "version": VERSION,
        "stage": "photo_motion",
        "status": status,
        "total_jobs": len(prepared),
        "completed_jobs": sum(row["status"] == "completed" for row in rows),
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
        raise RuntimeError(f"Photo Motion manifest mismatch: {len(entries)}/{len(prepared)}.")
    verified = []
    for item in prepared:
        scene_id = item["scene_id"]
        row = entries.get(scene_id)
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"{scene_id}: completed render manifest row missing.")
        for key, expected in (
            ("part", item["part"]),
            ("role", item["role"]),
            ("image_sha256", item["image_sha256"]),
            ("audio_sha256", item["audio_sha256"]),
            ("global_scene", item["number"]),
            ("local_scene", item["local_scene"]),
        ):
            if row.get(key) != expected:
                raise RuntimeError(f"{scene_id}: final render {key} mismatch.")
        target = safe_path(row.get("video"), f"{scene_id} rendered video")
        if target != item["target"]:
            raise RuntimeError(f"{scene_id}: unexpected output path.")
        if target.stat().st_size < MIN_VIDEO_BYTES or sha256_file(target) != row.get("video_sha256"):
            raise RuntimeError(f"{scene_id}: video missing, incomplete or checksum mismatch.")
        frames = max(1, round(item["duration"] * fps))
        probe_clip(target, ffprobe, width, height, frames / fps, frames, fps)
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

    scene_minimum, scene_maximum = get_scenes_range(config)

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

    prepared, part_numbers, scene_counts = prepare_jobs(
        rows=rows,
        expected_parts=configured_parts,
        scenes_minimum=scene_minimum,
        scenes_maximum=scene_maximum,
        mode=mode,
        ffprobe=ffprobe,
        metadata=metadata,
    )

    validate_job_metadata(
        metadata,
        configured_parts,
        scene_minimum,
        scene_maximum,
        mode,
        prepared,
        part_numbers,
        scene_counts,
    )

    for item in prepared:
        frames = max(1, round(item["duration"] * fps))

        if frames > MAX_FRAMES:
            raise RuntimeError(
                f"{item['scene_id']}: frame count exceeds safety limit."
            )

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    settings.update({
        "scenes_min": scene_minimum,
        "scenes_max": scene_maximum,
        "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
    })
    depth_maps = read_depth_maps(
        prepared,
        settings["parallax_enabled"],
        mode,
        part_numbers,
        scene_counts,
        scene_minimum,
        scene_maximum,
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
    log("SCENES RANGE:", f"{scene_minimum}-{scene_maximum}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
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
                    if item["audio_path"] is not None else None
                ),
                "audio_sha256": item["audio_sha256"],
                "audio_duration": item["duration"] if item["role"] == "main" else None,
                "duration_source": item["duration_source"],
                "voiceover_enabled": item["voiceover_enabled"],
                "narration_source": item["narration_source"],
                "source_text_sha256": item["source_text_sha256"],
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
            "scenes_per_part": None,
            "scenes_min": scene_minimum,
            "scenes_max": scene_maximum,
            "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
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
            "effect_plan": effect_plan,
            "motion_speed_profile": motion_scale(settings["speed"]),
            "depth_manifest_sha256": sha256_file(DEPTH_FILE) if settings["parallax_enabled"] else None,
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