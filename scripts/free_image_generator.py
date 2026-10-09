#!/usr/bin/env python3
"""KATHA LOK AI — Free Z-Image-Turbo scene image generator."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    from gradio_client import Client
    from PIL import Image
except ImportError as exc:
    print(f"ERROR: Missing dependency: {exc}")
    print("Install: pip install gradio_client pillow")
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_generation_manifest.json"

HF_SPACE = "mrfakename/Z-Image-Turbo"
STEPS = 9
BASE_SEED = 20261009

# Dimensions are multiples of 64, supported by the image workflow.
# Full = landscape; Short = portrait.
LANDSCAPE = (1024, 576)  # width, height
PORTRAIT = (576, 1024)   # width, height

QUALITY_SUFFIX = """
Photorealistic live-action photography, realistic Indian people and
Indian environments, natural skin texture, believable anatomy,
realistic clothing, physically accurate lighting and shadows,
cinematic documentary photography, natural depth of field.
No illustration, cartoon, painting, anime, 3D render, plastic skin,
text, logo, watermark, duplicate people, distorted hands or faces.
"""


def load_json(path, default=None):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def scene_number(job, fallback):
    for key in ("global_scene", "scene_number", "scene", "scene_id", "number", "id"):
        value = job.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, str):
                digits = "".join(c for c in value if c.isdigit())
                if digits:
                    return int(digits)
            return int(value)
        except (TypeError, ValueError):
            continue
    return fallback


def dimensions_for(job):
    """Use the format selected by generate_visuals.py."""
    value = str(job.get("image_size", "")).lower().replace(" ", "")
    if value in ("1280x720", "1024x576", "landscape", "full", "16:9"):
        return LANDSCAPE
    if value in ("720x1280", "576x1024", "portrait", "short", "9:16"):
        return PORTRAIT

    # If the job doesn't specify dimensions, read the input config.
    config = ROOT / "Input" / "topic.txt"
    if config.exists():
        for line in config.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key.strip().upper() == "FORMAT":
                if val.strip().lower() == "full":
                    return LANDSCAPE
                return PORTRAIT

    return PORTRAIT


def find_image(number):
    for suffix in (".png", ".jpg", ".jpeg"):
        path = VISUAL_DIR / f"scene_{number:02d}{suffix}"
        if path.exists() and path.stat().st_size > 5000:
            return path
    return None


def image_has_dimensions(path, expected):
    try:
        with Image.open(path) as im:
            return im.size == expected
    except Exception:
        return False


def get_prompt(job):
    for key in ("prompt", "image_prompt", "visual_prompt", "scene_prompt"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip() + "\n\n" + QUALITY_SUFFIX.strip()

    return (
        "Create a photorealistic cinematic scene from an Indian story. "
        + str(job.get("description", job.get("title", "")))
        + "\n\n" + QUALITY_SUFFIX.strip()
    )


def save_image_result(result, destination):
    if isinstance(result, (tuple, list)):
        result = result[0] if result else None

    if hasattr(result, "save"):
        result.save(destination)
        return True

    if isinstance(result, dict):
        for key in ("path", "filepath", "file"):
            value = result.get(key)
            if value and Path(str(value)).is_file():
                with Image.open(str(value)) as im:
                    im.save(destination)
                return True
        result = result.get("url")

    if isinstance(result, str):
        source = Path(result)
        if source.is_file():
            with Image.open(source) as im:
                im.save(destination)
            return True

        if result.startswith(("https://", "http://")):
            import requests
            response = requests.get(result, timeout=120)
            response.raise_for_status()
            destination.write_bytes(response.content)
            with Image.open(destination) as im:
                im.verify()
            return True

    return False


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    if not JOBS_FILE.exists():
        raise SystemExit(f"ERROR: Missing {JOBS_FILE}. Run generate_visuals.py first.")

    data = load_json(JOBS_FILE, {})
    jobs = data if isinstance(data, list) else (
        data.get("jobs") or data.get("visual_jobs") or data.get("scenes") or []
    )

    if not isinstance(jobs, list) or not jobs:
        raise SystemExit("ERROR: visual_jobs.json contains no scene jobs.")

    print("=" * 64)
    print("KATHA LOK AI — FREE IMAGE GENERATION")
    print("Space:", HF_SPACE)
    print("Scene jobs:", len(jobs))
    print("=" * 64)

    try:
        client = Client(HF_SPACE, verbose=False)
    except Exception as exc:
        raise SystemExit(f"ERROR: Could not connect to Hugging Face Space: {exc}")

    manifest = {
        "generator": "Z-Image-Turbo",
        "space": HF_SPACE,
        "scenes": [],
    }

    failed = []
    generated = 0
    skipped = 0

    for index, job in enumerate(jobs, start=1):
        number = scene_number(job, index)
        width, height = dimensions_for(job)
        destination = VISUAL_DIR / f"scene_{number:02d}.png"

        print(f"\n[{index}/{len(jobs)}] Scene {number:02d}")
        print(f"Format dimensions: {width}x{height}")

        existing = find_image(number)
        if existing and image_has_dimensions(existing, (width, height)):
            print("Existing image matches dimensions; skipping.")
            manifest["scenes"].append({
                "scene": number,
                "status": "existing",
                "file": str(existing.relative_to(ROOT)),
                "width": width,
                "height": height,
            })
            skipped += 1
            continue

        # Remove an old image with the wrong orientation so it cannot
        # accidentally be mistaken for the new result.
        if existing and existing != destination:
            existing.unlink(missing_ok=True)
        if destination.exists() and not image_has_dimensions(destination, (width, height)):
            destination.unlink()

        prompt = get_prompt(job)
        seed = BASE_SEED + number
        success = False
        last_error = None

        for attempt in range(1, 4):
            try:
                print(f"Generating image (attempt {attempt}/3)...")
                result = client.predict(
                    prompt,
                    height,
                    width,
                    STEPS,
                    seed,
                    False,
                    api_name="/generate_image",
                )

                if save_image_result(result, destination):
                    with Image.open(destination) as im:
                        actual_size = im.size

                    if actual_size == (width, height) and destination.stat().st_size > 5000:
                        success = True
                        generated += 1
                        manifest["scenes"].append({
                            "scene": number,
                            "status": "generated",
                            "file": str(destination.relative_to(ROOT)),
                            "seed": seed,
                            "width": width,
                            "height": height,
                            "model": HF_SPACE,
                        })
                        print("SUCCESS:", destination.name, actual_size)
                        break

                    last_error = f"Unexpected image dimensions: {actual_size}"
                    destination.unlink(missing_ok=True)
                else:
                    last_error = "The Space returned no usable image."

            except Exception as exc:
                last_error = str(exc)
                print("Attempt error:", last_error[:400])

            if attempt < 3:
                time.sleep(5)

        if not success:
            print("FAILED:", last_error)
            failed.append(number)
            manifest["scenes"].append({
                "scene": number,
                "status": "failed",
                "error": last_error,
                "width": width,
                "height": height,
            })

    manifest["summary"] = {
        "total": len(jobs),
        "generated": generated,
        "skipped": skipped,
        "failed": len(failed),
        "failed_scenes": failed,
    }
    save_json(MANIFEST_FILE, manifest)

    print("\nIMAGE GENERATION SUMMARY")
    print("Generated:", generated)
    print("Skipped:", skipped)
    print("Failed:", len(failed))
    print("Manifest:", MANIFEST_FILE)

    if failed:
        raise SystemExit(f"ERROR: Image generation failed for scenes: {failed}")

    print("ALL SCENE IMAGES READY.")


if __name__ == "__main__":
    main()