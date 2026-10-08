#!/usr/bin/env python3
"""
KATHA LOK AI — FREE IMAGE GENERATOR
===================================

PHOTO-MOTION REPO

Free image generation through a public Hugging Face
ZeroGPU Z-Image-Turbo Space.

NO:
- Gemini image API
- OpenAI image API
- paid API
- Cloudflare AI
- Wan2GP
- Colab

The generated images are saved into:
    output/visuals/

This script reads:
    output/visuals/visual_jobs.json

and generates missing scene images.

Verified HF Space:
    mrfakename/Z-Image-Turbo

Verified generation function:
    generate_image(
        prompt,
        height,
        width,
        num_inference_steps,
        seed,
        randomize_seed
    )

Returns:
    image, seed
"""

from __future__ import annotations

import json
import os
import sys
import time
import random
from pathlib import Path

try:
    from gradio_client import Client
except ImportError:
    print("ERROR: gradio_client is not installed.")
    print("Install with: pip install gradio_client")
    sys.exit(1)


# ============================================================
# PATHS
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_generation_manifest.json"

VISUAL_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# VERIFIED FREE SPACE
# ============================================================

HF_SPACE = "mrfakename/Z-Image-Turbo"


# ============================================================
# GENERATION SETTINGS
# ============================================================

# Photo-motion needs portrait images.
# Z-Image-Turbo supports dimensions >= 512 and multiples of 64.
HEIGHT = 1536
WIDTH = 1024

# Turbo model is designed for very few steps.
STEPS = 9

# Fixed starting seed; each scene gets a different deterministic seed.
BASE_SEED = 20261009


# ============================================================
# NEGATIVE / QUALITY GUIDANCE
# ============================================================

QUALITY_SUFFIX = """
Photorealistic live-action photography.
Real Indian people and real Indian environment.
Natural skin texture.
Natural facial proportions.
Natural human anatomy.
Realistic clothing and materials.
Realistic lighting.
Realistic shadows.
Natural depth of field.
Cinematic documentary photography.
Highly detailed but physically believable.
No illustration.
No cartoon.
No painting.
No 3D render.
No anime.
No fantasy CGI.
No artificial plastic skin.
No text.
No watermark.
"""


# ============================================================
# HELPERS
# ============================================================

def load_json(path: Path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"WARNING: Could not read {path}: {e}")
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def find_image(scene_number: int):
    candidates = [
        VISUAL_DIR / f"scene_{scene_number:02d}.png",
        VISUAL_DIR / f"scene_{scene_number:02d}.jpg",
        VISUAL_DIR / f"scene_{scene_number:02d}.jpeg",
        VISUAL_DIR / f"scene_{scene_number}.png",
        VISUAL_DIR / f"scene_{scene_number}.jpg",
        VISUAL_DIR / f"scene_{scene_number}.jpeg",
    ]

    for path in candidates:
        if path.exists() and path.stat().st_size > 5000:
            return path

    return None


def extract_prompt(job):
    """
    Accept multiple visual_jobs schemas so the generator
    remains compatible with the copied repository.
    """

    for key in (
        "prompt",
        "image_prompt",
        "visual_prompt",
        "scene_prompt",
        "description",
    ):
        value = job.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    # Nested prompt support
    prompt_data = job.get("prompts")

    if isinstance(prompt_data, dict):
        for key in (
            "image",
            "visual",
            "prompt",
            "scene",
        ):
            value = prompt_data.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


def extract_scene_number(job, fallback):
    for key in (
        "scene_number",
        "scene",
        "scene_id",
        "number",
        "id",
    ):
        value = job.get(key)

        if value is None:
            continue

        try:
            if isinstance(value, str):
                digits = "".join(ch for ch in value if ch.isdigit())

                if digits:
                    return int(digits)

            return int(value)

        except Exception:
            pass

    return fallback


def build_prompt(job):
    prompt = extract_prompt(job)

    if not prompt:
        title = job.get("title", "")
        description = job.get("description", "")

        prompt = f"""
A realistic cinematic scene from an Indian story.
Story title: {title}
Scene description: {description}
"""

    return (
        prompt.strip()
        + "\n\n"
        + QUALITY_SUFFIX.strip()
    )


