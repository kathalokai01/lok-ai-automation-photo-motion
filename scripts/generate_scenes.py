#!/usr/bin/env python3
"""Build visual scenes from the completed AI story without regenerating it.

This stage is deliberately offline: generate_ai_story.py already creates
visual, camera, lighting and sound directions. Calling Gemini again here
would duplicate API usage without adding a required pipeline stage.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from input_config import (
    load_input_config,
    normalize_format,
    get_topic,
    get_story_text,
)


ROOT = Path(".")
INPUT_FILE = ROOT / "Input/topic.txt"
STORY_FILE = ROOT / "output/story/story.json"
AI_STORY_FILE = ROOT / "output/story/ai_story.json"
BIBLE_FILE = ROOT / "output/story/character_bible.json"

OUTPUT_DIR = ROOT / "output/scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output/checkpoints/scenes_progress.json"

VERSION = "photo-motion-scenes-v5-offline"
SCENE_PROMPT_VERSION = 5


def log(message=""):
    print(message, flush=True)


def clean(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def cfg(config, key, default=""):
    value = config.get(key, default)
    return default if value is None else value


def read_json(path, required=False):
    path = Path(path)
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Required file missing: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return data


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")

    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temp, path)


def digest(data):
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_settings(config):
    return {
        "format": normalize_format(config),
        "audience": clean(cfg(config, "AUDIENCE", "adult")),
        "visual_style": clean(
            cfg(config, "VISUAL_STYLE", "cinematic_realistic")
        ),
        "realism": clean(cfg(config, "REALISM", "high")),
        "quality": clean(cfg(config, "QUALITY", "high")),
        "camera_style": clean(cfg(config, "CAMERA_STYLE", "cinematic")),
        "lighting": clean(cfg(config, "LIGHTING", "cinematic")),
        "mood": clean(cfg(config, "MOOD", "dramatic")),
        "natural_motion": as_bool(
            cfg(config, "NATURAL_MOTION", True), True
        ),
        "cinematic_camera": as_bool(
            cfg(config, "CINEMATIC_CAMERA", True), True
        ),
        "realistic_lighting": as_bool(
            cfg(config, "REALISTIC_LIGHTING", True), True
        ),
        "character_bible": as_bool(
            cfg(config, "CHARACTER_BIBLE", True), True
        ),
        "character_consistency": as_bool(
            cfg(config, "CHARACTER_CONSISTENCY", True), True
        ),
        "world_consistency": as_bool(
            cfg(config, "WORLD_CONSISTENCY", True), True
        ),
        "scene_continuity": as_bool(
            cfg(config, "SCENE_CONTINUITY", True), True
        ),
        "negative_prompt": as_bool(
            cfg(config, "NEGATIVE_PROMPT", True), True
        ),
        "avoid_cartoon": as_bool(
            cfg(config, "AVOID_CARTOON_LOOK", True), True
        ),
        "avoid_neon": as_bool(
            cfg(config, "AVOID_NEON", True), True
        ),
        "avoid_glitch": as_bool(
            cfg(config, "AVOID_GLITCH_EFFECTS", True), True
        ),
        "music": as_bool(cfg(config, "MUSIC", True), True),
        "sfx": as_bool(cfg(config, "SFX", True), True),
        "ambient_sound": as_bool(
            cfg(config, "AMBIENT_SOUND", True), True
        ),
        "music_style": clean(cfg(config, "MUSIC_STYLE", "cinematic")),
        "transitions": clean(cfg(config, "TRANSITIONS", "cinematic")),
        "resume_enabled": as_bool(
            cfg(config, "RESUME_ENABLED", True), True
        ),
        "skip_completed": as_bool(
            cfg(config, "SKIP_COMPLETED_SCENES", True), True
        ),
        "save_checkpoint": as_bool(
            cfg(config, "SAVE_CHECKPOINT_AFTER_EACH_SCENE", True), True
        ),
    }


def validate_story_sources(ai_story):
    if ai_story.get("status") != "completed":
        raise RuntimeError(
            "ai_story.json is not completed. Scene generation stopped."
        )

    mode = clean(ai_story.get("format")).lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported AI story format: {ai_story.get('format')!r}"
        )

    parts = ai_story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("ai_story.json has no story parts.")

    flattened = []
    seen = set()

    for part_index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Part {part_index} is not an object.")

        if part.get("part") != part_index:
            raise RuntimeError(
                f"Part numbering error: expected {part_index}, "
                f"found {part.get('part')!r}."
            )

        part_scenes = part.get("scenes")
        if not isinstance(part_scenes, list) or not part_scenes:
            raise RuntimeError(f"Part {part_index} has no scenes.")

        for scene_index, scene in enumerate(part_scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {part_index}, scene {scene_index} is invalid."
                )

            if scene.get("scene") != scene_index:
                raise RuntimeError(
                    f"Scene numbering error in part {part_index}: "
                    f"expected {scene_index}, found {scene.get('scene')!r}."
                )

            key = (part_index, scene_index)
            if key in seen:
                raise RuntimeError(f"Duplicate scene identifier: {key}")
            seen.add(key)

            narration = clean(scene.get("narration"))
            visual = clean(scene.get("visual_prompt"))

            if not narration:
                raise RuntimeError(
                    f"Empty narration in Part {part_index}, "
                    f"Scene {scene_index}."
                )

            if not visual:
                raise RuntimeError(
                    f"Empty visual_prompt in Part {part_index}, "
                    f"Scene {scene_index}. Run AI story generation again."
                )

            flattened.append({
                "part": part_index,
                "scene": scene_index,
                "part_data": part,
                "source": scene,
                "narration": narration,
            })

    declared_total = ai_story.get("total_scenes")
    if declared_total is not None and int(declared_total) != len(flattened):
        raise RuntimeError(
            f"total_scenes mismatch: {declared_total}/{len(flattened)}."
        )

    declared_parts = ai_story.get("total_parts")
    if declared_parts is not None and int(declared_parts) != len(parts):
        raise RuntimeError(
            f"total_parts mismatch: {declared_parts}/{len(parts)}."
        )

    return mode, parts, flattened


def load_character_bible(settings):
    bible = read_json(BIBLE_FILE, required=False)

    if bible is None:
        if settings["character_bible"]:
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but character_bible.json "
                "does not exist."
            )
        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "world": {},
        }

    status = clean(bible.get("status")).lower()
    if status not in {"completed", "disabled"}:
        raise RuntimeError(
            f"Character Bible status is not usable: {status or 'missing'}."
        )

    characters = bible.get("characters", [])
    scene_map = bible.get("scene_character_map", [])
    world = bible.get("world", {})

    if not isinstance(characters, list):
        raise RuntimeError("Character Bible characters must be an array.")
    if not isinstance(scene_map, list):
        raise RuntimeError(
            "Character Bible scene_character_map must be an array."
        )
    if not isinstance(world, dict):
        raise RuntimeError("Character Bible world must be an object.")

    return {
        "status": status,
        "characters": [
            item for item in characters if isinstance(item, dict)
        ],
        "scene_character_map": [
            item for item in scene_map if isinstance(item, dict)
        ],
        "world": world,
    }


def scene_key(part, scene):
    return f"{int(part)}:{int(scene)}"


def normalize_names(values):
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]

    result = []
    seen = set()
    for value in values:
        value = clean(value)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def find_scene_character_map(bible, part_number, scene_number):
    for row in bible.get("scene_character_map", []):
        try:
            row_part = int(row.get("part", -1))
            row_scene = int(row.get("scene", -1))
        except (TypeError, ValueError):
            continue
        if row_part == part_number and row_scene == scene_number:
            values = (
                row.get("characters")
                or row.get("character_ids")
                or row.get("character_names")
                or []
            )
            return normalize_names(values)
    return []


def find_character_records(bible, references):
    if not references:
        return []

    wanted = {item.casefold() for item in references}
    matches = []

    for character in bible.get("characters", []):
        candidates = [
            clean(character.get("id")),
            clean(character.get("name")),
            clean(character.get("character_id")),
        ]
        if any(item and item.casefold() in wanted for item in candidates):
            matches.append(character)

    return matches


def character_summary(records):
    summaries = []
    for item in records:
        summary = {}
        for key in (
            "id",
            "name",
            "age",
            "appearance",
            "face",
            "hair",
            "clothing",
            "distinctive_features",
            "personality",
            "visual_identity",
            "description",
        ):
            value = item.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                summary[key] = value
        if summary:
            summaries.append(summary)
    return summaries


def build_negative_prompt(source, settings):
    values = []

    if settings["negative_prompt"]:
        values.extend([
            "cartoon",
            "anime",
            "comic-book illustration",
            "2D illustration",
            "3D animated character",
            "plastic skin",
            "waxy face",
            "unrealistic anatomy",
            "extra fingers",
            "missing fingers",
            "duplicated people",
            "distorted face",
            "floating objects",
            "unintended text",
            "subtitles",
            "watermark",
            "logo",
        ])

        if settings["avoid_cartoon"]:
            values.extend(["cartoon style", "animated look", "drawn style"])
        if settings["avoid_neon"]:
            values.extend(["unnecessary neon", "artificial neon glow"])
        if settings["avoid_glitch"]:
            values.extend(["glitch effect", "digital distortion"])

    original = clean(source.get("negative_prompt"))
    if original:
        values.append(original)

    unique = []
    seen = set()
    for item in values:
        normalized = item.casefold()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(item)

    return ", ".join(unique)


def build_visual_prompt(
    source,
    settings,
    character_records,
    world,
    previous_scene,
):
    original = clean(source.get("visual_prompt"))
    if not original:
        raise RuntimeError("Source visual_prompt is empty.")

    camera = clean(
        source.get("camera_prompt")
        or source.get("camera")
    )
    lighting = clean(
        source.get("lighting_prompt")
        or source.get("lighting")
    )
    mood = clean(source.get("mood"))
    action = clean(source.get("action"))
    world_context = clean(
        source.get("world_context")
        or source.get("location")
    )

    pieces = [
        original,
        (
            "Visual treatment: photorealistic live-action cinematic "
            f"film still; style {settings['visual_style']}; "
            f"realism {settings['realism']}; quality {settings['quality']}."
        ),
    ]

    if action:
        pieces.append("Physical action: " + action + ".")

    if camera:
        pieces.append("Camera direction: " + camera)
    elif settings["cinematic_camera"]:
        pieces.append(
            "Camera direction: purposeful cinematic framing, "
            "natural perspective and physically plausible camera movement."
        )

    if lighting:
        pieces.append("Lighting direction: " + lighting)
    elif settings["realistic_lighting"]:
        pieces.append(
            "Lighting: physically plausible natural or motivated cinematic "
            "lighting, realistic shadows and skin tones."
        )

    if mood:
        pieces.append("Emotional tone: " + mood)
    else:
        pieces.append("Emotional tone: " + settings["mood"])

    if world_context:
        pieces.append("Location continuity: " + world_context)

    if settings["character_consistency"] and character_records:
        pieces.append(
            "Maintain the exact established character identities, facial "
            "features, age, hairstyle, body type and clothing described here: "
            + json.dumps(
                character_summary(character_records),
                ensure_ascii=False,
            )
        )

    if settings["world_consistency"] and world:
        pieces.append(
            "Maintain established story-world continuity: "
            + json.dumps(world, ensure_ascii=False)
        )

    if settings["scene_continuity"] and previous_scene:
        previous_location = clean(previous_scene.get("world_context"))
        previous_characters = normalize_names(
            previous_scene.get("character_ids", [])
        )
        continuity = []
        if previous_location:
            continuity.append(
                "Continue logically from the previous scene's location: "
                + previous_location
            )
        if previous_characters:
            continuity.append(
                "Keep recurring character identities consistent: "
                + ", ".join(previous_characters)
            )
        if continuity:
            pieces.append("Scene continuity: " + " ".join(continuity))

    if settings["natural_motion"]:
        pieces.append(
            "If motion is depicted, use believable body mechanics, "
            "natural gestures, subtle environmental movement and realistic "
            "interaction with nearby objects. Avoid frozen mannequin poses."
        )

    pieces.append(
        "Do not change the approved story event, character relationships, "
        "time period or location. Do not add unrelated people or objects. "
        "Do not render captions, subtitles, labels or written text."
    )

    return "\n".join(pieces).strip()


def build_scene_record(
    item,
    settings,
    bible,
    previous_scene,
    global_number,
):
    source = item["source"]
    part_number = item["part"]
    scene_number = item["scene"]

    references = normalize_names(
        source.get("characters")
        or source.get("character_ids")
        or source.get("character_names")
    )

    mapped = find_scene_character_map(
        bible,
        part_number,
        scene_number,
    )
    if not references:
        references = mapped

    character_records = find_character_records(bible, references)

    if not references and character_records:
        references = normalize_names([
            record.get("id") or record.get("name")
            for record in character_records
        ])

    world = bible.get("world", {})
    visual_prompt = build_visual_prompt(
        source,
        settings,
        character_records,
        world,
        previous_scene,
    )

    camera_prompt = clean(
        source.get("camera_prompt")
        or source.get("camera")
        or settings["camera_style"]
    )
    lighting_prompt = clean(
        source.get("lighting_prompt")
        or source.get("lighting")
        or settings["lighting"]
    )

    duration = (
        source.get("scene_duration")
        or source.get("duration")
        or item["part_data"].get("scene_duration")
        or "auto"
    )

    sfx = clean(
        source.get("sfx_prompt")
        or source.get("sfx")
    ) if settings["sfx"] else ""

    ambient = clean(
        source.get("ambient_sound")
        or source.get("ambient_prompt")
    ) if settings["ambient_sound"] else ""

    music = clean(
        source.get("music_prompt")
        or source.get("music_direction")
    ) if settings["music"] else ""

    negative = build_negative_prompt(source, settings)

    return {
        "part": part_number,
        "scene": scene_number,
        "scene_id": (
            f"part_{part_number:02d}_scene_{scene_number:02d}"
        ),
        "global_scene_number": global_number,
        "title": clean(source.get("title"))
        or f"Part {part_number} Scene {scene_number}",
        "role": clean(source.get("role")),
        "status": "completed",
        "narration": item["narration"],
        "dialogue": clean(source.get("dialogue")),
        "visual_prompt": visual_prompt,
        "negative_prompt": negative,
        "camera_prompt": camera_prompt,
        "lighting_prompt": lighting_prompt,
        "camera": camera_prompt,
        "lighting": lighting_prompt,
        "mood": clean(source.get("mood")) or settings["mood"],
        "duration": str(duration),
        "scene_duration": str(duration),
        "transition": clean(
            source.get("transition")
            or settings["transitions"]
        ),
        "character_ids": references,
        "characters": references,
        "character_details": character_summary(character_records),
        "world_context": clean(
            source.get("world_context")
            or source.get("location")
        ),
        "sfx_prompt": sfx,
        "ambient_sound": ambient,
        "music_prompt": music,
        "music_direction": music,
        "generation_method": "offline_story_prompt_enrichment",
        "source_scene_number": scene_number,
    }

def scene_is_valid(scene, expected):
    if not isinstance(scene, dict):
        return False

    if scene.get("part") != expected["part"]:
        return False
    if scene.get("scene") != expected["scene"]:
        return False
    if scene.get("status") != "completed":
        return False

    for field in ("narration", "visual_prompt"):
        if not clean(scene.get(field)):
            return False

    if clean(scene.get("narration")) != expected["narration"]:
        return False

    return True


def load_existing_scenes(signature, settings):
    if not settings["resume_enabled"]:
        return {}

    data = read_json(OUTPUT_FILE, required=False)
    if not isinstance(data, dict):
        return {}

    if data.get("generation_signature") != signature:
        log("Existing scenes have a different signature; not reusing them.")
        return {}

    rows = data.get("scenes", [])
    if not isinstance(rows, list):
        return {}

    result = {}
    for scene in rows:
        if not isinstance(scene, dict):
            continue
        try:
            key = scene_key(scene.get("part"), scene.get("scene"))
        except (TypeError, ValueError):
            continue
        result[key] = scene

    return result


def load_checkpoint(signature, settings):
    if not settings["resume_enabled"]:
        return set()

    data = read_json(CHECKPOINT_FILE, required=False)
    if not isinstance(data, dict):
        return set()

    if data.get("generation_signature") != signature:
        return set()

    values = data.get("completed_scenes", [])
    if not isinstance(values, list):
        return set()

    return {value for value in values if isinstance(value, str)}


def save_checkpoint(signature, completed, total, failed_scene=None):
    completed_keys = sorted(completed)
    write_json(
        CHECKPOINT_FILE,
        {
            "version": VERSION,
            "status": (
                "completed"
                if len(completed_keys) == total
                else "in_progress"
            ),
            "generation_signature": signature,
            "total_scenes": total,
            "completed_scenes": completed_keys,
            "completed_count": len(completed_keys),
            "remaining_scenes": max(
                0, total - len(completed_keys)
            ),
            "failed_scene": failed_scene,
            "updated_at": int(time.time()),
        },
    )


def sort_scene_key(scene):
    return (
        int(scene.get("part", 0)),
        int(scene.get("scene", 0)),
    )


def validate_final_scenes(scenes, expected_items):
    expected_keys = {
        scene_key(item["part"], item["scene"])
        for item in expected_items
    }

    actual_keys = set()

    for scene in scenes:
        if not isinstance(scene, dict):
            raise RuntimeError("Output contains an invalid scene object.")

        key = scene_key(scene.get("part"), scene.get("scene"))

        if key in actual_keys:
            raise RuntimeError(f"Duplicate scene in final output: {key}")

        if key not in expected_keys:
            raise RuntimeError(f"Unexpected scene in final output: {key}")

        actual_keys.add(key)

        if scene.get("status") != "completed":
            raise RuntimeError(f"Scene is incomplete: {key}")

        if not clean(scene.get("narration")):
            raise RuntimeError(f"Scene narration is empty: {key}")

        if not clean(scene.get("visual_prompt")):
            raise RuntimeError(f"Scene visual prompt is empty: {key}")

    missing = sorted(expected_keys - actual_keys)
    if missing:
        raise RuntimeError(
            "Missing scenes: " + ", ".join(missing[:30])
        )

    if len(scenes) != len(expected_items):
        raise RuntimeError(
            f"Scene count mismatch: {len(scenes)}/{len(expected_items)}"
        )


def build_output_metadata(
    ai_story,
    settings,
    bible,
    signature,
    scenes,
    status,
):
    return {
        "version": VERSION,
        "status": status,
        "format": settings["format"],
        "title": clean(ai_story.get("title")),
        "topic": clean(ai_story.get("topic")),
        "model": clean(ai_story.get("model")),
        "generation_method": "offline_story_prompt_enrichment",
        "generation_signature": signature,
        "source_story_file": str(AI_STORY_FILE),
        "character_bible_status": bible.get("status", "unknown"),
        "total_parts": len(ai_story.get("parts", [])),
        "total_scenes": len(scenes),
        "completed_scenes": sum(
            1 for item in scenes
            if item.get("status") == "completed"
        ),
        "input_config": settings,
        "scenes": scenes,
    }


def main():
    log("=" * 60)
    log("KATHA LOK AI — PHOTO MOTION SCENE BUILDER")
    log("=" * 60)

    config = load_input_config(str(INPUT_FILE))
    settings = build_settings(config)

    ai_story = read_json(AI_STORY_FILE, required=True)
    mode, parts, expected_items = validate_story_sources(ai_story)

    if mode != settings["format"]:
        raise RuntimeError(
            "FORMAT mismatch between Input/topic.txt and ai_story.json: "
            f"{settings['format']} != {mode}"
        )

    bible = load_character_bible(settings)

    topic = clean(get_topic(config)) or clean(ai_story.get("topic"))
    input_story_text = clean(get_story_text(config))
    story_text = clean(ai_story.get("story_text"))

    # Input story text is included in the signature when supplied so that
    # a changed source cannot accidentally reuse old scene prompts.
    signature = digest({
        "version": VERSION,
        "scene_prompt_version": SCENE_PROMPT_VERSION,
        "format": mode,
        "topic": topic,
        "input_story_text": input_story_text,
        "ai_story": ai_story,
        "character_bible": bible,
        "settings": settings,
    })

    total_parts = len(parts)
    total_scenes = len(expected_items)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing_scenes(signature, settings)
    checkpoint = load_checkpoint(signature, settings)

    expected_by_key = {
        scene_key(item["part"], item["scene"]): item
        for item in expected_items
    }

    completed = {}

    # Reuse only scenes that match the exact current source and settings.
    for key, item in expected_by_key.items():
        cached = existing.get(key)
        if not scene_is_valid(cached, item):
            continue

        if (
            settings["skip_completed"]
            and (key in checkpoint or key in existing)
        ):
            completed[key] = cached

    log(f"Format                 : {mode}")
    log(f"Parts                  : {total_parts}")
    log(f"Scenes                 : {total_scenes}")
    log(f"Visual style            : {settings['visual_style']}")
    log(f"Character Bible         : {bible['status']}")
    log(f"Reusable completed      : {len(completed)}/{total_scenes}")
    log("Scene generation mode   : OFFLINE")
    log("Additional Gemini calls : 0")
    log("=" * 60)

    ordered_completed = sorted(
        completed.values(),
        key=sort_scene_key,
    )

    output = build_output_metadata(
        ai_story,
        settings,
        bible,
        signature,
        ordered_completed,
        "in_progress",
    )

    write_json(OUTPUT_FILE, output)
    save_checkpoint(
        signature,
        completed.keys(),
        total_scenes,
    )

    for index, item in enumerate(expected_items, 1):
        key = scene_key(item["part"], item["scene"])

        if key in completed:
            log(
                f"[{index}/{total_scenes}] "
                f"Part {item['part']} Scene {item['scene']} "
                "reused; no API call."
            )
            continue

        previous_candidates = [
            scene
            for scene in completed.values()
            if sort_scene_key(scene) < (
                item["part"], item["scene"]
            )
        ]
        previous_candidates.sort(key=sort_scene_key)
        previous_scene = (
            previous_candidates[-1]
            if previous_candidates
            else None
        )

        scene = build_scene_record(
            item=item,
            settings=settings,
            bible=bible,
            previous_scene=previous_scene,
            global_number=index,
        )

        # Validate before saving so an invalid scene never enters checkpoint.
        if scene["narration"] != item["narration"]:
            raise RuntimeError(
                f"Narration changed in Part {item['part']} "
                f"Scene {item['scene']}."
            )

        if not scene["visual_prompt"]:
            raise RuntimeError(
                f"Visual prompt is empty in Part {item['part']} "
                f"Scene {item['scene']}."
            )

        completed[key] = scene

        ordered_completed = sorted(
            completed.values(),
            key=sort_scene_key,
        )

        output = build_output_metadata(
            ai_story,
            settings,
            bible,
            signature,
            ordered_completed,
            "in_progress",
        )

        # Persist the completed scene before updating its checkpoint.
        write_json(OUTPUT_FILE, output)

        if settings["save_checkpoint"]:
            save_checkpoint(
                signature,
                completed.keys(),
                total_scenes,
            )

        log(
            f"[{index}/{total_scenes}] Saved "
            f"Part {item['part']} Scene {item['scene']}."
        )

    final_scenes = sorted(
        completed.values(),
        key=sort_scene_key,
    )

    validate_final_scenes(final_scenes, expected_items)

    final_output = build_output_metadata(
        ai_story,
        settings,
        bible,
        signature,
        final_scenes,
        "completed",
    )
    final_output["completed_at"] = int(time.time())

    write_json(OUTPUT_FILE, final_output)

    save_checkpoint(
        signature,
        completed.keys(),
        total_scenes,
    )

    log("=" * 60)
    log("SCENE GENERATION COMPLETED")
    log(f"Parts                   : {total_parts}")
    log(f"Scenes                  : {len(final_scenes)}/{total_scenes}")
    log("Additional Gemini calls : 0")
    log(f"Output                  : {OUTPUT_FILE}")
    log(f"Checkpoint              : {CHECKPOINT_FILE}")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Scene generation interrupted. Completed checkpoints preserved.")
        raise SystemExit(130)
    except Exception as exc:
        log(f"SCENE GENERATION FAILED: {type(exc).__name__}: {exc}")
        raise SystemExit(1)