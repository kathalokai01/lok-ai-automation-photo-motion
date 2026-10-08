#!/usr/bin/env python3
"""
KATHA LOK AI — PHOTO MOTION ENGINE

Converts existing scene images into realistic camera-motion videos.

Effects:
- Slow Zoom In
- Slow Zoom Out
- Pan Left
- Pan Right
- Subtle combined camera movement

NO Wan2GP
NO GPU
NO paid API
NO external image-generation API

Input:
    output/visuals/*.png
    output/visuals/*.jpg
    output/visuals/*.jpeg

Output:
    output/photo_motion/*.mp4

The script can also read scene information from:
    output/scenes/scenes.json
"""

import os
import sys
import json
import glob
import shutil
import subprocess
from pathlib import Path


# ============================================================
# CONFIG
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

SCENES_DIR = ROOT / "output" / "scenes"
VISUALS_DIR = ROOT / "output" / "visuals"
OUTPUT_DIR = ROOT / "output" / "photo_motion"

SCENES_JSON = SCENES_DIR / "scenes.json"

FPS = 24

# Default scene duration.
# If scenes.json contains duration information, that is preferred.
DEFAULT_DURATION = 5.0

# Video quality
CRF = 20
PRESET = "medium"

# Motion strength
ZOOM_START = 1.00
ZOOM_END = 1.10

# Small extra movement prevents the result from looking like
# a completely frozen slideshow.
PAN_STRENGTH = 0.035


# ============================================================
# HELPERS
# ============================================================

def run(cmd):
    print("\nCOMMAND:")
    print(" ".join(str(x) for x in cmd))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}"
        )


def find_ffmpeg():
    path = shutil.which("ffmpeg")

    if not path:
        raise RuntimeError(
            "FFmpeg not found. GitHub Actions must install FFmpeg first."
        )

    return path


