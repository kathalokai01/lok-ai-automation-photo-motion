#!/usr/bin/env python3
"""Create validated, resumable image-generation jobs for every story scene."""

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
SCENES_FILE = OUT / "scenes" / "scenes.json"
BIBLE_FILE = OUT / "story" / "character_bible.json"

VISUALS_DIR = OUT / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
CHECKPOINT_FILE = OUT / "checkpoints" / "visual_jobs_progress.json"

VERSION = "photo-motion-visual-jobs-v6-all-scene-roles"
PROMPT_VERSION = 6
MIN_PROMPT_LENGTH = 12

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

ALLOWED_ROLES = set(ROLE_ORDER)


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
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


def read_json(path: Path, required: bool = False) -> dict | None:
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        data = json.loads(
            path.read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read JSON file {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            f"JSON root must be an object: {path}"
        )

    return data


def atomic_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            json.dump(
                data,
                stream,
                ensure_ascii=False,
                indent=2,
            )
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
        "", "auto", "dynamic", "variable"
    }:
        return "auto"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default

    if not 0 < number <= 3600:
        return default

    return round(number, 3)


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


def normalize_scene_id(scene: dict, part: int, role: str, local: int) -> str:
    scene_id = clean(
        scene.get("scene_id")
        or scene.get("id")
        or scene.get("source_scene_id")
    )

    if not scene_id:
        if role == "main":
            scene_id = f"part_{part:02d}_scene_{local:02d}"
        else:
            scene_id = f"part_{part:02d}_{role}"

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
        raise RuntimeError(
            f"Unsafe or invalid scene_id: {scene_id!r}"
        )

    return scene_id


def load_character_bible() -> dict:
    data = read_json(BIBLE_FILE)

    if data is None:
        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "world": {},
        }

    status = clean(data.get("status")).lower()

    if status not in {"completed", "disabled"}:
        raise RuntimeError(
            "Character Bible is not ready: "
            f"status={status or 'missing'}."
        )

    characters = data.get("characters", [])
    scene_map = data.get("scene_character_map", [])
    world = data.get("world", {})

    if not isinstance(characters, list):
        raise RuntimeError(
            "Character Bible characters must be a list."
        )

    if not isinstance(scene_map, list):
        raise RuntimeError(
            "Character Bible scene_character_map must be a list."
        )

    if not isinstance(world, dict):
        raise RuntimeError(
            "Character Bible world must be an object."
        )

    valid_ids = set()

    for character in characters:
        if not isinstance(character, dict):
            raise RuntimeError(
                "Character Bible contains an invalid character record."
            )

        character_id = clean(
            character.get("id") or character.get("character_id")
        )
        name = clean(character.get("name"))

        if not character_id or not name:
            raise RuntimeError(
                "Every Character Bible record requires an ID and name."
            )

        if character_id in valid_ids:
            raise RuntimeError(
                f"Duplicate Character Bible ID: {character_id}"
            )

        valid_ids.add(character_id)

    for row in scene_map:
        if not isinstance(row, dict):
            raise RuntimeError(
                "Character Bible contains an invalid scene mapping."
            )

        for character_id in row.get("character_ids", []):
            if character_id not in valid_ids:
                raise RuntimeError(
                    "Character Bible scene map contains an unknown ID: "
                    f"{character_id}"
                )

    return data


