#!/usr/bin/env python3
"""Validate generated story images and prepare all scene roles for Photo Motion."""

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
)

OUTPUT = ROOT / "output"
VISUALS_DIR = OUTPUT / "visuals"

SCENES_FILE = OUTPUT / "scenes" / "scenes.json"
CHARACTER_BIBLE_FILE = OUTPUT / "story" / "character_bible.json"

VISUAL_JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
IMAGE_MANIFEST_FILE = VISUALS_DIR / "image_manifest.json"
GENERATION_MANIFEST_FILE = VISUALS_DIR / "image_generation_manifest.json"

PHOTO_MOTION_JOBS_FILE = VISUALS_DIR / "photo_motion_jobs.json"
CHECKPOINT_FILE = OUTPUT / "checkpoints" / "prepare_visuals_progress.json"

VERSION = "prepare-visuals-v5-all-scene-roles"
MIN_IMAGE_BYTES = 1000

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

    text = str(value).strip().lower()

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    return default


def positive_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{label} की संख्या गलत है: {value!r}"
        ) from exc

    if result < 1:
        raise RuntimeError(
            f"{label} कम से कम 1 होना चाहिए."
        )

    return result


def stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with Path(path).open("rb") as stream:
        for chunk in iter(
            lambda: stream.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def read_json(path: Path, required: bool = False) -> Any:
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(
                f"आवश्यक फाइल नहीं मिली: {path.relative_to(ROOT)}"
            )

        return None

    try:
        return json.loads(
            path.read_text(encoding="utf-8-sig")
        )

    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"JSON पढ़ने में समस्या: {path}: {exc}"
        ) from exc


def atomic_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_name(
        path.name + ".tmp"
    )

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

        os.replace(
            temporary,
            path,
        )

    finally:
        temporary.unlink(missing_ok=True)


def safe_project_path(value: Any, label: str) -> Path:
    text = clean(value)

    if not text:
        raise RuntimeError(
            f"{label}: image path खाली है."
        )

    path = Path(text)

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(
            f"{label}: project directory के बाहर का path स्वीकार नहीं है."
        )

    return path


def validate_image(path: Path, label: str) -> dict:
    if not path.is_file():
        raise RuntimeError(
            f"{label}: image मौजूद नहीं है: {path}"
        )

    if path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(
            f"{label}: image file बहुत छोटी या अधूरी है."
        )

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format

        if width < 64 or height < 64:
            raise RuntimeError(
                f"{label}: image dimensions बहुत छोटी हैं."
            )

        if image_format not in {
            "PNG",
            "JPEG",
            "WEBP",
        }:
            raise RuntimeError(
                f"{label}: unsupported image format {image_format!r}."
            )

        return {
            "width": width,
            "height": height,
            "format": image_format,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    except Exception as exc:
        raise RuntimeError(
            f"{label}: image validation failed: {exc}"
        ) from exc


def normalize_role(row: dict) -> str:
    role = clean(
        row.get("role")
        or row.get("scene_type")
        or "main"
    ).lower()

    if role not in ALLOWED_ROLES:
        raise RuntimeError(
            f"Unsupported scene role: {role!r}"
        )

    return role


def normalize_scene_id(
    row: dict,
    part: int,
    role: str,
    source_scene_number: int | None,
) -> str:
    scene_id = clean(
        row.get("scene_id")
        or row.get("id")
        or row.get("source_scene_id")
    )

    if not scene_id:
        if role == "main":
            scene_id = (
                f"part_{part:02d}_scene_"
                f"{source_scene_number:02d}"
            )
        else:
            scene_id = f"part_{part:02d}_{role}"

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,120}",
        scene_id,
    ):
        raise RuntimeError(
            f"Invalid scene ID: {scene_id!r}"
        )

    return scene_id


