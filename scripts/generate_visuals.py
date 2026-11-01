#!/usr/bin/env python3

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

from input_config import load_input_config, normalize_format

BASE = Path("output")

SCENES_FILE = BASE / "scenes" / "scenes.json"
CHARACTER_BIBLE_FILE = BASE / "story" / "character_bible.json"

VISUALS_DIR = BASE / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"

WORKER_DIR = BASE / "visual_worker"
QUEUE_JSON = WORKER_DIR / "visual_generation_queue.json"
QUEUE_ZIP = BASE / "i2v" / "visual_generation_queue.zip"

QUEUE_VERSION = 2
MIN_IMAGE_BYTES = 1000


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    if value is None:
        return default

    value = str(value).strip().lower()

    if value in {"true", "1", "yes", "y", "on"}:
        return True

    if value in {"false", "0", "no", "n", "off"}:
        return False

    return default


def cfg_int(config, key, default=0):
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def load_json(path, required=False):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        temporary.replace(path)

    finally:
        if temporary.exists():
            temporary.unlink()


def stable_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def scene_key(part, scene):
    return f"part_{part:02d}/scene_{scene:02d}"


def valid_image(path):
    if not path.is_file():
        return False

    try:
        if path.stat().st_size <= MIN_IMAGE_BYTES:
            return False

        with path.open("rb") as handle:
            header = handle.read(16)

        png = header.startswith(b"\x89PNG\r\n\x1a\n")
        jpeg = header.startswith(b"\xff\xd8\xff")
        webp = (
            len(header) >= 12
            and header[:4] == b"RIFF"
            and header[8:12] == b"WEBP"
        )

        return png or jpeg or webp

    except OSError:
        return False


def build_negative_prompt(config):
    negative = []

    if cfg_bool(config, "AVOID_CARTOON_LOOK", True):
        negative.extend([
            "cartoon",
            "comic",
            "anime",
            "manga",
            "illustration",
            "drawing",
            "painting",
            "2D art",
        ])

    if cfg_bool(config, "AVOID_NEON", True):
        negative.append("neon colors")

    if cfg_bool(config, "AVOID_GLITCH_EFFECTS", True):
        negative.extend([
            "glitch",
            "digital distortion",
            "flicker",
            "frame tearing",
        ])

    negative.extend([
        "CGI look",
        "3D render",
        "video game graphics",
        "plastic skin",
        "artificial face",
        "deformed anatomy",
        "extra fingers",
        "extra limbs",
        "duplicate person",
        "watermark",
        "logo",
        "unwanted text",
        "low resolution",
        "blurry face",
        "oversharpening",
        "unnatural skin texture",
    ])

    return ", ".join(dict.fromkeys(negative))


