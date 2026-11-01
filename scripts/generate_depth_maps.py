#!/usr/bin/env python3
"""Generate validated, resumable AI depth maps for Photo Motion."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VISUALS_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
DEPTH_DIR = VISUALS_DIR / "depth_maps"
MANIFEST_FILE = DEPTH_DIR / "depth_manifest.json"

MODEL_ID = os.environ.get(
    "DEPTH_MODEL",
    "depth-anything/Depth-Anything-V2-Small-hf",
).strip()

FALLBACK_MODEL_ID = "content-aware-pseudo-depth-v6"
VERSION = "photo-motion-depth-v7"

MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100
MIN_DIMENSION = 64
MAX_DIMENSION = 4096

UPGRADE_FALLBACK = os.environ.get(
    "DEPTH_UPGRADE_FALLBACK", "false"
).strip().lower() in {"1", "true", "yes", "on"}

AI_REQUIRED = os.environ.get(
    "DEPTH_AI_REQUIRED", "false"
).strip().lower() in {"1", "true", "yes", "on"}


def log(*values):
    print(*values, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label} path.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} path escapes the project directory.")

    return path


def load_jobs():
    from PIL import Image

    document = read_json(JOBS_FILE, {})
    if isinstance(document, list):
        jobs = document
        metadata = {}
    elif isinstance(document, dict):
        jobs = (
            document.get("jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
            or []
        )
        metadata = document
    else:
        raise RuntimeError("visual_jobs.json has an unsupported structure.")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "No visual jobs found. Run generate_visuals.py first."
        )

    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
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

        if number < 1 or number in seen:
            raise RuntimeError(f"Duplicate or invalid scene number: {number}")
        seen.add(number)

        image_value = (
            job.get("image_path")
            or job.get("image")
            or job.get("output_path")
        )
        image_path = safe_path(
            image_value,
            f"source image for scene {number}",
        )

        if (
            not image_path.is_file()
            or image_path.stat().st_size < MIN_IMAGE_BYTES
        ):
            raise RuntimeError(
                f"Scene {number}: source image is missing or too small: "
                f"{image_path}"
            )

        try:
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                width, height = image.size
                image_format = (image.format or "").upper()
                if (
                    width < MIN_DIMENSION
                    or height < MIN_DIMENSION
                    or width > MAX_DIMENSION
                    or height > MAX_DIMENSION
                ):
                    raise ValueError(
                        f"Unsupported dimensions {width}x{height}."
                    )
                if image_format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError(
                        f"Unsupported image format: {image_format}."
                    )
        except Exception as exc:
            raise RuntimeError(
                f"Scene {number}: source image validation failed: {exc}"
            ) from exc

        prepared.append({
            "scene": number,
            "job": job,
            "image_path": image_path,
            "image_sha256": sha256_file(image_path),
            "width": width,
            "height": height,
        })

    prepared.sort(key=lambda row: row["scene"])
    numbers = [row["scene"] for row in prepared]

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
            if image.mode not in {"L", "I", "I;16", "F"}:
                image = image.convert("L")

            width, height = image.size
            if width < MIN_DIMENSION or height < MIN_DIMENSION:
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

    job = item["job"]
    relative_image = item["image_path"].relative_to(ROOT).as_posix()

    return {
        "scene": item["scene"],
        "global_scene": item["scene"],
        "global_scene_number": item["scene"],
        "part": job.get("part"),
        "local_scene": job.get(
            "local_scene",
            job.get("scene"),
        ),
        "image": relative_image,
        "image_path": relative_image,
        "image_sha256": item["image_sha256"],
        "depth_map": path.relative_to(ROOT).as_posix(),
        "depth_path": path.relative_to(ROOT).as_posix(),
        "width": dimensions[0],
        "height": dimensions[1],
        "model": model,
        "status": status,
        "size_bytes": path.stat().st_size,
        "depth_sha256": sha256_file(path),
        "script_version": VERSION,
    }


def try_reuse(item, old):
    if not isinstance(old, dict):
        return None

    if old.get("image_sha256") != item["image_sha256"]:
        return None

    try:
        path = safe_path(
            old.get("depth_map")
            or old.get("depth_path")
            or old.get("path")
            or old.get("file"),
            "previous depth map",
        )
    except RuntimeError:
        return None

    dimensions = validate_depth(
        path,
        expected_size=(item["width"], item["height"]),
    )
    if dimensions is None:
        return None

    expected_hash = old.get("depth_sha256")
    if expected_hash and sha256_file(path) != expected_hash:
        return None

    model = str(old.get("model", "")).strip()
    is_ai = (
        model == MODEL_ID
        and old.get("status") not in {
            "fallback_pseudo_depth",
            "pseudo_depth",
            "fallback_reused",
        }
    )

    status = "reused" if is_ai else "fallback_reused"
    entry = create_entry(
        item,
        path,
        model or FALLBACK_MODEL_ID,
        status,
    )
    entry["_is_ai"] = is_ai
    return entry


def load_depth_model():
    from transformers import pipeline

    return pipeline(
        task="depth-estimation",
        model=MODEL_ID,
        device=-1,
    )


def generate_ai_depth(estimator, item, destination):
    from PIL import Image, ImageOps

    temporary = destination.with_name(
        destination.stem + ".temporary.png"
    )

    try:
        with Image.open(item["image_path"]) as image:
            source = image.convert("RGB")

        result = estimator(source)
        depth = result.get("depth") if isinstance(result, dict) else None

        if depth is None:
            raise RuntimeError("Depth model returned no depth image.")

        if not isinstance(depth, Image.Image):
            import numpy as np
            depth = Image.fromarray(np.asarray(depth))

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
    """
    Create an explicitly labelled approximation using image content.
    This is not a replacement for AI-estimated scene geometry.
    """
    import numpy as np
    from PIL import Image, ImageFilter, ImageOps

    with Image.open(image_path) as source:
        image = source.convert("RGB").resize(
            (width, height),
            Image.Resampling.BILINEAR,
        )

    rgb = np.asarray(image, dtype=np.float32) / 255.0
    gray = (
        0.299 * rgb[:, :, 0]
        + 0.587 * rgb[:, :, 1]
        + 0.114 * rgb[:, :, 2]
    )

    saturation = (
        np.max(rgb, axis=2) - np.min(rgb, axis=2)
    )

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

    local_contrast = np.abs(gray - blurred)
    local_contrast = np.clip(local_contrast * 3.0, 0.0, 1.0)

    # A restrained center prior plus image-derived contrast and saturation.
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
    depth_image = Image.fromarray(
        np.clip(depth * 255.0, 0, 255).astype("uint8")
    )

    depth_image = depth_image.filter(
        ImageFilter.GaussianBlur(radius=2)
    )
    return ImageOps.autocontrast(depth_image)


def generate_fallback(item, destination):
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
            f"WARNING: Scene {item['scene']} uses content-aware "
            "pseudo-depth. It is not AI-estimated geometry."
        )

        return create_entry(
            item,
            destination,
            FALLBACK_MODEL_ID,
            "fallback_pseudo_depth",
        )
    finally:
        temporary.unlink(missing_ok=True)


def save_manifest(entries, metadata, total_scenes):
    rows = []

    for number in sorted(entries):
        row = dict(entries[number])
        row.pop("_is_ai", None)
        rows.append(row)

    ai_count = sum(row.get("model") == MODEL_ID for row in rows)
    fallback_count = len(rows) - ai_count

    document = {
        "version": VERSION,
        "status": (
            "completed"
            if len(rows) == total_scenes
            else "in_progress"
        ),
        "source_fingerprint": metadata.get("source_fingerprint"),
        "format": metadata.get("format"),
        "total_scenes": total_scenes,
        "completed_scenes": len(rows),
        "ai_depth_count": ai_count,
        "fallback_count": fallback_count,
        "scenes": rows,
    }

    write_json(MANIFEST_FILE, document)


def validate_final_manifest(prepared, entries):
    if len(entries) != len(prepared):
        raise RuntimeError(
            f"Depth manifest mismatch: {len(entries)}/{len(prepared)} scenes."
        )

    for item in prepared:
        number = item["scene"]
        row = entries.get(number)

        if not isinstance(row, dict):
            raise RuntimeError(f"Scene {number}: manifest entry is missing.")

        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(
                f"Scene {number}: source image checksum mismatch."
            )

        path = safe_path(
            row.get("depth_map") or row.get("depth_path"),
            f"final depth map for scene {number}",
        )

        if validate_depth(
            path,
            expected_size=(item["width"], item["height"]),
        ) is None:
            raise RuntimeError(
                f"Scene {number}: final depth map is invalid."
            )

        if row.get("depth_sha256") != sha256_file(path):
            raise RuntimeError(
                f"Scene {number}: depth map checksum mismatch."
            )

        if row.get("model") != MODEL_ID and AI_REQUIRED:
            raise RuntimeError(
                f"Scene {number}: AI depth was required, "
                "but only fallback pseudo-depth is available."
            )


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
            entries[number] = old
            log(f"Scene {number}: validated fallback depth map reused.")

        elif old is not None:
            upgrade_candidates.append((item, old))

        else:
            pending.append((item, None))

    # Save valid reused entries before attempting downloads or model loading.
    save_manifest(entries, metadata, len(prepared))

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
                raise RuntimeError(
                    "AI depth is required, but the model could not load: "
                    f"{exc}"
                ) from exc

            log("Configured fallback will be used for missing scenes.")

    generated_ai = 0
    generated_fallback = 0
    reused_fallback = sum(
        row.get("model") != MODEL_ID for row in entries.values()
    )

    for index, (item, old_fallback) in enumerate(ai_work, start=1):
        number = item["scene"]
        destination = DEPTH_DIR / f"depth_{number:04d}.png"
        entry = None

        log(f"[{index}/{len(ai_work)}] Processing scene {number}")

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
                log(f"WARNING: AI depth failed for scene {number}: {exc}")

                if AI_REQUIRED:
                    raise RuntimeError(
                        f"Scene {number}: AI depth generation failed."
                    ) from exc

        if entry is None and old_fallback is not None:
            entry = dict(old_fallback)
            entry.pop("_is_ai", None)
            entry["status"] = "fallback_reused"
            reused_fallback += 1

        elif entry is None:
            entry = generate_fallback(item, destination)
            generated_fallback += 1

        entries[number] = entry

        # Every successful scene is checkpointed immediately.
        save_manifest(entries, metadata, len(prepared))
        log(
            "Checkpoint saved:",
            entry["depth_map"],
            "|",
            entry["status"],
        )

    validate_final_manifest(prepared, entries)
    save_manifest(entries, metadata, len(prepared))

    final_rows = [
        entries[number] for number in sorted(entries)
    ]
    ai_count = sum(
        row.get("model") == MODEL_ID for row in final_rows
    )
    fallback_count = len(final_rows) - ai_count

    log("=" * 55)
    log("DEPTH MAP GENERATION COMPLETE")
    log("Version:", VERSION)
    log("Scenes:", len(final_rows))
    log("AI depth maps:", ai_count)
    log("Pseudo-depth maps:", fallback_count)
    log("New AI maps:", generated_ai)
    log("New fallback maps:", generated_fallback)
    log("Reused fallback maps:", reused_fallback)
    log("AI required:", AI_REQUIRED)
    log("Fallback upgrade enabled:", UPGRADE_FALLBACK)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("DEPTH GENERATION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"DEPTH GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)