def load_scenes():
    if not SCENES_JSON.exists():
        print("WARNING: scenes.json not found.")
        return []

    try:
        with open(SCENES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for key in [
                "scenes",
                "items",
                "scene_list",
                "data"
            ]:
                value = data.get(key)

                if isinstance(value, list):
                    return value

        return []

    except Exception as e:
        print(f"WARNING: could not read scenes.json: {e}")
        return []


def scene_duration(scene):
    if not isinstance(scene, dict):
        return DEFAULT_DURATION

    candidates = [
        scene.get("duration"),
        scene.get("scene_duration"),
        scene.get("duration_seconds"),
        scene.get("seconds"),
    ]

    for value in candidates:
        try:
            value = float(value)
            if value > 0:
                return value
        except Exception:
            pass

    return DEFAULT_DURATION


def get_scene_number(scene, fallback):
    if not isinstance(scene, dict):
        return fallback

    for key in [
        "scene",
        "scene_id",
        "scene_number",
        "id",
        "number"
    ]:
        value = scene.get(key)

        if value is not None:
            text = str(value)

            digits = "".join(
                ch for ch in text
                if ch.isdigit()
            )

            if digits:
                return int(digits)

    return fallback


def find_image(scene_number):
    patterns = [
        f"scene_{scene_number:02d}.png",
        f"scene_{scene_number:02d}.jpg",
        f"scene_{scene_number:02d}.jpeg",

        f"scene_{scene_number}.png",
        f"scene_{scene_number}.jpg",
        f"scene_{scene_number}.jpeg",

        f"{scene_number:02d}.png",
        f"{scene_number:02d}.jpg",
        f"{scene_number:02d}.jpeg",

        f"{scene_number}.png",
        f"{scene_number}.jpg",
        f"{scene_number}.jpeg",
    ]

    for name in patterns:
        path = VISUALS_DIR / name

        if path.exists():
            return path

    # Recursive fallback
    for ext in ["png", "jpg", "jpeg"]:
        matches = list(
            VISUALS_DIR.rglob(
                f"*{scene_number:02d}*.{ext}"
            )
        )

        if matches:
            return matches[0]

    return None


def discover_images():
    extensions = ["*.png", "*.jpg", "*.jpeg"]

    files = []

    for pattern in extensions:
        files.extend(
            VISUALS_DIR.rglob(pattern)
        )

    # Remove duplicates
    unique = {}

    for path in files:
        unique[str(path.resolve())] = path

    files = list(unique.values())

    def sort_key(path):
        name = path.stem

        digits = "".join(
            ch for ch in name
            if ch.isdigit()
        )

        if digits:
            return int(digits)

        return 999999

    files.sort(key=sort_key)

    return files


# ============================================================
# MOTION FILTERS
# ============================================================

def motion_filter(effect, frames, width, height):
    """
    Build FFmpeg zoompan expression.

    FFmpeg zoompan supports:
        zoom
        x
        y
        duration
        output size
        fps

    Reference:
    FFmpeg zoompan filter documentation.
    """

    if effect == "zoom_in":

        zoom = (
            f"min({ZOOM_START:.4f}+"
            f"({ZOOM_END - ZOOM_START:.4f})*on/"
            f"{max(frames - 1, 1)},"
            f"{ZOOM_END:.4f})"
        )

        x = (
            f"(iw-iw/zoom)/2"
        )

        y = (
            f"(ih-ih/zoom)/2"
        )

    elif effect == "zoom_out":

        zoom = (
            f"max({ZOOM_END:.4f}-"
            f"({ZOOM_END - ZOOM_START:.4f})*on/"
            f"{max(frames - 1, 1)},"
            f"{ZOOM_START:.4f})"
        )

        x = (
            f"(iw-iw/zoom)/2"
        )

        y = (
            f"(ih-ih/zoom)/2"
        )

    elif effect == "pan_left":

        zoom = "1.06"

        progress = (
            f"on/{max(frames - 1, 1)}"
        )

        x = (
            f"(iw-iw/zoom)*"
            f"(1-{progress})"
        )

        y = (
            f"(ih-ih/zoom)/2"
        )

    elif effect == "pan_right":

        zoom = "1.06"

        progress = (
            f"on/{max(frames - 1, 1)}"
        )

        x = (
            f"(iw-iw/zoom)*"
            f"{progress}"
        )

        y = (
            f"(ih-ih/zoom)/2"
        )

    elif effect == "pan_up":

        zoom = "1.05"

        progress = (
            f"on/{max(frames - 1, 1)}"
        )

        x = (
            f"(iw-iw/zoom)/2"
        )

        y = (
            f"(ih-ih/zoom)*"
            f"(1-{progress})"
        )

    elif effect == "pan_down":

        zoom = "1.05"

        progress = (
            f"on/{max(frames - 1, 1)}"
        )

        x = (
            f"(iw-iw/zoom)/2"
        )

        y = (
            f"(ih-ih/zoom)*"
            f"{progress}"
        )

    else:

        # Very subtle cinematic movement
        zoom = (
            f"1.00+0.035*"
            f"sin(on/{max(frames,1)}*PI)"
        )

        x = (
            "(iw-iw/zoom)/2"
        )

        y = (
            "(ih-ih/zoom)/2"
        )

    return (
        f"zoompan="
        f"z='{zoom}':"
        f"x='{x}':"
        f"y='{y}':"
        f"d=1:"
        f"s={width}x{height}:"
        f"fps={FPS},"
        f"format=yuv420p"
    )


# ============================================================
# EFFECT SELECTION
# ============================================================

EFFECTS = [
    "zoom_in",
    "zoom_out",
    "pan_left",
    "pan_right",
    "subtle",
]


def choose_effect(index):
    return EFFECTS[index % len(EFFECTS)]


# ============================================================
# CREATE VIDEO
# ============================================================

def create_scene_video(
    ffmpeg,
    image_path,
    output_path,
    duration,
    effect,
    width,
    height
):

    frames = max(
        int(round(duration * FPS)),
        FPS
    )

    vf = motion_filter(
        effect=effect,
        frames=frames,
        width=width,
        height=height
    )

    cmd = [
        ffmpeg,
        "-y",

        "-loop",
        "1",

        "-i",
        str(image_path),

        "-vf",
        vf,

        "-frames:v",
        str(frames),

        "-c:v",
        "libx264",

        "-preset",
        PRESET,

        "-crf",
        str(CRF),

        "-pix_fmt",
        "yuv420p",

        "-movflags",
        "+faststart",

        str(output_path)
    ]

    run(cmd)


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 68)
    print("KATHA LOK AI — PHOTO MOTION ENGINE")
    print("=" * 68)

    ffmpeg = find_ffmpeg()

    print()
    print("FFmpeg :", ffmpeg)
    print("Visuals:", VISUALS_DIR)
    print("Output :", OUTPUT_DIR)
    print("FPS    :", FPS)

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    scenes = load_scenes()

    # --------------------------------------------------------
    # Determine image list
    # --------------------------------------------------------

    image_jobs = []

    if scenes:

        print()
        print("SCENES.JSON FOUND")
        print("Scenes:", len(scenes))

        for index, scene in enumerate(
            scenes,
            start=1
        ):

            scene_number = get_scene_number(
                scene,
                index
            )

            image = find_image(
                scene_number
            )

            if image is None:
                print(
                    f"WARNING: image missing "
                    f"for scene {scene_number}"
                )
                continue

            duration = scene_duration(
                scene
            )

            image_jobs.append(
                (
                    scene_number,
                    image,
                    duration
                )
            )

    else:

        print()
        print("No usable scenes.json.")
        print("Searching visuals directory...")

        images = discover_images()

        for index, image in enumerate(
            images,
            start=1
        ):
            image_jobs.append(
                (
                    index,
                    image,
                    DEFAULT_DURATION
                )
            )

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    if not image_jobs:

        raise RuntimeError(
            "\nNO SCENE IMAGES FOUND.\n\n"
            "Photo Motion cannot create videos without "
            "scene images.\n\n"
            f"Expected images inside:\n"
            f"{VISUALS_DIR}\n\n"
            "This is intentional: the engine does NOT "
            "call a paid image-generation API."
        )

    print()
    print("=" * 68)
    print("PHOTO MOTION JOBS")
    print("=" * 68)

    print(
        f"Images found: {len(image_jobs)}"
    )

    # --------------------------------------------------------
    # Determine output format
    # --------------------------------------------------------

    # Keep the same visual aspect strategy as the existing
    # Katha Lok AI pipeline:
    #
    # Short = vertical
    # Full  = landscape
    #
    # The workflow may override these using environment vars.

    mode = os.environ.get(
        "VIDEO_MODE",
        os.environ.get(
            "FORMAT",
            "short"
        )
    ).lower()

    if mode == "full":

        width = 1280
        height = 720

    else:

        width = 720
        height = 1280

    print()
    print("Mode :", mode)
    print(
        "Size :",
        f"{width}x{height}"
    )

    # --------------------------------------------------------
    # Create scene videos
    # --------------------------------------------------------

    manifest = []

    for index, (
        scene_number,
        image,
        duration
    ) in enumerate(
        image_jobs,
        start=1
    ):

        effect = choose_effect(
            index - 1
        )

        output_name = (
            f"scene_{scene_number:02d}.mp4"
        )

        output_path = (
            OUTPUT_DIR /
            output_name
        )

        print()
        print("-" * 68)
        print(
            f"SCENE {scene_number}"
        )
        print(
            "Image   :",
            image
        )
        print(
            "Duration:",
            f"{duration:.2f}s"
        )
        print(
            "Effect  :",
            effect
        )
        print(
            "Output  :",
            output_path
        )

        create_scene_video(
            ffmpeg=ffmpeg,
            image_path=image,
            output_path=output_path,
            duration=duration,
            effect=effect,
            width=width,
            height=height
        )

        manifest.append(
            {
                "scene": scene_number,
                "image": str(image),
                "video": str(output_path),
                "duration": duration,
                "effect": effect,
                "width": width,
                "height": height,
                "fps": FPS,
            }
        )

    # --------------------------------------------------------
    # Save manifest
    # --------------------------------------------------------

    manifest_path = (
        OUTPUT_DIR /
        "photo_motion_manifest.json"
    )

    with open(
        manifest_path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            manifest,
            f,
            ensure_ascii=False,
            indent=2
        )

    print()
    print("=" * 68)
    print("PHOTO MOTION COMPLETE")
    print("=" * 68)

    print(
        "Scene videos:",
        len(manifest)
    )

    print(
        "Output:",
        OUTPUT_DIR
    )

    print(
        "Manifest:",
        manifest_path
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )
        sys.exit(130)

    except Exception as e:

        print()
        print("=" * 68)
        print("PHOTO MOTION FAILED")
        print("=" * 68)
        print(str(e))

        sys.exit(1)
