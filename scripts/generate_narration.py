
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
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


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


def positive_int(value, name):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise SystemExit(
            f"ERROR: {name} must be a valid integer; got {value!r}"
        )

    if result < 1:
        raise SystemExit(
            f"ERROR: {name} must be greater than zero."
        )

    return result


def get_scene_duration_value(config):
    value = config.get("SCENE_DURATION", "auto")

    if value is None:
        return "auto"

    text = str(value).strip()

    return text if text else "auto"


def resolve_topic_value(config):
    try:
        topic = get_topic(config)
    except Exception:
        topic = config.get("TOPIC", "")

    if topic is None:
        return ""

    return str(topic).strip()


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    if not path.is_file():
        raise SystemExit(
            f"ERROR: Required file not found: {path}"
        )

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"ERROR: Could not read valid JSON from {path}: {exc}"
        )

    if not isinstance(data, dict):
        raise SystemExit(
            f"ERROR: Expected a JSON object in {path}."
        )

    return data


def load_optional_json(path):
    if not path.is_file():
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"WARNING: Existing checkpoint could not be read: "
            f"{path}: {exc}"
        )
        return None


def save_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_file = path.with_name(path.name + ".tmp")

    try:
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_file, path)

    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass


# ============================================================
# SCENE EXTRACTION
# ============================================================

def extract_ai_story_scenes(ai_story):
    parts = ai_story.get("parts", [])

    if not isinstance(parts, list):
        raise SystemExit(
            "ERROR: ai_story.json 'parts' must be a list."
        )

    result = []

    for part_data in parts:
        if not isinstance(part_data, dict):
            continue

        part_number = part_data.get("part")
        scenes = part_data.get("scenes", [])

        if not isinstance(scenes, list):
            continue

        for scene in scenes:
            if not isinstance(scene, dict):
                continue

            scene_copy = dict(scene)
            scene_copy["part"] = part_number
            result.append(scene_copy)

    return result


def extract_generated_scenes():
    data = load_optional_json(SCENES_FILE)

    if data is None:
        return []

    if not isinstance(data, dict):
        raise SystemExit(
            f"ERROR: {SCENES_FILE} must contain a JSON object."
        )

    scenes = data.get("scenes", [])

    if not isinstance(scenes, list):
        raise SystemExit(
            f"ERROR: {SCENES_FILE} 'scenes' must be a list."
        )

    if any(not isinstance(scene, dict) for scene in scenes):
        raise SystemExit(
            f"ERROR: {SCENES_FILE} contains a non-object scene."
        )

    return [dict(scene) for scene in scenes]


# ============================================================
# SCENE IDENTIFICATION AND VALIDATION
# ============================================================

def scene_key(scene):
    if not isinstance(scene, dict):
        return None

    try:
        part = int(scene.get("part"))
        number = int(scene.get("scene"))
    except (TypeError, ValueError):
        return None

    if part < 1 or number < 1:
        return None

    return part, number


def validate_expected_scenes(scenes, expected_total):
    if len(scenes) != expected_total:
        raise SystemExit(
            "ERROR: Scene count mismatch.\n"
            f"Expected from Input: {expected_total}\n"
            f"Found: {len(scenes)}"
        )

    seen = set()

    for index, scene in enumerate(scenes, start=1):
        key = scene_key(scene)

        if key is None:
            raise SystemExit(
                f"ERROR: Invalid part/scene number at scene index {index}."
            )

        if key in seen:
            raise SystemExit(
                f"ERROR: Duplicate scene number found: {key}."
            )

        seen.add(key)

        text = scene.get("narration")

        if not isinstance(text, str) or not text.strip():
            raise SystemExit(
                "ERROR: Missing narration in source scene "
                f"Part {key[0]} Scene {key[1]}."
            )

    return seen


def validate_scene_numbers(narration_scenes, expected_scenes):
    expected_keys = set()
    actual_keys = set()

    for scene in expected_scenes:
        key = scene_key(scene)

        if key is None:
            raise ValueError(
                "Invalid part/scene number in expected scenes."
            )

        if key in expected_keys:
            raise ValueError(
                f"Duplicate expected scene number: {key}"
            )

        expected_keys.add(key)

    for scene in narration_scenes:
        key = scene_key(scene)

        if key is None:
            raise ValueError(
                "Invalid part/scene number in narration output."
            )

        if key in actual_keys:
            raise ValueError(
                f"Duplicate narration scene number: {key}"
            )

        actual_keys.add(key)

    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)

    if missing:
        raise ValueError(
            f"Missing narration scenes: {missing}"
        )

    if extra:
        raise ValueError(
            f"Unexpected narration scenes: {extra}"
        )


# ============================================================
# NARRATION PREPARATION
# ============================================================

