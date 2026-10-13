
#!/usr/bin/env python3
"""Prepare scene narration with strict mapping and resumable checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from input_config import (
    load_and_validate,
    get_parts,
    get_scenes,
    get_format,
    get_topic,
    get_story_text,
)

CONFIG_FILE = Path("Input/topic.txt")
SCENES_FILE = Path("output/scenes/scenes.json")
AI_STORY_FILE = Path("output/story/ai_story.json")
OUTPUT_FILE = Path("output/narration/narration.json")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise SystemExit(f"ERROR: Required file missing: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise SystemExit(f"ERROR: Invalid JSON in {path}: {exc}") from exc
        print(f"WARNING: Cannot reuse {path}: {exc}")
        return None
    if not isinstance(data, dict):
        if required:
            raise SystemExit(f"ERROR: Expected JSON object: {path}")
        return None
    return data


def parse_bool(value, default=True):
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


def scene_key(item):
    try:
        part = int(item["part"])
        scene = int(item.get("scene", item.get("local_scene")))
    except (KeyError, TypeError, ValueError):
        return None
    if part < 1 or scene < 1:
        return None
    return part, scene


def narration_text(item):
    value = item.get("narration")
    if not isinstance(value, str) or not value.strip():
        value = item.get("text")
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def extract_ai_story(data):
    result = []
    parts = data.get("parts", [])
    if not isinstance(parts, list):
        raise SystemExit("ERROR: ai_story.json parts must be a list.")

    for part_data in parts:
        if not isinstance(part_data, dict):
            continue
        part_number = part_data.get("part")
        scenes = part_data.get("scenes", [])
        if not isinstance(scenes, list):
            continue
        for item in scenes:
            if isinstance(item, dict):
                row = dict(item)
                row["part"] = part_number
                result.append(row)
    return result


def load_source_scenes():
    data = read_json(SCENES_FILE, required=False)
    if data:
        scenes = data.get("scenes", [])
        if not isinstance(scenes, list):
            raise SystemExit("ERROR: scenes.json scenes must be a list.")
        if scenes:
            if not all(isinstance(x, dict) for x in scenes):
                raise SystemExit("ERROR: Invalid scene entry in scenes.json.")
            return scenes, str(SCENES_FILE)

    data = read_json(AI_STORY_FILE)
    if data.get("status") != "completed":
        raise SystemExit("ERROR: AI story is not completed.")
    scenes = extract_ai_story(data)
    if not scenes:
        raise SystemExit("ERROR: No scenes found in AI story.")
    return scenes, str(AI_STORY_FILE)


def normalize_settings(config):
    return {
        "format": get_format(config),
        "parts": get_parts(config),
        "scenes_per_part": get_scenes(config),
        "voice": str(config.get("VOICE", "male")).strip(),
        "speed": str(config.get("SPEED", "+0%")).strip(),
        "captions": str(config.get("CAPTIONS", "hindi")).strip().lower(),
        "audience": str(config.get("AUDIENCE", "adult")).strip(),
        "scene_duration": str(config.get("SCENE_DURATION", "auto")).strip(),
        "story_length": str(config.get("STORY_LENGTH", "auto")).strip(),
        "music": parse_bool(config.get("MUSIC"), True),
        "sfx": parse_bool(config.get("SFX"), True),
        "ambient_sound": parse_bool(config.get("AMBIENT_SOUND"), True),
        "transitions": str(config.get("TRANSITIONS", "cinematic")).strip(),
    }


def validate_scenes(source, settings):
    expected = {
        (p, s)
        for p in range(1, settings["parts"] + 1)
        for s in range(1, settings["scenes_per_part"] + 1)
    }
    mapped = {}

    for index, item in enumerate(source, start=1):
        key = scene_key(item)
        if key is None:
            raise SystemExit(f"ERROR: Invalid scene mapping at row {index}.")
        if key in mapped:
            raise SystemExit(f"ERROR: Duplicate scene mapping: {key}.")
        if key not in expected:
            raise SystemExit(f"ERROR: Unexpected scene mapping: {key}.")
        if not narration_text(item):
            raise SystemExit(f"ERROR: Empty narration for {key}.")
        mapped[key] = item

    missing = sorted(expected - set(mapped))
    if missing:
        raise SystemExit(f"ERROR: Missing required scenes: {missing}")
    return mapped


def make_record(source, settings):
    key = scene_key(source)
    text = narration_text(source)
    duration = source.get("duration")
    if duration is None or not str(duration).strip():
        duration = settings["scene_duration"]

    return {
        "part": key[0],
        "scene": key[1],
        "text": text,
        "text_sha256": digest(text),
        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],
        "format": settings["format"],
        "audience": settings["audience"],
        "duration": duration,
        "estimated_duration": duration,
        "status": "completed",
    }


def load_reusable(settings_hash, source_map, settings):
    old = read_json(OUTPUT_FILE, required=False)
    if not old or old.get("status") not in {"in_progress", "completed"}:
        return {}

    if old.get("settings_sha256") != settings_hash:
        print("Settings changed; old narration checkpoint will not be reused.")
        return {}

    rows = old.get("scenes", [])
    if not isinstance(rows, list):
        return {}

    reusable = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "completed":
            continue
        key = scene_key(row)
        if key not in source_map:
            continue

        text = narration_text(source_map[key])
        if row.get("text") != text or row.get("text_sha256") != digest(text):
            continue

        expected = make_record(source_map[key], settings)
        fields = (
            "voice", "speed", "captions", "format", "audience",
            "duration", "estimated_duration",
        )
        if any(row.get(field) != expected.get(field) for field in fields):
            continue
        reusable[key] = dict(row)

    return reusable


def main():
    print("=" * 56)
    print("KATHA LOK AI — NARRATION PREPARATION")
    print("New story-generation API calls: 0")
    print("=" * 56)

    config = load_and_validate(str(CONFIG_FILE))
    settings = normalize_settings(config)
    total = settings["parts"] * settings["scenes_per_part"]

    source, source_path = load_source_scenes()
    source_map = validate_scenes(source, settings)

    topic = get_topic(config) or get_story_text(config)
    if not topic:
        story = read_json(AI_STORY_FILE, required=False) or {}
        topic = str(story.get("title") or story.get("topic") or "").strip()

    settings_hash = digest(json.dumps(
        settings, ensure_ascii=False, sort_keys=True
    ))

    reusable = load_reusable(settings_hash, source_map, settings)
    records = dict(reusable)

    output = {
        "status": "in_progress",
        "topic": topic,
        "model": "not_required",
        "api_calls": 0,
        "total_parts": settings["parts"],
        "scenes_per_part": settings["scenes_per_part"],
        "total_scenes": total,
        "settings_sha256": settings_hash,
        "source_file": source_path,
        "input_config": settings,
        "scenes": [
            records[k] for k in sorted(records)
        ],
        "completed_scenes": len(records),
        "updated_at": now(),
    }
    save_json(OUTPUT_FILE, output)

    print(f"Format: {settings['format']}")
    print(f"Expected scenes: {total}")
    print(f"Source: {source_path}")
    print(f"Reusable scenes: {len(reusable)}")

    for index, key in enumerate(sorted(source_map), start=1):
        if key in records:
            print(f"[{index}/{total}] Reuse Part {key[0]} Scene {key[1]}")
        else:
            records[key] = make_record(source_map[key], settings)
            print(f"[{index}/{total}] Prepare Part {key[0]} Scene {key[1]}")

        output["scenes"] = [records[k] for k in sorted(records)]
        output["completed_scenes"] = len(records)
        output["last_completed"] = {"part": key[0], "scene": key[1]}
        output["updated_at"] = now()
        save_json(OUTPUT_FILE, output)

    final_rows = [records[k] for k in sorted(source_map)]
    final_keys = [scene_key(row) for row in final_rows]

    if len(final_rows) != total or len(set(final_keys)) != total:
        raise SystemExit("ERROR: Final narration mapping/count is invalid.")

    if any(not row["text"].strip() for row in final_rows):
        raise SystemExit("ERROR: Final narration contains empty text.")

    output.update({
        "status": "completed",
        "scenes": final_rows,
        "completed_scenes": total,
        "api_calls": 0,
        "completed_at": now(),
        "updated_at": now(),
    })
    save_json(OUTPUT_FILE, output)

    print("=" * 56)
    print("NARRATION PREPARATION COMPLETED")
    print(f"Scenes prepared: {total}")
    print("Story-generation API calls: 0")
    print(f"Output: {OUTPUT_FILE}")
    print("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Narration preparation cancelled.")
