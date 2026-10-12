#!/usr/bin/env python3
"""Resumable depth maps with validated image-hash checkpoints."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VISUALS_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
DEPTH_DIR = VISUALS_DIR / "depth_maps"
MANIFEST_FILE = DEPTH_DIR / "depth_manifest.json"

MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"
FALLBACK_MODEL_ID = "fallback-radial-pseudo-depth-v1"
MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100


def log(*args):
    print(*args, flush=True)


def write_json(path, data):
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
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log("WARNING: Cannot read JSON:", path, exc)
        return default


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} points outside the repository.")

    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jobs():
    data = read_json(JOBS_FILE)
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
        raise RuntimeError(
            "No jobs found. Run scripts/generate_visuals.py first."
        )

    from PIL import Image

    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not an object.")

        number = job.get("global_scene", job.get("scene", index))
        try:
            number = int(number)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene number at job {index}."
            ) from exc

        if number < 1 or number in seen:
            raise RuntimeError(f"Invalid or duplicate scene number: {number}")
        seen.add(number)

        image_path = safe_path(
            job.get("image_path") or job.get("image"),
            f"source image for scene {number}",
        )

        if not image_path.is_file():
            raise RuntimeError(f"Missing scene image: {image_path}")

        if image_path.stat().st_size < MIN_IMAGE_BYTES:
            raise RuntimeError(f"Scene {number} image is too small.")

        try:
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                width, height = image.size
                if width < 64 or height < 64:
                    raise RuntimeError("Image dimensions are too small.")
        except Exception as exc:
            raise RuntimeError(
                f"Invalid source image for scene {number}: {exc}"
            ) from exc

        prepared.append({
            "scene": number,
            "job": job,
            "image_path": image_path,
            "image_sha256": sha256_file(image_path),
            "width": width,
            "height": height,
        })

    prepared.sort(key=lambda item: item["scene"])
    numbers = [item["scene"] for item in prepared]

    if numbers != list(range(1, len(prepared) + 1)):
        raise RuntimeError(
            f"Scene numbers must be continuous from 1. Found: {numbers}"
        )

    return prepared


def load_previous():
    data = read_json(MANIFEST_FILE, [])
    if isinstance(data, dict):
        rows = data.get("scenes", data.get("depth_maps", []))
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    previous = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            number = int(row.get("global_scene", row.get("scene")))
            if number > 0:
                previous[number] = row
        except (TypeError, ValueError):
            continue

    return previous


def validate_depth(path):
    from PIL import Image

    try:
        if not path.is_file() or path.stat().st_size < MIN_DEPTH_BYTES:
            return None

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            width, height = image.size
            if width < 64 or height < 64:
                return None
            image.load()

        return width, height
    except Exception:
        return None


def make_entry(item, path, model, status):
    dimensions = validate_depth(path)
    if dimensions is None:
        raise RuntimeError(f"Generated depth map failed validation: {path}")

    return {
        "scene": item["scene"],
        "global_scene": item["scene"],
        "image": item["image_path"].relative_to(ROOT).as_posix(),
        "image_sha256": item["image_sha256"],
        "depth_map": path.relative_to(ROOT).as_posix(),
        "width": dimensions[0],
        "height": dimensions[1],
        "model": model,
        "status": status,
        "size_bytes": path.stat().st_size,
    }


def try_reuse(item, previous_row):
    if not isinstance(previous_row, dict):
        return None

    if previous_row.get("image_sha256") != item["image_sha256"]:
        return None

    raw_path = previous_row.get("depth_map")
    try:
        path = safe_path(raw_path, "previous depth map")
    except RuntimeError:
        return None

    dimensions = validate_depth(path)
    if dimensions is None:
        return None

    # Reuse valid output for an unchanged image even if it was made
    # by the fallback earlier. Avoid redownloading models unnecessarily.
    entry = dict(previous_row)
    entry.update({
        "scene": item["scene"],
        "global_scene": item["scene"],
        "image": item["image_path"].relative_to(ROOT).as_posix(),
        "image_sha256": item["image_sha256"],
        "depth_map": path.relative_to(ROOT).as_posix(),
        "width": dimensions[0],
        "height": dimensions[1],
        "size_bytes": path.stat().st_size,
        "status": "reused",
    })
    return entry


def generate_ai_depth(estimator, item, output_path):
    from PIL import Image, ImageOps

    temporary = output_path.with_name(output_path.stem + ".tmp.png")
    try:
        with Image.open(item["image_path"]) as source:
            source = source.convert("RGB")

        result = estimator(source)
        depth = result.get("depth") if isinstance(result, dict) else None

        if depth is None:
            raise RuntimeError("Depth model returned no depth image.")

        if not isinstance(depth, Image.Image):
            depth = Image.fromarray(depth)

        depth = ImageOps.autocontrast(depth.convert("L"))
        if depth.width < 64 or depth.height < 64:
            raise RuntimeError("AI depth map is too small.")

        depth.save(temporary, format="PNG")
        if validate_depth(temporary) is None:
            raise RuntimeError("AI depth PNG validation failed.")

        temporary.replace(output_path)
        return make_entry(item, output_path, MODEL_ID, "generated")

    finally:
        temporary.unlink(missing_ok=True)


def generate_pseudo_depth(item, output_path):
    """Create a radial approximation, not real scene geometry."""
    from PIL import Image

    temporary = output_path.with_name(output_path.stem + ".tmp.png")
    try:
        with Image.open(item["image_path"]) as source:
            width, height = source.size

        depth = Image.new("L", (width, height), 0)
        pixels = depth.load()
        cx = (width - 1) / 2.0
        cy = (height - 1) / 2.0
        max_distance = math.sqrt(cx * cx + cy * cy) or 1.0

        for y in range(height):
            for x in range(width):
                distance = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
                normalized = min(1.0, distance / max_distance)
                pixels[x, y] = int(
                    max(0, min(255, 20 + 220 * (1 - normalized) ** 1.6))
                )

        depth.save(temporary, format="PNG")
        if validate_depth(temporary) is None:
            raise RuntimeError("Pseudo-depth validation failed.")

        temporary.replace(output_path)
        log(
            f"WARNING: Scene {item['scene']} uses radial pseudo-depth; "
            "this does not estimate true foreground/background geometry."
        )
        return make_entry(
            item, output_path, FALLBACK_MODEL_ID, "fallback_pseudo_depth"
        )

    finally:
        temporary.unlink(missing_ok=True)


def save_manifest(entries):
    write_json(
        MANIFEST_FILE,
        [entries[number] for number in sorted(entries)],
    )


def main():
    input_file = ROOT / "Input" / "topic.txt"
    if not input_file.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    prepared = load_jobs()
    previous = load_previous()
    DEPTH_DIR.mkdir(parents=True, exist_ok=True)

    entries = {}
    pending = []

    # Validate and reuse first. Do not load a model if all maps are valid.
    for item in prepared:
        number = item["scene"]
        reused = try_reuse(item, previous.get(number))

        if reused is not None:
            entries[number] = reused
            log(f"Scene {number}: reusing validated depth map.")
        else:
            pending.append(item)

    save_manifest(entries)

    estimator = None
    model_error = None

    if pending:
        try:
            from transformers import pipeline

            log("Loading depth model:", MODEL_ID)
            estimator = pipeline(
                task="depth-estimation",
                model=MODEL_ID,
                device=-1,
            )
            log("Depth model loaded.")
        except Exception as exc:
            model_error = f"{type(exc).__name__}: {exc}"
            log("WARNING: AI depth model unavailable:", model_error)
            log("Using pseudo-depth fallback for missing maps.")

    generated_ai = 0
    generated_fallback = 0

    for index, item in enumerate(pending, start=1):
        number = item["scene"]
        output_path = DEPTH_DIR / f"depth_{number:04d}.png"
        log(f"[{index}/{len(pending)}] Creating depth for scene {number}")

        entry = None

        if estimator is not None:
            try:
                entry = generate_ai_depth(estimator, item, output_path)
                generated_ai += 1
            except Exception as exc:
                log(f"WARNING: AI depth failed for scene {number}: {exc}")

        if entry is None:
            entry = generate_pseudo_depth(item, output_path)
            generated_fallback += 1

        entries[number] = entry
        save_manifest(entries)
        log("Checkpoint saved:", entry["depth_map"], entry["status"])

    final_rows = [entries[number] for number in sorted(entries)]
    if len(final_rows) != len(prepared):
        raise RuntimeError(
            f"Manifest count mismatch: {len(final_rows)} / {len(prepared)}"
        )

    for item in prepared:
        row = entries.get(item["scene"])
        if not isinstance(row, dict):
            raise RuntimeError(f"Missing depth entry for scene {item['scene']}.")

        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(
                f"Source image hash mismatch for scene {item['scene']}."
            )

        path = safe_path(row.get("depth_map"), "final depth map")
        if validate_depth(path) is None:
            raise RuntimeError(
                f"Invalid final depth map for scene {item['scene']}."
            )

    log("=" * 60)
    log("DEPTH MAP CHECKPOINTS VALIDATED")
    log("Scenes:", len(prepared))
    log("Reused maps:", len(prepared) - len(pending))
    log("New AI maps:", generated_ai)
    log("New pseudo-depth maps:", generated_fallback)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    if model_error:
        log("Model loading error:", model_error)
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Depth generation cancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"DEPTH GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)