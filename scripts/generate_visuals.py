#!/usr/bin/env python3
"""
KATHA LOK AI — PHOTO MOTION VISUAL PREPARATION

Purpose:
    Prepare realistic scene-image jobs for the PHOTO MOTION pipeline.

IMPORTANT:
    This script does NOT call a paid image-generation API.
    It does NOT use Wan2GP.
    It does NOT require Colab.

It prepares:
    output/visuals/visual_jobs.json
    output/visuals/image_manifest.json

If actual scene images already exist in output/visuals/,
they are detected automatically.

Expected image names:
    scene_01.png
    scene_02.png
    scene_03.png
    ...

The next stage:
    scripts/photo_motion.py

converts those images into MP4 camera-motion clips.
"""

import json
import os
import re
import sys
from pathlib import Path


# ============================================================
# PATHS
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

INPUT_DIR = ROOT / "Input"

OUTPUT_DIR = ROOT / "output"

SCENES_DIR = OUTPUT_DIR / "scenes"
VISUALS_DIR = OUTPUT_DIR / "visuals"
STORY_DIR = OUTPUT_DIR / "story"

SCENES_JSON = SCENES_DIR / "scenes.json"
CHARACTER_BIBLE_JSON = STORY_DIR / "character_bible.json"

VISUAL_JOBS_JSON = VISUALS_DIR / "visual_jobs.json"
IMAGE_MANIFEST_JSON = VISUALS_DIR / "image_manifest.json"

VISUALS_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# CONFIG
# ============================================================

DEFAULT_SHORT_SIZE = "720x1280"
DEFAULT_FULL_SIZE = "1280x720"

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp"
}


# ============================================================
# HELPERS
# ============================================================

def load_json(path, default=None):

    if default is None:
        default = {}

    if not path.exists():
        return default

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as exc:

        print(
            f"WARNING: Could not read {path}: {exc}"
        )

        return default


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


def first_value(data, keys, default=None):

    if not isinstance(data, dict):
        return default

    for key in keys:

        value = data.get(key)

        if value is not None:
            return value

    return default


def scene_number(scene, fallback):

    if not isinstance(scene, dict):
        return fallback

    value = first_value(
        scene,
        [
            "scene",
            "scene_id",
            "scene_number",
            "id",
            "number"
        ]
    )

    if value is None:
        return fallback

    digits = re.findall(
        r"\d+",
        str(value)
    )

    if digits:
        return int(digits[0])

    return fallback


def scene_text(scene):

    if not isinstance(scene, dict):
        return ""

    keys = [
        "visual_prompt",
        "image_prompt",
        "visual_description",
        "description",
        "scene_description",
        "prompt",
        "action",
        "text"
    ]

    parts = []

    for key in keys:

        value = scene.get(key)

        if isinstance(value, str) and value.strip():

            parts.append(
                value.strip()
            )

    # Remove duplicates while preserving order
    result = []

    seen = set()

    for item in parts:

        normalized = item.lower()

        if normalized not in seen:

            seen.add(normalized)
            result.append(item)

    return " ".join(result)


def get_duration(scene):

    if not isinstance(scene, dict):
        return 5.0

    keys = [
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds"
    ]

    for key in keys:

        value = scene.get(key)

        try:

            value = float(value)

            if value > 0:
                return value

        except Exception:
            pass

    return 5.0


def detect_format():

    format_value = os.environ.get(
        "FORMAT",
        "short"
    ).strip().lower()

    if format_value == "full":
        return "full"

    return "short"


def image_size_for_format(format_value):

    if format_value == "full":
        return DEFAULT_FULL_SIZE

    return DEFAULT_SHORT_SIZE


# ============================================================
# CHARACTER BIBLE
# ============================================================

def load_character_bible():

    data = load_json(
        CHARACTER_BIBLE_JSON,
        {}
    )

    if not data:
        return ""

    # Try common character-bible structures.
    if isinstance(data, dict):

        for key in [
            "character_bible",
            "characters",
            "main_character",
            "character"
        ]:

            value = data.get(key)

            if isinstance(value, str):
                return value

            if isinstance(value, list):

                text_parts = []

                for item in value:

                    if isinstance(item, str):
                        text_parts.append(item)

                    elif isinstance(item, dict):

                        text_parts.append(
                            json.dumps(
                                item,
                                ensure_ascii=False
                            )
                        )

                if text_parts:
                    return " ".join(text_parts)

            if isinstance(value, dict):

                return json.dumps(
                    value,
                    ensure_ascii=False
                )

        return json.dumps(
            data,
            ensure_ascii=False
        )

    if isinstance(data, list):

        return json.dumps(
            data,
            ensure_ascii=False
        )

    return str(data)


