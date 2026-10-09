#!/usr/bin/env python3

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

    return bool(default)


def as_text(config, key, default=""):
    value = config.get(key, default)

    if value is None:
        return str(default)

    return str(value).strip()


def main():
    # LOAD AND VALIDATE INPUT
    config = load_and_validate()

    format_type = get_format(config)
    topic = get_topic(config)
    story_text = get_story_text(config)

    parts = get_parts(config)
    scenes_per_part = get_scenes(config)

    story_length = as_text(config, "STORY_LENGTH", "auto")
    scene_duration = as_text(config, "SCENE_DURATION", "auto")
    audience = as_text(config, "AUDIENCE", "adult")
    caption_mode = as_text(config, "CAPTIONS", "hindi").lower()

    # STORY STRUCTURE
    part_hook = as_bool(config, "PART_HOOK", True)
    part_suspense = as_bool(config, "PART_SUSPENSE", True)
    final_resolution = as_bool(config, "FINAL_RESOLUTION", True)

    # CHARACTER AND WORLD CONTINUITY
    character_bible = as_bool(config, "CHARACTER_BIBLE", True)
    character_consistency = as_bool(config, "CHARACTER_CONSISTENCY", True)
    world_consistency = as_bool(config, "WORLD_CONSISTENCY", True)
    scene_continuity = as_bool(config, "SCENE_CONTINUITY", True)

    # VISUAL SETTINGS
    visual_style = as_text(config, "VISUAL_STYLE", "cinematic_realistic")
    realism = as_text(config, "REALISM", "high")
    camera_style = as_text(config, "CAMERA_STYLE", "cinematic")
    cinematic_camera = as_bool(config, "CINEMATIC_CAMERA", True)
    lighting = as_text(config, "LIGHTING", "cinematic")
    realistic_lighting = as_bool(config, "REALISTIC_LIGHTING", True)
    mood = as_text(config, "MOOD", "dramatic")
    quality = as_text(config, "QUALITY", "high")
    natural_motion = as_bool(config, "NATURAL_MOTION", True)

    # NEGATIVE PROMPTS
    negative_prompt_enabled = as_bool(config, "NEGATIVE_PROMPT", True)
    avoid_cartoon = as_bool(config, "AVOID_CARTOON_LOOK", True)
    avoid_neon = as_bool(config, "AVOID_NEON", True)
    avoid_glitch = as_bool(config, "AVOID_GLITCH_EFFECTS", True)

    # AUDIO
    music_enabled = as_bool(config, "MUSIC", True)
    music_style = as_text(config, "MUSIC_STYLE", "cinematic")
    sfx_enabled = as_bool(config, "SFX", True)
    ambient_enabled = as_bool(config, "AMBIENT_SOUND", True)
    transitions = as_text(config, "TRANSITIONS", "cinematic")

    try:
        fps = int(config.get("FPS", 24))
    except (TypeError, ValueError):
        fps = 24

    # EVERY STORY STARTS WITH A HOOK.
    # FINAL SCENE RESERVES A SUSPENSE BEAT.
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
        "final suspense hook: after resolving the main conflict, reveal a new question, threat, or unexpected clue",
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

    scene_roles = (
        scene_roles_short
        if format_type == "short"
        else scene_roles_full
    )

    plan = {
        "status": "planned",
        "topic": topic,
        "story_text": story_text,
        "title_source": "TOPIC" if topic else "STORY_TEXT",
        "format": format_type,
        "audience": audience,
        "story_length": story_length,
        "scene_duration": scene_duration,
        "parts_count": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": parts * scenes_per_part,
        "caption_mode": caption_mode,
        "parts": [],
        "generation_notes": {
            "part_hook": part_hook,
            "part_suspense": part_suspense,
            "final_resolution": final_resolution,
            "opening_hook_required": True,
            "final_scene_suspense_required": True,
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

    # BUILD PARTS AND SCENES
    for part_no in range(1, parts + 1):
        part = {
            "part": part_no,
            "hook_required": part_hook,
            # Suspense is enabled for the final part too.
            "suspense_required": part_suspense,
            "final_resolution": (
                final_resolution and part_no == parts
            ),
            "final_suspense_required": (
                part_suspense and part_no == parts
            ),
            "scenes": [],
        }

        for scene_no in range(1, scenes_per_part + 1):
            role = scene_roles[
                min(scene_no - 1, len(scene_roles) - 1)
            ]

            is_first_scene = scene_no == 1
            is_final_scene = scene_no == scenes_per_part

            scene = {
                "scene": scene_no,
                "status": "pending",
                "role": role,
                "opening_hook_required": (
                    part_hook and is_first_scene
                ),
                "suspense_required": (
                    part_suspense and is_final_scene
                ),
                "final_story_scene": (
                    part_no == parts and is_final_scene
                ),
                "resolve_main_conflict_before_suspense": (
                    final_resolution
                    and part_no == parts
                    and is_final_scene
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

        plan["parts"].append(part)

    # WRITE PLAN
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=" * 60)
    print("STORY PLAN CREATED")
    print("=" * 60)
    print(f"Output           : {OUT}")
    print(f"Format           : {format_type}")
    print(f"Topic            : {topic}")
    print(f"Story text       : {'provided' if story_text else 'not provided'}")
    print(f"Audience         : {audience}")
    print(f"Story length     : {story_length}")
    print(f"Scene duration   : {scene_duration}")
    print(f"Parts            : {parts}")
    print(f"Scenes per part  : {scenes_per_part}")
    print(f"Total scenes     : {parts * scenes_per_part}")
    print(f"Opening hook     : {part_hook}")
    print(f"Part suspense    : {part_suspense}")
    print(f"Final suspense   : {part_suspense}")
    print(f"Final resolution : {final_resolution}")
    print(f"Visual style     : {visual_style}")
    print(f"Realism          : {realism}")
    print("=" * 60)


if __name__ == "__main__":
    main()