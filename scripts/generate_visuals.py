#!/usr/bin/env python3
"""Build consistent, globally numbered scene-image jobs and manifest."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = ROOT / "output" / "scenes" / "scenes.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"
VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_manifest.json"


def read_json(path, default=None):
    if not path.is_file():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read JSON file {path.relative_to(ROOT)}: {exc}"
        ) from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_config():
    config = {}

    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    for raw in INPUT_FILE.read_text(
        encoding="utf-8-sig"
    ).splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        config[key.strip().upper()] = (
            value.split("#", 1)[0].strip().strip("'\"")
        )

    return config


def extract_scenes(data):
    if isinstance(data, list):
        scenes = data
    elif isinstance(data, dict):
        scenes = None

        for key in ("scenes", "scene_list", "items", "data"):
            value = data.get(key)

            if isinstance(value, list):
                scenes = value
                break

        if scenes is None:
            raise RuntimeError(
                "scenes.json has no supported scene list."
            )
    else:
        raise RuntimeError(
            "scenes.json must contain an object or list."
        )

    if not scenes:
        raise RuntimeError("scenes.json contains no scenes.")

    if any(not isinstance(scene, dict) for scene in scenes):
        raise RuntimeError(
            "Every scene must be a JSON object."
        )

    return scenes


def get_text(scene, keys):
    for key in keys:
        value = scene.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def scene_duration(scene, config):
    for key in (
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds",
    ):
        try:
            duration = float(scene.get(key))

            if 0.5 <= duration <= 600:
                return round(duration, 3)
        except (TypeError, ValueError):
            pass

    try:
        configured = float(config.get("SCENE_DURATION", "5"))

        if 0.5 <= configured <= 600:
            return round(configured, 3)
    except (TypeError, ValueError):
        pass

    return 5.0


def normalize_format(config):
    value = config.get("FORMAT", "short").strip().lower()

    if value in {"full", "long", "landscape", "youtube"}:
        return "full", "1280x720"

    return "short", "720x1280"


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    config = read_config()
    scenes = extract_scenes(read_json(SCENES_FILE))
    mode, dimensions = normalize_format(config)

    bible = read_json(BIBLE_FILE, {})
    if bible is None:
        bible = {}

    bible_text = json.dumps(
        bible,
        ensure_ascii=False,
        separators=(",", ":"),
    ) if bible else ""

    jobs = []
    manifest_scenes = []
    seen_pairs = set()

    for index, scene in enumerate(scenes, start=1):
        part = scene.get("part", 1)
        local_scene = scene.get("scene", index)

        pair = (str(part), str(local_scene))

        if pair in seen_pairs:
            raise RuntimeError(
                f"Duplicate part/scene pair: part={part}, "
                f"scene={local_scene}"
            )

        seen_pairs.add(pair)

        global_scene = index
        relative_image = (
            Path("output")
            / "visuals"
            / f"scene_{global_scene:02d}.png"
        )
        image_path = relative_image.as_posix()

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
            description = (
                "A realistic cinematic scene from an Indian story."
            )

        character_reference = (
            f"\nCharacter continuity reference: {bible_text}"
            if bible_text else ""
        )

        prompt = (
            "Photorealistic live-action cinematic still. "
            "Realistic Indian people, locations, clothing, "
            "architecture and everyday details. Natural skin "
            "texture, believable anatomy, realistic lighting "
            "and shadows. Maintain recurring character identity "
            "and clothing between scenes. No cartoon, anime, "
            "illustration, CGI, text, logo or watermark.\n"
            f"Composition: {mode} video, {dimensions}.\n"
            f"Story part: {part}; original scene: {local_scene}.\n"
            f"Scene description: {description}"
            f"{character_reference}"
        )

        duration = scene_duration(scene, config)

        job = {
            "scene": global_scene,
            "global_scene": global_scene,
            "part": part,
            "local_scene": local_scene,
            "status": "image_required",
            "image_path": image_path,
            "prompt": prompt,
            "negative_prompt": (
                "cartoon, anime, illustration, painting, CGI, "
                "plastic skin, distorted face, bad anatomy, "
                "extra limbs, duplicate people, blurry face, "
                "text, watermark, logo"
            ),
            "image_size": dimensions,
            "duration": duration,
            "motion_engine": "photo_motion",
            "output_video": (
                f"output/photo_motion/scene_{global_scene:02d}.mp4"
            ),
        }

        jobs.append(job)

        manifest_scenes.append({
            "scene": global_scene,
            "global_scene": global_scene,
            "part": part,
            "local_scene": local_scene,
            "image": image_path,
            "image_path": image_path,
            "duration": duration,
            "status": "image_required",
            "provider": None,
        })

    jobs_document = {
        "version": "photo-motion-3.0",
        "format": mode,
        "image_size": dimensions,
        "total_scenes": len(jobs),
        "motion_engine": "photo_motion",
        "uses_wan2gp": False,
        "uses_colab": False,
        "jobs": jobs,
    }

    manifest_document = {
        "version": "photo-motion-3.0",
        "format": mode,
        "total_scenes": len(manifest_scenes),
        "images_ready": 0,
        "images_required": len(manifest_scenes),
        "scenes": manifest_scenes,
    }

    if len(jobs) != len(manifest_scenes):
        raise RuntimeError(
            "Internal error: jobs and manifest counts differ."
        )

    if [
        row["global_scene"] for row in jobs
    ] != list(range(1, len(jobs) + 1)):
        raise RuntimeError(
            "Global scene numbering is not continuous."
        )

    write_json(JOBS_FILE, jobs_document)
    write_json(MANIFEST_FILE, manifest_document)

    print("VISUAL JOBS PREPARED")
    print("Format:", mode)
    print("Image dimensions:", dimensions)
    print("Total scenes:", len(jobs))
    print("Global scene numbering: verified")
    print("Jobs:", JOBS_FILE.relative_to(ROOT))
    print("Image manifest:", MANIFEST_FILE.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"VISUAL JOB PREPARATION FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)