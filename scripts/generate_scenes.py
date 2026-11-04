#!/usr/bin/env python3
"""Build validated, story-grounded and resumable photo-motion scenes."""

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
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_input_config,
    normalize_format,
    get_topic,
    get_story_text,
    get_scenes_range,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "ai_story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"

OUTPUT_DIR = ROOT / "output" / "scenes"
OUTPUT_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"

VERSION = "photo-motion-scenes-v9-variable-range-story-anchor-continuity"
PROMPT_VERSION = 9

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}
ALLOWED_ROLES = set(ROLE_ORDER)

REQUIRED_SOURCE_FIELDS = ("narration", "visual_prompt")
REQUIRED_OUTPUT_FIELDS = (
    "narration",
    "visual_prompt",
    "negative_prompt",
    "camera_prompt",
    "lighting_prompt",
)

DANGLING_ENDINGS = {
    "और", "या", "लेकिन", "मगर", "क्योंकि", "कि", "जो",
    "जिससे", "जिसका", "जिसकी", "जिसके", "जब", "जहाँ",
    "जहां", "ताकि", "से", "को", "का", "की", "के",
    "में", "पर", "तक", "लिए", "साथ", "अथवा", "तथा",
}

DANGLING_STARTERS = {
    "और", "या", "लेकिन", "मगर", "क्योंकि", "इसलिए",
    "तथा", "अथवा", "कि", "जो", "जिससे", "जिसका",
    "जिसकी", "जिसके", "जब", "जहाँ", "जहां", "ताकि",
}


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
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "n", "off", "disabled"}:
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


def read_json(path: Path, required: bool = False) -> dict | None:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")

    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_settings(config: dict) -> dict:
    return {
        "format": normalize_format(config),
        "audience": clean(config.get("AUDIENCE", "adult")),
        "visual_style": clean(
            config.get("VISUAL_STYLE", "cinematic_realistic")
        ),
        "realism": clean(config.get("REALISM", "high")),
        "quality": clean(config.get("QUALITY", "high")),
        "camera_style": clean(config.get("CAMERA_STYLE", "cinematic")),
        "lighting": clean(config.get("LIGHTING", "cinematic")),
        "mood": clean(config.get("MOOD", "dramatic")),
        "natural_motion": as_bool(config.get("NATURAL_MOTION"), True),
        "cinematic_camera": as_bool(config.get("CINEMATIC_CAMERA"), True),
        "realistic_lighting": as_bool(config.get("REALISTIC_LIGHTING"), True),
        "character_bible": as_bool(config.get("CHARACTER_BIBLE"), True),
        "character_consistency": as_bool(
            config.get("CHARACTER_CONSISTENCY"), True
        ),
        "world_consistency": as_bool(
            config.get("WORLD_CONSISTENCY"), True
        ),
        "scene_continuity": as_bool(
            config.get("SCENE_CONTINUITY"), True
        ),
        "negative_prompt": as_bool(config.get("NEGATIVE_PROMPT"), True),
        "avoid_cartoon": as_bool(config.get("AVOID_CARTOON_LOOK"), True),
        "avoid_neon": as_bool(config.get("AVOID_NEON"), True),
        "avoid_glitch": as_bool(config.get("AVOID_GLITCH_EFFECTS"), True),
        "music": as_bool(config.get("MUSIC"), True),
        "sfx": as_bool(config.get("SFX"), True),
        "ambient_sound": as_bool(config.get("AMBIENT_SOUND"), True),
        "music_style": clean(config.get("MUSIC_STYLE", "cinematic")),
        "transitions": clean(config.get("TRANSITIONS", "cinematic")),
        "resume_enabled": as_bool(config.get("RESUME_ENABLED"), True),
        "skip_completed": as_bool(
            config.get("SKIP_COMPLETED_SCENES"), True
        ),
        "save_checkpoint": as_bool(
            config.get("SAVE_CHECKPOINT_AFTER_EACH_SCENE"), True
        ),
    }


def canonical_main_id(part: int, scene: int) -> str:
    return f"part_{part:02d}_scene_{scene:02d}"


def canonical_role_id(part: int, role: str) -> str:
    return f"part_{part:02d}_{role}"


def validate_required_text(record: dict, scene_id: str) -> None:
    for field in REQUIRED_SOURCE_FIELDS:
        if not clean(record.get(field)):
            raise RuntimeError(
                f"{scene_id}: source field {field} is missing."
            )


def validate_sentence_boundary(text: str, scene_id: str) -> None:
    value = clean(text)
    if not value:
        raise RuntimeError(f"{scene_id}: narration is empty.")

    tokens = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    if not tokens:
        raise RuntimeError(f"{scene_id}: narration has no readable words.")

    last = tokens[-1].casefold()
    first = tokens[0].casefold()

    if last in DANGLING_ENDINGS:
        raise RuntimeError(
            f"{scene_id}: narration ends with an incomplete connector "
            f"({last}). Complete the sentence or meaningful clause."
        )

    if first in DANGLING_STARTERS:
        raise RuntimeError(
            f"{scene_id}: narration starts with a dangling connector "
            f"({first})."
        )


