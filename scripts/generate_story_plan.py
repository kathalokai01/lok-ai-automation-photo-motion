#!/usr/bin/env python3
"""Create a validated, deterministic story/part/scene plan."""

import json
from pathlib import Path

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)

OUT = Path("output/story/story_plan.json")


def as_bool(config, key, default=True):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in {"true", "yes", "on", "1"}:
        return True

    if text in {"false", "no", "off", "0"}:
        return False

    return default


def as_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def positive_int(value, name):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc

    if result < 1:
        raise ValueError(f"{name} must be greater than zero.")

    return result


def main():
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

    visual_style = as_text(
        config, "VISUAL_STYLE", "cinematic_realistic"
    )
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

    plan = {
        "status": "planned",
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
        "mapping_version": 2,
        "scene_order": "part_then_scene",
        "parts": [],
        "generation_notes": {
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
        },
    }

    global_scene_number = 0

    for part_no in range(1, parts_count + 1):
        part = {
            "part": part_no,
            "part_id": f"part_{part_no:02d}",
            "hook_required": part_hook,
            "suspense_required": part_suspense,
            "final_resolution": (
                final_resolution and part_no == parts_count
            ),
            "final_suspense_required": (
                part_suspense and part_no == parts_count
            ),
            "expected_scene_count": scenes_per_part,
            "scenes": [],
        }

        for scene_no in range(1, scenes_per_part + 1):
            global_scene_number += 1
            role = roles[min(scene_no - 1, len(roles) - 1)]
            is_first = scene_no == 1
            is_last = scene_no == scenes_per_part
            is_final_story_scene = (
                part_no == parts_count and is_last
            )

            scene = {
                "scene": scene_no,
                "scene_id": f"part_{part_no:02d}_scene_{scene_no:02d}",
                "global_scene_number": global_scene_number,
                "part": part_no,
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
            }

            part["scenes"].append(scene)

        if len(part["scenes"]) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_no} scene count mismatch."
            )

        plan["parts"].append(part)

    # Validate all part/scene IDs before writing.
    seen_ids = set()
    seen_global_numbers = set()

    for expected_part, part in enumerate(plan["parts"], start=1):
        if part["part"] != expected_part:
            raise RuntimeError("Part ordering validation failed.")

        if len(part["scenes"]) != scenes_per_part:
            raise RuntimeError(
                f"Part {expected_part} does not have exactly "
                f"{scenes_per_part} scenes."
            )

        for expected_scene, scene in enumerate(
            part["scenes"], start=1
        ):
            expected_id = (
                f"part_{expected_part:02d}_scene_{expected_scene:02d}"
            )

            if scene["part"] != expected_part:
                raise RuntimeError(
                    f"Wrong part mapping for {expected_id}."
                )

            if scene["scene"] != expected_scene:
                raise RuntimeError(
                    f"Wrong scene order for {expected_id}."
                )

            if scene["scene_id"] != expected_id:
                raise RuntimeError(
                    f"Invalid scene ID: {scene['scene_id']}."
                )

            if scene["scene_id"] in seen_ids:
                raise RuntimeError(
                    f"Duplicate scene ID: {scene['scene_id']}."
                )

            if scene["global_scene_number"] in seen_global_numbers:
                raise RuntimeError("Duplicate global scene number.")

            seen_ids.add(scene["scene_id"])
            seen_global_numbers.add(scene["global_scene_number"])

    if len(seen_ids) != total_scenes:
        raise RuntimeError(
            f"Expected {total_scenes} scenes, found {len(seen_ids)}."
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=" * 60)
    print("STORY PLAN CREATED AND VALIDATED")
    print("=" * 60)
    print(f"Output            : {OUT}")
    print(f"Format            : {format_type}")
    print(f"Topic             : {topic or '(using story text)'}")
    print(f"Story text        : {'provided' if story_text else 'not provided'}")
    print(f"Parts             : {parts_count}")
    print(f"Scenes per part   : {scenes_per_part}")
    print(f"Expected total    : {total_scenes}")
    print(f"Validated IDs     : {len(seen_ids)}")
    print(f"Mapping version   : {plan['mapping_version']}")
    print("Part/scene mapping: PASS")
    print("=" * 60)


if __name__ == "__main__":
    main()