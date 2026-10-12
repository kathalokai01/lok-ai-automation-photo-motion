#!/usr/bin/env python3
"""Prepare resumable photo-motion image jobs with consistent prompt hashes."""

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

VERSION = "photo-motion-5.0"
MIN_IMAGE_BYTES = 5000

# Keep this text identical to free_image_generator.py.
QUALITY = (
    "Photorealistic live-action cinematic photography. Realistic Indian "
    "people, clothing, locations and architecture. Natural skin texture, "
    "believable anatomy, realistic eyes, natural lighting and shadows. "
    "Keep recurring character identity, age, hairstyle and clothing "
    "consistent. No cartoon, anime, illustration, CGI, text, logo or "
    "watermark."
)

NEGATIVE_PROMPT = (
    "cartoon, anime, illustration, painting, CGI, plastic skin, "
    "distorted face, bad anatomy, extra limbs, duplicated people, "
    "blurry face, unrealistic eyes, text, subtitles, watermark, logo"
)

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
            raise RuntimeError(f"Required file is missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


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
        scenes = next(
            (
                data[key]
                for key in ("scenes", "scene_list", "items", "data")
                if isinstance(data.get(key), list)
            ),
            None,
        )
        if scenes is None:
            raise RuntimeError(
                "scenes.json must contain a list under scenes, "
                "scene_list, items or data."
            )
    else:
        raise RuntimeError("scenes.json must be a JSON object or list.")

    if not scenes:
        raise RuntimeError("scenes.json contains no scenes.")

    if any(not isinstance(scene, dict) for scene in scenes):
        raise RuntimeError("Every scene must be a JSON object.")

    return scenes


def get_text(scene, keys):
    for key in keys:
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def positive_number(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
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

    if data is None or data == {} or data == []:
        log("NOTICE: Character Bible unavailable.")
        return ""

    if not isinstance(data, (dict, list)):
        raise RuntimeError("character_bible.json must be an object or list.")

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:12000]


def validate_scene_identity(scene, index, seen_pairs):
    part = scene.get("part", 1)
    local_scene = scene.get("scene", index)

    if isinstance(part, (dict, list)) or isinstance(local_scene, (dict, list)):
        raise RuntimeError(
            f"Scene {index}: part and scene identifiers must be scalar."
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
            "\nCharacter continuity reference: preserve identity, age, "
            "face, hairstyle, clothing and physical appearance. "
            + bible
        )

    return (
        "Create one photorealistic live-action cinematic photograph "
        "for an Indian story. Show one believable real-world moment, "
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


def generation_prompt_hash(prompt, negative_prompt):
    """Must match free_image_generator.py prompt_for() and get_hash()."""
    complete_prompt = (
        f"{prompt}\n\nQuality: {QUALITY}\n\nAvoid: {negative_prompt}"
    )
    return hashlib.sha256(complete_prompt.encode("utf-8")).hexdigest()


def read_previous_image_manifest():
    data = read_json(MANIFEST_FILE, required=False)

    if isinstance(data, dict):
        rows = data.get("scenes", [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    previous = {}
    if not isinstance(rows, list):
        return previous

    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            number = int(row.get("global_scene", row.get("scene")))
        except (TypeError, ValueError):
            continue
        if number > 0:
            previous[number] = row

    return previous


def valid_existing_image(path, expected_dimensions):
    try:
        from PIL import Image

        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return False

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            return image.size == expected_dimensions

    except Exception:
        return False


def main():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_input_config(INPUT_FILE)
    mode, dimensions = get_format_and_size(config)
    width, height = map(int, dimensions.split("x"))

    scenes = extract_scenes(read_json(SCENES_FILE))
    bible = load_character_bible()

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    # Read the previous manifest before replacing it.
    previous_rows = read_previous_image_manifest()

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
                f"Scene {index} has no visual description. "
                "Fix scenes.json rather than generating an unrelated image."
            )

        duration = scene_duration(scene, config)
        image_path = f"output/visuals/scene_{index:02d}.png"

        prompt = build_prompt(
            mode, dimensions, part, local_scene, description, bible
        )
        prompt_hash = generation_prompt_hash(prompt, NEGATIVE_PROMPT)

        job = {
            "scene": index,
            "global_scene": index,
            "part": part,
            "local_scene": local_scene,
            "status": "image_required",
            "image_path": image_path,
            "prompt": prompt,
            "prompt_sha256": prompt_hash,
            "negative_prompt": NEGATIVE_PROMPT,
            "image_size": dimensions,
            "duration": duration,
            "motion_engine": "photo_motion",
            "output_video": f"output/photo_motion/scene_{index:04d}.mp4",
        }
        jobs.append(job)

        destination = ROOT / image_path
        old = previous_rows.get(index, {})

        same_prompt = old.get("prompt_sha256") == prompt_hash
        image_is_valid = valid_existing_image(
            destination, (width, height)
        )

        if same_prompt and image_is_valid:
            status = old.get("status", "existing")
            provider = old.get("provider") or "previous run"
            if status not in ("generated", "existing", "reused"):
                status = "existing"
        else:
            status = "image_required"
            provider = None

        manifest_scenes.append({
            "scene": index,
            "global_scene": index,
            "part": part,
            "local_scene": local_scene,
            "image": image_path,
            "image_path": image_path,
            "duration": duration,
            "status": status,
            "provider": provider,
            "prompt_sha256": prompt_hash,
        })

    if len(jobs) != len(manifest_scenes):
        raise RuntimeError("Image job and manifest counts do not match.")

    expected = list(range(1, len(jobs) + 1))
    if [job["global_scene"] for job in jobs] != expected:
        raise RuntimeError("Global scene numbering validation failed.")

    images_ready = sum(
        row["status"] in ("generated", "existing", "reused")
        for row in manifest_scenes
    )

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
        "images_ready": images_ready,
        "images_required": len(manifest_scenes),
        "scenes": manifest_scenes,
    }

    write_json(JOBS_FILE, jobs_document)
    write_json(MANIFEST_FILE, manifest_document)

    log("=" * 60)
    log("PHOTO-MOTION IMAGE JOBS PREPARED")
    log("Format:", mode)
    log("Image dimensions:", dimensions)
    log("Scenes:", len(jobs))
    log("Valid matching images retained:", images_ready)
    log("Images requiring generation:", len(jobs) - images_ready)
    log("Character Bible:", "available" if bible else "not available")
    log("Prompt hashes: aligned with free_image_generator.py")
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
        print(f"VISUAL JOB PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)