def validate_source_record(
    record: Any,
    scene_id: str,
    expected_role: str,
) -> None:
    if not isinstance(record, dict):
        raise RuntimeError(f"{scene_id}: source record is invalid.")

    if clean(record.get("scene_id")) != scene_id:
        raise RuntimeError(f"{scene_id}: source scene_id mismatch.")

    if clean(record.get("role")).lower() != expected_role:
        raise RuntimeError(f"{scene_id}: source role mismatch.")

    validate_required_text(record, scene_id)
    validate_sentence_boundary(record["narration"], scene_id)


def validate_plan(
    plan: dict,
    mode: str,
    parts: list,
    scene_range: tuple[int, int],
) -> None:
    if clean(plan.get("status")).lower() not in {
        "completed", "planned", "in_progress"
    }:
        raise RuntimeError("story_plan.json has an invalid status.")

    if clean(plan.get("format")).lower() != mode:
        raise RuntimeError("Story Plan format does not match Input/topic.txt.")

    plan_parts = plan.get("parts")
    if not isinstance(plan_parts, list) or len(plan_parts) != len(parts):
        raise RuntimeError("Story Plan and AI Story Part counts differ.")

    minimum, maximum = scene_range
    seen_ids = set()

    for number, (plan_part, story_part) in enumerate(
        zip(plan_parts, parts), 1
    ):
        if not isinstance(plan_part, dict):
            raise RuntimeError(f"Story Plan Part {number} is invalid.")

        if plan_part.get("part") != number:
            raise RuntimeError(f"Story Plan Part order mismatch: {number}.")

        plan_scenes = plan_part.get("scenes")
        story_scenes = story_part.get("scenes")

        if not isinstance(plan_scenes, list) or not isinstance(story_scenes, list):
            raise RuntimeError(f"Part {number}: main scenes are missing.")

        if len(plan_scenes) != len(story_scenes):
            raise RuntimeError(
                f"Part {number}: AI Story scene count differs from Story Plan."
            )

        if not minimum <= len(plan_scenes) <= maximum:
            raise RuntimeError(
                f"Part {number}: actual scene count {len(plan_scenes)} "
                f"is outside configured SCENES range {minimum}-{maximum}."
            )

        for scene_number, (source, generated) in enumerate(
            zip(plan_scenes, story_scenes), 1
        ):
            scene_id = canonical_main_id(number, scene_number)

            if clean(source.get("scene_id")) != scene_id:
                raise RuntimeError(
                    f"Story Plan scene ID mismatch: {scene_id}."
                )

            if clean(generated.get("scene_id")) != scene_id:
                raise RuntimeError(
                    f"AI Story scene ID mismatch: {scene_id}."
                )

            if source.get("scene") != scene_number:
                raise RuntimeError(f"Story Plan scene order mismatch: {scene_id}.")

            if generated.get("scene") != scene_number:
                raise RuntimeError(f"AI Story scene order mismatch: {scene_id}.")

            if clean(generated.get("role")).lower() != "main":
                raise RuntimeError(f"{scene_id}: role must be main.")

            anchor = clean(source.get("story_anchor"))
            generated_anchor = clean(generated.get("story_anchor"))

            if anchor and generated_anchor and anchor != generated_anchor:
                raise RuntimeError(
                    f"{scene_id}: story_anchor changed from Story Plan."
                )

            for field in ("beat", "purpose"):
                source_value = clean(source.get(field))
                generated_value = clean(generated.get(field))
                if (
                    source_value and generated_value
                    and source_value != generated_value
                ):
                    raise RuntimeError(
                        f"{scene_id}: {field} changed from Story Plan."
                    )

            validate_source_record(generated, scene_id, "main")

            if scene_id in seen_ids:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}.")
            seen_ids.add(scene_id)

        final_part = number == len(parts)

        role_specs = (
            ("hook_scene", "hook", True),
            ("suspense_scene", "suspense", not final_part),
            ("resolution_scene", "resolution", final_part),
        )

        for field, role, required in role_specs:
            source_role = plan_part.get(field)
            generated_role = story_part.get(field)

            if required:
                if not isinstance(source_role, dict):
                    raise RuntimeError(
                        f"Part {number}: Story Plan {role} is missing."
                    )
                if not isinstance(generated_role, dict):
                    raise RuntimeError(
                        f"Part {number}: AI Story {role} is missing."
                    )
            else:
                if source_role is not None or generated_role is not None:
                    raise RuntimeError(
                        f"Part {number}: unexpected {role} record."
                    )
                continue

            expected_id = canonical_role_id(number, role)
            validate_source_record(source_role, expected_id, role)
            validate_source_record(generated_role, expected_id, role)

            for field in ("story_anchor", "purpose"):
                original = clean(source_role.get(field))
                current = clean(generated_role.get(field))
                if original and current and original != current:
                    raise RuntimeError(
                        f"{expected_id}: {field} changed from Story Plan."
                    )

            if expected_id in seen_ids:
                raise RuntimeError(f"Duplicate scene ID: {expected_id}.")
            seen_ids.add(expected_id)

    if plan.get("parts_count") not in (None, len(parts)):
        raise RuntimeError("Story Plan parts_count mismatch.")