# ============================================================
# REALISTIC IMAGE PROMPT
# ============================================================

def build_prompt(
    scene,
    number,
    character_bible,
    format_value
):

    description = scene_text(
        scene
    )

    if not description:

        description = (
            "A realistic cinematic scene "
            "from an Indian human story"
        )

    if format_value == "full":

        framing = (
            "16:9 cinematic landscape composition"
        )

    else:

        framing = (
            "9:16 vertical cinematic composition"
        )

    prompt_parts = [

        "Photorealistic live-action cinematic still.",

        "Real Indian environment and natural human appearance.",

        "Natural skin texture, realistic clothing, "
        "realistic lighting and physically believable details.",

        "No cartoon, no illustration, no anime, "
        "no 3D render, no painting.",

        framing,

        "The same characters must remain visually consistent "
        "across all scenes.",

        "Natural realistic photography, cinematic depth, "
        "subtle atmospheric detail.",

        description

    ]

    if character_bible:

        prompt_parts.extend([
            "",
            "CHARACTER CONSISTENCY:",
            character_bible
        ])

    return "\n".join(
        prompt_parts
    )


def build_negative_prompt():

    return (
        "cartoon, anime, illustration, painting, "
        "3d render, CGI, plastic skin, doll face, "
        "deformed face, distorted body, extra fingers, "
        "extra limbs, duplicate person, "
        "bad anatomy, unrealistic eyes, "
        "asymmetrical face, warped background, "
        "text, watermark, logo, blurry face, "
        "low quality, oversharpened, artificial skin"
    )


# ============================================================
# FIND EXISTING IMAGES
# ============================================================

def find_existing_image(number):

    candidates = [

        VISUALS_DIR / f"scene_{number:02d}.png",
        VISUALS_DIR / f"scene_{number:02d}.jpg",
        VISUALS_DIR / f"scene_{number:02d}.jpeg",
        VISUALS_DIR / f"scene_{number:02d}.webp",

        VISUALS_DIR / f"scene_{number}.png",
        VISUALS_DIR / f"scene_{number}.jpg",
        VISUALS_DIR / f"scene_{number}.jpeg",
        VISUALS_DIR / f"scene_{number}.webp",

        VISUALS_DIR / f"{number:02d}.png",
        VISUALS_DIR / f"{number:02d}.jpg",
        VISUALS_DIR / f"{number:02d}.jpeg",
        VISUALS_DIR / f"{number:02d}.webp",

        VISUALS_DIR / f"{number}.png",
        VISUALS_DIR / f"{number}.jpg",
        VISUALS_DIR / f"{number}.jpeg",
        VISUALS_DIR / f"{number}.webp",
    ]

    for path in candidates:

        if path.exists():
            return path

    # Recursive fallback
    for path in VISUALS_DIR.rglob("*"):

        if not path.is_file():
            continue

        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        digits = re.findall(
            r"\d+",
            path.stem
        )

        if not digits:
            continue

        try:

            if int(digits[-1]) == number:
                return path

        except Exception:
            pass

    return None


# ============================================================
# BUILD VISUAL JOBS
# ============================================================

