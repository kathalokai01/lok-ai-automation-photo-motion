
#!/usr/bin/env python3
"""Prepare scene narration with deterministic mapping and safe checkpoints.

This stage makes no Gemini generation requests. It prepares narration text
from completed story scenes and checkpoints each scene independently.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from input_config import (
    load_and_validate,
    get_parts,
    get_scenes,
    get_format,
    get_topic,
    get_story_text,
)

AI_STORY_FILE = Path("output/story/ai_story.json")
SCENES_FILE = Path("output/scenes/scenes.json")
MODEL_FILE = Path("output/config/selected_model.json")
OUTPUT_FILE = Path("output/narration/narration.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, required: bool = True) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise SystemExit(f"ERROR: Required file not found: {path}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise SystemExit(f"ERROR: Invalid JSON in {path}: {exc}") from exc
        print(f"WARNING: Ignoring unreadable checkpoint {path}: {exc}")
        return None

    if not isinstance(data, dict):
        if required:
            raise SystemExit(f"ERROR: Expected a JSON object in {path}.")
        return None

    return data


def scene_key(scene: dict[str, Any]) -> tuple[int, int] | None:
    try:
        part = int(scene.get("part"))
        number = int(scene.get("scene"))
    except (TypeError, ValueError):
        return None

    if part < 1 or number < 1:
        return None

    return part, number


def extract_ai_story_scenes(data: dict[str, Any]) -> list[dict[str, Any]]:
    parts = data.get("parts", [])

    if not isinstance(parts, list):
        raise SystemExit("ERROR: ai_story.json 'parts' must be a list.")

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

            item = dict(scene)
            item["part"] = part_number
            result.append(item)

    return result


def extract_source_scenes() -> tuple[list[dict[str, Any]], str]:
    generated = read_json(SCENES_FILE, required=False)

    if generated is not None:
        scenes = generated.get("scenes", [])

        if not isinstance(scenes, list):
            raise SystemExit(
                f"ERROR: {SCENES_FILE} 'scenes' must be a list."
            )

        if scenes:
            if any(not isinstance(item, dict) for item in scenes):
                raise SystemExit(
                    f"ERROR: {SCENES_FILE} contains a non-object scene."
                )

            return [dict(item) for item in scenes], str(SCENES_FILE)

    story = read_json(AI_STORY_FILE)

    if story.get("status") != "completed":
        raise SystemExit("ERROR: AI story is not marked completed.")

    scenes = extract_ai_story_scenes(story)

    if not scenes:
        raise SystemExit("ERROR: No scenes found in AI story.")

    return scenes, str(AI_STORY_FILE)


def source_narration(scene: dict[str, Any]) -> str:
    """Use the scene narration field, with text as a safe fallback."""
    value = scene.get("narration")

    if not isinstance(value, str) or not value.strip():
        value = scene.get("text")

    if not isinstance(value, str):
        return ""

    return " ".join(value.split())


def validate_source_scenes(
    scenes: list[dict[str, Any]],
    expected_parts: int,
    scenes_per_part: int,
) -> dict[tuple[int, int], dict[str, Any]]:
    expected = {
        (part, scene)
        for part in range(1, expected_parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    result: dict[tuple[int, int], dict[str, Any]] = {}

    for index, scene in enumerate(scenes, start=1):
        key = scene_key(scene)

        if key is None:
            raise SystemExit(
                f"ERROR: Invalid part/scene mapping at source row {index}."
            )

        if key in result:
            raise SystemExit(f"ERROR: Duplicate source scene {key}.")

        text = source_narration(scene)

        if not text:
            raise SystemExit(
                f"ERROR: Empty narration in Part {key[0]} Scene {key[1]}."
            )

        if key not in expected:
            raise SystemExit(
                f"ERROR: Unexpected source scene {key}; "
                "it does not match Input/topic.txt."
            )

        result[key] = scene

    missing = sorted(expected - set(result))

    if missing:
        raise SystemExit(
            f"ERROR: Input/source scene mapping is incomplete: {missing}"
        )

    return result


def build_settings(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": get_format(config),
        "audience": str(config.get("AUDIENCE", "adult")).strip(),
        "voice": str(config.get("VOICE", "male")).strip(),
        "speed": str(config.get("SPEED", "+0%")).strip(),
        "captions": str(config.get("CAPTIONS", "hindi")).strip().lower(),
        "scene_duration": str(config.get("SCENE_DURATION", "auto")).strip(),
        "story_length": str(config.get("STORY_LENGTH", "auto")).strip(),
        "music": bool(config.get("MUSIC", True)),
        "sfx": bool(config.get("SFX", True)),
        "ambient_sound": bool(config.get("AMBIENT_SOUND", True)),
        "transitions": str(config.get("TRANSITIONS", "cinematic")).strip(),
        "parts": get_parts(config),
        "scenes_per_part": get_scenes(config),
    }


def get_scene_duration(
    scene: dict[str, Any],
    settings: dict[str, Any],
) -> Any:
    value = scene.get("duration")

    if value is None or not str(value).strip():
        return settings["scene_duration"]

    return value


def build_scene_record(
    scene: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    key = scene_key(scene)

    if key is None:
        raise ValueError("Cannot build narration for an invalid scene.")

    part, number = key
    text = source_narration(scene)

    if not text:
        raise ValueError(f"Empty narration for Part {part} Scene {number}.")

    duration = get_scene_duration(scene, settings)

    return {
        "part": part,
        "scene": number,
        "text": text,
        "text_sha256": sha256_text(text),
        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],
        "format": settings["format"],
        "audience": settings["audience"],
        "duration": duration,
        "estimated_duration": duration,
        "status": "completed",
    }


def load_reusable_checkpoint(
    expected: dict[tuple[int, int], dict[str, Any]],
    settings: dict[str, Any],
    settings_sha: str,
) -> dict[tuple[int, int], dict[str, Any]]:
    existing = read_json(OUTPUT_FILE, required=False)

    if not existing or existing.get("status") not in {
        "in_progress",
        "completed",
    }:
        return {}

    # A changed format, voice, caption mode, scene count or duration policy
    # invalidates the old narration checkpoint.
    if existing.get("settings_sha256") != settings_sha:
        print("Checkpoint settings changed; rebuilding narration mapping.")
        return {}

    old_scenes = existing.get("scenes", [])

    if not isinstance(old_scenes, list):
        return {}

    reusable = {}

    for old in old_scenes:
        if not isinstance(old, dict):
            continue

        key = scene_key(old)

        if key is None or key not in expected:
            continue

        if old.get("status") != "completed":
            continue

        current_text = source_narration(expected[key])
        old_text = old.get("text")

        if not isinstance(old_text, str):
            continue

        if old_text.strip() != current_text:
            continue

        expected_hash = sha256_text(current_text)

        if old.get("text_sha256") != expected_hash:
            continue

        # Only reuse records whose settings still match.
        if old.get("voice") != settings["voice"]:
            continue
        if old.get("speed") != settings["speed"]:
            continue
        if old.get("captions") != settings["captions"]:
            continue
        if old.get("format") != settings["format"]:
            continue
        if old.get("audience") != settings["audience"]:
            continue

        reusable[key] = dict(old)

    return reusable


def main() -> None:
    print("=" * 56)
    print("KATHA LOK AI — NARRATION PREPARATION")
    print("Gemini generation calls in this stage: 0")
    print("=" * 56)

    config = load_and_validate("Input/topic.txt")
    settings = build_settings(config)

    expected_total = settings["parts"] * settings["scenes_per_part"]

    model_data = read_json(MODEL_FILE)

    if model_data.get("status") != "selected":
        raise SystemExit(
            f"ERROR: No selected Gemini model found in {MODEL_FILE}."
        )

    model = str(model_data.get("model", "")).strip()

    if not model:
        raise SystemExit("ERROR: Selected Gemini model name is empty.")

    source_scenes, source_path = extract_source_scenes()

    expected = validate_source_scenes(
        source_scenes,
        settings["parts"],
        settings["scenes_per_part"],
    )

    topic = get_topic(config) or get_story_text(config)

    if not topic:
        story = read_json(AI_STORY_FILE, required=False) or {}
        topic = str(story.get("title") or story.get("topic") or "").strip()

    settings_sha = sha256_text(
        json.dumps(settings, ensure_ascii=False, sort_keys=True)
    )

    print(f"Format: {settings['format']}")
    print(f"Parts: {settings['parts']}")
    print(f"Scenes per part: {settings['scenes_per_part']}")
    print(f"Expected total scenes: {expected_total}")
    print(f"Source: {source_path}")
    print(f"Selected model metadata: {model}")
    print(f"Topic/title source: {topic or '(not provided)'}")

    narration_map = load_reusable_checkpoint(
        expected=expected,
        settings=settings,
        settings_sha=settings_sha,
    )

    print(f"Reusable validated scenes: {len(narration_map)}")

    output: dict[str, Any] = {
        "status": "in_progress",
        "topic": topic,
        "model": model,
        "api_calls": 0,
        "total_parts": settings["parts"],
        "scenes_per_part": settings["scenes_per_part"],
        "total_scenes": expected_total,
        "settings_sha256": settings_sha,
        "source_file": source_path,
        "input_config": settings,
        "scenes": [],
        "updated_at": utc_now(),
    }

    # Preserve compatible completed records before starting the loop.
    output["scenes"] = sorted(
        narration_map.values(),
        key=lambda item: (int(item["part"]), int(item["scene"])),
    )
    atomic_write_json(OUTPUT_FILE, output)

    for index, key in enumerate(sorted(expected), start=1):
        source_scene = expected[key]
        part, number = key

        if key in narration_map:
            record = dict(narration_map[key])
            print(
                f"[{index}/{expected_total}] "
                f"Reuse Part {part} Scene {number}"
            )
        else:
            record = build_scene_record(source_scene, settings)
            print(
                f"[{index}/{expected_total}] "
                f"Prepare Part {part} Scene {number}"
            )

        narration_map[key] = record

        output["scenes"] = [
            narration_map[item_key]
            for item_key in sorted(narration_map)
        ]
        output["last_completed"] = {
            "part": part,
            "scene": number,
        }
        output["updated_at"] = utc_now()

        # Checkpoint after each scene so an interrupted run can resume.
        atomic_write_json(OUTPUT_FILE, output)

    final_scenes = [
        narration_map[key]
        for key in sorted(expected)
    ]

    final_keys = [scene_key(item) for item in final_scenes]

    if len(final_scenes) != expected_total:
        raise SystemExit("ERROR: Final narration count does not match Input.")

    if len(set(final_keys)) != expected_total:
        raise SystemExit("ERROR: Final narration has duplicate scene mappings.")

    for item in final_scenes:
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            raise SystemExit(
                f"ERROR: Empty final narration at "
                f"Part {item.get('part')} Scene {item.get('scene')}."
            )

    output["status"] = "completed"
    output["scenes"] = final_scenes
    output["completed_scenes"] = len(final_scenes)
    output["api_calls"] = 0
    output["completed_at"] = utc_now()
    output["updated_at"] = utc_now()

    atomic_write_json(OUTPUT_FILE, output)

    print("=" * 56)
    print("NARRATION PREPARATION COMPLETED")
    print(f"Prepared scenes: {len(final_scenes)}")
    print("Gemini generation calls: 0")
    print(f"Output: {OUTPUT_FILE}")
    print("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Narration preparation cancelled.")
