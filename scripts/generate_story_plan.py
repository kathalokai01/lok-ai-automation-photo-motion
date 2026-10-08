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
    # ----------------------------------------------------------
    # LOAD + VALIDATE CENTRAL CONFIG
    # ----------------------------------------------------------

    config = load_and_validate()

    format_type = get_format(config)
    topic = get_topic(config)
    story_text = get_story_text(config)

    parts = get_parts(config)
    scenes_per_part = get_scenes(config)

    story_length = as_text(
        config,
        "STORY_LENGTH",
        "auto",
    )

    scene_duration = as_text(
        config,
        "SCENE_DURATION",
        "auto",
    )

    audience = as_text(
        config,
        "AUDIENCE",
        "adult",
    )

    caption_mode = as_text(
        config,
        "CAPTIONS",
        "hindi",
    ).lower()

    # ----------------------------------------------------------
    # STORY STRUCTURE SETTINGS
    # ----------------------------------------------------------

    part_hook = as_bool(
        config,
        "PART_HOOK",
        True,
    )

    part_suspense = as_bool(
        config,
        "PART_SUSPENSE",
        True,
    )

    final_resolution = as_bool(
        config,
        "FINAL_RESOLUTION",
        True,
    )

    # ----------------------------------------------------------
    # CHARACTER / WORLD CONTINUITY
    # ----------------------------------------------------------

    character_bible = as_bool(
        config,
        "CHARACTER_BIBLE",
        True,
    )

    character_consistency = as_bool(
        config,
        "CHARACTER_CONSISTENCY",
        True,
    )

    world_consistency = as_bool(
        config,
        "WORLD_CONSISTENCY",
        True,
    )

    scene_continuity = as_bool(
        config,
        "SCENE_CONTINUITY",
        True,
    )

    # ----------------------------------------------------------
    # VISUAL SETTINGS
    # ----------------------------------------------------------

    visual_style = as_text(
        config,
        "VISUAL_STYLE",
        "cinematic_realistic",
    )

    realism = as_text(
        config,
        "REALISM",
        "high",
    )

    camera_style = as_text(
        config,
        "CAMERA_STYLE",
        "cinematic",
    )

    cinematic_camera = as_bool(
        config,
        "CINEMATIC_CAMERA",
        True,
    )

    lighting = as_text(
        config,
        "LIGHTING",
        "cinematic",
    )

    realistic_lighting = as_bool(
        config,
        "REALISTIC_LIGHTING",
        True,
    )

    mood = as_text(
        config,
        "MOOD",
        "dramatic",
    )

    quality = as_text(
        config,
        "QUALITY",
        "high",
    )

    natural_motion = as_bool(
        config,
        "NATURAL_MOTION",
        True,
    )

    # ----------------------------------------------------------
    # NEGATIVE PROMPT SETTINGS
    # ----------------------------------------------------------

    negative_prompt_enabled = as_bool(
        config,
        "NEGATIVE_PROMPT",
        True,
    )

    avoid_cartoon = as_bool(
        config,
        "AVOID_CARTOON_LOOK",
        True,
    )

    avoid_neon = as_bool(
        config,
        "AVOID_NEON",
        True,
    )

    avoid_glitch = as_bool(
        config,
        "AVOID_GLITCH_EFFECTS",
        True,
    )

    # ----------------------------------------------------------
    # AUDIO SETTINGS
    # ----------------------------------------------------------

    music_enabled = as_bool(
        config,
        "MUSIC",
        True,
    )

    music_style = as_text(
        config,
        "MUSIC_STYLE",
        "cinematic",
    )

    sfx_enabled = as_bool(
        config,
        "SFX",
        True,
    )

    ambient_enabled = as_bool(
        config,
        "AMBIENT_SOUND",
        True,
    )

    transitions = as_text(
        config,
        "TRANSITIONS",
        "cinematic",
    )

    fps = int(
        config.get(
            "FPS",
            24,
        )
    )

    # ----------------------------------------------------------
    # FORMAT-SPECIFIC SCENE ROLES
    # ----------------------------------------------------------

    scene_roles_full = [
        "opening hook and setup",
        "introduce the central situation",
        "develop the main conflict",
        "raise the stakes",
        "reveal an important clue",
        "turning point",
        "confrontation",
        "consequence",
        "emotional or thematic development",
        "climax preparation",
        "climax or resolution",
    ]

    scene_roles_short = [
        "cold opening hook",
        "immediate setup",
        "mystery or unanswered question",
        "first escalation",
        "important clue",
        "major reveal",
        "tension escalation",
        "unexpected turn",
        "twist or emotional payoff",
        "final suspense beat",
        "strong closing hook",
    ]

    if format_type == "short":
        scene_roles = scene_roles_short
    else:
        scene_roles = scene_roles_full

    # ----------------------------------------------------------
    # STORY PLAN
    # ----------------------------------------------------------

    plan = {
        "status": "planned",

        # Canonical content source.
        # TOPIC is preferred.
        # STORY_TEXT is fallback.
        "topic": topic,

        "story_text": story_text,

        "title_source": (
            "TOPIC"
            if topic
            else "STORY_TEXT"
        ),

        "format": format_type,

        "audience": audience,

        "story_length": story_length,

        "scene_duration": scene_duration,

        "parts_count": parts,

        "scenes_per_part": scenes_per_part,

        "total_scenes": (
            parts * scenes_per_part
        ),

        "caption_mode": caption_mode,

        "parts": [],

        "generation_notes": {
            "part_hook": part_hook,
            "part_suspense": part_suspense,
            "final_resolution": final_resolution,

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

    # ----------------------------------------------------------
    # BUILD PARTS + SCENES
    # ----------------------------------------------------------

    for part_no in range(
        1,
        parts + 1,
    ):

        part = {
            "part": part_no,

            "hook_required": (
                part_hook
            ),

            "suspense_required": (
                part_suspense
                and part_no < parts
            ),

            "final_resolution": (
                final_resolution
                and part_no == parts
            ),

            "scenes": [],
        }

        for scene_no in range(
            1,
            scenes_per_part + 1,
        ):

            role = scene_roles[
                min(
                    scene_no - 1,
                    len(scene_roles) - 1,
                )
            ]

            scene = {
                "scene": scene_no,

                "status": "pending",

                "role": role,

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

                "character_consistency": (
                    character_consistency
                ),

                "world_consistency": (
                    world_consistency
                ),

                "scene_continuity": (
                    scene_continuity
                ),
            }

            part["scenes"].append(
                scene
            )

        plan["parts"].append(
            part
        )

    # ----------------------------------------------------------
    # WRITE OUTPUT
    # ----------------------------------------------------------

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUT.write_text(
        json.dumps(
            plan,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    # ----------------------------------------------------------
    # LOG
    # ----------------------------------------------------------

    print("=" * 60)
    print("STORY PLAN CREATED")
    print("=" * 60)

    print(
        f"Output          : {OUT}"
    )

    print(
        f"Format          : {format_type}"
    )

    print(
        f"Topic           : {topic}"
    )

    print(
        "Story text      : "
        + (
            "provided"
            if story_text
            else "not provided"
        )
    )

    print(
        f"Audience        : {audience}"
    )

    print(
        f"Story length    : {story_length}"
    )

    print(
        f"Scene duration  : {scene_duration}"
    )

    print(
        f"Parts           : {parts}"
    )

    print(
        f"Scenes/part     : {scenes_per_part}"
    )

    print(
        f"Total scenes    : "
        f"{parts * scenes_per_part}"
    )

    print(
        f"Hook enabled    : {part_hook}"
    )

    print(
        f"Suspense        : {part_suspense}"
    )

    print(
        f"Final resolution: {final_resolution}"
    )

    print(
        f"Visual style    : {visual_style}"
    )

    print(
        f"Realism         : {realism}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()
