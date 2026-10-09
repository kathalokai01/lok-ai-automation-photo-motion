
#!/usr/bin/env python3
"""Generate depth maps for Photo Motion parallax processing."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate

VISUALS = ROOT / "output" / "visuals"
JOBS_FILE = VISUALS / "visual_jobs.json"
DEPTH_DIR = VISUALS / "depth_maps"
MANIFEST_FILE = DEPTH_DIR / "depth_manifest.json"

MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"


def log(*items):
    print(*items, flush=True)


def safe_path(value, label):
    if not value:
        raise RuntimeError(f"Missing {label}.")

    path = (ROOT / str(value)).resolve()

    if not path.is_relative_to(ROOT):
        raise RuntimeError(
            f"{label} must remain inside the repository: {value}"
        )

    return path


def read_jobs():
    if not JOBS_FILE.is_file():
        raise RuntimeError(
            "visual_jobs.json is missing. "
            "Run generate_visuals.py first."
        )

    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read visual jobs: {exc}") from exc

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
        raise RuntimeError("No scene jobs were found.")

    return jobs


def get_scene_number(job, index):
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


def main():
    # Validate the same input configuration used by the pipeline.
    load_and_validate(ROOT / "Input" / "topic.txt")

    try:
        from PIL import Image, ImageOps
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependencies. Install Pillow, Transformers, "
            "and PyTorch in the workflow before running this script."
        ) from exc

    jobs = read_jobs()
    prepared = []
    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not an object.")

        number = get_scene_number(job, index)

        if number in seen:
            raise RuntimeError(f"Duplicate scene number: {number}")

        seen.add(number)

        image_value = job.get("image_path") or job.get("image")
        image_path = safe_path(
            image_value,
            f"image path for scene {number}",
        )

        if not image_path.is_file() or image_path.stat().st_size < 1000:
            raise RuntimeError(
                f"Scene {number} image is missing or invalid: {image_path}"
            )

        try:
            with Image.open(image_path) as image:
                image.verify()
        except Exception as exc:
            raise RuntimeError(
                f"Cannot open source image for scene {number}: {exc}"
            ) from exc

        prepared.append({
            "number": number,
            "image": image_path,
        })

    prepared.sort(key=lambda item: item["number"])

    expected = list(range(1, len(prepared) + 1))
    actual = [item["number"] for item in prepared]

    if actual != expected:
        raise RuntimeError(
            "Scene numbers must be continuous from 1. "
            f"Found: {actual}"
        )

    DEPTH_DIR.mkdir(parents=True, exist_ok=True)

    # Use a freely downloadable pretrained model, running on CPU.
    # No paid API key or billable provider is required.
    log("Loading depth-estimation model:", MODEL_ID)
    depth_estimator = pipeline(
        task="depth-estimation",
        model=MODEL_ID,
        device=-1,
    )

    manifest = []

    for index, item in enumerate(prepared, start=1):
        number = item["number"]
        image_path = item["image"]
        output_path = DEPTH_DIR / f"depth_{number:04d}.png"

        log(f"Generating depth map {index}/{len(prepared)}: scene {number}")

        try:
            with Image.open(image_path) as source:
                rgb_image = source.convert("RGB")

            result = depth_estimator(rgb_image)

            depth_image = result.get("depth")

            if depth_image is None:
                raise RuntimeError(
                    "The depth model returned no depth image."
                )

            if not isinstance(depth_image, Image.Image):
                depth_image = Image.fromarray(depth_image)

            # Stretch the depth range for usable grayscale contrast.
            depth_image = ImageOps.autocontrast(
                depth_image.convert("L")
            )

            if depth_image.width < 64 or depth_image.height < 64:
                raise RuntimeError(
                    "Generated depth map dimensions are too small."
                )

            temporary = output_path.with_suffix(".tmp.png")
            depth_image.save(temporary, format="PNG")
            temporary.replace(output_path)

        except Exception as exc:
            raise RuntimeError(
                f"Depth-map generation failed for scene {number}: {exc}"
            ) from exc

        if not output_path.is_file() or output_path.stat().st_size < 100:
            raise RuntimeError(
                f"Depth map is missing or empty: {output_path}"
            )

        manifest.append({
            "scene": number,
            "global_scene": number,
            "image": str(image_path.relative_to(ROOT)),
            "depth_map": str(output_path.relative_to(ROOT)),
            "width": depth_image.width,
            "height": depth_image.height,
            "model": MODEL_ID,
            "status": "generated",
            "size_bytes": output_path.stat().st_size,
        })

        temporary_manifest = MANIFEST_FILE.with_suffix(".json.tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.replace(MANIFEST_FILE)

    if len(manifest) != len(prepared):
        raise RuntimeError(
            f"Depth-map count mismatch: "
            f"images={len(prepared)}, maps={len(manifest)}"
        )

    log("=" * 55)
    log("DEPTH MAP GENERATION SUCCESS")
    log("Scenes:", len(manifest))
    log("Model:", MODEL_ID)
    log("Output directory:", DEPTH_DIR.relative_to(ROOT))
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"DEPTH MAP GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