def normalize_source_scenes(
    document: dict,
    expected_parts: int,
    scenes_per_part: int,
) -> list[dict]:
    if not isinstance(document, dict):
        raise RuntimeError(
            "scenes.json का root JSON object होना चाहिए."
        )

    rows = document.get("scenes")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            "scenes.json में scenes list नहीं मिली."
        )

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
                f"Scene record #{index} JSON object नहीं है."
            )

        part = positive_int(
            original.get("part"),
            f"Scene #{index} part",
        )

        if part > expected_parts:
            raise RuntimeError(
                f"Scene #{index}: part {part} configured सीमा "
                f"1-{expected_parts} से बाहर है."
            )

        role = normalize_role(original)

        raw_scene = original.get(
            "scene",
            original.get("local_scene"),
        )

        if role == "main":
            source_scene_number = positive_int(
                raw_scene,
                f"Part {part} main scene",
            )

            if source_scene_number > scenes_per_part:
                raise RuntimeError(
                    f"Part {part}: main scene {source_scene_number} "
                    f"configured सीमा 1-{scenes_per_part} से बाहर है."
                )

            pair = (part, source_scene_number)

            if pair in seen_main:
                raise RuntimeError(
                    f"Duplicate main scene: part={part}, "
                    f"scene={source_scene_number}."
                )

            seen_main.add(pair)

        else:
            source_scene_number = None

            if raw_scene not in (None, ""):
                try:
                    int(raw_scene)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Extra scene {original.get('scene_id')} "
                        "का source scene number invalid है."
                    ) from exc

        scene_id = normalize_scene_id(
            original,
            part,
            role,
            source_scene_number,
        )

        if scene_id in seen_ids:
            raise RuntimeError(
                f"Duplicate scene_id: {scene_id}"
            )

        seen_ids.add(scene_id)

        counts[role][part] = (
            counts[role].get(part, 0) + 1
        )

        prepared.append({
            **dict(original),
            "part": part,
            "role": role,
            "scene_id": scene_id,
            "source_scene_number": source_scene_number,
            "is_extra_scene": role != "main",
        })

    expected_main = {
        (part, scene)
        for part in range(1, expected_parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    missing_main = sorted(
        expected_main - seen_main
    )

    extra_main = sorted(
        seen_main - expected_main
    )

    if missing_main or extra_main:
        raise RuntimeError(
            "Main scene mapping गलत है. "
            f"Missing={missing_main[:20]}, "
            f"extra={extra_main[:20]}."
        )

    for part in range(1, expected_parts + 1):
        if counts["hook"].get(part, 0) != 1:
            raise RuntimeError(
                f"Part {part} में exactly one separate Hook आवश्यक है."
            )

        if part < expected_parts:
            if counts["suspense"].get(part, 0) != 1:
                raise RuntimeError(
                    f"Non-final part {part} में exactly one "
                    "separate Suspense आवश्यक है."
                )

            if counts["resolution"].get(part, 0):
                raise RuntimeError(
                    f"Non-final part {part} में Resolution नहीं होना चाहिए."
                )

        else:
            if counts["resolution"].get(part, 0) != 1:
                raise RuntimeError(
                    "Final part में exactly one separate Resolution आवश्यक है."
                )

            if counts["suspense"].get(part, 0):
                raise RuntimeError(
                    "Final part में Suspense के बजाय Resolution होना चाहिए."
                )

    prepared.sort(
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
            row["source_scene_number"] or 0,
            row["scene_id"],
        )
    )

    return prepared


def extract_job_rows(document: Any) -> tuple[list[dict], dict]:
    if isinstance(document, list):
        return document, {}

    if not isinstance(document, dict):
        raise RuntimeError(
            "visual_jobs.json का root object या list होना चाहिए."
        )

    rows = document.get(
        "jobs",
        document.get("visual_jobs"),
    )

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            "visual_jobs.json में jobs की valid list नहीं मिली."
        )

    return rows, document


def extract_manifest_rows(document: Any) -> list[dict]:
    if isinstance(document, list):
        rows = document

    elif isinstance(document, dict):
        rows = document.get("scenes", [])

    else:
        rows = []

    if not isinstance(rows, list):
        raise RuntimeError(
            "Image manifest scenes list invalid है."
        )

    return [
        row for row in rows
        if isinstance(row, dict)
    ]


def index_by_scene_id(rows: list[dict], label: str) -> dict:
    result = {}

    for index, row in enumerate(rows, 1):
        scene_id = clean(row.get("scene_id"))

        if not scene_id:
            raise RuntimeError(
                f"{label} record #{index} में scene_id missing है."
            )

        if scene_id in result:
            raise RuntimeError(
                f"{label} में duplicate scene_id: {scene_id}"
            )

        result[scene_id] = row

    return result


