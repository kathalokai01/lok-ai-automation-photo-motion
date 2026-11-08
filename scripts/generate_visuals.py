#!/usr/bin/env python3
"""Prepare story-grounded visual jobs for the photo-motion pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes_range,
)

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = OUT / "scenes" / "scenes.json"
BIBLE_FILE = OUT / "story" / "character_bible.json"

VISUALS_DIR = OUT / "visuals"
IMAGES_DIR = VISUALS_DIR / "images"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUALS_DIR / "visual_jobs_manifest.json"
CHECKPOINT_FILE = OUT / "checkpoints" / "visual_jobs_progress.json"

VERSION = "photo-motion-visual-jobs-v12-narrated-extra-roles"
PROMPT_VERSION = 12

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}
ALLOWED_ROLES = set(ROLE_ORDER)

IMAGE_SIZES = {
    "short": (720, 1280),
    "full": (1280, 720),
}

MIN_PROMPT_LENGTH = 24
MIN_NARRATION_LENGTH = 8
MAX_PROMPT_LENGTH = 24000
MAX_IMAGE_FILE_SIZE = 80 * 1024 * 1024


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

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be a positive integer.")

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{label} must be a positive integer."
        ) from exc

    if number < 1:
        raise RuntimeError(f"{label} must be a positive integer.")

    return number


def digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def read_json(path: Path, required: bool = False) -> Any:
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def atomic_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def text_sha256(value: str) -> str:
    normalized = clean(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def image_dimensions(format_name: str) -> tuple[int, int]:
    name = str(format_name).strip().lower()

    if name not in IMAGE_SIZES:
        raise RuntimeError(
            f"Unsupported FORMAT: {format_name!r}. "
            "Only short and full are supported."
        )

    return IMAGE_SIZES[name]


def normalize_role(scene: dict) -> str:
    raw = clean(
        scene.get("role")
        or scene.get("scene_type")
        or scene.get("type")
        or "main"
    ).lower()

    aliases = {
        "opening": "hook",
        "intro": "hook",
        "part_hook": "hook",
        "main_scene": "main",
        "story": "main",
        "cliffhanger": "suspense",
        "ending": "resolution",
        "final": "resolution",
    }

    role = aliases.get(raw, raw)

    if role not in ALLOWED_ROLES:
        raise RuntimeError(
            f"Unsupported role {raw!r} in scene "
            f"{scene.get('scene_id')!r}."
        )

    return role


def normalize_scene_id(
    scene: dict,
    part: int,
    role: str,
    local_scene: int | None,
) -> str:
    scene_id = clean(
        scene.get("scene_id")
        or scene.get("id")
        or scene.get("source_scene_id")
    )

    expected = (
        f"part_{part:02d}_scene_{local_scene:02d}"
        if role == "main"
        else f"part_{part:02d}_{role}"
    )

    if not scene_id:
        scene_id = expected

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
        raise RuntimeError(f"Invalid scene ID: {scene_id!r}.")

    if scene_id != expected:
        raise RuntimeError(
            f"Scene ID mismatch: expected {expected}, "
            f"received {scene_id}."
        )

    return scene_id


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


def normalize_scene_rows(
    document: dict,
    expected_parts: int,
    expected_scene_counts: dict[int, int],
) -> list[dict]:
    if not isinstance(document, dict):
        raise RuntimeError("scenes.json root must be an object.")

    rows = document.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json has no scenes array.")

    prepared = []
    seen_ids = set()
    seen_main = set()
    counts = {
        role: {}
        for role in ALLOWED_ROLES
    }

    for index, original in enumerate(rows, 1):
        if not isinstance(original, dict):
            raise RuntimeError(
                f"Scene row {index} is not an object."
            )

        part = positive_int(
            original.get("part"),
            f"Scene row {index} part",
        )

        if part > expected_parts:
            raise RuntimeError(
                f"Scene row {index}: part {part} exceeds "
                f"selected PARTS={expected_parts}."
            )

        role = normalize_role(original)
        raw_scene = original.get(
            "scene",
            original.get("local_scene"),
        )

        if role == "main":
            local = positive_int(
                raw_scene,
                f"Part {part} main scene number",
            )

            if local > expected_scene_counts[part]:
                raise RuntimeError(
                    f"Main scene {part}/{local} exceeds "
                    f"actual Scene count={expected_scene_counts[part]}."
                )

            pair = (part, local)
            if pair in seen_main:
                raise RuntimeError(
                    f"Duplicate main scene mapping: {pair}."
                )
            seen_main.add(pair)

        else:
            local = None
            if raw_scene not in (None, ""):
                raise RuntimeError(
                    f"{original.get('scene_id')!r}: extra role "
                    "must not use a main scene number."
                )

        scene_id = normalize_scene_id(
            original,
            part,
            role,
            local,
        )

        if scene_id in seen_ids:
            raise RuntimeError(
                f"Duplicate scene ID: {scene_id}."
            )
        seen_ids.add(scene_id)

        counts[role][part] = counts[role].get(part, 0) + 1

        narration = (
            clean(
                original.get("narration")
                or original.get("text")
                or original.get("story_text")
            )
            if role == "main"
            else clean(original.get("narration"))
        )

        visual = clean(
            original.get("visual_prompt")
            or original.get("visual")
            or original.get("description")
        )

        story_anchor = clean(original.get("story_anchor"))
        if role == "main":
            if len(narration) < MIN_NARRATION_LENGTH:
                raise RuntimeError(
                    f"{scene_id}: Main narration is missing or too short."
                )
            if original.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: Main voiceover must be enabled.")
            if original.get("source_text_sha256") != text_sha256(narration):
                raise RuntimeError(f"{scene_id}: locked narration hash mismatch.")
            if original.get("source_word_count") != len(narration.split()):
                raise RuntimeError(f"{scene_id}: locked narration word count mismatch.")
        else:
            if len(narration.split()) < 8:
                raise RuntimeError(f"{scene_id}: extra-role narration is missing or too short.")
            if original.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: extra-role voiceover must be enabled.")
            if original.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{scene_id}: extra narration source is invalid.")
            if original.get("source_text_sha256") != text_sha256(narration):
                raise RuntimeError(f"{scene_id}: extra narration hash mismatch.")
            if not story_anchor:
                raise RuntimeError(f"{scene_id}: extra-role story anchor is missing.")

        if len(visual) < MIN_PROMPT_LENGTH:
            raise RuntimeError(
                f"{scene_id}: visual_prompt is missing or too short."
            )

        prepared.append({
            **dict(original),
            "part": part,
            "scene": local,
            "local_scene": local,
            "scene_id": scene_id,
            "id": scene_id,
            "role": role,
            "scene_type": role,
            "source_scene_number": local,
            "is_extra_scene": role != "main",
            "narration": narration,
            "story_anchor": story_anchor,
            "voiceover_enabled": True,
            "narration_source": "locked_part_text" if role == "main" else "ai_generated_role_narration",
            "source_text_sha256": text_sha256(narration),
            "source_word_count": len(narration.split()),
            "visual_prompt": visual,
        })

    expected_main = {
        (part, scene)
        for part in range(1, expected_parts + 1)
        for scene in range(1, expected_scene_counts[part] + 1)
    }

    missing = sorted(expected_main - seen_main)
    unexpected = sorted(seen_main - expected_main)

    if missing or unexpected:
        raise RuntimeError(
            "Main scene mapping mismatch. "
            f"Missing={missing[:20]}, "
            f"unexpected={unexpected[:20]}."
        )

    for part in range(1, expected_parts + 1):
        if counts["main"].get(part, 0) != expected_scene_counts[part]:
            raise RuntimeError(
                f"Part {part} requires {expected_scene_counts[part]} Main Scenes."
            )

        if counts["hook"].get(part, 0) != 1:
            raise RuntimeError(
                f"Part {part} must have exactly one Hook."
            )

        if part < expected_parts:
            if counts["suspense"].get(part, 0) != 1:
                raise RuntimeError(
                    f"Part {part} must have exactly one Suspense."
                )

            if counts["resolution"].get(part, 0):
                raise RuntimeError(
                    f"Non-final part {part} cannot contain Resolution."
                )
        else:
            if counts["resolution"].get(part, 0) != 1:
                raise RuntimeError(
                    "Final part must have exactly one Resolution."
                )

            if counts["suspense"].get(part, 0):
                raise RuntimeError(
                    "Final part cannot contain Suspense."
                )

    return sorted(
        prepared,
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
            row["scene"] or 0,
            row["scene_id"],
        ),
    )


def load_character_bible(config: dict) -> dict:
    enabled = as_bool(config.get("CHARACTER_BIBLE"), True)
    bible = read_json(BIBLE_FILE)

    if bible is None:
        if enabled:
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but "
                "character_bible.json is missing."
            )

        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "scene_roles": [],
            "world": {},
        }

    if not isinstance(bible, dict):
        raise RuntimeError(
            "Character Bible root must be an object."
        )

    status = clean(bible.get("status")).lower()

    if status not in {"completed", "disabled"}:
        raise RuntimeError(
            f"Character Bible is not ready: {status or 'missing'}."
        )

    if enabled and status != "completed":
        raise RuntimeError(
            "Character Bible is disabled in the configuration."
        )

    characters = bible.get("characters", [])
    scene_map = bible.get("scene_character_map", [])
    scene_roles = bible.get("scene_roles", [])
    world = bible.get("world", {})

    if not isinstance(characters, list):
        raise RuntimeError(
            "Character Bible characters must be a list."
        )

    if not isinstance(scene_map, list):
        raise RuntimeError(
            "Character Bible scene_character_map must be a list."
        )

    if not isinstance(scene_roles, list):
        raise RuntimeError(
            "Character Bible scene_roles must be a list."
        )

    if not isinstance(world, dict):
        raise RuntimeError(
            "Character Bible world must be an object."
        )

    known_ids = set()
    known_names = set()

    for index, character in enumerate(characters, 1):
        if not isinstance(character, dict):
            raise RuntimeError(
                f"Invalid character record #{index}."
            )

        identifier = clean(
            character.get("id")
            or character.get("character_id")
        )
        name = clean(character.get("name"))

        if not identifier or not name:
            raise RuntimeError(
                f"Character #{index} requires ID and name."
            )

        if (
            identifier.casefold() in known_ids
            or name.casefold() in known_names
        ):
            raise RuntimeError(
                f"Duplicate Character Bible identity: "
                f"{identifier}/{name}."
            )

        known_ids.add(identifier.casefold())
        known_names.add(name.casefold())

    for label, mappings in (
        ("scene_character_map", scene_map),
        ("scene_roles", scene_roles),
    ):
        seen = set()

        for index, row in enumerate(mappings, 1):
            if (
                not isinstance(row, dict)
                or not clean(row.get("scene_id"))
            ):
                raise RuntimeError(
                    f"Invalid {label} row #{index}."
                )

            key = (
                clean(row.get("scene_id")),
                clean(row.get("role")).lower(),
            )

            if key in seen:
                raise RuntimeError(
                    f"Duplicate {label} mapping: {key}."
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


def mapping_references(row: dict) -> list[str]:
    return normalize_names(
        row.get("character_ids")
        or row.get("character_names")
        or row.get("characters")
    )


def find_scene_character_references(
    scene: dict,
    bible: dict,
) -> list[dict]:
    scene_id = clean(scene.get("scene_id"))
    role = normalize_role(scene)
    part = int(scene["part"])
    scene_number = scene.get("scene")

    rows = (
        bible.get("scene_roles", [])
        + bible.get("scene_character_map", [])
    )

    exact = [
        row
        for row in rows
        if clean(row.get("scene_id")) == scene_id
        and clean(row.get("role")).lower() in {"", role}
    ]

    refs = mapping_references(exact[0]) if exact else []

    if not refs and role == "main":
        for row in bible.get("scene_character_map", []):
            try:
                same_position = (
                    int(row.get("part")) == part
                    and int(row.get("scene")) == int(scene_number)
                )
            except (TypeError, ValueError):
                same_position = False

            if same_position:
                refs = mapping_references(row)
                break

    explicit = normalize_names(
        scene.get("character_ids")
        or scene.get("characters")
        or scene.get("character_names")
    )

    if explicit:
        refs = explicit

    wanted = {
        value.casefold()
        for value in refs
    }

    selected = []
    matched = set()

    for character in bible.get("characters", []):
        candidates = {
            clean(character.get("id")).casefold(),
            clean(character.get("character_id")).casefold(),
            clean(character.get("name")).casefold(),
        }

        candidates.update(
            value.casefold()
            for value in normalize_names(
                character.get("aliases")
            )
        )
        candidates.discard("")

        overlap = wanted.intersection(candidates)

        if overlap:
            selected.append(character)
            matched.update(overlap)

    unresolved = wanted - matched

    if unresolved:
        raise RuntimeError(
            f"{scene_id}: unknown Character Bible references: "
            + ", ".join(sorted(unresolved))
        )

    return selected


def compact_character(character: dict) -> dict:
    fields = (
        "id", "character_id", "name", "aliases",
        "age", "gender", "appearance", "face",
        "face_features", "skin_tone", "hair", "eyes",
        "body_features", "height", "build", "clothing",
        "accessories", "distinctive_features",
        "visual_identity", "continuity_notes",
        "consistency_rules", "description",
    )

    return {
        key: character[key]
        for key in fields
        if key in character
        and character[key] not in (None, "", [], {})
    }


def validate_bible_coverage(
    scenes: list[dict],
    bible: dict,
    enabled: bool,
) -> None:
    if not enabled:
        return

    if bible.get("status") != "completed":
        raise RuntimeError(
            "Character Bible must be completed."
        )

    mapped = {
        clean(row.get("scene_id"))
        for row in (
            bible.get("scene_character_map", [])
            + bible.get("scene_roles", [])
        )
        if isinstance(row, dict)
    }

    missing = [
        row["scene_id"]
        for row in scenes
        if row["scene_id"] not in mapped
    ]

    if missing:
        raise RuntimeError(
            "Character Bible does not map every scene: "
            + ", ".join(missing[:30])
        )

    for scene in scenes:
        find_scene_character_references(scene, bible)


def scene_continuity_context(
    scene: dict,
    previous_main: dict | None,
    bible: dict,
) -> str:
    if not as_bool(scene.get("scene_continuity"), True):
        return ""

    if previous_main is None:
        return ""

    previous_id = clean(previous_main.get("scene_id"))
    previous_location = clean(
        previous_main.get("location")
        or previous_main.get("world_context")
    )
    current_location = clean(
        scene.get("location")
        or scene.get("world_context")
    )

    previous_characters = [
        clean(item.get("name"))
        for item in find_scene_character_references(
            previous_main,
            bible,
        )
    ]
    current_characters = [
        clean(item.get("name"))
        for item in find_scene_character_references(
            scene,
            bible,
        )
    ]

    details = [
        f"Immediately preceding story scene: {previous_id}."
    ]

    if previous_location:
        details.append(
            f"Previous location: {previous_location}."
        )

    if current_location:
        details.append(
            f"Current location: {current_location}."
        )

    recurring = sorted(
        set(previous_characters).intersection(current_characters)
    )

    if recurring:
        details.append(
            "Keep recurring characters visually identical: "
            + ", ".join(recurring)
            + "."
        )

    details.append(
        "Maintain chronological continuity in identity, props, "
        "clothing, weather and location when the story has not "
        "changed them. Do not copy the previous frame or repeat "
        "its action."
    )

    return " ".join(details)


def build_negative_prompt(
    config: dict,
    source: dict,
) -> str:
    values = [
        "wrong story event",
        "unrelated characters",
        "unrelated props",
        "wrong location",
        "wrong time period",
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
        "captions",
        "subtitles",
        "unintended written text",
        "logo",
        "watermark",
        "low resolution",
        "blurry main subject",
        "body animation",
        "face animation",
        "character animation",
        "motion blur",
    ]

    if as_bool(config.get("AVOID_CARTOON_LOOK"), True):
        values.extend([
            "cartoon",
            "anime",
            "comic-book illustration",
            "2D drawing",
            "3D animated character",
            "painting",
            "storybook illustration",
        ])

    if as_bool(config.get("AVOID_NEON"), True):
        values.extend([
            "unnecessary neon",
            "artificial neon glow",
        ])

    if as_bool(config.get("AVOID_GLITCH_EFFECTS"), True):
        values.extend([
            "glitch effect",
            "digital distortion",
            "frame tearing",
        ])

    configured = clean(config.get("NEGATIVE_PROMPT"))
    original = clean(source.get("negative_prompt"))

    if configured and configured.lower() not in {
        "true", "false", "1", "0",
    }:
        values.append(configured)

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


def scene_detail(source: dict, key: str) -> str:
    value = source.get(key)

    if isinstance(value, list):
        return ", ".join(
            clean(item)
            for item in value
            if clean(item)
        )

    if isinstance(value, dict):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return clean(value)


def build_prompt(
    config: dict,
    source: dict,
    bible: dict,
    format_name: str,
    width: int,
    height: int,
    previous_main: dict | None,
) -> str:
    visual = clean(source.get("visual_prompt"))
    narration = clean(source.get("narration"))

    if len(visual) < MIN_PROMPT_LENGTH:
        raise RuntimeError(
            f"{source['scene_id']}: visual prompt is too short."
        )

    role = normalize_role(source)
    story_anchor = clean(source.get("story_anchor"))
    if role == "main" and len(narration) < MIN_NARRATION_LENGTH:
        raise RuntimeError(
            f"{source['scene_id']}: Main narration is too short."
        )
    if role != "main" and (len(narration.split()) < 8 or not story_anchor):
        raise RuntimeError(
            f"{source['scene_id']}: extra role needs spoken narration and a source anchor."
        )

    style = clean(config.get("VISUAL_STYLE")) or "cinematic_realistic"
    realism = clean(config.get("REALISM")) or "high"
    quality = clean(config.get("QUALITY")) or "high"
    camera = clean(config.get("CAMERA_STYLE")) or "cinematic"
    lighting = clean(config.get("LIGHTING")) or "cinematic"
    mood = clean(source.get("mood") or config.get("MOOD")) or "dramatic"

    sections = [
        "EXACT STORY EVENT — PRIMARY VISUAL INSTRUCTION:",
        visual,
        (
            "LOCKED SPOKEN NARRATION CONTEXT — DO NOT PRINT THESE WORDS IN THE IMAGE:"
        ),
        narration,
        (
            "Create one photorealistic live-action cinematic film still. "
            "Depict the exact event in the primary visual instruction. "
            "Do not substitute a generic scene, invent a new event, "
            "omit story-specific people or objects, or alter "
            "relationships between characters."
        ),
        (
            "Preserve every explicit story detail: character names, "
            "people, locations, buildings, vehicles, animals, clothing, "
            "objects, weather, time period and described action. "
            "Do not replace named objects with generic alternatives. "
            "Do not add unrelated people, props, locations or events. "
            "Every explicitly named or specified story entity must be "
            "visibly represented when it belongs in this exact scene."
        ),
        (
            f"Output format: {format_name}. Exact dimensions: "
            f"{width}x{height}. Keep the complete important subject "
            "inside the frame with safe margins for later pan, zoom "
            "and 2.5D photo motion."
        ),
        (
            f"Visual style: {style}. Realism: {realism}. "
            f"Quality: {quality}."
        ),
        f"Camera language: {camera}.",
        (
            "Lighting: "
            + clean(
                source.get("lighting_prompt")
                or source.get("lighting")
                or lighting
            )
            + "."
        ),
        f"Emotional mood: {mood}.",
        (
            "Use realistic skin texture, natural anatomy, physically "
            "plausible light and shadows, accurate perspective, realistic "
            "materials and a clear readable focal subject."
        ),
        (
            "This is a still image for photo motion only. Do not create "
            "body animation, face animation, character animation, "
            "video motion, motion blur or an illustrated slideshow."
        ),
        (
            "No captions, subtitles, typography, labels, speech bubbles, "
            "logos, watermarks or readable text."
        ),
    ]

    if role != "main":
        sections.append(
            f"SEPARATE {role.upper()} ROLE: This is an independent "
            f"image for the {role} segment. Do not merge it into a "
            "main scene, change its role, or renumber main scenes."
        )

    for key, label in (
        ("action", "Visible action"),
        ("objects", "Story-specific objects and props"),
        ("animals", "Exact story animals and their actions"),
        ("vehicles", "Exact story vehicles and their state"),
        ("buildings", "Story-specific buildings and structures"),
        ("clothing", "Story-specific clothing and accessories"),
        ("location", "Exact location"),
        ("environment", "Story-specific environment"),
        ("weather", "Story weather conditions"),
        ("time_of_day", "Story time of day"),
        ("named_entities", "Named people, places and objects"),
        ("story_entities", "Required story entities"),
        ("world_context", "Story-world context"),
        ("time_period", "Story time period"),
        ("camera_prompt", "Camera direction"),
        ("dialogue", "Dialogue context for expression and action"),
    ):
        value = scene_detail(source, key)
        if value:
            sections.append(f"{label}: {value}")

    characters = find_scene_character_references(source, bible)

    if as_bool(config.get("CHARACTER_CONSISTENCY"), True):
        if characters:
            sections.append(
                "CHARACTER IDENTITY LOCK: Preserve approved identity, "
                "face, age, skin tone, hair, eyes, body proportions, "
                "distinctive features and story-consistent clothing. "
                "Do not swap, merge or redesign characters. Do not "
                "invent unspecified physical traits. Approved records: "
                + json.dumps(
                    [
                        compact_character(item)
                        for item in characters
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

    if as_bool(config.get("WORLD_CONSISTENCY"), True):
        world = bible.get("world", {})
        if world:
            sections.append(
                "WORLD CONTINUITY LOCK: Preserve established era, "
                "architecture, locations and environment. Apply only "
                "details relevant to this scene: "
                + json.dumps(
                    world,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

    continuity = scene_continuity_context(
        source,
        previous_main,
        bible,
    )

    if continuity:
        sections.append(
            "CHRONOLOGICAL CONTINUITY: " + continuity
        )

    prompt = "\n\n".join(
        section for section in sections if section
    ).strip()

    if len(prompt) > MAX_PROMPT_LENGTH:
        raise RuntimeError(
            f"{source['scene_id']}: visual prompt exceeds "
            f"{MAX_PROMPT_LENGTH} characters."
        )

    return prompt

def build_config_signature(
    config: dict,
    format_name: str,
    parts_count: int,
    scene_counts: dict[int, int],
    image_size: str,
) -> str:
    keys = (
        "VISUAL_STYLE",
        "REALISM",
        "QUALITY",
        "CAMERA_STYLE",
        "LIGHTING",
        "MOOD",
        "CHARACTER_BIBLE",
        "CHARACTER_CONSISTENCY",
        "WORLD_CONSISTENCY",
        "SCENE_CONTINUITY",
        "AVOID_CARTOON_LOOK",
        "AVOID_NEON",
        "AVOID_GLITCH_EFFECTS",
        "NEGATIVE_PROMPT",
        "NATURAL_MOTION",
        "CINEMATIC_CAMERA",
        "REALISTIC_LIGHTING",
        "PHOTO_MOTION_STYLE",
        "PHOTO_MOTION_PARALLAX",
        "PHOTO_MOTION_PARALLAX_STRENGTH",
        "PHOTO_MOTION_SPEED",
        "PHOTO_MOTION_VARIATION",
        "PHOTO_MOTION_PRESERVE_SUBJECT",
    )

    return digest({
        "version": VERSION,
        "prompt_version": PROMPT_VERSION,
        "format": format_name,
        "parts": parts_count,
        "actual_scene_counts": {str(part): scene_counts[part] for part in sorted(scene_counts)},
        "image_size": image_size,
        "settings": {
            key: config.get(key)
            for key in keys
        },
    })


def build_jobs(
    config: dict,
    scenes: list[dict],
    bible: dict,
    format_name: str,
    source_fingerprint: str,
    config_signature: str,
) -> list[dict]:
    width, height = image_dimensions(format_name)
    image_size = f"{width}x{height}"

    jobs = []
    part_slots: dict[int, int] = {}
    previous_main_by_part: dict[int, dict] = {}

    for global_number, source in enumerate(scenes, 1):
        part = int(source["part"])
        role = normalize_role(source)
        scene_id = clean(source["scene_id"])

        part_slots[part] = part_slots.get(part, 0) + 1
        image_slot = part_slots[part]

        previous_main = (
            previous_main_by_part.get(part)
            if role == "main"
            else None
        )

        prompt = build_prompt(
            config=config,
            source=source,
            bible=bible,
            format_name=format_name,
            width=width,
            height=height,
            previous_main=previous_main,
        )

        negative = build_negative_prompt(config, source)

        duration_value = (
            source.get("duration")
            or source.get("scene_duration")
            or source.get("duration_seconds")
            or source.get("narration_duration")
        )

        try:
            duration = float(duration_value)
            if not 0.5 <= duration <= 3600:
                duration = "auto"
            else:
                duration = round(duration, 4)
        except (TypeError, ValueError):
            duration = "auto"

        image_path = (
            f"output/visuals/images/"
            f"{global_number:04d}_{scene_id}.png"
        )

        character_records = find_scene_character_references(
            source,
            bible,
        )

        character_ids = [
            clean(item.get("id") or item.get("character_id"))
            for item in character_records
        ]

        character_names = [
            clean(item.get("name"))
            for item in character_records
        ]

        source_signature = digest(source)

        prompt_signature = digest({
            "prompt_version": PROMPT_VERSION,
            "prompt": prompt,
            "negative_prompt": negative,
            "image_size": image_size,
            "scene_id": scene_id,
            "part": part,
            "role": role,
            "source_scene_signature": source_signature,
            "config_signature": config_signature,
            "character_ids": character_ids,
        })

        job = {
            "id": scene_id,
            "scene_id": scene_id,
            "source_scene_id": scene_id,
            "part": part,
            "scene": image_slot,
            "local_scene": image_slot,
            "global_scene": global_number,
            "scene_index": global_number,
            "source_scene_number": (
                int(source["source_scene_number"])
                if role == "main"
                else None
            ),
            "source_local_scene": (
                int(source["source_scene_number"])
                if role == "main"
                else None
            ),
            "role": role,
            "scene_type": role,
            "is_extra_scene": role != "main",
            "title": clean(source.get("title")),
            "narration": clean(source.get("narration")),
            "story_anchor": clean(source.get("story_anchor")),
            "voiceover_enabled": True,
            "narration_source": "locked_part_text" if role == "main" else "ai_generated_role_narration",
            "source_text_sha256": text_sha256(clean(source.get("narration"))),
            "source_word_count": len(clean(source.get("narration")).split()),
            "dialogue": clean(source.get("dialogue")),
            "objects": source.get("objects", []),
            "animals": source.get("animals", []),
            "vehicles": source.get("vehicles", []),
            "clothing": source.get("clothing", ""),
            "location": source.get("location", ""),
            "time_period": source.get("time_period", ""),
            "required_story_entities": {
                key: source.get(key)
                for key in (
                    "objects", "animals", "vehicles", "clothing",
                    "location", "time_period", "named_entities",
                    "story_entities",
                )
                if source.get(key) not in (None, "", [], {})
            },
            "prompt": prompt,
            "image_prompt": prompt,
            "visual_prompt": prompt,
            "negative_prompt": negative,
            "image_size": image_size,
            "image_width": width,
            "image_height": height,
            "image_path": image_path,
            "output_path": image_path,
            "duration": duration,
            "scene_duration": duration,
            "duration_source": (
                "source_scene"
                if isinstance(duration, (int, float))
                else (
                    "pending_narration_audio_measurement"
                    if role == "main"
                    else "visual_scene_default"
                )
            ),
            "character_ids": character_ids,
            "characters": character_names,
            "character_names": character_names,
            "character_references": [
                compact_character(item)
                for item in character_records
            ],
            "world_context": clean(
                source.get("world_context")
                or source.get("location")
            ),
            "location": clean(source.get("location")),
            "time_period": clean(source.get("time_period")),
            "camera_prompt": clean(
                source.get("camera_prompt")
                or source.get("camera")
            ),
            "lighting_prompt": clean(
                source.get("lighting_prompt")
                or source.get("lighting")
            ),
            "mood": clean(source.get("mood")),
            "source_scene_signature": source_signature,
            "prompt_signature": prompt_signature,
            "visual_jobs_version": VERSION,
            "prompt_version": PROMPT_VERSION,
            "source_fingerprint": source_fingerprint,
            "config_signature": config_signature,
            "image_status": "pending",
            "image_validation": {
                "required": True,
                "file_integrity": "pending_image_generation",
                "semantic_source": (
                    "scene_visual_prompt_narration_and_required_story_entities"
                ),
                "semantic_validation": (
                    "pending_external_vision_verification"
                ),
                "vision_verification_required": True,
            },
        }

        if len(prompt) < MIN_PROMPT_LENGTH:
            raise RuntimeError(
                f"{scene_id}: generated prompt is too short."
            )

        jobs.append(job)

        if role == "main":
            previous_main_by_part[part] = source

    return jobs


def validate_jobs(
    jobs: list[dict],
    expected_scenes: list[dict],
    expected_parts: int,
    expected_main_scenes: int,
    format_name: str,
) -> None:
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("No visual jobs were created.")

    expected_ids = [
        clean(row.get("scene_id"))
        for row in expected_scenes
    ]
    actual_ids = [
        clean(row.get("scene_id"))
        for row in jobs
    ]

    if actual_ids != expected_ids:
        raise RuntimeError(
            "Visual job IDs/order do not match scenes.json."
        )

    if len(actual_ids) != len(set(actual_ids)):
        raise RuntimeError("Duplicate visual job scene IDs.")

    if len(jobs) != len(expected_scenes):
        raise RuntimeError(
            "Visual job count does not match source scene count."
        )

    expected_width, expected_height = image_dimensions(format_name)

    main_jobs = [
        job for job in jobs
        if job.get("role") == "main"
    ]

    if len(main_jobs) != expected_main_scenes:
        raise RuntimeError(
            f"Main job count mismatch: "
            f"{len(main_jobs)}/{expected_main_scenes}."
        )

    paths = set()
    slots: dict[int, list[int]] = {}

    for index, job in enumerate(jobs, 1):
        scene_id = clean(job.get("scene_id"))

        if job.get("global_scene") != index:
            raise RuntimeError(
                f"{scene_id}: invalid global numbering."
            )

        part = positive_int(job.get("part"), "job part")
        local = positive_int(
            job.get("local_scene"),
            "job local slot",
        )

        if part > expected_parts:
            raise RuntimeError(
                f"{scene_id}: invalid part number."
            )

        slots.setdefault(part, []).append(local)

        if (
            job.get("image_width") != expected_width
            or job.get("image_height") != expected_height
            or job.get("image_size")
            != f"{expected_width}x{expected_height}"
        ):
            raise RuntimeError(
                f"{scene_id}: incorrect image dimensions."
            )

        for field in (
            "prompt",
            "image_path",
            "prompt_signature",
            "source_scene_signature",
            "negative_prompt",
        ):
            if not clean(job.get(field)):
                raise RuntimeError(f"{scene_id}: missing {field}.")
        if job.get("role") == "main":
            narration = clean(job.get("narration"))
            if not narration or job.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: Main narration/voiceover is invalid.")
            if job.get("narration_source") != "locked_part_text":
                raise RuntimeError(f"{scene_id}: Main narration source is not locked.")
            if job.get("source_text_sha256") != text_sha256(narration):
                raise RuntimeError(f"{scene_id}: Main narration hash mismatch.")
            if job.get("source_word_count") != len(narration.split()):
                raise RuntimeError(f"{scene_id}: Main narration word count mismatch.")
        else:
            narration = clean(job.get("narration"))
            if len(narration.split()) < 8:
                raise RuntimeError(f"{scene_id}: extra-role narration is missing.")
            if job.get("voiceover_enabled") is not True or job.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{scene_id}: extra-role narration metadata is invalid.")
            if job.get("source_text_sha256") != text_sha256(narration):
                raise RuntimeError(f"{scene_id}: extra-role narration hash mismatch.")

        parsed = Path(clean(job["image_path"]))

        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or not clean(job["image_path"]).startswith(
                "output/visuals/images/"
            )
        ):
            raise RuntimeError(
                f"{scene_id}: unsafe image output path."
            )

        if str(parsed) in paths:
            raise RuntimeError(
                f"Duplicate image path: {parsed}."
            )

        paths.add(str(parsed))

    for part, values in slots.items():
        if values != list(range(1, len(values) + 1)):
            raise RuntimeError(
                f"Part {part}: image-worker slots are not continuous."
            )


def resolve_image_path(relative_path: str) -> Path:
    parsed = Path(relative_path)

    if parsed.is_absolute() or ".." in parsed.parts:
        raise RuntimeError(
            f"Unsafe image path: {relative_path}."
        )

    root = ROOT.resolve()
    path = (ROOT / parsed).resolve()

    if path != root and root not in path.parents:
        raise RuntimeError(
            f"Image path escapes repository: {relative_path}."
        )

    return path


def validate_png(
    path: Path,
    expected_width: int,
    expected_height: int,
) -> dict:
    """Verify PNG chunks, dimensions, CRCs and decompressed pixel data."""

    path = Path(path)

    if not path.is_file():
        raise RuntimeError(
            f"Image file missing: {path}."
        )

    file_size = path.stat().st_size

    if file_size < 1024:
        raise RuntimeError(
            f"Image file is too small: {path}."
        )

    if file_size > MAX_IMAGE_FILE_SIZE:
        raise RuntimeError(
            f"Image file exceeds size limit: {path}."
        )

    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError(
                f"Image is not a PNG file: {path}."
            )

        width = height = None
        seen_ihdr = False
        seen_iend = False
        compressed_size = 0
        decompressor = zlib.decompressobj()
        pixel_bytes = 0
        unique_bytes = set()
        consumed = 8

        while consumed < file_size:
            header = stream.read(8)

            if len(header) != 8:
                raise RuntimeError(
                    f"Truncated PNG chunk header: {path}."
                )

            length, chunk_type = struct.unpack(">I4s", header)
            consumed += 8

            if (
                length > file_size
                or consumed + length + 4 > file_size
            ):
                raise RuntimeError(
                    f"Invalid PNG chunk length: {path}."
                )

            payload = stream.read(length)
            crc_bytes = stream.read(4)

            if len(payload) != length or len(crc_bytes) != 4:
                raise RuntimeError(
                    f"Truncated PNG chunk: {path}."
                )

            expected_crc = struct.unpack(">I", crc_bytes)[0]
            actual_crc = zlib.crc32(
                chunk_type + payload
            ) & 0xFFFFFFFF

            if actual_crc != expected_crc:
                raise RuntimeError(
                    f"PNG CRC check failed: {path}."
                )

            consumed += length + 4

            if chunk_type == b"IHDR":
                if seen_ihdr or length != 13:
                    raise RuntimeError(
                        f"Invalid PNG IHDR: {path}."
                    )

                (
                    width,
                    height,
                    bit_depth,
                    color_type,
                    compression,
                    filtering,
                    interlace,
                ) = struct.unpack(">IIBBBBB", payload)

                if (
                    width != expected_width
                    or height != expected_height
                    or bit_depth not in {8, 16}
                    or color_type not in {2, 6}
                    or compression != 0
                    or filtering != 0
                    or interlace not in {0, 1}
                ):
                    raise RuntimeError(
                        f"PNG dimensions/encoding invalid: {path} "
                        f"({width}x{height}, color={color_type})."
                    )

                seen_ihdr = True

            elif chunk_type == b"IDAT":
                if not seen_ihdr:
                    raise RuntimeError(
                        f"PNG IDAT occurs before IHDR: {path}."
                    )

                compressed_size += length

                if compressed_size > MAX_IMAGE_FILE_SIZE:
                    raise RuntimeError(
                        f"PNG compressed data exceeds size limit: {path}."
                    )

                try:
                    decoded = decompressor.decompress(payload)
                except zlib.error as exc:
                    raise RuntimeError(
                        f"PNG pixel stream is corrupt: {path}."
                    ) from exc

                pixel_bytes += len(decoded)
                unique_bytes.update(decoded[:65536])

            elif chunk_type == b"IEND":
                if length != 0:
                    raise RuntimeError(
                        f"Invalid PNG IEND: {path}."
                    )

                seen_iend = True
                break

        if not seen_ihdr or not seen_iend or compressed_size == 0:
            raise RuntimeError(
                f"Incomplete PNG image: {path}."
            )

        try:
            tail = decompressor.flush()
        except zlib.error as exc:
            raise RuntimeError(
                f"PNG decompression failed: {path}."
            ) from exc

        pixel_bytes += len(tail)
        unique_bytes.update(tail[:65536])

        if not decompressor.eof:
            raise RuntimeError(
                f"PNG pixel stream is incomplete: {path}."
            )

        if decompressor.unused_data:
            raise RuntimeError(
                f"PNG contains unexpected trailing compressed data: {path}."
            )

    if pixel_bytes < expected_height:
        raise RuntimeError(
            f"PNG contains insufficient pixel data: {path}."
        )

    if len(unique_bytes) < 8:
        raise RuntimeError(
            f"PNG pixel payload is blank or invalid: {path}."
        )

    return {
        "width": width,
        "height": height,
        "size_bytes": file_size,
        "sha256": file_sha256(path),
        "integrity": "png_chunks_crc_and_pixel_stream_verified",
        "semantic_verification": (
            "pending_external_vision_verification"
        ),
    }


def load_previous_progress() -> dict:
    previous = read_json(CHECKPOINT_FILE)

    if not isinstance(previous, dict):
        return {}

    rows = previous.get("scene_progress", [])

    if not isinstance(rows, list):
        return {}

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        scene_id = clean(row.get("scene_id"))
        signature = clean(row.get("prompt_signature"))

        if scene_id and signature:
            key = (scene_id, signature)

            if key in result:
                raise RuntimeError(
                    f"Duplicate previous progress record: {scene_id}."
                )

            result[key] = row

    return result


def verified_previous_image(
    old: dict,
    job: dict,
) -> dict | None:
    if clean(old.get("status")).lower() not in {
        "generated",
        "existing",
        "completed",
    }:
        return None

    checks = (
        ("image_path", job.get("image_path")),
        ("prompt_signature", job.get("prompt_signature")),
        (
            "source_scene_signature",
            job.get("source_scene_signature"),
        ),
        ("source_fingerprint", job.get("source_fingerprint")),
        ("config_signature", job.get("config_signature")),
    )

    for field, expected in checks:
        if clean(old.get(field)) != clean(expected):
            return None

    expected_hash = clean(old.get("image_sha256"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash):
        return None

    path = resolve_image_path(job["image_path"])

    try:
        info = validate_png(
            path,
            int(job["image_width"]),
            int(job["image_height"]),
        )
    except (OSError, RuntimeError, ValueError):
        return None

    if info["sha256"].casefold() != expected_hash.casefold():
        return None

    return info


def build_progress(
    jobs: list[dict],
    previous: dict,
    source_fingerprint: str,
    config_signature: str,
) -> dict:
    rows = []
    reused = 0

    for job in jobs:
        old = previous.get(
            (
                job["scene_id"],
                job["prompt_signature"],
            ),
            {},
        )

        image_info = verified_previous_image(old, job)
        status = "existing" if image_info else "pending"

        if image_info:
            reused += 1

        rows.append({
            "scene_id": job["scene_id"],
            "part": job["part"],
            "global_scene": job["global_scene"],
            "role": job["role"],
            "image_path": job["image_path"],
            "prompt_signature": job["prompt_signature"],
            "source_scene_signature": job[
                "source_scene_signature"
            ],
            "source_fingerprint": source_fingerprint,
            "config_signature": config_signature,
            "status": status,
            "image_sha256": (
                image_info["sha256"]
                if image_info else ""
            ),
            "image_integrity": (
                image_info["integrity"]
                if image_info else "not_verified"
            ),
            "semantic_verification": (
                "pending_external_vision_verification"
                if image_info
                else "pending_image_generation"
            ),
        })

    return {
        "version": VERSION,
        "status": "ready",
        "stage": "visual_jobs_prepared",
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "total_jobs": len(jobs),
        "prepared_jobs": len(jobs),
        "completed_jobs": reused,
        "remaining_jobs": len(jobs) - reused,
        "scene_progress": rows,
        "scene_order": [
            {
                "part": job["part"],
                "scene": job["scene"],
                "source_scene_number": job[
                    "source_scene_number"
                ],
                "global_scene": job["global_scene"],
                "scene_id": job["scene_id"],
                "role": job["role"],
                "image_path": job["image_path"],
                "prompt_signature": job["prompt_signature"],
            }
            for job in jobs
        ],
    }


def validate_saved_artifacts(
    jobs: list[dict],
    manifest: dict,
    progress: dict,
    source_fingerprint: str,
    config_signature: str,
) -> None:
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "Saved visual_jobs.json is empty or invalid."
        )

    if manifest.get("status") != "completed":
        raise RuntimeError(
            "Visual jobs manifest is not completed."
        )

    if manifest.get("source_fingerprint") != source_fingerprint:
        raise RuntimeError(
            "Visual jobs manifest source fingerprint mismatch."
        )

    if manifest.get("config_signature") != config_signature:
        raise RuntimeError(
            "Visual jobs manifest config signature mismatch."
        )

    if progress.get("status") != "ready":
        raise RuntimeError(
            "Visual progress checkpoint is not ready."
        )

    if progress.get("source_fingerprint") != source_fingerprint:
        raise RuntimeError(
            "Visual progress source fingerprint mismatch."
        )

    if progress.get("config_signature") != config_signature:
        raise RuntimeError(
            "Visual progress config signature mismatch."
        )

    rows = progress.get("scene_progress")

    if not isinstance(rows, list) or len(rows) != len(jobs):
        raise RuntimeError(
            "Visual progress scene count mismatch."
        )

    for job, row in zip(jobs, rows):
        if (
            job.get("scene_id") != row.get("scene_id")
            or job.get("prompt_signature")
            != row.get("prompt_signature")
            or job.get("source_scene_signature")
            != row.get("source_scene_signature")
        ):
            raise RuntimeError(
                f"{job.get('scene_id')}: saved job/checkpoint mismatch."
            )

        if row.get("status") == "existing":
            image_info = verified_previous_image(row, job)
            if not image_info:
                raise RuntimeError(
                    f"{job['scene_id']}: reused image failed "
                    "read-after-write verification."
                )


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — STORY-GROUNDED VISUAL JOB BUILDER")
    log("VERSION:", VERSION)
    log("IMAGE API CALLS: 0")
    log("=" * 64)

    config = load_and_validate(INPUT_FILE)

    if not isinstance(config, dict):
        raise RuntimeError(
            "Input configuration is invalid."
        )

    format_name = get_format(config)
    parts_count = positive_int(
        get_parts(config),
        "PARTS",
    )
    scenes_min, scenes_max = get_scenes_range(config)

    width, height = image_dimensions(format_name)
    image_size = f"{width}x{height}"

    source_document = read_json(
        SCENES_FILE,
        required=True,
    )
    bible = load_character_bible(config)

    raw_counts = source_document.get("actual_scene_counts")
    if not isinstance(raw_counts, list) or len(raw_counts) != parts_count:
        raise RuntimeError("scenes.json actual_scene_counts must contain one count per selected Part.")
    scene_counts = {}
    for part, raw_count in enumerate(raw_counts, 1):
        count = positive_int(raw_count, f"Part {part} actual Scene count")
        if not scenes_min <= count <= scenes_max:
            raise RuntimeError(
                f"Part {part}: actual Scene count {count} is outside configured range {scenes_min}-{scenes_max}."
            )
        scene_counts[part] = count

    if source_document.get("scenes_min") != scenes_min or source_document.get("scenes_max") != scenes_max:
        raise RuntimeError("scenes.json SCENES range does not match Input/topic.txt.")

    scenes = normalize_scene_rows(
        source_document,
        expected_parts=parts_count,
        expected_scene_counts=scene_counts,
    )

    bible_enabled = as_bool(
        config.get("CHARACTER_BIBLE"),
        True,
    )

    validate_bible_coverage(
        scenes,
        bible,
        bible_enabled,
    )

    source_fingerprint = digest({
        "scenes": source_document,
        "character_bible": bible,
    })

    config_signature = build_config_signature(
        config,
        format_name,
        parts_count,
        scene_counts,
        image_size,
    )

    jobs = build_jobs(
        config=config,
        scenes=scenes,
        bible=bible,
        format_name=format_name,
        source_fingerprint=source_fingerprint,
        config_signature=config_signature,
    )

    validate_jobs(
        jobs,
        scenes,
        parts_count,
        sum(scene_counts.values()),
        format_name,
    )

    role_counts = {
        role: sum(
            1 for job in jobs
            if job["role"] == role
        )
        for role in ROLE_ORDER
    }

    previous = load_previous_progress()

    progress = build_progress(
        jobs,
        previous,
        source_fingerprint,
        config_signature,
    )

    manifest = {
        "version": VERSION,
        "prompt_version": PROMPT_VERSION,
        "status": "completed",
        "stage": "visual_jobs_prepared",
        "format": format_name,
        "parts": parts_count,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": {str(part): scene_counts[part] for part in sorted(scene_counts)},
        "total_jobs": len(jobs),
        "total_main_scenes": sum(scene_counts.values()),
        "total_extra_scenes": len(jobs) - sum(scene_counts.values()),
        "scene_role_counts": role_counts,
        "image_size": image_size,
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "character_bible_status": bible.get(
            "status",
            "unknown",
        ),
        "character_bible_fingerprint": clean(
            bible.get("source_fingerprint")
        ),
        "character_count": len(
            bible.get("characters", [])
        ),
        "scene_order": (
            "part_then_hook_then_main_then_suspense_or_resolution"
        ),
        "image_api_calls": 0,
        "semantic_image_verification": (
            "pending_external_vision_verification"
        ),
        "vision_verification_required": True,
        "jobs_file": "output/visuals/visual_jobs.json",
    }

    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    atomic_json(JOBS_FILE, jobs)
    atomic_json(MANIFEST_FILE, manifest)
    atomic_json(CHECKPOINT_FILE, progress)

    saved_jobs = read_json(
        JOBS_FILE,
        required=True,
    )
    saved_manifest = read_json(
        MANIFEST_FILE,
        required=True,
    )
    saved_progress = read_json(
        CHECKPOINT_FILE,
        required=True,
    )

    validate_saved_artifacts(
        saved_jobs,
        saved_manifest,
        saved_progress,
        source_fingerprint,
        config_signature,
    )

    log("-" * 64)
    log("VISUAL JOBS READY")
    log("Format:", format_name)
    log("Image dimensions:", image_size)
    log("Parts:", parts_count)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("TOTAL MAIN SCENES:", sum(scene_counts.values()))

    for role in ROLE_ORDER:
        log(role.upper() + ":", role_counts[role])

    log("Total image jobs:", len(jobs))
    log(
        "Verified reusable images:",
        progress["completed_jobs"],
    )
    log(
        "Images requiring generation:",
        progress["remaining_jobs"],
    )
    log("Image API calls: 0")
    log(
        "Semantic image verification:",
        "pending external vision verification",
    )
    log(
        "Jobs file:",
        JOBS_FILE.relative_to(ROOT),
    )
    log(
        "Manifest:",
        MANIFEST_FILE.relative_to(ROOT),
    )
    log(
        "Checkpoint:",
        CHECKPOINT_FILE.relative_to(ROOT),
    )
    log("-" * 64)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("VISUAL JOB GENERATION CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log(
            "VISUAL JOB GENERATION FAILED:",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)