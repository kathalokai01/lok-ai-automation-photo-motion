#!/usr/bin/env python3
"""Generate validated, resumable AI depth maps for Photo Motion."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VISUALS_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
IMAGE_MANIFEST = VISUALS_DIR / "image_manifest.json"
VISUAL_JOBS_MANIFEST = VISUALS_DIR / "visual_jobs_manifest.json"
DEPTH_DIR = VISUALS_DIR / "depth_maps"
MANIFEST_FILE = DEPTH_DIR / "depth_manifest.json"
CHECKPOINT_FILE = DEPTH_DIR / "depth_progress.json"

MODEL_ID = os.environ.get(
    "DEPTH_MODEL",
    "depth-anything/Depth-Anything-V2-Small-hf",
).strip()

MODEL_REVISION = os.environ.get("DEPTH_MODEL_REVISION", "").strip()
DEVICE_SETTING = os.environ.get("DEPTH_DEVICE", "auto").strip().lower()

AI_REQUIRED = os.environ.get(
    "DEPTH_AI_REQUIRED", "false"
).strip().lower() in {"1", "true", "yes", "on"}

UPGRADE_FALLBACK = os.environ.get(
    "DEPTH_UPGRADE_FALLBACK", "false"
).strip().lower() in {"1", "true", "yes", "on"}

ALLOW_PSEUDO_FALLBACK = os.environ.get(
    "DEPTH_ALLOW_PSEUDO_FALLBACK", "true"
).strip().lower() in {"1", "true", "yes", "on"}

VERSION = "photo-motion-depth-v10-all-role-narration-provenance"
FALLBACK_MODEL_ID = "content-aware-pseudo-depth-v9"

MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100
MIN_DIMENSION = 64
MAX_DIMENSION = 4096


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


def safe_path(value, label, must_exist=False):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label} path.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} path escapes project directory.")

    if must_exist and not path.is_file():
        raise RuntimeError(f"{label} does not exist: {path}")

    return path


def image_info(path, scene):
    from PIL import Image

    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(
            f"Scene {scene}: source image missing or too small: {path}"
        )

    try:
        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            width, height = image.size
            image_format = (image.format or "").upper()

        if not (
            MIN_DIMENSION <= width <= MAX_DIMENSION
            and MIN_DIMENSION <= height <= MAX_DIMENSION
        ):
            raise ValueError(f"Unsupported dimensions: {width}x{height}")

        if image_format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError(f"Unsupported image format: {image_format}")

        return width, height

    except Exception as exc:
        raise RuntimeError(
            f"Scene {scene}: source image validation failed: {exc}"
        ) from exc


def extract_job_rows(document):
    if isinstance(document, list):
        return document, {}

    if isinstance(document, dict):
        rows = (
            document.get("jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
            or []
        )
        return rows, document

    raise RuntimeError("visual_jobs.json has an unsupported structure.")


def load_jobs():
    document = read_json(JOBS_FILE, None)
    if document is None:
        raise RuntimeError(
            "visual_jobs.json is missing. Run generate_visuals.py first."
        )

    rows, metadata = extract_job_rows(document)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("visual_jobs.json contains no visual jobs.")

    jobs_manifest = read_json(VISUAL_JOBS_MANIFEST, None)
    if not isinstance(jobs_manifest, dict):
        raise RuntimeError("visual_jobs_manifest.json is missing or invalid.")
    if jobs_manifest.get("status") != "completed":
        raise RuntimeError("visual_jobs_manifest.json is not completed.")
    if jobs_manifest.get("stage") != "visual_jobs_prepared":
        raise RuntimeError("visual_jobs_manifest.json has an unexpected stage.")

    source_fingerprint = str(jobs_manifest.get("source_fingerprint") or "").strip().lower()
    config_signature = str(jobs_manifest.get("config_signature") or "").strip().lower()
    if len(source_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in source_fingerprint):
        raise RuntimeError("Visual jobs source_fingerprint is missing or invalid.")
    if len(config_signature) != 64 or any(c not in "0123456789abcdef" for c in config_signature):
        raise RuntimeError("Visual jobs config_signature is missing or invalid.")

    format_name = str(jobs_manifest.get("format") or "").strip().lower()
    if format_name not in {"short", "full"}:
        raise RuntimeError("Visual jobs manifest FORMAT must be SHORT or FULL.")
    if metadata:
        for key, expected in (
            ("source_fingerprint", source_fingerprint),
            ("config_signature", config_signature),
        ):
            declared = str(metadata.get(key) or "").strip().lower()
            if declared and declared != expected:
                raise RuntimeError(f"visual_jobs.json {key} disagrees with its manifest.")
        declared_format = str(metadata.get("format") or "").strip().lower()
        if declared_format and declared_format != format_name:
            raise RuntimeError("visual_jobs.json FORMAT disagrees with its manifest.")

    image_data = read_json(IMAGE_MANIFEST, None)
    if not isinstance(image_data, dict):
        raise RuntimeError("image_manifest.json is missing or invalid.")
    if image_data.get("status") != "completed":
        raise RuntimeError("image_manifest.json is not completed.")
    for key, expected in (
        ("source_fingerprint", source_fingerprint),
        ("config_signature", config_signature),
        ("format", format_name),
    ):
        actual = str(image_data.get(key) or "").strip().lower()
        if actual != expected:
            raise RuntimeError(f"image_manifest.json {key} mismatch.")
    if jobs_manifest.get("total_jobs") != len(rows):
        raise RuntimeError("visual_jobs_manifest total_jobs does not match visual_jobs.json.")
    if image_data.get("total_scenes") != len(rows):
        raise RuntimeError("image_manifest.json total_scenes does not match visual_jobs.json.")
    if image_data.get("images_ready") != len(rows):
        raise RuntimeError("image_manifest.json does not confirm every image is ready.")
    if image_data.get("images_failed") != 0 or image_data.get("images_pending") != 0:
        raise RuntimeError("image_manifest.json reports failed or pending images.")
    expected_size = (720, 1280) if format_name == "short" else (1280, 720)
    if image_data.get("image_size") != f"{expected_size[0]}x{expected_size[1]}":
        raise RuntimeError("image_manifest.json image_size does not match SHORT/FULL format.")

    raw_min = jobs_manifest.get("scenes_min")
    raw_max = jobs_manifest.get("scenes_max")
    if (
        not isinstance(raw_min, int) or isinstance(raw_min, bool)
        or not isinstance(raw_max, int) or isinstance(raw_max, bool)
        or raw_min < 1 or raw_max < raw_min
    ):
        raise RuntimeError("Visual jobs manifest SCENES range is invalid.")

    actual_counts = jobs_manifest.get("actual_scene_counts")
    if not isinstance(actual_counts, dict) or not actual_counts:
        raise RuntimeError("Visual jobs manifest actual_scene_counts is missing.")
    normalized_counts = {}
    for raw_part, raw_count in actual_counts.items():
        try:
            part = int(raw_part)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid actual_scene_counts Part: {raw_part!r}") from exc
        if (
            part < 1 or isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or not raw_min <= raw_count <= raw_max
        ):
            raise RuntimeError(f"Part {part}: actual Scene count is outside {raw_min}-{raw_max}.")
        normalized_counts[part] = raw_count
    if sorted(normalized_counts) != list(range(1, len(normalized_counts) + 1)):
        raise RuntimeError("actual_scene_counts Part numbers must be continuous from 1.")

    image_rows = image_data.get("scenes")
    if not isinstance(image_rows, list) or len(image_rows) != len(rows):
        raise RuntimeError("image_manifest.json scene rows are missing or incomplete.")
    image_by_id = {}
    for index, row in enumerate(image_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"image_manifest scene row {index} is invalid.")
        scene_id = str(row.get("scene_id") or "").strip()
        if not scene_id or scene_id in image_by_id:
            raise RuntimeError(f"image_manifest contains a missing or duplicate scene_id: {scene_id!r}.")
        image_by_id[scene_id] = row

    if image_data.get("scenes_min") != raw_min or image_data.get("scenes_max") != raw_max:
        raise RuntimeError("image_manifest.json SCENES range disagrees with visual_jobs_manifest.json.")
    if image_data.get("actual_scene_counts") != jobs_manifest.get("actual_scene_counts"):
        raise RuntimeError("image_manifest.json actual_scene_counts disagree with visual_jobs_manifest.json.")

    prepared = []
    seen_numbers = set()
    seen_ids = set()
    seen_paths = set()
    seen_local_slots = set()
    main_source_numbers = {part: set() for part in normalized_counts}
    part_main_counts = {part: 0 for part in normalized_counts}
    role_counts = {}
    for index, job in enumerate(rows, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Visual job {index} is not an object.")

        raw_number = job.get("global_scene", job.get("global_scene_number", job.get("scene_index", index)))
        if isinstance(raw_number, bool):
            raise RuntimeError(f"Invalid global scene number in job {index}.")
        try:
            number = int(raw_number)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid global scene number in job {index}.") from exc
        if number < 1 or number in seen_numbers:
            raise RuntimeError(f"Duplicate or invalid global scene number: {number}")
        seen_numbers.add(number)

        scene_id = str(job.get("scene_id") or "").strip()
        if not scene_id or scene_id in seen_ids:
            raise RuntimeError(f"Missing or duplicate scene_id in job {number}: {scene_id!r}")
        seen_ids.add(scene_id)

        try:
            part = int(job.get("part"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{scene_id}: invalid Part number.") from exc
        if part not in normalized_counts:
            raise RuntimeError(f"{scene_id}: Part is not in actual_scene_counts.")

        role = str(job.get("role") or job.get("scene_type") or "main").strip().lower()
        if role not in {"hook", "main", "suspense", "resolution"}:
            raise RuntimeError(f"{scene_id}: unsupported scene role {role!r}.")
        narration = str(job.get("narration") or "").strip()
        source_text_hash = str(job.get("source_text_sha256") or "").strip().lower()
        voiceover_enabled = job.get("voiceover_enabled")
        narration_source = job.get("narration_source")
        if role == "main":
            expected_text_hash = hashlib.sha256(narration.encode("utf-8")).hexdigest()
            if not narration or voiceover_enabled is not True or narration_source != "locked_part_text":
                raise RuntimeError(f"{scene_id}: Main locked narration metadata is invalid.")
            if source_text_hash != expected_text_hash:
                raise RuntimeError(f"{scene_id}: Main source text hash mismatch.")
            if job.get("source_word_count") != len(narration.split()):
                raise RuntimeError(f"{scene_id}: Main source word count mismatch.")
        else:
            expected_text_hash = hashlib.sha256(narration.encode("utf-8")).hexdigest()
            if not narration or voiceover_enabled is not True or narration_source != "ai_generated_role_narration":
                raise RuntimeError(f"{scene_id}: extra-role narration metadata is invalid.")
            if source_text_hash != expected_text_hash:
                raise RuntimeError(f"{scene_id}: extra-role narration hash mismatch.")
            if job.get("source_word_count") != len(narration.split()):
                raise RuntimeError(f"{scene_id}: extra-role narration word count mismatch.")
        local_raw = job.get("local_scene", job.get("scene", job.get("source_scene_number")))
        try:
            local_scene = int(local_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{scene_id}: invalid local Scene number.") from exc
        if local_scene < 1:
            raise RuntimeError(f"{scene_id}: local Scene number must be positive.")
        if (part, local_scene) in seen_local_slots:
            raise RuntimeError(f"{scene_id}: duplicate local Photo Motion slot.")
        seen_local_slots.add((part, local_scene))

        image_value = job.get("image_path") or job.get("image") or job.get("output_path")
        image_path = safe_path(image_value, f"source image for {scene_id}", must_exist=True)
        if image_path in seen_paths:
            raise RuntimeError(f"Multiple scenes reference the same image: {image_path}")
        seen_paths.add(image_path)

        image_row = image_by_id.get(scene_id)
        if image_row is None:
            raise RuntimeError(f"{scene_id}: image_manifest record is missing.")
        image_status = str(image_row.get("status") or "").strip().lower()
        if image_status not in {"generated", "existing"}:
            raise RuntimeError(f"{scene_id}: image_manifest status is not ready: {image_status!r}.")

        manifest_path_value = image_row.get("image_path") or image_row.get("image")
        manifest_path = safe_path(manifest_path_value, f"manifest image for {scene_id}", must_exist=True)
        if manifest_path != image_path:
            raise RuntimeError(f"{scene_id}: visual job and image manifest paths disagree.")

        width, height = image_info(image_path, number)
        if (width, height) != expected_size:
            raise RuntimeError(
                f"{scene_id}: image dimensions {width}x{height} do not match "
                f"{format_name.upper()} dimensions {expected_size[0]}x{expected_size[1]}."
            )
        image_hash = sha256_file(image_path)
        declared_hash = str(image_row.get("image_sha256") or "").strip().lower()
        if len(declared_hash) != 64 or declared_hash != image_hash:
            raise RuntimeError(f"{scene_id}: image manifest SHA256 missing or mismatched.")
        for dimension_key, actual in (("width", width), ("height", height)):
            declared = image_row.get(dimension_key)
            if declared is not None and declared != actual:
                raise RuntimeError(f"{scene_id}: image manifest {dimension_key} mismatch.")

        for key, expected in (
            ("source_fingerprint", source_fingerprint),
            ("config_signature", config_signature),
        ):
            job_value = str(job.get(key) or "").strip().lower()
            image_value = str(image_row.get(key) or "").strip().lower()
            if job_value != expected or image_value != expected:
                raise RuntimeError(f"{scene_id}: {key} missing or mismatched.")
        if image_row.get("job_signature") and job.get("job_signature"):
            if image_row["job_signature"] != job["job_signature"]:
                raise RuntimeError(f"{scene_id}: image job signature mismatch.")

        if role == "main":
            try:
                source_scene_number = int(job.get("source_scene_number"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"{scene_id}: main Scene source_scene_number is invalid.") from exc
            if not 1 <= source_scene_number <= normalized_counts[part]:
                raise RuntimeError(f"{scene_id}: source Scene number is outside the Part's actual Scene count.")
            if source_scene_number in main_source_numbers[part]:
                raise RuntimeError(f"Part {part}: duplicate main source Scene number {source_scene_number}.")
            main_source_numbers[part].add(source_scene_number)
            part_main_counts[part] += 1
        role_counts[(part, role)] = role_counts.get((part, role), 0) + 1

        prepared.append({
            "scene": number,
            "global_scene": number,
            "scene_id": scene_id,
            "part": part,
            "local_scene": local_scene,
            "role": role,
            "voiceover_enabled": voiceover_enabled,
            "narration_source": narration_source,
            "source_text_sha256": source_text_hash,
            "source_word_count": len(narration.split()),
            "visual_only": False,
            "job": job,
            "image_path": image_path,
            "image_sha256": image_hash,
            "width": width,
            "height": height,
            "job_signature": digest_data(job),
            "source_fingerprint": source_fingerprint,
            "config_signature": config_signature,
            "format": format_name,
        })

    prepared.sort(key=lambda item: item["scene"])
    numbers = [item["scene"] for item in prepared]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"Global scene numbers must be continuous from 1. Received: {numbers}")
    if seen_ids != set(image_by_id):
        raise RuntimeError("visual_jobs.json and image_manifest.json scene IDs do not match.")
    if part_main_counts != normalized_counts:
        raise RuntimeError(
            "Main Scene counts disagree with actual_scene_counts: "
            f"expected={normalized_counts}, found={part_main_counts}."
        )

    for part, expected_count in normalized_counts.items():
        if main_source_numbers[part] != set(range(1, expected_count + 1)):
            raise RuntimeError(f"Part {part}: main source Scene numbers are incomplete or duplicated.")
        if role_counts.get((part, "hook"), 0) != 1:
            raise RuntimeError(f"Part {part}: exactly one Hook image is required.")
        if part < len(normalized_counts):
            if role_counts.get((part, "suspense"), 0) != 1:
                raise RuntimeError(f"Part {part}: exactly one Suspense image is required.")
            if role_counts.get((part, "resolution"), 0) != 0:
                raise RuntimeError(f"Part {part}: non-final Part cannot have Resolution image.")
        else:
            if role_counts.get((part, "resolution"), 0) != 1:
                raise RuntimeError(f"Final Part {part}: exactly one Resolution image is required.")
            if role_counts.get((part, "suspense"), 0) != 0:
                raise RuntimeError(f"Final Part {part}: Suspense image is not allowed.")

    expected_total_jobs = sum(normalized_counts.values()) + 2 * len(normalized_counts)
    if len(prepared) != expected_total_jobs:
        raise RuntimeError(
            f"Visual job total mismatch: expected {expected_total_jobs} "
            f"(Main + Hook + Suspense/Resolution), found {len(prepared)}."
        )

    return prepared, {
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "format": format_name,
        "scenes_min": raw_min,
        "scenes_max": raw_max,
        "actual_scene_counts": {str(k): v for k, v in normalized_counts.items()},
        "model_signature": digest_data({
            "model": MODEL_ID,
            "revision": MODEL_REVISION,
            "device": DEVICE_SETTING,
            "ai_required": AI_REQUIRED,
            "fallback_allowed": ALLOW_PSEUDO_FALLBACK,
            "upgrade_fallback": UPGRADE_FALLBACK,
        }),
    }

def load_previous(metadata=None):
    metadata = metadata or {}
    previous = {}

    manifest = read_json(MANIFEST_FILE, {})
    if isinstance(manifest, dict):
        identity_matches = all(
            manifest.get(key) == metadata.get(key)
            for key in ("source_fingerprint", "config_signature", "model_signature", "format")
        ) and manifest.get("version") == VERSION
        if identity_matches and manifest.get("status") in {"in_progress", "completed", "failed"}:
            rows = manifest.get("scenes", [])
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        try:
                            number = int(row.get("global_scene", row.get("scene")))
                        except (TypeError, ValueError):
                            continue
                        if number > 0:
                            previous[number] = row

    checkpoint = read_json(CHECKPOINT_FILE, {})
    if isinstance(checkpoint, dict):
        checkpoint_matches = all(
            checkpoint.get(key) == metadata.get(key)
            for key in ("source_fingerprint", "config_signature", "model_signature", "format")
        ) and checkpoint.get("version") == VERSION
        if checkpoint_matches and checkpoint.get("stage") == "depth_maps":
            rows = checkpoint.get("jobs", [])
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        number = int(row.get("global_scene"))
                    except (TypeError, ValueError):
                        continue
                    if number < 1 or number in previous:
                        continue
                    previous[number] = row
    return previous

def validate_depth(path, expected_size=None):
    from PIL import Image

    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size < MIN_DEPTH_BYTES:
            return None
        with Image.open(path) as image:
            if (image.format or "").upper() != "PNG":
                return None
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            gray = image.convert("L")
            extrema = gray.getextrema()
            if not extrema or extrema[1] - extrema[0] < 2:
                return None
            if gray.getbbox() is None:
                return None
        if (
            width < MIN_DIMENSION or height < MIN_DIMENSION
            or width > MAX_DIMENSION or height > MAX_DIMENSION
        ):
            return None
        if expected_size and (width, height) != tuple(expected_size):
            return None
        return width, height
    except Exception:
        return None

def create_entry(item, path, model, status):
    dimensions = validate_depth(
        path,
        expected_size=(item["width"], item["height"]),
    )
    if dimensions is None:
        raise RuntimeError(f"Depth map validation failed: {path}")

    path = Path(path).resolve()
    if not path.is_relative_to(DEPTH_DIR.resolve()):
        raise RuntimeError(f"Depth output must remain inside {DEPTH_DIR}: {path}")

    relative_image = item["image_path"].relative_to(ROOT).as_posix()
    relative_depth = path.relative_to(ROOT).as_posix()
    is_ai = model == MODEL_ID and status not in {"fallback_pseudo_depth", "fallback_reused", "pseudo_depth"}

    return {
        "scene": item["scene"],
        "global_scene": item["scene"],
        "global_scene_number": item["scene"],
        "scene_id": item["scene_id"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "role": item["role"],
        "voiceover_enabled": item["voiceover_enabled"],
        "narration_source": item["narration_source"],
        "source_text_sha256": item["source_text_sha256"],
        "source_word_count": item["source_word_count"],
        "visual_only": item["visual_only"],
        "image": relative_image,
        "image_path": relative_image,
        "image_sha256": item["image_sha256"],
        "job_signature": item["job_signature"],
        "source_fingerprint": item["source_fingerprint"],
        "config_signature": item["config_signature"],
        "format": item["format"],
        "depth_map": relative_depth,
        "depth_path": relative_depth,
        "width": dimensions[0],
        "height": dimensions[1],
        "model": model,
        "model_signature": item["model_signature"],
        "status": status,
        "is_ai_estimated": is_ai,
        "size_bytes": path.stat().st_size,
        "depth_sha256": sha256_file(path),
        "script_version": VERSION,
    }

def try_reuse(item, old):
    if not isinstance(old, dict):
        return None
    for key in (
        "image_sha256", "job_signature", "source_fingerprint",
        "config_signature", "model_signature", "source_text_sha256",
        "voiceover_enabled", "narration_source",
    ):
        if old.get(key) != item.get(key):
            return None

    try:
        path = safe_path(
            old.get("depth_map") or old.get("depth_path"),
            "previous depth map",
            must_exist=True,
        )
    except RuntimeError:
        return None

    expected_destination = (DEPTH_DIR / f"depth_{item['scene']:04d}.png").resolve()
    if path.resolve() != expected_destination:
        return None
    if validate_depth(path, expected_size=(item["width"], item["height"])) is None:
        return None

    expected_hash = str(old.get("depth_sha256") or "").strip().lower()
    actual_hash = sha256_file(path)
    if len(expected_hash) != 64 or expected_hash != actual_hash:
        return None
    if old.get("size_bytes") != path.stat().st_size:
        return None

    model = str(old.get("model") or "").strip()
    if not model:
        return None
    is_ai = model == MODEL_ID and old.get("is_ai_estimated") is True
    if is_ai and model != MODEL_ID:
        return None
    if not is_ai and (AI_REQUIRED or not ALLOW_PSEUDO_FALLBACK):
        return None
    if not is_ai and model != FALLBACK_MODEL_ID:
        return None

    status = "reused" if is_ai else "fallback_reused"
    entry = create_entry(item, path, model, status)
    entry["_is_ai"] = is_ai
    return entry

def resolve_device():
    if DEVICE_SETTING not in {"auto", "cpu", "cuda"}:
        raise RuntimeError(
            "DEPTH_DEVICE must be auto, cpu or cuda."
        )

    if DEVICE_SETTING == "cpu":
        return -1

    try:
        import torch
        has_cuda = torch.cuda.is_available()
    except Exception:
        has_cuda = False

    if DEVICE_SETTING == "cuda" and not has_cuda:
        raise RuntimeError(
            "DEPTH_DEVICE=cuda was requested but CUDA is unavailable."
        )

    return 0 if has_cuda else -1


def load_depth_model():
    from transformers import pipeline

    device = resolve_device()
    options = {
        "task": "depth-estimation",
        "model": MODEL_ID,
        "device": device,
    }

    if MODEL_REVISION:
        options["revision"] = MODEL_REVISION

    log("Depth model:", MODEL_ID)
    log("Depth device:", "cuda" if device == 0 else "cpu")

    return pipeline(**options)


def generate_ai_depth(estimator, item, destination):
    from PIL import Image, ImageOps

    temporary = destination.with_name(
        destination.stem + ".temporary.png"
    )

    try:
        with Image.open(item["image_path"]) as image:
            source = ImageOps.exif_transpose(image).convert("RGB")

        result = estimator(source)

        if not isinstance(result, dict):
            raise RuntimeError("Depth model returned an invalid result.")

        depth = result.get("depth")

        if depth is None:
            raise RuntimeError("Depth model returned no depth image.")

        if not isinstance(depth, Image.Image):
            import numpy as np
            array = np.asarray(depth)

            if array.ndim == 3:
                array = array.squeeze()

            if array.ndim != 2:
                raise RuntimeError(
                    f"Unsupported depth output shape: {array.shape}"
                )

            if not np.isfinite(array).all():
                raise RuntimeError("Depth model returned non-finite values.")

            low = float(np.percentile(array, 2))
            high = float(np.percentile(array, 98))

            if high - low <= 1e-8:
                array = np.full(array.shape, 0.5, dtype=np.float32)
            else:
                array = (array - low) / (high - low)

            array = np.clip(array, 0.0, 1.0)
            depth = Image.fromarray(
                (array * 65535).astype("uint16"),
                mode="I;16",
            )

        depth = ImageOps.grayscale(depth)
        depth = ImageOps.autocontrast(depth)

        if depth.size != (item["width"], item["height"]):
            depth = depth.resize(
                (item["width"], item["height"]),
                Image.Resampling.BILINEAR,
            )

        depth.save(temporary, format="PNG")

        if validate_depth(
            temporary,
            expected_size=(item["width"], item["height"]),
        ) is None:
            raise RuntimeError("AI depth output failed validation.")

        temporary.replace(destination)

        return create_entry(
            item,
            destination,
            MODEL_ID,
            "generated",
        )

    finally:
        temporary.unlink(missing_ok=True)


def content_aware_pseudo_depth(image_path, width, height):
    """Explicitly labelled visual approximation, not AI scene geometry."""
    import numpy as np
    from PIL import Image, ImageFilter, ImageOps

    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB").resize(
            (width, height),
            Image.Resampling.BILINEAR,
        )

    rgb = np.asarray(image, dtype=np.float32) / 255.0

    gray = (
        0.299 * rgb[:, :, 0]
        + 0.587 * rgb[:, :, 1]
        + 0.114 * rgb[:, :, 2]
    )

    saturation = np.max(rgb, axis=2) - np.min(rgb, axis=2)

    x = np.linspace(-1.0, 1.0, width, dtype=np.float32)
    y = np.linspace(-1.0, 1.0, height, dtype=np.float32)
    xx, yy = np.meshgrid(x, y)

    center_prior = np.exp(
        -((xx * xx) / 0.72 + (yy * yy) / 1.05) * 1.8
    )

    gray_image = Image.fromarray(
        np.clip(gray * 255.0, 0, 255).astype("uint8")
    )

    blurred = np.asarray(
        gray_image.filter(ImageFilter.GaussianBlur(radius=12)),
        dtype=np.float32,
    ) / 255.0

    local_contrast = np.clip(np.abs(gray - blurred) * 3.0, 0.0, 1.0)

    depth = (
        0.48 * center_prior
        + 0.25 * local_contrast
        + 0.12 * saturation
        + 0.15 * (1.0 - gray)
    )

    low = float(np.percentile(depth, 2))
    high = float(np.percentile(depth, 98))

    if high - low > 1e-6:
        depth = (depth - low) / (high - low)
    else:
        depth = np.full_like(depth, 0.5)

    depth = np.clip(depth, 0.0, 1.0)

    output = Image.fromarray(
        np.clip(depth * 255.0, 0, 255).astype("uint8")
    )

    output = output.filter(ImageFilter.GaussianBlur(radius=2))
    return ImageOps.autocontrast(output)

def save_manifest(entries, metadata, total_scenes, status, error=None):
    rows = []
    for number in sorted(entries):
        row = dict(entries[number])
        row.pop("_is_ai", None)
        rows.append(row)

    ai_count = sum(row.get("is_ai_estimated") is True for row in rows)
    fallback_count = len(rows) - ai_count
    document = {
        "version": VERSION,
        "status": status,
        "stage": "depth_maps",
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "model_signature": metadata.get("model_signature"),
        "format": metadata.get("format"),
        "scenes_min": metadata.get("scenes_min"),
        "scenes_max": metadata.get("scenes_max"),
        "actual_scene_counts": metadata.get("actual_scene_counts"),
        "total_scenes": total_scenes,
        "completed_scenes": len(rows),
        "ai_depth_count": ai_count,
        "fallback_count": fallback_count,
        "ai_required": AI_REQUIRED,
        "fallback_allowed": ALLOW_PSEUDO_FALLBACK,
        "model": MODEL_ID,
        "scenes": rows,
    }
    if error:
        document["last_error"] = str(error)[:3000]
    write_json(MANIFEST_FILE, document)

    checkpoint_rows = []
    by_number = {int(row.get("global_scene", row.get("scene", 0))): row for row in rows}
    for number in range(1, total_scenes + 1):
        row = by_number.get(number)
        checkpoint_rows.append({
            "global_scene": number,
            "scene_id": row.get("scene_id") if row else None,
            "part": row.get("part") if row else None,
            "local_scene": row.get("local_scene") if row else None,
            "role": row.get("role") if row else None,
            "voiceover_enabled": row.get("voiceover_enabled") if row else None,
            "narration_source": row.get("narration_source") if row else None,
            "source_text_sha256": row.get("source_text_sha256") if row else None,
            "source_word_count": row.get("source_word_count") if row else None,
            "visual_only": row.get("visual_only") if row else None,
            "image_sha256": row.get("image_sha256") if row else None,
            "job_signature": row.get("job_signature") if row else None,
            "source_fingerprint": row.get("source_fingerprint") if row else metadata.get("source_fingerprint"),
            "config_signature": row.get("config_signature") if row else metadata.get("config_signature"),
            "format": row.get("format") if row else metadata.get("format"),
            "depth_map": row.get("depth_map") if row else None,
            "depth_sha256": row.get("depth_sha256") if row else None,
            "size_bytes": row.get("size_bytes") if row else None,
            "model": row.get("model") if row else None,
            "model_signature": row.get("model_signature") if row else None,
            "status": row.get("status", "completed") if row else "pending",
        })
    checkpoint = {
        "version": VERSION,
        "stage": "depth_maps",
        "status": status,
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "model_signature": metadata.get("model_signature"),
        "format": metadata.get("format"),
        "scenes_min": metadata.get("scenes_min"),
        "scenes_max": metadata.get("scenes_max"),
        "actual_scene_counts": metadata.get("actual_scene_counts"),
        "total_scenes": total_scenes,
        "completed_scenes": len(rows),
        "jobs": checkpoint_rows,
    }
    if error:
        checkpoint["last_error"] = str(error)[:3000]
    write_json(CHECKPOINT_FILE, checkpoint)

def validate_final_manifest(prepared, entries):
    if len(entries) != len(prepared):
        raise RuntimeError(
            f"Depth manifest mismatch: {len(entries)}/{len(prepared)} scenes."
        )
    expected_ids = {item["scene_id"] for item in prepared}
    actual_ids = {
        row.get("scene_id") for row in entries.values()
        if isinstance(row, dict)
    }
    if actual_ids != expected_ids:
        raise RuntimeError("Depth manifest Scene IDs do not exactly match visual jobs.")

    for item in prepared:
        number = item["scene"]
        row = entries.get(number)
        if not isinstance(row, dict):
            raise RuntimeError(f"Scene {number}: depth manifest entry is missing.")
        for key in (
            "scene_id", "part", "local_scene", "role",
            "voiceover_enabled", "narration_source", "source_text_sha256",
            "source_word_count", "visual_only",
            "image_sha256", "job_signature", "source_fingerprint",
            "config_signature", "model_signature", "format",
        ):
            if row.get(key) != item.get(key):
                raise RuntimeError(f"Scene {number}: {key} does not match its source job.")

        path = safe_path(
            row.get("depth_map") or row.get("depth_path"),
            f"final depth map for scene {number}",
            must_exist=True,
        )
        expected_path = (DEPTH_DIR / f"depth_{number:04d}.png").resolve()
        if path.resolve() != expected_path:
            raise RuntimeError(f"Scene {number}: depth map path is not canonical.")
        if validate_depth(path, expected_size=(item["width"], item["height"])) is None:
            raise RuntimeError(f"Scene {number}: final depth map is invalid.")
        actual_hash = sha256_file(path)
        if row.get("depth_sha256") != actual_hash:
            raise RuntimeError(f"Scene {number}: depth map checksum mismatch.")
        if row.get("size_bytes") != path.stat().st_size:
            raise RuntimeError(f"Scene {number}: depth map byte count mismatch.")

        is_ai = row.get("is_ai_estimated") is True and row.get("model") == MODEL_ID
        if AI_REQUIRED and not is_ai:
            raise RuntimeError(f"Scene {number}: AI depth is required, but fallback was used.")
        if not is_ai and (
            not ALLOW_PSEUDO_FALLBACK or row.get("model") != FALLBACK_MODEL_ID
        ):
            raise RuntimeError(f"Scene {number}: pseudo-depth fallback is not permitted.")

def generate_fallback(item, destination):
    if not ALLOW_PSEUDO_FALLBACK:
        raise RuntimeError(
            f"Scene {item['scene']}: pseudo-depth fallback is disabled."
        )

    if AI_REQUIRED:
        raise RuntimeError(
            f"Scene {item['scene']}: AI depth is required; "
            "pseudo-depth fallback is not permitted."
        )

    temporary = destination.with_name(
        destination.stem + ".temporary.png"
    )

    try:
        depth = content_aware_pseudo_depth(
            item["image_path"],
            item["width"],
            item["height"],
        )

        depth.save(temporary, format="PNG")

        if validate_depth(
            temporary,
            expected_size=(item["width"], item["height"]),
        ) is None:
            raise RuntimeError("Pseudo-depth map failed validation.")

        temporary.replace(destination)

        log(
            f"WARNING: Scene {item['scene']} uses approximate "
            "pseudo-depth, not AI-estimated scene geometry."
        )

        return create_entry(
            item,
            destination,
            FALLBACK_MODEL_ID,
            "fallback_pseudo_depth",
        )

    finally:
        temporary.unlink(missing_ok=True)


def main():
    if not (ROOT / "Input" / "topic.txt").is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    if not MODEL_ID:
        raise RuntimeError("DEPTH_MODEL cannot be empty.")
    if len(MODEL_ID) > 256 or any(ord(char) < 32 for char in MODEL_ID):
        raise RuntimeError("DEPTH_MODEL contains invalid characters.")

    prepared, metadata = load_jobs()
    previous = load_previous(metadata)

    DEPTH_DIR.mkdir(parents=True, exist_ok=True)

    entries = {}
    pending = []
    upgrade_candidates = []

    for item in prepared:
        number = item["scene"]
        old = try_reuse(item, previous.get(number))

        if old is not None and old.get("_is_ai"):
            entries[number] = old
            log(f"Scene {number}: validated AI depth map reused.")

        elif old is not None and not UPGRADE_FALLBACK:
            if AI_REQUIRED:
                pending.append((item, old))
            else:
                entries[number] = old
                log(f"Scene {number}: validated fallback depth map reused.")

        elif old is not None:
            upgrade_candidates.append((item, old))

        else:
            pending.append((item, None))

    # Save all validated reusable outputs before loading the model.
    save_manifest(
        entries,
        metadata,
        len(prepared),
        "in_progress",
    )

    ai_work = pending + upgrade_candidates
    estimator = None

    if ai_work:
        try:
            log("Loading depth-estimation model:", MODEL_ID)
            estimator = load_depth_model()
            log("Depth-estimation model loaded.")

        except Exception as exc:
            log("WARNING: AI depth model unavailable:", exc)

            if AI_REQUIRED:
                save_manifest(
                    entries,
                    metadata,
                    len(prepared),
                    "failed",
                    f"AI model load failed: {exc}",
                )
                raise RuntimeError(
                    "AI depth is required, but the model could not load."
                ) from exc

            if not ALLOW_PSEUDO_FALLBACK:
                save_manifest(
                    entries,
                    metadata,
                    len(prepared),
                    "failed",
                    f"AI model load failed: {exc}",
                )
                raise RuntimeError(
                    "AI model unavailable and pseudo-depth fallback "
                    "is disabled."
                ) from exc

            log("Configured pseudo-depth fallback will be used.")

    generated_ai = 0
    generated_fallback = 0
    reused_ai = sum(
        row.get("_is_ai") is True for row in entries.values()
    )
    reused_fallback = sum(
        row.get("_is_ai") is False for row in entries.values()
    )

    try:
        for index, (item, old_fallback) in enumerate(ai_work, start=1):
            number = item["scene"]
            destination = DEPTH_DIR / f"depth_{number:04d}.png"
            entry = None

            log(
                f"[{index}/{len(ai_work)}] "
                f"Processing scene {number}/{len(prepared)}"
            )

            if estimator is not None:
                try:
                    entry = generate_ai_depth(
                        estimator,
                        item,
                        destination,
                    )

                    generated_ai += 1
                    log(f"Scene {number}: AI depth map generated.")

                except Exception as exc:
                    log(
                        f"WARNING: AI depth failed for scene {number}: "
                        f"{exc}"
                    )

                    if AI_REQUIRED:
                        raise RuntimeError(
                            f"Scene {number}: AI depth generation failed."
                        ) from exc

            if entry is None and old_fallback is not None:
                if ALLOW_PSEUDO_FALLBACK:
                    entry = dict(old_fallback)
                    entry.pop("_is_ai", None)
                    entry["status"] = "fallback_reused"
                    reused_fallback += 1

            if entry is None:
                entry = generate_fallback(item, destination)
                generated_fallback += 1

            entries[number] = entry

            # Every successfully processed scene is checkpointed.
            save_manifest(
                entries,
                metadata,
                len(prepared),
                "in_progress",
            )

            log(
                "Checkpoint saved:",
                entry["depth_map"],
                "|",
                entry["status"],
            )

        validate_final_manifest(prepared, entries)

        save_manifest(
            entries,
            metadata,
            len(prepared),
            "completed",
        )

    except Exception as exc:
        try:
            save_manifest(
                entries,
                metadata,
                len(prepared),
                "failed",
                exc,
            )
        except Exception as checkpoint_error:
            log(
                "WARNING: Failed to save error checkpoint:",
                checkpoint_error,
            )

        raise

    final_rows = [
        entries[number] for number in sorted(entries)
    ]

    ai_count = sum(
        row.get("model") == MODEL_ID
        for row in final_rows
    )

    fallback_count = len(final_rows) - ai_count

    log("=" * 55)
    log("DEPTH MAP GENERATION COMPLETE")
    log("Version:", VERSION)
    log("Scenes:", len(final_rows))
    log("AI depth maps:", ai_count)
    log("Approximate pseudo-depth maps:", fallback_count)
    log("New AI maps:", generated_ai)
    log("New fallback maps:", generated_fallback)
    log("Reused AI maps:", reused_ai)
    log("Reused fallback maps:", reused_fallback)
    log("AI required:", AI_REQUIRED)
    log("Pseudo-depth fallback allowed:", ALLOW_PSEUDO_FALLBACK)
    log("Fallback upgrade enabled:", UPGRADE_FALLBACK)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("Checkpoint:", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("DEPTH GENERATION CANCELLED")
        sys.exit(130)
    except Exception as exc:
        log("DEPTH GENERATION FAILED:", str(exc))
        traceback.print_exc()
        sys.exit(1)