def main():

    print("=" * 68)
    print("KATHA LOK AI — PHOTO MOTION VISUAL PREPARATION")
    print("=" * 68)

    format_value = detect_format()

    image_size = image_size_for_format(
        format_value
    )

    print()
    print("FORMAT      :", format_value)
    print("IMAGE SIZE  :", image_size)
    print("SCENES JSON :", SCENES_JSON)
    print("VISUAL DIR  :", VISUALS_DIR)

    scenes_data = load_json(
        SCENES_JSON,
        []
    )

    # --------------------------------------------------------
    # Normalize scene list
    # --------------------------------------------------------

    if isinstance(scenes_data, list):

        scenes = scenes_data

    elif isinstance(scenes_data, dict):

        scenes = None

        for key in [
            "scenes",
            "scene_list",
            "items",
            "data"
        ]:

            if isinstance(
                scenes_data.get(key),
                list
            ):

                scenes = scenes_data[key]
                break

        if scenes is None:
            scenes = []

    else:

        scenes = []

    print()
    print("SCENES FOUND:", len(scenes))

    character_bible = load_character_bible()

    jobs = []
    manifest = []

    # --------------------------------------------------------
    # Build one job per scene
    # --------------------------------------------------------

    for index, scene in enumerate(
        scenes,
        start=1
    ):

        number = scene_number(
            scene,
            index
        )

        duration = get_duration(
            scene
        )

        image_path = find_existing_image(
            number
        )

        prompt = build_prompt(
            scene=scene,
            number=number,
            character_bible=character_bible,
            format_value=format_value
        )

        negative_prompt = (
            build_negative_prompt()
        )

        if image_path:

            status = "ready"

            relative_image = str(
                image_path.relative_to(ROOT)
            )

        else:

            status = "image_required"

            relative_image = (
                f"output/visuals/"
                f"scene_{number:02d}.png"
            )

        job = {

            "scene": number,

            "status": status,

            "image_path": relative_image,

            "prompt": prompt,

            "negative_prompt": negative_prompt,

            "image_size": image_size,

            "duration": duration,

            "motion_engine": "photo_motion",

            "output_video":
                f"output/photo_motion/"
                f"scene_{number:02d}.mp4"

        }

        jobs.append(
            job
        )

        manifest.append({

            "scene": number,

            "image": relative_image,

            "image_exists":
                image_path is not None,

            "duration": duration,

            "status": status

        })

    # --------------------------------------------------------
    # Save jobs
    # --------------------------------------------------------

    save_json(
        VISUAL_JOBS_JSON,
        {
            "version": "photo-motion-1.0",
            "format": format_value,
            "image_size": image_size,
            "motion_engine": "photo_motion",
            "uses_wan2gp": False,
            "uses_colab": False,
            "uses_paid_image_api": False,
            "jobs": jobs
        }
    )

    save_json(
        IMAGE_MANIFEST_JSON,
        {
            "version": "photo-motion-1.0",
            "format": format_value,
            "total_scenes": len(manifest),
            "images_ready": sum(
                1
                for item in manifest
                if item["image_exists"]
            ),
            "images_required": sum(
                1
                for item in manifest
                if not item["image_exists"]
            ),
            "scenes": manifest
        }
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    ready = sum(
        1
        for item in manifest
        if item["image_exists"]
    )

    missing = (
        len(manifest) - ready
    )

    print()
    print("=" * 68)
    print("PHOTO MOTION VISUAL JOBS READY")
    print("=" * 68)

    print(
        "Total scenes       :",
        len(manifest)
    )

    print(
        "Images already here:",
        ready
    )

    print(
        "Images required    :",
        missing
    )

    print()
    print(
        "Visual jobs:",
        VISUAL_JOBS_JSON
    )

    print(
        "Image manifest:",
        IMAGE_MANIFEST_JSON
    )

    # --------------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------------

    if missing > 0:

        print()
        print("=" * 68)
        print("IMAGE GENERATION IS STILL A SEPARATE STAGE")
        print("=" * 68)

        print(
            "Scene prompts/jobs have been prepared."
        )

        print(
            "No paid image-generation API was called."
        )

        print(
            "No Wan2GP queue was created."
        )

        print(
            "No Colab worker is required by this script."
        )

        print()
        print(
            "Once scene images exist in:"
        )

        print(
            f"  {VISUALS_DIR}"
        )

        print()
        print(
            "run:"
        )

        print(
            "  python scripts/photo_motion.py"
        )

    else:

        print()
        print(
            "ALL SCENE IMAGES ARE AVAILABLE."
        )

        print(
            "Photo Motion can now create scene videos."
        )

        print()
        print(
            "Next:"
        )

        print(
            "  python scripts/photo_motion.py"
        )

    print()
    print("=" * 68)
    print("DONE")
    print("=" * 68)


if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print("=" * 68)
        print("FAILED")
        print("=" * 68)

        print(
            str(exc)
        )

        sys.exit(1)
