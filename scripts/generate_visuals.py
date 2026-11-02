#!/usr/bin/env python3
"""Build validated, story-grounded and resumable visual jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
)

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = OUT / "scenes" / "scenes.json"
BIBLE_FILE = OUT / "story" / "character_bible.json"

VISUALS_DIR = OUT / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
JOBS_MANIFEST_FILE = VISUALS_DIR / "visual_jobs_manifest.json"
CHECKPOINT_FILE = OUT / "checkpoints" / "visual_jobs_progress.json"

VERSION = "photo-motion-visual-jobs-v7-story-grounded-list-schema"
PROMPT_VERSION = 7
MIN_PROMPT_LENGTH = 24
MIN_SCENE_TEXT = 8

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
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_json(path: Path, required: bool = False) -> Any:
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON file {path}: {exc}") from exc


def atomic_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_number(value: Any, default: Any = "auto") -> Any:
    if value is None:
        return default

    if isinstance(value, str) and value.strip().lower() in {
        "", "auto", "dynamic", "variable", "unknown"
    }:
        return "auto"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default

    if not 0.5 <= number <= 3600:
        return default

    return round(number, 4)


def normalize_role(scene: dict) -> str:
    role = clean(
        scene.get("role")
        or scene.get("scene_type")
        or "main"
    ).lower()

    if role not in ALLOWED_ROLES:
        raise RuntimeError(
            f"Unsupported scene role {role!r} "
            f"for scene {scene.get('scene_id')!r}."
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

    if not scene_id:
        if role == "main":
            scene_id = f"part_{part:02d}_scene_{local_scene:02d}"
        else:
            scene_id = f"part_{part:02d}_{role}"

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
        raise RuntimeError(f"Unsafe or invalid scene_id: {scene_id!r}")

    return scene_id


def normalize_character_values(value: Any) -> list[str]:
    if value is None:
        return []

    if not isinstance(value, list):
        value = [value]

    result = []

    for item in value:
        if isinstance(item, dict):
            candidate = (
                item.get("id")
                or item.get("character_id")
                or item.get("name")
            )
        else:
            candidate = item

        text = clean(candidate)
        if text and text.casefold() not in {
            existing.casefold() for existing in result
        }:
            result.append(text)

    return result


def load_character_bible(config: dict) -> dict:
    enabled = as_bool(config.get("CHARACTER_BIBLE"), True)
    data = read_json(BIBLE_FILE)

    if data is None:
        if enabled:
            log("WARNING: Character Bible missing; visual jobs will use scene references only.")
        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "world": {},
        }

    if not isinstance(data, dict):
        raise RuntimeError("Character Bible root must be a JSON object.")

    status = clean(data.get("status")).lower()

    if status not in {"completed", "disabled"}:
        raise RuntimeError(
            f"Character Bible is not ready: status={status or 'missing'}."
        )

    characters = data.get("characters", [])
    scene_map = data.get("scene_character_map", [])
    world = data.get("world", {})

    if not isinstance(characters, list):
        raise RuntimeError("Character Bible characters must be a list.")
    if not isinstance(scene_map, list):
        raise RuntimeError("Character Bible scene_character_map must be a list.")
    if not isinstance(world, dict):
        raise RuntimeError("Character Bible world must be an object.")

    known_ids = set()
    known_names = set()

    for index, character in enumerate(characters, 1):
        if not isinstance(character, dict):
            raise RuntimeError(f"Invalid Character Bible record #{index}.")

        character_id = clean(
            character.get("id") or character.get("character_id")
        )
        name = clean(character.get("name"))

        if not character_id or not name:
            raise RuntimeError(
                f"Character Bible record #{index} requires an ID and name."
            )

        if character_id.casefold() in known_ids:
            raise RuntimeError(f"Duplicate Character Bible ID: {character_id}")

        known_ids.add(character_id.casefold())
        known_names.add(name.casefold())

    for index, row in enumerate(scene_map, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid Character Bible scene map row #{index}.")

        references = normalize_character_values(
            row.get("character_ids")
        )

        unknown = [
            value for value in references
            if value.casefold() not in known_ids
            and value.casefold() not in known_names
        ]

        if unknown:
            raise RuntimeError(
                f"Character Bible mapping #{index} contains unknown "
                f"characters: {unknown[:10]}"
            )

    return data


def find_scene_character_references(
    scene: dict,
    bible: dict,
) -> list[dict]:
    wanted = {
        value.casefold()
        for value in normalize_character_values(
            scene.get("character_ids")
            or scene.get("characters")
            or scene.get("character_names")
        )
    }

    scene_id = clean(scene.get("scene_id"))
    part = scene.get("part")
    scene_number = scene.get("scene")

    for row in bible.get("scene_character_map", []):
        if not isinstance(row, dict):
            continue

        same_id = (
            bool(scene_id)
            and clean(row.get("scene_id")) == scene_id
        )
        same_position = (
            scene_number is not None
            and row.get("part") == part
            and row.get("scene") == scene_number
        )

        if same_id or same_position:
            for key in (
                "character_ids",
                "character_names",
                "characters",
            ):
                wanted.update(
                    value.casefold()
                    for value in normalize_character_values(row.get(key))
                )

    selected = []

    for character in bible.get("characters", []):
        if not isinstance(character, dict):
            continue

        candidates = {
            clean(character.get("id")).casefold(),
            clean(character.get("character_id")).casefold(),
            clean(character.get("name")).casefold(),
        }

        aliases = normalize_character_values(character.get("aliases"))
        candidates.update(value.casefold() for value in aliases)

        if wanted.intersection(candidates):
            selected.append(character)

    return selected


def compact_character_reference(character: dict) -> dict:
    fields = (
        "id",
        "character_id",
        "name",
        "aliases",
        "age",
        "gender",
        "appearance",
        "face_features",
        "skin_tone",
        "hair",
        "eyes",
        "body_features",
        "height",
        "build",
        "clothing",
        "accessories",
        "distinctive_features",
        "visual_identity",
        "continuity_notes",
        "consistency_rules",
    )

    result = {}

    for key in fields:
        value = character.get(key)
        if value not in (None, "", [], {}):
            result[key] = value

    return result


def character_prompt_context(scene: dict, bible: dict) -> str:
    selected = find_scene_character_references(scene, bible)

    if not selected:
        return ""

    compact = [
        compact_character_reference(character)
        for character in selected
    ]

    return json.dumps(
        compact,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_negative_prompt(config: dict, source: dict) -> str:
    values = [
        "distorted face",
        "unrealistic anatomy",
        "extra fingers",
        "missing fingers",
        "fused limbs",
        "duplicated people",
        "plastic skin",
        "waxy face",
        "crossed eyes",
        "deformed hands",
        "floating objects",
        "wrong story event",
        "unrelated characters",
        "unrelated props",
        "wrong location",
        "wrong time period",
        "captions",
        "subtitles",
        "written text",
        "logo",
        "watermark",
        "low resolution",
        "blurry main subject",
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


def build_prompt(
    config: dict,
    source: dict,
    bible: dict,
    format_name: str,
    width: int,
    height: int,
) -> str:
    visual = clean(
        source.get("visual_prompt")
        or source.get("visual")
        or source.get("description")
    )

    narration = clean(
        source.get("narration")
        or source.get("text")
        or source.get("story_text")
    )

    if len(visual) < MIN_PROMPT_LENGTH:
        raise RuntimeError(
            f"Scene {source.get('scene_id')!r} has no usable visual_prompt."
        )

    if len(narration) < MIN_SCENE_TEXT:
        raise RuntimeError(
            f"Scene {source.get('scene_id')!r} has no usable narration."
        )

    role = normalize_role(source)

    style = clean(config.get("VISUAL_STYLE")) or "cinematic_realistic"
    realism = clean(config.get("REALISM")) or "high"
    quality = clean(config.get("QUALITY")) or "high"
    camera = clean(config.get("CAMERA_STYLE")) or "cinematic"
    lighting = clean(config.get("LIGHTING")) or "cinematic"
    mood = clean(source.get("mood") or config.get("MOOD")) or "dramatic"

    sections = [
        "STORY-SPECIFIC IMAGE: " + visual,
        (
            "Create one coherent, photorealistic live-action cinematic "
            "film frame. Use realistic human actors, natural skin texture, "
            "believable facial features, accurate anatomy and real-world "
            "materials. It must look like a frame from a professionally "
            "shot live-action film, not an illustration or slideshow."
        ),
        (
            "Show the exact action and story moment described by this "
            "scene. Preserve the scene's actual people, relationships, "
            "location, time period, objects and emotional meaning. "
            "Do not invent a different event or replace specific story "
            "details with generic imagery."
        ),
        (
            "The visual_prompt is the primary visual instruction. "
            "Narration is context for understanding the event, not a "
            "request to print words into the image."
        ),
        (
            f"Output composition: {format_name}; exact target dimensions "
            f"{width}x{height}; preserve the complete important subject "
            "inside the frame with suitable headroom and safe margins."
        ),
        f"Visual style: {style}. Realism: {realism}. Quality: {quality}.",
        f"Camera language: {camera}.",
        f"Lighting: {clean(source.get('lighting_prompt') or source.get('lighting') or lighting)}.",
        f"Emotional mood: {mood}.",
        (
            "Use physically plausible lighting, natural shadows, "
            "realistic reflections, believable depth, correct perspective "
            "and cinematic composition."
        ),
        (
            "No captions, subtitles, typography, labels, speech bubbles, "
            "logos, watermarks or readable text."
        ),
    ]

    if role != "main":
        sections.append(
            f"SCENE ROLE: {role.upper()}. Preserve this as its own "
            "separate story image. Do not merge it with another scene "
            "or change the main scene numbering."
        )

    for key, label in (
        ("action", "Visible physical action"),
        ("world_context", "Story-world continuity"),
        ("location", "Exact location"),
        ("time_period", "Time period"),
        ("camera_prompt", "Camera direction"),
        ("dialogue", "Dialogue context for facial expression and action"),
    ):
        value = clean(source.get(key))
        if value:
            sections.append(f"{label}: {value}")

    if as_bool(config.get("CHARACTER_CONSISTENCY"), True):
        context = character_prompt_context(source, bible)

        if context:
            sections.append(
                "CHARACTER IDENTITY LOCK: Use only the approved character "
                "references relevant to this scene. Preserve the same "
                "recognizable face, age range, skin tone, hair, eyes, "
                "body proportions and identity across every scene. "
                "Do not swap, merge or randomly redesign characters. "
                "Change clothing only when the story explicitly requires it. "
                "Approved references: " + context
            )

    if as_bool(config.get("WORLD_CONSISTENCY"), True):
        world = bible.get("world", {})

        if world:
            sections.append(
                "STORY WORLD CONTINUITY: Preserve this world's established "
                "locations, era, architecture, environment and visual rules: "
                + json.dumps(world, ensure_ascii=False, separators=(",", ":"))
            )

    return "\n\n".join(
        section for section in sections if section
    ).strip()


def image_dimensions(format_name: str) -> tuple[int, int]:
    if format_name not in IMAGE_SIZES:
        raise RuntimeError(f"Unsupported output format: {format_name!r}")
    return IMAGE_SIZES[format_name]


def scene_order_key(scene: dict) -> tuple:
    role = normalize_role(scene)
    part = int(scene["part"])

    try:
        local = int(scene.get("scene") or 0)
    except (TypeError, ValueError):
        local = 0

    return (
        part,
        ROLE_ORDER[role],
        local,
        clean(scene.get("scene_id")),
    )


def normalize_scene_rows(
    document: dict,
    expected_parts: int,
    expected_scenes: int,
) -> list[dict]:
    if not isinstance(document, dict):
        raise RuntimeError("scenes.json root must be an object.")

    rows = document.get("scenes")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json has no scenes array.")

    prepared = []
    seen_ids = set()
    seen_main = set()

    role_counts = {
        role: {}
        for role in ALLOWED_ROLES
    }

    for index, original in enumerate(rows, 1):
        if not isinstance(original, dict):
            raise RuntimeError(f"Scene row {index} is not a JSON object.")

        try:
            part = int(original.get("part"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part number in scene row {index}."
            ) from exc

        if not 1 <= part <= expected_parts:
            raise RuntimeError(
                f"Scene row {index}: part {part} is outside "
                f"the configured range 1-{expected_parts}."
            )

        role = normalize_role(original)
        raw_scene = original.get(
            "scene",
            original.get("local_scene"),
        )

        if role == "main":
            try:
                local_scene = int(raw_scene)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Main scene in part {part} has no valid scene number."
                ) from exc

            if not 1 <= local_scene <= expected_scenes:
                raise RuntimeError(
                    f"Main scene number outside configured range: "
                    f"{part}/{local_scene}."
                )

            pair = (part, local_scene)
            if pair in seen_main:
                raise RuntimeError(
                    f"Duplicate main scene mapping: {part}/{local_scene}."
                )

            seen_main.add(pair)
            source_scene_number = local_scene

        else:
            source_scene_number = None

            if raw_scene not in (None, ""):
                try:
                    int(raw_scene)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Extra scene {original.get('scene_id')!r} "
                        "contains an invalid source scene number."
                    ) from exc

        scene_id = normalize_scene_id(
            original,
            part,
            role,
            source_scene_number,
        )

        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate visual scene ID: {scene_id}.")

        seen_ids.add(scene_id)
        role_counts[role][part] = role_counts[role].get(part, 0) + 1

        prepared.append({
            **dict(original),
            "part": part,
            "scene": source_scene_number,
            "local_scene": source_scene_number,
            "scene_id": scene_id,
            "id": scene_id,
            "role": role,
            "scene_type": role,
            "source_scene_number": source_scene_number,
            "is_extra_scene": role != "main",
        })

    expected_main = {
        (part, scene)
        for part in range(1, expected_parts + 1)
        for scene in range(1, expected_scenes + 1)
    }

    missing_main = sorted(expected_main - seen_main)
    unexpected_main = sorted(seen_main - expected_main)

    if missing_main or unexpected_main:
        raise RuntimeError(
            "Main scene mapping mismatch. "
            f"Missing={missing_main[:20]}, "
            f"unexpected={unexpected_main[:20]}."
        )

    for part in range(1, expected_parts + 1):
        if role_counts["hook"].get(part, 0) != 1:
            raise RuntimeError(
                f"Part {part} must contain exactly one separate Hook scene."
            )

        if part < expected_parts:
            if role_counts["suspense"].get(part, 0) != 1:
                raise RuntimeError(
                    f"Non-final part {part} must contain exactly one "
                    "separate Suspense scene."
                )

            if role_counts["resolution"].get(part, 0) != 0:
                raise RuntimeError(
                    f"Non-final part {part} must not contain Resolution."
                )

        else:
            if role_counts["resolution"].get(part, 0) != 1:
                raise RuntimeError(
                    "Final part must contain exactly one separate "
                    "Resolution scene."
                )

            if role_counts["suspense"].get(part, 0) != 0:
                raise RuntimeError(
                    "Final part must not contain a Suspense scene."
                )

    return sorted(prepared, key=scene_order_key)

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

    for global_number, source in enumerate(scenes, 1):
        part = int(source["part"])
        role = normalize_role(source)

        part_slots[part] = part_slots.get(part, 0) + 1
        image_slot = part_slots[part]

        scene_id = clean(source["scene_id"])

        prompt = build_prompt(
            config=config,
            source=source,
            bible=bible,
            format_name=format_name,
            width=width,
            height=height,
        )

        negative = build_negative_prompt(config, source)

        duration = safe_number(
            source.get("duration")
            or source.get("scene_duration")
            or source.get("duration_seconds")
            or source.get("narration_duration"),
            default="auto",
        )

        # Store images in one canonical image directory.
        # No legacy part_XX/scene_XX image-manifest layout is created.
        relative_image = (
            f"output/visuals/images/"
            f"{global_number:04d}_{scene_id}.png"
        )

        source_scene_number = (
            int(source["source_scene_number"])
            if role == "main"
            else None
        )

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
        })

        job = {
            "id": scene_id,
            "scene_id": scene_id,
            "source_scene_id": scene_id,

            "part": part,

            # Sequential image-worker slot within this part.
            "scene": image_slot,
            "local_scene": image_slot,

            # Global order is continuous across all parts and extra roles.
            "global_scene": global_number,
            "scene_index": global_number,

            # Preserve the original numbered story scene separately.
            "source_scene_number": source_scene_number,
            "source_local_scene": source_scene_number,

            "role": role,
            "scene_type": role,
            "is_extra_scene": role != "main",

            "title": clean(source.get("title")),
            "narration": clean(
                source.get("narration")
                or source.get("text")
                or source.get("story_text")
            ),
            "dialogue": clean(source.get("dialogue")),

            "prompt": prompt,
            "image_prompt": prompt,
            "visual_prompt": prompt,
            "negative_prompt": negative,

            "image_size": image_size,
            "image_width": width,
            "image_height": height,

            "image_path": relative_image,
            "output_path": relative_image,

            "duration": duration,
            "scene_duration": duration,
            "duration_source": (
                "source_scene"
                if isinstance(duration, (int, float))
                else "pending_narration_audio_measurement"
            ),

            "character_ids": normalize_character_values(
                source.get("character_ids")
                or source.get("characters")
                or source.get("character_names")
            ),
            "characters": normalize_character_values(
                source.get("characters")
                or source.get("character_names")
            ),

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
        }

        if len(prompt) < MIN_PROMPT_LENGTH:
            raise RuntimeError(
                f"Generated prompt is too short for scene {scene_id}."
            )

        jobs.append(job)

    return jobs


def validate_jobs(
    jobs: list[dict],
    expected_scenes: list[dict],
    expected_parts: int,
    expected_main_scenes: int,
    format_name: str,
) -> None:
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("No visual jobs were generated.")

    expected_ids = [
        clean(scene.get("scene_id"))
        for scene in expected_scenes
    ]
    actual_ids = [
        clean(job.get("scene_id"))
        for job in jobs
    ]

    if len(actual_ids) != len(set(actual_ids)):
        raise RuntimeError("Duplicate scene_id in visual jobs.")

    if actual_ids != expected_ids:
        raise RuntimeError(
            "Visual jobs do not match the validated story-scene order."
        )

    if len(jobs) != len(expected_scenes):
        raise RuntimeError(
            f"Visual job count mismatch: {len(jobs)}/{len(expected_scenes)}."
        )

    main_jobs = [
        job for job in jobs
        if job.get("role") == "main"
    ]

    if len(main_jobs) != expected_main_scenes:
        raise RuntimeError(
            "Main visual job count mismatch: "
            f"{len(main_jobs)}/{expected_main_scenes}."
        )

    expected_width, expected_height = image_dimensions(format_name)

    seen_paths = set()
    part_slots: dict[int, list[int]] = {}

    for index, job in enumerate(jobs, 1):
        if job.get("global_scene") != index:
            raise RuntimeError(
                f"Global numbering error at {job.get('scene_id')}."
            )

        try:
            part = int(job.get("part"))
            local = int(job.get("local_scene"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part or local slot for {job.get('scene_id')}."
            ) from exc

        if not 1 <= part <= expected_parts:
            raise RuntimeError(
                f"Invalid part mapping for {job.get('scene_id')}."
            )

        if local < 1:
            raise RuntimeError(
                f"Invalid image-worker slot for {job.get('scene_id')}."
            )

        part_slots.setdefault(part, []).append(local)

        if job.get("image_width") != expected_width:
            raise RuntimeError(
                f"Incorrect image width for {job.get('scene_id')}."
            )

        if job.get("image_height") != expected_height:
            raise RuntimeError(
                f"Incorrect image height for {job.get('scene_id')}."
            )

        if job.get("image_size") != f"{expected_width}x{expected_height}":
            raise RuntimeError(
                f"Incorrect image_size for {job.get('scene_id')}."
            )

        if not clean(job.get("prompt")):
            raise RuntimeError(
                f"Empty visual prompt for {job.get('scene_id')}."
            )

        if not clean(job.get("narration")):
            raise RuntimeError(
                f"Missing narration for {job.get('scene_id')}."
            )

        if not clean(job.get("image_path")):
            raise RuntimeError(
                f"Missing image path for {job.get('scene_id')}."
            )

        if not clean(job.get("prompt_signature")):
            raise RuntimeError(
                f"Missing prompt signature for {job.get('scene_id')}."
            )

        path = clean(job.get("image_path"))

        if path in seen_paths:
            raise RuntimeError(f"Duplicate image output path: {path}")

        seen_paths.add(path)

        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise RuntimeError(
                f"Unsafe image output path for {job.get('scene_id')}: {path}"
            )

    for part, slots in part_slots.items():
        if slots != list(range(1, len(slots) + 1)):
            raise RuntimeError(
                f"Image-worker slots for part {part} are not continuous."
            )


def build_config_signature(
    config: dict,
    format_name: str,
    parts_count: int,
    scenes_per_part: int,
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
    )

    settings = {
        key: config.get(key)
        for key in keys
    }

    return digest({
        "version": VERSION,
        "prompt_version": PROMPT_VERSION,
        "format": format_name,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "image_size": image_size,
        "settings": settings,
    })


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
            result[(scene_id, signature)] = row

    return result


def build_progress(
    jobs: list[dict],
    source_fingerprint: str,
    config_signature: str,
    previous_progress: dict,
) -> dict:
    scene_progress = []
    completed = 0

    for job in jobs:
        key = (
            clean(job.get("scene_id")),
            clean(job.get("prompt_signature")),
        )
        old = previous_progress.get(key, {})

        # Keep only matching records. A changed scene or prompt cannot
        # inherit a stale completion state.
        old_status = clean(old.get("status")).lower()
        status = (
            old_status
            if old_status in {"generated", "existing", "completed"}
            else "pending"
        )

        if status in {"generated", "existing", "completed"}:
            completed += 1

        scene_progress.append({
            "scene_id": job["scene_id"],
            "part": job["part"],
            "global_scene": job["global_scene"],
            "role": job["role"],
            "image_path": job["image_path"],
            "prompt_signature": job["prompt_signature"],
            "status": status,
            "image_sha256": clean(old.get("image_sha256")),
        })

    total = len(jobs)

    return {
        "version": VERSION,
        "status": "ready",
        "stage": "visual_jobs_prepared",
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "total_jobs": total,
        "prepared_jobs": total,
        "completed_jobs": completed,
        "remaining_jobs": total - completed,
        "scene_progress": scene_progress,
        "scene_order": [
            {
                "part": job["part"],
                "scene": job["scene"],
                "source_scene_number": job["source_scene_number"],
                "global_scene": job["global_scene"],
                "scene_id": job["scene_id"],
                "role": job["role"],
                "image_path": job["image_path"],
                "prompt_signature": job["prompt_signature"],
            }
            for job in jobs
        ],
    }


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — STORY-GROUNDED VISUAL JOB BUILDER")
    log("VERSION:", VERSION)
    log("IMAGE API CALLS: 0")
    log("=" * 64)

    config = load_and_validate(INPUT_FILE)

    format_name = get_format(config)
    parts_count = get_parts(config)
    scenes_per_part = get_scenes(config)

    if format_name not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported output format: {format_name!r}."
        )

    if not isinstance(parts_count, int) or parts_count < 1:
        raise RuntimeError("PARTS must be a positive integer.")

    if not isinstance(scenes_per_part, int) or scenes_per_part < 1:
        raise RuntimeError("SCENES must be a positive integer.")

    source_document = read_json(SCENES_FILE, required=True)

    if not isinstance(source_document, dict):
        raise RuntimeError("scenes.json root must be an object.")

    bible = load_character_bible(config)

    scenes = normalize_scene_rows(
        source_document,
        expected_parts=parts_count,
        expected_scenes=scenes_per_part,
    )

    source_fingerprint = digest({
        "scenes": source_document,
        "character_bible": bible,
    })

    width, height = image_dimensions(format_name)
    image_size = f"{width}x{height}"

    config_signature = build_config_signature(
        config=config,
        format_name=format_name,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        image_size=image_size,
    )

    jobs = build_jobs(
        config=config,
        scenes=scenes,
        bible=bible,
        format_name=format_name,
        source_fingerprint=source_fingerprint,
        config_signature=config_signature,
    )

    expected_main_scenes = parts_count * scenes_per_part

    validate_jobs(
        jobs=jobs,
        expected_scenes=scenes,
        expected_parts=parts_count,
        expected_main_scenes=expected_main_scenes,
        format_name=format_name,
    )

    role_counts = {
        role: sum(1 for job in jobs if job.get("role") == role)
        for role in ROLE_ORDER
    }

    previous_progress = load_previous_progress()

    progress = build_progress(
        jobs=jobs,
        source_fingerprint=source_fingerprint,
        config_signature=config_signature,
        previous_progress=previous_progress,
    )

    # The canonical worker input is one JSON list.
    # Metadata is stored separately so image workers can consume
    # the list directly without schema conversion.
    manifest = {
        "version": VERSION,
        "prompt_version": PROMPT_VERSION,
        "status": "completed",
        "stage": "visual_jobs_prepared",
        "format": format_name,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_jobs": len(jobs),
        "total_main_scenes": expected_main_scenes,
        "total_extra_scenes": len(jobs) - expected_main_scenes,
        "scene_role_counts": role_counts,
        "image_size": image_size,
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "character_bible_status": bible.get("status", "unknown"),
        "character_count": len(bible.get("characters", [])),
        "scene_order": "part_then_hook_then_main_then_suspense_or_resolution",
        "image_api_calls": 0,
        "jobs_file": "output/visuals/visual_jobs.json",
    }

    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Write the validated canonical jobs list only after every job passes.
    atomic_json(JOBS_FILE, jobs)
    atomic_json(JOBS_MANIFEST_FILE, manifest)
    atomic_json(CHECKPOINT_FILE, progress)

    # Read-after-write validation prevents partial or malformed artifacts
    # from being treated as successful job preparation.
    saved_jobs = read_json(JOBS_FILE, required=True)
    saved_manifest = read_json(JOBS_MANIFEST_FILE, required=True)
    saved_progress = read_json(CHECKPOINT_FILE, required=True)

    if not isinstance(saved_jobs, list) or len(saved_jobs) != len(jobs):
        raise RuntimeError("Saved visual_jobs.json failed validation.")

    if saved_manifest.get("source_fingerprint") != source_fingerprint:
        raise RuntimeError("Saved visual jobs manifest fingerprint mismatch.")

    if saved_progress.get("total_jobs") != len(jobs):
        raise RuntimeError("Saved visual progress checkpoint is incomplete.")

    log("-" * 64)
    log("VISUAL JOBS READY")
    log("Format:", format_name)
    log("Image dimensions:", image_size)
    log("Parts:", parts_count)
    log("Main scenes per part:", scenes_per_part)
    log("Main jobs:", role_counts["main"])
    log("Hook jobs:", role_counts["hook"])
    log("Suspense jobs:", role_counts["suspense"])
    log("Resolution jobs:", role_counts["resolution"])
    log("Total image jobs:", len(jobs))
    log("Matching previous progress:", progress["completed_jobs"])
    log("Remaining jobs:", progress["remaining_jobs"])
    log("Jobs file:", JOBS_FILE.relative_to(ROOT))
    log("Manifest:", JOBS_MANIFEST_FILE.relative_to(ROOT))
    log("Checkpoint:", CHECKPOINT_FILE.relative_to(ROOT))
    log("Image API calls: 0")
    log("-" * 64)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("VISUAL JOB GENERATION CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("VISUAL JOB GENERATION FAILED:", exc)
        raise