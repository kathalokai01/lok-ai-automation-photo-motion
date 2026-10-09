
#!/usr/bin/env python3
"""Generate validated depth maps for Photo Motion 2.5D parallax."""

from __future__ import annotations

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
MIN_FILE_SIZE = 1000


def log(*args):
    print(*args, flush=True)


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


def read_jobs():
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            "Missing output/visuals/visual_jobs.json. "
            "Run scripts/generate_visuals.py first."
        )

    try:
        document = json.loads(
            JOBS_FILE.read_text(encoding="utf-8")
        )
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
        raise RuntimeError(
            f"Scene number must be positive: {number}"
        )

    return number


def load_and_validate_jobs():
    jobs = read_jobs()
    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(
                f"Scene job {index} must be a JSON object."
            )

        number = scene_number(job, index)

        if number in seen:
            raise RuntimeError(
                f"Duplicate global scene number: {number}"
            )

        seen.add(number)

        image_value = job.get("image_path") or job.get("image")
        image_path = safe_repo_path(
            image_value,
            f"image path for scene {number}",
        )

        if not image_path.is_file():
            raise RuntimeError(
                f"Scene {number} image is missing: {image_path}"
            )

        if image_path.stat().st_size < MIN_FILE_SIZE:
            raise RuntimeError(
                f"Scene {number} image is too small: {image_path}"
            )

        prepared.append({
            "scene": number,
            "image_path": image_path,
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

    log("=" * 60)
    log("KATHA LOK AI DEPTH MAP GENERATION")
    log("Model:", MODEL_ID)
    log("Scenes:", len(prepared))
    log("Device: CPU")
    log("Output:", DEPTH_DIR.relative_to(ROOT))
    log("=" * 60)

    # This is a free pretrained model. Model download and CPU
    # inference may take time on a GitHub-hosted runner.
    estimator = pipeline(
        task="depth-estimation",
        model=MODEL_ID,
        device=-1,
    )

    manifest = []

    for index, item in enumerate(prepared, start=1):
        number = item["scene"]
        image_path = item["image_path"]
        output_path = DEPTH_DIR / f"depth_{number:04d}.png"
        temporary_path = DEPTH_DIR / f"depth_{number:04d}.tmp.png"

        log(f"[{index}/{len(prepared)}] Scene {number}: estimating depth")

        try:
            with Image.open(image_path) as source:
                source_rgb = source.convert("RGB")

            result = estimator(source_rgb)
            depth_image = result.get("depth")

            if depth_image is None:
                raise RuntimeError(
                    "Depth model returned no depth image."
                )

            if not isinstance(depth_image, Image.Image):
                depth_image = Image.fromarray(depth_image)

            depth_image = ImageOps.autocontrast(
                depth_image.convert("L")
            )

            if depth_image.width < 64 or depth_image.height < 64:
                raise RuntimeError(
                    "Depth map resolution is below 64x64."
                )

            depth_image.save(temporary_path, format="PNG")
            temporary_path.replace(output_path)

        except Exception as exc:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Depth generation failed for scene {number}: {exc}"
            ) from exc

        if not output_path.is_file() or output_path.stat().st_size < 100:
            raise RuntimeError(
                f"Depth output is missing or invalid: {output_path}"
            )

        manifest.append({
            "scene": number,
            "global_scene": number,
            "image": image_path.relative_to(ROOT).as_posix(),
            "depth_map": output_path.relative_to(ROOT).as_posix(),
            "width": depth_image.width,
            "height": depth_image.height,
            "model": MODEL_ID,
            "status": "generated",
            "size_bytes": output_path.stat().st_size,
        })

        # Save progress after every scene, so completed maps remain
        # available if a later scene fails.
        temporary_manifest = MANIFEST_FILE.with_suffix(".json.tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.replace(MANIFEST_FILE)

        log(
            f"Saved scene {number}: "
            f"{output_path.relative_to(ROOT)} "
            f"({output_path.stat().st_size} bytes)"
        )

    if len(manifest) != len(prepared):
        raise RuntimeError(
            f"Depth map count mismatch: expected {len(prepared)}, "
            f"created {len(manifest)}"
        )

    log("=" * 60)
    log("DEPTH MAP GENERATION COMPLETE")
    log("Validated scenes:", len(prepared))
    log("Generated depth maps:", len(manifest))
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
