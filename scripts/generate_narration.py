#!/usr/bin/env python3

import hashlib
import json
import os
import sys
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
OUTPUT_FILE = Path("output/narration/narration.json")

SCRIPT_VERSION = 3


# ============================================================
# CONFIG HELPERS
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

    value = str(value).strip().lower()

    if value in {"true", "yes", "on", "1"}:
        return True

    if value in {"false", "no", "off", "0"}:
        return False

    return bool(default)


def get_scene_duration(config):
    value = cfg_text(config, "SCENE_DURATION", "auto")
    return value if value else "auto"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file not found: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def save_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_name(path.name + ".tmp")

    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_path, path)

    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def stable_hash(data):
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


# ============================================================
# SCENE VALIDATION
# ============================================================

def scene_key(scene):
    try:
        part = int(scene.get("part"))
        number = int(scene.get("scene"))

        if part < 1 or number < 1:
            return None

        return part, number

    except (TypeError, ValueError, AttributeError):
        return None


def load_source_scenes(parts, scenes_per_part):
    data = load_json(SCENES_FILE)

    if not isinstance(data, dict):
        raise RuntimeError("scenes.json must contain a JSON object.")

    if data.get("status") != "completed":
        raise RuntimeError(
            "Scene generation is not completed. "
            "Refusing to create narration from incomplete scenes."
        )

    scenes = data.get("scenes")

    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("scenes.json contains no scene list.")

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    scene_map = {}

    for item in scenes:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid scene entry in scenes.json.")

        key = scene_key(item)

        if key is None:
            raise RuntimeError(
                f"Invalid part/scene number in scene: {item}"
            )

        if key not in expected:
            raise RuntimeError(
                f"Unexpected scene number in scenes.json: {key}"
            )

        if key in scene_map:
            raise RuntimeError(
                f"Duplicate scene found: Part {key[0]} Scene {key[1]}"
            )

        narration = item.get("narration")

        if not isinstance(narration, str) or not narration.strip():
            raise RuntimeError(
                f"Missing narration for Part {key[0]} Scene {key[1]}."
            )

        scene_map[key] = dict(item)

    missing = sorted(expected - set(scene_map))

    if missing:
        raise RuntimeError(
            f"Missing {len(missing)} configured scenes: {missing[:20]}"
        )

    return [
        scene_map[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]


# ============================================================
# STORY METADATA
# ============================================================

def load_story_metadata():
    data = load_json(AI_STORY_FILE, required=False)

    if not isinstance(data, dict):
        return {}

    return data


def resolve_title(config, story):
    title = get_topic(config)

    if title:
        return title

    for key in ("title", "generated_title", "topic"):
        value = story.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return "Untitled Story"


# ============================================================
# NARRATION RECORD
# ============================================================

def build_narration_record(scene, settings):
    part = int(scene["part"])
    number = int(scene["scene"])
    narration_text = scene["narration"].strip()

    if not narration_text:
        raise ValueError(
            f"Empty narration: Part {part} Scene {number}"
        )

    duration = scene.get("duration")

    if duration is None or not str(duration).strip():
        duration = settings["scene_duration"]

    return {
        "part": part,
        "scene": number,
        "text": narration_text,

        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],

        "format": settings["format"],
        "audience": settings["audience"],

        "duration": duration,
        "estimated_duration": duration,

        "title": str(scene.get("title", "")).strip(),
        "dialogue": scene.get("dialogue", ""),
        "character_ids": scene.get("character_ids", []),

        "status": "completed",
    }


# ============================================================
# CHECKPOINT VALIDATION
# ============================================================

def load_reusable_records(source_signature, settings):
    existing = load_json(OUTPUT_FILE, required=False)

    if not isinstance(existing, dict):
        return {}

    if existing.get("status") not in {"in_progress", "completed"}:
        return {}

    if existing.get("source_signature") != source_signature:
        print("Existing narration belongs to different source scenes; rebuilding.")
        return {}

    if existing.get("input_config") != settings:
        print("Narration settings changed; rebuilding narration records.")
        return {}

    records = existing.get("scenes", [])

    if not isinstance(records, list):
        return {}

    reusable = {}

    for record in records:
        if not isinstance(record, dict):
            continue

        key = scene_key(record)

        if key is None:
            continue

        text = record.get("text")

        if (
            record.get("status") == "completed"
            and isinstance(text, str)
            and text.strip()
        ):
            reusable[key] = record

    return reusable


