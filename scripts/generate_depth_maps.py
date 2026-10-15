
#!/usr/bin/env python3
"""Resumable depth-map generation with truthful fallback labelling."""

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

MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"
FALLBACK_MODEL_ID = "fallback-radial-pseudo-depth-v5"
VERSION = "depth-checkpoint-v6"
MIN_IMAGE_BYTES = 1000
MIN_DEPTH_BYTES = 100

# Default: preserve successful fallback checkpoints and avoid repeated model
# downloads. Set DEPTH_UPGRADE_FALLBACK=true to retry AI for old fallbacks.
UPGRADE_FALLBACK = (
    os.environ.get("DEPTH_UPGRADE_FALLBACK", "").strip().lower()
    in {"1", "true", "yes", "on"}
)


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


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
        raise RuntimeError(f"Missing path for {label}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} points outside the project.")
    return path


def load_jobs():
    from PIL import Image

    document = read_json(JOBS_FILE, {})
    if isinstance(document, list):
        jobs = document
    elif isinstance(document, dict):
        jobs = (
            document.get("jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
            or []
        )
    else:
        jobs = []

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "No image jobs found. Run generate_visuals.py first."
        )

    result = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not an object.")

        try:
            number = int(job.get("global_scene", job.get("scene", index)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene number in job {index}."
            ) from exc

        if number < 1 or number in seen:
            raise RuntimeError(f"Duplicate or invalid scene number: {number}")
        seen.add(number)

        image_path = safe_path(
            job.get("image_path") or job.get("image"),
            f"source image for scene {number}",
        )

        if (
            not image_path.is_file()
            or image_path.stat().st_size < MIN_IMAGE_BYTES
        ):
            raise RuntimeError(
                f"Missing or too-small source image for scene {number}."
            )

        try:
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                width, height = image.size
                if width < 64 or height < 64:
                    raise ValueError("Image dimensions are too small.")
        except Exception as exc:
            raise RuntimeError(
                f"Invalid source image for scene {number}: {exc}"
            ) from exc

        result.append({
            "scene": number,
            "job": job,
            "image_path": image_path,
            "image_sha256": sha256_file(image_path),
            "width": width,
            "height": height,
        })

    result.sort(key=lambda item: item["scene"])
    numbers = [item["scene"] for item in result]
    if numbers != list(range(1, len(result) + 1)):
        raise RuntimeError(
            f"Global scene numbers must be continuous from 1: {numbers}"
        )

    return result


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
                number = int(row.get("global_scene", row.get("scene")))
            except (TypeError, ValueError):
                continue
            if number > 0:
                previous[number] = row
    return previous


def validate_depth(path):
    from PIL import Image

    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size < MIN_DEPTH_BYTES:
            return None

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            if image.width < 64 or image.height < 64:
                return None
            return image.size
    except Exception:
        return None


def create_entry(item, path, model, status):
    dimensions = validate_depth(path)
    if dimensions is None:
        raise RuntimeError(f"Depth map failed validation: {path}")

    job = item["job"]
    return {
        "scene": item["scene"],
        "global_scene": item["scene"],
        "part": job.get("part"),
        "local_scene": job.get("local_scene", job.get("scene")),
        "image": item["image_path"].relative_to(ROOT).as_posix(),
        "image_sha256": item["image_sha256"],
        "depth_map": path.relative_to(ROOT).as_posix(),
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
            old.get("depth_map") or old.get("path") or old.get("file"),
            "previous depth map",
        )
    except RuntimeError:
        return None

    dimensions = validate_depth(path)
    if dimensions is None:
        return None

    expected_hash = old.get("depth_sha256")
    if expected_hash:
        try:
            if sha256_file(path) != expected_hash:
                return None
        except OSError:
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

    if is_ai:
        status = "reused"
    else:
        status = "fallback_reused"

    entry = create_entry(item, path, model or FALLBACK_MODEL_ID, status)
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

    temporary = destination.with_name(destination.stem + ".tmp.png")
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

        depth = ImageOps.autocontrast(depth.convert("L"))
        if depth.size != (item["width"], item["height"]):
            depth = depth.resize(
                (item["width"], item["height"]),
                Image.Resampling.BILINEAR,
            )

        depth.save(temporary, format="PNG")
        if validate_depth(temporary) is None:
            raise RuntimeError("Generated AI depth map is invalid.")

        temporary.replace(destination)
        return create_entry(item, destination, MODEL_ID, "generated")
    finally:
        temporary.unlink(missing_ok=True)


def radial_depth(width, height):
    """Create an approximation, explicitly not AI-estimated geometry."""
    import numpy as np
    from PIL import Image

    x = np.linspace(-1.0, 1.0, width, dtype=np.float32)
    y = np.linspace(-1.0, 1.0, height, dtype=np.float32)
    radius = np.sqrt(y[:, None] ** 2 + x[None, :] ** 2)
    radius = np.clip(radius / math.sqrt(2.0), 0.0, 1.0)
    values = 20.0 + 220.0 * np.power(1.0 - radius, 1.6)
    return Image.fromarray(np.clip(values, 0, 255).astype("uint8"))


def generate_fallback(item, destination):
    temporary = destination.with_name(destination.stem + ".tmp.png")
    try:
        depth = radial_depth(item["width"], item["height"])
        depth.save(temporary, format="PNG")

        if validate_depth(temporary) is None:
            raise RuntimeError("Fallback depth map failed validation.")

        temporary.replace(destination)
        log(
            f"WARNING: Scene {item['scene']} uses radial pseudo-depth; "
            "this is not AI-estimated geometry."
        )
        return create_entry(
            item,
            destination,
            FALLBACK_MODEL_ID,
            "fallback_pseudo_depth",
        )
    finally:
        temporary.unlink(missing_ok=True)


def save_manifest(entries, metadata):
    rows = []
    for number in sorted(entries):
        row = dict(entries[number])
        row.pop("_is_ai", None)
        rows.append(row)

    write_json(MANIFEST_FILE, {
        "version": VERSION,
        "source_fingerprint": metadata.get("source_fingerprint"),
        "format": metadata.get("format"),
        "total_scenes": len(rows),
        "ai_depth_count": sum(
            row.get("model") == MODEL_ID for row in rows
        ),
        "fallback_count": sum(
            row.get("model") != MODEL_ID for row in rows
        ),
        "scenes": rows,
    })


def main():
    if not (ROOT / "Input" / "topic.txt").is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    jobs_document = read_json(JOBS_FILE, {})
    metadata = jobs_document if isinstance(jobs_document, dict) else {}

    prepared = load_jobs()
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
            log(f"Scene {number}: reusing validated AI depth map.")
        elif old is not None and not UPGRADE_FALLBACK:
            entries[number] = old
            log(
                f"Scene {number}: reusing validated pseudo-depth; "
                "AI upgrade is disabled."
            )
        elif old is not None:
            upgrade_candidates.append((item, old))
        else:
            pending.append((item, None))

    # Only load the AI model when a scene actually needs a new map or an
    # explicitly requested upgrade. Do not download a model for pure reuse.
    ai_work = pending + upgrade_candidates
    estimator = None

    if ai_work:
        try:
            log("Loading depth model:", MODEL_ID)
            estimator = load_depth_model()
            log("Depth model ready.")
        except Exception as exc:
            log("WARNING: AI depth model unavailable:", exc)
            log("Missing maps will use labelled pseudo-depth.")

    generated_ai = 0
    generated_fallback = 0
    reused_fallback = sum(
        row.get("model") != MODEL_ID for row in entries.values()
    )

    for index, (item, old_fallback) in enumerate(ai_work, start=1):
        number = item["scene"]
        destination = DEPTH_DIR / f"depth_{number:04d}.png"
        entry = None

        log(f"[{index}/{len(ai_work)}] Scene {number}")

        if estimator is not None:
            try:
                entry = generate_ai_depth(estimator, item, destination)
                generated_ai += 1
            except Exception as exc:
                log(f"WARNING: AI depth failed for scene {number}: {exc}")

        if entry is None and old_fallback is not None:
            entry = dict(old_fallback)
            entry.pop("_is_ai", None)
            entry["status"] = "fallback_reused"
            reused_fallback += 1
        elif entry is None:
            entry = generate_fallback(item, destination)
            generated_fallback += 1

        entries[number] = entry
        save_manifest(entries, metadata)
        log("Checkpoint saved:", entry["depth_map"], entry["status"])

    if len(entries) != len(prepared):
        raise RuntimeError(
            f"Depth manifest mismatch: {len(entries)}/{len(prepared)} scenes."
        )

    for item in prepared:
        row = entries[item["scene"]]
        if row.get("image_sha256") != item["image_sha256"]:
            raise RuntimeError(
                f"Source image hash mismatch for scene {item['scene']}."
            )

        path = safe_path(row.get("depth_map"), "final depth map")
        if validate_depth(path) is None:
            raise RuntimeError(f"Invalid depth map for scene {item['scene']}.")

        if row.get("depth_sha256") != sha256_file(path):
            raise RuntimeError(
                f"Depth map checksum mismatch for scene {item['scene']}."
            )

    save_manifest(entries, metadata)

    final_rows = [entries[number] for number in sorted(entries)]
    ai_count = sum(row.get("model") == MODEL_ID for row in final_rows)

    log("=" * 55)
    log("DEPTH MAPS VALIDATED")
    log("Scenes:", len(final_rows))
    log("AI depth maps:", ai_count)
    log("Fallback pseudo-depth maps:", len(final_rows) - ai_count)
    log("New AI maps:", generated_ai)
    log("New fallback maps:", generated_fallback)
    log("Reused fallback maps:", reused_fallback)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Depth generation cancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"DEPTH GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
