
#!/usr/bin/env python3
"""Katha Lok AI — source-grounded, continuity-safe photo-motion scene preparation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_topic,
)

CONFIG_FILE = ROOT / "Input" / "topic.txt"
STORY_DIR = ROOT / "output" / "story"
SOURCE_STORY_FILE = STORY_DIR / "story.json"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
BIBLE_FILE = STORY_DIR / "character_bible.json"

# The story generator rebuilds this file after SHORT part-count resolution.
# Prefer it over the provisional plan in output/config/.
PLAN_CANDIDATES = (
    STORY_DIR / "story_plan.json",
    ROOT / "output" / "config" / "story_plan.json",
)

OUTPUT_DIR = ROOT / "output" / "scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"

VERSION = "scene-preparation-v14-story-grounded"
MAPPING_VERSION = 11


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def digest(data: Any) -> str:
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def read_json(path: Path, required: bool = False) -> dict | None:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file is missing: {path}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        log(f"WARNING: Cannot read optional {path.name}: {exc}")
        return None

    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected a JSON object: {path}")
        return None
    return data


def text_value(
    item: Any,
    *keys: str,
    default: str = "",
) -> str:
    if not isinstance(item, dict):
        return default
    for key in keys:
        value = clean(item.get(key))
        if value:
            return value
    return default


def bool_value(value: Any, default: bool = False) -> bool:
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


def positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer.") from exc
    if number < 1:
        raise RuntimeError(f"{label} must be greater than zero.")
    return number


def word_count(value: Any) -> int:
    return len(re.findall(r"\S+", clean(value)))


def scene_id(part: int, scene: int) -> str:
    return f"part_{part:02d}_scene_{scene:02d}"


def scene_text(scene: dict) -> str:
    return text_value(
        scene,
        "narration",
        "narration_text",
        "voiceover",
        "text",
    )


def scene_visual(scene: dict) -> str:
    return text_value(
        scene,
        "visual_prompt",
        "visual",
        "image_prompt",
        "scene_prompt",
        "visual_description",
        "description",
    )


def validate_story_layout(
    story: dict,
    format_type: str,
    configured_parts: int,
    scenes_per_part: int,
) -> tuple[list[dict], int]:
    if story.get("status") != "completed":
        raise RuntimeError("Selected story is not marked completed.")

    actual_format = clean(story.get("format")).lower()
    if actual_format != format_type:
        raise RuntimeError(
            f"Story FORMAT mismatch: requested={format_type}, "
            f"found={actual_format or '(missing)'}."
        )

    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Selected story has no parts.")

    if format_type == "full" and len(parts) != configured_parts:
        raise RuntimeError(
            f"FULL mode requires {configured_parts} parts; "
            f"story contains {len(parts)}."
        )

    if format_type == "short" and len(parts) < 1:
        raise RuntimeError("SHORT mode requires at least one part.")

    total_scenes = 0
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {part_number}.")

        declared_part = int(part.get("part", part_number))
        if declared_part != part_number:
            raise RuntimeError(
                f"Part order mismatch at part {part_number}."
            )

        scenes = part.get("scenes")
        if not isinstance(scenes, list):
            raise RuntimeError(
                f"Part {part_number} has no scene list."
            )
        if len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number}: expected {scenes_per_part} scenes, "
                f"found {len(scenes)}."
            )

        for local_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Invalid scene record {part_number}:{local_number}."
                )

            declared_scene = int(scene.get("scene", local_number))
            if declared_scene != local_number:
                raise RuntimeError(
                    f"Scene order mismatch at {part_number}:{local_number}."
                )

            narration = scene_text(scene)
            visual = scene_visual(scene)
            if not narration:
                raise RuntimeError(
                    f"Missing narration: {scene_id(part_number, local_number)}"
                )
            if not visual:
                raise RuntimeError(
                    f"Missing visual prompt: {scene_id(part_number, local_number)}"
                )

            total_scenes += 1

    return parts, total_scenes


def select_story(format_type: str) -> tuple[dict, str, Path]:
    errors = []

    # The offline mapper runs after generate_story.py and preserves the
    # source narration while supplying a compatible scene representation.
    for path, label in (
        (AI_STORY_FILE, "ai_story"),
        (SOURCE_STORY_FILE, "story"),
    ):
        data = read_json(path)
        if not isinstance(data, dict):
            errors.append(f"{path.name}: missing or invalid")
            continue
        if data.get("status") != "completed":
            errors.append(f"{path.name}: not completed")
            continue
        if clean(data.get("format")).lower() != format_type:
            errors.append(f"{path.name}: FORMAT mismatch")
            continue
        if not isinstance(data.get("parts"), list) or not data["parts"]:
            errors.append(f"{path.name}: no parts")
            continue

        log("Selected scene source:", path.relative_to(ROOT).as_posix())
        return data, label, path

    raise RuntimeError(
        "No completed story matches the requested format. "
        + "; ".join(errors)
    )


def load_character_bible() -> dict:
    data = read_json(BIBLE_FILE)
    if not isinstance(data, dict):
        log("WARNING: Character bible unavailable; using story scene fields.")
        return {}
    if data.get("status") not in {"completed", "disabled"}:
        log("WARNING: Character bible is not completed; ignoring it.")
        return {}
    return data


def character_records(bible: dict) -> list[dict]:
    rows = bible.get("characters", [])
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows
        if isinstance(row, dict)
        and text_value(row, "id", "name", "character_name")
    ]


def character_names(scene: dict) -> list[str]:
    values = []
    for key in ("characters", "character_names", "character_ids"):
        raw = scene.get(key)
        if isinstance(raw, list):
            values.extend(clean(item) for item in raw)
        elif isinstance(raw, str) and clean(raw):
            values.extend(
                item.strip() for item in re.split(r"[,;|]", raw)
                if item.strip()
            )

    for key in (
        "character",
        "character_name",
        "protagonist",
        "person_name",
    ):
        value = clean(scene.get(key))
        if value:
            values.append(value)

    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def match_characters(
    scene: dict,
    bible: dict,
) -> list[dict]:
    records = character_records(bible)
    if not records:
        return []

    names = character_names(scene)
    if not names:
        # Do not assign every character to every scene.
        return []

    wanted = {name.casefold() for name in names}
    matched = []

    for record in records:
        identifiers = {
            clean(record.get("id")).casefold(),
            clean(record.get("name")).casefold(),
            clean(record.get("character_name")).casefold(),
        }
        identifiers.discard("")
        if wanted.intersection(identifiers):
            matched.append(record)

    return matched


def load_story_plan(
    format_type: str,
    parts_count: int,
    scenes_per_part: int,
) -> tuple[dict[tuple[int, int], dict], Path]:
    errors = []

    for path in PLAN_CANDIDATES:
        if not path.is_file():
            errors.append(f"{path.relative_to(ROOT)} missing")
            continue

        plan = read_json(path)
        if not isinstance(plan, dict):
            errors.append(f"{path.relative_to(ROOT)} invalid")
            continue

        if plan.get("status") != "planned":
            errors.append(f"{path.relative_to(ROOT)} status is not planned")
            continue
        if clean(plan.get("format")).lower() != format_type:
            errors.append(f"{path.relative_to(ROOT)} FORMAT mismatch")
            continue

        try:
            plan_parts = int(plan.get("parts_count", -1))
            plan_scenes = int(plan.get("scenes_per_part", -1))
            plan_total = int(plan.get("total_scenes", -1))
        except (TypeError, ValueError):
            errors.append(f"{path.relative_to(ROOT)} has invalid counts")
            continue

        expected_total = parts_count * scenes_per_part
        if (
            plan_parts != parts_count
            or plan_scenes != scenes_per_part
            or plan_total != expected_total
        ):
            errors.append(
                f"{path.relative_to(ROOT)} counts do not match "
                "the completed story"
            )
            continue

        parts = plan.get("parts")
        if not isinstance(parts, list) or len(parts) != parts_count:
            errors.append(f"{path.relative_to(ROOT)} has invalid parts")
            continue

        mapping = {}
        valid = True
        for part_number, part in enumerate(parts, 1):
            if not isinstance(part, dict):
                valid = False
                break
            scenes = part.get("scenes")
            if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
                valid = False
                break

            for local_number, scene in enumerate(scenes, 1):
                if not isinstance(scene, dict):
                    valid = False
                    break
                expected = scene_id(part_number, local_number)
                if clean(scene.get("scene_id")) != expected:
                    valid = False
                    break
                if not text_value(scene, "purpose", "beat_description"):
                    valid = False
                    break
                mapping[(part_number, local_number)] = scene

            if not valid:
                break

        if valid and len(mapping) == expected_total:
            log(
                "Story plan integrated:",
                len(mapping),
                "scenes from",
                path.relative_to(ROOT).as_posix(),
            )
            return mapping, path

        errors.append(f"{path.relative_to(ROOT)} scene map invalid")

    raise RuntimeError(
        "No story plan matches the completed story. "
        + "; ".join(errors)
    )



def estimate_duration(narration: str, source: dict) -> float:
    """Estimate scene duration from narration length, not a fixed clip."""
    raw = source.get(
        "duration_seconds",
        source.get("duration"),
    )
    if raw is not None:
        try:
            value = float(raw)
            if 1.0 <= value <= 60.0:
                return round(value, 2)
        except (TypeError, ValueError):
            pass

    words = max(1, word_count(narration))
    # Approximate natural narration at 2.5 words per second.
    return round(max(2.0, min(30.0, words / 2.5)), 2)


def resolve_title(config: dict, story: dict) -> str:
    configured_topic = clean(get_topic(config))
    if configured_topic:
        return configured_topic

    for key in (
        "final_title",
        "generated_title",
        "story_title",
        "title",
        "topic",
    ):
        value = clean(story.get(key))
        if value:
            return value

    raise RuntimeError(
        "Story title is missing. Generate the title from the complete "
        "story in generate_story.py."
    )


def make_visual_prompt(
    narration: str,
    source: dict,
    plan_scene: dict,
    matched_characters: list[dict],
    settings: dict,
) -> str:
    original = scene_visual(source)
    anchor = text_value(plan_scene, "story_anchor")
    purpose = text_value(plan_scene, "purpose", "beat_description")
    beat = text_value(plan_scene, "beat")
    location = text_value(
        source,
        "world_context",
        "location",
        "setting",
        "place",
    )

    # Keep the supplied visual prompt, but explicitly ground it in the
    # narration and plan anchor so the image stage cannot ignore the event.
    parts = [original]

    if anchor and anchor.casefold() not in original.casefold():
        parts.append(f"Exact story context: {anchor}")

    if narration and narration.casefold() not in original.casefold():
        parts.append(f"Scene event to depict accurately: {narration}")

    if purpose:
        parts.append(f"Scene purpose: {purpose}")
    if beat:
        parts.append(f"Narrative beat: {beat}")
    if location:
        parts.append(f"Story location: {location}")

    style = clean(settings.get("VISUAL_STYLE", "cinematic_realistic"))
    realism = clean(settings.get("REALISM", "high"))
    mood = text_value(
        source,
        "mood",
        "emotion",
        "emotional_tone",
        default=clean(settings.get("MOOD", "dramatic")),
    )

    parts.append(
        f"Visual style: {style}; realism: {realism}; mood: {mood}."
    )

    if matched_characters:
        character_details = []
        for character in matched_characters:
            details = {}
            for key in (
                "id",
                "name",
                "character_name",
                "gender",
                "age",
                "appearance",
                "face_features",
                "hair",
                "eyes",
                "skin_tone",
                "body_features",
                "clothing",
                "distinctive_features",
            ):
                value = clean(character.get(key))
                if value:
                    details[key] = value
            if details:
                character_details.append(details)

        if character_details:
            parts.append(
                "Use these exact recurring character references; "
                "do not change identity, age, gender, face, body, hair "
                "or clothing without a story-supported reason: "
                + json.dumps(
                    character_details,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

    parts.append(
        "Depict only the people, objects, action, location and event "
        "supported by this scene's narration and story context. "
        "Do not add unrelated people, animals, vehicles, weapons, "
        "buildings, clothing or events. Maintain realistic anatomy, "
        "natural proportions, consistent identity, physically plausible "
        "lighting and chronological continuity. Photorealistic live-action "
        "cinematic photography; no cartoon, anime, illustration, text, "
        "logo or watermark."
    )

    return "\n".join(item for item in parts if item)


def normalize_scene(
    source: dict,
    plan_scene: dict,
    part_number: int,
    local_number: int,
    global_number: int,
    total_parts: int,
    settings: dict,
    bible: dict,
) -> dict:
    identifier = scene_id(part_number, local_number)

    narration = scene_text(source)
    if not narration:
        raise RuntimeError(f"Missing narration: {identifier}")

    matched = match_characters(source, bible)
    visual = make_visual_prompt(
        narration,
        source,
        plan_scene,
        matched,
        settings,
    )

    if len(visual) < 40:
        raise RuntimeError(
            f"Visual prompt is too short: {identifier}"
        )

    role = text_value(
        plan_scene,
        "role",
        default=text_value(source, "role", "purpose", "scene_purpose"),
    )
    purpose = text_value(
        plan_scene,
        "purpose",
        "beat_description",
        default=text_value(source, "purpose", "description"),
    )
    beat = text_value(
        plan_scene,
        "beat",
        default=text_value(source, "beat"),
    )
    anchor = text_value(
        plan_scene,
        "story_anchor",
        default=text_value(source, "story_anchor"),
    )

    final_scene = (
        part_number == total_parts
        and local_number == positive_int(settings["SCENES"], "SCENES")
    )

    part_hook = bool_value(settings.get("PART_HOOK", True), True)
    part_suspense = bool_value(
        settings.get("PART_SUSPENSE", True),
        True,
    )
    final_resolution = bool_value(
        settings.get("FINAL_RESOLUTION", True),
        True,
    )

    is_part_opening = local_number == 1
    is_part_ending = (
        local_number == positive_int(settings["SCENES"], "SCENES")
    )

    resolves_story = final_scene and final_resolution

    suspense = text_value(
        source,
        "suspense_prompt",
        "suspense",
    )
    if not suspense and is_part_ending and not resolves_story:
        suspense = purpose or beat or anchor or narration

    if not suspense and resolves_story:
        suspense = (
            "Complete the central conflict using only the established "
            "story events and its actual ending."
        )

    opening_required = (
        is_part_opening
        and (part_number == 1 or part_hook)
    )
    suspense_required = (
        is_part_ending
        and part_suspense
        and not resolves_story
    )

    negative = text_value(
        source,
        "negative_prompt",
        "avoid_prompt",
        default=(
            "cartoon, anime, illustration, CGI appearance, plastic skin, "
            "deformed anatomy, extra fingers, extra limbs, duplicated "
            "people, inconsistent face, inconsistent age, wrong gender, "
            "unrelated objects, unrelated events, text, logo, watermark"
        ),
    )

    camera = text_value(
        source,
        "camera_prompt",
        "camera",
        "camera_direction",
        default="Natural cinematic camera framing appropriate to the action",
    )
    lighting = text_value(
        source,
        "lighting_prompt",
        "lighting",
        "lighting_direction",
        default=(
            "Physically plausible lighting, natural skin tones, "
            "balanced exposure and realistic shadow detail"
        ),
    )

    sfx = text_value(source, "sfx_prompt", "sfx", "sound_effects")
    ambient = text_value(
        source,
        "ambient_sound",
        "ambient_prompt",
        "ambience",
    )
    music = text_value(
        source,
        "music_prompt",
        "music_direction",
        "music",
    )

    if not bool_value(settings.get("SFX", True), True):
        sfx = ""
    if not bool_value(settings.get("MUSIC", True), True):
        music = ""
    if not bool_value(settings.get("AMBIENT_SOUND", True), True):
        ambient = ""

    duration = estimate_duration(narration, source)
    character_ids = [
        text_value(item, "id", "name", "character_name")
        for item in matched
    ]
    character_ids = [value for value in character_ids if value]

    continuity = (
        "Keep recurring characters visually identical across scenes. "
        "Preserve established face, age, gender, hairstyle, body type, "
        "clothing, accessories and carried objects. Preserve location, "
        "time progression, weather, object placement and cause-and-effect. "
        "Never change a character or event without evidence in the story."
    )

    row = {
        "part": part_number,
        "scene": local_number,
        "local_scene": local_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": identifier,
        "source_scene_id": text_value(
            source,
            "scene_id",
            default=identifier,
        ),
        "global_scene_number": global_number,
        "mapping_version": MAPPING_VERSION,
        "title": text_value(
            source,
            "title",
            "scene_title",
            default=f"Scene {global_number}",
        ),
        "role": role,
        "narration": narration,
        "narration_word_count": word_count(narration),
        "dialogue": text_value(
            source,
            "dialogue",
            "spoken_dialogue",
        ),
        "visual_prompt": visual,
        "visual": visual,
        "negative_prompt": negative,
        "duration": duration,
        "duration_seconds": duration,
        "transition": text_value(
            source,
            "transition",
            "transition_prompt",
            default=clean(settings.get("TRANSITIONS", "cinematic")),
        ),
        "camera": camera,
        "camera_prompt": camera,
        "lighting": lighting,
        "lighting_prompt": lighting,
        "mood": text_value(
            source,
            "mood",
            "emotion",
            "emotional_tone",
            default=clean(settings.get("MOOD", "dramatic")),
        ),
        "sfx": sfx,
        "sfx_prompt": sfx,
        "ambient_sound": ambient,
        "music_direction": music,
        "music_prompt": music,
        "world_context": text_value(
            source,
            "world_context",
            "location",
            "setting",
            "place",
        ),
        "characters": character_ids,
        "character_references": matched,
        "character_bible_available": bool(character_records(bible)),
        "continuity_prompt": continuity,
        "story_plan_role": text_value(plan_scene, "role"),
        "story_plan_beat": beat,
        "story_plan_purpose": purpose,
        "story_anchor": anchor,
        "story_plan_integrated": True,
        "opening_hook_required": opening_required,
        "suspense_required": suspense_required,
        "final_resolution_required": resolves_story,
        "suspense_prompt": suspense,
        "status": "completed",
    }

    # Preserve existing caption translations and other downstream fields.
    passthrough_fields = (
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
        "characters",
        "character_names",
        "character_ids",
        "location",
        "setting",
        "time_period",
        "weather",
        "props",
        "objects",
        "vehicles",
        "animals",
        "clothing",
    )

    for field in passthrough_fields:
        value = source.get(field)
        if value not in (None, "", [], {}):
            # Keep the normalized matched character IDs in "characters".
            if field == "characters":
                continue
            row[field] = value

    row["characters"] = character_ids
    row["character_references"] = matched
    return row


def validate_rows(
    rows: list[dict],
    parts_count: int,
    scenes_per_part: int,
) -> None:
    expected = [
        scene_id(part, scene)
        for part in range(1, parts_count + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    actual = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid scene record.")
        if row.get("status") != "completed":
            raise RuntimeError(
                f"Incomplete scene record: {row.get('scene_id')}"
            )
        if not clean(row.get("narration")):
            raise RuntimeError(
                f"Missing narration: {row.get('scene_id')}"
            )
        if len(clean(row.get("visual_prompt"))) < 40:
            raise RuntimeError(
                f"Missing or short visual prompt: {row.get('scene_id')}"
            )
        actual.append(row.get("scene_id"))

    if actual != expected:
        raise RuntimeError(
            "Scene IDs are missing, duplicated or out of sequence."
        )


def plan_records(plan_map: dict) -> list[dict]:
    return [
        {
            "part": part,
            "scene": scene,
            "plan": plan_map[(part, scene)],
        }
        for part, scene in sorted(plan_map)
    ]


def make_result(
    *,
    status: str,
    source_type: str,
    source_path: Path,
    plan_path: Path,
    fingerprint: str,
    format_type: str,
    title: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
    rows: list[dict],
    plan_map: dict,
    bible: dict,
    reused: int,
) -> dict:
    expected_total = parts_count * scenes_per_part
    return {
        "status": status,
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "scene_order": "part_then_scene",
        "source_type": source_type,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "story_plan_file": plan_path.relative_to(ROOT).as_posix(),
        "source_fingerprint": fingerprint,
        "model": "not_required",
        "api_calls": 0,
        "format": format_type,
        "title": title,
        "topic": title,
        "story_text": story_text,
        "total_parts": parts_count,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "completed_scenes": len(rows),
        "reused_scenes": reused,
        "character_bible_available": bool(character_records(bible)),
        "story_plan_integrated": len(plan_map) == expected_total,
        "story_plan_scene_count": len(plan_map),
        "validation": {
            "exact_scene_count": len(rows) == expected_total,
            "unique_scene_ids": len({
                row.get("scene_id") for row in rows
            }) == expected_total,
            "sequential_scene_order": True,
            "narration_present": True,
            "visual_prompts_present": True,
            "format_matches_input": True,
            "story_plan_integrated": len(plan_map) == expected_total,
            "external_api_calls": 0,
        },
        "scenes": rows,
    }


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — STORY-GROUNDED SCENE PREPARATION")
    log("External API calls: 0")
    log("=" * 64)

    config = load_and_validate(CONFIG_FILE)
    format_type = clean(get_format(config)).lower()
    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")

    configured_parts = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    story, source_type, source_path = select_story(format_type)
    parts, actual_scene_total = validate_story_layout(
        story,
        format_type,
        configured_parts,
        scenes_per_part,
    )
    parts_count = len(parts)

    plan_map, plan_path = load_story_plan(
        format_type,
        parts_count,
        scenes_per_part,
    )
    bible = load_character_bible()

    title = resolve_title(config, story)
    story_text = clean(story.get("story_text"))
    if not story_text:
        story_text = "\n\n".join(
            clean(part.get("narration"))
            or " ".join(scene_text(scene) for scene in part["scenes"])
            for part in parts
        )

    if not story_text:
        raise RuntimeError("Completed story has no complete narration text.")

    settings = dict(config)
    # SHORT parts are dynamic. All ending and resolution decisions must use
    # the actual generated part count, not Input/topic.txt PARTS.
    settings["PARTS"] = parts_count
    settings["SCENES"] = scenes_per_part

    setting_keys = (
        "PARTS",
        "SCENES",
        "FORMAT",
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
    settings_snapshot = {
        key: config.get(key) for key in setting_keys
    }

    source_fingerprint = digest({
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "source_type": source_type,
        "story": story,
        "story_plan": plan_records(plan_map),
        "title": title,
        "story_text": story_text,
        "format": format_type,
        "configured_parts": configured_parts,
        "actual_parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "character_bible": bible,
        "settings": settings_snapshot,
    })

    old = read_json(OUTPUT_FILE)
    reusable = {}

    if (
        isinstance(old, dict)
        and old.get("source_fingerprint") == source_fingerprint
        and old.get("mapping_version") == MAPPING_VERSION
        and old.get("format") == format_type
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
                reusable[key] = item

    rows = []
    reused = 0
    global_number = 0
    expected_total = parts_count * scenes_per_part

    for part_number, part in enumerate(parts, 1):
        for local_number, source_scene in enumerate(part["scenes"], 1):
            global_number += 1
            plan_scene = plan_map[(part_number, local_number)]

            expected = normalize_scene(
                source_scene,
                plan_scene,
                part_number,
                local_number,
                global_number,
                parts_count,
                settings,
                bible,
            )

            previous = reusable.get((part_number, local_number))
            if previous == expected:
                row = previous
                reused += 1
            else:
                row = expected

            rows.append(row)

            progress = make_result(
                status="in_progress",
                source_type=source_type,
                source_path=source_path,
                plan_path=plan_path,
                fingerprint=source_fingerprint,
                format_type=format_type,
                title=title,
                story_text=story_text,
                parts_count=parts_count,
                scenes_per_part=scenes_per_part,
                rows=rows,
                plan_map=plan_map,
                bible=bible,
                reused=reused,
            )
            write_json(OUTPUT_FILE, progress)

            write_json(CHECKPOINT_FILE, {
                "status": "in_progress",
                "version": VERSION,
                "mapping_version": MAPPING_VERSION,
                "source_fingerprint": source_fingerprint,
                "format": format_type,
                "total_scenes": expected_total,
                "completed_count": len(rows),
                "completed_scene_ids": [
                    item["scene_id"] for item in rows
                ],
                "failed_scene": None,
            })

    validate_rows(rows, parts_count, scenes_per_part)

    result = make_result(
        status="completed",
        source_type=source_type,
        source_path=source_path,
        plan_path=plan_path,
        fingerprint=source_fingerprint,
        format_type=format_type,
        title=title,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        rows=rows,
        plan_map=plan_map,
        bible=bible,
        reused=reused,
    )

    result["validation"] = {
        "exact_scene_count": len(rows) == expected_total,
        "unique_scene_ids": len({
            row["scene_id"] for row in rows
        }) == expected_total,
        "sequential_scene_order": True,
        "narration_present": True,
        "visual_prompts_present": True,
        "format_matches_input": True,
        "story_plan_integrated": len(plan_map) == expected_total,
        "external_api_calls": 0,
    }

    write_json(OUTPUT_FILE, result)
    write_json(CHECKPOINT_FILE, {
        "status": "completed",
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "source_fingerprint": source_fingerprint,
        "format": format_type,
        "total_scenes": expected_total,
        "completed_count": len(rows),
        "completed_scene_ids": [
            row["scene_id"] for row in rows
        ],
        "failed_scene": None,
    })

    log("FORMAT:", format_type)
    log("Configured PARTS:", configured_parts)
    log("Actual story parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", actual_scene_total)
    log("Story source:", source_path.relative_to(ROOT))
    log("Story plan:", plan_path.relative_to(ROOT))
    log("Story title:", title)
    log("Character bible records:", len(character_records(bible)))
    log("Reused identical scenes:", reused)
    log("External API calls: 0")
    log("Story narration modified: false")
    log("Output:", OUTPUT_FILE)
    log("Checkpoint:", CHECKPOINT_FILE)
    log("SCENE PREPARATION: SUCCESS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Scene preparation cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"SCENE PREPARATION ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