def normalize_duration(row: dict) -> float | str:
    for key in (
        "duration",
        "scene_duration",
        "duration_seconds",
        "narration_duration",
        "audio_duration",
    ):
        value = row.get(key)

        if value is None:
            continue

        if isinstance(value, str) and value.strip().lower() in {
            "",
            "auto",
            "dynamic",
            "variable",
        }:
            continue

        try:
            number = float(value)
        except (TypeError, ValueError):
            continue

        if 0.5 <= number <= 3600:
            return round(number, 4)

    # Keep duration unresolved until measured narration/audio timing
    # is available to the Photo Motion stage.
    return "auto"


def validate_character_bible(config: dict) -> dict:
    enabled = as_bool(
        config.get("CHARACTER_BIBLE", True),
        True,
    )

    if not enabled:
        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "world": {},
        }

    document = read_json(
        CHARACTER_BIBLE_FILE,
        required=True,
    )

    if not isinstance(document, dict):
        raise RuntimeError(
            "character_bible.json का root object होना चाहिए."
        )

    status = clean(
        document.get("status")
    ).lower()

    if status not in {"completed", "disabled"}:
        raise RuntimeError(
            "Character Bible तैयार नहीं है: "
            f"status={status or 'missing'}."
        )

    if not isinstance(
        document.get("characters", []),
        list,
    ):
        raise RuntimeError(
            "Character Bible characters list invalid है."
        )

    if not isinstance(
        document.get("scene_character_map", []),
        list,
    ):
        raise RuntimeError(
            "Character Bible scene_character_map invalid है."
        )

    if not isinstance(
        document.get("world", {}),
        dict,
    ):
        raise RuntimeError(
            "Character Bible world object invalid है."
        )

    return document


def validate_scene_character_links(
    source_scenes: list[dict],
    bible: dict,
) -> None:
    if bible.get("status") == "disabled":
        return

    valid_ids = {
        clean(
            row.get("id")
            or row.get("character_id")
        )
        for row in bible.get("characters", [])
        if isinstance(row, dict)
    }

    valid_ids.discard("")

    scene_map = bible.get(
        "scene_character_map",
        [],
    )

    mapped = {
        clean(row.get("scene_id")): row
        for row in scene_map
        if isinstance(row, dict)
        and clean(row.get("scene_id"))
    }

    for scene in source_scenes:
        row = mapped.get(scene["scene_id"])

        if row is None:
            continue

        ids = row.get("character_ids", [])

        if not isinstance(ids, list):
            raise RuntimeError(
                f"Character mapping invalid: {scene['scene_id']}"
            )

        unknown = [
            value for value in ids
            if clean(value) not in valid_ids
        ]

        if unknown:
            raise RuntimeError(
                f"{scene['scene_id']} में unknown Character IDs: "
                f"{unknown[:10]}"
            )


def verify_job_source_mapping(
    source_scenes: list[dict],
    job_rows: list[dict],
) -> dict[str, dict]:
    jobs_by_id = index_by_scene_id(
        job_rows,
        "visual_jobs",
    )

    expected_ids = {
        row["scene_id"]
        for row in source_scenes
    }

    actual_ids = set(jobs_by_id)

    if expected_ids != actual_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)

        raise RuntimeError(
            "Visual job और story scene mapping mismatch. "
            f"Missing={missing[:20]}, extra={extra[:20]}."
        )

    for source in source_scenes:
        scene_id = source["scene_id"]
        job = jobs_by_id[scene_id]

        try:
            job_part = int(job.get("part"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{scene_id}: visual job part invalid है."
            ) from exc

        job_role = normalize_role(job)

        if job_part != source["part"]:
            raise RuntimeError(
                f"{scene_id}: part mismatch "
                f"{job_part}/{source['part']}."
            )

        if job_role != source["role"]:
            raise RuntimeError(
                f"{scene_id}: role mismatch "
                f"{job_role}/{source['role']}."
            )

        job_source_number = job.get(
            "source_scene_number",
            job.get("source_local_scene"),
        )

        if source["role"] == "main":
            try:
                job_source_number = int(job_source_number)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{scene_id}: original main scene number missing."
                ) from exc

            if job_source_number != source["source_scene_number"]:
                raise RuntimeError(
                    f"{scene_id}: original main scene number changed."
                )

        elif job_source_number not in (None, ""):
            raise RuntimeError(
                f"{scene_id}: extra scene ने main scene number ले लिया है."
            )

    return jobs_by_id