def validate_story(story: dict, settings: dict) -> tuple[list, list, int]:
    if clean(story.get("status")).lower() != "completed":
        raise RuntimeError("ai_story.json is not completed.")

    mode = clean(story.get("format")).lower()
    if mode != settings["format"]:
        raise RuntimeError(
            f"FORMAT mismatch: Input={settings['format']}, AI Story={mode}."
        )

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode!r}.")

    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("ai_story.json contains no Parts.")

    if story.get("total_parts") not in (None, len(parts)):
        raise RuntimeError("AI Story total_parts mismatch.")

    minimum, maximum = get_scenes_range(
        load_input_config(str(INPUT_FILE))
    )

    expected_items = []
    main_total = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(
                f"AI Story Part numbering mismatch at {part_number}."
            )

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {part_number} has no main scenes.")

        if not minimum <= len(scenes) <= maximum:
            raise RuntimeError(
                f"Part {part_number}: {len(scenes)} scenes outside "
                f"SCENES={minimum}-{maximum}."
            )

        for scene_number, source in enumerate(scenes, 1):
            scene_id = canonical_main_id(part_number, scene_number)
            validate_source_record(source, scene_id, "main")

            expected_items.append({
                "part": part_number,
                "scene": scene_number,
                "scene_id": scene_id,
                "role": "main",
                "part_data": part,
                "source": source,
                "narration": clean(source["narration"]),
                "global_scene_number": main_total + 1,
            })
            main_total += 1

        final_part = part_number == len(parts)

        for field, role, required in (
            ("hook_scene", "hook", True),
            ("suspense_scene", "suspense", not final_part),
            ("resolution_scene", "resolution", final_part),
        ):
            source = part.get(field)

            if not required:
                if source is not None:
                    raise RuntimeError(
                        f"Part {part_number}: unexpected {role} record."
                    )
                continue

            scene_id = canonical_role_id(part_number, role)
            validate_source_record(source, scene_id, role)

            expected_items.append({
                "part": part_number,
                "scene": None,
                "scene_id": scene_id,
                "role": role,
                "part_data": part,
                "source": source,
                "narration": clean(source["narration"]),
                "global_scene_number": None,
            })

    if story.get("total_scenes") not in (None, main_total):
        raise RuntimeError("AI Story total_scenes mismatch.")

    if story.get("total_extra_scenes") not in (
        None, len(expected_items) - main_total
    ):
        raise RuntimeError("AI Story total_extra_scenes mismatch.")

    return parts, expected_items, main_total


def load_character_bible(settings: dict) -> dict:
    bible = read_json(BIBLE_FILE)

    if bible is None:
        if settings["character_bible"]:
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but character_bible.json is missing."
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
        raise RuntimeError("Character Bible is not ready.")

    if settings["character_bible"] and status != "completed":
        raise RuntimeError(
            "Character Bible is disabled while CHARACTER_BIBLE=true."
        )

    for field, expected_type in (
        ("characters", list),
        ("scene_character_map", list),
        ("scene_roles", list),
        ("world", dict),
    ):
        if not isinstance(bible.get(field, expected_type()), expected_type):
            raise RuntimeError(
                f"Character Bible field {field} has an invalid type."
            )

    ids = set()
    names = set()

    for character in bible["characters"]:
        if not isinstance(character, dict):
            raise RuntimeError("Invalid Character Bible character record.")

        identifier = clean(
            character.get("id") or character.get("character_id")
        )
        name = clean(character.get("name"))

        if not identifier or not name:
            raise RuntimeError("Character Bible record requires ID and name.")

        if identifier.casefold() in ids or name.casefold() in names:
            raise RuntimeError(
                f"Duplicate Character Bible identity: {identifier}/{name}."
            )

        ids.add(identifier.casefold())
        names.add(name.casefold())

    return bible


def normalize_names(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]

    result = []
    seen = set()

    for item in value:
        if isinstance(item, dict):
            item = (
                item.get("id")
                or item.get("character_id")
                or item.get("name")
            )

        name = clean(item)
        key = name.casefold()

        if name and key not in seen:
            seen.add(key)
            result.append(name)

    return result


def mapping_references(row: dict) -> list[str]:
    return normalize_names(
        row.get("character_ids")
        or row.get("character_names")
        or row.get("characters")
    )


