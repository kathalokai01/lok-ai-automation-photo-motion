#!/usr/bin/env python3
"""Katha Lok AI Photo Motion engine."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format

VISUALS = ROOT / "output" / "visuals"
SCENES_JSON = ROOT / "output" / "scenes" / "scenes.json"
OUT = ROOT / "output" / "photo_motion"

FPS = 24
CRF = 20
PRESET = "medium"
EFFECTS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "subtle"]


def run(cmd):
    print("\n$", " ".join(map(str, cmd)))
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True
    )
    print(result.stdout)
    if result.returncode:
        raise RuntimeError(f"Command failed: {result.returncode}")


def load_scenes():
    if not SCENES_JSON.exists():
        return []
    try:
        data = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("scenes", "items", "scene_list", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
    except Exception as exc:
        print("WARNING: scenes.json:", exc)
    return []


def scene_number(scene, fallback):
    if isinstance(scene, dict):
        for key in ("scene", "scene_id", "scene_number", "id", "number"):
            value = scene.get(key)
            if value is not None:
                match = re.search(r"\d+", str(value))
                if match:
                    return int(match.group())
    return fallback


def scene_duration(scene):
    if isinstance(scene, dict):
        for key in ("duration", "scene_duration", "duration_seconds", "seconds"):
            try:
                value = float(scene.get(key))
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    return 5.0


def find_image(number):
    for ext in ("png", "jpg", "jpeg"):
        for name in (
            f"scene_{number:02d}.{ext}",
            f"scene_{number}.{ext}",
        ):
            path = VISUALS / name
            if path.is_file():
                return path

    if VISUALS.exists():
        pattern = re.compile(rf"(?<!\d)0*{number}(?!\d)")
        for path in sorted(VISUALS.rglob("*")):
            if path.suffix.lower() in (".png", ".jpg", ".jpeg"):
                if pattern.search(path.stem):
                    return path
    return None


def discover_images():
    if not VISUALS.exists():
        return []
    files = [
        p for p in VISUALS.rglob("*")
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")
    ]

    def sort_key(path):
        nums = re.findall(r"\d+", path.stem)
        return (int(nums[-1]) if nums else 999999, path.name.lower())

    return sorted(files, key=sort_key)


def make_filter(effect, frames, width, height):
    denom = max(frames - 1, 1)

    if effect == "zoom_in":
        zoom = f"min(1+0.10*on/{denom},1.10)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif effect == "zoom_out":
        zoom = f"max(1.10-0.10*on/{denom},1.0)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif effect == "pan_left":
        zoom = "1.06"
        x = f"(iw-iw/zoom)*(1-on/{denom})"
        y = "(ih-ih/zoom)/2"
    elif effect == "pan_right":
        zoom = "1.06"
        x = f"(iw-iw/zoom)*on/{denom}"
        y = "(ih-ih/zoom)/2"
    else:
        zoom = f"1+0.025*sin(on/{max(frames, 1)}*PI)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    return (
        f"scale={width*2}:{height*2}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={width*2}:{height*2},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':"
        f"d=1:s={width}x{height}:fps={FPS},"
        "format=yuv420p"
    )


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)

    # Short = vertical; Full = landscape.
    width, height = (1280, 720) if mode == "full" else (720, 1280)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found.")

    OUT.mkdir(parents=True, exist_ok=True)

    scenes = load_scenes()
    jobs = []

    if scenes:
        for index, scene in enumerate(scenes, 1):
            number = scene_number(scene, index)
            image = find_image(number)
            if image:
                jobs.append((number, image, scene_duration(scene)))
            else:
                print(f"WARNING: image missing for scene {number}")
    else:
        jobs = [(i, image, 5.0) for i, image in enumerate(discover_images(), 1)]

    if not jobs:
        raise RuntimeError(f"No scene images found in {VISUALS}")

    # Remove stale clips so previous runs cannot leak into this video.
    for old in OUT.glob("scene_*.mp4"):
        old.unlink()

    manifest = []

    for index, (number, image, seconds) in enumerate(jobs):
        frames = max(FPS, round(seconds * FPS))
        effect = EFFECTS[index % len(EFFECTS)]
        target = OUT / f"scene_{number:02d}.mp4"

        print(
            f"\nScene {number}: {image.name}; "
            f"{seconds:.2f}s; {effect}; FORMAT={mode}"
        )

        run([
            ffmpeg, "-y",
            "-loop", "1", "-i", str(image),
            "-vf", make_filter(effect, frames, width, height),
            "-frames:v", str(frames),
            "-an",
            "-c:v", "libx264",
            "-preset", PRESET,
            "-crf", str(CRF),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(target),
        ])

        manifest.append({
            "scene": number,
            "image": str(image.relative_to(ROOT)),
            "video": str(target.relative_to(ROOT)),
            "duration": seconds,
            "effect": effect,
            "width": width,
            "height": height,
            "fps": FPS,
            "format": mode,
        })

    (OUT / "photo_motion_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"SUCCESS: {len(manifest)} clips; FORMAT={mode}; {width}x{height}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print("PHOTO MOTION FAILED:", exc)
        sys.exit(1)