def validate_final_records(records, expected_total):
    keys = []

    for record in records:
        key = scene_key(record)

        if key is None:
            raise RuntimeError("Invalid scene number in final narration.")

        if not isinstance(record.get("text"), str):
            raise RuntimeError(
                f"Invalid narration text for scene {key}."
            )

        if not record["text"].strip():
            raise RuntimeError(
                f"Empty narration text for scene {key}."
            )

        keys.append(key)

    if len(keys) != expected_total:
        raise RuntimeError(
            f"Narration count mismatch: {len(keys)}/{expected_total}"
        )

    if len(set(keys)) != len(keys):
        raise RuntimeError("Duplicate scenes found in final narration.")

    return True

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("          KATHA LOK AI NARRATION")
    print("=" * 60)

    # --------------------------------------------------------
    # LOAD CONFIGURATION
    # --------------------------------------------------------

    config = load_input_config()

    fmt = normalize_format(config)
    parts = get_parts(config)
    scenes_per_part = get_scenes(config)
    expected_total = parts * scenes_per_part

    settings = {
        "format": fmt,
        "audience": cfg_text(config, "AUDIENCE", "adult"),
        "voice": cfg_text(config, "VOICE", "male"),
        "speed": cfg_text(config, "SPEED", "+0%"),
        "captions": cfg_text(config, "CAPTIONS", "hindi"),
        "scene_duration": get_scene_duration(config),
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "music": cfg_bool(config, "MUSIC", True),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),
    }

    resume_enabled = cfg_bool(config, "RESUME_ENABLED", True)
    skip_completed = cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
    save_each_scene = cfg_bool(
        config,
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
        True,
    )

    # --------------------------------------------------------
    # LOAD GENERATED STORY AND SCENES
    # --------------------------------------------------------

    story = load_story_metadata()
    title = resolve_title(config, story)

    source_scenes = load_source_scenes(
        parts,
        scenes_per_part,
    )

    source_signature = stable_hash({
        "script_version": SCRIPT_VERSION,
        "title": title,
        "settings": settings,
        "scenes": [
            {
                "part": item["part"],
                "scene": item["scene"],
                "narration": item["narration"],
                "duration": item.get("duration"),
            }
            for item in source_scenes
        ],
    })

    print(f"FORMAT          : {fmt}")
    print(f"TITLE           : {title}")
    print(f"PARTS           : {parts}")
    print(f"SCENES/PART     : {scenes_per_part}")
    print(f"TOTAL SCENES    : {expected_total}")
    print(f"VOICE           : {settings['voice']}")
    print(f"SPEED           : {settings['speed']}")
    print(f"CAPTIONS        : {settings['captions']}")
    print(f"RESUME_ENABLED  : {resume_enabled}")
    print(f"SKIP_COMPLETED  : {skip_completed}")
    print("GEMINI API CALLS: 0")

    # --------------------------------------------------------
    # RESTORE VALID CHECKPOINTS
    # --------------------------------------------------------

    reusable = {}

    if resume_enabled and skip_completed:
        reusable = load_reusable_records(
            source_signature,
            settings,
        )

    narration_map = {}

    # --------------------------------------------------------
    # PREPARE EACH SCENE
    # --------------------------------------------------------

    for index, scene in enumerate(source_scenes, start=1):
        part = int(scene["part"])
        number = int(scene["scene"])
        key = (part, number)

        cached = reusable.get(key)

        if cached:
            # Preserve existing record only when its text
            # still exactly matches the current source scene.
            if cached.get("text", "").strip() == scene["narration"].strip():
                narration_map[key] = dict(cached)

                print(
                    f"[{index}/{expected_total}] "
                    f"Reusing Part {part} Scene {number}"
                )
                continue

        record = build_narration_record(
            scene,
            settings,
        )

        narration_map[key] = record

        print(
            f"[{index}/{expected_total}] "
            f"Prepared Part {part} Scene {number}"
        )

        # ----------------------------------------------------
        # CHECKPOINT AFTER EACH SCENE
        # ----------------------------------------------------

        if save_each_scene:
            partial_records = [
                narration_map[item]
                for item in sorted(narration_map)
            ]

            output = {
                "status": "in_progress",
                "script_version": SCRIPT_VERSION,
                "title": title,
                "topic": title,
                "format": fmt,
                "model": "not_required",
                "api_calls": 0,

                "source_file": str(SCENES_FILE),
                "source_signature": source_signature,

                "total_parts": parts,
                "scenes_per_part": scenes_per_part,
                "total_scenes": expected_total,
                "completed_scenes": len(partial_records),

                "input_config": settings,
                "scenes": partial_records,

                "updated_at": utc_now(),
            }

            save_json_atomic(
                OUTPUT_FILE,
                output,
            )

    # --------------------------------------------------------
    # FINAL ORDER AND VALIDATION
    # --------------------------------------------------------

    final_records = [
        narration_map[(part, number)]
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    ]

    validate_final_records(
        final_records,
        expected_total,
    )

    # Ensure every record carries the current settings.
    for record in final_records:
        record["voice"] = settings["voice"]
        record["speed"] = settings["speed"]
        record["captions"] = settings["captions"]
        record["format"] = fmt
        record["audience"] = settings["audience"]

    # --------------------------------------------------------
    # FINAL OUTPUT
    # --------------------------------------------------------

    output = {
        "status": "completed",
        "script_version": SCRIPT_VERSION,

        "title": title,
        "topic": title,
        "format": fmt,

        "model": "not_required",
        "api_calls": 0,

        "source_file": str(SCENES_FILE),
        "source_signature": source_signature,

        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(final_records),
        "completed_scenes": len(final_records),

        "input_config": settings,
        "scenes": final_records,

        "generated_at": utc_now(),
        "completed_at": utc_now(),
    }

    save_json_atomic(
        OUTPUT_FILE,
        output,
    )

    # --------------------------------------------------------
    # FINAL REPORT
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("       NARRATION PREPARATION COMPLETE")
    print("=" * 60)
    print(f"Format          : {fmt}")
    print(f"Parts           : {parts}")
    print(f"Scenes          : {len(final_records)}")
    print(f"Voice           : {settings['voice']}")
    print(f"Captions        : {settings['captions']}")
    print("Gemini API calls: 0")
    print(f"Output          : {OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)