def scene_character_references(
    bible: dict,
    source: dict,
    scene_id: str,
    part: int,
    scene: int | None,
    role: str,
) -> list[str]:
    mapped = []

    rows = bible.get("scene_character_map", [])
    role_rows = bible.get("scene_roles", [])

    exact_map = [
        row for row in rows
        if clean(row.get("scene_id")) == scene_id
    ]
    exact_roles = [
        row for row in role_rows
        if clean(row.get("scene_id")) == scene_id
    ]

    if len(exact_map) > 1 or len(exact_roles) > 1:
        raise RuntimeError(
            f"{scene_id}: duplicate Character Bible scene mappings."
        )

    if exact_map and clean(exact_map[0].get("role")).lower() != role:
        raise RuntimeError(f"{scene_id}: Character Bible role mismatch.")

    if exact_roles and clean(exact_roles[0].get("role")).lower() != role:
        raise RuntimeError(f"{scene_id}: Character Bible role mismatch.")

    if exact_map:
        mapped.extend(mapping_references(exact_map[0]))
    if exact_roles:
        role_refs = mapping_references(exact_roles[0])
        if mapped and set(x.casefold() for x in mapped) != set(
            x.casefold() for x in role_refs
        ):
            raise RuntimeError(
                f"{scene_id}: scene_roles and scene_character_map conflict."
            )
        mapped.extend(role_refs)

    if not mapped and role == "main" and scene is not None:
        positional = [
            row for row in rows
            if row.get("part") == part and row.get("scene") == scene
        ]
        if len(positional) > 1:
            raise RuntimeError(
                f"{scene_id}: ambiguous positional character mapping."
            )
        if positional:
            mapped.extend(mapping_references(positional[0]))

    explicit = normalize_names(
        source.get("character_ids")
        or source.get("characters")
        or source.get("character_names")
    )

    # Merge explicit references and Bible references; do not silently
    # discard either source.
    combined = normalize_names(mapped + explicit)

    return combined


def find_character_records(
    bible: dict,
    references: list[str],
    scene_id: str,
) -> list[dict]:
    if not references:
        return []

    matches = []
    matched = set()

    for character in bible.get("characters", []):
        candidates = {
            clean(character.get("id")).casefold(),
            clean(character.get("character_id")).casefold(),
            clean(character.get("name")).casefold(),
        }
        candidates.update(
            value.casefold()
            for value in normalize_names(character.get("aliases"))
        )
        candidates.discard("")

        found = candidates.intersection(
            value.casefold() for value in references
        )
        if found:
            matches.append(character)
            matched.update(found)

    unresolved = {
        value for value in references
        if value.casefold() not in matched
    }

    if unresolved:
        raise RuntimeError(
            f"{scene_id}: unknown Character Bible references: "
            + ", ".join(sorted(unresolved))
        )

    return matches


def character_summary(records: list[dict]) -> list[dict]:
    fields = (
        "id", "character_id", "name", "aliases", "age", "gender",
        "appearance", "character_description", "face_features",
        "skin_tone", "hair", "eyes", "body_features", "height",
        "build", "clothing", "accessories", "distinctive_features",
        "personality", "visual_identity", "continuity_notes",
        "consistency_rules", "physical_constraints",
    )

    result = []
    for character in records:
        item = {
            key: character[key]
            for key in fields
            if key in character and character[key] not in (None, "", [], {})
        }
        if item:
            result.append(item)

    return result


