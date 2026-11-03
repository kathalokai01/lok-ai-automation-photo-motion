#!/usr/bin/env python3
"""Build strictly mapped, role-aware and resumable photo-motion scenes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from input_config import (
    load_input_config,
    normalize_format,
    get_topic,
    get_story_text,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
AI_STORY_FILE = ROOT / "output" / "story" / "ai_story.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"

OUTPUT_DIR = ROOT / "output" / "scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"

VERSION = "photo-motion-scenes-v8-final-plan-role-map-fingerprint"
SCENE_PROMPT_VERSION = 8

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}
ALLOWED_ROLES = set(ROLE_ORDER)
EXTRA_ROLES = ("hook", "suspense", "resolution")

REQUIRED_SOURCE_TEXT = ("narration", "visual_prompt")
REQUIRED_OUTPUT_TEXT = (
    "narration",
    "visual_prompt",
    "negative_prompt",
    "camera_prompt",
    "lighting_prompt",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default

    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path, required: bool = False):
    path = Path(path)
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Required file missing: {path}")
        return None

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")

    return value


def write_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def config_value(config: dict, key: str, default: Any = None) -> Any:
    value = config.get(key, default)
    return default if value is None else value


def build_settings(config: dict) -> dict:
    return {
        "format": normalize_format(config),
        "audience": clean(config_value(config, "AUDIENCE", "adult")),
        "visual_style": clean(
            config_value(config, "VISUAL_STYLE", "cinematic_realistic")
        ),
        "realism": clean(config_value(config, "REALISM", "high")),
        "quality": clean(config_value(config, "QUALITY", "high")),
        "camera_style": clean(
            config_value(config, "CAMERA_STYLE", "cinematic")
        ),
        "lighting": clean(config_value(config, "LIGHTING", "cinematic")),
        "mood": clean(config_value(config, "MOOD", "dramatic")),
        "natural_motion": as_bool(
            config_value(config, "NATURAL_MOTION", True), True
        ),
        "cinematic_camera": as_bool(
            config_value(config, "CINEMATIC_CAMERA", True), True
        ),
        "realistic_lighting": as_bool(
            config_value(config, "REALISTIC_LIGHTING", True), True
        ),
        "character_bible": as_bool(
            config_value(config, "CHARACTER_BIBLE", True), True
        ),
        "character_consistency": as_bool(
            config_value(config, "CHARACTER_CONSISTENCY", True), True
        ),
        "world_consistency": as_bool(
            config_value(config, "WORLD_CONSISTENCY", True), True
        ),
        "scene_continuity": as_bool(
            config_value(config, "SCENE_CONTINUITY", True), True
        ),
        "negative_prompt": as_bool(
            config_value(config, "NEGATIVE_PROMPT", True), True
        ),
        "avoid_cartoon": as_bool(
            config_value(config, "AVOID_CARTOON_LOOK", True), True
        ),
        "avoid_neon": as_bool(
            config_value(config, "AVOID_NEON", True), True
        ),
        "avoid_glitch": as_bool(
            config_value(config, "AVOID_GLITCH_EFFECTS", True), True
        ),
        "music": as_bool(config_value(config, "MUSIC", True), True),
        "sfx": as_bool(config_value(config, "SFX", True), True),
        "ambient_sound": as_bool(
            config_value(config, "AMBIENT_SOUND", True), True
        ),
        "music_style": clean(
            config_value(config, "MUSIC_STYLE", "cinematic")
        ),
        "transitions": clean(
            config_value(config, "TRANSITIONS", "cinematic")
        ),
        "resume_enabled": as_bool(
            config_value(config, "RESUME_ENABLED", True), True
        ),
        "skip_completed": as_bool(
            config_value(config, "SKIP_COMPLETED_SCENES", True), True
        ),
        "save_checkpoint": as_bool(
            config_value(config, "SAVE_CHECKPOINT_AFTER_EACH_SCENE", True),
            True,
        ),
    }


def canonical_main_id(part_number: int, scene_number: int) -> str:
    return f"part_{part_number:02d}_scene_{scene_number:02d}"


def canonical_extra_id(part_number: int, role: str) -> str:
    return f"part_{part_number:02d}_{role}"


def validate_source_text(source: dict, scene_id: str) -> None:
    for field in REQUIRED_SOURCE_TEXT:
        if not clean(source.get(field)):
            raise RuntimeError(
                f"{scene_id}: source {field} is missing."
            )


def validate_extra_source(
    record: Any,
    role: str,
    part_number: int,
) -> None:
    if not isinstance(record, dict):
        raise RuntimeError(
            f"Part {part_number}: {role} source record is invalid."
        )

    if clean(record.get("role")).lower() != role:
        raise RuntimeError(
            f"Part {part_number}: expected role {role}, "
            f"received {clean(record.get('role'))!r}."
        )

    expected_id = canonical_extra_id(part_number, role)
    if clean(record.get("scene_id")) != expected_id:
        raise RuntimeError(
            f"Part {part_number}: {role} scene_id must be {expected_id}."
        )

    validate_source_text(record, expected_id)


def validate_story_sources(ai_story: dict):
    if ai_story.get("status") != "completed":
        raise RuntimeError(
            "ai_story.json is not completed; scene generation stopped."
        )

    mode = clean(ai_story.get("format")).lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported AI story format: {mode!r}.")

    parts = ai_story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("ai_story.json contains no parts.")

    if ai_story.get("total_parts") not in (None, len(parts)):
        raise RuntimeError("ai_story.json total_parts mismatch.")

    if ai_story.get("parts_count") not in (None, len(parts)):
        raise RuntimeError("ai_story.json parts_count mismatch.")

    expected_items = []
    seen_ids = set()
    main_total = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(
                f"Invalid or out-of-order story part: {part_number}."
            )

        main_scenes = part.get("scenes")
        if not isinstance(main_scenes, list) or not main_scenes:
            raise RuntimeError(f"Part {part_number} has no main scenes.")

        for scene_number, source in enumerate(main_scenes, 1):
            if not isinstance(source, dict):
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number} is invalid."
                )

            if source.get("scene") != scene_number:
                raise RuntimeError(
                    f"Part {part_number}: main scene numbering mismatch."
                )

            expected_id = canonical_main_id(part_number, scene_number)
            scene_id = clean(source.get("scene_id"))

            if scene_id != expected_id:
                raise RuntimeError(
                    f"Final Story Plan ID mismatch: expected "
                    f"{expected_id}, received {scene_id or 'missing'}."
                )

            if scene_id in seen_ids:
                raise RuntimeError(f"Duplicate source scene_id: {scene_id}")
            seen_ids.add(scene_id)

            if clean(source.get("role")).lower() != "main":
                raise RuntimeError(
                    f"{scene_id}: main scene role must be 'main'."
                )

            validate_source_text(source, scene_id)

            expected_items.append({
                "part": part_number,
                "scene": scene_number,
                "scene_id": scene_id,
                "role": "main",
                "part_data": part,
                "source": source,
                "narration": clean(source.get("narration")),
                "global_scene_number": (
                    source.get("global_scene_number", main_total + 1)
                ),
            })
            main_total += 1

        final_part = part_number == len(parts)

        role_fields = (
            ("hook_scene", "hook", True),
            ("suspense_scene", "suspense", not final_part),
            ("resolution_scene", "resolution", final_part),
        )

        for field, role, required in role_fields:
            record = part.get(field)

            if required and record is None:
                raise RuntimeError(
                    f"Part {part_number}: required {role} record missing."
                )

            if not required and record is not None:
                raise RuntimeError(
                    f"Part {part_number}: unexpected {role} record."
                )

            if not required:
                continue

            validate_extra_source(record, role, part_number)
            scene_id = clean(record["scene_id"])

            if scene_id in seen_ids:
                raise RuntimeError(f"Duplicate source scene_id: {scene_id}")
            seen_ids.add(scene_id)

            expected_items.append({
                "part": part_number,
                "scene": None,
                "scene_id": scene_id,
                "role": role,
                "part_data": part,
                "source": record,
                "narration": clean(record.get("narration")),
                "global_scene_number": None,
            })

    declared_main = ai_story.get("total_scenes")
    if declared_main is not None:
        try:
            declared_main = int(declared_main)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("total_scenes must be an integer.") from exc

        if declared_main != main_total:
            raise RuntimeError(
                f"Main scene total mismatch: {declared_main}/{main_total}."
            )

    declared_extra = ai_story.get("total_extra_scenes")
    if declared_extra is not None:
        try:
            declared_extra = int(declared_extra)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "total_extra_scenes must be an integer."
            ) from exc

        actual_extra = len(expected_items) - main_total
        if declared_extra != actual_extra:
            raise RuntimeError(
                f"Extra scene total mismatch: {declared_extra}/{actual_extra}."
            )

    return mode, parts, expected_items, main_total


def load_character_bible(settings: dict) -> dict:
    bible = read_json(BIBLE_FILE)

    if bible is None:
        if settings["character_bible"]:
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but character_bible.json "
                "does not exist."
            )
        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "scene_roles": [],
            "world": {},
        }

    status = clean(bible.get("status")).lower()
    if status not in {"completed", "disabled"}:
        raise RuntimeError(
            f"Character Bible is not usable: {status or 'missing'}."
        )

    if settings["character_bible"] and status != "completed":
        raise RuntimeError(
            "Character Bible is disabled although the setting is enabled."
        )

    characters = bible.get("characters", [])
    scene_map = bible.get("scene_character_map", [])
    scene_roles = bible.get("scene_roles", [])
    world = bible.get("world", {})

    if not isinstance(characters, list):
        raise RuntimeError("Character Bible characters must be an array.")
    if not isinstance(scene_map, list):
        raise RuntimeError("Character Bible scene_character_map must be an array.")
    if not isinstance(scene_roles, list):
        raise RuntimeError("Character Bible scene_roles must be an array.")
    if not isinstance(world, dict):
        raise RuntimeError("Character Bible world must be an object.")

    known_ids = set()
    known_names = set()

    for index, character in enumerate(characters, 1):
        if not isinstance(character, dict):
            raise RuntimeError(
                f"Invalid Character Bible character record #{index}."
            )

        identifier = clean(
            character.get("id") or character.get("character_id")
        )
        name = clean(character.get("name"))

        if not identifier or not name:
            raise RuntimeError(
                f"Character Bible record #{index} requires ID and name."
            )

        if identifier.casefold() in known_ids:
            raise RuntimeError(
                f"Duplicate Character Bible character ID: {identifier}"
            )
        if name.casefold() in known_names:
            raise RuntimeError(
                f"Duplicate Character Bible character name: {name}"
            )

        known_ids.add(identifier.casefold())
        known_names.add(name.casefold())

    for label, rows in (
        ("scene_character_map", scene_map),
        ("scene_roles", scene_roles),
    ):
        seen = set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Invalid Character Bible {label} row #{index}."
                )

            scene_id = clean(row.get("scene_id"))
            if not scene_id:
                raise RuntimeError(
                    f"Character Bible {label} row #{index} has no scene_id."
                )

            key = (scene_id, clean(row.get("role")).lower())
            if key in seen:
                raise RuntimeError(
                    f"Duplicate Character Bible {label} mapping: {scene_id}."
                )
            seen.add(key)

    return {
        **bible,
        "status": status,
        "characters": characters,
        "scene_character_map": scene_map,
        "scene_roles": scene_roles,
        "world": world,
    }


def normalize_names(values: Any) -> list[str]:
    if values is None:
        return []

    if not isinstance(values, list):
        values = [values]

    result = []
    seen = set()

    for value in values:
        if isinstance(value, dict):
            value = (
                value.get("id")
                or value.get("character_id")
                or value.get("name")
            )

        text = clean(value)
        key = text.casefold()

        if text and key not in seen:
            seen.add(key)
            result.append(text)

    return result


def mapping_references(row: dict) -> list[str]:
    return normalize_names(
        row.get("character_ids")
        or row.get("characters")
        or row.get("character_names")
    )


def find_scene_character_map(
    bible: dict,
    scene_id: str,
    part_number: int,
    scene_number: int | None,
    role: str,
) -> list[str]:
    # Prefer exact scene ID; role-specific mappings are preferred.
    rows = bible.get("scene_roles", []) + bible.get(
        "scene_character_map", []
    )

    exact = [
        row for row in rows
        if clean(row.get("scene_id")) == scene_id
    ]

    role_match = [
        row for row in exact
        if clean(row.get("role")).lower() == role
    ]
    if role_match:
        return mapping_references(role_match[0])

    if exact:
        return mapping_references(exact[0])

    # Legacy positional mapping is valid only for numbered main scenes.
    if role == "main" and scene_number is not None:
        for row in bible.get("scene_character_map", []):
            try:
                row_part = int(row.get("part", -1))
                row_scene = int(row.get("scene", -1))
            except (TypeError, ValueError):
                continue

            if row_part == part_number and row_scene == scene_number:
                return mapping_references(row)

    return []


def find_character_records(
    bible: dict,
    references: list[str],
    scene_id: str,
) -> list[dict]:
    if not references:
        return []

    wanted = {item.casefold() for item in references}
    matches = []
    matched = set()

    for character in bible.get("characters", []):
        candidates = {
            clean(character.get("id")).casefold(),
            clean(character.get("character_id")).casefold(),
            clean(character.get("name")).casefold(),
        }
        candidates.update(
            item.casefold()
            for item in normalize_names(character.get("aliases"))
        )
        candidates.discard("")

        if wanted.intersection(candidates):
            matches.append(character)
            matched.update(wanted.intersection(candidates))

    unresolved = wanted - matched
    if unresolved:
        raise RuntimeError(
            f"{scene_id}: Character Bible references not found: "
            + ", ".join(sorted(unresolved))
        )

    return matches


def character_summary(records: list[dict]) -> list[dict]:
    fields = (
        "id", "character_id", "name", "aliases", "age", "gender",
        "appearance", "face", "face_features", "skin_tone", "hair",
        "eyes", "body_features", "height", "build", "clothing",
        "accessories", "distinctive_features", "personality",
        "visual_identity", "continuity_notes", "consistency_rules",
        "description",
    )

    result = []
    for item in records:
        summary = {
            key: item[key]
            for key in fields
            if key in item and item[key] not in (None, "", [], {})
        }
        if summary:
            result.append(summary)

    return result


def build_negative_prompt(source: dict, settings: dict) -> str:
    values = []

    if settings["negative_prompt"]:
        values.extend([
            "distorted face",
            "unrealistic anatomy",
            "extra fingers",
            "missing fingers",
            "fused limbs",
            "duplicated people",
            "plastic skin",
            "waxy face",
            "deformed hands",
            "floating objects",
            "wrong story event",
            "unrelated characters",
            "unrelated props",
            "wrong location",
            "wrong time period",
            "unintended text",
            "captions",
            "subtitles",
            "watermark",
            "logo",
        ])

        if settings["avoid_cartoon"]:
            values.extend([
                "cartoon", "anime", "comic-book illustration",
                "2D drawing", "3D animated character", "painting",
            ])
        if settings["avoid_neon"]:
            values.extend([
                "unnecessary neon", "artificial neon glow",
            ])
        if settings["avoid_glitch"]:
            values.extend([
                "glitch effect", "digital distortion", "frame tearing",
            ])

    original = clean(source.get("negative_prompt"))
    if original:
        values.append(original)

    unique = []
    seen = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)

    return ", ".join(unique)


def build_visual_prompt(
    source: dict,
    settings: dict,
    character_records: list[dict],
    world: dict,
    previous_scene: dict | None,
    role: str,
) -> str:
    original = clean(source.get("visual_prompt"))
    if len(original) < 24:
        raise RuntimeError(
            f"{role} scene visual_prompt is missing or too short."
        )

    pieces = [
        "STORY-SPECIFIC VISUAL EVENT: " + original,
        (
            "Photorealistic live-action cinematic film still. "
            f"Visual style: {settings['visual_style']}; "
            f"realism: {settings['realism']}; "
            f"quality: {settings['quality']}."
        ),
    ]

    for key, label in (
        ("action", "Visible physical action"),
        ("location", "Exact story location"),
        ("world_context", "Story-world context"),
        ("time_period", "Story time period"),
        ("objects", "Story-specific objects"),
        ("clothing", "Story-specific clothing"),
        ("camera_prompt", "Camera direction"),
        ("lighting_prompt", "Lighting direction"),
        ("dialogue", "Dialogue context"),
    ):
        value = source.get(key)
        if isinstance(value, list):
            value = ", ".join(clean(item) for item in value if clean(item))
        value = clean(value)
        if value:
            pieces.append(f"{label}: {value}")

    mood = clean(source.get("mood")) or settings["mood"]
    pieces.append("Emotional tone: " + mood)

    if settings["cinematic_camera"] and not clean(
        source.get("camera_prompt") or source.get("camera")
    ):
        pieces.append(
            "Use intentional cinematic framing and a physically plausible "
            "camera perspective."
        )

    if settings["realistic_lighting"] and not clean(
        source.get("lighting_prompt") or source.get("lighting")
    ):
        pieces.append(
            "Use motivated realistic light, natural skin tones and "
            "physically plausible shadows."
        )

    if settings["character_consistency"] and character_records:
        pieces.append(
            "CHARACTER IDENTITY LOCK. Preserve the exact established "
            "character face, age, gender, skin tone, hair, eyes, body "
            "proportions, distinctive features and clothing continuity. "
            "Do not swap, merge or redesign characters. Approved records: "
            + json.dumps(
                character_summary(character_records),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    if settings["world_consistency"] and world:
        pieces.append(
            "WORLD CONTINUITY LOCK. Preserve established era, location, "
            "architecture, environment and visual rules. Apply only "
            "scene-relevant details: "
            + json.dumps(world, ensure_ascii=False, separators=(",", ":"))
        )

    if settings["scene_continuity"] and previous_scene:
        continuity = []

        previous_location = clean(previous_scene.get("world_context"))
        previous_characters = normalize_names(
            previous_scene.get("character_ids", [])
        )

        if previous_location:
            continuity.append("Previous location: " + previous_location)
        if previous_characters:
            continuity.append(
                "Recurring identities to preserve: "
                + ", ".join(previous_characters)
            )

        if continuity:
            pieces.append("Chronological continuity: " + " ".join(continuity))

    if settings["natural_motion"]:
        pieces.append(
            "Depict believable posture, natural gestures and realistic "
            "interaction with story objects. This is a still image, not "
            "a request for character animation."
        )

    pieces.append(
        "Show only the approved story event. Do not add unrelated "
        "people, objects, locations, vehicles, animals or events. "
        "Do not render captions, subtitles, labels, logos or text. "
        "Keep important subjects and objects fully visible with safe "
        "margins for later photo-motion crop and pan."
    )

    return "\n".join(pieces).strip()


def build_scene_record(
    item: dict,
    settings: dict,
    bible: dict,
    previous_scene: dict | None,
) -> dict:
    source = item["source"]
    part_number = item["part"]
    scene_number = item["scene"]
    role = item["role"]
    scene_id = item["scene_id"]

    explicit_refs = normalize_names(
        source.get("character_ids")
        or source.get("characters")
        or source.get("character_names")
    )

    mapped_refs = find_scene_character_map(
        bible,
        scene_id,
        part_number,
        scene_number,
        role,
    )

    references = explicit_refs or mapped_refs
    character_records = find_character_records(
        bible, references, scene_id
    )

    world = bible.get("world", {})
    visual_prompt = build_visual_prompt(
        source=source,
        settings=settings,
        character_records=character_records,
        world=world,
        previous_scene=previous_scene,
        role=role,
    )

    camera_prompt = clean(
        source.get("camera_prompt")
        or source.get("camera")
        or settings["camera_style"]
    )
    lighting_prompt = clean(
        source.get("lighting_prompt")
        or source.get("lighting")
        or settings["lighting"]
    )

    duration = (
        source.get("scene_duration")
        or source.get("duration")
        or item["part_data"].get("scene_duration")
        or "auto"
    )

    sfx = (
        clean(source.get("sfx_prompt") or source.get("sfx"))
        if settings["sfx"] else ""
    )
    ambient = (
        clean(
            source.get("ambient_sound")
            or source.get("ambient_prompt")
        )
        if settings["ambient_sound"] else ""
    )
    music = (
        clean(
            source.get("music_prompt")
            or source.get("music_direction")
        )
        if settings["music"] else ""
    )

    title = clean(source.get("title"))
    if not title:
        title = (
            f"Part {part_number} {role.title()}"
            if role != "main"
            else f"Part {part_number} Scene {scene_number}"
        )

    source_signature = digest({
        "source": source,
        "part": part_number,
        "scene": scene_number,
        "role": role,
        "scene_prompt_version": SCENE_PROMPT_VERSION,
    })

    return {
        "part": part_number,
        "scene": scene_number,
        "local_scene": scene_number,
        "scene_id": scene_id,
        "id": scene_id,
        "role": role,
        "scene_type": role,
        "is_extra_scene": role != "main",
        "global_scene_number": item["global_scene_number"],
        "title": title,
        "status": "completed",
        "narration": item["narration"],
        "dialogue": clean(source.get("dialogue")),
        "visual_prompt": visual_prompt,
        "negative_prompt": build_negative_prompt(source, settings),
        "camera_prompt": camera_prompt,
        "lighting_prompt": lighting_prompt,
        "camera": camera_prompt,
        "lighting": lighting_prompt,
        "mood": clean(source.get("mood")) or settings["mood"],
        "duration": str(duration),
        "scene_duration": str(duration),
        "transition": clean(
            source.get("transition") or settings["transitions"]
        ),
        "character_ids": references,
        "characters": references,
        "character_details": character_summary(character_records),
        "world_context": clean(
            source.get("world_context") or source.get("location")
        ),
        "sfx_prompt": sfx,
        "ambient_sound": ambient,
        "music_prompt": music,
        "music_direction": music,
        "generation_method": "offline_story_prompt_enrichment",
        "source_scene_number": scene_number,
        "source_scene_id": scene_id,
        "source_role": role,
        "source_scene_signature": source_signature,
        "scene_prompt_version": SCENE_PROMPT_VERSION,
    }

def scene_order_key(scene: dict) -> tuple:
    role = clean(scene.get("role")).lower()
    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported scene role: {role!r}")

    try:
        part = int(scene.get("part"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Scene has an invalid part number.") from exc

    raw_scene = scene.get("scene")
    try:
        local_scene = int(raw_scene) if raw_scene is not None else 0
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Scene has an invalid local scene number.") from exc

    return (
        part,
        ROLE_ORDER[role],
        local_scene,
        clean(scene.get("scene_id")),
    )


def expected_scene_signature(item: dict) -> str:
    return digest({
        "source": item["source"],
        "part": item["part"],
        "scene": item["scene"],
        "role": item["role"],
        "scene_prompt_version": SCENE_PROMPT_VERSION,
    })


def scene_is_valid(
    scene: Any,
    expected: dict,
    generation_signature: str,
) -> bool:
    if not isinstance(scene, dict):
        return False

    if scene.get("status") != "completed":
        return False

    if scene.get("part") != expected["part"]:
        return False
    if scene.get("scene") != expected["scene"]:
        return False
    if clean(scene.get("scene_id")) != expected["scene_id"]:
        return False
    if clean(scene.get("role")).lower() != expected["role"]:
        return False
    if scene.get("is_extra_scene") != (expected["role"] != "main"):
        return False
    if clean(scene.get("narration")) != expected["narration"]:
        return False
    if scene.get("source_scene_id") != expected["scene_id"]:
        return False
    if scene.get("source_role") != expected["role"]:
        return False
    if scene.get("source_scene_signature") != expected_scene_signature(
        expected
    ):
        return False
    if scene.get("generation_signature") != generation_signature:
        return False
    if scene.get("scene_prompt_version") != SCENE_PROMPT_VERSION:
        return False

    for field in REQUIRED_OUTPUT_TEXT:
        if not clean(scene.get(field)):
            return False

    if expected["role"] == "main":
        if scene.get("scene") != expected["scene"]:
            return False
    elif scene.get("scene") is not None:
        return False

    return True


def load_existing_scenes(
    generation_signature: str,
    settings: dict,
) -> dict:
    if not settings["resume_enabled"]:
        return {}

    data = read_json(OUTPUT_FILE)
    if not isinstance(data, dict):
        return {}

    if data.get("version") != VERSION:
        log("Old scene version detected; old scene records will not be reused.")
        return {}

    if data.get("scene_prompt_version") != SCENE_PROMPT_VERSION:
        return {}

    if data.get("generation_signature") != generation_signature:
        log("Scene fingerprint changed; previous scene records are invalid.")
        return {}

    rows = data.get("scenes")
    if not isinstance(rows, list):
        return {}

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue

        scene_id = clean(row.get("scene_id"))
        if not scene_id:
            continue

        if scene_id in result:
            raise RuntimeError(
                f"Duplicate scene_id in existing scenes.json: {scene_id}"
            )

        result[scene_id] = row

    return result


def load_checkpoint(generation_signature: str, settings: dict) -> set:
    if not settings["resume_enabled"]:
        return set()

    data = read_json(CHECKPOINT_FILE)
    if not isinstance(data, dict):
        return set()

    if data.get("version") != VERSION:
        return set()

    if data.get("generation_signature") != generation_signature:
        return set()

    values = data.get("completed_scene_ids", [])
    if not isinstance(values, list):
        return set()

    return {
        clean(value)
        for value in values
        if clean(value)
    }


def save_checkpoint(
    generation_signature: str,
    completed_scenes: dict,
    expected_items: list,
) -> None:
    expected_ids = {
        item["scene_id"] for item in expected_items
    }
    completed_ids = sorted(
        scene_id
        for scene_id, scene in completed_scenes.items()
        if scene_id in expected_ids
        and isinstance(scene, dict)
        and scene.get("status") == "completed"
        and scene.get("generation_signature") == generation_signature
    )

    total = len(expected_items)
    write_json(
        CHECKPOINT_FILE,
        {
            "version": VERSION,
            "status": (
                "completed" if len(completed_ids) == total
                else "in_progress"
            ),
            "generation_signature": generation_signature,
            "total_scenes": total,
            "completed_scene_ids": completed_ids,
            "completed_count": len(completed_ids),
            "remaining_scenes": max(0, total - len(completed_ids)),
            "updated_at": int(time.time()),
        },
    )


def validate_role_counts(
    scenes: list,
    parts: list,
    expected_items: list,
) -> dict:
    expected_counts = {}
    actual_counts = {}

    for part in parts:
        number = part["part"]
        expected_counts[number] = {
            role: 0 for role in ROLE_ORDER
        }
        actual_counts[number] = {
            role: 0 for role in ROLE_ORDER
        }

    for item in expected_items:
        expected_counts[item["part"]][item["role"]] += 1

    for scene in scenes:
        role = clean(scene.get("role")).lower()
        part = scene.get("part")

        if role not in ALLOWED_ROLES:
            raise RuntimeError(
                f"Unsupported final scene role: {role!r}."
            )
        if part not in actual_counts:
            raise RuntimeError(
                f"Scene belongs to an unexpected part: {part!r}."
            )

        actual_counts[part][role] += 1

    if actual_counts != expected_counts:
        raise RuntimeError(
            "Final per-part role counts differ from the AI Story Plan. "
            f"Expected={expected_counts}; actual={actual_counts}"
        )

    for part_number in range(1, len(parts) + 1):
        counts = actual_counts[part_number]

        if counts["hook"] != 1:
            raise RuntimeError(
                f"Part {part_number} must contain exactly one Hook."
            )

        if counts["main"] < 1:
            raise RuntimeError(
                f"Part {part_number} must contain at least one main scene."
            )

        if part_number < len(parts):
            if counts["suspense"] != 1:
                raise RuntimeError(
                    f"Non-final part {part_number} must contain "
                    "exactly one Suspense."
                )
            if counts["resolution"] != 0:
                raise RuntimeError(
                    f"Non-final part {part_number} cannot contain Resolution."
                )
        else:
            if counts["suspense"] != 0:
                raise RuntimeError(
                    "Final part cannot contain a Suspense record."
                )
            if counts["resolution"] != 1:
                raise RuntimeError(
                    "Final part must contain exactly one Resolution."
                )

    return actual_counts


def validate_final_scenes(
    scenes: list,
    expected_items: list,
    parts: list,
    generation_signature: str,
) -> dict:
    expected_by_id = {
        item["scene_id"]: item
        for item in expected_items
    }

    if len(expected_by_id) != len(expected_items):
        raise RuntimeError("Final Story Plan contains duplicate scene IDs.")

    actual_ids = set()

    for scene in scenes:
        if not isinstance(scene, dict):
            raise RuntimeError("Output contains an invalid scene record.")

        scene_id = clean(scene.get("scene_id"))
        if not scene_id:
            raise RuntimeError("Output scene is missing scene_id.")

        if scene_id in actual_ids:
            raise RuntimeError(
                f"Duplicate scene_id in output: {scene_id}"
            )

        if scene_id not in expected_by_id:
            raise RuntimeError(
                f"Scene not present in final Story Plan: {scene_id}"
            )

        actual_ids.add(scene_id)
        expected = expected_by_id[scene_id]

        if not scene_is_valid(
            scene,
            expected,
            generation_signature,
        ):
            raise RuntimeError(
                f"Scene validation failed against final Story Plan: "
                f"{scene_id}"
            )

        if scene.get("is_extra_scene") != (
            expected["role"] != "main"
        ):
            raise RuntimeError(
                f"Extra-scene flag mismatch: {scene_id}"
            )

        if expected["role"] == "main":
            if scene.get("scene") != expected["scene"]:
                raise RuntimeError(
                    f"Main scene number changed: {scene_id}"
                )
        elif scene.get("scene") is not None:
            raise RuntimeError(
                f"Extra role scene must not use a main scene number: "
                f"{scene_id}"
            )

        if clean(scene.get("narration")) != expected["narration"]:
            raise RuntimeError(
                f"Narration/source mapping mismatch: {scene_id}"
            )

    missing = sorted(set(expected_by_id) - actual_ids)
    if missing:
        raise RuntimeError(
            "Missing final scene records: " + ", ".join(missing[:50])
        )

    if len(scenes) != len(expected_items):
        raise RuntimeError(
            f"Scene count mismatch: {len(scenes)}/{len(expected_items)}."
        )

    ordered = sorted(scenes, key=scene_order_key)
    if scenes != ordered:
        raise RuntimeError(
            "Scenes are not ordered by part, role and scene number."
        )

    return validate_role_counts(scenes, parts, expected_items)


def build_output_metadata(
    ai_story: dict,
    settings: dict,
    bible: dict,
    generation_signature: str,
    scenes: list,
    status: str,
    main_scene_total: int,
    role_counts: dict | None = None,
) -> dict:
    parts = ai_story.get("parts", [])

    if role_counts is None:
        role_counts = {
            role: sum(
                1 for item in scenes
                if item.get("role") == role
            )
            for role in ROLE_ORDER
        }

    part_role_counts = {
        str(part_number): {
            role: sum(
                1 for item in scenes
                if item.get("part") == part_number
                and item.get("role") == role
            )
            for role in ROLE_ORDER
        }
        for part_number in range(1, len(parts) + 1)
    }

    return {
        "version": VERSION,
        "scene_prompt_version": SCENE_PROMPT_VERSION,
        "status": status,
        "format": settings["format"],
        "title": clean(ai_story.get("title")),
        "topic": clean(ai_story.get("topic")),
        "model": clean(ai_story.get("model")),
        "generation_method": "offline_story_prompt_enrichment",
        "generation_signature": generation_signature,
        "source_story_file": str(AI_STORY_FILE.relative_to(ROOT)),
        "source_story_signature": digest(ai_story),
        "character_bible_status": bible.get("status", "unknown"),
        "character_bible_signature": digest(bible),
        "total_parts": len(parts),
        "total_scenes": len(scenes),
        "total_main_scenes": main_scene_total,
        "total_extra_scenes": len(scenes) - main_scene_total,
        "scene_role_counts": role_counts,
        "part_role_counts": part_role_counts,
        "completed_scenes": sum(
            1 for item in scenes
            if item.get("status") == "completed"
        ),
        "scene_order": (
            "part_then_hook_then_main_then_suspense_or_resolution"
        ),
        "input_config": settings,
        "scenes": scenes,
    }


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — PHOTO MOTION SCENE BUILDER")
    log("=" * 64)

    config = load_input_config(str(INPUT_FILE))
    if not isinstance(config, dict):
        raise RuntimeError("Input configuration is invalid.")

    settings = build_settings(config)
    ai_story = read_json(AI_STORY_FILE, required=True)

    mode, parts, expected_items, main_scene_total = (
        validate_story_sources(ai_story)
    )

    if mode != settings["format"]:
        raise RuntimeError(
            "FORMAT mismatch between Input/topic.txt and ai_story.json: "
            f"{settings['format']} != {mode}."
        )

    bible = load_character_bible(settings)

    topic = (
        clean(get_topic(config))
        or clean(ai_story.get("topic"))
        or clean(ai_story.get("title"))
    )

    input_story_text = clean(get_story_text(config))
    story_text = (
        clean(ai_story.get("story_text"))
        or clean(ai_story.get("main_story_text"))
        or input_story_text
    )

    if not story_text:
        raise RuntimeError(
            "Story text is missing; scene generation cannot continue."
        )

    generation_signature = digest({
        "version": VERSION,
        "scene_prompt_version": SCENE_PROMPT_VERSION,
        "format": mode,
        "topic": topic,
        "input_story_text": input_story_text,
        "ai_story_text": story_text,
        "ai_story": ai_story,
        "character_bible": bible,
        "settings": settings,
    })

    total_parts = len(parts)
    total_records = len(expected_items)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing_scenes(
        generation_signature,
        settings,
    )
    checkpoint_ids = load_checkpoint(
        generation_signature,
        settings,
    )

    expected_by_id = {
        item["scene_id"]: item
        for item in expected_items
    }

    completed = {}

    for scene_id, item in expected_by_id.items():
        cached = existing.get(scene_id)

        if not settings["skip_completed"]:
            continue

        if not scene_is_valid(
            cached,
            item,
            generation_signature,
        ):
            continue

        # A checkpoint entry alone is never enough to reuse a scene.
        # The complete cached record must pass all source/fingerprint checks.
        if scene_id in checkpoint_ids or scene_id in existing:
            completed[scene_id] = cached

    log(f"Format                  : {mode}")
    log(f"Parts                   : {total_parts}")
    log(f"Main scenes             : {main_scene_total}")
    log(f"Extra scenes            : {total_records - main_scene_total}")
    log(f"All scene records       : {total_records}")
    log(f"Character Bible         : {bible['status']}")
    log(f"Reusable completed      : {len(completed)}/{total_records}")
    log("Scene generation mode   : OFFLINE")
    log("Additional Gemini calls : 0")
    log("=" * 64)

    for index, item in enumerate(expected_items, 1):
        scene_id = item["scene_id"]

        if scene_id in completed:
            log(
                f"[{index}/{total_records}] "
                f"{scene_id}: valid checkpoint reused."
            )
            continue

        current_key = (
            item["part"],
            ROLE_ORDER[item["role"]],
            int(item["scene"] or 0),
            scene_id,
        )

        earlier = [
            scene for scene in completed.values()
            if scene_order_key(scene) < current_key
        ]
        earlier.sort(key=scene_order_key)
        previous_scene = earlier[-1] if earlier else None

        scene = build_scene_record(
            item=item,
            settings=settings,
            bible=bible,
            previous_scene=previous_scene,
        )
        scene["generation_signature"] = generation_signature

        if scene["narration"] != item["narration"]:
            raise RuntimeError(
                f"Narration changed unexpectedly: {scene_id}"
            )

        if not scene["visual_prompt"]:
            raise RuntimeError(f"Empty visual prompt: {scene_id}")

        if scene["scene_id"] != scene_id:
            raise RuntimeError(
                f"Scene ID changed unexpectedly: {scene_id}"
            )

        if scene["source_scene_signature"] != expected_scene_signature(item):
            raise RuntimeError(
                f"Source fingerprint mismatch: {scene_id}"
            )

        completed[scene_id] = scene
        ordered_completed = sorted(
            completed.values(),
            key=scene_order_key,
        )

        partial_output = build_output_metadata(
            ai_story=ai_story,
            settings=settings,
            bible=bible,
            generation_signature=generation_signature,
            scenes=ordered_completed,
            status="in_progress",
            main_scene_total=main_scene_total,
        )
        write_json(OUTPUT_FILE, partial_output)

        if settings["save_checkpoint"]:
            save_checkpoint(
                generation_signature,
                completed,
                expected_items,
            )

        log(
            f"[{index}/{total_records}] "
            f"Saved {scene_id} ({item['role']})."
        )

    final_scenes = sorted(
        completed.values(),
        key=scene_order_key,
    )

    final_role_counts = validate_final_scenes(
        final_scenes,
        expected_items,
        parts,
        generation_signature,
    )

    final_output = build_output_metadata(
        ai_story=ai_story,
        settings=settings,
        bible=bible,
        generation_signature=generation_signature,
        scenes=final_scenes,
        status="completed",
        main_scene_total=main_scene_total,
        role_counts={
            role: sum(
                final_role_counts[part][role]
                for part in final_role_counts
            )
            for role in ROLE_ORDER
        },
    )
    final_output["completed_at"] = int(time.time())

    write_json(OUTPUT_FILE, final_output)
    save_checkpoint(
        generation_signature,
        completed,
        expected_items,
    )

    # Read-after-write verification: do not report completion based only
    # on in-memory objects.
    written = read_json(OUTPUT_FILE, required=True)
    if written.get("status") != "completed":
        raise RuntimeError("Final scenes.json status verification failed.")

    if written.get("generation_signature") != generation_signature:
        raise RuntimeError("Final scenes.json fingerprint verification failed.")

    written_scenes = written.get("scenes")
    if not isinstance(written_scenes, list):
        raise RuntimeError("Final scenes.json has no scene array.")

    validate_final_scenes(
        written_scenes,
        expected_items,
        parts,
        generation_signature,
    )

    written_checkpoint = read_json(CHECKPOINT_FILE, required=True)
    if written_checkpoint.get("status") != "completed":
        raise RuntimeError("Scene checkpoint is not completed.")

    if written_checkpoint.get("generation_signature") != generation_signature:
        raise RuntimeError("Scene checkpoint fingerprint mismatch.")

    if written_checkpoint.get("completed_count") != total_records:
        raise RuntimeError("Scene checkpoint completed count mismatch.")

    log("=" * 64)
    log("SCENE GENERATION COMPLETED")
    log(f"Parts                   : {total_parts}")
    log(f"Main scenes             : {main_scene_total}")
    log(f"Extra scenes            : {total_records - main_scene_total}")
    log(f"All scene records       : {len(final_scenes)}/{total_records}")

    for role in ("hook", "main", "suspense", "resolution"):
        log(
            f"{role.upper():23}: "
            f"{final_output['scene_role_counts'][role]}"
        )

    log("Scene generation mode   : OFFLINE")
    log("Additional Gemini calls : 0")
    log(f"Output                  : {OUTPUT_FILE.relative_to(ROOT)}")
    log(f"Checkpoint              : {CHECKPOINT_FILE.relative_to(ROOT)}")
    log("=" * 64)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log(
            "Scene generation interrupted. "
            "Completed valid checkpoints are preserved."
        )
        sys.exit(130)
    except Exception as exc:
        log(
            f"SCENE GENERATION FAILED: "
            f"{type(exc).__name__}: {exc}"
        )
        sys.exit(1)