
#!/usr/bin/env python3
"""Offline, resumable conversion from story.json to ai_story.json.

This stage makes zero API requests. It preserves source narration and creates
the prompt fields required by downstream scene, image and audio stages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

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

STORY_DIR = ROOT / "output" / "story"
SOURCE_FILE = STORY_DIR / "story.json"
OUTPUT_FILE = STORY_DIR / "ai_story.json"
CONFIG_FILE = ROOT / "Input" / "topic.txt"

VERSION = "offline-ai-story-mapper-v1"


def log(*values):
    print(*values, flush=True)


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc
        return None
    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected JSON object: {path}")
        return None
    return data


def save_json(data):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT_FILE.with_suffix(".json.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, OUTPUT_FILE)
    finally:
        temp.unlink(missing_ok=True)


def digest(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def text_value(item, *keys, default=""):
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()
    return default


def bool_value(config, key, default=True):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "1", "yes", "on", "enabled"}:
        return True
    if value in {"false", "0", "no", "off", "disabled", ""}:
        return False
    return default


def word_count(value):
    return len(re.findall(
        r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*|[\u0900-\u097F]+",
        str(value or ""),
    ))


def validate_source(story, config):
    if story.get("status") != "completed":
        raise RuntimeError("story.json is not marked completed.")

    parts = story.get("parts")
    requested_parts = get_parts(config)
    requested_scenes = get_scenes(config)

    if not isinstance(parts, list) or len(parts) != requested_parts:
        raise RuntimeError(
            f"Expected {requested_parts} parts in story.json."
        )

    total = 0
    for p_index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid part {p_index}.")
        if int(part.get("part", -1)) != p_index:
            raise RuntimeError(f"Part numbering mismatch at {p_index}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != requested_scenes:
            raise RuntimeError(
                f"Part {p_index}: expected {requested_scenes} scenes."
            )

        for s_index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene {p_index}:{s_index}.")
            if int(scene.get("scene", -1)) != s_index:
                raise RuntimeError(f"Scene numbering mismatch at {p_index}:{s_index}.")

            narration = text_value(scene, "narration", "text")
            visual = text_value(
                scene, "visual", "visual_prompt", "visual_description"
            )
            if not narration:
                raise RuntimeError(f"Empty narration at {p_index}:{s_index}.")
            if not visual:
                raise RuntimeError(f"Missing visual at {p_index}:{s_index}.")
        total += len(scenes)

    return total


def normalize_scene(source, part_number, scene_number, global_number, config):
    narration = text_value(source, "narration", "text")
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
        raise RuntimeError(f"Missing narration at {part_number}:{scene_number}.")
    if len(visual) < 20:
        visual = (
            f"Realistic live-action cinematic depiction of this story event: "
            f"{visual or narration}. Believable physical setting, natural "
            "human expressions, consistent characters and clothing, "
            "realistic lighting, no cartoon or slideshow appearance."
        )

    negative = text_value(
        source,
        "negative_prompt",
        default=(
            "cartoon, anime, illustration, slideshow, distorted anatomy, "
            "extra fingers, inconsistent character identity, watermark, text"
        ),
    )
    camera = text_value(
        source,
        "camera_prompt",
        default="Natural cinematic framing with physically plausible camera movement",
    )
    lighting = text_value(
        source,
        "lighting_prompt",
        default="Realistic cinematic lighting matching the scene location and time",
    )

    sfx = text_value(source, "sfx_prompt", "sfx")
    music = text_value(source, "music_prompt", "music_direction")
    suspense = text_value(source, "suspense_prompt", "suspense")
    dialogue = text_value(source, "dialogue", "spoken_dialogue")

    if not bool_value(config, "SFX", True):
        sfx = ""
    if not bool_value(config, "MUSIC", True):
        music = ""

    scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

    row = {
        "part": part_number,
        "scene": scene_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": scene_id,
        "global_scene_number": global_number,
        "mapping_version": 3,
        "source_scene_id": str(source.get("scene_id") or scene_id),
        "role": text_value(source, "role", "purpose"),
        "status": "completed",
        "narration": narration,
        "dialogue": dialogue,
        "visual_prompt": visual,
        "negative_prompt": negative,
        "camera_prompt": camera,
        "lighting_prompt": lighting,
        "sfx_prompt": sfx,
        "music_prompt": music,
        "suspense_prompt": suspense,
        "duration_seconds": source.get(
            "duration_seconds", source.get("duration", 5)
        ),
        "mood": text_value(
            source, "mood", "emotional_tone", default="cinematic"
        ),
        "generation_method": "offline_source_mapping",
    }

    # Carry explicit caption translations and other useful scene metadata.
    preserve = (
        "caption_hindi", "hindi_caption", "caption_hi",
        "caption_english", "english_caption", "caption_en",
        "caption_hinglish", "hinglish_caption", "caption_text",
        "display_caption", "caption_language", "caption_lang",
        "language", "narration_language", "translations",
        "world_context", "location", "setting", "transition",
    )
    for field in preserve:
        if field in source:
            row[field] = source[field]

    return row


def main():
    log("=" * 60)
    log("KATHA LOK AI — OFFLINE AI-STORY MAPPER")
    log("External API requests: 0")
    log("=" * 60)

    config = load_and_validate(CONFIG_FILE)
    format_type = normalize_format(config)
    story = read_json(SOURCE_FILE)
    total = validate_source(story, config)

    topic = str(get_topic(config) or story.get("topic", "")).strip()
    story_text = str(get_story_text(config) or "").strip()
    hook = str(story.get("hook", "")).strip()

    minimum_hook = 7 if format_type == "short" else 14
    if word_count(hook) < minimum_hook:
        raise RuntimeError(
            f"story.json hook needs at least {minimum_hook} words; "
            "fix the source story before continuing."
        )

    first_scene = story["parts"][0]["scenes"][0]
    if not text_value(first_scene, "narration", "text").lower().startswith(hook.lower()):
        # Compare normalized text too, because Hindi punctuation can vary.
        def normalize(value):
            return re.sub(r"[^\w\s]", " ", str(value or "").lower()).split()

        if normalize(text_value(first_scene, "narration", "text"))[:len(normalize(hook))] != normalize(hook):
            raise RuntimeError(
                "First scene narration must begin with the exact story hook."
            )

    source_fingerprint = digest({
        "version": VERSION,
        "story": story,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "config": {
            key: config.get(key)
            for key in (
                "PART_HOOK", "PART_SUSPENSE", "FINAL_RESOLUTION",
                "VISUAL_STYLE", "REALISM", "CAMERA_STYLE", "LIGHTING",
                "MOOD", "MUSIC", "MUSIC_STYLE", "SFX", "AMBIENT_SOUND",
            )
        },
    })

    old = read_json(OUTPUT_FILE, required=False)
    old_parts = {}
    if (
        isinstance(old, dict)
        and old.get("source_fingerprint") == source_fingerprint
        and isinstance(old.get("parts"), list)
    ):
        for item in old["parts"]:
            if isinstance(item, dict):
                try:
                    old_parts[int(item.get("part", -1))] = item
                except (TypeError, ValueError):
                    pass

    result = {
        "status": "in_progress",
        "version": VERSION,
        "mapping_version": 3,
        "scene_order": "part_then_scene",
        "topic": topic,
        "story_text": story_text,
        "format": format_type,
        "model": "not_required",
        "generation_method": "offline_source_mapping",
        "api_calls": 0,
        "source_fingerprint": source_fingerprint,
        "total_scenes": total,
        "parts": [],
    }
    save_json(result)

    global_number = 0
    for part_index, source_part in enumerate(story["parts"], 1):
        saved = old_parts.get(part_index)
        scenes = None

        if (
            isinstance(saved, dict)
            and saved.get("status") == "completed"
            and isinstance(saved.get("scenes"), list)
            and len(saved["scenes"]) == len(source_part["scenes"])
        ):
            expected_narrations = [
                text_value(s, "narration", "text")
                for s in source_part["scenes"]
            ]
            saved_narrations = [
                text_value(s, "narration")
                for s in saved["scenes"]
            ]
            if expected_narrations == saved_narrations:
                scenes = saved["scenes"]
                log(f"Part {part_index}: checkpoint reused.")

        if scenes is None:
            scenes = []
            for scene_index, source_scene in enumerate(
                source_part["scenes"], 1
            ):
                global_number += 1
                scenes.append(normalize_scene(
                    source_scene,
                    part_index,
                    scene_index,
                    global_number,
                    config,
                ))
            log(f"Part {part_index}: {len(scenes)} scenes mapped offline.")
        else:
            global_number += len(scenes)

        result["parts"].append({
            "part": part_index,
            "part_id": f"part_{part_index:02d}",
            "status": "completed",
            "title": str(source_part.get("title") or f"Part {part_index}"),
            "hook_required": (
                part_index == 1 or bool_value(config, "PART_HOOK", True)
            ),
            "suspense_required": bool_value(config, "PART_SUSPENSE", True),
            "final_resolution": (
                bool_value(config, "FINAL_RESOLUTION", True)
                and part_index == len(story["parts"])
            ),
            "scenes": scenes,
        })

        # Checkpoint every completed part; no generation requests are made.
        result["completed_scenes"] = sum(
            len(part["scenes"]) for part in result["parts"]
        )
        result["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(result)

    rows = [
        scene
        for part in result["parts"]
        for scene in part["scenes"]
    ]
    expected_ids = [
        f"part_{p:02d}_scene_{s:02d}"
        for p in range(1, get_parts(config) + 1)
        for s in range(1, get_scenes(config) + 1)
    ]
    actual_ids = [row["scene_id"] for row in rows]

    if len(rows) != total or actual_ids != expected_ids:
        raise RuntimeError(
            f"Final scene validation failed: {len(rows)}/{total}."
        )

    result.update({
        "status": "completed",
        "parts": result["parts"],
        "completed_scenes": total,
        "api_calls": 0,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "validation": {
            "exact_scene_count": True,
            "unique_sequential_scene_ids": True,
            "narration_preserved": True,
            "visual_prompts_present": True,
            "external_api_calls": 0,
        },
    })
    save_json(result)

    log("=" * 60)
    log("OFFLINE AI-STORY MAPPING COMPLETED")
    log("Scenes:", total)
    log("API calls: 0")
    log("Output:", OUTPUT_FILE)
    log("Note: Source narration is preserved, not AI-rewritten.")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
