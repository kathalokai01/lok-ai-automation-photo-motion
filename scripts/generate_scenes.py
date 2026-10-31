#!/usr/bin/env python3
"""Katha Lok AI — validated scene preparation with strict story-plan integration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

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

PLAN_CANDIDATES = (
    ROOT / "output" / "config" / "story_plan.json",
    STORY_DIR / "story_plan.json",
)

BIBLE_FILE = STORY_DIR / "character_bible.json"
OUTPUT_DIR = ROOT / "output" / "scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"

VERSION = "scene-preparation-v12-json-safe-plan-fingerprint"
MAPPING_VERSION = 10


def log(*values: Any, file=None) -> None:
    print(*values, file=file or sys.stdout, flush=True)


def clean(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def digest(data: Any) -> str:
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def read_json(path: Path, required: bool = False) -> dict | None:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file is missing: {path}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        log(f"WARNING: Could not read optional {path.name}: {exc}")
        return None

    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected a JSON object in {path}")
        return None

    return data


def text_value(item: Any, *keys: str, default: str = "") -> str:
    if not isinstance(item, dict):
        return default

    for key in keys:
        value = clean(item.get(key))
        if value:
            return value

    return default


def bool_value(value: Any, default: bool = False) -> bool:
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


def positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer") from exc

    if number < 1:
        raise RuntimeError(f"{label} must be greater than zero")

    return number


def expected_scene_id(part: int, scene: int) -> str:
    return f"part_{part:02d}_scene_{scene:02d}"


def validate_layout(
    data: dict,
    parts_count: int,
    scenes_per_part: int,
    label: str,
) -> None:
    parts = data.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_count:
        actual = len(parts) if isinstance(parts, list) else 0
        raise RuntimeError(
            f"{label}: expected {parts_count} parts, found {actual}"
        )

    global_number = 0

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
            global_number += 1

            if not isinstance(scene, dict):
                raise RuntimeError(f"{label}: invalid scene {p}:{s}")

            try:
                declared_scene = int(scene.get("scene", s))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{label}: invalid scene number {p}:{s}"
                ) from exc

            if declared_scene != s:
                raise RuntimeError(f"{label}: scene order mismatch at {p}:{s}")

            scene_id = clean(scene.get("scene_id"))
            expected_id = expected_scene_id(p, s)

            if scene_id and scene_id != expected_id:
                raise RuntimeError(
                    f"{label}: expected scene ID {expected_id}, found {scene_id}"
                )

            if "global_scene_number" in scene:
                try:
                    declared_global = int(scene["global_scene_number"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"{label}: invalid global scene number for {expected_id}"
                    ) from exc

                if declared_global != global_number:
                    raise RuntimeError(
                        f"{label}: global scene number mismatch for {expected_id}"
                    )


def selected_story(format_type: str) -> tuple[dict, str, Path]:
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
            errors.append(f"{path.name} status is not completed")
            continue

        source_format = clean(data.get("format")).lower()

        if source_format != format_type:
            errors.append(
                f"{path.name} format is {source_format or '(missing)'}, "
                f"requested {format_type}"
            )
            continue

        if not isinstance(data.get("parts"), list) or not data["parts"]:
            errors.append(f"{path.name} has no parts")
            continue

        log("Selected story:", path.relative_to(ROOT).as_posix())
        return data, label, path

    raise RuntimeError(
        "No completed story matches the requested FORMAT. "
        + "; ".join(errors)
    )


def load_character_bible() -> dict:
    data = read_json(BIBLE_FILE)

    if not isinstance(data, dict):
        return {}

    if data.get("status") not in {"completed", "disabled"}:
        return {}

    return data


def load_plan(
    format_type: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict[tuple[int, int], dict]:
    plan = None
    selected_path = None
    errors = []

    for path in PLAN_CANDIDATES:
        if not path.is_file():
            errors.append(f"{path.relative_to(ROOT).as_posix()} missing")
            continue

        candidate = read_json(path)
        if candidate is None:
            errors.append(f"{path.relative_to(ROOT).as_posix()} invalid JSON")
            continue

        plan = candidate
        selected_path = path
        break

    if plan is None:
        raise RuntimeError(
            "Required story_plan.json not found or invalid. "
            + "; ".join(errors)
        )

    if plan.get("status") != "planned":
        raise RuntimeError("story_plan.json status must be planned")

    if clean(plan.get("format")).lower() != format_type:
        raise RuntimeError("Story plan FORMAT does not match Input/topic.txt")

    try:
        plan_parts = int(plan.get("parts_count", -1))
        plan_scenes = int(plan.get("scenes_per_part", -1))
        total_scenes = int(plan.get("total_scenes", -1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Story plan counts are invalid") from exc

    expected_total = parts_count * scenes_per_part

    if plan_parts != parts_count:
        raise RuntimeError("Story plan PARTS does not match input")
    if plan_scenes != scenes_per_part:
        raise RuntimeError("Story plan SCENES does not match input")
    if total_scenes != expected_total:
        raise RuntimeError("Story plan total_scenes does not match input")

    validate_layout(plan, parts_count, scenes_per_part, "Story plan")

    result: dict[tuple[int, int], dict] = {}

    for p, part in enumerate(plan["parts"], 1):
        for s, scene in enumerate(part["scenes"], 1):
            expected_id = expected_scene_id(p, s)

            if clean(scene.get("scene_id")) != expected_id:
                raise RuntimeError(
                    f"Story plan scene ID mismatch: {expected_id}"
                )

            if not text_value(scene, "purpose", "beat_description"):
                raise RuntimeError(f"Story plan purpose missing: {expected_id}")

            key = (p, s)
            if key in result:
                raise RuntimeError(f"Duplicate story-plan scene: {expected_id}")

            result[key] = scene

    if len(result) != expected_total:
        raise RuntimeError("Story plan has missing or duplicate scenes")

    log(
        "Story plan integrated:",
        len(result),
        "scenes from",
        selected_path.relative_to(ROOT).as_posix(),
    )
    return result


def normalize_scene(
    source: dict,
    plan_scene: dict,
    part_number: int,
    scene_number: int,
    global_number: int,
    settings: dict,
    bible: dict,
) -> dict:
    scene_id = expected_scene_id(part_number, scene_number)

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
            "visible shadow detail, balanced exposure"
        ),
    )

    mood = text_value(
        source, "mood", "emotion", "emotional_tone", default="cinematic"
    )

    sfx = text_value(source, "sfx_prompt", "sfx", "sound_effects")
    ambient = text_value(
        source, "ambient_sound", "ambient_prompt", "ambience"
    )
    music = text_value(source, "music_prompt", "music_direction", "music")
    suspense = text_value(source, "suspense_prompt", "suspense")
    title = text_value(
        source, "title", "scene_title", default=f"Scene {global_number}"
    )

    plan_role = text_value(plan_scene, "role")
    source_role = text_value(source, "role", "purpose", "scene_purpose")
    role = plan_role or source_role

    world_context = text_value(source, "world_context", "location", "setting")
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
        "hair, clothing, body type and carried objects. Maintain location, "
        "chronology and causal continuity. Do not invent unsupported "
        "identity changes. Follow the character bible when present."
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

def validate_rows(rows, parts_count, scenes_per_part):
    expected_ids = [
        expected_scene_id(p, s)
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


def serializable_plan(plan_map):
    """Convert tuple-keyed plan mapping into deterministic JSON-safe records."""
    return [
        {
            "part": part_number,
            "scene": scene_number,
            "plan": plan_map[(part_number, scene_number)],
        }
        for part_number, scene_number in sorted(plan_map)
    ]


def make_result(
    status,
    source_type,
    source_path,
    source_fingerprint,
    format_type,
    topic,
    story_text,
    parts_count,
    scenes_per_part,
    rows,
    plan_map,
    bible,
    reused,
):
    return {
        "status": status,
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
        "total_scenes": parts_count * scenes_per_part,
        "completed_scenes": len(rows),
        "reused_scenes": reused,
        "character_bible_available": bool(bible),
        "story_plan_integrated": len(plan_map) == parts_count * scenes_per_part,
        "story_plan_scene_count": len(plan_map),
        "validation": {
            "exact_scene_count": status == "completed",
            "unique_scene_ids": True,
            "sequential_scene_order": True,
            "narration_present": True,
            "visual_prompts_present": True,
            "format_matches_input": True,
            "external_api_calls": 0,
        },
        "scenes": rows,
    }


def resolve_story_title(config, story):
    topic = clean(get_topic(config))
    if topic:
        return topic

    title = text_value(
        story,
        "final_title",
        "generated_title",
        "story_title",
        "title",
        "topic",
    )
    if title:
        return title

    raise RuntimeError(
        "Story title missing. Generate a title from the complete story "
        "in generate_story.py first."
    )


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

    story, source_type, source_path = selected_story(format_type)
    validate_layout(story, parts_count, scenes_per_part, "Story")

    plan_map = load_plan(format_type, parts_count, scenes_per_part)
    bible = load_character_bible()

    topic = resolve_story_title(config, story)
    story_text = clean(get_story_text(config)) or clean(
        story.get("story_text")
    )

    settings_keys = (
        "PARTS", "SCENES", "AUDIENCE", "STORY_LENGTH",
        "SCENE_DURATION", "VISUAL_STYLE", "REALISM", "QUALITY",
        "CAMERA_STYLE", "LIGHTING", "MOOD", "TRANSITIONS",
        "PART_HOOK", "PART_SUSPENSE", "FINAL_RESOLUTION",
        "CHARACTER_CONSISTENCY", "WORLD_CONSISTENCY",
        "SCENE_CONTINUITY", "NATURAL_MOTION", "MUSIC",
        "MUSIC_STYLE", "SFX", "AMBIENT_SOUND",
    )
    settings_snapshot = {
        key: config.get(key) for key in settings_keys
    }

    source_fingerprint = digest({
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "source_type": source_type,
        "story": story,
        "story_plan": serializable_plan(plan_map),
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
        and old.get("format") == format_type
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
    expected_total = parts_count * scenes_per_part

    for p, part in enumerate(story["parts"], 1):
        for s, source_scene in enumerate(part["scenes"], 1):
            global_number += 1
            key = (p, s)
            plan_scene = plan_map[(p, s)]

            expected = normalize_scene(
                source_scene,
                plan_scene,
                p,
                s,
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

            progress = make_result(
                "in_progress",
                source_type,
                source_path,
                source_fingerprint,
                format_type,
                topic,
                story_text,
                parts_count,
                scenes_per_part,
                rows,
                plan_map,
                bible,
                reused,
            )
            write_json(OUTPUT_FILE, progress)

            write_json(CHECKPOINT_FILE, {
                "status": "in_progress",
                "version": VERSION,
                "mapping_version": MAPPING_VERSION,
                "source_fingerprint": source_fingerprint,
                "format": format_type,
                "total_scenes": expected_total,
                "completed_count": len(rows),
                "completed_scene_ids": [
                    item["scene_id"] for item in rows
                ],
                "failed_scene": None,
            })

    validate_rows(rows, parts_count, scenes_per_part)

    result = make_result(
        "completed",
        source_type,
        source_path,
        source_fingerprint,
        format_type,
        topic,
        story_text,
        parts_count,
        scenes_per_part,
        rows,
        plan_map,
        bible,
        reused,
    )

    result["validation"] = {
        "exact_scene_count": len(rows) == expected_total,
        "unique_scene_ids": len({
            item["scene_id"] for item in rows
        }) == expected_total,
        "sequential_scene_order": True,
        "narration_present": True,
        "visual_prompts_present": True,
        "format_matches_input": True,
        "story_plan_integrated": len(plan_map) == expected_total,
        "external_api_calls": 0,
    }

    write_json(OUTPUT_FILE, result)

    write_json(CHECKPOINT_FILE, {
        "status": "completed",
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "source_fingerprint": source_fingerprint,
        "format": format_type,
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