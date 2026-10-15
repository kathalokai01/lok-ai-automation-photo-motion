
#!/usr/bin/env python3
"""Build stable scene records from saved story data without extra API calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
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

CONFIG_FILE = ROOT / "Input" / "topic.txt"
STORY_DIR = ROOT / "output" / "story"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
SOURCE_STORY_FILE = STORY_DIR / "story.json"
BIBLE_FILE = STORY_DIR / "character_bible.json"

OUTPUT_DIR = ROOT / "output" / "scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"

VERSION = 6


def log(*items):
    print(*items, flush=True)


def digest(data):
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json(path, required=False):
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file is missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        log(f"WARNING: Cannot read optional file {path}: {exc}")
        return None


def write_json(path, data):
    path = Path(path)
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


def text_value(item, *keys, default=""):
    for key in keys:
        value = item.get(key)

        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()

    return default


def bool_value(value, default=False):
    if isinstance(value, bool):
        return value

    if value is None:
        return default

    value = str(value).strip().lower()

    if value in {"true", "1", "yes", "on", "enabled"}:
        return True

    if value in {"false", "0", "no", "off", "disabled"}:
        return False

    return default


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer.") from exc

    if result < 1:
        raise RuntimeError(f"{label} must be greater than zero.")

    return result


def selected_story():
    """Prefer completed AI story data, then completed source story data."""
    candidates = (
        (AI_STORY_FILE, "ai_story"),
        (SOURCE_STORY_FILE, "story"),
    )

    errors = []

    for path, label in candidates:
        data = read_json(path)

        if not isinstance(data, dict):
            errors.append(f"{path.name} missing or not a JSON object")
            continue

        if data.get("status") != "completed":
            errors.append(f"{path.name} is not marked completed")
            continue

        parts = data.get("parts")

        if not isinstance(parts, list) or not parts:
            errors.append(f"{path.name} has no parts")
            continue

        log(f"Using saved story source: {path.relative_to(ROOT)}")
        return data, label, path

    raise RuntimeError(
        "No completed story source is available. "
        + "; ".join(errors)
        + ". Story generation must finish before scene preparation."
    )


def load_character_bible():
    data = read_json(BIBLE_FILE)

    if not isinstance(data, dict):
        return {}

    if data.get("status") not in {"completed", "disabled"}:
        return {}

    return data


def extract_scene_text(source, keys, required=False, default=""):
    value = text_value(source, *keys)

    if required and not value:
        raise RuntimeError(
            "Scene is missing a required field: "
            + "/".join(keys)
        )

    return value or default


def normalize_scene(
    source,
    part_number,
    scene_number,
    global_number,
    settings,
    bible,
):
    if not isinstance(source, dict):
        raise RuntimeError(
            f"Invalid source scene at {part_number}:{scene_number}."
        )

    narration = extract_scene_text(
        source,
        ("narration", "text", "dialogue"),
        required=True,
    )

    visual = extract_scene_text(
        source,
        (
            "visual_prompt",
            "visual",
            "image_prompt",
            "scene_prompt",
            "visual_description",
            "description",
        ),
        required=True,
    )

    if len(visual) < 20:
        raise RuntimeError(
            f"Visual prompt too short at {part_number}:{scene_number}."
        )

    scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

    try:
        duration = float(
            source.get(
                "duration_seconds",
                source.get("duration", 5),
            )
        )
        if not 0.5 <= duration <= 3600:
            duration = 5.0
    except (TypeError, ValueError):
        duration = 5.0

    negative = extract_scene_text(
        source,
        ("negative_prompt", "avoid_prompt"),
        default="cartoon, anime, illustration, distorted anatomy, watermark",
    )

    camera = extract_scene_text(
        source,
        ("camera_prompt", "camera", "camera_direction"),
        default="Natural cinematic framing",
    )

    lighting = extract_scene_text(
        source,
        ("lighting_prompt", "lighting", "lighting_direction"),
        default="Physically plausible natural cinematic lighting",
    )

    mood = extract_scene_text(
        source,
        ("mood", "emotion", "emotional_tone"),
        default="cinematic",
    )

    sfx = extract_scene_text(
        source,
        ("sfx_prompt", "sfx", "sound_effects"),
    )

    ambient = extract_scene_text(
        source,
        ("ambient_sound", "ambient_prompt", "ambience"),
    )

    music = extract_scene_text(
        source,
        ("music_prompt", "music_direction", "music"),
    )

    suspense = extract_scene_text(
        source,
        ("suspense_prompt", "suspense"),
    )

    title = extract_scene_text(
        source,
        ("title", "scene_title"),
        default=f"Scene {global_number}",
    )

    role = extract_scene_text(
        source,
        ("role", "purpose", "scene_purpose"),
    )

    world_context = extract_scene_text(
        source,
        ("world_context", "location", "setting"),
    )

    dialogue = extract_scene_text(
        source,
        ("dialogue", "spoken_dialogue"),
    )

    if not bool_value(settings.get("SFX", True), True):
        sfx = ""

    if not bool_value(settings.get("MUSIC", True), True):
        music = ""

    if not bool_value(settings.get("AMBIENT_SOUND", True), True):
        ambient = ""

    source_scene_id = text_value(
        source,
        "scene_id",
        default=scene_id,
    )

    final_part = positive_int(settings["PARTS"], "PARTS")
    last_scene = positive_int(settings["SCENES"], "SCENES")

    final_resolution = bool_value(
        settings.get("FINAL_RESOLUTION", True),
        True,
    )
    part_suspense = bool_value(
        settings.get("PART_SUSPENSE", True),
        True,
    )
    part_hook = bool_value(
        settings.get("PART_HOOK", True),
        True,
    )

    # Only the final scene of the final part is exempt from the
    # suspense requirement when final resolution is explicitly enabled.
    is_final_scene = (
        part_number == final_part
        and scene_number == last_scene
    )
    resolution_at_ending = is_final_scene and final_resolution

    row = {
        "part": part_number,
        "scene": scene_number,
        "local_scene": scene_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": scene_id,
        "source_scene_id": source_scene_id,
        "global_scene_number": global_number,
        "mapping_version": VERSION,
        "title": title,
        "role": role,
        "narration": narration,
        "dialogue": dialogue,
        "visual_prompt": visual,
        "negative_prompt": negative,
        "duration": duration,
        "transition": text_value(
            source,
            "transition",
            "transition_prompt",
            default=str(settings.get("TRANSITIONS", "cinematic")),
        ),
        "camera": camera,
        "camera_prompt": camera,
        "lighting": lighting,
        "lighting_prompt": lighting,
        "mood": mood,
        "sfx": sfx,
        "sfx_prompt": sfx,
        "ambient_sound": ambient,
        "music_direction": music,
        "music_prompt": music,
        "world_context": world_context,
        "suspense_prompt": suspense,
        "opening_hook_required": (
            scene_number == 1
            and (part_number == 1 or part_hook)
        ),
        "suspense_required": (
            scene_number == last_scene
            and part_suspense
            and not resolution_at_ending
        ),
        "final_resolution_required": resolution_at_ending,
        "character_bible_available": bool(bible),
        "status": "completed",
    }

    caption_fields = (
        "caption_hindi",
        "hindi_caption",
        "caption_hi",
        "caption_english",
        "english_caption",
        "caption_en",
        "caption_hinglish",
        "hinglish_caption",
        "caption_text",
        "display_caption",
        "caption_language",
        "caption_lang",
        "language",
        "narration_language",
        "translations",
    )

    for field in caption_fields:
        if field in source:
            row[field] = source[field]

    return row


def validate_story_layout(story, parts_count, scenes_per_part):
    parts = story.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_count:
        actual = len(parts) if isinstance(parts, list) else 0
        raise RuntimeError(
            f"Part count mismatch: input requests {parts_count}, "
            f"story contains {actual}."
        )

    for part_index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Part {part_index} is invalid.")

        try:
            declared_part = int(part.get("part", part_index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part number at part {part_index}."
            ) from exc

        if declared_part != part_index:
            raise RuntimeError(
                f"Part order mismatch: expected {part_index}, "
                f"found {declared_part}."
            )

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            actual = len(scenes) if isinstance(scenes, list) else 0
            raise RuntimeError(
                f"Part {part_index}: expected {scenes_per_part} scenes, "
                f"found {actual}. Fix story/input mismatch before continuing."
            )

        for scene_index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Scene {part_index}:{scene_index} is invalid."
                )

            try:
                declared_scene = int(scene.get("scene", scene_index))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid scene number at {part_index}:{scene_index}."
                ) from exc

            if declared_scene != scene_index:
                raise RuntimeError(
                    f"Scene order mismatch at {part_index}:{scene_index}; "
                    f"found scene number {declared_scene}."
                )


def main():
    log("=" * 60)
    log("KATHA LOK AI — DETERMINISTIC SCENE PREPARATION")
    log("Gemini generation requests in this stage: 0")
    log("=" * 60)

    config = load_and_validate(CONFIG_FILE)
    format_type = normalize_format(config)
    parts_count = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    story, source_type, source_path = selected_story()
    validate_story_layout(story, parts_count, scenes_per_part)

    topic = str(get_topic(config) or "").strip()
    story_text = str(get_story_text(config) or "").strip()

    if not topic:
        topic = str(
            story.get("title")
            or story.get("topic")
            or (story_text.splitlines()[0] if story_text else "")
        ).strip()

    bible = load_character_bible()

    source_fingerprint = digest({
        "version": VERSION,
        "source_type": source_type,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "format": format_type,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "character_bible": bible,
        "settings": {
            key: config.get(key)
            for key in (
                "AUDIENCE",
                "STORY_LENGTH",
                "SCENE_DURATION",
                "VISUAL_STYLE",
                "REALISM",
                "QUALITY",
                "CAMERA_STYLE",
                "LIGHTING",
                "MOOD",
                "TRANSITIONS",
                "PART_HOOK",
                "PART_SUSPENSE",
                "FINAL_RESOLUTION",
                "CHARACTER_CONSISTENCY",
                "WORLD_CONSISTENCY",
                "SCENE_CONTINUITY",
                "NATURAL_MOTION",
                "MUSIC",
                "MUSIC_STYLE",
                "SFX",
                "AMBIENT_SOUND",
            )
        },
    })

    old = read_json(OUTPUT_FILE)
    old_scenes = {}

    if (
        isinstance(old, dict)
        and old.get("source_fingerprint") == source_fingerprint
        and old.get("mapping_version") == VERSION
        and isinstance(old.get("scenes"), list)
    ):
        for item in old["scenes"]:
            if not isinstance(item, dict):
                continue

            try:
                key = (int(item["part"]), int(item["scene"]))
            except (KeyError, TypeError, ValueError):
                continue

            if item.get("status") == "completed":
                old_scenes[key] = item

    rows = []
    reused = 0
    global_number = 0

    for part_index, part in enumerate(story["parts"], 1):
        for scene_index, source_scene in enumerate(part["scenes"], 1):
            global_number += 1
            key = (part_index, scene_index)

            expected = normalize_scene(
                source_scene,
                part_index,
                scene_index,
                global_number,
                config,
                bible,
            )

            previous = old_scenes.get(key)

            if previous == expected:
                row = previous
                reused += 1
            else:
                row = expected

            rows.append(row)

    expected_total = parts_count * scenes_per_part

    if len(rows) != expected_total:
        raise RuntimeError(
            f"Final scene count mismatch: {len(rows)}/{expected_total}."
        )

    expected_ids = [
        f"part_{part:02d}_scene_{scene:02d}"
        for part in range(1, parts_count + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    actual_ids = [row["scene_id"] for row in rows]

    if actual_ids != expected_ids:
        raise RuntimeError("Stable scene ID sequence validation failed.")

    if len(set(actual_ids)) != len(actual_ids):
        raise RuntimeError("Duplicate scene IDs detected.")

    result = {
        "status": "completed",
        "version": VERSION,
        "mapping_version": VERSION,
        "scene_order": "part_then_scene",
        "source_type": source_type,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "source_fingerprint": source_fingerprint,
        "model": "not_required",
        "api_calls": 0,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "total_parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "completed_scenes": len(rows),
        "reused_scenes": reused,
        "character_bible_available": bool(bible),
        "scenes": rows,
    }

    write_json(OUTPUT_FILE, result)

    write_json(CHECKPOINT_FILE, {
        "status": "completed",
        "mapping_version": VERSION,
        "source_fingerprint": source_fingerprint,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "total_scenes": expected_total,
        "completed_count": len(rows),
        "completed_scene_ids": actual_ids,
        "failed_scene": None,
    })

    log(f"Format: {format_type}")
    log(f"Parts: {parts_count}")
    log(f"Scenes per part: {scenes_per_part}")
    log(f"Total scenes: {expected_total}")
    log(f"Reused identical scene records: {reused}")
    log("Additional Gemini calls: 0")
    log(f"Scenes file: {OUTPUT_FILE}")
    log(f"Checkpoint: {CHECKPOINT_FILE}")
    log("SCENE PREPARATION: SUCCESS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"SCENE PREPARATION ERROR: {exc}")
        sys.exit(1)
