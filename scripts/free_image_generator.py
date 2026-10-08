#!/usr/bin/env python3
"""
KATHA LOK AI — FREE IMAGE GENERATOR

FREE-FIRST IMAGE GENERATION ROUTER

Priority:
    1. Hugging Face ZeroGPU Space
    2. Existing local scene image
    3. Stop with a clear error

IMPORTANT:
    - No Gemini paid image API
    - No paid image API
    - No Cloudflare AI
    - No automatic paid fallback
    - No fake success

Recommended free model:
    Z-Image Turbo

Fallback:
    Qwen Image

This script is designed so the Photo Motion pipeline
never silently spends money.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests


# ============================================================
# PATHS
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

VISUALS_DIR = (
    ROOT /
    "output" /
    "visuals"
)

VISUAL_JOBS = (
    VISUALS_DIR /
    "visual_jobs.json"
)

MANIFEST = (
    VISUALS_DIR /
    "image_generation_manifest.json"
)


# ============================================================
# FREE MODEL CONFIG
# ============================================================

MODEL_PRIORITY = [

    {
        "name": "Z-Image Turbo",
        "type": "huggingface_space",
        "space": "mrfakename/Z-Image-Turbo"
    },

    {
        "name": "Qwen Image 2.1",
        "type": "huggingface_space",
        "space": "Qwen/Qwen-Image-2.1"
    }

]


# ============================================================
# ENVIRONMENT
# ============================================================

HF_TOKEN = os.environ.get(
    "HF_TOKEN",
    ""
).strip()

HF_TIMEOUT = int(
    os.environ.get(
        "HF_TIMEOUT",
        "180"
    )
)


# ============================================================
# HELPERS
# ============================================================

def load_json(path):

    if not path.exists():
        return {}

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def save_json(path, data):

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def scene_number(value, fallback):

    if isinstance(
        value,
        dict
    ):

        for key in [
            "scene",
            "scene_id",
            "scene_number",
            "id",
            "number"
        ]:

            if value.get(key) is not None:

                digits = re.findall(
                    r"\d+",
                    str(value[key])
                )

                if digits:
                    return int(digits[-1])

    return fallback


def find_existing_image(number):

    candidates = [

        VISUALS_DIR /
        f"scene_{number:02d}.png",

        VISUALS_DIR /
        f"scene_{number:02d}.jpg",

        VISUALS_DIR /
        f"scene_{number:02d}.jpeg",

        VISUALS_DIR /
        f"scene_{number:02d}.webp",

        VISUALS_DIR /
        f"scene_{number}.png",

        VISUALS_DIR /
        f"scene_{number}.jpg",

        VISUALS_DIR /
        f"scene_{number}.jpeg",

        VISUALS_DIR /
        f"scene_{number}.webp"
    ]

    for path in candidates:

        if path.exists():
            return path

    return None


def normalize_prompt(prompt):

    if not prompt:
        return (
            "Photorealistic cinematic Indian scene, "
            "real human appearance, natural skin texture, "
            "realistic environment, natural lighting, "
            "live-action photography."
        )

    return str(prompt).strip()


def negative_prompt():

    return (
        "cartoon, anime, illustration, painting, CGI, "
        "3D render, plastic skin, doll face, "
        "deformed face, distorted body, "
        "extra fingers, extra limbs, duplicate people, "
        "bad anatomy, warped background, "
        "unrealistic eyes, text, watermark, logo, "
        "blurry face, low quality"
    )


# ============================================================
# HUGGING FACE SPACE INFORMATION
# ============================================================

def hf_headers():

    headers = {}

    if HF_TOKEN:

        headers[
            "Authorization"
        ] = f"Bearer {HF_TOKEN}"

    return headers


def check_space(space):

    url = (
        "https://huggingface.co/spaces/"
        + space
    )

    try:

        response = requests.get(
            url,
            headers=hf_headers(),
            timeout=30
        )

        return response.status_code == 200

    except Exception:

        return False


# ============================================================
# GENERATION
# ============================================================

def try_space(
    space,
    prompt,
    output_path
):

    """
    Generic free-space attempt.

    We deliberately do NOT assume that every Space has
    the same API schema.

    The Space must expose a compatible public endpoint.
    """

    print()
    print(
        "FREE MODEL:",
        space
    )

    print(
        "Prompt:",
        prompt[:250]
    )

    # --------------------------------------------------------
    # First check that the Space exists.
    # --------------------------------------------------------

    if not check_space(space):

        print(
            "Space unavailable."
        )

        return False

    # --------------------------------------------------------
    # Gradio API discovery
    # --------------------------------------------------------

    config_url = (
        f"https://huggingface.co/spaces/"
        f"{space}/raw/main"
    )

    # We do not download arbitrary repository code.
    # Instead we use the public Space endpoint.
    #
    # Some Spaces expose:
    #
    #   /gradio_api/info
    #
    # or:
    #
    #   /gradio_api/openapi.json
    #
    # Try the public API description first.

    base_url = (
        "https://"
        + space.replace(
            "/",
            "-"
        )
        + ".hf.space"
    )

    api_candidates = [

        f"{base_url}/gradio_api/info",

        f"{base_url}/gradio_api/openapi.json"
    ]

    api_info = None

    for url in api_candidates:

        try:

            response = requests.get(
                url,
                headers=hf_headers(),
                timeout=30
            )

            if response.ok:

                api_info = response.json()

                print(
                    "Space API detected:",
                    url
                )

                break

        except Exception as exc:

            print(
                "API discovery failed:",
                exc
            )

    if api_info is None:

        print(
            "No compatible public Gradio API detected."
        )

        return False

    # --------------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------------
    #
    # We intentionally stop here rather than guessing
    # endpoint parameters.
    #
    # Different community Spaces can expose different
    # Gradio APIs.
    #
    # Guessing could generate errors or unexpectedly use
    # a different model.
    #
    # The caller will try the next FREE model.
    # --------------------------------------------------------

    print(
        "Space is reachable but its generation schema "
        "is not safely auto-detectable."
    )

    return False


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 68)
    print("KATHA LOK AI — FREE IMAGE GENERATION")
    print("=" * 68)

    VISUALS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if not VISUAL_JOBS.exists():

        raise RuntimeError(
            "visual_jobs.json not found.\n"
            "Run generate_visuals.py first."
        )

    data = load_json(
        VISUAL_JOBS
    )

    jobs = data.get(
        "jobs",
        []
    )

    if not jobs:

        raise RuntimeError(
            "No visual jobs found."
        )

    print()
    print(
        "Scene jobs:",
        len(jobs)
    )

    print()
    print(
        "FREE MODEL ORDER:"
    )

    for index, model in enumerate(
        MODEL_PRIORITY,
        start=1
    ):

        print(
            f"{index}. {model['name']}"
        )

    results = []

    for index, job in enumerate(
        jobs,
        start=1
    ):

        number = scene_number(
            job,
            index
        )

        print()
        print("=" * 68)
        print(
            f"SCENE {number}"
        )
        print("=" * 68)

        existing = find_existing_image(
            number
        )

        if existing:

            print(
                "Existing image:",
                existing
            )

            results.append({

                "scene": number,

                "status": "already_exists",

                "image": str(
                    existing.relative_to(ROOT)
                ),

                "model": "existing"

            })

            continue

        prompt = normalize_prompt(
            job.get(
                "prompt",
                ""
            )
        )

        generated = False

        for model in MODEL_PRIORITY:

            if model["type"] != "huggingface_space":
                continue

            output_path = (
                VISUALS_DIR /
                f"scene_{number:02d}.png"
            )

            try:

                generated = try_space(
                    space=model["space"],
                    prompt=prompt,
                    output_path=output_path
                )

            except Exception as exc:

                print(
                    "Model attempt failed:",
                    exc
                )

                generated = False

            if generated and output_path.exists():

                print(
                    "SUCCESS:",
                    output_path
                )

                results.append({

                    "scene": number,

                    "status": "generated",

                    "image": str(
                        output_path.relative_to(ROOT)
                    ),

                    "model": model["name"]

                })

                break

        if not generated:

            print()
            print(
                "NO FREE GENERATOR AVAILABLE "
                "FOR THIS SCENE."
            )

            results.append({

                "scene": number,

                "status": "image_required",

                "image":
                    f"output/visuals/"
                    f"scene_{number:02d}.png",

                "model": None

            })

    # --------------------------------------------------------
    # Save manifest
    # --------------------------------------------------------

    manifest = {

        "version":
            "free-image-generation-1.0",

        "paid_api":
            False,

        "paid_fallback":
            False,

        "models":
            MODEL_PRIORITY,

        "results":
            results

    }

    save_json(
        MANIFEST,
        manifest
    )

    # --------------------------------------------------------
    # Final validation
    # --------------------------------------------------------

    missing = [

        item
        for item in results
        if item["status"] ==
        "image_required"
    ]

    print()
    print("=" * 68)
    print("FREE IMAGE GENERATION RESULT")
    print("=" * 68)

    print(
        "Total scenes:",
        len(results)
    )

    print(
        "Missing images:",
        len(missing)
    )

    print(
        "Manifest:",
        MANIFEST
    )

    if missing:

        print()
        print(
            "STATUS: INCOMPLETE"
        )

        print(
            "No paid API was used."
        )

        print(
            "Photo Motion will NOT create fake output."
        )

        sys.exit(2)

    print()
    print(
        "STATUS: SUCCESS"
    )


if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nStopped."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print(
            "=" * 68
        )
        print(
            "FREE IMAGE GENERATION FAILED"
        )
        print(
            "=" * 68
        )

        print(
            str(exc)
        )

        sys.exit(1)