def save_result_image(result, output_path: Path):
    """
    Gradio can return:
      - local filepath
      - PIL image
      - dict containing a filepath
      - dict containing a URL
    """

    # --------------------------------------------------------
    # PIL image
    # --------------------------------------------------------
    if hasattr(result, "save"):
        result.save(output_path)
        return True

    # --------------------------------------------------------
    # String filepath
    # --------------------------------------------------------
    if isinstance(result, str):

        source = Path(result)

        if source.exists():
            output_path.write_bytes(source.read_bytes())
            return True

        return False

    # --------------------------------------------------------
    # Dictionary result
    # --------------------------------------------------------
    if isinstance(result, dict):

        for key in (
            "path",
            "filepath",
            "file",
        ):
            value = result.get(key)

            if value:
                source = Path(str(value))

                if source.exists():
                    output_path.write_bytes(
                        source.read_bytes()
                    )
                    return True

        # Some Gradio versions may expose a URL.
        url = result.get("url")

        if url:
            try:
                import requests

                response = requests.get(
                    url,
                    timeout=120
                )

                response.raise_for_status()

                output_path.write_bytes(
                    response.content
                )

                return True

            except Exception as e:
                print(
                    "WARNING: Could not download image URL:",
                    e
                )

    return False


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("KATHA LOK AI — FREE Z-IMAGE-TURBO GENERATOR")
    print("=" * 70)

    print()
    print("HF Space :", HF_SPACE)
    print("Size     :", f"{WIDTH}x{HEIGHT}")
    print("Steps    :", STEPS)
    print("Output   :", VISUAL_DIR)
    print()

    # --------------------------------------------------------
    # JOB FILE
    # --------------------------------------------------------

    if not JOBS_FILE.exists():
        print("ERROR:")
        print(f"Missing: {JOBS_FILE}")
        print()
        print("Run generate_visuals.py first.")
        sys.exit(2)

    jobs_data = load_json(JOBS_FILE, [])

    # Support:
    # [] 
    # {"jobs":[...]}
    # {"scenes":[...]}

    if isinstance(jobs_data, dict):

        jobs = (
            jobs_data.get("jobs")
            or jobs_data.get("visual_jobs")
            or jobs_data.get("scenes")
            or []
        )

    else:
        jobs = jobs_data

    if not isinstance(jobs, list):
        print("ERROR: visual_jobs.json does not contain a list.")
        sys.exit(2)

    if not jobs:
        print("ERROR: No visual jobs found.")
        sys.exit(2)

    print(f"Visual jobs: {len(jobs)}")
    print()

    # --------------------------------------------------------
    # EXISTING IMAGES
    # --------------------------------------------------------

    existing = []

    for i, job in enumerate(jobs, start=1):

        scene_number = extract_scene_number(
            job,
            i
        )

        image = find_image(scene_number)

        if image:
            existing.append(scene_number)

    if existing:
        print(
            "Existing scene images:",
            len(existing)
        )

    # --------------------------------------------------------
    # CONNECT TO HF
    # --------------------------------------------------------

    print()
    print("Connecting to free Hugging Face Space...")

    try:
        client = Client(
            HF_SPACE,
            verbose=False
        )

    except Exception as e:

        print()
        print("ERROR: Could not connect to Hugging Face Space.")
        print(str(e))
        print()
        print(
            "This is usually a temporary Space/queue/network "
            "availability problem."
        )

        sys.exit(3)

    print("HF connection: OK")
    print()

    # --------------------------------------------------------
    # MANIFEST
    # --------------------------------------------------------

    manifest = {
        "generator": "Tongyi-MAI/Z-Image-Turbo",
        "space": HF_SPACE,
        "mode": "free_zerogpu",
        "width": WIDTH,
        "height": HEIGHT,
        "steps": STEPS,
        "scenes": [],
    }

    # --------------------------------------------------------
    # GENERATE
    # --------------------------------------------------------

    generated_count = 0
    skipped_count = 0
    failed_count = 0

    for index, job in enumerate(jobs, start=1):

        scene_number = extract_scene_number(
            job,
            index
        )

        output_path = (
            VISUAL_DIR
            / f"scene_{scene_number:02d}.png"
        )

        print("-" * 70)
        print(
            f"SCENE {scene_number:02d} "
            f"({index}/{len(jobs)})"
        )

        # ----------------------------------------------------
        # Existing image
        # ----------------------------------------------------

        existing_image = find_image(scene_number)

        if existing_image:

            print(
                "STATUS : EXISTING IMAGE — SKIP"
            )

            manifest["scenes"].append({
                "scene": scene_number,
                "status": "existing",
                "file": str(existing_image.relative_to(ROOT)),
            })

            skipped_count += 1
            continue

        # ----------------------------------------------------
        # Prompt
        # ----------------------------------------------------

        prompt = build_prompt(job)

        print()
        print("Generating image...")
        print("Model  :", HF_SPACE)
        print("Size   :", f"{WIDTH}x{HEIGHT}")
        print("Steps  :", STEPS)

        # Different deterministic seed per scene.
        seed = BASE_SEED + scene_number

        success = False
        last_error = None

        # ----------------------------------------------------
        # Retry
        # ----------------------------------------------------

        for attempt in range(1, 4):

            print(
                f"Attempt {attempt}/3..."
            )

            try:

                result = client.predict(
                    prompt,
                    HEIGHT,
                    WIDTH,
                    STEPS,
                    seed,
                    False,
                    api_name="/generate_image",
                )

                if isinstance(result, tuple):
                    image_result = result[0]
                    used_seed = (
                        result[1]
                        if len(result) > 1
                        else seed
                    )
                else:
                    image_result = result
                    used_seed = seed

                if save_result_image(
                    image_result,
                    output_path
                ):

                    if (
                        output_path.exists()
                        and output_path.stat().st_size > 5000
                    ):
                        success = True

                        print()
                        print(
                            "STATUS : SUCCESS"
                        )
                        print(
                            "IMAGE  :",
                            output_path
                        )
                        print(
                            "SIZE   :",
                            output_path.stat().st_size,
                            "bytes"
                        )

                        manifest["scenes"].append({
                            "scene": scene_number,
                            "status": "generated",
                            "file": str(
                                output_path.relative_to(ROOT)
                            ),
                            "seed": used_seed,
                            "model": HF_SPACE,
                        })

                        generated_count += 1
                        break

            except Exception as e:

                last_error = str(e)

                print(
                    "Attempt failed:",
                    last_error[:500]
                )

                if attempt < 3:
                    time.sleep(5)

        if not success:

            print()
            print(
                "STATUS : FAILED"
            )

            if last_error:
                print(
                    "ERROR  :",
                    last_error[:800]
                )

            manifest["scenes"].append({
                "scene": scene_number,
                "status": "failed",
                "error": last_error,
            })

            failed_count += 1

    # --------------------------------------------------------
    # SAVE MANIFEST
    # --------------------------------------------------------

    manifest["summary"] = {
        "total_jobs": len(jobs),
        "existing": skipped_count,
        "generated": generated_count,
        "failed": failed_count,
    }

    save_json(
        MANIFEST_FILE,
        manifest
    )

    # --------------------------------------------------------
    # FINAL STATUS
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("IMAGE GENERATION COMPLETE")
    print("=" * 70)

    print(
        "Total scenes :",
        len(jobs)
    )

    print(
        "Existing     :",
        skipped_count
    )

    print(
        "Generated    :",
        generated_count
    )

    print(
        "Failed       :",
        failed_count
    )

    print(
        "Manifest     :",
        MANIFEST_FILE
    )

    print()

    if failed_count > 0:

        print(
            "ERROR: Some scene images could not be generated."
        )

        print(
            "Photo Motion will NOT continue with incomplete visuals."
        )

        sys.exit(4)

    # Verify every scene exists.
    missing = []

    for index, job in enumerate(jobs, start=1):

        scene_number = extract_scene_number(
            job,
            index
        )

        image = find_image(scene_number)

        if not image:
            missing.append(scene_number)

    if missing:

        print(
            "ERROR: Missing final images:",
            missing
        )

        sys.exit(5)

    print(
        "ALL SCENE IMAGES READY."
    )

    sys.exit(0)


if __name__ == "__main__":
    main()