def character_references(scene: dict, bible: dict) -> list[str]:
    """Resolve names and stable IDs for main and extra scenes."""

    values = (
        scene.get("character_ids")
        or scene.get("characters")
        or scene.get("character_names")
        or []
    )

    if not isinstance(values, list):
        values = [values]

    wanted = {
        clean(value).casefold()
        for value in values
        if clean(value)
    }

    scene_id = clean(scene.get("scene_id"))
    part = scene.get("part")
    scene_number = scene.get("scene")

    # Match extra scenes by scene_id, because their main scene number is None.
    for row in bible.get("scene_character_map", []):
        if not isinstance(row, dict):
            continue

        same_scene = (
            clean(row.get("scene_id")) == scene_id
            if scene_id else False
        )

        same_main_position = (
            row.get("part") == part
            and row.get("scene") == scene_number
            and scene_number is not None
        )

        if same_scene or same_main_position:
            for value in (
                row.get("character_ids", [])
                + row.get("character_names", [])
                + row.get("characters", [])
            ):
                if clean(value):
                    wanted.add(clean(value).casefold())

    selected = []

    for character in bible.get("characters", []):
        candidates = {
            clean(character.get("id")).casefold(),
            clean(character.get("character_id")).casefold(),
            clean(character.get("name")).casefold(),
        }

        candidates.update(
            clean(alias).casefold()
            for alias in character.get("aliases", [])
            if clean(alias)
        )

        if wanted.intersection(candidates):
            selected.append(character)

    # A scene may have no explicit character reference. Do not inject
    # every character in the story into that scene.
    return [
        {
            key: character[key]
            for key in (
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
            if key in character and character[key] not in (None, "", [])
        }
        for character in selected
    ]


def character_context(scene: dict, bible: dict) -> str:
    selected = character_references(scene, bible)

    if not selected:
        return ""

    return json.dumps(
        selected,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_negative_prompt(config: dict, source: dict) -> str:
    values = [
        "unrealistic anatomy",
        "distorted face",
        "extra fingers",
        "missing fingers",
        "duplicated people",
        "fused limbs",
        "plastic skin",
        "waxy face",
        "floating objects",
        "unwanted text",
        "captions",
        "subtitles",
        "watermark",
        "logo",
        "low resolution",
        "blurred main subject",
        "unrelated characters",
        "unrelated objects",
        "wrong story event",
    ]

    if as_bool(config.get("AVOID_CARTOON_LOOK"), True):
        values.extend([
            "cartoon",
            "anime",
            "comic-book illustration",
            "2D drawing",
            "3D animated character",
            "painting",
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
) -> str:
    visual = clean(
        source.get("visual_prompt")
        or source.get("visual")
        or source.get("description")
    )

    if len(visual) < MIN_PROMPT_LENGTH:
        raise RuntimeError(
            f"Scene {source.get('scene_id')!r} "
            "has no usable story-matched visual prompt."
        )

    role = normalize_role(source)

    sections = [
        visual,
        (
            "Photorealistic live-action cinematic film still. "
            "Real human actors, believable physical environment, "
            "natural skin texture, realistic eyes and anatomy."
        ),
        (
            "Depict the exact event described by this scene's narration. "
            "Do not replace the event with a generic illustration."
        ),
        (
            "Show the story-relevant people together with the objects, "
            "creatures, vehicles, tools, buildings, clothing and environment "
            "that the scene actually requires."
        ),
        (
            "Preserve the source story's location, time period, actions, "
            "relationships and emotional context. Do not add unrelated "
            "people, props, events or settings."
        ),
        (
            "Use physically plausible lighting, shadows, reflections, "
            "materials, perspective and natural human proportions."
        ),
        (
            "Do not include captions, subtitles, logos, labels, "
            "watermarks or written text."
        ),
    ]

    style = clean(config.get("VISUAL_STYLE")) or "cinematic_realistic"
    realism = clean(config.get("REALISM")) or "high"
    quality = clean(config.get("QUALITY")) or "high"
    camera_style = clean(config.get("CAMERA_STYLE")) or "cinematic"
    lighting = clean(config.get("LIGHTING")) or "cinematic"
    mood = clean(source.get("mood") or config.get("MOOD")) or "dramatic"

    sections.append(
        f"Visual style: {style}. Realism: {realism}. Quality: {quality}."
    )
    sections.append(f"Camera language: {camera_style}.")
    sections.append(
        "Lighting treatment: "
        + clean(
            source.get("lighting_prompt")
            or source.get("lighting")
            or lighting
        )
        + "."
    )
    sections.append(f"Emotional mood: {mood}.")

    if role != "main":
        sections.append(
            f"Scene role: {role.upper()}. "
            "Keep this as a separate visual record; do not merge it "
            "into or renumber the main numbered scenes."
        )

    for key, label in (
        ("action", "Visible physical action"),
        ("world_context", "Story-world location and continuity"),
        ("location", "Location"),
        ("time_period", "Time period"),
        ("camera_prompt", "Camera direction"),
        ("dialogue", "Story dialogue context"),
    ):
        value = clean(source.get(key))
        if value:
            sections.append(f"{label}: {value}")

    if as_bool(config.get("CHARACTER_CONSISTENCY"), True):
        context = character_context(source, bible)

        if context:
            sections.append(
                "APPROVED CHARACTER IDENTITY REFERENCES. "
                "Preserve each listed character's face, age range, "
                "gender identity, body proportions, hair, defining features "
                "and identity. Clothing changes only when the scene "
                "explicitly requires them. Do not mix identities: "
                + context
            )

    if as_bool(config.get("WORLD_CONSISTENCY"), True):
        world = bible.get("world", {})

        if world:
            sections.append(
                "APPROVED STORY-WORLD CONTINUITY: "
                + json.dumps(world, ensure_ascii=False)
            )

    return "\n\n".join(
        section for section in sections if section
    ).strip()


def image_dimensions(format_name: str) -> tuple[int, int]:
    if format_name == "short":
        return 768, 1024

    return 1024, 576


def scene_order_key(scene: dict) -> tuple:
    role = normalize_role(scene)

    try:
        local = int(scene.get("scene") or 0)
    except (TypeError, ValueError):
        local = 0

    return (
        int(scene.get("part", 0)),
        ROLE_ORDER[role],
        local,
        clean(scene.get("scene_id")),
    )


def flatten_scenes(
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
        "hook": {},
        "main": {},
        "suspense": {},
        "resolution": {},
    }

    for index, original in enumerate(rows, 1):
        if not isinstance(original, dict):
            raise RuntimeError(
                f"Scene row {index} is not an object."
            )

        try:
            part = int(original.get("part"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part number in scene row {index}."
            ) from exc

        if not 1 <= part <= expected_parts:
            raise RuntimeError(
                f"Invalid part number {part} in scene row {index}."
            )

        role = normalize_role(original)

        scene_value = original.get(
            "scene",
            original.get("local_scene"),
        )

        if role == "main":
            try:
                local = int(scene_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Main scene in part {part} has no valid scene number."
                ) from exc

            if not 1 <= local <= expected_scenes:
                raise RuntimeError(
                    f"Main scene number outside configured range: "
                    f"{part}/{local}."
                )

            pair = (part, local)
            if pair in seen_main:
                raise RuntimeError(
                    f"Duplicate main scene: {part}/{local}."
                )

            seen_main.add(pair)
            scene_number = local

        else:
            # Extra scene records stay separate and do not alter main
            # scene numbering. Their image-job slot is assigned later.
            scene_number = None

            if scene_value not in (None, ""):
                try:
                    int(scene_value)
                except (TypeError, ValueError):
                    raise RuntimeError(
                        f"Invalid scene field on extra scene "
                        f"{original.get('scene_id')}."
                    )

        scene_id = normalize_scene_id(
            original,
            part,
            role,
            scene_number or 0,
        )

        if scene_id in seen_ids:
            raise RuntimeError(
                f"Duplicate visual scene ID: {scene_id}."
            )

        seen_ids.add(scene_id)

        if role == "hook":
            role_counts["hook"][part] = (
                role_counts["hook"].get(part, 0) + 1
            )

        elif role == "suspense":
            role_counts["suspense"][part] = (
                role_counts["suspense"].get(part, 0) + 1
            )

        elif role == "resolution":
            role_counts["resolution"][part] = (
                role_counts["resolution"].get(part, 0) + 1
            )

        prepared.append({
            **dict(original),
            "part": part,
            "scene": scene_number,
            "local_scene": scene_number,
            "scene_id": scene_id,
            "id": scene_id,
            "role": role,
            "scene_type": role,
            "is_extra_scene": role != "main",
        })

    expected_main = {
        (part, scene)
        for part in range(1, expected_parts + 1)
        for scene in range(1, expected_scenes + 1)
    }

    missing_main = sorted(expected_main - seen_main)
    extra_main = sorted(seen_main - expected_main)

    if missing_main or extra_main:
        raise RuntimeError(
            "Main scene mapping mismatch. "
            f"Missing={missing_main[:20]}, extra={extra_main[:20]}."
        )

    # Each part must have one Hook. Non-final parts need one Suspense;
    # final part needs one Resolution. Extras are separate records.
    for part in range(1, expected_parts + 1):
        if role_counts["hook"].get(part, 0) != 1:
            raise RuntimeError(
                f"Part {part} must have exactly one separate Hook scene."
            )

        if part < expected_parts:
            if role_counts["suspense"].get(part, 0) != 1:
                raise RuntimeError(
                    f"Non-final part {part} must have exactly one "
                    "separate Suspense scene."
                )

            if role_counts["resolution"].get(part, 0):
                raise RuntimeError(
                    f"Non-final part {part} must not contain Resolution."
                )

        else:
            if role_counts["resolution"].get(part, 0) != 1:
                raise RuntimeError(
                    "Final part must have exactly one separate "
                    "Resolution scene."
                )

            if role_counts["suspense"].get(part, 0):
                raise RuntimeError(
                    "Final part must not contain a Suspense scene."
                )

    return sorted(prepared, key=scene_order_key)

def build_jobs(
    config: dict,
    scenes: list[dict],
    bible: dict,
    format_name: str,
) -> list[dict]:
    width, height = image_dimensions(format_name)
    jobs = []
    global_number = 0
    part_slots = {}

    for source in scenes:
        global_number += 1

        part = int(source["part"])
        role = normalize_role(source)

        part_slots[part] = part_slots.get(part, 0) + 1
        image_slot = part_slots[part]

        prompt = build_prompt(
            config,
            source,
            bible,
        )

        negative = build_negative_prompt(
            config,
            source,
        )

        # Keep actual narration timing metadata. "auto" is retained
        # when no measured duration is available; no arbitrary 5-second
        # duration is assigned here.
        duration = safe_number(
            source.get("duration")
            or source.get("scene_duration")
            or source.get("duration_seconds"),
            default="auto",
        )

        scene_id = clean(source["scene_id"])

        relative_image = (
            f"output/visuals/part_{part:02d}/"
            f"{scene_id}.png"
        )

        if role == "main":
            source_scene_number = int(source["scene"])
        else:
            source_scene_number = None

        job = {
            # Image-worker sequence number. This remains unique even
            # for Hook/Suspense/Resolution records with scene=None.
            "id": scene_id,
            "scene_id": scene_id,
            "part": part,
            "scene": image_slot,
            "local_scene": image_slot,
            "global_scene": global_number,
            "scene_index": global_number,

            # Original story numbering is preserved separately.
            "source_scene_number": source_scene_number,
            "source_local_scene": source_scene_number,
            "source_scene_id": scene_id,

            "role": role,
            "scene_type": role,
            "is_extra_scene": role != "main",

            "title": clean(source.get("title")),
            "narration": clean(source.get("narration")),
            "dialogue": clean(source.get("dialogue")),

            "prompt": prompt,
            "image_prompt": prompt,
            "visual_prompt": prompt,
            "negative_prompt": negative,

            "image_size": f"{width}x{height}",
            "image_width": width,
            "image_height": height,
            "image_path": relative_image,
            "output_path": relative_image,

            "duration": duration,
            "scene_duration": duration,

            "character_ids": (
                source.get("character_ids")
                if isinstance(source.get("character_ids"), list)
                else []
            ),
            "characters": (
                source.get("characters")
                if isinstance(source.get("characters"), list)
                else []
            ),

            "world_context": clean(
                source.get("world_context")
                or source.get("location")
            ),

            "camera_prompt": clean(
                source.get("camera_prompt")
                or source.get("camera")
            ),
            "lighting_prompt": clean(
                source.get("lighting_prompt")
                or source.get("lighting")
            ),
            "mood": clean(source.get("mood")),

            "source_scene_signature": digest(source),
            "prompt_signature": digest({
                "prompt_version": PROMPT_VERSION,
                "prompt": prompt,
                "negative_prompt": negative,
                "image_size": f"{width}x{height}",
                "scene_id": scene_id,
                "role": role,
                "source_scene_signature": digest(source),
            }),
        }

        if len(prompt) < MIN_PROMPT_LENGTH:
            raise RuntimeError(
                f"Generated prompt is too short for {scene_id}."
            )

        jobs.append(job)

    return jobs


def validate_jobs(
    jobs: list[dict],
    expected_scenes: list[dict],
    expected_parts: int,
    expected_main_scenes: int,
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

    if set(actual_ids) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(actual_ids))
        extra = sorted(set(actual_ids) - set(expected_ids))

        raise RuntimeError(
            "Visual job scene mapping mismatch. "
            f"Missing={missing[:20]}, extra={extra[:20]}."
        )

    if len(jobs) != len(expected_scenes):
        raise RuntimeError(
            f"Visual job count mismatch: "
            f"{len(jobs)}/{len(expected_scenes)}."
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

    for index, job in enumerate(jobs, 1):
        if job.get("global_scene") != index:
            raise RuntimeError(
                f"Global job numbering error at {job.get('scene_id')}."
            )

        part = job.get("part")
        local = job.get("local_scene")

        if not isinstance(part, int) or not 1 <= part <= expected_parts:
            raise RuntimeError(
                f"Invalid part mapping for {job.get('scene_id')}."
            )

        if not isinstance(local, int) or local < 1:
            raise RuntimeError(
                f"Invalid image-worker scene slot for {job.get('scene_id')}."
            )

        if not clean(job.get("prompt")):
            raise RuntimeError(
                f"Empty visual prompt for {job.get('scene_id')}."
            )

        if not clean(job.get("image_path")):
            raise RuntimeError(
                f"Missing image output path for {job.get('scene_id')}."
            )

        if not clean(job.get("prompt_signature")):
            raise RuntimeError(
                f"Missing prompt signature for {job.get('scene_id')}."
            )

    expected_order = sorted(
        expected_scenes,
        key=scene_order_key,
    )

    if [
        clean(row.get("scene_id"))
        for row in expected_order
    ] != actual_ids:
        raise RuntimeError(
            "Visual jobs are not in required part/role order."
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

    selected = {
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
        "visual_settings": selected,
    })


def build_progress(
    jobs: list[dict],
    source_fingerprint: str,
    config_signature: str,
) -> dict:
    return {
        "version": VERSION,
        "status": "ready",
        "stage": "visual_jobs_prepared",
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "total_jobs": len(jobs),

        # Preparing a job is not the same as successfully generating
        # its image. The image worker owns image-completion checkpoints.
        "prepared_jobs": len(jobs),
        "completed_jobs": 0,
        "remaining_jobs": len(jobs),

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
    log("=" * 60)
    log("KATHA LOK AI — STORY-MATCHED VISUAL JOB BUILDER")
    log("=" * 60)

    config = load_and_validate(
        ROOT / "Input" / "topic.txt"
    )

    format_name = get_format(config)
    parts_count = get_parts(config)
    scenes_per_part = get_scenes(config)

    if format_name not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported output format: {format_name!r}."
        )

    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "PARTS and SCENES must both be positive integers."
        )

    source_document = read_json(
        SCENES_FILE,
        required=True,
    )

    bible = load_character_bible()

    scenes = flatten_scenes(
        source_document,
        parts_count,
        scenes_per_part,
    )

    jobs = build_jobs(
        config=config,
        scenes=scenes,
        bible=bible,
        format_name=format_name,
    )

    expected_main_scenes = parts_count * scenes_per_part

    validate_jobs(
        jobs=jobs,
        expected_scenes=scenes,
        expected_parts=parts_count,
        expected_main_scenes=expected_main_scenes,
    )

    source_fingerprint = digest({
        "scenes": source_document,
        "character_bible": bible,
    })

    image_size = jobs[0]["image_size"]

    config_signature = build_config_signature(
        config=config,
        format_name=format_name,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        image_size=image_size,
    )

    role_counts = {
        role: sum(
            1 for job in jobs
            if job.get("role") == role
        )
        for role in ("hook", "main", "suspense", "resolution")
    }

    document = {
        "version": VERSION,
        "prompt_version": PROMPT_VERSION,
        "status": "completed",
        "stage": "visual_jobs_prepared",

        "format": format_name,
        "title": clean(source_document.get("title")),
        "topic": clean(source_document.get("topic")),

        "parts": parts_count,
        "total_parts": parts_count,
        "scenes_per_part": scenes_per_part,

        "total_scenes": len(jobs),
        "total_jobs": len(jobs),
        "total_main_scenes": expected_main_scenes,
        "total_extra_scenes": len(jobs) - expected_main_scenes,

        "scene_role_counts": role_counts,
        "image_size": image_size,

        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,

        "character_bible_status": bible.get(
            "status", "unknown"
        ),
        "character_count": len(
            bible.get("characters", [])
        ),

        "scene_order": (
            "part_then_hook_then_main_then_suspense_or_resolution"
        ),

        "jobs": jobs,
    }

    progress = build_progress(
        jobs=jobs,
        source_fingerprint=source_fingerprint,
        config_signature=config_signature,
    )

    # Do not overwrite files until all records and mappings validate.
    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    atomic_json(JOBS_FILE, document)
    atomic_json(CHECKPOINT_FILE, progress)

    log("-" * 60)
    log("VISUAL JOBS READY")
    log("Format:", format_name)
    log("Parts:", parts_count)
    log("Main scenes per part:", scenes_per_part)
    log("Total main scene jobs:", role_counts["main"])
    log("Hook jobs:", role_counts["hook"])
    log("Suspense jobs:", role_counts["suspense"])
    log("Resolution jobs:", role_counts["resolution"])
    log("Total image jobs:", len(jobs))
    log("Image size:", image_size)
    log("Character Bible:", bible.get("status", "unknown"))
    log("Jobs file:", JOBS_FILE.relative_to(ROOT))
    log("Checkpoint:", CHECKPOINT_FILE.relative_to(ROOT))
    log("Image API calls: 0")
    log("-" * 60)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        print(
            "VISUAL JOB GENERATION CANCELLED",
            file=sys.stderr,
        )
        sys.exit(130)

    except Exception as exc:
        print(
            f"VISUAL JOB GENERATION FAILED: {exc}",
            file=sys.stderr,
        )
        raise