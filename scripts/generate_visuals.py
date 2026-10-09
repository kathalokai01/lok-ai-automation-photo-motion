#!/usr/bin/env python3
"""Prepare uniquely numbered image jobs for every story scene."""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENES_FILE = ROOT / "output/scenes/scenes.json"
BIBLE_FILE = ROOT / "output/story/character_bible.json"
VISUAL_DIR = ROOT / "output/visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_manifest.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def read_json(path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def scene_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("scenes", "scene_list", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def get_text(scene, keys):
    for key in keys:
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def scene_duration(scene):
    for key in ("duration", "scene_duration", "duration_seconds", "seconds"):
        try:
            value = float(scene.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 5.0


def image_dimensions():
    config_path = ROOT / "Input/topic.txt"
    config = {}
    if config_path.is_file():
        for line in config_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip().upper()] = value.split("#", 1)[0].strip().strip("'\"")

    mode = config.get("FORMAT", "short").lower()
    if mode in {"full", "long", "landscape", "youtube"}:
        return "full", "1280x720"
    return "short", "720x1280"


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    scenes = scene_list(read_json(SCENES_FILE, {}))
    if not scenes:
        raise RuntimeError(f"No scenes found in {SCENES_FILE}")

    mode, size = image_dimensions()
    bible = read_json(BIBLE_FILE, {})
    bible_text = json.dumps(bible, ensure_ascii=False) if bible else ""

    jobs = []
    manifest = []
    seen_keys = set()

    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Invalid scene record at position {index}")

        part = scene.get("part", 1)
        local_scene = scene.get("scene", index)

        key = (str(part), str(local_scene))
        if key in seen_keys:
            raise RuntimeError(
                f"Duplicate part/scene pair found: Part {part}, Scene {local_scene}"
            )
        seen_keys.add(key)

        # The global number is unique even when each part restarts at Scene 1.
        global_number = index
        image_path = VISUAL_DIR / f"scene_{global_number:02d}.png"

        description = get_text(
            scene,
            (
                "visual_prompt",
                "image_prompt",
                "visual_description",
                "description",
                "scene_description",
                "prompt",
                "action",
                "text",
            ),
        )
        if not description:
            description = "A realistic cinematic scene from an Indian story."

        prompt = "\n".join([
            "Photorealistic live-action cinematic still.",
            "Realistic Indian people and environment.",
            "Natural skin texture, anatomy, clothing, lighting and shadows.",
            "Keep recurring character identity and clothing consistent.",
            "No cartoon, anime, illustration, CGI, text, or watermark.",
            f"Composition: {mode} video, {size}.",
            f"Story part: {part}. Original scene number: {local_scene}.",
            description,
            f"Character reference: {bible_text}" if bible_text else "",
        ]).strip()

        jobs.append({
            "scene": global_number,
            "global_scene": global_number,
            "part": part,
            "local_scene": local_scene,
            "status": "image_required",
            "image_path": str(image_path.relative_to(ROOT)),
            "prompt": prompt,
            "negative_prompt": (
                "cartoon, anime, illustration, painting, CGI, plastic skin, "
                "deformed face, bad anatomy, extra limbs, duplicate person, "
                "blurry face, text, watermark, logo"
            ),
            "image_size": size,
            "duration": scene_duration(scene),
            "motion_engine": "photo_motion",
            "output_video": f"output/photo_motion/scene_{global_number:02d}.mp4",
        })

        manifest.append({
            "scene": global_number,
            "global_scene": global_number,
            "part": part,
            "local_scene": local_scene,
            "image": str(image_path.relative_to(ROOT)),
            "duration": scene_duration(scene),
            "status": "image_required",
        })

    write_json(JOBS_FILE, {
        "version": "photo-motion-2.0",
        "format": mode,
        "image_size": size,
        "total_scenes": len(jobs),
        "motion_engine": "photo_motion",
        "uses_wan2gp": False,
        "uses_colab": False,
        "jobs": jobs,
    })

    write_json(MANIFEST_FILE, {
        "version": "photo-motion-2.0",
        "format": mode,
        "total_scenes": len(manifest),
        "images_ready": 0,
        "images_required": len(manifest),
        "scenes": manifest,
    })

    print("VISUAL JOBS PREPARED")
    print("Format:", mode)
    print("Image size:", size)
    print("Total scenes:", len(jobs))
    print("Jobs file:", JOBS_FILE)
    print("Manifest:", MANIFEST_FILE)
    print("Scene numbers are globally unique.")

    if len(jobs) != len(seen_keys):
        raise RuntimeError("Scene count validation failed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"VISUAL JOB PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)