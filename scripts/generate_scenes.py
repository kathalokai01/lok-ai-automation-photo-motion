#!/usr/bin/env python3
"""Build main, hook, suspense and resolution visual scene records."""

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
AI_STORY_FILE = ROOT / "output/story/ai_story.json"
BIBLE_FILE = ROOT / "output/story/character_bible.json"

OUTPUT_DIR = ROOT / "output/scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output/checkpoints/scenes_progress.json"

VERSION = "photo-motion-scenes-v6-separated-extra-scenes"
SCENE_PROMPT_VERSION = 6

EXTRA_ROLES = ("hook", "suspense", "resolution")


def log(message=""):
    print(message, flush=True)


def clean(value):
    if value is None or isinstance(value, (dict, list)):
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
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def digest(data):
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
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
        "natural_motion": as_bool(cfg(config, "NATURAL_MOTION", True), True),
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
        "avoid_neon": as_bool(cfg(config, "AVOID_NEON", True), True),
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


def validate_extra_source(record, role, part_number):
    if not isinstance(record, dict):
        raise RuntimeError(
            f"Part {part_number} {role} record is not an object."
        )
    if clean(record.get("role")).lower() not in {"", role}:
        raise RuntimeError(
            f"Part {part_number}: {role} record has an incorrect role."
        )
    if not clean(record.get("scene_id")):
        raise RuntimeError(
            f"Part {part_number}: {role} record has no scene_id."
        )
    if not clean(record.get("narration")):
        raise RuntimeError(
            f"Part {part_number}: {role} narration is empty."
        )
    if not clean(record.get("visual_prompt")):
        raise RuntimeError(
            f"Part {part_number}: {role} visual_prompt is empty."
        )


def validate_story_sources(ai_story):
    if ai_story.get("status") != "completed":
        raise RuntimeError(
            "ai_story.json is not completed. Scene generation stopped."
        )

    mode = clean(ai_story.get("format")).lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported AI story format: {mode!r}")

    parts = ai_story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("ai_story.json has no story parts.")

    expected_items = []
    seen_ids = set()
    main_scene_total = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(
                f"Part numbering error at part {part_number}."
            )

        main_scenes = part.get("scenes")
        if not isinstance(main_scenes, list) or not main_scenes:
            raise RuntimeError(f"Part {part_number} has no main scenes.")

        for scene_number, source in enumerate(main_scenes, 1):
            if not isinstance(source, dict):
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number} is invalid."
                )
            if source.get("scene") != scene_number:
                raise RuntimeError(
                    f"Main scene numbering error in part {part_number}: "
                    f"expected {scene_number}, got {source.get('scene')}."
                )

            scene_id = (
                clean(source.get("scene_id"))
                or f"part_{part_number:02d}_scene_{scene_number:02d}"
            )
            if scene_id in seen_ids:
                raise RuntimeError(f"Duplicate scene_id: {scene_id}")
            seen_ids.add(scene_id)

            narration = clean(source.get("narration"))
            visual = clean(source.get("visual_prompt"))
            if not narration or not visual:
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number} is missing "
                    "narration or visual_prompt."
                )

            expected_items.append({
                "part": part_number,
                "scene": scene_number,
                "scene_id": scene_id,
                "role": "main",
                "part_data": part,
                "source": source,
                "narration": narration,
                "global_scene_number": len(expected_items) + 1,
            })
            main_scene_total += 1

        is_final = part_number == len(parts)
        extra_definitions = [
            ("hook_scene", "hook", True),
            ("suspense_scene", "suspense", not is_final),
            ("resolution_scene", "resolution", is_final),
        ]

        for field, role, should_exist in extra_definitions:
            record = part.get(field)
            if not should_exist:
                if record is not None:
                    raise RuntimeError(
                        f"Unexpected {role} record in part {part_number}."
                    )
                continue

            if record is None:
                continue

            validate_extra_source(record, role, part_number)
            scene_id = clean(record.get("scene_id"))
            if scene_id in seen_ids:
                raise RuntimeError(f"Duplicate scene_id: {scene_id}")
            seen_ids.add(scene_id)

            expected_items.append({
                "part": part_number,
                "scene": None,
                "scene_id": scene_id,
                "role": role,
                "part_data": part,
                "source": record,
                "narration": clean(record.get("narration")),
                "global_scene_number": None,
            })

    declared_total = ai_story.get("total_scenes")
    if declared_total is not None and int(declared_total) != main_scene_total:
        raise RuntimeError(
            f"Main total_scenes mismatch: {declared_total}/{main_scene_total}."
        )

    declared_parts = ai_story.get("total_parts")
    if declared_parts is not None and int(declared_parts) != len(parts):
        raise RuntimeError(
            f"total_parts mismatch: {declared_parts}/{len(parts)}."
        )

    return mode, parts, expected_items, main_scene_total


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
        raise RuntimeError("Character Bible scene_character_map must be an array.")
    if not isinstance(world, dict):
        raise RuntimeError("Character Bible world must be an object.")

    return {
        "status": status,
        "characters": [x for x in characters if isinstance(x, dict)],
        "scene_character_map": [x for x in scene_map if isinstance(x, dict)],
        "world": world,
    }


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
    if scene_number is None:
        return []

    for row in bible.get("scene_character_map", []):
        try:
            row_part = int(row.get("part", -1))
            row_scene = int(row.get("scene", -1))
        except (TypeError, ValueError):
            continue
        if row_part == part_number and row_scene == scene_number:
            return normalize_names(
                row.get("characters")
                or row.get("character_ids")
                or row.get("character_names")
                or []
            )
    return []


