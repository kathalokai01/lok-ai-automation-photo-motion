
#!/usr/bin/env python3
"""Generate resumable, validated depth maps for Photo Motion 2.5D."""

from __future__ import annotations

import hashlib
import json
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


def safe_repo_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")

    candidate = Path(value.strip())

    if not candidate.is_absolute():
        candidate = ROOT / candidate

    resolved = candidate.resolve()

    if not resolved.is_relative_to(ROOT):
        raise RuntimeError(
            f"{label} must be inside the repository: {value}"
        )

    return resolved


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

    try:
        document = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot parse visual_jobs.json: {exc}"
        ) from exc

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
        raise RuntimeError(f"Scene number must be positive: {number}")

    return number


def load_and_validate_jobs():
    from PIL import Image

    prepared = []
    seen = set()

    for index, job in enumerate(read_jobs(), start=1):
        if not isinstance(job, dict):
            raise RuntimeError(
                f"Scene job {index} must be a JSON object."
            )

        number = scene_number(job, index)

        if number in seen:
            raise RuntimeError(f"Duplicate global scene number: {number}")

        seen.add(number)

        image_path = safe_repo_path(
            job.get("image_path") or job.get("image"),
            f"image path for scene {number}",
        )

        if not image_path.is_file():
            raise RuntimeError(
                f"Scene {number} image is missing: {image_path}"
            )

        if image_path.stat().st_size < MIN_IMAGE_SIZE:
            raise RuntimeError(
                f"Scene {number} image is too small: {image_path}"
            )

        try:
            with Image.open(image_path) as source:
                source.verify()

            with Image.open(image_path) as source:
                if source.width < 64 or source.height < 64:
                    raise RuntimeError(
                        "Image dimensions are below 64x64."
                    )
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
            "Scene numbering must be continuous from 1. "
            f"Found: {actual}"
        )

    return prepared


def read_previous_manifest():
    if not MANIFEST_FILE.is_file():
        return {}

    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log("WARNING: Existing depth manifest cannot be read:", exc)
        return {}

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("scenes", data.get("depth_maps", []))
    else:
        entries = []

    if not isinstance(entries, list):
        return {}

    previous = {}

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


def reusable_depth(item, previous, Image):
    """Reuse a depth map only when its source image still matches."""
    number = item["scene"]
    old = previous.get(number)

    if not isinstance(old, dict):
        return None

    if old.get("model") != MODEL_ID:
        return None

    if old.get("image_sha256") != item["image_sha256"]:
        return None

    try:
        output_path = safe_repo_path(
            old.get("depth_map"),
            f"previous depth map for scene {number}",
        )

        if not output_path.is_file():
            return None

        if output_path.stat().st_size < MIN_DEPTH_SIZE:
            return None

        with Image.open(output_path) as depth:
            depth.verify()

        with Image.open(output_path) as depth:
            if depth.width < 64 or depth.height < 64:
                return None

            width, height = depth.size

        entry = dict(old)
        entry.update({
            "scene": number,
            "global_scene": number,
            "image": item["image_path"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "depth_map": output_path.relative_to(ROOT).as_posix(),
            "width": width,
            "height": height,
            "model": MODEL_ID,
            "status": "reused",
            "size_bytes": output_path.stat().st_size,
        })

        return entry

    except (OSError, ValueError, RuntimeError):
        return None


def generate_depth(estimator, item, output_path, Image, ImageOps):
    number = item["scene"]
    temporary_path = output_path.with_name(
        output_path.stem + ".tmp.png"
    )

    try:
        with Image.open(item["image_path"]) as source:
            source_rgb = source.convert("RGB")

        result = estimator(source_rgb)
        depth_image = result.get("depth")

        if depth_image is None:
            raise RuntimeError("Depth model returned no depth image.")

        if not isinstance(depth_image, Image.Image):
            depth_image = Image.fromarray(depth_image)

        depth_image = ImageOps.autocontrast(
            depth_image.convert("L")
        )

        if depth_image.width < 64 or depth_image.height < 64:
            raise RuntimeError(
                "Generated depth map resolution is below 64x64."
            )

        depth_image.save(temporary_path, format="PNG")
        temporary_path.replace(output_path)

        if (
            not output_path.is_file()
            or output_path.stat().st_size < MIN_DEPTH_SIZE
        ):
            raise RuntimeError("Generated depth map is missing or too small.")

        with Image.open(output_path) as check:
            check.verify()

        return {
            "scene": number,
            "global_scene": number,
            "image": item["image_path"].relative_to(ROOT).as_posix(),
            "image_sha256": item["image_sha256"],
            "depth_map": output_path.relative_to(ROOT).as_posix(),
            "width": depth_image.width,
            "height": depth_image.height,
            "model": MODEL_ID,
            "status": "generated",
            "size_bytes": output_path.stat().st_size,
        }

    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Depth generation failed for scene {number}: {exc}"
        ) from exc


def main():
    input_file = ROOT / "Input" / "topic.txt"

    if not input_file.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_input_config(input_file)
    normalize_format(config)

    try:
        from PIL import Image, ImageOps
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Depth dependencies are missing. Install CPU PyTorch, "
            "Transformers and Pillow in the workflow."
        ) from exc

    prepared = load_and_validate_jobs()
    DEPTH_DIR.mkdir(parents=True, exist_ok=True)

    previous = read_previous_manifest()
    manifest = []
    estimator = None
    reused_count = 0
    generated_count = 0

    log("=" * 60)
    log("KATHA LOK AI DEPTH MAP GENERATION")
    log("Model:", MODEL_ID)
    log("Scenes:", len(prepared))
    log("Device: CPU")
    log("Resume enabled: yes")
    log("Output:", DEPTH_DIR.relative_to(ROOT))
    log("=" * 60)

    for index, item in enumerate(prepared, start=1):
        number = item["scene"]
        output_path = DEPTH_DIR / f"depth_{number:04d}.png"

        log(f"[{index}/{len(prepared)}] Scene {number}")

        # Reuse only a validated map made from the exact same source image.
        entry = reusable_depth(item, previous, Image)

        if entry is not None:
            log("Reusing validated depth map:", entry["depth_map"])
            reused_count += 1
        else:
            if estimator is None:
                log("Loading free pretrained depth model:", MODEL_ID)
                log("First run may take time to download model weights.")

                estimator = pipeline(
                    task="depth-estimation",
                    model=MODEL_ID,
                    device=-1,
                )

            log("Generating new depth map...")
            entry = generate_depth(
                estimator,
                item,
                output_path,
                Image,
                ImageOps,
            )
            generated_count += 1

        manifest.append(entry)

        # Save every completed scene so a later failure can resume safely.
        atomic_json(MANIFEST_FILE, manifest)

        log(
            "Saved:",
            entry["depth_map"],
            "| status:",
            entry["status"],
            "| bytes:",
            entry["size_bytes"],
        )

    if len(manifest) != len(prepared):
        raise RuntimeError(
            f"Depth map count mismatch: expected {len(prepared)}, "
            f"completed {len(manifest)}"
        )

    log("=" * 60)
    log("DEPTH MAP GENERATION COMPLETE")
    log("Validated scenes:", len(prepared))
    log("New maps generated:", generated_count)
    log("Existing maps reused:", reused_count)
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
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