def get_character_context(scene, character_bible):
    if not isinstance(character_bible, dict):
        return ""

    references = scene.get(
        "characters",
        scene.get("character_ids", []),
    )

    if not isinstance(references, list):
        references = [references] if references else []

    if not references:
        return ""

    bible_characters = character_bible.get("characters", [])
    if not isinstance(bible_characters, list):
        return ""

    wanted = {str(item).strip() for item in references}
    selected = []

    for character in bible_characters:
        if not isinstance(character, dict):
            continue

        character_id = str(character.get("id", "")).strip()
        character_name = str(character.get("name", "")).strip()

        if character_id in wanted or character_name in wanted:
            selected.append(character)

    if not selected:
        return ""

    return json.dumps(
        selected,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_visual_prompt(config, scene, character_bible):
    visual_style = cfg_text(
        config, "VISUAL_STYLE", "cinematic_realistic"
    )
    realism = cfg_text(config, "REALISM", "high")
    camera_style = cfg_text(config, "CAMERA_STYLE", "cinematic")
    lighting = cfg_text(config, "LIGHTING", "cinematic")
    mood = cfg_text(config, "MOOD", "dramatic")
    quality = cfg_text(config, "QUALITY", "high")

    scene_description = str(
        scene.get("visual_prompt")
        or scene.get("visual")
        or scene.get("description")
        or ""
    ).strip()

    action = str(scene.get("action") or "").strip()
    world = str(
        scene.get("world_context")
        or scene.get("world")
        or ""
    ).strip()

    camera_prompt = str(scene.get("camera_prompt") or "").strip()
    lighting_prompt = str(scene.get("lighting_prompt") or "").strip()

    character_context = get_character_context(
        scene,
        character_bible,
    )

    if not scene_description:
        raise RuntimeError(
            f"Scene {scene.get('part')}/{scene.get('scene')} "
            "has no visual description."
        )

    sections = [
        "Photorealistic live-action cinematic film frame.",
        "Real human actors in a physically believable real-world environment.",
        f"Visual style: {visual_style}.",
        f"Realism level: {realism}.",
        f"Camera language: {camera_style}.",
        f"Lighting: {lighting}.",
        f"Emotional mood: {mood}.",
        f"Image quality: {quality}.",
        "Natural human anatomy, realistic skin pores and natural facial proportions.",
        "Physically accurate light, shadows, reflections and material textures.",
        "Cinematic composition with clear foreground, subject and background separation.",
        "Maintain character identity, age appearance, hairstyle and clothing continuity.",
        "Maintain location, architecture, time period and environmental continuity.",
        "No captions, subtitles, labels, typography or watermarks in the image.",
    ]

    if cfg_bool(config, "CHARACTER_CONSISTENCY", True):
        sections.append(
            "Recurring characters must retain the same recognizable face, "
            "body proportions, hairstyle and clothing across every scene."
        )

    if cfg_bool(config, "WORLD_CONSISTENCY", True):
        sections.append(
            "Keep the same story world, historical period, architecture, "
            "weather and environmental design wherever the story requires it."
        )

    if cfg_bool(config, "SCENE_CONTINUITY", True):
        sections.append(
            "The frame must visually connect with the preceding and following "
            "story scenes without changing established details."
        )

    sections.append("Scene: " + scene_description)

    if action:
        sections.append("Visible action: " + action)

    if world:
        sections.append("World context: " + world)

    if camera_prompt:
        sections.append("Camera direction: " + camera_prompt)

    if lighting_prompt:
        sections.append("Scene-specific lighting: " + lighting_prompt)

    if character_context:
        sections.append("Character Bible references: " + character_context)

    return "\n\n".join(sections)


def get_image_size(format_name):
    if format_name == "short":
        return "768x1024"

    return "1024x576"


def validate_scenes(scenes, parts, scenes_per_part):
    if not isinstance(scenes, list):
        raise RuntimeError("scenes.json must contain a scenes list.")

    expected = parts * scenes_per_part

    if len(scenes) != expected:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected}, got {len(scenes)}."
        )

    seen = set()
    normalized = []

    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Scene #{index} is not a JSON object.")

        default_part = ((index - 1) // scenes_per_part) + 1
        default_scene = ((index - 1) % scenes_per_part) + 1

        try:
            part = int(scene.get("part", default_part))
            scene_number = int(scene.get("scene", default_scene))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Scene #{index} has invalid part/scene values."
            ) from exc

        if not 1 <= part <= parts:
            raise RuntimeError(f"Invalid part number in scene #{index}: {part}")

        if not 1 <= scene_number <= scenes_per_part:
            raise RuntimeError(
                f"Invalid scene number in scene #{index}: {scene_number}"
            )

        key = scene_key(part, scene_number)

        if key in seen:
            raise RuntimeError(f"Duplicate scene identifier: {key}")

        seen.add(key)

        normalized_scene = dict(scene)
        normalized_scene["part"] = part
        normalized_scene["scene"] = scene_number

        if not str(
            normalized_scene.get("visual_prompt")
            or normalized_scene.get("visual")
            or normalized_scene.get("description")
            or ""
        ).strip():
            raise RuntimeError(f"{key} has no visual prompt.")

        normalized.append((key, normalized_scene, index))

    expected_keys = {
        scene_key(part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    missing = sorted(expected_keys - seen)

    if missing:
        raise RuntimeError(
            "Missing scene identifiers: " + ", ".join(missing[:20])
        )

    return sorted(
        normalized,
        key=lambda item: (
            item[1]["part"],
            item[1]["scene"],
        ),
    )

