#!/usr/bin/env python3

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from input_config import (
    load_input_config,
    get_parts,
    get_scenes,
    normalize_format,
    get_topic,
)


AI_STORY_FILE = Path("output/story/ai_story.json")
SCENES_FILE = Path("output/scenes/scenes.json")
MODEL_FILE = Path("output/config/selected_model.json")
OUTPUT_FILE = Path("output/narration/narration.json")


# ============================================================
# INPUT CONFIG COMPATIBILITY HELPERS
# ============================================================

def cfg_text(config, key, default=""):
    value = config.get(key, default)

    if value is None:
        return str(default)

    return str(value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in {"true", "yes", "on", "1"}:
        return True

    if text in {"false", "no", "off", "0"}:
        return False

    return bool(default)


def get_scene_duration_value(config):
    value = config.get("SCENE_DURATION", "auto")

    if value is None:
        return "auto"

    text = str(value).strip()

    if not text:
        return "auto"

    if text.lower() == "auto":
        return "auto"

    return text


def resolve_topic_value(config):
    try:
        topic = get_topic(config)
    except Exception:
        topic = config.get("TOPIC", "")

    if topic is None:
        return ""

    return str(topic).strip()


# ============================================================
# JSON
# ============================================================

def load_json(path):
    if not path.is_file():
        raise SystemExit(
            f"ERROR: Required file not found: {path}"
        )

    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as file:
            return json.load(file)

    except Exception as exc:
        raise SystemExit(
            f"ERROR: Could not read {path}: {exc}"
        )


def load_optional_json(path):
    if not path.is_file():
        return None

    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as file:
            return json.load(file)

    except Exception as exc:
        print(
            f"WARNING: Could not read {path}: {exc}"
        )
        return None


def save_json_atomic(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_file = Path(
        f"{path}.tmp"
    )

    with temp_file.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )
        file.write("\n")

    os.replace(
        temp_file,
        path
    )


# ============================================================
# STORY SCENES
# ============================================================

def extract_ai_story_scenes(ai_story):
    parts = ai_story.get(
        "parts",
        []
    )

    if not isinstance(
        parts,
        list
    ):
        return []

    result = []

    for part_data in parts:
        if not isinstance(
            part_data,
            dict
        ):
            continue

        part_number = part_data.get(
            "part"
        )

        scenes = part_data.get(
            "scenes",
            []
        )

        if not isinstance(
            scenes,
            list
        ):
            continue

        for scene in scenes:
            if not isinstance(
                scene,
                dict
            ):
                continue

            scene_copy = dict(
                scene
            )

            scene_copy["part"] = (
                part_number
            )

            result.append(
                scene_copy
            )

    return result


def extract_generated_scenes():
    data = load_optional_json(
        SCENES_FILE
    )

    if not isinstance(
        data,
        dict
    ):
        return []

    scenes = data.get(
        "scenes",
        []
    )

    if not isinstance(
        scenes,
        list
    ):
        return []

    result = []

    for scene in scenes:
        if isinstance(
            scene,
            dict
        ):
            result.append(
                dict(scene)
            )

    return result


# ============================================================
# SCENE VALIDATION
# ============================================================

def scene_key(scene):
    try:
        return (
            int(scene.get("part")),
            int(scene.get("scene")),
        )
    except Exception:
        return None


def validate_scene_numbers(
    narration_scenes,
    expected_scenes
):
    expected_keys = set()

    for scene in expected_scenes:
        key = scene_key(scene)

        if key is not None:
            expected_keys.add(key)

    actual_keys = set()

    for scene in narration_scenes:
        key = scene_key(scene)

        if key is not None:
            actual_keys.add(key)

    missing = sorted(
        expected_keys - actual_keys
    )

    extra = sorted(
        actual_keys - expected_keys
    )

    if missing:
        raise ValueError(
            "Missing narration scenes: "
            f"{missing}"
        )

    if extra:
        raise ValueError(
            "Unexpected narration scenes: "
            f"{extra}"
        )

    if len(narration_scenes) != len(
        actual_keys
    ):
        raise ValueError(
            "Duplicate narration scene numbers found."
        )


# ============================================================
# NARRATION
# ============================================================

def build_narration_scene(
    scene,
    settings
):
    part = scene.get(
        "part"
    )

    scene_number = scene.get(
        "scene"
    )

    text = scene.get(
        "narration",
        ""
    )

    if not isinstance(
        text,
        str
    ):
        raise ValueError(
            f"Narration is not text for "
            f"Part {part} Scene {scene_number}"
        )

    text = text.strip()

    if not text:
        raise ValueError(
            f"Empty narration found for "
            f"Part {part} Scene {scene_number}"
        )

    configured_duration = (
        settings["scene_duration"]
    )

    scene_duration = scene.get(
        "duration",
        configured_duration
    )

    if (
        scene_duration is None
        or not str(scene_duration).strip()
    ):
        scene_duration = (
            configured_duration
        )

    return {
        "part": part,
        "scene": scene_number,

        "text": text,

        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],

        "format": settings["format"],
        "audience": settings["audience"],

        "duration": scene_duration,
        "estimated_duration": scene_duration,

        "status": "completed",
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "======================================"
    )
    print(
        "       NARRATION PREPARATION"
    )
    print(
        "======================================"
    )

    print(
        "No Gemini API call will be made."
    )

    print(
        "Narration is taken from generated "
        "scene data."
    )

    # --------------------------------------------------------
    # CENTRALIZED INPUT
    # --------------------------------------------------------

    config = load_input_config()

    settings = {
        "format": normalize_format(
            config
        ),

        "audience": cfg_text(
            config,
            "AUDIENCE",
            "adult"
        ),

        "voice": cfg_text(
            config,
            "VOICE",
            "male"
        ),

        "speed": cfg_text(
            config,
            "SPEED",
            "+0%"
        ),

        "captions": cfg_text(
            config,
            "CAPTIONS",
            "hindi"
        ),

        "scene_duration": get_scene_duration_value(
            config
        ),

        "parts": get_parts(
            config
        ),

        "scenes_per_part": get_scenes(
            config
        ),

        "music": cfg_bool(
            config,
            "MUSIC",
            True
        ),

        "sfx": cfg_bool(
            config,
            "SFX",
            True
        ),

        "ambient_sound": cfg_bool(
            config,
            "AMBIENT_SOUND",
            True
        ),

        "transitions": cfg_text(
            config,
            "TRANSITIONS",
            "cinematic"
        ),
    }

    # --------------------------------------------------------
    # MODEL METADATA
    # --------------------------------------------------------

    model_config = load_json(
        MODEL_FILE
    )

    if model_config.get(
        "status"
    ) != "selected":
        raise SystemExit(
            "ERROR: Gemini model is not selected."
        )

    model = model_config.get(
        "model",
        ""
    )

    # --------------------------------------------------------
    # AI STORY
    # --------------------------------------------------------

    ai_story = load_json(
        AI_STORY_FILE
    )

    if ai_story.get(
        "status"
    ) != "completed":
        raise SystemExit(
            "ERROR: AI story is not completed."
        )

    # --------------------------------------------------------
    # TOPIC / TITLE
    # --------------------------------------------------------

    topic = resolve_topic_value(
        config
    )

    if not topic:
        topic = ai_story.get(
            "title",
            ""
        )

    if not topic:
        topic = ai_story.get(
            "topic",
            ""
        )

    # --------------------------------------------------------
    # EXPECTED SCENES
    # --------------------------------------------------------

    ai_story_scenes = (
        extract_ai_story_scenes(
            ai_story
        )
    )

    generated_scenes = (
        extract_generated_scenes()
    )

    # Prefer generate_scenes.py output.
    #
    # This is important because scene generation may
    # contain improved narration, duration, continuity,
    # character IDs, and scene-specific information.
    if generated_scenes:
        expected_scenes = (
            generated_scenes
        )

        scene_source = (
            "output/scenes/scenes.json"
        )

    else:
        expected_scenes = (
            ai_story_scenes
        )

        scene_source = (
            "output/story/ai_story.json"
        )

    if not expected_scenes:
        raise SystemExit(
            "ERROR: No scenes found for narration."
        )

    # --------------------------------------------------------
    # EXACT INPUT COUNT VALIDATION
    # --------------------------------------------------------

    expected_total = (
        settings["parts"]
        * settings["scenes_per_part"]
    )

    if len(expected_scenes) != expected_total:
        raise SystemExit(
            "ERROR: Scene count mismatch.\n"
            f"Input expected: {expected_total}\n"
            f"Found: {len(expected_scenes)}"
        )

    # --------------------------------------------------------
    # CONFIG SUMMARY
    # --------------------------------------------------------

    print()
    print(
        "========== INPUT CONFIG =========="
    )
    print(
        f"FORMAT          : {settings['format']}"
    )
    print(
        f"AUDIENCE        : {settings['audience']}"
    )
    print(
        f"VOICE           : {settings['voice']}"
    )
    print(
        f"SPEED           : {settings['speed']}"
    )
    print(
        f"CAPTIONS        : {settings['captions']}"
    )
    print(
        f"SCENE_DURATION  : {settings['scene_duration']}"
    )
    print(
        f"PARTS           : {settings['parts']}"
    )
    print(
        f"SCENES/PART     : "
        f"{settings['scenes_per_part']}"
    )
    print(
        f"MUSIC           : {settings['music']}"
    )
    print(
        f"SFX             : {settings['sfx']}"
    )
    print(
        f"AMBIENT_SOUND   : "
        f"{settings['ambient_sound']}"
    )
    print(
        f"TRANSITIONS     : "
        f"{settings['transitions']}"
    )
    print(
        "=================================="
    )

    print()
    print(
        f"Topic/Title: {topic}"
    )

    print(
        f"Scene source: {scene_source}"
    )

    print(
        f"Selected model metadata: {model}"
    )

    print(
        f"Total parts: {settings['parts']}"
    )

    print(
        f"Total scenes: {len(expected_scenes)}"
    )

    # --------------------------------------------------------
    # EXISTING NARRATION CHECKPOINT
    # --------------------------------------------------------

    existing = load_optional_json(
        OUTPUT_FILE
    )

    narration_map = {}

    if isinstance(
        existing,
        dict
    ):
        existing_scenes = existing.get(
            "scenes",
            []
        )

        if isinstance(
            existing_scenes,
            list
        ):
            for item in existing_scenes:

                if not isinstance(
                    item,
                    dict
                ):
                    continue

                key = scene_key(
                    item
                )

                if key is None:
                    continue

                if (
                    item.get("status")
                    == "completed"
                    and item.get("text")
                ):
                    narration_map[key] = (
                        item
                    )

    if narration_map:
        print(
            f"Existing completed scenes: "
            f"{len(narration_map)}"
        )

    # --------------------------------------------------------
    # INITIAL OUTPUT
    # --------------------------------------------------------

    output = {
        "status": "in_progress",

        "topic": topic,

        "model": model,

        "api_calls": 0,

        "total_parts": settings[
            "parts"
        ],

        "scenes_per_part": settings[
            "scenes_per_part"
        ],

        "total_scenes": len(
            expected_scenes
        ),

        "input_config": {
            "format": settings["format"],
            "audience": settings["audience"],
            "voice": settings["voice"],
            "speed": settings["speed"],
            "captions": settings["captions"],
            "scene_duration": settings[
                "scene_duration"
            ],
            "music": settings["music"],
            "sfx": settings["sfx"],
            "ambient_sound": settings[
                "ambient_sound"
            ],
            "transitions": settings[
                "transitions"
            ],
        },

        "scenes": [],

        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    save_json_atomic(
        OUTPUT_FILE,
        output
    )

    # --------------------------------------------------------
    # PREPARE EVERY SCENE
    # --------------------------------------------------------

    for index, scene in enumerate(
        expected_scenes,
        start=1
    ):

        part = scene.get(
            "part"
        )

        scene_number = scene.get(
            "scene"
        )

        key = scene_key(
            scene
        )

        if key is None:
            raise SystemExit(
                f"ERROR: Invalid scene "
                f"number at index {index}"
            )

        if key in narration_map:

            # Update configuration fields even
            # for resumed scenes.
            existing_item = dict(
                narration_map[key]
            )

            existing_item[
                "voice"
            ] = settings["voice"]

            existing_item[
                "speed"
            ] = settings["speed"]

            existing_item[
                "captions"
            ] = settings["captions"]

            existing_item[
                "format"
            ] = settings["format"]

            existing_item[
                "duration"
            ] = scene.get(
                "duration",
                settings["scene_duration"]
            )

            narration_map[key] = (
                existing_item
            )

            print(
                f"[{index}/{len(expected_scenes)}] "
                f"Skipping completed "
                f"Part {part} Scene {scene_number}"
            )

            continue

        print()
        print(
            f"[{index}/{len(expected_scenes)}] "
            f"Preparing narration "
            f"Part {part} Scene {scene_number}"
        )

        generated = build_narration_scene(
            scene,
            settings
        )

        narration_map[key] = (
            generated
        )

        output["scenes"] = sorted(
            narration_map.values(),
            key=lambda item: (
                int(item.get("part", 0)),
                int(item.get("scene", 0)),
            )
        )

        output["last_completed"] = (
            generated
        )

        # Checkpoint after EVERY scene.
        save_json_atomic(
            OUTPUT_FILE,
            output
        )

        print(
            f"Completed: "
            f"Part {part} Scene {scene_number}"
        )

    # --------------------------------------------------------
    # FINAL VALIDATION
    # --------------------------------------------------------

    final_scenes = sorted(
        narration_map.values(),
        key=lambda item: (
            int(item.get("part", 0)),
            int(item.get("scene", 0)),
        )
    )

    validate_scene_numbers(
        final_scenes,
        expected_scenes
    )

    if len(final_scenes) != expected_total:
        raise SystemExit(
            "ERROR: Final narration scene "
            "count does not match Input."
        )

    # Validate every narration text.
    for item in final_scenes:

        text = item.get(
            "text",
            ""
        )

        if not isinstance(
            text,
            str
        ) or not text.strip():

            raise SystemExit(
                "ERROR: Empty narration for "
                f"Part {item.get('part')} "
                f"Scene {item.get('scene')}"
            )

    # --------------------------------------------------------
    # FINAL OUTPUT
    # --------------------------------------------------------

    output["status"] = "completed"

    output["scenes"] = final_scenes

    output["total_scenes"] = (
        len(final_scenes)
    )

    output["api_calls"] = 0

    output["completed_at"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    save_json_atomic(
        OUTPUT_FILE,
        output
    )

    print()
    print(
        "======================================"
    )
    print(
        "    NARRATION PREPARATION COMPLETED"
    )
    print(
        "======================================"
    )

    print(
        f"Scenes prepared: "
        f"{len(final_scenes)}"
    )

    print(
        "Gemini API calls: 0"
    )

    print(
        f"Voice: {settings['voice']}"
    )

    print(
        f"Speed: {settings['speed']}"
    )

    print(
        f"Captions: {settings['captions']}"
    )

    print(
        f"Output: {OUTPUT_FILE}"
    )

    print(
        "======================================"
    )


if __name__ == "__main__":
    main()
