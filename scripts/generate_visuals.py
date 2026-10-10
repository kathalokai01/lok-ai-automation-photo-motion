
#!/usr/bin/env python3
"""Create validated, resumable-compatible photo-motion image jobs."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_input_config, normalize_format

INPUT_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = ROOT / "output" / "scenes" / "scenes.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"
VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_manifest.json"

VERSION = "photo-motion-4.0"

DESCRIPTION_KEYS = (
    "visual_prompt",
    "image_prompt",
    "visual_description",
    "description",
    "scene_description",
    "prompt",
    "action",
    "text",
)

DURATION_KEYS = (
    "duration",
    "scene_duration",
    "duration_seconds",
    "seconds",
)


def log(*items):
    print(*items, flush=True)


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(
                f"Required file is missing: {path.relative_to(ROOT)}"
            )
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read {path.relative_to(ROOT)}: {exc}"
        ) from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def extract_scenes(data):
    if isinstance(data, list):
        scenes = data
    elif isinstance(data, dict):
        scenes = None
        for key in ("scenes", "scene_list", "items", "data"):
            if isinstance(data.get(key), list):
                scenes = data[key]
                break

        if scenes is None:
            raise RuntimeError(
                "scenes.json must contain a list under "
                "'scenes', 'scene_list', 'items', or 'data'."
            )
    else:
        raise RuntimeError(
            "scenes.json must contain a JSON object or list."
        )

    if not scenes:
        raise RuntimeError("scenes.json contains no scenes.")

    if any(not isinstance(scene, dict) for scene in scenes):
        raise RuntimeError(
            "Every entry in scenes.json must be a JSON object."
        )

    return scenes


def get_text(scene, keys):
    for key in keys:
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def positive_number(value):
    try:
        result = float(value)
        if math.isfinite(result) and result > 0:
            return result
    except (TypeError, ValueError):
        pass
    return None


def scene_duration(scene, config):
    for key in DURATION_KEYS:
        value = positive_number(scene.get(key))
        if value is not None and 0.5 <= value <= 3600:
            return round(value, 3)

    configured = positive_number(config.get("SCENE_DURATION"))

    if configured is not None and 0.5 <= configured <= 3600:
        return round(configured, 3)

    return 5.0


def get_format_and_size(config):
    normalized = normalize_format(config)

    if isinstance(normalized, (tuple, list)) and normalized:
        mode = str(normalized[0]).strip().lower()
    else:
        mode = str(config.get("FORMAT", "short")).strip().lower()

    if mode in {"full", "long", "landscape", "youtube"}:
        return "full", "1280x720"

    return "short", "720x1280"


def load_character_bible():
    data = read_json(BIBLE_FILE, required=False)

    if data is None:
        log("NOTICE: Character Bible not found; continuing without it.")
        return ""

    if not isinstance(data, (dict, list)):
        raise RuntimeError(
            "character_bible.json must contain an object or list."
        )

    if not data:
        return ""

    serialized = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    # Keep prompts within reasonable provider request sizes.
    return serialized[:12000]


def validate_scene_identity(scene, index, seen_pairs):
    part = scene.get("part", 1)
    local_scene = scene.get("scene", index)

    if isinstance(part, (dict, list)) or isinstance(local_scene, (dict, list)):
        raise RuntimeError(
            f"Scene {index}: part and scene identifiers must be scalar values."
        )

    pair = (str(part), str(local_scene))

    if pair in seen_pairs:
        raise RuntimeError(
            f"Duplicate part/scene pair: part={part}, scene={local_scene}"
        )

    seen_pairs.add(pair)
    return part, local_scene


def build_prompt(mode, dimensions, part, local_scene, description, bible):
    continuity = ""

    if bible:
        continuity = (
            "\nCharacter continuity reference (keep identity, age, "
            "face, hairstyle and clothing consistent): "
            + bible
        )

    return (
        "Create one photorealistic live-action cinematic photograph "
        "for an Indian story. Show a believable real-world moment, "
        "not a collage, poster, drawing, animation or slideshow frame. "
        "Use realistic Indian people, clothing, locations, architecture "
        "and everyday details when relevant. Natural skin texture, "
        "anatomically plausible bodies, realistic eyes, natural lighting "
        "and shadows, physically plausible depth and composition. "
        "Maintain recurring character identity and clothing across "
        "scenes. No text, subtitles, logo or watermark.\n"
        f"Video format: {mode}, target image size {dimensions}.\n"
        f"Story part: {part}; original scene: {local_scene}.\n"
        f"Scene description: {description}"
        f"{continuity}"
    )


def main():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_input_config(INPUT_FILE)

    mode, dimensions = get_format_and_size(config)
    scenes = extract_scenes(read_json(SCENES_FILE))
    bible = load_character_bible()

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    jobs = []
    manifest_scenes = []
    seen_pairs = set()

    for index, scene in enumerate(scenes, start=1):
        part, local_scene = validate_scene_identity(
            scene, index, seen_pairs
        )

        description = get_text(scene, DESCRIPTION_KEYS)

        if not description:
            raise RuntimeError(
                f"Scene {index} has no usable visual description. "
                "Fix scenes.json instead of silently generating an "
                "unrelated image."
            )

        duration = scene_duration(scene, config)
        image_path = (
            Path("output")
            / "visuals"
            / f"scene_{index:02d}.png"
        ).as_posix()

        prompt = build_prompt(
            mode,
            dimensions,
            part,
            local_scene,
            description,
            bible,
        )

        prompt_hash = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()

        job = {
            "scene": index,
            "global_scene": index,
            "part": part,
            "local_scene": local_scene,
            "status": "image_required",
            "image_path": image_path,
            "prompt": prompt,
            "prompt_sha256": prompt_hash,
            "negative_prompt": (
                "cartoon, anime, illustration, painting, CGI, "
                "plastic skin, distorted face, bad anatomy, "
                "extra limbs, duplicated people, blurry face, "
                "unrealistic eyes, text, subtitles, watermark, logo"
            ),
            "image_size": dimensions,
            "duration": duration,
            "motion_engine": "photo_motion",
            "output_video": (
                f"output/photo_motion/scene_{index:04d}.mp4"
            ),
        }

        jobs.append(job)

        manifest_scenes.append({
            "scene": index,
            "global_scene": index,
            "part": part,
            "local_scene": local_scene,
            "image": image_path,
            "image_path": image_path,
            "duration": duration,
            "status": "image_required",
            "provider": None,
            "prompt_sha256": prompt_hash,
        })

    expected_numbers = list(range(1, len(jobs) + 1))

    if [job["global_scene"] for job in jobs] != expected_numbers:
        raise RuntimeError("Global scene numbering validation failed.")

    if len(jobs) != len(manifest_scenes):
        raise RuntimeError("Image jobs and manifest counts do not match.")

    jobs_document = {
        "version": VERSION,
        "format": mode,
        "image_size": dimensions,
        "total_scenes": len(jobs),
        "motion_engine": "photo_motion",
        "uses_wan2gp": False,
        "uses_colab": False,
        "jobs": jobs,
    }

    manifest_document = {
        "version": VERSION,
        "format": mode,
        "total_scenes": len(manifest_scenes),
        "images_ready": 0,
        "images_required": len(manifest_scenes),
        "scenes": manifest_scenes,
    }

    # Write the jobs first. The image generator consumes this file.
    write_json(JOBS_FILE, jobs_document)
    write_json(MANIFEST_FILE, manifest_document)

    log("=" * 60)
    log("PHOTO-MOTION IMAGE JOBS PREPARED")
    log("Format:", mode)
    log("Image dimensions:", dimensions)
    log("Scenes:", len(jobs))
    log("Character Bible:", "available" if bible else "not available")
    log("Scene numbering: validated")
    log("Jobs:", JOBS_FILE.relative_to(ROOT))
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("VISUAL JOB PREPARATION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(
            f"VISUAL JOB PREPARATION FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
