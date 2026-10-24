
#!/usr/bin/env python3
"""Prepare schema-compatible scenes using saved story and offline story plan."""

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

VERSION = 7
MAPPING_VERSION = 7


def log(*items):
    print(*items, flush=True)


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
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        log(f"WARNING: Cannot read optional file {path}: {exc}")
        return None


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
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()
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
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer.") from exc
    if result < 1:
        raise RuntimeError(f"{label} must be greater than zero.")
    return result


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
        log("Story source:", path.relative_to(ROOT))
        return data, label, path

    raise RuntimeError(
        "No completed story source found. " + "; ".join(errors)
    )


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
            f"{label}: expected {parts_count} parts; found {actual}."
        )

    for part_index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"{label}: part {part_index} is invalid.")
        try:
            declared_part = int(part.get("part", part_index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{label}: invalid part number.") from exc
        if declared_part != part_index:
            raise RuntimeError(f"{label}: part order mismatch at {part_index}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            actual = len(scenes) if isinstance(scenes, list) else 0
            raise RuntimeError(
                f"{label}: part {part_index} expected {scenes_per_part} "
                f"scenes; found {actual}."
            )

        for scene_index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"{label}: invalid scene {part_index}:{scene_index}."
                )
            try:
                declared_scene = int(scene.get("scene", scene_index))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{label}: invalid scene number."
                ) from exc
            if declared_scene != scene_index:
                raise RuntimeError(
                    f"{label}: scene order mismatch at "
                    f"{part_index}:{scene_index}."
                )


def load_plan(format_type, parts_count, scenes_per_part):
    plan = read_json(PLAN_FILE)
    if plan is None:
        log("WARNING: story_plan.json missing; using story scene fields.")
        return {}

    if not isinstance(plan, dict):
        raise RuntimeError("story_plan.json must be a JSON object.")
    if plan.get("status") != "planned":
        raise RuntimeError("story_plan.json status is not planned.")
    if str(plan.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story plan FORMAT does not match input.")
    if int(plan.get("parts_count", -1)) != parts_count:
        raise RuntimeError("Story plan PARTS does not match input.")
    if int(plan.get("scenes_per_part", -1)) != scenes_per_part:
        raise RuntimeError("Story plan SCENES does not match input.")
    validate_layout(plan, parts_count, scenes_per_part, "Story plan")

    result = {}
    for part in plan["parts"]:
        part_number = int(part["part"])
        for scene in part["scenes"]:
            scene_number = int(scene["scene"])
            expected_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(
                    f"Story plan scene ID mismatch: {expected_id}."
                )
            result[(part_number, scene_number)] = scene

    expected_total = parts_count * scenes_per_part
    if len(result) != expected_total:
        raise RuntimeError("Story plan contains duplicate or missing scene IDs.")

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
        raise RuntimeError(
            f"Invalid source scene at {part_number}:{scene_number}."
        )

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

    if not narration:
        raise RuntimeError(
            f"Missing narration at {part_number}:{scene_number}."
        )
    if len(visual) < 20:
        raise RuntimeError(
            f"Visual prompt missing or too short at {part_number}:{scene_number}."
        )

    scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

    try:
        duration = float(
            source.get("duration_seconds", source.get("duration", 5))
        )
        if not 0.5 <= duration <= 3600:
            duration = 5.0
    except (TypeError, ValueError):
        duration = 5.0

    negative = text_value(
        source,
        "negative_prompt",
        "avoid_prompt",
        default=(
            "cartoon, anime, illustration, distorted anatomy, "
            "watermark, inconsistent character identity, duplicate limbs"
        ),
    )
    camera = text_value(
        source, "camera_prompt", "camera", "camera_direction",
        default="Natural cinematic framing",
    )
    lighting = text_value(
        source, "lighting_prompt", "lighting", "lighting_direction",
        default="Physically plausible natural cinematic lighting",
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

    source_scene_id = text_value(source, "scene_id", default=scene_id)
    final_part = positive_int(settings["PARTS"], "PARTS")
    last_scene = positive_int(settings["SCENES"], "SCENES")
    final_resolution = bool_value(
        settings.get("FINAL_RESOLUTION", True), True
    )
    part_suspense = bool_value(
        settings.get("PART_SUSPENSE", True), True
    )
    part_hook = bool_value(settings.get("PART_HOOK", True), True)

    is_final_scene = (
        part_number == final_part and scene_number == last_scene
    )
    resolution_at_ending = is_final_scene and final_resolution

    plan_purpose = text_value(
        plan_scene, "purpose", "beat_description"
    )
    story_anchor = text_value(plan_scene, "story_anchor")
    if plan_purpose:
        suspense = suspense or (
            plan_purpose if role in {"part_ending", "part_hook", "finale"}
            else ""
        )

    continuity = (
        "Maintain the same established character identity, age range, "
        "gender presentation, facial features, hair, clothing, body type "
        "and carried objects across scenes. Preserve established locations, "
        "time order and causal continuity. Do not change character gender "
        "or invent a new identity without support from the source story."
    )
    if bible:
        continuity += (
            " Use the available character bible as the primary reference "
            "for recurring character appearance."
        )

    row = {
        "part": part_number,
        "scene": scene_number,
        "local_scene": scene_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": scene_id,
        "source_scene_id": source_scene_id,
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
            and not resolution_at_ending
        ),
        "final_resolution_required": resolution_at_ending,
        "character_bible_available": bool(bible),
        "continuity_prompt": continuity,
        "story_plan_role": plan_role,
        "story_plan_beat": text_value(plan_scene, "beat"),
        "story_plan_purpose": plan_purpose,
        "story_anchor": story_anchor,
        "story_plan_integrated": bool(plan_scene),
        "status": "completed",
    }

    caption_fields = (
        "caption_hindi", "hindi_caption", "caption_hi",
        "caption_english", "english_caption", "caption_en",
        "caption_hinglish", "hinglish_caption", "caption_text",
        "display_caption", "caption_language", "caption_lang",
        "language", "narration_language", "translations",
    )
    for field in caption_fields:
        if field in source:
            row[field] = source[field]

    return row


def main():
    log("=" * 60)
    log("KATHA LOK AI — STORY-PLAN-AWARE SCENE PREPARATION")
    log("External API calls in this stage: 0")
    log("=" * 60)

    config = load_and_validate(CONFIG_FILE)
    format_type = normalize_format(config)
    parts_count = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    story, source_type, source_path = selected_story()
    validate_layout(story, parts_count, scenes_per_part, "Story")

    topic = str(get_topic(config) or "").strip()
    story_text = str(get_story_text(config) or "").strip()
    if not topic:
        topic = str(story.get("title") or story.get("topic") or "").strip()
    if not topic and story_text:
        topic = story_text.splitlines()[0].strip()

    plan_map = load_plan(format_type, parts_count, scenes_per_part)
    bible = load_character_bible()

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
        "settings": {
            key: config.get(key)
            for key in (
                "AUDIENCE", "STORY_LENGTH", "SCENE_DURATION",
                "VISUAL_STYLE", "REALISM", "QUALITY", "CAMERA_STYLE",
                "LIGHTING", "MOOD", "TRANSITIONS", "PART_HOOK",
                "PART_SUSPENSE", "FINAL_RESOLUTION",
                "CHARACTER_CONSISTENCY", "WORLD_CONSISTENCY",
                "SCENE_CONTINUITY", "NATURAL_MOTION", "MUSIC",
                "MUSIC_STYLE", "SFX", "AMBIENT_SOUND",
            )
        },
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

    for part_index, part in enumerate(story["parts"], 1):
        for scene_index, source_scene in enumerate(part["scenes"], 1):
            global_number += 1
            key = (part_index, scene_index)
            expected = normalize_scene(
                source_scene,
                plan_map.get(key, {}),
                part_index,
                scene_index,
                global_number,
                config,
                bible,
            )
            previous = old_scenes.get(key)
            if previous == expected:
                row = previous
                reused += 1
            else:
                row = expected
            rows.append(row)

    expected_total = parts_count * scenes_per_part
    if len(rows) != expected_total:
        raise RuntimeError(
            f"Final scene count mismatch: {len(rows)}/{expected_total}."
        )

    expected_ids = [
        f"part_{part:02d}_scene_{scene:02d}"
        for part in range(1, parts_count + 1)
        for scene in range(1, scenes_per_part + 1)
    ]
    actual_ids = [row["scene_id"] for row in rows]
    if actual_ids != expected_ids:
        raise RuntimeError("Stable scene ID sequence validation failed.")
    if len(set(actual_ids)) != len(actual_ids):
        raise RuntimeError("Duplicate scene IDs detected.")

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
        "scenes": rows,
    }

    write_json(OUTPUT_FILE, result)
    write_json(CHECKPOINT_FILE, {
        "status": "completed",
        "mapping_version": MAPPING_VERSION,
        "source_fingerprint": source_fingerprint,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "total_scenes": expected_total,
        "completed_count": len(rows),
        "completed_scene_ids": actual_ids,
        "failed_scene": None,
    })

    log("Format:", format_type)
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", expected_total)
    log("Story-plan scenes integrated:", len(plan_map))
    log("Reused identical scene records:", reused)
    log("External API calls: 0")
    log("Scenes file:", OUTPUT_FILE)
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
