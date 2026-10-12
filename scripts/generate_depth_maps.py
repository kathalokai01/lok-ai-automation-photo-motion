#!/usr/bin/env python3
"""Resumable depth maps with an explicit pseudo-depth fallback."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_input_config, normalize_format

VISUALS_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
DEPTH_DIR = VISUALS_DIR / "depth_maps"
MANIFEST_FILE = DEPTH_DIR / "depth_manifest.json"

MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"
FALLBACK_MODEL_ID = "fallback-radial-pseudo-depth-v1"

MIN_IMAGE_SIZE = 1000
MIN_DEPTH_SIZE = 100


def log(*args):
    print(*args, flush=True)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} is outside the repository.")

    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jobs():
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            "Missing output/visuals/visual_jobs.json. "
            "Run scripts/generate_visuals.py first."
        )

    data = json.loads(JOBS_FILE.read_text(encoding="utf-8-sig"))

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
        raise RuntimeError("No visual scene jobs were found.")

    return jobs


def scene_number(job, index):
    value = job.get("global_scene", job.get("scene", index))

    if isinstance(value, dict):
        value = value.get("global_scene", value.get("number"))

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid scene number in job {index}: {value!r}"
        ) from exc

    if number < 1:
        raise RuntimeError("Scene numbers must be positive.")

    return number


def load_and_validate_jobs():
    from PIL import Image

    prepared = []
    seen = set()

    for index, job in enumerate(read_jobs(), start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not an object.")

        number = scene_number(job, index)
        if number in seen:
            raise RuntimeError(f"Duplicate scene number: {number}")
        seen.add(number)

        image_path = safe_path(
            job.get("image_path") or job.get("image"),
            f"image for scene {number}",
        )

        if not image_path.is_file():
            raise RuntimeError(f"Scene image missing: {image_path}")

        if image_path.stat().st_size < MIN_IMAGE_SIZE:
            raise RuntimeError(f"Scene {number} image is too small.")

        try:
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                if image.width < 64 or image.height < 64:
                    raise RuntimeError("Image dimensions below 64x64.")
        except Exception as exc:
            raise RuntimeError(
                f"Invalid image for scene {number}: {exc}"
            ) from exc

        prepared.append({
            "scene": number,
            "image_path": image_path,
            "image_sha256": sha256_file(image_path),
        })

    prepared.sort(key=lambda item: item["scene"])

    actual = [item["scene"] for item in prepared]
    expected = list(range(1, len(prepared) + 1))

    if actual != expected:
        raise RuntimeError(
            f"Scene numbers must be continuous from 1. Found: {actual}"
        )

    return prepared


def read_previous_manifest():
    if not MANIFEST_FILE.is_file():
        return {}

    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log("WARNING: Previous manifest unreadable:", exc)
        return {}

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("scenes", data.get("depth_maps", []))
    else:
        entries = []

    previous = {}
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                number = int(
                    entry.get("global_scene", entry.get("scene"))
                )
            except (TypeError, ValueError):
                continue
            if number > 0:
                previous[number] = entry

    return previous


def validate_existing_map(item, old, Image, allow_fallback):
    if not isinstance(old, dict):
        return None

    model = old.get("model")
    accepted = {MODEL_ID}

    if allow_fallback:
        accepted.add(FALLBACK_MODEL_ID)

    if model not in accepted:
        return None

    if old.get("image_sha256") != item["image_sha256"]:
        return None

    try:
        path = safe_path(
            old.get("depth_map"),
            f"previous depth map for scene {item['scene']}",
        )

        if not path.is_file() or path.stat().st_size < MIN_DEPTH_SIZE:
            return None

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            if image.width < 64 or image.height < 64:
                return None
            width, height = image.size

        entry = dict(old)
        entry.update({
            "scene": item["scene"],
            "global_scene": item["scene"],
            "image": item["image_path"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "depth_map": path.relative_to(ROOT).as_posix(),
            "width": width,
            "height": height,
            "size_bytes": path.stat().st_size,
            "status": "reused",
        })
        return entry

    except (OSError, ValueError, RuntimeError):
        return None


def make_manifest_entry(item, path, width, height, model, status):
    return {
        "scene": item["scene"],
        "global_scene": item["scene"],
        "image": item["image_path"].relative_to(ROOT).as_posix(),
        "image_sha256": item["image_sha256"],
        "depth_map": path.relative_to(ROOT).as_posix(),
        "width": width,
        "height": height,
        "model": model,
        "status": status,
        "size_bytes": path.stat().st_size,
    }


def generate_model_depth(estimator, item, output_path, Image, ImageOps):
    temporary = output_path.with_name(output_path.stem + ".tmp.png")

    try:
        with Image.open(item["image_path"]) as source:
            source_rgb = source.convert("RGB")

        result = estimator(source_rgb)
        depth = result.get("depth")

        if depth is None:
            raise RuntimeError("Depth model returned no depth image.")

        if not isinstance(depth, Image.Image):
            depth = Image.fromarray(depth)

        depth = ImageOps.autocontrast(depth.convert("L"))

        if depth.width < 64 or depth.height < 64:
            raise RuntimeError("Generated depth map is too small.")

        depth.save(temporary, format="PNG")
        temporary.replace(output_path)

        with Image.open(output_path) as check:
            check.verify()

        return make_manifest_entry(
            item, output_path, depth.width, depth.height,
            MODEL_ID, "generated",
        )

    except Exception:
        temporary.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        raise


def generate_pseudo_depth(item, output_path, Image, ImageDraw):
    """Create a clearly labelled synthetic depth approximation.

    This is NOT AI-estimated scene geometry. It is only a fallback
    to let the existing subtle photo-motion renderer continue.
    """
    temporary = output_path.with_name(output_path.stem + ".tmp.png")

    try:
        with Image.open(item["image_path"]) as source:
            width, height = source.size

        # Bright centre, darker edges. The map is intentionally subtle;
        # it cannot infer the true foreground/background of the photo.
        width = max(64, width)
        height = max(64, height)
        depth = Image.new("L", (width, height), 0)
        pixels = depth.load()

        cx = (width - 1) / 2.0
        cy = (height - 1) / 2.0
        max_distance = math.sqrt(cx * cx + cy * cy) or 1.0

        for y in range(height):
            for x in range(width):
                distance = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
                normalized = min(1.0, distance / max_distance)
                # Smooth centre-to-edge falloff.
                value = int(220 * (1.0 - normalized) ** 1.6 + 20)
                pixels[x, y] = max(0, min(255, value))

        depth.save(temporary, format="PNG")
        temporary.replace(output_path)

        with Image.open(output_path) as check:
            check.verify()

        log(
            f"WARNING: Scene {item['scene']} uses pseudo-depth, "
            "not model-estimated depth."
        )

        return make_manifest_entry(
            item, output_path, width, height,
            FALLBACK_MODEL_ID, "fallback_pseudo_depth",
        )

    except Exception:
        temporary.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        raise


def save_manifest(entries):
    atomic_json(
        MANIFEST_FILE,
        [entries[number] for number in sorted(entries)],
    )


def main():
    input_file = ROOT / "Input" / "topic.txt"
    if not input_file.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_input_config(input_file)
    normalize_format(config)

    from PIL import Image, ImageOps, ImageDraw

    prepared = load_and_validate_jobs()
    DEPTH_DIR.mkdir(parents=True, exist_ok=True)

    previous = read_previous_manifest()
    current_numbers = {item["scene"] for item in prepared}

    entries = {
        number: dict(entry)
        for number, entry in previous.items()
        if number in current_numbers and isinstance(entry, dict)
    }
    save_manifest(entries)

    estimator = None
    model_error = None

    try:
        from transformers import pipeline

        log("Loading free depth model:", MODEL_ID)
        estimator = pipeline(
            task="depth-estimation",
            model=MODEL_ID,
            device=-1,
        )
        log("Depth model loaded successfully.")

    except Exception as exc:
        model_error = str(exc)
        estimator = None
        log("WARNING: Depth model unavailable:", model_error)
        log("Using pseudo-depth fallback where needed.")

    reused_count = 0
    model_count = 0
    fallback_count = 0

    log("=" * 60)
    log("KATHA LOK AI DEPTH MAP GENERATION")
    log("Scenes:", len(prepared))
    log("Mode:", "AI depth with per-scene fallback" if estimator else "pseudo-depth fallback")
    log("=" * 60)

    for index, item in enumerate(prepared, start=1):
        number = item["scene"]
        output_path = DEPTH_DIR / f"depth_{number:04d}.png"

        log(f"[{index}/{len(prepared)}] Scene {number}")

        old_entry = previous.get(number)
        reusable = validate_existing_map(
            item,
            old_entry,
            Image,
            allow_fallback=(estimator is None),
        )

        if reusable is not None:
            entries[number] = reusable
            reused_count += 1
            if reusable.get("model") == FALLBACK_MODEL_ID:
                fallback_count += 1
            else:
                model_count += 1
            save_manifest(entries)
            log("Reused validated map:", reusable["depth_map"])
            continue

        entry = None

        if estimator is not None:
            try:
                entry = generate_model_depth(
                    estimator, item, output_path, Image, ImageOps
                )
                model_count += 1
                log("AI depth map generated.")
            except Exception as exc:
                log(
                    f"WARNING: AI depth failed for scene {number}: {exc}"
                )

        if entry is None:
            entry = generate_pseudo_depth(
                item, output_path, Image, ImageDraw
            )
            fallback_count += 1

        entries[number] = entry
        save_manifest(entries)
        log(
            "Checkpoint saved:",
            entry["depth_map"],
            "| status:",
            entry["status"],
        )

    final_rows = [entries[number] for number in sorted(entries)]

    if len(final_rows) != len(prepared):
        raise RuntimeError(
            f"Manifest count mismatch: {len(final_rows)} / {len(prepared)}"
        )

    for item in prepared:
        number = item["scene"]
        row = entries.get(number)

        if not isinstance(row, dict):
            raise RuntimeError(f"Missing manifest entry for scene {number}.")

        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(
                f"Source image hash mismatch for scene {number}."
            )

        path = safe_path(row.get("depth_map"), f"depth map for scene {number}")
        if not path.is_file() or path.stat().st_size < MIN_DEPTH_SIZE:
            raise RuntimeError(f"Invalid final depth map for scene {number}.")

        with Image.open(path) as image:
            image.verify()

    log("=" * 60)
    log("DEPTH MAP GENERATION COMPLETE")
    log("Scenes validated:", len(prepared))
    log("AI depth maps generated/reused:", model_count)
    log("Pseudo-depth fallbacks generated/reused:", fallback_count)
    log("Maps reused:", reused_count)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    if model_error:
        log("Initial model error:", model_error)
    log(
        "NOTE: Pseudo-depth is an approximation; it does not estimate "
        "true scene geometry."
    )
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Depth generation interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"DEPTH GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)