def build_negative_prompt(source: dict, settings: dict) -> str:
    values = []

    if settings["negative_prompt"]:
        values.extend([
            "distorted face",
            "unrealistic anatomy",
            "extra or missing fingers",
            "fused or duplicated limbs",
            "duplicated people",
            "plastic skin",
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
            values.append("unnecessary neon glow")
        if settings["avoid_glitch"]:
            values.append("glitch effect, digital distortion, frame tearing")

    original = clean(source.get("negative_prompt"))
    if original:
        values.append(original)

    result = []
    seen = set()

    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)

    return ", ".join(result)


def build_visual_prompt(
    source: dict,
    settings: dict,
    characters: list[dict],
    world: dict,
    previous_scene: dict | None,
    role: str,
) -> str:
    visual = clean(source.get("visual_prompt"))
    if len(visual) < 24:
        raise RuntimeError(
            f"{role} visual_prompt is missing or too short."
        )

    pieces = [
        "STORY-SPECIFIC VISUAL EVENT: " + visual,
        (
            "Photorealistic live-action cinematic film still. "
            f"Style: {settings['visual_style']}; "
            f"realism: {settings['realism']}; "
            f"quality: {settings['quality']}."
        ),
    ]

    context_fields = (
        ("story_anchor", "Exact story moment"),
        ("beat", "Story beat"),
        ("purpose", "Scene purpose"),
        ("action", "Visible action"),
        ("location", "Exact story location"),
        ("world_context", "Story world"),
        ("time_period", "Story time period"),
        ("objects", "Story-specific objects"),
        ("animals", "Story-specific animals"),
        ("vehicles", "Story-specific vehicles"),
        ("clothing", "Story-specific clothing"),
        ("dialogue", "Dialogue context"),
        ("camera_prompt", "Camera direction"),
        ("lighting_prompt", "Lighting direction"),
    )

    for field, label in context_fields:
        value = source.get(field)
        if isinstance(value, list):
            value = ", ".join(clean(item) for item in value if clean(item))
        value = clean(value)
        if value:
            pieces.append(f"{label}: {value}")

    pieces.append(
        "Emotional tone: "
        + (clean(source.get("mood")) or settings["mood"])
    )

    if settings["cinematic_camera"] and not clean(
        source.get("camera_prompt") or source.get("camera")
    ):
        pieces.append(
            "Use intentional cinematic framing and physically plausible "
            "camera perspective."
        )

    if settings["realistic_lighting"] and not clean(
        source.get("lighting_prompt") or source.get("lighting")
    ):
        pieces.append(
            "Use motivated realistic lighting, natural skin tones "
            "and physically plausible shadows."
        )

    if settings["character_consistency"] and characters:
        pieces.append(
            "CHARACTER IDENTITY LOCK. Preserve established face, age, "
            "gender evidence, skin tone, hair, eyes, body proportions, "
            "distinctive features and source-supported clothing continuity. "
            "Do not swap, merge or redesign characters. Character records: "
            + json.dumps(
                character_summary(characters),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    if settings["world_consistency"] and world:
        pieces.append(
            "WORLD CONTINUITY LOCK. Preserve story era, location, "
            "architecture and environment. Apply only relevant details: "
            + json.dumps(
                world,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    if settings["scene_continuity"] and previous_scene:
        previous_context = []

        for field, label in (
            ("world_context", "Previous location"),
            ("time_period", "Previous time period"),
            ("story_anchor", "Previous story moment"),
            ("character_ids", "Previous character IDs"),
        ):
            value = previous_scene.get(field)
            if isinstance(value, list):
                value = ", ".join(clean(item) for item in value if clean(item))
            value = clean(value)
            if value:
                previous_context.append(f"{label}: {value}")

        if previous_context:
            pieces.append(
                "CHRONOLOGICAL CONTINUITY: "
                + " ".join(previous_context)
                + ". Continue the established timeline without repeating "
                "or contradicting the previous event."
            )

    if settings["natural_motion"]:
        pieces.append(
            "Depict believable posture, natural gestures and realistic "
            "interaction with story objects. This is a still image, "
            "not character animation."
        )

    pieces.append(
        "Show only the approved story event. Do not add unrelated people, "
        "objects, locations, vehicles, animals or events. Do not render "
        "captions, subtitles, labels, logos or text. Keep important subjects "
        "and objects fully visible with safe margins for later photo-motion "
        "crop, zoom and pan."
    )

    return "\n".join(pieces).strip()


def build_scene_record(
    item: dict,
    settings: dict,
    bible: dict,
    previous_scene: dict | None,
) -> dict:
    source = item["source"]
    part = item["part"]
    scene = item["scene"]
    role = item["role"]
    scene_id = item["scene_id"]

    references = scene_character_references(
        bible, source, scene_id, part, scene, role
    )

    characters = find_character_records(
        bible, references, scene_id
    )

    world = bible.get("world", {})
    visual_prompt = build_visual_prompt(
        source=source,
        settings=settings,
        characters=characters,
        world=world,
        previous_scene=previous_scene,
        role=role,
    )

    camera = (
        clean(source.get("camera_prompt") or source.get("camera"))
        or settings["camera_style"]
    )
    lighting = (
        clean(source.get("lighting_prompt") or source.get("lighting"))
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
        clean(source.get("ambient_sound") or source.get("ambient_prompt"))
        if settings["ambient_sound"] else ""
    )
    music = (
        clean(source.get("music_prompt") or source.get("music_direction"))
        if settings["music"] else ""
    )

    title = clean(source.get("title"))
    if not title:
        title = (
            f"Part {part} {role.title()}"
            if role != "main"
            else f"Part {part} Scene {scene}"
        )

    source_signature = digest({
        "source": source,
        "part": part,
        "scene": scene,
        "role": role,
        "prompt_version": PROMPT_VERSION,
    })

    return {
        "part": part,
        "scene": scene,
        "local_scene": scene,
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

        "story_anchor": clean(source.get("story_anchor")),
        "beat": clean(source.get("beat")),
        "purpose": clean(source.get("purpose")),
        "action": clean(source.get("action")),
        "location": clean(source.get("location")),
        "world_context": (
            clean(source.get("world_context"))
            or clean(source.get("location"))
        ),
        "time_period": clean(source.get("time_period")),
        "objects": source.get("objects", []),
        "animals": source.get("animals", []),
        "vehicles": source.get("vehicles", []),
        "clothing": clean(source.get("clothing")),

        "visual_prompt": visual_prompt,
        "negative_prompt": build_negative_prompt(source, settings),
        "camera_prompt": camera,
        "lighting_prompt": lighting,
        "camera": camera,
        "lighting": lighting,
        "mood": clean(source.get("mood")) or settings["mood"],

        "duration": str(duration),
        "scene_duration": str(duration),
        "transition": (
            clean(source.get("transition")) or settings["transitions"]
        ),

        "character_ids": references,
        "characters": references,
        "character_details": character_summary(characters),

        "sfx_prompt": sfx,
        "ambient_sound": ambient,
        "music_prompt": music,
        "music_direction": music,

        "generation_method": "offline_story_prompt_enrichment",
        "source_scene_number": scene,
        "source_scene_id": scene_id,
        "source_role": role,
        "source_scene_signature": source_signature,
        "scene_prompt_version": PROMPT_VERSION,
    }


def scene_order_key(scene: dict) -> tuple:
    role = clean(scene.get("role")).lower()
    if role not in ALLOWED_ROLES:
        raise RuntimeError(f"Unsupported scene role: {role!r}.")

    try:
        part = int(scene.get("part"))
        local = int(scene.get("scene") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Scene has invalid part/scene numbering.") from exc

    return part, ROLE_ORDER[role], local, clean(scene.get("scene_id"))


def expected_source_signature(item: dict) -> str:
    return digest({
        "source": item["source"],
        "part": item["part"],
        "scene": item["scene"],
        "role": item["role"],
        "prompt_version": PROMPT_VERSION,
    })


def scene_is_valid(
    scene: Any,
    expected: dict,
    generation_signature: str,
) -> bool:
    if not isinstance(scene, dict):
        return False

    checks = {
        "status": "completed",
        "part": expected["part"],
        "scene": expected["scene"],
        "scene_id": expected["scene_id"],
        "role": expected["role"],
        "narration": expected["narration"],
        "source_scene_id": expected["scene_id"],
        "source_role": expected["role"],
        "source_scene_signature": expected_source_signature(expected),
        "generation_signature": generation_signature,
        "scene_prompt_version": PROMPT_VERSION,
    }

    for field, value in checks.items():
        if scene.get(field) != value:
            return False

    if scene.get("is_extra_scene") != (expected["role"] != "main"):
        return False

    if expected["role"] == "main":
        if scene.get("scene") != expected["scene"]:
            return False
    elif scene.get("scene") is not None:
        return False

    return all(clean(scene.get(field)) for field in REQUIRED_OUTPUT_FIELDS)


def load_existing_scenes(
    generation_signature: str,
    settings: dict,
) -> dict:
    if not settings["resume_enabled"] or not settings["skip_completed"]:
        return {}

    data = read_json(OUTPUT_FILE)
    if not isinstance(data, dict):
        return {}

    if data.get("version") != VERSION:
        return {}

    if data.get("scene_prompt_version") != PROMPT_VERSION:
        return {}

    if data.get("generation_signature") != generation_signature:
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
                f"Duplicate cached scene ID: {scene_id}."
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

    return {clean(value) for value in values if clean(value)}


def save_checkpoint(
    generation_signature: str,
    completed: dict,
    expected_items: list,
) -> None:
    expected_ids = {item["scene_id"] for item in expected_items}

    completed_ids = sorted(
        scene_id
        for scene_id, scene in completed.items()
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
    expected = {
        number: {role: 0 for role in ROLE_ORDER}
        for number in range(1, len(parts) + 1)
    }
    actual = {
        number: {role: 0 for role in ROLE_ORDER}
        for number in range(1, len(parts) + 1)
    }

    for item in expected_items:
        expected[item["part"]][item["role"]] += 1

    for scene in scenes:
        role = clean(scene.get("role")).lower()
        part = scene.get("part")

        if role not in ALLOWED_ROLES or part not in actual:
            raise RuntimeError("Unexpected final scene role or Part.")

        actual[part][role] += 1

    if actual != expected:
        raise RuntimeError(
            f"Per-Part role counts mismatch. Expected={expected}; actual={actual}."
        )

    for part_number, counts in actual.items():
        if counts["hook"] != 1:
            raise RuntimeError(
                f"Part {part_number} requires exactly one Hook."
            )

        if counts["main"] < 1:
            raise RuntimeError(
                f"Part {part_number} requires at least one Main scene."
            )

        if part_number < len(parts):
            if counts["suspense"] != 1 or counts["resolution"] != 0:
                raise RuntimeError(
                    f"Part {part_number} requires one Suspense and no Resolution."
                )
        else:
            if counts["resolution"] != 1 or counts["suspense"] != 0:
                raise RuntimeError(
                    "Final Part requires one Resolution and no Suspense."
                )

    return actual


def validate_final_scenes(
    scenes: list,
    expected_items: list,
    parts: list,
    generation_signature: str,
) -> dict:
    expected_by_id = {
        item["scene_id"]: item for item in expected_items
    }

    if len(expected_by_id) != len(expected_items):
        raise RuntimeError("Duplicate scene IDs in source Story Plan.")

    actual_ids = set()

    for scene in scenes:
        if not isinstance(scene, dict):
            raise RuntimeError("Final output contains an invalid scene.")

        scene_id = clean(scene.get("scene_id"))
        if not scene_id or scene_id in actual_ids:
            raise RuntimeError(
                f"Missing or duplicate final scene ID: {scene_id!r}."
            )

        if scene_id not in expected_by_id:
            raise RuntimeError(
                f"Scene is not present in AI Story: {scene_id}."
            )

        actual_ids.add(scene_id)
        expected = expected_by_id[scene_id]

        if not scene_is_valid(scene, expected, generation_signature):
            raise RuntimeError(
                f"Final scene validation failed: {scene_id}."
            )

        for field in (
            "story_anchor", "beat", "purpose", "character_ids",
            "visual_prompt", "negative_prompt",
        ):
            if field not in scene:
                raise RuntimeError(
                    f"{scene_id}: final output is missing {field}."
                )

    missing = set(expected_by_id) - actual_ids
    if missing:
        raise RuntimeError(
            "Missing scene records: " + ", ".join(sorted(missing)[:50])
        )

    if len(scenes) != len(expected_items):
        raise RuntimeError("Final scene count mismatch.")

    ordered = sorted(scenes, key=scene_order_key)
    if scenes != ordered:
        raise RuntimeError(
            "Scenes are not ordered as Hook, Main, Suspense/Resolution per Part."
        )

    return validate_role_counts(scenes, parts, expected_items)


def build_output_metadata(
    story: dict,
    settings: dict,
    bible: dict,
    generation_signature: str,
    scenes: list,
    status: str,
    main_total: int,
    role_counts: dict | None = None,
) -> dict:
    parts = story.get("parts", [])

    if role_counts is None:
        role_counts = {
            role: sum(
                item.get("role") == role for item in scenes
            )
            for role in ROLE_ORDER
        }

    part_counts = {
        str(number): {
            role: sum(
                item.get("part") == number
                and item.get("role") == role
                for item in scenes
            )
            for role in ROLE_ORDER
        }
        for number in range(1, len(parts) + 1)
    }

    return {
        "version": VERSION,
        "scene_prompt_version": PROMPT_VERSION,
        "status": status,
        "format": settings["format"],
        "title": clean(story.get("title")),
        "topic": clean(story.get("topic")),
        "model": clean(story.get("model")),
        "generation_method": "offline_story_prompt_enrichment",
        "generation_signature": generation_signature,
        "source_story_file": str(STORY_FILE.relative_to(ROOT)),
        "source_story_signature": digest(story),
        "character_bible_status": bible.get("status", "unknown"),
        "character_bible_signature": digest(bible),
        "total_parts": len(parts),
        "total_scenes": len(scenes),
        "total_main_scenes": main_total,
        "total_extra_scenes": len(scenes) - main_total,
        "scene_role_counts": role_counts,
        "part_role_counts": part_counts,
        "completed_scenes": sum(
            item.get("status") == "completed" for item in scenes
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
    settings = build_settings(config)
    scene_range = get_scenes_range(config)

    story = read_json(STORY_FILE, required=True)
    plan = read_json(PLAN_FILE, required=True)

    parts, expected_items, main_total = validate_story(story, settings)
    validate_plan(plan, settings["format"], parts, scene_range)

    if clean(story.get("source_plan_signature")):
        if story["source_plan_signature"] != digest(plan):
            raise RuntimeError(
                "AI Story source_plan_signature does not match story_plan.json."
            )

    bible = load_character_bible(settings)

    topic = (
        clean(get_topic(config))
        or clean(story.get("topic"))
        or clean(story.get("title"))
    )

    input_story_text = clean(get_story_text(config))
    story_text = (
        clean(story.get("story_text"))
        or clean(story.get("main_story_text"))
        or input_story_text
    )

    if not story_text:
        raise RuntimeError("Complete story text is missing.")

    generation_signature = digest({
        "version": VERSION,
        "prompt_version": PROMPT_VERSION,
        "format": settings["format"],
        "scene_range": scene_range,
        "topic": topic,
        "input_story_text": input_story_text,
        "ai_story_text": story_text,
        "ai_story": story,
        "story_plan": plan,
        "character_bible": bible,
        "settings": settings,
    })

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing_scenes(
        generation_signature, settings
    )
    checkpoint_ids = load_checkpoint(
        generation_signature, settings
    )

    expected_by_id = {
        item["scene_id"]: item for item in expected_items
    }

    completed = {}

    for scene_id, item in expected_by_id.items():
        cached = existing.get(scene_id)

        if not scene_is_valid(cached, item, generation_signature):
            continue

        # Reuse only the full valid cached record. A checkpoint ID alone
        # never authorizes reuse.
        if scene_id in checkpoint_ids or scene_id in existing:
            completed[scene_id] = cached

    total_parts = len(parts)
    total_records = len(expected_items)

    log("Format                  :", settings["format"])
    log("Parts                   :", total_parts)
    log("SCENES range            :", f"{scene_range[0]}-{scene_range[1]}")
    log("Main scenes             :", main_total)
    log("Extra scenes            :", total_records - main_total)
    log("All scene records       :", total_records)
    log("Character Bible         :", bible["status"])
    log("Reusable completed      :", f"{len(completed)}/{total_records}")
    log("Generation mode         : OFFLINE")
    log("Additional Gemini calls : 0")
    log("=" * 64)

    for index, item in enumerate(expected_items, 1):
        scene_id = item["scene_id"]

        if scene_id in completed:
            log(
                f"[{index}/{total_records}] {scene_id}: valid checkpoint reused."
            )
            continue

        current_key = (
            item["part"],
            ROLE_ORDER[item["role"]],
            int(item["scene"] or 0),
            scene_id,
        )

        previous = [
            row for row in completed.values()
            if scene_order_key(row) < current_key
        ]
        previous.sort(key=scene_order_key)
        previous_scene = previous[-1] if previous else None

        scene = build_scene_record(
            item=item,
            settings=settings,
            bible=bible,
            previous_scene=previous_scene,
        )
        scene["generation_signature"] = generation_signature

        if scene["narration"] != item["narration"]:
            raise RuntimeError(
                f"Narration changed unexpectedly: {scene_id}."
            )

        if scene["source_scene_signature"] != expected_source_signature(item):
            raise RuntimeError(
                f"Source fingerprint mismatch: {scene_id}."
            )

        completed[scene_id] = scene
        ordered = sorted(completed.values(), key=scene_order_key)

        partial = build_output_metadata(
            story=story,
            settings=settings,
            bible=bible,
            generation_signature=generation_signature,
            scenes=ordered,
            status="in_progress",
            main_total=main_total,
        )
        write_json(OUTPUT_FILE, partial)

        if settings["save_checkpoint"]:
            save_checkpoint(
                generation_signature,
                completed,
                expected_items,
            )

        log(
            f"[{index}/{total_records}] Saved {scene_id} ({item['role']})."
        )

    final_scenes = sorted(
        completed.values(), key=scene_order_key
    )

    role_counts = validate_final_scenes(
        final_scenes,
        expected_items,
        parts,
        generation_signature,
    )

    final_output = build_output_metadata(
        story=story,
        settings=settings,
        bible=bible,
        generation_signature=generation_signature,
        scenes=final_scenes,
        status="completed",
        main_total=main_total,
        role_counts={
            role: sum(
                role_counts[part][role]
                for part in role_counts
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

    # Verify the persisted JSON and checkpoint, not only memory objects.
    written = read_json(OUTPUT_FILE, required=True)

    if written.get("status") != "completed":
        raise RuntimeError("Final scenes.json status verification failed.")

    if written.get("generation_signature") != generation_signature:
        raise RuntimeError("Final scenes.json fingerprint mismatch.")

    written_scenes = written.get("scenes")
    if not isinstance(written_scenes, list):
        raise RuntimeError("Final scenes.json scene list is missing.")

    validate_final_scenes(
        written_scenes,
        expected_items,
        parts,
        generation_signature,
    )

    checkpoint = read_json(CHECKPOINT_FILE, required=True)

    if checkpoint.get("status") != "completed":
        raise RuntimeError("Scene checkpoint is not completed.")

    if checkpoint.get("generation_signature") != generation_signature:
        raise RuntimeError("Scene checkpoint fingerprint mismatch.")

    if checkpoint.get("completed_count") != total_records:
        raise RuntimeError("Scene checkpoint completed count mismatch.")

    log("=" * 64)
    log("SCENE GENERATION COMPLETED")
    log("Parts                   :", total_parts)
    log("Main scenes             :", main_total)
    log("Extra scenes            :", total_records - main_total)
    log("All scene records       :", len(final_scenes), "/", total_records)

    for role in ("hook", "main", "suspense", "resolution"):
        log(
            f"{role.upper():23}: "
            f"{final_output['scene_role_counts'][role]}"
        )

    log("Generation mode         : OFFLINE")
    log("Additional Gemini calls : 0")
    log("Output                  :", OUTPUT_FILE.relative_to(ROOT))
    log("Checkpoint              :", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 64)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log(
            "Scene generation interrupted. "
            "Valid completed checkpoints are preserved."
        )
        sys.exit(130)
    except Exception as exc:
        log(
            f"SCENE GENERATION FAILED: "
            f"{type(exc).__name__}: {exc}"
        )
        sys.exit(1)