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

VERSION = "photo-motion-depth-v8-validated-resume"
FALLBACK_MODEL_ID = "content-aware-pseudo-depth-v8"

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
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
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

    image_data = read_json(IMAGE_MANIFEST, {})
    image_rows = {}

    if isinstance(image_data, dict):
        image_rows = {
            str(row.get("scene_id")): row
            for row in image_data.get("scenes", [])
            if isinstance(row, dict) and row.get("scene_id")
        }
    elif isinstance(image_data, list):
        image_rows = {
            str(row.get("scene_id")): row
            for row in image_data
            if isinstance(row, dict) and row.get("scene_id")
        }

    prepared = []
    seen_numbers = set()
    seen_paths = set()

    for index, job in enumerate(rows, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Visual job {index} is not an object.")

        raw_number = job.get(
            "global_scene",
            job.get("global_scene_number", job.get("scene", index)),
        )

        try:
            number = int(raw_number)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid global scene number in job {index}."
            ) from exc

        if number < 1 or number in seen_numbers:
            raise RuntimeError(
                f"Duplicate or invalid global scene number: {number}"
            )
        seen_numbers.add(number)

        scene_id = str(
            job.get("scene_id") or f"scene_{number:04d}"
        ).strip()

        if not scene_id:
            raise RuntimeError(f"Scene {number}: scene_id is empty.")

        image_value = (
            job.get("image_path")
            or job.get("image")
            or job.get("output_path")
        )

        image_path = safe_path(
            image_value,
            f"source image for scene {number}",
            must_exist=True,
        )

        if image_path in seen_paths:
            raise RuntimeError(
                f"Multiple scenes reference the same image: {image_path}"
            )
        seen_paths.add(image_path)

        width, height = image_info(image_path, number)
        image_hash = sha256_file(image_path)

        image_row = image_rows.get(scene_id, {})
        declared_hash = image_row.get("image_sha256")

        if declared_hash and declared_hash != image_hash:
            raise RuntimeError(
                f"Scene {number}: image manifest checksum mismatch."
            )

        prepared.append({
            "scene": number,
            "global_scene": number,
            "scene_id": scene_id,
            "part": job.get("part"),
            "local_scene": job.get("local_scene", job.get("scene")),
            "role": job.get("role", "main"),
            "job": job,
            "image_path": image_path,
            "image_sha256": image_hash,
            "width": width,
            "height": height,
            "job_signature": digest_data(job),
        })

    prepared.sort(key=lambda item: item["scene"])
    numbers = [item["scene"] for item in prepared]

    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(
            "Global scene numbers must be continuous from 1. "
            f"Received: {numbers}"
        )

    return prepared, metadata


def load_previous():
    document = read_json(MANIFEST_FILE, {})
    if isinstance(document, dict):
        rows = document.get("scenes", document.get("depth_maps", []))
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
                number = int(
                    row.get("global_scene", row.get("scene"))
                )
            except (TypeError, ValueError):
                continue

            if number > 0:
                previous[number] = row

    return previous