def prepare_photo_motion_jobs(
    source_scenes: list[dict],
    jobs_by_id: dict[str, dict],
    image_manifest: dict,
    config: dict,
    format_name: str,
    parts: int,
    scenes_per_part: int,
    bible: dict,
) -> tuple[list[dict], dict]:
    manifest_rows = extract_manifest_rows(
        image_manifest
    )

    if not manifest_rows:
        raise RuntimeError(
            "Image manifest में कोई generated image record नहीं है."
        )

    manifest_by_id = index_by_scene_id(
        manifest_rows,
        "image_manifest",
    )

    expected_ids = {
        row["scene_id"]
        for row in source_scenes
    }

    actual_ids = set(manifest_by_id)

    if expected_ids != actual_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)

        raise RuntimeError(
            "Generated image manifest mapping mismatch. "
            f"Missing={missing[:20]}, extra={extra[:20]}."
        )

    jobs = []
    role_counts = {
        role: 0
        for role in ROLE_ORDER
    }

    part_role_counts = {
        part: {
            role: 0
            for role in ROLE_ORDER
        }
        for part in range(1, parts + 1)
    }

    part_slots = {
        part: 0
        for part in range(1, parts + 1)
    }

    for global_number, source in enumerate(
        source_scenes,
        1,
    ):
        scene_id = source["scene_id"]
        part = source["part"]
        role = source["role"]

        source_job = jobs_by_id[scene_id]
        image_record = manifest_by_id[scene_id]

        if image_record.get("status") not in {
            "generated",
            "existing",
            "completed",
        }:
            raise RuntimeError(
                f"{scene_id}: image generation completed नहीं है. "
                f"Status={image_record.get('status')!r}."
            )

        image_value = (
            source_job.get("image_path")
            or source_job.get("output_path")
            or source_job.get("image")
            or image_record.get("image_path")
            or image_record.get("image")
            or image_record.get("file")
        )

        image_path = safe_project_path(
            image_value,
            f"{scene_id} image",
        )

        image_info = validate_image(
            image_path,
            scene_id,
        )

        expected_hash = clean(
            image_record.get("image_sha256")
        )

        if expected_hash and (
            image_info["sha256"] != expected_hash
        ):
            raise RuntimeError(
                f"{scene_id}: image checksum manifest से मेल नहीं खाता."
            )

        manifest_prompt_hash = clean(
            image_record.get("prompt_signature")
        )

        source_prompt_hash = clean(
            source_job.get("prompt_signature")
        )

        if (
            manifest_prompt_hash
            and source_prompt_hash
            and manifest_prompt_hash != source_prompt_hash
        ):
            raise RuntimeError(
                f"{scene_id}: image prompt signature mismatch."
            )

        try:
            job_part = int(source_job.get("part"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{scene_id}: visual job part invalid है."
            ) from exc

        if job_part != part:
            raise RuntimeError(
                f"{scene_id}: visual job part बदल गया है."
            )

        part_slots[part] += 1
        local_slot = part_slots[part]

        if role == "main":
            original_scene_number = source[
                "source_scene_number"
            ]
        else:
            original_scene_number = None

        narration = clean(
            source.get("narration")
            or source_job.get("narration")
            or source.get("text")
        )

        if not narration:
            raise RuntimeError(
                f"{scene_id}: scene narration missing है."
            )

        prompt = clean(
            source_job.get("prompt")
            or source_job.get("image_prompt")
            or source_job.get("visual_prompt")
        )

        if not prompt:
            raise RuntimeError(
                f"{scene_id}: original visual prompt missing है."
            )

        duration = normalize_duration({
            **source,
            **source_job,
        })

        relative_image = image_path.relative_to(
            ROOT
        ).as_posix()

        row = {
            # Photo Motion's sequential clip number.
            "global_scene": global_number,
            "global_scene_number": global_number,
            "scene": local_slot,
            "scene_index": global_number,

            # Part identity and worker slot.
            "part": part,
            "local_scene": local_slot,

            # Original story numbering remains unchanged.
            "source_scene_number": original_scene_number,
            "source_local_scene": original_scene_number,
            "source_scene_id": scene_id,

            # Separate scene roles.
            "scene_id": scene_id,
            "id": scene_id,
            "role": role,
            "scene_type": role,
            "is_extra_scene": role != "main",

            # Story and narration.
            "title": clean(
                source.get("title")
                or source_job.get("title")
            ),
            "narration": narration,
            "dialogue": clean(
                source.get("dialogue")
                or source_job.get("dialogue")
            ),

            # Validated generated image.
            "image": relative_image,
            "image_path": relative_image,
            "output_path": relative_image,
            "image_sha256": image_info["sha256"],
            "image_width": image_info["width"],
            "image_height": image_info["height"],
            "image_format": image_info["format"],
            "image_size_bytes": image_info["size_bytes"],

            # Prompt and source integrity.
            "prompt": prompt,
            "image_prompt": prompt,
            "negative_prompt": clean(
                source_job.get("negative_prompt")
            ),
            "prompt_signature": source_job.get(
                "prompt_signature"
            ),
            "source_scene_signature": source_job.get(
                "source_scene_signature"
            ),
            "prompt_sha256": image_record.get(
                "prompt_sha256"
            ),

            # Actual duration is retained when already measured.
            # "auto" is resolved later from narration/audio.
            "duration": duration,
            "scene_duration": duration,
            "duration_source": (
                "measured_source"
                if isinstance(duration, (int, float))
                else "pending_narration_audio_measurement"
            ),

            # Character and world continuity references.
            "character_ids": (
                source.get("character_ids")
                if isinstance(source.get("character_ids"), list)
                else source_job.get("character_ids", [])
            ),
            "characters": (
                source.get("characters")
                if isinstance(source.get("characters"), list)
                else source_job.get("characters", [])
            ),
            "world_context": clean(
                source.get("world_context")
                or source_job.get("world_context")
            ),
            "camera_prompt": clean(
                source.get("camera_prompt")
                or source_job.get("camera_prompt")
            ),
            "lighting_prompt": clean(
                source.get("lighting_prompt")
                or source_job.get("lighting_prompt")
            ),
            "mood": clean(
                source.get("mood")
                or source_job.get("mood")
            ),

            "image_provider": image_record.get(
                "provider"
            ),
            "image_status": image_record.get(
                "status"
            ),
            "photo_motion_status": "pending",
        }

        jobs.append(row)

        role_counts[role] += 1
        part_role_counts[part][role] += 1

    expected_main = parts * scenes_per_part

    if role_counts["main"] != expected_main:
        raise RuntimeError(
            "Photo Motion main scene count mismatch: "
            f"expected {expected_main}, "
            f"found {role_counts['main']}."
        )

    for part in range(1, parts + 1):
        counts = part_role_counts[part]

        if counts["hook"] != 1:
            raise RuntimeError(
                f"Part {part}: Hook mapping validation failed."
            )

        if counts["main"] != scenes_per_part:
            raise RuntimeError(
                f"Part {part}: main scene count validation failed."
            )

        if part < parts:
            if counts["suspense"] != 1:
                raise RuntimeError(
                    f"Part {part}: Suspense mapping validation failed."
                )

            if counts["resolution"] != 0:
                raise RuntimeError(
                    f"Non-final part {part} must not have Resolution."
                )

        else:
            if counts["resolution"] != 1:
                raise RuntimeError(
                    "Final part: Resolution mapping validation failed."
                )

            if counts["suspense"] != 0:
                raise RuntimeError(
                    "Final part must not have Suspense."
                )

    if len(jobs) != len(source_scenes):
        raise RuntimeError(
            "Photo Motion jobs count differs from source scene count."
        )

    if len({
        row["scene_id"]
        for row in jobs
    }) != len(jobs):
        raise RuntimeError(
            "Photo Motion jobs contain duplicate scene IDs."
        )

    if [
        row["global_scene"]
        for row in jobs
    ] != list(range(1, len(jobs) + 1)):
        raise RuntimeError(
            "Photo Motion global scene numbering is not continuous."
        )

    settings = {
        "format": format_name,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "character_bible_enabled": (
            bible.get("status") != "disabled"
        ),
        "character_bible_signature": stable_hash(bible),
        "source_scene_ids": [
            row["scene_id"]
            for row in source_scenes
        ],
        "image_hashes": {
            row["scene_id"]: row["image_sha256"]
            for row in jobs
        },
    }

    config_signature = stable_hash(settings)

    document = {
        "version": VERSION,
        "status": "completed",
        "stage": "photo_motion_jobs_prepared",

        "format": format_name,
        "parts": parts,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,

        "total_jobs": len(jobs),
        "total_scenes": len(jobs),
        "total_main_scenes": role_counts["main"],
        "total_extra_scenes": (
            len(jobs) - role_counts["main"]
        ),

        "scene_role_counts": role_counts,
        "part_scene_role_counts": {
            str(part): part_role_counts[part]
            for part in range(1, parts + 1)
        },

        "scene_order": (
            "part_then_hook_then_main_then_"
            "suspense_or_final_resolution"
        ),

        "main_scene_numbering": (
            "source_scene_number_preserved_separately"
        ),

        "extra_scene_numbering": (
            "unique_local_scene_slot_without_changing_main_numbering"
        ),

        "duration_policy": (
            "use_measured_duration_or_resolve_auto_from_narration_audio"
        ),

        "config_signature": config_signature,
        "character_bible_status": bible.get(
            "status",
            "unknown",
        ),

        "jobs": jobs,
    }

    return jobs, document


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — PREPARING PHOTO MOTION VISUALS")
    log("Version:", VERSION)
    log("=" * 64)

    config = load_and_validate()

    if not isinstance(config, dict):
        raise RuntimeError(
            "Input configuration valid dictionary नहीं है."
        )

    format_name = get_format(config)

    if format_name not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported output format: {format_name!r}"
        )

    parts = positive_int(
        get_parts(config),
        "PARTS",
    )

    scenes_per_part = positive_int(
        get_scenes(config),
        "SCENES",
    )

    resume_enabled = as_bool(
        config.get("RESUME_ENABLED", True),
        True,
    )

    skip_completed = as_bool(
        config.get("SKIP_COMPLETED_SCENES", True),
        True,
    )

    log("FORMAT:", format_name)
    log("PARTS:", parts)
    log("MAIN SCENES PER PART:", scenes_per_part)
    log("RESUME ENABLED:", resume_enabled)
    log("SKIP COMPLETED SCENES:", skip_completed)

    scenes_document = read_json(
        SCENES_FILE,
        required=True,
    )

    source_scenes = normalize_source_scenes(
        scenes_document,
        parts,
        scenes_per_part,
    )

    bible = validate_character_bible(
        config
    )

    validate_scene_character_links(
        source_scenes,
        bible,
    )

    visual_document = read_json(
        VISUAL_JOBS_FILE,
        required=True,
    )

    visual_rows, visual_metadata = extract_job_rows(
        visual_document
    )

    jobs_by_id = verify_job_source_mapping(
        source_scenes,
        visual_rows,
    )

    image_manifest = read_json(
        IMAGE_MANIFEST_FILE,
        required=True,
    )

    if not isinstance(image_manifest, dict):
        raise RuntimeError(
            "image_manifest.json का root object होना चाहिए."
        )

    image_status = clean(
        image_manifest.get("status")
    ).lower()

    if image_status not in {
        "completed",
        "complete",
        "success",
    }:
        raise RuntimeError(
            "Image manifest completed नहीं है: "
            f"status={image_status or 'missing'}."
        )

    expected_image_total = len(source_scenes)

    manifest_total = image_manifest.get(
        "images_required",
        image_manifest.get(
            "total_scenes",
            expected_image_total,
        ),
    )

    try:
        manifest_total = int(manifest_total)
    except (TypeError, ValueError):
        raise RuntimeError(
            "Image manifest का total scene count invalid है."
        )

    if manifest_total != expected_image_total:
        raise RuntimeError(
            "Image manifest total mismatch: "
            f"expected {expected_image_total}, "
            f"found {manifest_total}."
        )

    generation_document = read_json(
        GENERATION_MANIFEST_FILE,
    )

    if generation_document is not None:
        if not isinstance(generation_document, dict):
            raise RuntimeError(
                "image_generation_manifest.json invalid है."
            )

        generation_status = clean(
            generation_document.get("status")
        ).lower()

        if generation_status not in {
            "completed",
            "complete",
            "success",
        }:
            raise RuntimeError(
                "Image generation manifest completed नहीं है: "
                f"{generation_status or 'missing'}."
            )

    jobs, document = prepare_photo_motion_jobs(
        source_scenes=source_scenes,
        jobs_by_id=jobs_by_id,
        image_manifest=image_manifest,
        config=config,
        format_name=format_name,
        parts=parts,
        scenes_per_part=scenes_per_part,
        bible=bible,
    )

    VISUALS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    CHECKPOINT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    old_checkpoint = {}

    if resume_enabled:
        previous = read_json(
            CHECKPOINT_FILE,
        )

        if isinstance(previous, dict):
            old_checkpoint = previous

    previous_signature = clean(
        old_checkpoint.get("config_signature")
    )

    signature_matches = (
        previous_signature
        == document["config_signature"]
    )

    completed_scene_ids = set()

    if (
        resume_enabled
        and skip_completed
        and signature_matches
    ):
        previous_rows = old_checkpoint.get(
            "scenes",
            [],
        )

        if isinstance(previous_rows, list):
            for row in previous_rows:
                if not isinstance(row, dict):
                    continue

                if row.get("status") != "prepared":
                    continue

                scene_id = clean(
                    row.get("scene_id")
                )

                if scene_id:
                    completed_scene_ids.add(scene_id)

    checkpoint_rows = []

    for row in jobs:
        scene_id = row["scene_id"]

        checkpoint_rows.append({
            "scene_id": scene_id,
            "part": row["part"],
            "local_scene": row["local_scene"],
            "source_scene_number": row[
                "source_scene_number"
            ],
            "role": row["role"],
            "image_path": row["image_path"],
            "image_sha256": row["image_sha256"],
            "status": "prepared",
            "reused_checkpoint": (
                scene_id in completed_scene_ids
            ),
        })

    completed = sum(
        row["status"] == "prepared"
        for row in checkpoint_rows
    )

    checkpoint = {
        "version": VERSION,
        "status": "completed",
        "config_signature": document[
            "config_signature"
        ],
        "total_jobs": len(jobs),
        "prepared": completed,
        "pending": 0,
        "scenes": checkpoint_rows,
    }

    # Write only after every source scene, role, image and mapping
    # has passed validation.
    atomic_json(
        PHOTO_MOTION_JOBS_FILE,
        document,
    )

    atomic_json(
        CHECKPOINT_FILE,
        checkpoint,
    )

    log("-" * 64)
    log("PHOTO MOTION JOBS PREPARED")
    log("Format:", format_name)
    log("Parts:", parts)
    log("Main scenes per part:", scenes_per_part)
    log("Total main scenes:", document["total_main_scenes"])
    log("Total extra scenes:", document["total_extra_scenes"])
    log("Total image jobs:", len(jobs))

    for role in (
        "hook",
        "main",
        "suspense",
        "resolution",
    ):
        log(
            role.upper() + ":",
            document["scene_role_counts"][role],
        )

    log("Photo Motion jobs:", PHOTO_MOTION_JOBS_FILE.relative_to(ROOT))
    log("Checkpoint:", CHECKPOINT_FILE.relative_to(ROOT))
    log("Image API calls: 0")
    log("-" * 64)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        log("PHOTO MOTION VISUAL PREPARATION CANCELLED")
        sys.exit(130)

    except Exception as exc:
        log(
            "PHOTO MOTION VISUAL PREPARATION FAILED:",
            str(exc),
        )
        sys.exit(1)