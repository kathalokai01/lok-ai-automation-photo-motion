#!/usr/bin/env python3
"""Validate generated images and prepare measured, role-aware Photo Motion jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUTPUT = ROOT / "output"
VISUALS_DIR = OUTPUT / "visuals"
CHECKPOINT_DIR = OUTPUT / "checkpoints"

SCENES_FILE = OUTPUT / "scenes" / "scenes.json"
CHARACTER_BIBLE_FILE = OUTPUT / "story" / "character_bible.json"
NARRATION_FILE = OUTPUT / "narration" / "narration.json"

VISUAL_JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
IMAGE_MANIFEST_FILE = VISUALS_DIR / "image_manifest.json"
GENERATION_MANIFEST_FILE = VISUALS_DIR / "image_generation_manifest.json"

PHOTO_MOTION_JOBS_FILE = VISUALS_DIR / "photo_motion_jobs.json"
PHOTO_MOTION_MANIFEST_FILE = VISUALS_DIR / "photo_motion_manifest.json"
CHECKPOINT_FILE = CHECKPOINT_DIR / "prepare_visuals_progress.json"

VERSION = "prepare-visuals-v12-measured-role-safe-sha-verified"
MIN_IMAGE_BYTES = 1000
MIN_IMAGE_DIMENSION = 256
MAX_IMAGE_DIMENSION = 4096
MIN_DURATION = 0.5
MAX_DURATION = 3600.0

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


def positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}: invalid number {value!r}") from exc

    if number < 1:
        raise RuntimeError(f"{label}: value must be at least 1.")
    return number


def stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, required: bool = False) -> Any:
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path.relative_to(ROOT)}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


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


def safe_project_path(value: Any, label: str) -> Path:
    text = clean(value)
    if not text:
        raise RuntimeError(f"{label}: image path missing.")

    path = Path(text)
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path outside project directory is forbidden.")

    return path


def validate_image(path: Path, label: str) -> dict:
    if not path.is_file():
        raise RuntimeError(f"{label}: image missing: {path}")

    if path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(f"{label}: image is too small or incomplete.")

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format

        if not (
            MIN_IMAGE_DIMENSION <= width <= MAX_IMAGE_DIMENSION
            and MIN_IMAGE_DIMENSION <= height <= MAX_IMAGE_DIMENSION
        ):
            raise RuntimeError(f"Invalid image dimensions: {width}x{height}")

        if image_format not in {"PNG", "JPEG", "WEBP"}:
            raise RuntimeError(f"Unsupported image format: {image_format}")

        return {
            "width": width,
            "height": height,
            "format": image_format,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    except Exception as exc:
        raise RuntimeError(f"{label}: image validation failed: {exc}") from exc


def normalize_role(row: dict) -> str:
    raw = clean(
        row.get("role")
        or row.get("scene_type")
        or row.get("type")
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
            f"Unsupported scene role {raw!r} for {row.get('scene_id')!r}."
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
            scene_id = f"part_{part:02d}_scene_{source_scene_number:02d}"
        else:
            scene_id = f"part_{part:02d}_{role}"

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
        raise RuntimeError(f"Invalid scene ID: {scene_id!r}")

    return scene_id


def resolve_part_numbers(rows: list[dict], expected_parts: int) -> list[int]:
    found = sorted({
        positive_int(row.get("part"), "scene part")
        for row in rows
        if isinstance(row, dict)
    })

    if len(found) != expected_parts:
        raise RuntimeError(
            f"Selected PARTS mismatch: expected {expected_parts}, "
            f"found part numbers {found}."
        )

    if found != list(range(found[0], found[0] + expected_parts)):
        raise RuntimeError(
            f"Part numbering must be continuous: {found}."
        )

    return found


def normalize_source_scenes(
    document: dict,
    expected_parts: int,
    scenes_per_part: int,
) -> tuple[list[dict], list[int]]:
    if not isinstance(document, dict):
        raise RuntimeError("scenes.json root must be a JSON object.")

    rows = document.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json has no scenes array.")

    part_numbers = resolve_part_numbers(rows, expected_parts)
    part_set = set(part_numbers)

    prepared = []
    seen_ids = set()
    seen_main = set()

    counts = {
        role: {part: 0 for part in part_numbers}
        for role in ROLE_ORDER
    }

    for index, original in enumerate(rows, 1):
        if not isinstance(original, dict):
            raise RuntimeError(f"Scene record #{index} is not an object.")

        part = positive_int(original.get("part"), f"Scene #{index} part")
        if part not in part_set:
            raise RuntimeError(
                f"Scene #{index}: part {part} is outside selected parts {part_numbers}."
            )

        role = normalize_role(original)
        raw_scene = original.get("scene", original.get("local_scene"))

        if role == "main":
            source_number = positive_int(raw_scene, f"Part {part} main scene")

            if source_number > scenes_per_part:
                raise RuntimeError(
                    f"Part {part}: main scene {source_number} exceeds "
                    f"SCENES={scenes_per_part}."
                )

            pair = (part, source_number)
            if pair in seen_main:
                raise RuntimeError(f"Duplicate main scene: {pair}.")
            seen_main.add(pair)
        else:
            source_number = None
            if raw_scene not in (None, ""):
                raise RuntimeError(
                    f"Extra scene {original.get('scene_id')} must not "
                    "use a main scene number."
                )

        scene_id = normalize_scene_id(original, part, role, source_number)
        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate scene_id: {scene_id}")

        seen_ids.add(scene_id)
        counts[role][part] += 1

        prepared.append({
            **dict(original),
            "part": part,
            "role": role,
            "scene_id": scene_id,
            "source_scene_number": source_number,
            "is_extra_scene": role != "main",
        })

    expected_main = {
        (part, scene)
        for part in part_numbers
        for scene in range(1, scenes_per_part + 1)
    }

    missing = sorted(expected_main - seen_main)
    extra = sorted(seen_main - expected_main)

    if missing or extra:
        raise RuntimeError(
            f"Main scene mapping mismatch. Missing={missing[:20]}, extra={extra[:20]}."
        )

    for position, part in enumerate(part_numbers):
        if counts["hook"][part] != 1:
            raise RuntimeError(f"Part {part} requires exactly one Hook.")

        if counts["main"][part] != scenes_per_part:
            raise RuntimeError(
                f"Part {part} requires exactly {scenes_per_part} Main scenes."
            )

        if position < len(part_numbers) - 1:
            if counts["suspense"][part] != 1:
                raise RuntimeError(
                    f"Non-final part {part} requires exactly one Suspense."
                )
            if counts["resolution"][part]:
                raise RuntimeError(
                    f"Non-final part {part} cannot contain Resolution."
                )
        else:
            if counts["resolution"][part] != 1:
                raise RuntimeError(
                    f"Final part {part} requires exactly one Resolution."
                )
            if counts["suspense"][part]:
                raise RuntimeError(
                    f"Final part {part} cannot contain Suspense."
                )

    prepared.sort(
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
            row["source_scene_number"] or 0,
            row["scene_id"],
        )
    )

    return prepared, part_numbers


def extract_job_rows(document: Any) -> tuple[list[dict], dict]:
    if isinstance(document, list):
        return document, {}

    if not isinstance(document, dict):
        raise RuntimeError("visual_jobs.json root must be an object or list.")

    rows = (
        document.get("jobs")
        or document.get("visual_jobs")
        or document.get("scenes")
    )

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("visual_jobs.json has no valid jobs list.")

    return rows, document


def extract_manifest_rows(document: Any, label: str) -> list[dict]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        rows = document.get("scenes", [])
    else:
        rows = []

    if not isinstance(rows, list):
        raise RuntimeError(f"{label} scenes must be a list.")

    return [row for row in rows if isinstance(row, dict)]


def index_by_scene_id(rows: list[dict], label: str) -> dict[str, dict]:
    result = {}

    for index, row in enumerate(rows, 1):
        scene_id = clean(row.get("scene_id"))
        if not scene_id:
            raise RuntimeError(f"{label} record #{index} has no scene_id.")

        if scene_id in result:
            raise RuntimeError(f"{label} has duplicate scene_id: {scene_id}")

        result[scene_id] = row

    return result


def verify_job_source_mapping(
    source_scenes: list[dict],
    job_rows: list[dict],
) -> dict[str, dict]:
    jobs = index_by_scene_id(job_rows, "visual_jobs")

    expected = {row["scene_id"] for row in source_scenes}
    actual = set(jobs)

    if expected != actual:
        raise RuntimeError(
            "Visual job mapping mismatch. "
            f"Missing={sorted(expected - actual)[:20]}, "
            f"extra={sorted(actual - expected)[:20]}."
        )

    for source in source_scenes:
        scene_id = source["scene_id"]
        job = jobs[scene_id]

        try:
            part = int(job.get("part"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{scene_id}: visual job part invalid.") from exc

        if part != source["part"]:
            raise RuntimeError(f"{scene_id}: visual job part mismatch.")

        if normalize_role(job) != source["role"]:
            raise RuntimeError(f"{scene_id}: visual job role mismatch.")

        if source["role"] == "main":
            number = job.get(
                "source_scene_number",
                job.get("source_local_scene", job.get("scene")),
            )
            try:
                number = int(number)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{scene_id}: source main scene number missing."
                ) from exc

            if number != source["source_scene_number"]:
                raise RuntimeError(f"{scene_id}: main scene number changed.")

    return jobs


def normalize_duration_value(value: Any) -> float | None:
    if value is None or clean(value).lower() in {
        "", "auto", "dynamic", "variable", "unknown"
    }:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number) or not MIN_DURATION <= number <= MAX_DURATION:
        return None

    return round(number, 4)


def narration_record_map(document: Any) -> dict[str, dict]:
    if not isinstance(document, dict):
        return {}

    result = {}

    for field in ("scenes", "extra_scenes"):
        rows = document.get(field, [])
        if not isinstance(rows, list):
            raise RuntimeError(f"narration.json {field} must be a list.")

        for row in rows:
            if not isinstance(row, dict):
                continue

            scene_id = clean(row.get("scene_id"))
            if not scene_id:
                continue

            if scene_id in result:
                raise RuntimeError(f"Duplicate narration scene_id: {scene_id}")

            result[scene_id] = row

    return result


def measured_audio_record(
    scene_id: str,
    narration_row: dict | None,
) -> dict | None:
    if not isinstance(narration_row, dict):
        return None

    if clean(narration_row.get("status")).lower() != "completed":
        return None

    duration = None
    for key in (
        "actual_audio_duration_seconds",
        "audio_duration",
        "duration",
    ):
        duration = normalize_duration_value(narration_row.get(key))
        if duration is not None:
            break

    audio_path_value = (
        narration_row.get("audio_path")
        or narration_row.get("audio_file")
        or narration_row.get("audio")
        or narration_row.get("output")
    )

    if duration is None or not audio_path_value:
        return None

    audio_path = safe_project_path(audio_path_value, f"{scene_id} narration audio")
    if not audio_path.is_file():
        return None

    expected_hash = clean(
        narration_row.get("audio_sha256")
        or narration_row.get("audio_hash")
        or narration_row.get("sha256")
    )

    actual_hash = sha256_file(audio_path)
    if expected_hash and actual_hash != expected_hash:
        raise RuntimeError(f"{scene_id}: narration audio SHA256 mismatch.")

    return {
        "duration": duration,
        "audio_path": audio_path.relative_to(ROOT).as_posix(),
        "audio_sha256": actual_hash,
        "duration_source": "ffprobe_measured",
        "audio_provider": narration_row.get("tts_provider") or narration_row.get("provider"),
        "audio_model": narration_row.get("tts_model") or narration_row.get("model"),
    }


def validate_character_bible(config: dict) -> dict:
    enabled = as_bool(config.get("CHARACTER_BIBLE", True), True)

    if not enabled:
        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "world": {},
        }

    document = read_json(CHARACTER_BIBLE_FILE, required=True)

    if not isinstance(document, dict):
        raise RuntimeError("Character Bible root must be a JSON object.")

    status = clean(document.get("status")).lower()
    if status != "completed":
        raise RuntimeError(
            f"Character Bible is not completed: {status or 'missing'}."
        )

    characters = document.get("characters", [])
    scene_map = document.get("scene_character_map", [])
    world = document.get("world", {})

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
            raise RuntimeError(f"Invalid Character Bible character #{index}.")

        character_id = clean(
            character.get("id") or character.get("character_id")
        )
        name = clean(character.get("name"))

        if not character_id or not name:
            raise RuntimeError(
                f"Character Bible record #{index} needs ID and name."
            )

        if character_id.casefold() in known_ids:
            raise RuntimeError(f"Duplicate Character ID: {character_id}")
        if name.casefold() in known_names:
            raise RuntimeError(f"Duplicate Character name: {name}")

        known_ids.add(character_id.casefold())
        known_names.add(name.casefold())

    seen = set()
    for index, row in enumerate(scene_map, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid Character Bible scene map #{index}.")

        scene_id = clean(row.get("scene_id"))
        if not scene_id or scene_id in seen:
            raise RuntimeError(
                f"Missing or duplicate Character Bible scene map ID: {scene_id!r}"
            )
        seen.add(scene_id)

        values = row.get("character_ids", [])
        if not isinstance(values, list):
            raise RuntimeError(f"{scene_id}: character_ids must be a list.")

        unknown = [
            clean(value) for value in values
            if clean(value).casefold() not in known_ids
            and clean(value).casefold() not in known_names
        ]
        if unknown:
            raise RuntimeError(f"{scene_id}: unknown Character IDs {unknown[:10]}.")

    return document


def validate_scene_character_links(
    source_scenes: list[dict],
    bible: dict,
) -> dict[str, dict]:
    if bible.get("status") == "disabled":
        return {}

    character_ids = {
        clean(row.get("id") or row.get("character_id")).casefold()
        for row in bible.get("characters", [])
        if isinstance(row, dict)
    }
    character_ids.discard("")

    mapping = {}
    for row in bible.get("scene_character_map", []):
        scene_id = clean(row.get("scene_id"))
        if scene_id:
            mapping[scene_id] = row

    source_ids = {row["scene_id"] for row in source_scenes}
    extra_ids = set(mapping) - source_ids

    if extra_ids:
        raise RuntimeError(
            "Character Bible contains unmapped scene IDs: "
            + ", ".join(sorted(extra_ids)[:20])
        )

    for scene in source_scenes:
        scene_id = scene["scene_id"]
        mapped = mapping.get(scene_id)

        if mapped is None:
            raise RuntimeError(
                f"Character Bible mapping missing for scene {scene_id}."
            )

        references = mapped.get("character_ids", [])
        if not isinstance(references, list):
            raise RuntimeError(f"{scene_id}: invalid character_ids mapping.")

        unknown = [
            clean(value) for value in references
            if clean(value).casefold() not in character_ids
        ]
        if unknown:
            raise RuntimeError(f"{scene_id}: unknown character IDs {unknown[:10]}.")

    return mapping

def scene_order_key(row: dict) -> tuple:
    return (
        int(row["part"]),
        ROLE_ORDER[row["role"]],
        int(row.get("source_scene_number") or 0),
        str(row["scene_id"]),
    )


def verify_image_record(
    source: dict,
    visual_job: dict,
    image_record: dict,
    format_name: str,
) -> tuple[Path, dict]:
    scene_id = source["scene_id"]

    status = clean(image_record.get("status")).lower()
    if status not in {"generated", "existing", "completed"}:
        raise RuntimeError(
            f"{scene_id}: image is not completed; status={status!r}."
        )

    path_value = (
        visual_job.get("image_path")
        or visual_job.get("output_path")
        or visual_job.get("image")
        or image_record.get("image_path")
        or image_record.get("image")
        or image_record.get("file")
    )

    image_path = safe_project_path(path_value, scene_id)
    info = validate_image(image_path, scene_id)

    expected_hash = clean(
        image_record.get("image_sha256")
        or image_record.get("sha256")
    )

    if not expected_hash:
        raise RuntimeError(f"{scene_id}: image manifest SHA256 missing.")

    if info["sha256"] != expected_hash:
        raise RuntimeError(f"{scene_id}: image SHA256 mismatch.")

    job_hash = clean(
        visual_job.get("image_sha256")
        or visual_job.get("sha256")
    )

    if job_hash and job_hash != info["sha256"]:
        raise RuntimeError(f"{scene_id}: visual job image SHA256 mismatch.")

    expected_size = IMAGE_SIZES[format_name]
    actual_size = (info["width"], info["height"])

    # The image generator may store the target dimensions directly.
    # Any different dimensions must be explicitly rejected here.
    if actual_size != expected_size:
        raise RuntimeError(
            f"{scene_id}: {format_name.upper()} image must be "
            f"{expected_size[0]}x{expected_size[1]}, "
            f"found {info['width']}x{info['height']}."
        )

    manifest_prompt_hash = clean(
        image_record.get("prompt_sha256")
    )
    job_prompt_hash = clean(
        visual_job.get("prompt_sha256")
    )

    if (
        manifest_prompt_hash
        and job_prompt_hash
        and manifest_prompt_hash != job_prompt_hash
    ):
        raise RuntimeError(f"{scene_id}: prompt SHA256 mismatch.")

    manifest_signature = clean(
        image_record.get("prompt_signature")
        or image_record.get("job_signature")
    )
    job_signature = clean(
        visual_job.get("prompt_signature")
        or visual_job.get("job_signature")
    )

    if (
        manifest_signature
        and job_signature
        and manifest_signature != job_signature
    ):
        raise RuntimeError(f"{scene_id}: image job signature mismatch.")

    return image_path, info


def character_context(
    scene: dict,
    character_map: dict[str, dict],
    bible: dict,
) -> dict:
    mapping = character_map.get(scene["scene_id"], {})
    ids = mapping.get("character_ids", [])

    wanted = {clean(value).casefold() for value in ids}
    selected = []

    for character in bible.get("characters", []):
        if not isinstance(character, dict):
            continue

        candidates = {
            clean(character.get("id")).casefold(),
            clean(character.get("character_id")).casefold(),
            clean(character.get("name")).casefold(),
        }

        aliases = character.get("aliases", [])
        if isinstance(aliases, list):
            candidates.update(clean(value).casefold() for value in aliases)

        if wanted.intersection(candidates):
            selected.append(character)

    return {
        "character_ids": ids,
        "characters": selected,
        "world": bible.get("world", {}),
    }


def prepare_photo_motion_jobs(
    source_scenes: list[dict],
    jobs_by_id: dict[str, dict],
    image_manifest: dict,
    config: dict,
    format_name: str,
    part_numbers: list[int],
    scenes_per_part: int,
    bible: dict,
    character_map: dict[str, dict],
    narration_map: dict[str, dict],
) -> tuple[list[dict], dict]:
    manifest_rows = extract_manifest_rows(image_manifest, "image_manifest")
    if not manifest_rows:
        raise RuntimeError("Image manifest has no scene records.")

    manifest_by_id = index_by_scene_id(manifest_rows, "image_manifest")

    expected_ids = {row["scene_id"] for row in source_scenes}
    actual_ids = set(manifest_by_id)

    if expected_ids != actual_ids:
        raise RuntimeError(
            "Image manifest scene mapping mismatch. "
            f"Missing={sorted(expected_ids - actual_ids)[:20]}, "
            f"extra={sorted(actual_ids - expected_ids)[:20]}."
        )

    role_counts = {role: 0 for role in ROLE_ORDER}
    part_role_counts = {
        part: {role: 0 for role in ROLE_ORDER}
        for part in part_numbers
    }

    part_slots = {part: 0 for part in part_numbers}
    jobs = []

    for global_number, source in enumerate(source_scenes, 1):
        scene_id = source["scene_id"]
        part = source["part"]
        role = source["role"]

        visual_job = jobs_by_id[scene_id]
        image_record = manifest_by_id[scene_id]

        image_path, image_info = verify_image_record(
            source,
            visual_job,
            image_record,
            format_name,
        )

        narration = clean(
            source.get("narration")
            or source.get("text")
            or visual_job.get("narration")
            or visual_job.get("text")
        )

        if not narration:
            raise RuntimeError(f"{scene_id}: narration text missing.")

        prompt = clean(
            visual_job.get("prompt")
            or visual_job.get("image_prompt")
            or visual_job.get("visual_prompt")
            or source.get("visual_prompt")
            or source.get("visual")
        )

        if len(prompt) < 24:
            raise RuntimeError(f"{scene_id}: visual prompt missing or too short.")

        narration_row = narration_map.get(scene_id)
        measured = measured_audio_record(scene_id, narration_row)

        source_duration = None
        for candidate in (
            narration_row,
            source,
            visual_job,
        ):
            if not isinstance(candidate, dict):
                continue

            for key in (
                "actual_audio_duration_seconds",
                "audio_duration",
                "duration",
                "scene_duration",
            ):
                source_duration = normalize_duration_value(
                    candidate.get(key)
                )
                if source_duration is not None:
                    break

            if source_duration is not None:
                break

        # Audio-file measurements are authoritative. A configured or
        # estimated scene duration is never allowed to overwrite them.
        if measured:
            duration = measured["duration"]
            duration_source = "ffprobe_measured"
        else:
            duration = source_duration
            duration_source = (
                "source_duration"
                if source_duration is not None
                else "pending_tts_measurement"
            )

        part_slots[part] += 1
        local_slot = part_slots[part]

        source_scene_number = (
            source["source_scene_number"]
            if role == "main"
            else None
        )

        character_data = character_context(
            source,
            character_map,
            bible,
        )

        relative_image = image_path.relative_to(ROOT).as_posix()
        audio_data = measured or {}

        row = {
            "global_scene": global_number,
            "global_scene_number": global_number,
            "scene_index": global_number,

            "part": part,
            "scene": local_slot,
            "local_scene": local_slot,

            "source_scene_number": source_scene_number,
            "source_local_scene": source_scene_number,
            "source_scene_id": scene_id,

            "scene_id": scene_id,
            "id": scene_id,
            "role": role,
            "scene_type": role,
            "is_extra_scene": role != "main",

            "title": clean(
                source.get("title")
                or visual_job.get("title")
            ),
            "narration": narration,
            "text": narration,
            "dialogue": clean(
                source.get("dialogue")
                or visual_job.get("dialogue")
            ),

            "image": relative_image,
            "image_path": relative_image,
            "output_path": relative_image,
            "image_sha256": image_info["sha256"],
            "image_width": image_info["width"],
            "image_height": image_info["height"],
            "image_format": image_info["format"],
            "image_size_bytes": image_info["size_bytes"],

            "prompt": prompt,
            "image_prompt": prompt,
            "negative_prompt": clean(
                visual_job.get("negative_prompt")
                or source.get("negative_prompt")
            ),
            "prompt_sha256": image_record.get("prompt_sha256"),
            "prompt_signature": (
                visual_job.get("prompt_signature")
                or visual_job.get("job_signature")
            ),
            "source_scene_signature": (
                visual_job.get("source_scene_signature")
                or source.get("source_scene_signature")
            ),

            "duration": duration if duration is not None else "auto",
            "scene_duration": duration if duration is not None else "auto",
            "duration_source": duration_source,
            "audio_path": audio_data.get("audio_path"),
            "audio_sha256": audio_data.get("audio_sha256"),
            "audio_duration": duration,
            "actual_audio_duration_seconds": duration,
            "audio_duration_source": (
                "ffprobe_measured" if measured else None
            ),

            "character_ids": character_data["character_ids"],
            "characters": character_data["characters"],
            "world": character_data["world"],
            "world_context": clean(
                source.get("world_context")
                or visual_job.get("world_context")
            ),

            "camera_prompt": clean(
                source.get("camera_prompt")
                or visual_job.get("camera_prompt")
            ),
            "lighting_prompt": clean(
                source.get("lighting_prompt")
                or visual_job.get("lighting_prompt")
            ),
            "mood": clean(
                source.get("mood")
                or visual_job.get("mood")
            ),

            "image_provider": image_record.get("provider"),
            "image_status": image_record.get("status"),
            "photo_motion_status": "pending",
        }

        jobs.append(row)
        role_counts[role] += 1
        part_role_counts[part][role] += 1

    expected_total_main = len(part_numbers) * scenes_per_part

    if role_counts["main"] != expected_total_main:
        raise RuntimeError(
            f"Main scene count mismatch: expected {expected_total_main}, "
            f"found {role_counts['main']}."
        )

    for position, part in enumerate(part_numbers):
        counts = part_role_counts[part]

        if counts["hook"] != 1 or counts["main"] != scenes_per_part:
            raise RuntimeError(f"Part {part}: Hook/Main role counts invalid.")

        if position < len(part_numbers) - 1:
            if counts["suspense"] != 1 or counts["resolution"] != 0:
                raise RuntimeError(f"Part {part}: Suspense/Resolution counts invalid.")
        else:
            if counts["resolution"] != 1 or counts["suspense"] != 0:
                raise RuntimeError(f"Final part {part}: Resolution/Suspense counts invalid.")

    if len({row["scene_id"] for row in jobs}) != len(jobs):
        raise RuntimeError("Prepared Photo Motion jobs contain duplicate IDs.")

    if [row["global_scene"] for row in jobs] != list(range(1, len(jobs) + 1)):
        raise RuntimeError("Photo Motion global numbering is not continuous.")

    settings = {
        "version": VERSION,
        "format": format_name,
        "part_numbers": part_numbers,
        "scenes_per_part": scenes_per_part,
        "character_bible_signature": stable_hash(bible),
        "source_scene_ids": [row["scene_id"] for row in source_scenes],
        "image_hashes": {
            row["scene_id"]: row["image_sha256"]
            for row in jobs
        },
        "audio_hashes": {
            row["scene_id"]: row.get("audio_sha256")
            for row in jobs
        },
    }

    config_signature = stable_hash(settings)

    document = {
        "version": VERSION,
        "status": "completed",
        "stage": "photo_motion_jobs_prepared",
        "format": format_name,
        "parts": len(part_numbers),
        "part_numbers": part_numbers,
        "total_parts": len(part_numbers),
        "scenes_per_part": scenes_per_part,
        "total_jobs": len(jobs),
        "total_scenes": len(jobs),
        "total_main_scenes": role_counts["main"],
        "total_extra_scenes": len(jobs) - role_counts["main"],
        "scene_role_counts": role_counts,
        "part_scene_role_counts": {
            str(part): part_role_counts[part]
            for part in part_numbers
        },
        "scene_order": (
            "part_then_hook_then_main_then_suspense_or_final_resolution"
        ),
        "main_scene_numbering": "source_scene_number_preserved",
        "extra_scene_numbering": "separate_local_photo_motion_slot",
        "duration_policy": (
            "ffprobe_measured_audio_is_authoritative; "
            "unmeasured_audio_duration_remains_auto"
        ),
        "measured_audio_count": sum(
            row.get("audio_duration_source") == "ffprobe_measured"
            for row in jobs
        ),
        "pending_audio_measurement_count": sum(
            row.get("audio_duration_source") != "ffprobe_measured"
            for row in jobs
        ),
        "config_signature": config_signature,
        "character_bible_status": bible.get("status", "unknown"),
        "jobs": jobs,
    }

    return jobs, document


def validate_manifest_status(document: dict, label: str) -> None:
    if not isinstance(document, dict):
        raise RuntimeError(f"{label} root must be an object.")

    status = clean(document.get("status")).lower()
    if status not in {"completed", "complete", "success"}:
        raise RuntimeError(f"{label} is not completed: {status or 'missing'}.")


def validate_manifest_total(document: dict, expected: int, label: str) -> None:
    value = document.get(
        "images_required",
        document.get("total_scenes", document.get("total_jobs", expected)),
    )

    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}: total count invalid.") from exc

    if value != expected:
        raise RuntimeError(
            f"{label}: expected {expected} records, found {value}."
        )


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — PREPARING PHOTO MOTION VISUALS")
    log("Version:", VERSION)
    log("=" * 64)

    config = load_and_validate()
    if not isinstance(config, dict):
        raise RuntimeError("Input configuration is not a valid dictionary.")

    format_name = str(get_format(config)).strip().lower()
    if format_name not in IMAGE_SIZES:
        raise RuntimeError(f"Unsupported output format: {format_name!r}.")

    parts = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    resume_enabled = as_bool(config.get("RESUME_ENABLED", True), True)
    skip_completed = as_bool(config.get("SKIP_COMPLETED_SCENES", True), True)

    log("FORMAT:", format_name)
    log("PARTS:", parts)
    log("MAIN SCENES PER PART:", scenes_per_part)
    log("RESUME ENABLED:", resume_enabled)
    log("SKIP COMPLETED:", skip_completed)

    scenes_document = read_json(SCENES_FILE, required=True)
    source_scenes, part_numbers = normalize_source_scenes(
        scenes_document,
        parts,
        scenes_per_part,
    )

    bible = validate_character_bible(config)
    character_map = validate_scene_character_links(source_scenes, bible)

    visual_document = read_json(VISUAL_JOBS_FILE, required=True)
    visual_rows, visual_metadata = extract_job_rows(visual_document)
    jobs_by_id = verify_job_source_mapping(source_scenes, visual_rows)

    image_manifest = read_json(IMAGE_MANIFEST_FILE, required=True)
    validate_manifest_status(image_manifest, "image_manifest.json")
    validate_manifest_total(
        image_manifest,
        len(source_scenes),
        "image_manifest.json",
    )

    generation_document = read_json(GENERATION_MANIFEST_FILE, required=True)
    validate_manifest_status(
        generation_document,
        "image_generation_manifest.json",
    )
    validate_manifest_total(
        generation_document,
        len(source_scenes),
        "image_generation_manifest.json",
    )

    generation_rows = extract_manifest_rows(
        generation_document,
        "image_generation_manifest",
    )
    generation_by_id = index_by_scene_id(
        generation_rows,
        "image_generation_manifest",
    )

    image_rows = extract_manifest_rows(
        image_manifest,
        "image_manifest",
    )
    image_by_id = index_by_scene_id(image_rows, "image_manifest")

    if set(generation_by_id) != set(image_by_id):
        raise RuntimeError(
            "Image manifest और generation manifest scene IDs अलग हैं."
        )

    for scene_id in image_by_id:
        image_row = image_by_id[scene_id]
        generation_row = generation_by_id[scene_id]

        if clean(image_row.get("image_sha256")) != clean(
            generation_row.get("image_sha256")
        ):
            raise RuntimeError(
                f"{scene_id}: image manifest SHA256 generation manifest "
                "से मेल नहीं खाता."
            )

        if clean(image_row.get("status")).lower() not in {
            "generated", "existing", "completed"
        }:
            raise RuntimeError(f"{scene_id}: image is not ready.")

    narration_document = read_json(NARRATION_FILE, required=False)
    narration_map = narration_record_map(narration_document)

    if narration_document is not None:
        if not isinstance(narration_document, dict):
            raise RuntimeError("narration.json root must be an object.")

        narration_status = clean(
            narration_document.get("status")
        ).lower()

        if narration_status not in {
            "completed", "complete", "success", "in_progress"
        }:
            raise RuntimeError(
                f"Invalid narration status: {narration_status!r}."
            )

    jobs, document = prepare_photo_motion_jobs(
        source_scenes=source_scenes,
        jobs_by_id=jobs_by_id,
        image_manifest=image_manifest,
        config=config,
        format_name=format_name,
        part_numbers=part_numbers,
        scenes_per_part=scenes_per_part,
        bible=bible,
        character_map=character_map,
        narration_map=narration_map,
    )

    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    previous = read_json(CHECKPOINT_FILE) if resume_enabled else None
    previous_signature = (
        clean(previous.get("config_signature"))
        if isinstance(previous, dict)
        else ""
    )

    signature_matches = previous_signature == document["config_signature"]

    previous_rows = (
        previous.get("scenes", [])
        if isinstance(previous, dict) and signature_matches
        else []
    )

    previous_by_id = {
        clean(row.get("scene_id")): row
        for row in previous_rows
        if isinstance(row, dict) and clean(row.get("scene_id"))
    }

    checkpoint_rows = []

    for row in jobs:
        old = previous_by_id.get(row["scene_id"], {})
        reusable = (
            signature_matches
            and skip_completed
            and old.get("status") == "prepared"
            and old.get("image_sha256") == row["image_sha256"]
            and old.get("audio_sha256") == row.get("audio_sha256")
            and old.get("job_signature") == document["config_signature"]
        )

        checkpoint_rows.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "local_scene": row["local_scene"],
            "source_scene_number": row["source_scene_number"],
            "role": row["role"],
            "image_path": row["image_path"],
            "image_sha256": row["image_sha256"],
            "audio_path": row.get("audio_path"),
            "audio_sha256": row.get("audio_sha256"),
            "job_signature": document["config_signature"],
            "status": "prepared",
            "reused_checkpoint": bool(reusable),
        })

    checkpoint = {
        "version": VERSION,
        "status": "completed",
        "config_signature": document["config_signature"],
        "total_jobs": len(jobs),
        "prepared": len(checkpoint_rows),
        "pending": 0,
        "scenes": checkpoint_rows,
    }

    manifest = {
        "version": VERSION,
        "status": "completed",
        "format": format_name,
        "parts": len(part_numbers),
        "part_numbers": part_numbers,
        "total_jobs": len(jobs),
        "config_signature": document["config_signature"],
        "image_manifest_signature": stable_hash(image_manifest),
        "generation_manifest_signature": stable_hash(generation_document),
        "visual_jobs_signature": stable_hash(visual_metadata),
        "measured_audio_count": document["measured_audio_count"],
        "pending_audio_measurement_count": document[
            "pending_audio_measurement_count"
        ],
        "scene_ids": [row["scene_id"] for row in jobs],
    }

    # Validate in-memory structures before publishing any completed status.
    if len(jobs) != len(source_scenes):
        raise RuntimeError("Prepared job count differs from source scene count.")

    for row in jobs:
        if not row["image_sha256"] or not row["image_path"]:
            raise RuntimeError(f"{row['scene_id']}: final image validation missing.")

    atomic_json(PHOTO_MOTION_JOBS_FILE, document)
    atomic_json(PHOTO_MOTION_MANIFEST_FILE, manifest)
    atomic_json(CHECKPOINT_FILE, checkpoint)

    # Read-after-write verification prevents false completed checkpoints.
    written_jobs = read_json(PHOTO_MOTION_JOBS_FILE, required=True)
    written_manifest = read_json(PHOTO_MOTION_MANIFEST_FILE, required=True)
    written_checkpoint = read_json(CHECKPOINT_FILE, required=True)

    if (
        not isinstance(written_jobs, dict)
        or written_jobs.get("status") != "completed"
        or len(written_jobs.get("jobs", [])) != len(jobs)
    ):
        raise RuntimeError("Photo Motion jobs write verification failed.")

    if (
        not isinstance(written_manifest, dict)
        or written_manifest.get("status") != "completed"
    ):
        raise RuntimeError("Photo Motion manifest write verification failed.")

    if (
        not isinstance(written_checkpoint, dict)
        or written_checkpoint.get("status") != "completed"
        or written_checkpoint.get("prepared") != len(jobs)
    ):
        raise RuntimeError("Photo Motion checkpoint write verification failed.")

    log("-" * 64)
    log("PHOTO MOTION JOBS PREPARED")
    log("Format:", format_name)
    log("Parts:", part_numbers)
    log("Main scenes per part:", scenes_per_part)
    log("Total main scenes:", document["total_main_scenes"])
    log("Total extra scenes:", document["total_extra_scenes"])
    log("Total jobs:", len(jobs))

    for role in ("hook", "main", "suspense", "resolution"):
        log(role.upper() + ":", document["scene_role_counts"][role])

    log("Measured narration audio:", document["measured_audio_count"])
    log("Pending audio measurements:", document["pending_audio_measurement_count"])
    log("Photo Motion jobs:", PHOTO_MOTION_JOBS_FILE.relative_to(ROOT))
    log("Photo Motion manifest:", PHOTO_MOTION_MANIFEST_FILE.relative_to(ROOT))
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
        log("PHOTO MOTION VISUAL PREPARATION FAILED:", str(exc))
        sys.exit(1)