def validate_depth(path, expected_size=None):
    from PIL import Image

    try:
        path = Path(path)

        if not path.is_file() or path.stat().st_size < MIN_DEPTH_BYTES:
            return None

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            image.load()
            width, height = image.size

            if image.mode not in {"L", "I", "I;16", "F"}:
                image.convert("L")

        if (
            width < MIN_DIMENSION
            or height < MIN_DIMENSION
            or width > MAX_DIMENSION
            or height > MAX_DIMENSION
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

    relative_image = item["image_path"].relative_to(ROOT).as_posix()
    relative_depth = Path(path).relative_to(ROOT).as_posix()

    return {
        "scene": item["scene"],
        "global_scene": item["scene"],
        "global_scene_number": item["scene"],
        "scene_id": item["scene_id"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "role": item["role"],
        "image": relative_image,
        "image_path": relative_image,
        "image_sha256": item["image_sha256"],
        "job_signature": item["job_signature"],
        "depth_map": relative_depth,
        "depth_path": relative_depth,
        "width": dimensions[0],
        "height": dimensions[1],
        "model": model,
        "status": status,
        "is_ai_estimated": model != FALLBACK_MODEL_ID,
        "size_bytes": Path(path).stat().st_size,
        "depth_sha256": sha256_file(path),
        "script_version": VERSION,
    }


def try_reuse(item, old):
    if not isinstance(old, dict):
        return None

    if old.get("image_sha256") != item["image_sha256"]:
        return None

    if old.get("job_signature") not in (None, item["job_signature"]):
        return None

    try:
        path = safe_path(
            old.get("depth_map")
            or old.get("depth_path")
            or old.get("path")
            or old.get("file"),
            "previous depth map",
            must_exist=True,
        )
    except RuntimeError:
        return None

    if validate_depth(
        path,
        expected_size=(item["width"], item["height"]),
    ) is None:
        return None

    expected_hash = old.get("depth_sha256")
    actual_hash = sha256_file(path)

    if expected_hash and expected_hash != actual_hash:
        return None

    model = str(old.get("model", "")).strip()
    if not model:
        return None

    is_ai = (
        model != FALLBACK_MODEL_ID
        and "pseudo" not in model.lower()
        and "fallback" not in str(old.get("status", "")).lower()
    )

    if is_ai and model != MODEL_ID:
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

    ai_count = sum(
        row.get("model") == MODEL_ID
        and row.get("status") not in {
            "fallback_pseudo_depth",
            "fallback_reused",
            "pseudo_depth",
        }
        for row in rows
    )

    fallback_count = len(rows) - ai_count

    document = {
        "version": VERSION,
        "status": status,
        "source_fingerprint": metadata.get("source_fingerprint"),
        "format": metadata.get("format"),
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

    for number in range(1, total_scenes + 1):
        row = entries.get(number)

        checkpoint_rows.append({
            "global_scene": number,
            "scene_id": (
                row.get("scene_id") if isinstance(row, dict) else None
            ),
            "status": (
                row.get("status", "completed")
                if isinstance(row, dict)
                else "pending"
            ),
            "depth_map": (
                row.get("depth_map") if isinstance(row, dict) else None
            ),
            "depth_sha256": (
                row.get("depth_sha256") if isinstance(row, dict) else None
            ),
        })

    checkpoint = {
        "version": VERSION,
        "stage": "depth_maps",
        "status": status,
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
            f"Depth manifest mismatch: "
            f"{len(entries)}/{len(prepared)} scenes."
        )

    for item in prepared:
        number = item["scene"]
        row = entries.get(number)

        if not isinstance(row, dict):
            raise RuntimeError(
                f"Scene {number}: depth manifest entry is missing."
            )

        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(
                f"Scene {number}: source image checksum mismatch."
            )

        if row.get("job_signature") != item["job_signature"]:
            raise RuntimeError(
                f"Scene {number}: source job signature mismatch."
            )

        path = safe_path(
            row.get("depth_map") or row.get("depth_path"),
            f"final depth map for scene {number}",
            must_exist=True,
        )

        if validate_depth(
            path,
            expected_size=(item["width"], item["height"]),
        ) is None:
            raise RuntimeError(
                f"Scene {number}: final depth map is invalid."
            )

        actual_hash = sha256_file(path)

        if row.get("depth_sha256") != actual_hash:
            raise RuntimeError(
                f"Scene {number}: depth map checksum mismatch."
            )

        is_ai = row.get("model") == MODEL_ID

        if AI_REQUIRED and not is_ai:
            raise RuntimeError(
                f"Scene {number}: AI depth is required, "
                "but this scene has fallback depth."
            )

        if not is_ai and not ALLOW_PSEUDO_FALLBACK:
            raise RuntimeError(
                f"Scene {number}: pseudo-depth fallback is disabled."
            )


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

    prepared, metadata = load_jobs()
    previous = load_previous()

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