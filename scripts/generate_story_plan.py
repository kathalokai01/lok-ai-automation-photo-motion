
#!/usr/bin/env python3
"""Create a deterministic, validated, resumable story plan."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "story" / "story_plan.json"
SCRIPT_VERSION = "story-plan-3"


def as_bool(config: dict, key: str, default: bool = True) -> bool:
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()
    if text in {"true", "yes", "on", "1"}:
        return True
    if text in {"false", "no", "off", "0", ""}:
        return False
    return default


def as_text(config: dict, key: str, default: str = "") -> str:
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def positive_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc

    if result < 1:
        raise ValueError(f"{name} must be greater than zero.")
    return result


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def validate_plan(
    plan: dict,
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> None:
    if plan.get("status") != "planned":
        raise RuntimeError("Plan status must be 'planned'.")

    if str(plan.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Plan format mismatch.")

    if str(plan.get("topic", "")).strip() != topic:
        raise RuntimeError("Plan topic mismatch.")

    expected_total = parts_count * scenes_per_part

    try:
        actual_parts = int(plan.get("parts_count", -1))
        actual_scenes = int(plan.get("scenes_per_part", -1))
        actual_total = int(plan.get("total_scenes", -1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Plan counts are invalid.") from exc

    if (
        actual_parts != parts_count
        or actual_scenes != scenes_per_part
        or actual_total != expected_total
    ):
        raise RuntimeError("Plan counts do not match current input.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Plan parts list is invalid.")

    seen_ids = set()
    global_number = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid part {part_number}.")

        if part.get("part") != part_number:
            raise RuntimeError(f"Part ordering error at part {part_number}.")

        if part.get("part_id") != f"part_{part_number:02d}":
            raise RuntimeError(f"Invalid part ID at part {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must contain {scenes_per_part} scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1

            if not isinstance(scene, dict):
                raise RuntimeError("Plan contains an invalid scene.")

            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

            if scene.get("scene") != scene_number:
                raise RuntimeError(f"Scene numbering error at {scene_id}.")

            if scene.get("part") != part_number:
                raise RuntimeError(f"Wrong part mapping at {scene_id}.")

            if scene.get("scene_id") != scene_id:
                raise RuntimeError(f"Wrong scene ID at {scene_id}.")

            if scene.get("global_scene_number") != global_number:
                raise RuntimeError(f"Global scene order error at {scene_id}.")

            if scene_id in seen_ids:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}.")

            seen_ids.add(scene_id)

    if len(seen_ids) != expected_total:
        raise RuntimeError("Total scene mapping validation failed.")


def main() -> None:
    config = load_and_validate()

    format_type = get_format(config)
    topic = get_topic(config)
    story_text = get_story_text(config)

    parts_count = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")
    total_scenes = parts_count * scenes_per_part

    story_length = as_text(config, "STORY_LENGTH", "auto")
    scene_duration = as_text(config, "SCENE_DURATION", "auto")
    audience = as_text(config, "AUDIENCE", "adult")
    caption_mode = as_text(config, "CAPTIONS", "hindi").lower()

    part_hook = as_bool(config, "PART_HOOK", True)
    part_suspense = as_bool(config, "PART_SUSPENSE", True)
    final_resolution = as_bool(config, "FINAL_RESOLUTION", True)

    character_bible = as_bool(config, "CHARACTER_BIBLE", True)
    character_consistency = as_bool(config, "CHARACTER_CONSISTENCY", True)
    world_consistency = as_bool(config, "WORLD_CONSISTENCY", True)
    scene_continuity = as_bool(config, "SCENE_CONTINUITY", True)

    visual_style = as_text(config, "VISUAL_STYLE", "cinematic_realistic")
    realism = as_text(config, "REALISM", "high")
    camera_style = as_text(config, "CAMERA_STYLE", "cinematic")
    cinematic_camera = as_bool(config, "CINEMATIC_CAMERA", True)
    lighting = as_text(config, "LIGHTING", "cinematic")
    realistic_lighting = as_bool(config, "REALISTIC_LIGHTING", True)
    mood = as_text(config, "MOOD", "dramatic")
    quality = as_text(config, "QUALITY", "high")
    natural_motion = as_bool(config, "NATURAL_MOTION", True)

    negative_prompt_enabled = as_bool(config, "NEGATIVE_PROMPT", True)
    avoid_cartoon = as_bool(config, "AVOID_CARTOON_LOOK", True)
    avoid_neon = as_bool(config, "AVOID_NEON", True)
    avoid_glitch = as_bool(config, "AVOID_GLITCH_EFFECTS", True)

    music_enabled = as_bool(config, "MUSIC", True)
    music_style = as_text(config, "MUSIC_STYLE", "cinematic")
    sfx_enabled = as_bool(config, "SFX", True)
    ambient_enabled = as_bool(config, "AMBIENT_SOUND", True)
    transitions = as_text(config, "TRANSITIONS", "cinematic")
    fps = positive_int(config.get("FPS", 24), "FPS")

    scene_roles_full = [
        "opening hook: begin inside a mysterious, emotional, dangerous, or surprising moment",
        "essential setup after the hook",
        "introduce the central situation and conflict",
        "raise the stakes through a consequential event",
        "reveal an important clue",
        "turning point that changes the characters' choices",
        "confrontation or difficult decision",
        "show consequences and emotional impact",
        "prepare the climax and reveal what is at risk",
        "resolve the main conflict with a satisfying payoff",
        "final suspense hook: reveal a new question, threat, or unexpected clue",
    ]

    scene_roles_short = [
        "cold opening hook: immediate curiosity, danger, mystery, or surprise",
        "fast essential setup",
        "introduce a mystery or unanswered question",
        "first escalation",
        "reveal an important clue",
        "major reveal that changes the situation",
        "increase tension",
        "unexpected turn",
        "pay off the central question",
        "resolve the main conflict",
        "final suspense hook: end on a new unanswered question, reveal, or imminent threat",
    ]

    roles = scene_roles_short if format_type == "short" else scene_roles_full

    generation_notes = {
        "part_hook": part_hook,
        "part_suspense": part_suspense,
        "final_resolution": final_resolution,
        "opening_hook_required": True,
        "final_scene_suspense_required": part_suspense,
        "resolve_main_conflict_before_final_suspense": True,
        "character_bible": character_bible,
        "character_consistency": character_consistency,
        "world_consistency": world_consistency,
        "scene_continuity": scene_continuity,
        "visual_style": visual_style,
        "realism": realism,
        "cinematic_camera": cinematic_camera,
        "camera_style": camera_style,
        "lighting": lighting,
        "realistic_lighting": realistic_lighting,
        "mood": mood,
        "quality": quality,
        "natural_motion": natural_motion,
        "negative_prompt": negative_prompt_enabled,
        "avoid_cartoon_look": avoid_cartoon,
        "avoid_neon": avoid_neon,
        "avoid_glitch_effects": avoid_glitch,
        "music": music_enabled,
        "music_style": music_style,
        "sfx": sfx_enabled,
        "ambient_sound": ambient_enabled,
        "transitions": transitions,
        "fps": fps,
    }

    plan = {
        "status": "planned",
        "script_version": SCRIPT_VERSION,
        "topic": topic,
        "story_text": story_text,
        "title_source": "TOPIC" if topic else "STORY_TEXT",
        "format": format_type,
        "audience": audience,
        "story_length": story_length,
        "scene_duration": scene_duration,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total_scenes,
        "caption_mode": caption_mode,
        "mapping_version": 3,
        "scene_order": "part_then_scene",
        "generation_notes": generation_notes,
        "parts": [],
    }

    for part_number in range(1, parts_count + 1):
        part = {
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "hook_required": part_hook,
            "suspense_required": part_suspense,
            "final_resolution": (
                final_resolution and part_number == parts_count
            ),
            "final_suspense_required": part_suspense,
            "expected_scene_count": scenes_per_part,
            "scenes": [],
        }

        for scene_number in range(1, scenes_per_part + 1):
            global_number = (
                (part_number - 1) * scenes_per_part + scene_number
            )
            is_first = scene_number == 1
            is_last = scene_number == scenes_per_part
            is_final_story_scene = (
                part_number == parts_count and is_last
            )

            role = roles[min(scene_number - 1, len(roles) - 1)]

            part["scenes"].append({
                "scene": scene_number,
                "scene_id": (
                    f"part_{part_number:02d}_scene_{scene_number:02d}"
                ),
                "global_scene_number": global_number,
                "part": part_number,
                "status": "pending",
                "role": role,
                "opening_hook_required": part_hook and is_first,
                "suspense_required": part_suspense and is_last,
                "final_story_scene": is_final_story_scene,
                "resolve_main_conflict_before_suspense": (
                    final_resolution and is_final_story_scene
                ),
                "narration": "",
                "visual_prompt": "",
                "negative_prompt": "",
                "sfx_prompt": "",
                "music_prompt": "",
                "scene_duration": scene_duration,
                "visual_style": visual_style,
                "realism": realism,
                "camera_style": camera_style,
                "lighting": lighting,
                "mood": mood,
                "natural_motion": natural_motion,
                "character_consistency": character_consistency,
                "world_consistency": world_consistency,
                "scene_continuity": scene_continuity,
            })

        plan["parts"].append(part)

    validate_plan(
        plan, format_type, topic, parts_count, scenes_per_part
    )

    # The fingerprint excludes generated metadata such as timestamps.
    plan["input_fingerprint"] = sha256_json({
        "script_version": SCRIPT_VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "story_length": story_length,
        "scene_duration": scene_duration,
        "audience": audience,
        "caption_mode": caption_mode,
        "generation_notes": generation_notes,
    })

    existing = load_json(OUT)
    if (
        isinstance(existing, dict)
        and existing.get("input_fingerprint") == plan["input_fingerprint"]
    ):
        try:
            validate_plan(
                existing, format_type, topic, parts_count, scenes_per_part
            )
            print("RESUME: Existing story plan is valid and unchanged.")
            print("No story-generation API call was made by this script.")
            print("Plan:", OUT)
            print("Parts:", parts_count, "| Scenes:", total_scenes)
            return
        except Exception as exc:
            print("Existing plan failed validation; rebuilding:", exc)

    atomic_write_json(OUT, plan)

    # Read back the written file to verify it is valid JSON and correctly mapped.
    saved = load_json(OUT)
    if not isinstance(saved, dict):
        raise RuntimeError("Saved plan could not be read back.")

    validate_plan(
        saved, format_type, topic, parts_count, scenes_per_part
    )
    if saved.get("input_fingerprint") != plan["input_fingerprint"]:
        raise RuntimeError("Saved plan fingerprint verification failed.")

    print("=" * 60)
    print("STORY PLAN CREATED AND VALIDATED")
    print("=" * 60)
    print("Output             :", OUT)
    print("Format             :", format_type)
    print("Topic              :", topic or "(using story text)")
    print("Story text         :", "provided" if story_text else "not provided")
    print("Parts              :", parts_count)
    print("Scenes per part    :", scenes_per_part)
    print("Expected total     :", total_scenes)
    print("Validated IDs      :", total_scenes)
    print("Mapping version    :", plan["mapping_version"])
    print("Fingerprint        :", plan["input_fingerprint"][:16] + "...")
    print("Part/scene mapping : PASS")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Story planning cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