def find_character_records(bible, references):
    if not references:
        return []
    wanted = {value.casefold() for value in references}
    matches = []

    for character in bible.get("characters", []):
        candidates = (
            clean(character.get("id")),
            clean(character.get("name")),
            clean(character.get("character_id")),
        )
        if any(value and value.casefold() in wanted for value in candidates):
            matches.append(character)
    return matches


def character_summary(records):
    summaries = []
    fields = (
        "id", "name", "age", "appearance", "face", "hair", "clothing",
        "distinctive_features", "personality", "visual_identity", "description",
    )
    for item in records:
        summary = {
            key: item[key]
            for key in fields
            if key in item
            and isinstance(item[key], (str, int, float))
            and str(item[key]).strip()
        }
        if summary:
            summaries.append(summary)
    return summaries


def build_negative_prompt(source, settings):
    values = []
    if settings["negative_prompt"]:
        values.extend([
            "cartoon", "anime", "comic-book illustration",
            "2D illustration", "3D animated character", "plastic skin",
            "waxy face", "unrealistic anatomy", "extra fingers",
            "missing fingers", "duplicated people", "distorted face",
            "floating objects", "unintended text", "subtitles",
            "watermark", "logo",
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
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return ", ".join(unique)


def build_visual_prompt(
    source,
    settings,
    character_records,
    world,
    previous_scene,
    role,
):
    original = clean(source.get("visual_prompt"))
    if not original:
        raise RuntimeError(f"{role} scene has an empty visual prompt.")

    pieces = [
        original,
        (
            "Photorealistic live-action cinematic film still. "
            f"Visual style: {settings['visual_style']}; "
            f"realism: {settings['realism']}; quality: {settings['quality']}."
        ),
    ]

    action = clean(source.get("action"))
    if action:
        pieces.append("Physical action: " + action)

    camera = clean(source.get("camera_prompt") or source.get("camera"))
    if camera:
        pieces.append("Camera direction: " + camera)
    elif settings["cinematic_camera"]:
        pieces.append(
            "Use purposeful cinematic framing, natural perspective and "
            "physically plausible camera movement."
        )

    lighting = clean(source.get("lighting_prompt") or source.get("lighting"))
    if lighting:
        pieces.append("Lighting direction: " + lighting)
    elif settings["realistic_lighting"]:
        pieces.append(
            "Use physically plausible natural or motivated cinematic light, "
            "realistic shadows and natural skin tones."
        )

    mood = clean(source.get("mood")) or settings["mood"]
    pieces.append("Emotional tone: " + mood)

    world_context = clean(
        source.get("world_context") or source.get("location")
    )
    if world_context:
        pieces.append("Story location: " + world_context)

    if settings["character_consistency"] and character_records:
        pieces.append(
            "Preserve the exact established character identities, face, age, "
            "hair, body type and clothing: "
            + json.dumps(character_summary(character_records), ensure_ascii=False)
        )

    if settings["world_consistency"] and world:
        pieces.append(
            "Preserve established story-world continuity: "
            + json.dumps(world, ensure_ascii=False)
        )

    if settings["scene_continuity"] and previous_scene:
        previous_location = clean(previous_scene.get("world_context"))
        previous_characters = normalize_names(
            previous_scene.get("character_ids", [])
        )
        continuity = []
        if previous_location:
            continuity.append("Previous location: " + previous_location)
        if previous_characters:
            continuity.append(
                "Maintain recurring character identities: "
                + ", ".join(previous_characters)
            )
        if continuity:
            pieces.append("Continuity: " + " ".join(continuity))

    if settings["natural_motion"]:
        pieces.append(
            "Depict believable body mechanics, natural gestures, subtle "
            "environmental movement and realistic interaction with objects."
        )

    pieces.append(
        "Show only the approved story event. Do not add unrelated people, "
        "objects or events. Do not render captions, subtitles, labels or text."
    )
    return "\n".join(pieces).strip()

def build_scene_record(
    item,
    settings,
    bible,
    previous_scene,
):
    source = item["source"]
    part_number = item["part"]
    scene_number = item["scene"]
    role = item["role"]

    references = normalize_names(
        source.get("characters")
        or source.get("character_ids")
        or source.get("character_names")
    )

    mapped = find_scene_character_map(
        bible, part_number, scene_number
    )
    if not references:
        references = mapped

    character_records = find_character_records(bible, references)
    world = bible.get("world", {})

    visual_prompt = build_visual_prompt(
        source=source,
        settings=settings,
        character_records=character_records,
        world=world,
        previous_scene=previous_scene,
        role=role,
    )

    if not references and character_records:
        references = normalize_names([
            item.get("id") or item.get("name")
            for item in character_records
        ])

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

    sfx = (
        clean(source.get("sfx_prompt") or source.get("sfx"))
        if settings["sfx"] else ""
    )
    ambient = (
        clean(source.get("ambient_sound") or source.get("ambient_prompt"))
        if settings["ambient_sound"] else ""
    )
    music = (
        clean(source.get("music_prompt") or source.get("music_direction"))
        if settings["music"] else ""
    )

    scene_id = item["scene_id"]
    scene_title = clean(source.get("title"))
    if not scene_title:
        scene_title = (
            f"Part {part_number} {role.title()}"
            if role != "main"
            else f"Part {part_number} Scene {scene_number}"
        )

    return {
        "part": part_number,
        "scene": scene_number,
        "local_scene": scene_number,
        "scene_id": scene_id,
        "id": scene_id,
        "role": role,
        "scene_type": role,
        "is_extra_scene": role != "main",
        "global_scene_number": item["global_scene_number"],
        "title": scene_title,
        "status": "completed",
        "narration": item["narration"],
        "dialogue": clean(source.get("dialogue")),
        "visual_prompt": visual_prompt,
        "negative_prompt": build_negative_prompt(source, settings),
        "camera_prompt": camera_prompt,
        "lighting_prompt": lighting_prompt,
        "camera": camera_prompt,
        "lighting": lighting_prompt,
        "mood": clean(source.get("mood")) or settings["mood"],
        "duration": str(duration),
        "scene_duration": str(duration),
        "transition": clean(
            source.get("transition") or settings["transitions"]
        ),
        "character_ids": references,
        "characters": references,
        "character_details": character_summary(character_records),
        "world_context": clean(
            source.get("world_context") or source.get("location")
        ),
        "sfx_prompt": sfx,
        "ambient_sound": ambient,
        "music_prompt": music,
        "music_direction": music,
        "generation_method": "offline_story_prompt_enrichment",
        "source_scene_number": scene_number,
        "source_scene_id": scene_id,
        "source_role": role,
        "source_scene_signature": digest(source),
    }


def scene_order_key(scene):
    role_order = {
        "hook": 0,
        "main": 1,
        "suspense": 2,
        "resolution": 3,
    }
    return (
        int(scene.get("part", 0)),
        role_order.get(clean(scene.get("role")).lower(), 9),
        int(scene.get("scene") or 0),
        clean(scene.get("scene_id")),
    )


def scene_is_valid(scene, expected):
    if not isinstance(scene, dict):
        return False

    for key in ("part", "scene_id", "role"):
        if scene.get(key) != expected.get(key):
            return False

    if scene.get("status") != "completed":
        return False

    if clean(scene.get("narration")) != expected["narration"]:
        return False

    if scene.get("source_scene_signature") != digest(expected["source"]):
        return False

    for field in (
        "narration",
        "visual_prompt",
        "camera_prompt",
        "lighting_prompt",
    ):
        if not clean(scene.get(field)):
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
        scene_id = clean(scene.get("scene_id"))
        if scene_id:
            result[scene_id] = scene
    return result


def load_checkpoint(signature, settings):
    if not settings["resume_enabled"]:
        return set()

    data = read_json(CHECKPOINT_FILE, required=False)
    if not isinstance(data, dict):
        return set()

    if data.get("generation_signature") != signature:
        return set()

    values = data.get("completed_scene_ids", [])
    if not isinstance(values, list):
        return set()

    return {
        clean(value)
        for value in values
        if clean(value)
    }


def save_checkpoint(signature, completed_ids, total):
    completed = sorted(set(completed_ids))
    write_json(
        CHECKPOINT_FILE,
        {
            "version": VERSION,
            "status": (
                "completed" if len(completed) == total else "in_progress"
            ),
            "generation_signature": signature,
            "total_scenes": total,
            "completed_scene_ids": completed,
            "completed_count": len(completed),
            "remaining_scenes": max(0, total - len(completed)),
            "updated_at": int(time.time()),
        },
    )


def validate_final_scenes(scenes, expected_items):
    expected_by_id = {
        item["scene_id"]: item
        for item in expected_items
    }
    actual_ids = set()

    for scene in scenes:
        if not isinstance(scene, dict):
            raise RuntimeError("Output contains an invalid scene object.")

        scene_id = clean(scene.get("scene_id"))
        if not scene_id:
            raise RuntimeError("Output scene is missing scene_id.")
        if scene_id in actual_ids:
            raise RuntimeError(f"Duplicate scene_id in output: {scene_id}")
        if scene_id not in expected_by_id:
            raise RuntimeError(f"Unexpected scene in output: {scene_id}")

        actual_ids.add(scene_id)
        expected = expected_by_id[scene_id]

        if not scene_is_valid(scene, expected):
            raise RuntimeError(f"Scene validation failed: {scene_id}")

    missing = sorted(set(expected_by_id) - actual_ids)
    if missing:
        raise RuntimeError(
            "Missing scene records: " + ", ".join(missing[:30])
        )

    if len(scenes) != len(expected_items):
        raise RuntimeError(
            f"Scene count mismatch: {len(scenes)}/{len(expected_items)}"
        )

    ordered = sorted(scenes, key=scene_order_key)
    if scenes != ordered:
        raise RuntimeError("Scene records are not in required part/role order.")


def build_output_metadata(
    ai_story,
    settings,
    bible,
    signature,
    scenes,
    status,
    main_scene_total,
):
    parts = ai_story.get("parts", [])
    role_counts = {
        role: sum(1 for item in scenes if item.get("role") == role)
        for role in ("hook", "main", "suspense", "resolution")
    }

    return {
        "version": VERSION,
        "scene_prompt_version": SCENE_PROMPT_VERSION,
        "status": status,
        "format": settings["format"],
        "title": clean(ai_story.get("title")),
        "topic": clean(ai_story.get("topic")),
        "model": clean(ai_story.get("model")),
        "generation_method": "offline_story_prompt_enrichment",
        "generation_signature": signature,
        "source_story_file": str(AI_STORY_FILE),
        "character_bible_status": bible.get("status", "unknown"),
        "total_parts": len(parts),
        "total_scenes": len(scenes),
        "total_main_scenes": main_scene_total,
        "total_extra_scenes": len(scenes) - main_scene_total,
        "scene_role_counts": role_counts,
        "completed_scenes": sum(
            1 for item in scenes if item.get("status") == "completed"
        ),
        "scene_order": "part_then_hook_then_main_then_suspense_or_resolution",
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
    mode, parts, expected_items, main_scene_total = (
        validate_story_sources(ai_story)
    )

    if mode != settings["format"]:
        raise RuntimeError(
            "FORMAT mismatch between Input/topic.txt and ai_story.json: "
            f"{settings['format']} != {mode}"
        )

    bible = load_character_bible(settings)
    topic = clean(get_topic(config)) or clean(ai_story.get("topic"))
    input_story_text = clean(get_story_text(config))
    story_text = clean(ai_story.get("story_text"))

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
    total_records = len(expected_items)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing_scenes(signature, settings)
    checkpoint = load_checkpoint(signature, settings)

    expected_by_id = {
        item["scene_id"]: item
        for item in expected_items
    }

    completed = {}
    for scene_id, item in expected_by_id.items():
        cached = existing.get(scene_id)
        if not scene_is_valid(cached, item):
            continue

        if (
            settings["skip_completed"]
            and (scene_id in checkpoint or scene_id in existing)
        ):
            completed[scene_id] = cached

    log(f"Format                  : {mode}")
    log(f"Parts                   : {total_parts}")
    log(f"Main scenes             : {main_scene_total}")
    log(f"Extra scenes            : {total_records - main_scene_total}")
    log(f"All scene records       : {total_records}")
    log(f"Character Bible         : {bible['status']}")
    log(f"Reusable completed      : {len(completed)}/{total_records}")
    log("Scene generation mode   : OFFLINE")
    log("Additional Gemini calls : 0")
    log("=" * 60)

    for index, item in enumerate(expected_items, 1):
        scene_id = item["scene_id"]
        if scene_id in completed:
            log(
                f"[{index}/{total_records}] {scene_id} reused; no API call."
            )
            continue

        earlier = [
            scene for scene in completed.values()
            if scene_order_key(scene) < (
                item["part"],
                {
                    "hook": 0,
                    "main": 1,
                    "suspense": 2,
                    "resolution": 3,
                }.get(item["role"], 9),
                int(item["scene"] or 0),
                scene_id,
            )
        ]
        earlier.sort(key=scene_order_key)
        previous_scene = earlier[-1] if earlier else None

        scene = build_scene_record(
            item=item,
            settings=settings,
            bible=bible,
            previous_scene=previous_scene,
        )

        if scene["narration"] != item["narration"]:
            raise RuntimeError(
                f"Narration changed unexpectedly: {scene_id}"
            )
        if not scene["visual_prompt"]:
            raise RuntimeError(f"Empty visual prompt: {scene_id}")

        completed[scene_id] = scene
        ordered_completed = sorted(
            completed.values(),
            key=scene_order_key,
        )

        output = build_output_metadata(
            ai_story,
            settings,
            bible,
            signature,
            ordered_completed,
            "in_progress",
            main_scene_total,
        )
        write_json(OUTPUT_FILE, output)

        if settings["save_checkpoint"]:
            save_checkpoint(
                signature,
                completed.keys(),
                total_records,
            )

        log(
            f"[{index}/{total_records}] Saved {scene_id} "
            f"({item['role']})."
        )

    final_scenes = sorted(
        completed.values(),
        key=scene_order_key,
    )

    validate_final_scenes(final_scenes, expected_items)

    final_output = build_output_metadata(
        ai_story,
        settings,
        bible,
        signature,
        final_scenes,
        "completed",
        main_scene_total,
    )
    final_output["completed_at"] = int(time.time())
    write_json(OUTPUT_FILE, final_output)

    save_checkpoint(
        signature,
        completed.keys(),
        total_records,
    )

    log("=" * 60)
    log("SCENE GENERATION COMPLETED")
    log(f"Parts                   : {total_parts}")
    log(f"Main scenes             : {main_scene_total}")
    log(f"Extra scenes            : {total_records - main_scene_total}")
    log(f"All scene records       : {len(final_scenes)}/{total_records}")
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