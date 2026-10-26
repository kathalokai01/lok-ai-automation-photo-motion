
#!/usr/bin/env python3
"""Katha Lok AI scene preparation with schema validation and safe checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    normalize_format,
    get_parts,
    get_scenes,
    get_topic,
    get_story_text,
)

CONFIG_FILE = ROOT / "Input" / "topic.txt"
STORY_DIR = ROOT / "output" / "story"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
SOURCE_STORY_FILE = STORY_DIR / "story.json"
PLAN_FILE = STORY_DIR / "story_plan.json"
BIBLE_FILE = STORY_DIR / "character_bible.json"

OUTPUT_DIR = ROOT / "output" / "scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"

VERSION = "scene-preparation-v8-validated-checkpoint"
MAPPING_VERSION = 8


def log(*items):
    print(*items, flush=True)


def clean(value):
    if isinstance(value, (dict, list, tuple)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def digest(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, required=False):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file is missing: {path}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        log(f"WARNING: Optional JSON unavailable: {path.name}: {exc}")
        return None

    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected a JSON object in {path}")
        return None
    return data


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def text_value(item, *keys, default=""):
    if not isinstance(item, dict):
        return default
    for key in keys:
        value = clean(item.get(key))
        if value:
            return value
    return default


def bool_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"true", "1", "yes", "on", "enabled"}:
        return True
    if value in {"false", "0", "no", "off", "disabled"}:
        return False
    return default


def positive_int(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer") from exc
    if number < 1:
        raise RuntimeError(f"{label} must be greater than zero")
    return number


def selected_story():
    errors = []

    for path, label in (
        (AI_STORY_FILE, "ai_story"),
        (SOURCE_STORY_FILE, "story"),
    ):
        data = read_json(path)
        if not isinstance(data, dict):
            errors.append(f"{path.name} missing or invalid")
            continue
        if data.get("status") != "completed":
            errors.append(f"{path.name} is not completed")
            continue
        if not isinstance(data.get("parts"), list) or not data["parts"]:
            errors.append(f"{path.name} has no parts")
            continue

        log("Story source:", path.relative_to(ROOT).as_posix())
        return data, label, path

    raise RuntimeError("No completed story source: " + "; ".join(errors))


def load_character_bible():
    data = read_json(BIBLE_FILE)
    if not isinstance(data, dict):
        return {}
    if data.get("status") not in {"completed", "disabled"}:
        return {}
    return data


def validate_layout(story, parts_count, scenes_per_part, label):
    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        actual = len(parts) if isinstance(parts, list) else 0
        raise RuntimeError(
            f"{label}: expected {parts_count} parts, found {actual}"
        )

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"{label}: invalid part {p}")
        try:
            declared_part = int(part.get("part", p))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{label}: invalid part number {p}") from exc
        if declared_part != p:
            raise RuntimeError(f"{label}: part ordering mismatch at {p}")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            actual = len(scenes) if isinstance(scenes, list) else 0
            raise RuntimeError(
                f"{label}: part {p} has {actual} scenes; "
                f"expected {scenes_per_part}"
            )

        for s, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(f"{label}: invalid scene {p}:{s}")
            try:
                declared_scene = int(scene.get("scene", s))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"{label}: invalid scene number {p}:{s}") from exc
            if declared_scene != s:
                raise RuntimeError(f"{label}: scene order mismatch at {p}:{s}")

            scene_id = clean(scene.get("scene_id"))
            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if scene_id and scene_id != expected_id:
                raise RuntimeError(
                    f"{label}: scene ID mismatch; expected {expected_id}, "
                    f"found {scene_id}"
                )


def load_plan(format_type, parts_count, scenes_per_part):
    plan = read_json(PLAN_FILE)
    if plan is None:
        log("WARNING: story_plan.json unavailable; using source story fields")
        return {}

    if plan.get("status") != "planned":
        raise RuntimeError("story_plan.json status must be planned")
    if clean(plan.get("format")).lower() != format_type:
        raise RuntimeError("Story plan FORMAT does not match Input/topic.txt")

    try:
        plan_parts = int(plan.get("parts_count", -1))
        plan_scenes = int(plan.get("scenes_per_part", -1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Story plan counts are invalid") from exc

    if plan_parts != parts_count:
        raise RuntimeError("Story plan PARTS does not match input")
    if plan_scenes != scenes_per_part:
        raise RuntimeError("Story plan SCENES does not match input")

    validate_layout(plan, parts_count, scenes_per_part, "Story plan")

    expected_total = parts_count * scenes_per_part
    try:
        declared_total = int(plan.get("total_scenes", expected_total))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Story plan total_scenes is invalid") from exc
    if declared_total != expected_total:
        raise RuntimeError("Story plan total_scenes does not match input")

    result = {}
    for part in plan["parts"]:
        p = int(part["part"])
        for scene in part["scenes"]:
            s = int(scene["scene"])
            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if clean(scene.get("scene_id")) != expected_id:
                raise RuntimeError(f"Story plan scene ID mismatch: {expected_id}")
            key = (p, s)
            if key in result:
                raise RuntimeError(f"Duplicate story-plan scene: {expected_id}")
            result[key] = scene

    if len(result) != expected_total:
        raise RuntimeError("Story plan has missing or duplicate scenes")

    log("Story plan integrated:", len(result), "scenes")
    return result


def normalize_scene(
    source,
    plan_scene,
    part_number,
    scene_number,
    global_number,
    settings,
    bible,
):
    if not isinstance(source, dict):
        raise RuntimeError(f"Invalid source scene {part_number}:{scene_number}")

    plan_scene = plan_scene if isinstance(plan_scene, dict) else {}

    narration = text_value(source, "narration", "text", "dialogue")
    visual = text_value(
        source,
        "visual_prompt",
        "visual",
        "image_prompt",
        "scene_prompt",
        "visual_description",
        "description",
    )

    scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

    if not narration:
        raise RuntimeError(f"Missing narration: {scene_id}")
    if len(visual) < 20:
        raise RuntimeError(f"Visual prompt missing or too short: {scene_id}")

    try:
        duration = float(
            source.get("duration_seconds", source.get("duration", 5))
        )
    except (TypeError, ValueError):
        duration = 5.0
    if not 0.5 <= duration <= 3600:
        duration = 5.0

    negative = text_value(
        source,
        "negative_prompt",
        "avoid_prompt",
        default=(
            "cartoon, anime, illustration, distorted anatomy, watermark, "
            "inconsistent character identity, duplicate limbs, unintended text"
        ),
    )
    camera = text_value(
        source,
        "camera_prompt",
        "camera",
        "camera_direction",
        default="Natural cinematic framing appropriate to the scene",
    )
    lighting = text_value(
        source,
        "lighting_prompt",
        "lighting",
        "lighting_direction",
        default=(
            "Physically plausible cinematic lighting, natural skin tones, "
            "visible shadow detail, no excessive contrast"
        ),
    )
    mood = text_value(
        source, "mood", "emotion", "emotional_tone", default="cinematic"
    )
    sfx = text_value(source, "sfx_prompt", "sfx", "sound_effects")
    ambient = text_value(
        source, "ambient_sound", "ambient_prompt", "ambience"
    )
    music = text_value(
        source, "music_prompt", "music_direction", "music"
    )
    suspense = text_value(source, "suspense_prompt", "suspense")
    title = text_value(
        source, "title", "scene_title", default=f"Scene {global_number}"
    )
    source_role = text_value(source, "role", "purpose", "scene_purpose")
    plan_role = text_value(plan_scene, "role")
    role = plan_role or source_role

    world_context = text_value(
        source, "world_context", "location", "setting"
    )
    dialogue = text_value(source, "dialogue", "spoken_dialogue")

    if not bool_value(settings.get("SFX", True), True):
        sfx = ""
    if not bool_value(settings.get("MUSIC", True), True):
        music = ""
    if not bool_value(settings.get("AMBIENT_SOUND", True), True):
        ambient = ""

    final_part = positive_int(settings["PARTS"], "PARTS")
    last_scene = positive_int(settings["SCENES"], "SCENES")
    part_hook = bool_value(settings.get("PART_HOOK", True), True)
    part_suspense = bool_value(settings.get("PART_SUSPENSE", True), True)
    final_resolution = bool_value(
        settings.get("FINAL_RESOLUTION", True), True
    )

    is_final_scene = (
        part_number == final_part and scene_number == last_scene
    )
    resolves_story = is_final_scene and final_resolution

    plan_purpose = text_value(plan_scene, "purpose", "beat_description")
    story_anchor = text_value(plan_scene, "story_anchor")
    plan_beat = text_value(plan_scene, "beat")

    if not suspense and role in {"part_ending", "part_hook", "finale"}:
        suspense = plan_purpose or plan_beat

    continuity = (
        "Preserve established character identity, facial features, age range, "
        "gender presentation, hair, clothing, body type and carried objects. "
        "Maintain location, time order and causal continuity. Do not invent "
        "unsupported identity changes. Follow the character bible when present."
    )

    if bible:
        continuity += " A character bible is available as a continuity reference."

    row = {
        "part": part_number,
        "scene": scene_number,
        "local_scene": scene_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": scene_id,
        "source_scene_id": text_value(source, "scene_id", default=scene_id),
        "global_scene_number": global_number,
        "mapping_version": MAPPING_VERSION,
        "title": title,
        "role": role,
        "narration": narration,
        "dialogue": dialogue,
        "visual_prompt": visual,
        "negative_prompt": negative,
        "duration": duration,
        "transition": text_value(
            source,
            "transition",
            "transition_prompt",
            default=str(settings.get("TRANSITIONS", "cinematic")),
        ),
        "camera": camera,
        "camera_prompt": camera,
        "lighting": lighting,
        "lighting_prompt": lighting,
        "mood": mood,
        "sfx": sfx,
        "sfx_prompt": sfx,
        "ambient_sound": ambient,
        "music_direction": music,
        "music_prompt": music,
        "world_context": world_context,
        "suspense_prompt": suspense,
        "opening_hook_required": (
            scene_number == 1 and (part_number == 1 or part_hook)
        ),
        "suspense_required": (
            scene_number == last_scene
            and part_suspense
            and not resolves_story
        ),
        "final_resolution_required": resolves_story,
        "character_bible_available": bool(bible),
        "continuity_prompt": continuity,
        "story_plan_role": plan_role,
        "story_plan_beat": plan_beat,
        "story_plan_purpose": plan_purpose,
        "story_anchor": story_anchor,
        "story_plan_integrated": bool(plan_scene),
        "status": "completed",
    }

    caption_fields = (
        "caption_hindi",
        "hindi_caption",
        "caption_hi",
        "caption_english",
        "english_caption",
        "caption_en",
        "caption_hinglish",
        "hinglish_caption",
        "caption_text",
        "display_caption",
        "caption_language",
        "caption_lang",
        "language",
        "narration_language",
        "translations",
    )
    for field in caption_fields:
        if field in source:
            row[field] = source[field]

    return row


def validate_rows(rows, parts_count, scenes_per_part):
    expected_ids = [
        f"part_{p:02d}_scene_{s:02d}"
        for p in range(1, parts_count + 1)
        for s in range(1, scenes_per_part + 1)
    ]
    actual_ids = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Output contains an invalid scene record")
        if row.get("status") != "completed":
            raise RuntimeError("Output contains an incomplete scene")
        if not clean(row.get("narration")):
            raise RuntimeError(f"Empty narration: {row.get('scene_id')}")
        if not clean(row.get("visual_prompt")):
            raise RuntimeError(f"Empty visual prompt: {row.get('scene_id')}")
        actual_ids.append(row.get("scene_id"))

    if len(rows) != len(expected_ids):
        raise RuntimeError(
            f"Scene count mismatch: {len(rows)}/{len(expected_ids)}"
        )
    if actual_ids != expected_ids:
        raise RuntimeError("Scene IDs are missing, duplicated or out of order")


def main():
    log("=" * 60)
    log("KATHA LOK AI — VALIDATED SCENE PREPARATION")
    log("External API calls in this stage: 0")
    log("=" * 60)

    config = load_and_validate(CONFIG_FILE)
    format_type = clean(normalize_format(config)).lower()
    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full")

    parts_count = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    story, source_type, source_path = selected_story()
    if clean(story.get("format")).lower() != format_type:
        raise RuntimeError("Selected story FORMAT does not match input")

    validate_layout(story, parts_count, scenes_per_part, "Story")
    plan_map = load_plan(format_type, parts_count, scenes_per_part)
    bible = load_character_bible()

    topic = clean(get_topic(config))
    story_text = clean(get_story_text(config))
    if not topic:
        topic = clean(story.get("title") or story.get("topic"))
    if not topic and story_text:
        topic = story_text.splitlines()[0].strip()
    if not topic:
        raise RuntimeError("Could not determine story title/topic")

    settings_keys = (
        "PARTS", "SCENES", "AUDIENCE", "STORY_LENGTH",
        "SCENE_DURATION", "VISUAL_STYLE", "REALISM", "QUALITY",
        "CAMERA_STYLE", "LIGHTING", "MOOD", "TRANSITIONS",
        "PART_HOOK", "PART_SUSPENSE", "FINAL_RESOLUTION",
        "CHARACTER_CONSISTENCY", "WORLD_CONSISTENCY",
        "SCENE_CONTINUITY", "NATURAL_MOTION", "MUSIC",
        "MUSIC_STYLE", "SFX", "AMBIENT_SOUND",
    )
    settings_snapshot = {key: config.get(key) for key in settings_keys}

    source_fingerprint = digest({
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "source_type": source_type,
        "story": story,
        "story_plan": plan_map,
        "topic": topic,
        "story_text": story_text,
        "format": format_type,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "character_bible": bible,
        "settings": settings_snapshot,
    })

    old = read_json(OUTPUT_FILE)
    old_scenes = {}
    if (
        isinstance(old, dict)
        and old.get("source_fingerprint") == source_fingerprint
        and old.get("mapping_version") == MAPPING_VERSION
        and isinstance(old.get("scenes"), list)
    ):
        for item in old["scenes"]:
            if not isinstance(item, dict):
                continue
            try:
                key = (int(item["part"]), int(item["scene"]))
            except (KeyError, TypeError, ValueError):
                continue
            if item.get("status") == "completed":
                old_scenes[key] = item

    rows = []
    reused = 0
    global_number = 0

    for p, part in enumerate(story["parts"], 1):
        for s, source_scene in enumerate(part["scenes"], 1):
            global_number += 1
            key = (p, s)

            expected = normalize_scene(
                source_scene,
                plan_map.get(key, {}),
                p,
                s,
                global_number,
                config,
                bible,
            )

            previous = old_scenes.get(key)
            if previous == expected:
                rows.append(previous)
                reused += 1
            else:
                rows.append(expected)

    validate_rows(rows, parts_count, scenes_per_part)

    expected_total = parts_count * scenes_per_part
    result = {
        "status": "completed",
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "scene_order": "part_then_scene",
        "source_type": source_type,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "source_fingerprint": source_fingerprint,
        "model": "not_required",
        "api_calls": 0,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "total_parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "completed_scenes": len(rows),
        "reused_scenes": reused,
        "character_bible_available": bool(bible),
        "story_plan_integrated": bool(plan_map),
        "story_plan_scene_count": len(plan_map),
        "validation": {
            "exact_scene_count": True,
            "unique_scene_ids": True,
            "sequential_scene_order": True,
            "narration_present": True,
            "visual_prompts_present": True,
            "format_matches_input": True,
            "external_api_calls": 0,
        },
        "scenes": rows,
    }

    write_json(OUTPUT_FILE, result)
    write_json(CHECKPOINT_FILE, {
        "status": "completed",
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "source_fingerprint": source_fingerprint,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "total_scenes": expected_total,
        "completed_count": len(rows),
        "completed_scene_ids": [row["scene_id"] for row in rows],
        "failed_scene": None,
    })

    log("Format:", format_type)
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", expected_total)
    log("Story-plan scenes integrated:", len(plan_map))
    log("Reused identical scene records:", reused)
    log("External API calls: 0")
    log("Output:", OUTPUT_FILE)
    log("Checkpoint:", CHECKPOINT_FILE)
    log("SCENE PREPARATION: SUCCESS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"SCENE PREPARATION ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