def build_narration_scene(scene, settings):
    key = scene_key(scene)

    if key is None:
        raise ValueError("Cannot prepare narration for an invalid scene.")

    part, scene_number = key
    text = scene.get("narration")

    if not isinstance(text, str) or not text.strip():
        raise ValueError(
            f"Empty narration for Part {part} Scene {scene_number}."
        )

    text = text.strip()

    duration = scene.get("duration")

    if duration is None or not str(duration).strip():
        duration = settings["scene_duration"]

    return {
        "part": part,
        "scene": scene_number,
        "text": text,
        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],
        "format": settings["format"],
        "audience": settings["audience"],
        "duration": duration,
        "estimated_duration": duration,
        "status": "completed",
    }


def load_compatible_checkpoint(expected_by_key):
    existing = load_optional_json(OUTPUT_FILE)
    narration_map = {}

    if not isinstance(existing, dict):
        return narration_map

    existing_scenes = existing.get("scenes", [])

    if not isinstance(existing_scenes, list):
        return narration_map

    for item in existing_scenes:
        if not isinstance(item, dict):
            continue

        key = scene_key(item)

        if key is None or key not in expected_by_key:
            continue

        if item.get("status") != "completed":
            continue

        old_text = item.get("text")
        new_text = expected_by_key[key].get("narration")

        if not isinstance(old_text, str):
            continue

        if not isinstance(new_text, str):
            continue

        # Do not reuse stale narration if the source scene changed.
        if old_text.strip() != new_text.strip():
            print(
                f"Refreshing changed narration: "
                f"Part {key[0]} Scene {key[1]}"
            )
            continue

        narration_map[key] = dict(item)

    return narration_map


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 54)
    print("       NARRATION PREPARATION")
    print("=" * 54)
    print("Mode: Prepare narration from generated scene data")
    print("Gemini API calls: 0")

    # --------------------------------------------------------
    # CENTRALIZED INPUT CONFIGURATION
    # --------------------------------------------------------

    config = load_input_config()

    parts = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(
        get_scenes(config),
        "SCENES per part",
    )

    settings = {
        "format": normalize_format(config),
        "audience": cfg_text(config, "AUDIENCE", "adult"),
        "voice": cfg_text(config, "VOICE", "male"),
        "speed": cfg_text(config, "SPEED", "+0%"),
        "captions": cfg_text(config, "CAPTIONS", "hindi"),
        "scene_duration": get_scene_duration_value(config),
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "music": cfg_bool(config, "MUSIC", True),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),
    }

    expected_total = parts * scenes_per_part

    # --------------------------------------------------------
    # SELECTED MODEL METADATA
    # --------------------------------------------------------

    model_config = load_json(MODEL_FILE)

    if model_config.get("status") != "selected":
        raise SystemExit(
            "ERROR: No selected model found in "
            f"{MODEL_FILE}. Run model selection first."
        )

    model = model_config.get("model")

    if not isinstance(model, str) or not model.strip():
        raise SystemExit(
            "ERROR: Selected model metadata has no valid 'model' value."
        )

    model = model.strip()

    # --------------------------------------------------------
    # STORY INPUT
    # --------------------------------------------------------

    ai_story = load_json(AI_STORY_FILE)

    if ai_story.get("status") != "completed":
        raise SystemExit(
            "ERROR: AI story is not completed."
        )

    topic = resolve_topic_value(config)

    if not topic:
        topic = str(
            ai_story.get("title") or ai_story.get("topic") or ""
        ).strip()

    # Prefer processed scenes, which may contain updated narration.
    generated_scenes = extract_generated_scenes()

    if generated_scenes:
        expected_scenes = generated_scenes
        scene_source = str(SCENES_FILE)
    else:
        expected_scenes = extract_ai_story_scenes(ai_story)
        scene_source = str(AI_STORY_FILE)

    if not expected_scenes:
        raise SystemExit(
            "ERROR: No scenes found for narration."
        )

    expected_keys = validate_expected_scenes(
        expected_scenes,
        expected_total,
    )

    expected_by_key = {
        scene_key(scene): scene
        for scene in expected_scenes
    }

    # --------------------------------------------------------
    # CONFIG SUMMARY
    # --------------------------------------------------------

    print()
    print("========== INPUT CONFIG ==========")
    print(f"FORMAT          : {settings['format']}")
    print(f"AUDIENCE        : {settings['audience']}")
    print(f"VOICE           : {settings['voice']}")
    print(f"SPEED           : {settings['speed']}")
    print(f"CAPTIONS        : {settings['captions']}")
    print(f"SCENE_DURATION  : {settings['scene_duration']}")
    print(f"PARTS           : {parts}")
    print(f"SCENES/PART     : {scenes_per_part}")
    print(f"MUSIC           : {settings['music']}")
    print(f"SFX             : {settings['sfx']}")
    print(f"AMBIENT_SOUND   : {settings['ambient_sound']}")
    print(f"TRANSITIONS     : {settings['transitions']}")
    print("==================================")
    print(f"Topic/Title     : {topic}")
    print(f"Scene source    : {scene_source}")
    print(f"Selected model  : {model}")
    print(f"Total scenes    : {len(expected_scenes)}")

    # --------------------------------------------------------
    # RESUME CHECKPOINT
    # --------------------------------------------------------

    narration_map = load_compatible_checkpoint(expected_by_key)

    print(
        f"Reusable completed scenes: {len(narration_map)}"
    )

    # Keep valid existing checkpoints in the first saved output.
    # This prevents the checkpoint from being erased before the
    # first new scene is processed.
    output = {
        "status": "in_progress",
        "topic": topic,
        "model": model,
        "api_calls": 0,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(expected_scenes),
        "input_config": {
            "format": settings["format"],
            "audience": settings["audience"],
            "voice": settings["voice"],
            "speed": settings["speed"],
            "captions": settings["captions"],
            "scene_duration": settings["scene_duration"],
            "music": settings["music"],
            "sfx": settings["sfx"],
            "ambient_sound": settings["ambient_sound"],
            "transitions": settings["transitions"],
        },
        "scenes": sorted(
            narration_map.values(),
            key=lambda item: (
                int(item["part"]),
                int(item["scene"]),
            ),
        ),
        "generated_at": utc_now(),
    }

    save_json_atomic(OUTPUT_FILE, output)

    # --------------------------------------------------------
    # PREPARE AND CHECKPOINT EVERY SCENE
    # --------------------------------------------------------

    for index, scene in enumerate(expected_scenes, start=1):
        key = scene_key(scene)
        part, scene_number = key

        if key in narration_map:
            item = dict(narration_map[key])

            # Refresh configuration fields on resumed scenes.
            item["voice"] = settings["voice"]
            item["speed"] = settings["speed"]
            item["captions"] = settings["captions"]
            item["format"] = settings["format"]
            item["audience"] = settings["audience"]

            duration = scene.get("duration")

            if duration is None or not str(duration).strip():
                duration = settings["scene_duration"]

            item["duration"] = duration
            item["estimated_duration"] = duration
            narration_map[key] = item

            print(
                f"[{index}/{len(expected_scenes)}] "
                f"Reusing Part {part} Scene {scene_number}"
            )

        else:
            print(
                f"[{index}/{len(expected_scenes)}] "
                f"Preparing Part {part} Scene {scene_number}"
            )

            narration_map[key] = build_narration_scene(
                scene,
                settings,
            )

        output["scenes"] = sorted(
            narration_map.values(),
            key=lambda item: (
                int(item["part"]),
                int(item["scene"]),
            ),
        )

        output["last_completed"] = {
            "part": part,
            "scene": scene_number,
        }

        # Save after EVERY scene, including resumed scenes.
        save_json_atomic(OUTPUT_FILE, output)

    # --------------------------------------------------------
    # FINAL VALIDATION
    # --------------------------------------------------------

    final_scenes = sorted(
        narration_map.values(),
        key=lambda item: (
            int(item["part"]),
            int(item["scene"]),
        ),
    )

    validate_scene_numbers(final_scenes, expected_scenes)

    if len(final_scenes) != expected_total:
        raise SystemExit(
            "ERROR: Final narration scene count does not match Input."
        )

    for item in final_scenes:
        text = item.get("text")

        if not isinstance(text, str) or not text.strip():
            raise SystemExit(
                "ERROR: Empty narration for "
                f"Part {item.get('part')} "
                f"Scene {item.get('scene')}."
            )

    # Ensure the output includes precisely the expected scene set.
    final_keys = {scene_key(item) for item in final_scenes}

    if final_keys != expected_keys:
        raise SystemExit(
            "ERROR: Final narration scene mapping does not match source."
        )

    # --------------------------------------------------------
    # FINAL OUTPUT
    # --------------------------------------------------------

    output["status"] = "completed"
    output["scenes"] = final_scenes
    output["total_scenes"] = len(final_scenes)
    output["api_calls"] = 0
    output["completed_at"] = utc_now()

    save_json_atomic(OUTPUT_FILE, output)

    print()
    print("=" * 54)
    print("    NARRATION PREPARATION COMPLETED")
    print("=" * 54)
    print(f"Scenes prepared : {len(final_scenes)}")
    print("Gemini API calls: 0")
    print(f"Voice           : {settings['voice']}")
    print(f"Speed           : {settings['speed']}")
    print(f"Captions        : {settings['captions']}")
    print(f"Output          : {OUTPUT_FILE}")
    print("=" * 54)


if __name__ == "__main__":
    main()
