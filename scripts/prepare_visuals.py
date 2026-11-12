#!/usr/bin/env python3
"""Strictly validate image, story, character and measured-audio records
before creating resumable Photo Motion jobs.
"""

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

from input_config import load_and_validate, get_format, get_parts, get_scenes_range

OUTPUT = ROOT / "output"
VISUALS = OUTPUT / "visuals"
CHECKPOINTS = OUTPUT / "checkpoints"

SCENES_FILE = OUTPUT / "scenes" / "scenes.json"
BIBLE_FILE = OUTPUT / "story" / "character_bible.json"
NARRATION_FILE = OUTPUT / "narration" / "narration.json"

VISUAL_JOBS_FILE = VISUALS / "visual_jobs.json"
VISUAL_JOBS_MANIFEST = VISUALS / "visual_jobs_manifest.json"
IMAGE_MANIFEST = VISUALS / "image_manifest.json"
GENERATION_MANIFEST = VISUALS / "image_generation_manifest.json"

PHOTO_JOBS_FILE = VISUALS / "photo_motion_jobs.json"
PHOTO_MANIFEST_FILE = VISUALS / "photo_motion_manifest.json"
CHECKPOINT_FILE = CHECKPOINTS / "prepare_visuals_progress.json"

VERSION = "prepare-visuals-v16-canonical-scene-metadata"

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

IMAGE_SIZES = {
    "short": (720, 1280),
    "full": (1280, 720),
}

MIN_IMAGE_BYTES = 1000
MIN_AUDIO_BYTES = 100
MIN_IMAGE_DIMENSION = 256
MAX_IMAGE_DIMENSION = 4096
MIN_DURATION = 0.5
MAX_DURATION = 3600.0
DEFAULT_VISUAL_SCENE_SECONDS = 6.0


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
    if isinstance(value, bool):
        raise RuntimeError(f"{label}: boolean is not a valid number.")

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}: invalid number {value!r}.") from exc

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
            raise RuntimeError(
                f"Required file missing: {path.relative_to(ROOT)}"
            )
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
    raw = clean(value)

    if not raw:
        raise RuntimeError(f"{label}: file path is missing.")

    path = Path(raw)

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(
            f"{label}: path outside the project directory is forbidden."
        )

    return path


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

    if role not in ROLE_ORDER:
        raise RuntimeError(
            f"Unsupported role {raw!r} for scene {row.get('scene_id')!r}."
        )

    return role


def normalize_scene_id(
    row: dict,
    part: int,
    role: str,
    scene_number: int | None,
) -> str:
    scene_id = clean(
        row.get("scene_id")
        or row.get("id")
        or row.get("source_scene_id")
    )

    expected = (
        f"part_{part:02d}_scene_{scene_number:02d}"
        if role == "main"
        else f"part_{part:02d}_{role}"
    )

    if not scene_id:
        scene_id = expected

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
        raise RuntimeError(f"Invalid scene ID: {scene_id!r}.")

    if scene_id != expected:
        raise RuntimeError(
            f"Scene ID mismatch: expected {expected}, received {scene_id}."
        )

    return scene_id


def validate_image(path: Path, scene_id: str) -> dict:
    if not path.is_file():
        raise RuntimeError(f"{scene_id}: image file missing: {path}")

    if path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(f"{scene_id}: image is too small or incomplete.")

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            width, height = image.size
            image_format = (image.format or "").upper()

        if not (
            MIN_IMAGE_DIMENSION <= width <= MAX_IMAGE_DIMENSION
            and MIN_IMAGE_DIMENSION <= height <= MAX_IMAGE_DIMENSION
        ):
            raise RuntimeError(
                f"Invalid image dimensions: {width}x{height}."
            )

        if image_format not in {"PNG", "JPEG", "WEBP"}:
            raise RuntimeError(
                f"Unsupported image format: {image_format}."
            )

    except Exception as exc:
        raise RuntimeError(
            f"{scene_id}: image integrity validation failed: {exc}"
        ) from exc

    return {
        "width": width,
        "height": height,
        "format": image_format,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def validate_image_dimensions(
    image_info: dict,
    format_name: str,
    scene_id: str,
) -> None:
    expected = IMAGE_SIZES[format_name]
    actual = (image_info["width"], image_info["height"])

    if actual != expected:
        raise RuntimeError(
            f"{scene_id}: {format_name.upper()} image must be "
            f"{expected[0]}x{expected[1]}, found "
            f"{actual[0]}x{actual[1]}."
        )


def normalize_source_scenes(
    document: Any,
    expected_parts: int,
    scene_counts: dict[int, int],
    scenes_min: int,
    scenes_max: int,
) -> tuple[list[dict], list[int]]:
    if not isinstance(document, dict):
        raise RuntimeError("scenes.json root must be an object.")
    rows = document.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json has no scenes list.")
    prepared, seen_ids, seen_main, part_numbers = [], set(), set(), set()
    counts: dict[int, dict[str, int]] = {}
    for index, original in enumerate(rows, 1):
        if not isinstance(original, dict):
            raise RuntimeError(f"Scene record #{index} is not an object.")
        part = positive_int(original.get("part"), f"Scene #{index} part")
        part_numbers.add(part)
        role = normalize_role(original)
        raw_scene = original.get("scene", original.get("local_scene"))
        if role == "main":
            scene_number = positive_int(raw_scene, f"Part {part} main scene number")
            actual_count = scene_counts.get(part)
            if actual_count is None or scene_number > actual_count:
                raise RuntimeError(f"Part {part}: invalid main Scene number {scene_number}.")
            pair = (part, scene_number)
            if pair in seen_main:
                raise RuntimeError(f"Duplicate main scene: {pair}.")
            seen_main.add(pair)
        else:
            scene_number = None
            if raw_scene not in (None, ""):
                raise RuntimeError(f"{original.get('scene_id')}: extra role {role} must not use a main scene number.")
        scene_id = normalize_scene_id(original, part, role, scene_number)
        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate scene ID: {scene_id}.")
        seen_ids.add(scene_id)
        narration = clean(original.get("narration"))
        if role == "main":
            if not narration or original.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: Main narration/voiceover is invalid.")
            if original.get("narration_source") != "locked_part_text":
                raise RuntimeError(f"{scene_id}: Main narration source is not locked.")
            if original.get("source_text_sha256") != hashlib.sha256(narration.encode("utf-8")).hexdigest():
                raise RuntimeError(f"{scene_id}: Main narration hash mismatch.")
            if original.get("source_word_count") != len(narration.split()):
                raise RuntimeError(f"{scene_id}: Main narration word count mismatch.")
        else:
            if len(narration.split()) < 8 or original.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: extra role narration/voiceover is invalid.")
            if original.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{scene_id}: extra narration source is invalid.")
            if original.get("source_text_sha256") != hashlib.sha256(narration.encode("utf-8")).hexdigest():
                raise RuntimeError(f"{scene_id}: extra narration hash mismatch.")
            if not clean(original.get("story_anchor")):
                raise RuntimeError(f"{scene_id}: extra-role story anchor is missing.")
        counts.setdefault(part, {name: 0 for name in ROLE_ORDER})
        counts[part][role] += 1
        prepared.append({**dict(original), "part": part, "role": role,
                         "scene_id": scene_id, "source_scene_number": scene_number,
                         "is_extra_scene": role != "main"})
    part_numbers = sorted(part_numbers)
    if len(part_numbers) != expected_parts:
        raise RuntimeError(f"Selected PARTS mismatch: expected {expected_parts}, found {part_numbers}.")
    if part_numbers != list(range(part_numbers[0], part_numbers[0] + expected_parts)):
        raise RuntimeError(f"Part numbering must be continuous: {part_numbers}.")
    if set(scene_counts) != set(part_numbers):
        raise RuntimeError("Actual Scene-count map does not match selected Parts.")
    if any(not scenes_min <= scene_counts[p] <= scenes_max for p in part_numbers):
        raise RuntimeError(f"Actual Scene counts must stay within {scenes_min}-{scenes_max}.")
    expected_main = {(p, n) for p in part_numbers for n in range(1, scene_counts[p] + 1)}
    missing, extra = sorted(expected_main - seen_main), sorted(seen_main - expected_main)
    if missing or extra:
        raise RuntimeError(f"Main Scene mapping mismatch. Missing={missing[:20]}, extra={extra[:20]}.")
    for position, part in enumerate(part_numbers):
        rc = counts.get(part, {})
        if rc.get("hook", 0) != 1:
            raise RuntimeError(f"Part {part} requires exactly one Hook scene.")
        if rc.get("main", 0) != scene_counts[part]:
            raise RuntimeError(f"Part {part} requires exactly {scene_counts[part]} Main scenes.")
        if position < len(part_numbers) - 1:
            if rc.get("suspense", 0) != 1 or rc.get("resolution", 0) != 0:
                raise RuntimeError(f"Non-final Part {part} requires one Suspense and no Resolution.")
        elif rc.get("resolution", 0) != 1 or rc.get("suspense", 0) != 0:
            raise RuntimeError(f"Final Part {part} requires one Resolution and no Suspense.")
    prepared.sort(key=lambda row: (row["part"], ROLE_ORDER[row["role"]],
                                   row["source_scene_number"] or 0, row["scene_id"]))
    return prepared, part_numbers

def extract_rows(document: Any, label: str) -> tuple[list[dict], dict]:
    if isinstance(document, list):
        rows = document
        metadata = {}
    elif isinstance(document, dict):
        rows = (
            document.get("jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
        )
        metadata = document
    else:
        raise RuntimeError(f"{label}: unsupported JSON structure.")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{label}: records list is missing or empty.")

    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{label}: every record must be an object.")

    return rows, metadata


def index_by_scene_id(rows: list[dict], label: str) -> dict[str, dict]:
    result = {}

    for index, row in enumerate(rows, 1):
        scene_id = clean(row.get("scene_id"))

        if not scene_id:
            raise RuntimeError(
                f"{label} record #{index} has no scene_id."
            )

        if scene_id in result:
            raise RuntimeError(
                f"{label} contains duplicate scene ID: {scene_id}."
            )

        result[scene_id] = row

    return result


def verify_visual_jobs(
    source_scenes: list[dict],
    visual_rows: list[dict],
) -> dict[str, dict]:
    jobs = index_by_scene_id(visual_rows, "visual_jobs")

    expected = {row["scene_id"] for row in source_scenes}
    actual = set(jobs)

    if expected != actual:
        raise RuntimeError(
            "Visual jobs do not match scenes.json. "
            f"Missing={sorted(expected - actual)[:20]}, "
            f"extra={sorted(actual - expected)[:20]}."
        )

    for source in source_scenes:
        scene_id = source["scene_id"]
        job = jobs[scene_id]

        if positive_int(job.get("part"), scene_id + " visual part") != source["part"]:
            raise RuntimeError(f"{scene_id}: visual job part mismatch.")

        if normalize_role(job) != source["role"]:
            raise RuntimeError(f"{scene_id}: visual job role mismatch.")

        if source["role"] == "main":
            value = job.get(
                "source_scene_number",
                job.get("source_local_scene", job.get("scene")),
            )

            if positive_int(value, scene_id + " visual scene number") != (
                source["source_scene_number"]
            ):
                raise RuntimeError(
                    f"{scene_id}: visual job scene number mismatch."
                )

        if not clean(job.get("prompt") or job.get("image_prompt")):
            raise RuntimeError(f"{scene_id}: visual prompt is missing.")
        if source["role"] == "main":
            if clean(job.get("narration")) != clean(source.get("narration")):
                raise RuntimeError(f"{scene_id}: visual job narration differs from locked source.")
            if job.get("source_text_sha256") != hashlib.sha256(clean(source.get("narration")).encode("utf-8")).hexdigest():
                raise RuntimeError(f"{scene_id}: visual job narration hash mismatch.")
        else:
            if clean(job.get("narration")) != clean(source.get("narration")) or job.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: extra-role visual job narration mismatch.")
            if job.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{scene_id}: extra-role visual job narration source mismatch.")

        if not clean(job.get("source_scene_signature")):
            raise RuntimeError(
                f"{scene_id}: source scene signature is missing."
            )

        if not clean(job.get("prompt_signature")):
            raise RuntimeError(
                f"{scene_id}: prompt signature is missing."
            )

    return jobs


def validate_manifest_status(document: Any, label: str) -> None:
    if not isinstance(document, dict):
        raise RuntimeError(f"{label}: manifest root must be an object.")

    status = clean(document.get("status")).lower()

    if status not in {"completed", "complete", "success"}:
        raise RuntimeError(
            f"{label}: manifest is not completed "
            f"(status={status or 'missing'})."
        )


def validate_manifest_count(
    document: dict,
    expected: int,
    label: str,
) -> None:
    values = (
        document.get("images_required"),
        document.get("total_scenes"),
        document.get("total_jobs"),
    )

    declared = []

    for value in values:
        if value is None:
            continue

        try:
            declared.append(int(value))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{label}: invalid record count {value!r}."
            ) from exc

    if declared and any(value != expected for value in declared):
        raise RuntimeError(
            f"{label}: expected {expected} records, "
            f"manifest declares {declared}."
        )


def manifest_rows(document: Any, label: str) -> list[dict]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        rows = document.get("scenes")
    else:
        rows = None

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{label}: scenes list is missing or empty.")

    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{label}: invalid scene record.")

    return rows


def validate_image_manifests(
    image_document: dict,
    generation_document: dict,
    expected_ids: set[str],
) -> dict[str, dict]:
    image_rows = manifest_rows(image_document, "image_manifest")
    generation_rows = manifest_rows(
        generation_document,
        "image_generation_manifest",
    )

    image_by_id = index_by_scene_id(image_rows, "image_manifest")
    generation_by_id = index_by_scene_id(
        generation_rows,
        "image_generation_manifest",
    )

    if set(image_by_id) != expected_ids:
        raise RuntimeError(
            "image_manifest scene IDs do not match scenes.json. "
            f"Missing={sorted(expected_ids - set(image_by_id))[:20]}, "
            f"extra={sorted(set(image_by_id) - expected_ids)[:20]}."
        )

    if set(generation_by_id) != expected_ids:
        raise RuntimeError(
            "image_generation_manifest scene IDs do not match scenes.json. "
            f"Missing={sorted(expected_ids - set(generation_by_id))[:20]}, "
            f"extra={sorted(set(generation_by_id) - expected_ids)[:20]}."
        )

    for scene_id in expected_ids:
        image_row = image_by_id[scene_id]
        generation_row = generation_by_id[scene_id]

        image_hash = clean(image_row.get("image_sha256")).lower()
        generation_hash = clean(
            generation_row.get("image_sha256")
        ).lower()

        if not re.fullmatch(r"[a-f0-9]{64}", image_hash):
            raise RuntimeError(
                f"{scene_id}: image manifest SHA256 is invalid."
            )

        if image_hash != generation_hash:
            raise RuntimeError(
                f"{scene_id}: image manifest and generation manifest "
                "SHA256 values do not match."
            )

        for row, label in (
            (image_row, "image_manifest"),
            (generation_row, "image_generation_manifest"),
        ):
            status = clean(row.get("status")).lower()

            if status not in {"generated", "existing", "completed"}:
                raise RuntimeError(
                    f"{scene_id}: {label} status is not ready: {status!r}."
                )

            declared_prompt = clean(row.get("prompt_sha256"))

            if declared_prompt and not re.fullmatch(
                r"[a-fA-F0-9]{64}",
                declared_prompt,
            ):
                raise RuntimeError(
                    f"{scene_id}: invalid prompt SHA256 in {label}."
                )

    return image_by_id


def validate_character_bible(config: dict) -> dict:
    enabled = as_bool(config.get("CHARACTER_BIBLE", True), True)

    if not enabled:
        return {
            "status": "disabled",
            "characters": [],
            "scene_character_map": [],
            "world": {},
        }

    document = read_json(BIBLE_FILE, required=True)

    if not isinstance(document, dict):
        raise RuntimeError("Character Bible root must be an object.")

    if clean(document.get("status")).lower() != "completed":
        raise RuntimeError(
            "Character Bible must have status=completed."
        )

    characters = document.get("characters")
    scene_map = document.get("scene_character_map")
    world = document.get("world", {})

    if not isinstance(characters, list):
        raise RuntimeError("Character Bible characters must be a list.")

    if not isinstance(scene_map, list):
        raise RuntimeError(
            "Character Bible scene_character_map must be a list."
        )

    if not isinstance(world, dict):
        raise RuntimeError("Character Bible world must be an object.")

    known_ids = set()
    known_names = set()

    for index, character in enumerate(characters, 1):
        if not isinstance(character, dict):
            raise RuntimeError(
                f"Invalid Character Bible character #{index}."
            )

        character_id = clean(
            character.get("id") or character.get("character_id")
        )
        name = clean(character.get("name"))

        if not character_id or not name:
            raise RuntimeError(
                f"Character Bible character #{index} needs ID and name."
            )

        if character_id.casefold() in known_ids:
            raise RuntimeError(
                f"Duplicate Character ID: {character_id}."
            )

        if name.casefold() in known_names:
            raise RuntimeError(
                f"Duplicate Character name: {name}."
            )

        known_ids.add(character_id.casefold())
        known_names.add(name.casefold())

    mapping = {}

    for index, row in enumerate(scene_map, 1):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Invalid Character Bible scene map record #{index}."
            )

        scene_id = clean(row.get("scene_id"))

        if not scene_id or scene_id in mapping:
            raise RuntimeError(
                f"Missing or duplicate Character Bible scene ID: {scene_id!r}."
            )

        references = row.get("character_ids", [])

        if not isinstance(references, list):
            raise RuntimeError(
                f"{scene_id}: character_ids must be a list."
            )

        for reference in references:
            key = clean(reference).casefold()

            if key not in known_ids and key not in known_names:
                raise RuntimeError(
                    f"{scene_id}: unknown Character Bible reference "
                    f"{reference!r}."
                )

        mapping[scene_id] = row

    return {
        **document,
        "_character_ids": known_ids,
        "_character_names": known_names,
        "_scene_map": mapping,
    }


def validate_character_mapping(
    source_scenes: list[dict],
    bible: dict,
) -> dict[str, dict]:
    if bible.get("status") == "disabled":
        return {}

    mapping = bible.get("_scene_map", {})
    expected_ids = {row["scene_id"] for row in source_scenes}
    actual_ids = set(mapping)

    if expected_ids != actual_ids:
        raise RuntimeError(
            "Character Bible scene map mismatch. "
            f"Missing={sorted(expected_ids - actual_ids)[:20]}, "
            f"extra={sorted(actual_ids - expected_ids)[:20]}."
        )

    known_ids = bible.get("_character_ids", set())
    known_names = bible.get("_character_names", set())

    for scene in source_scenes:
        scene_id = scene["scene_id"]
        row = mapping[scene_id]
        references = row.get("character_ids", [])

        for reference in references:
            key = clean(reference).casefold()

            if key not in known_ids and key not in known_names:
                raise RuntimeError(
                    f"{scene_id}: invalid character mapping {reference!r}."
                )

    return mapping


def character_context(
    scene: dict,
    character_map: dict[str, dict],
    bible: dict,
) -> dict:
    if bible.get("status") == "disabled":
        return {
            "character_ids": [],
            "characters": [],
            "world": {},
        }

    mapping = character_map[scene["scene_id"]]
    references = mapping.get("character_ids", [])
    wanted = {clean(value).casefold() for value in references}

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
            candidates.update(
                clean(value).casefold()
                for value in aliases
                if clean(value)
            )

        if wanted.intersection(candidates):
            selected.append(character)

    return {
        "character_ids": references,
        "characters": selected,
        "world": bible.get("world", {}),
    }


def valid_duration(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None

    if clean(value).lower() in {
        "", "auto", "dynamic", "variable", "unknown"
    }:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number):
        return None

    if not MIN_DURATION <= number <= MAX_DURATION:
        return None

    return round(number, 4)


def narration_map(document: Any) -> dict[str, dict]:
    if not isinstance(document, dict):
        raise RuntimeError("narration.json root must be an object.")

    status = clean(document.get("status")).lower()

    if status not in {"completed", "complete", "success"}:
        raise RuntimeError(
            "Narration preparation must be completed before visual preparation."
        )

    result = {}

    for field in ("scenes", "extra_scenes"):
        rows = document.get(field, [])

        if not isinstance(rows, list):
            raise RuntimeError(
                f"narration.json {field} must be a list."
            )

        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Invalid narration record {field}[{index - 1}]."
                )

            scene_id = clean(row.get("scene_id"))

            if not scene_id:
                raise RuntimeError(
                    f"Narration record {field}[{index - 1}] has no scene_id."
                )

            if scene_id in result:
                raise RuntimeError(
                    f"Duplicate narration scene ID: {scene_id}."
                )

            result[scene_id] = row

    return result


def measured_audio_record(
    scene_id: str,
    narration_row: dict,
    source_scene: dict,
) -> dict:
    status = clean(narration_row.get("status")).lower()

    if status != "completed":
        raise RuntimeError(
            f"{scene_id}: TTS narration status is not completed."
        )

    duration = None

    for key in (
        "actual_audio_duration_seconds",
        "audio_duration",
        "duration",
    ):
        duration = valid_duration(narration_row.get(key))

        if duration is not None:
            break

    if duration is None:
        raise RuntimeError(
            f"{scene_id}: real measured TTS duration is missing; "
            "'auto' or estimated duration cannot be used."
        )

    audio_path_value = (
        narration_row.get("audio_path")
        or narration_row.get("audio_file")
        or narration_row.get("audio")
        or narration_row.get("output")
    )

    if not audio_path_value:
        raise RuntimeError(
            f"{scene_id}: TTS audio file path is missing."
        )

    audio_path = safe_project_path(
        audio_path_value,
        f"{scene_id} narration audio",
    )

    if not audio_path.is_file():
        raise RuntimeError(
            f"{scene_id}: TTS audio file does not exist: {audio_path}."
        )

    if audio_path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(
            f"{scene_id}: TTS audio file is empty or incomplete."
        )

    actual_hash = sha256_file(audio_path)

    declared_hash = clean(
        narration_row.get("audio_sha256")
        or narration_row.get("audio_hash")
        or narration_row.get("sha256")
    ).lower()

    if not re.fullmatch(r"[a-f0-9]{64}", declared_hash):
        raise RuntimeError(
            f"{scene_id}: TTS audio SHA256 is missing or invalid."
        )

    if actual_hash != declared_hash:
        raise RuntimeError(
            f"{scene_id}: TTS audio SHA256 does not match the file."
        )

    return {
        "audio_path": audio_path.relative_to(ROOT).as_posix(),
        "audio_sha256": actual_hash,
        "duration": duration,
        "actual_audio_duration_seconds": duration,
        "audio_duration": duration,
        "duration_source": "ffprobe_measured",
        "audio_provider": (
            narration_row.get("tts_provider")
            or narration_row.get("provider")
        ),
        "audio_model": (
            narration_row.get("tts_model")
            or narration_row.get("model")
        ),
    }


def validate_narration_mapping(
    source_scenes: list[dict],
    narration_records: dict[str, dict],
) -> dict[str, dict]:
    expected_sources = source_scenes
    expected_ids = {row["scene_id"] for row in expected_sources}
    actual_ids = set(narration_records)
    if expected_ids != actual_ids:
        raise RuntimeError(
            "TTS narration records must match every scene role. "
            f"Missing={sorted(expected_ids - actual_ids)[:20]}, "
            f"extra={sorted(actual_ids - expected_ids)[:20]}."
        )

    by_part = {}
    for source in expected_sources:
        scene_id = source["scene_id"]
        record = narration_records[scene_id]
        if positive_int(record.get("part"), scene_id + " narration part") != source["part"]:
            raise RuntimeError(f"{scene_id}: narration part mismatch.")
        if normalize_role(record) != source["role"]:
            raise RuntimeError(f"{scene_id}: TTS role differs from the visual source.")
        narration_text = clean(record.get("narration") or record.get("text") or record.get("content"))
        source_text = clean(source.get("narration"))
        if not narration_text or narration_text != source_text:
            raise RuntimeError(f"{scene_id}: TTS text differs from locked scenes.json narration.")
        declared_hash = clean(record.get("text_sha256") or record.get("narration_sha256"))
        expected_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if declared_hash and declared_hash != expected_hash:
            raise RuntimeError(f"{scene_id}: TTS source text hash mismatch.")
        if source.get("source_text_sha256") != expected_hash:
            raise RuntimeError(f"{scene_id}: source scene text hash mismatch.")
        if source["role"] == "main":
            by_part.setdefault(source["part"], []).append((source["source_scene_number"], narration_text))
        else:
            if record.get("voiceover_enabled") is not True or record.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{scene_id}: extra-role TTS metadata is invalid.")

    for part, values in by_part.items():
        values.sort(key=lambda item: item[0])
        source_part = next(
            row.get("part_narration") or row.get("locked_part_narration")
            for row in source_scenes
            if row["part"] == part and row["role"] == "main"
            if row.get("part_narration") or row.get("locked_part_narration")
        ) if any(
            row["part"] == part and (row.get("part_narration") or row.get("locked_part_narration"))
            for row in source_scenes
        ) else None
        if source_part and clean(" ".join(item[1] for item in values)) != clean(source_part):
            raise RuntimeError(f"Part {part}: TTS Main Scene text does not reconstruct its locked Part.")

    for source in source_scenes:
        if source["role"] != "main":
            narration_text = clean(source.get("narration"))
            if len(narration_text.split()) < 8 or source.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{source['scene_id']}: extra-role narration/voiceover is missing.")
            if source.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{source['scene_id']}: extra-role narration source is invalid.")
    return narration_records


def verify_image_record(
    source: dict,
    visual_job: dict,
    image_record: dict,
    generation_record: dict,
    format_name: str,
) -> tuple[Path, dict]:
    scene_id = source["scene_id"]

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
    validate_image_dimensions(info, format_name, scene_id)

    expected_hash = clean(
        image_record.get("image_sha256")
    ).lower()

    generation_hash = clean(
        generation_record.get("image_sha256")
    ).lower()

    if info["sha256"] != expected_hash:
        raise RuntimeError(
            f"{scene_id}: image file SHA256 does not match image_manifest."
        )

    if info["sha256"] != generation_hash:
        raise RuntimeError(
            f"{scene_id}: image file SHA256 does not match "
            "image_generation_manifest."
        )

    job_hash = clean(
        visual_job.get("image_sha256")
        or visual_job.get("sha256")
    ).lower()

    if job_hash and job_hash != info["sha256"]:
        raise RuntimeError(
            f"{scene_id}: visual job image SHA256 mismatch."
        )

    prompt_hash = clean(image_record.get("prompt_sha256")).lower()
    generation_prompt_hash = clean(generation_record.get("prompt_sha256")).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", prompt_hash):
        raise RuntimeError(f"{scene_id}: image prompt SHA256 is missing or invalid.")
    if prompt_hash != generation_prompt_hash:
        raise RuntimeError(f"{scene_id}: image/generation prompt SHA256 mismatch.")

    prompt_signature = clean(image_record.get("prompt_signature"))
    generation_prompt_signature = clean(generation_record.get("prompt_signature"))
    job_signature = clean(visual_job.get("prompt_signature"))
    if not prompt_signature or prompt_signature != generation_prompt_signature:
        raise RuntimeError(f"{scene_id}: image/generation prompt signature mismatch.")
    if not job_signature or prompt_signature != job_signature:
        raise RuntimeError(f"{scene_id}: image/visual-job prompt signature mismatch.")

    for field, label in (
        ("source_scene_signature", "source scene"),
        ("source_fingerprint", "source fingerprint"),
        ("config_signature", "config signature"),
    ):
        expected = clean(visual_job.get(field))
        image_value = clean(image_record.get(field))
        generation_value = clean(generation_record.get(field))
        if not expected or image_value != expected or generation_value != expected:
            raise RuntimeError(f"{scene_id}: {label} provenance mismatch.")

    return image_path, info


def prepare_jobs(
    source_scenes: list[dict],
    jobs_by_id: dict[str, dict],
    image_by_id: dict[str, dict],
    generation_by_id: dict[str, dict],
    narration_by_id: dict[str, dict],
    bible: dict,
    character_map: dict[str, dict],
    format_name: str,
    part_numbers: list[int],
    scene_counts: dict[int, int],
    scenes_min: int,
    scenes_max: int,
) -> tuple[list[dict], dict]:
    jobs = []
    role_counts = {role: 0 for role in ROLE_ORDER}
    part_role_counts = {
        part: {role: 0 for role in ROLE_ORDER}
        for part in part_numbers
    }
    part_slots = {part: 0 for part in part_numbers}

    for global_number, source in enumerate(source_scenes, 1):
        scene_id = source["scene_id"]
        part = source["part"]
        role = source["role"]

        visual_job = jobs_by_id[scene_id]
        image_record = image_by_id[scene_id]
        generation_record = generation_by_id[scene_id]
        narration_row = narration_by_id.get(scene_id)

        image_path, image_info = verify_image_record(
            source,
            visual_job,
            image_record,
            generation_record,
            format_name,
        )

        if role == "main":
            if narration_row is None:
                raise RuntimeError(f"{scene_id}: Main TTS record is missing.")
            measured_audio = measured_audio_record(scene_id, narration_row, source)
            narration = clean(
                narration_row.get("narration")
                or narration_row.get("text")
                or source.get("narration")
            )
            duration = measured_audio["duration"]
            audio_fields = {
                "duration_source": "ffprobe_measured",
                "audio_path": measured_audio["audio_path"],
                "audio_sha256": measured_audio["audio_sha256"],
                "audio_duration": duration,
                "actual_audio_duration_seconds": duration,
                "audio_duration_source": "ffprobe_measured",
                "audio_provider": measured_audio["audio_provider"],
                "audio_model": measured_audio["audio_model"],
            }
        else:
            if narration_row is None:
                raise RuntimeError(f"{scene_id}: extra-role TTS record is missing.")
            measured_audio = measured_audio_record(scene_id, narration_row, source)
            narration = clean(narration_row.get("narration") or narration_row.get("text"))
            if narration != clean(source.get("narration")):
                raise RuntimeError(f"{scene_id}: extra-role TTS text differs from source narration.")
            duration = measured_audio["duration"]
            audio_fields = {
                "duration_source": "ffprobe_measured",
                "audio_path": measured_audio["audio_path"],
                "audio_sha256": measured_audio["audio_sha256"],
                "audio_duration": duration,
                "actual_audio_duration_seconds": duration,
                "audio_duration_source": "ffprobe_measured",
                "audio_provider": measured_audio["audio_provider"],
                "audio_model": measured_audio["audio_model"],
                "audio_model": None,
            }

        prompt = clean(
            visual_job.get("prompt")
            or visual_job.get("image_prompt")
            or visual_job.get("visual_prompt")
            or source.get("visual_prompt")
            or source.get("visual")
        )

        if len(prompt) < 24:
            raise RuntimeError(
                f"{scene_id}: visual prompt is missing or too short."
            )

        part_slots[part] += 1
        local_slot = part_slots[part]

        character_data = character_context(
            source,
            character_map,
            bible,
        )

        relative_image = image_path.relative_to(ROOT).as_posix()

        row = {
            "global_scene": global_number,
            "global_scene_number": global_number,
            "scene_index": global_number,

            "part": part,
            "scene": local_slot,
            "local_scene": local_slot,

            "source_scene_number": (
                source["source_scene_number"]
                if role == "main"
                else None
            ),
            "source_local_scene": (
                source["source_scene_number"]
                if role == "main"
                else None
            ),
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
            "story_anchor": clean(source.get("story_anchor")),
            "voiceover_enabled": True,
            "narration_source": "locked_part_text" if role == "main" else "ai_generated_role_narration",
            "visual_only": False,
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
            ),

            "duration": duration,
            "scene_duration": duration,
            **audio_fields,
            "voiceover_enabled": True,
            "narration_source": "locked_part_text" if role == "main" else "ai_generated_role_narration",
            "source_text_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
            "source_word_count": len(narration.split()),

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

    expected_main = sum(scene_counts[part] for part in part_numbers)

    if role_counts["main"] != expected_main:
        raise RuntimeError(
            f"Main scene count mismatch: expected {expected_main}, "
            f"found {role_counts['main']}."
        )

    for position, part in enumerate(part_numbers):
        counts = part_role_counts[part]

        if counts["hook"] != 1 or counts["main"] != scene_counts[part]:
            raise RuntimeError(
                f"Part {part}: Hook/Main scene counts are invalid."
            )

        if position < len(part_numbers) - 1:
            if counts["suspense"] != 1 or counts["resolution"] != 0:
                raise RuntimeError(
                    f"Part {part}: Suspense/Resolution counts are invalid."
                )
        else:
            if counts["resolution"] != 1 or counts["suspense"] != 0:
                raise RuntimeError(
                    f"Final part {part}: Resolution/Suspense counts are invalid."
                )

    if len({row["scene_id"] for row in jobs}) != len(jobs):
        raise RuntimeError("Prepared Photo Motion jobs contain duplicate IDs.")

    if [row["global_scene"] for row in jobs] != list(
        range(1, len(jobs) + 1)
    ):
        raise RuntimeError(
            "Photo Motion global scene numbering is not continuous."
        )

    audio_hashes = {
        row["scene_id"]: row["audio_sha256"]
        for row in jobs
    }

    image_hashes = {
        row["scene_id"]: row["image_sha256"]
        for row in jobs
    }

    settings = {
        "version": VERSION,
        "format": format_name,
        "part_numbers": part_numbers,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
        "source_scene_ids": [
            row["scene_id"] for row in source_scenes
        ],
        "image_hashes": image_hashes,
        "audio_hashes": audio_hashes,
        "character_bible_signature": stable_hash({
            "status": bible.get("status"),
            "characters": bible.get("characters", []),
            "scene_character_map": bible.get("scene_character_map", []),
            "world": bible.get("world", {}),
        }),
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
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
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
            "Every role uses ffprobe-measured TTS audio duration."
        ),
        "measured_audio_count": len(jobs),
        "visual_only_scene_count": 0,
        "pending_audio_measurement_count": 0,
        "config_signature": config_signature,
        "character_bible_status": bible.get("status", "unknown"),
        "jobs": jobs,
    }

    return jobs, document


def make_checkpoint(
    jobs: list[dict],
    config_signature: str,
    previous: Any,
    resume_enabled: bool,
    skip_completed: bool,
) -> dict:
    old_rows = []

    if (
        resume_enabled
        and isinstance(previous, dict)
        and previous.get("config_signature") == config_signature
        and isinstance(previous.get("scenes"), list)
    ):
        old_rows = previous["scenes"]

    old_by_id = {
        clean(row.get("scene_id")): row
        for row in old_rows
        if isinstance(row, dict) and clean(row.get("scene_id"))
    }

    rows = []

    for job in jobs:
        old = old_by_id.get(job["scene_id"], {})

        reusable = (
            skip_completed
            and old.get("status") == "prepared"
            and old.get("job_signature") == config_signature
            and old.get("image_sha256") == job["image_sha256"]
            and old.get("audio_sha256") == job["audio_sha256"]
            and old.get("image_path") == job["image_path"]
            and old.get("audio_path") == job["audio_path"]
        )

        rows.append({
            "scene_id": job["scene_id"],
            "part": job["part"],
            "local_scene": job["local_scene"],
            "source_scene_number": job["source_scene_number"],
            "role": job["role"],
            "image_path": job["image_path"],
            "image_sha256": job["image_sha256"],
            "audio_path": job["audio_path"],
            "audio_sha256": job["audio_sha256"],
            "audio_duration": job["audio_duration"],
            "job_signature": config_signature,
            "status": "prepared",
            "reused_checkpoint": bool(reusable),
        })

    return {
        "version": VERSION,
        "status": "completed",
        "config_signature": config_signature,
        "total_jobs": len(jobs),
        "prepared": len(rows),
        "pending": 0,
        "scenes": rows,
    }


def verify_written_outputs(
    jobs_document: dict,
    manifest: dict,
    checkpoint: dict,
    expected_count: int,
) -> None:
    if jobs_document.get("status") != "completed":
        raise RuntimeError(
            "Photo Motion jobs read-after-write status verification failed."
        )

    written_jobs = jobs_document.get("jobs")

    if not isinstance(written_jobs, list):
        raise RuntimeError(
            "Photo Motion jobs read-after-write list is invalid."
        )

    if len(written_jobs) != expected_count:
        raise RuntimeError(
            "Photo Motion jobs read-after-write count mismatch."
        )

    if manifest.get("status") != "completed":
        raise RuntimeError(
            "Photo Motion manifest read-after-write status mismatch."
        )

    if manifest.get("total_jobs") != expected_count:
        raise RuntimeError(
            "Photo Motion manifest read-after-write count mismatch."
        )

    if checkpoint.get("status") != "completed":
        raise RuntimeError(
            "Photo Motion checkpoint read-after-write status mismatch."
        )

    if checkpoint.get("prepared") != expected_count:
        raise RuntimeError(
            "Photo Motion checkpoint read-after-write count mismatch."
        )

    for row in written_jobs:
        if not isinstance(row, dict):
            raise RuntimeError(
                "Photo Motion jobs contain an invalid written record."
            )

        if not clean(row.get("scene_id")):
            raise RuntimeError(
                "Photo Motion job has no scene ID after writing."
            )

        if row.get("role") == "main":
            if valid_duration(row.get("actual_audio_duration_seconds")) is None:
                raise RuntimeError(f"{row.get('scene_id')}: Main TTS duration is missing.")
            if not clean(row.get("audio_sha256")):
                raise RuntimeError(f"{row.get('scene_id')}: Main TTS SHA256 is missing.")
        else:
            if valid_duration(row.get("actual_audio_duration_seconds")) is None or not clean(row.get("audio_sha256")):
                raise RuntimeError(f"{row.get('scene_id')}: extra-role measured TTS audio is missing.")

        if not clean(row.get("image_sha256")):
            raise RuntimeError(
                f"{row.get('scene_id')}: written job has no image SHA256."
            )


def resolve_actual_scene_counts(
    source_document: dict,
    parts: int,
    scenes_min: int,
    scenes_max: int,
) -> dict[int, int]:
    """Read canonical generate_scenes metadata, with safe legacy fallbacks."""
    if not isinstance(source_document, dict):
        raise RuntimeError("scenes.json root must be an object.")

    raw_counts = source_document.get("actual_scene_counts")
    counts: dict[int, int] = {}

    if isinstance(raw_counts, list) and len(raw_counts) == parts:
        counts = {
            part: positive_int(raw_counts[part - 1], f"Part {part} actual Scene count")
            for part in range(1, parts + 1)
        }
    else:
        role_counts = source_document.get("part_role_counts")
        if isinstance(role_counts, dict):
            for part in range(1, parts + 1):
                row = role_counts.get(str(part), role_counts.get(part))
                if not isinstance(row, dict):
                    raise RuntimeError(f"scenes.json part_role_counts is missing Part {part}.")
                counts[part] = positive_int(
                    row.get("main"),
                    f"Part {part} Main Scene count",
                )
        else:
            rows = source_document.get("scenes")
            if not isinstance(rows, list) or not rows:
                raise RuntimeError(
                    "scenes.json has no actual_scene_counts, part_role_counts, or scene rows."
                )
            counts = {part: 0 for part in range(1, parts + 1)}
            for index, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    raise RuntimeError(f"scenes.json scene row #{index} is invalid.")
                if normalize_role(row) != "main":
                    continue
                part = positive_int(row.get("part"), f"Scene row #{index} Part")
                if part not in counts:
                    raise RuntimeError(f"Scene row #{index} uses unselected Part {part}.")
                counts[part] += 1
            if any(value < 1 for value in counts.values()):
                raise RuntimeError("scenes.json does not contain Main scenes for every selected Part.")

    if set(counts) != set(range(1, parts + 1)):
        raise RuntimeError("scenes.json actual Scene counts do not match selected PARTS.")
    if any(not scenes_min <= count <= scenes_max for count in counts.values()):
        raise RuntimeError(
            f"scenes.json actual Scene counts must stay within {scenes_min}-{scenes_max}."
        )

    declared_min = source_document.get("scenes_min")
    declared_max = source_document.get("scenes_max")
    if declared_min is not None and int(declared_min) != scenes_min:
        raise RuntimeError("scenes.json SCENES minimum does not match Input/topic.txt.")
    if declared_max is not None and int(declared_max) != scenes_max:
        raise RuntimeError("scenes.json SCENES maximum does not match Input/topic.txt.")

    return counts


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — PREPARING PHOTO MOTION VISUALS")
    log("Version:", VERSION)
    log("=" * 64)

    config = load_and_validate()

    if not isinstance(config, dict):
        raise RuntimeError("Input configuration is not a valid dictionary.")

    format_name = clean(get_format(config)).lower()

    if format_name not in IMAGE_SIZES:
        raise RuntimeError(
            f"Unsupported output format: {format_name!r}."
        )

    parts = positive_int(get_parts(config), "PARTS")
    scenes_min, scenes_max = get_scenes_range(config)

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
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("RESUME ENABLED:", resume_enabled)
    log("SKIP COMPLETED:", skip_completed)

    source_document = read_json(SCENES_FILE, required=True)

    scene_counts = resolve_actual_scene_counts(
        source_document,
        parts,
        scenes_min,
        scenes_max,
    )
    source_scenes, part_numbers = normalize_source_scenes(
        source_document, parts, scene_counts, scenes_min, scenes_max,
    )

    expected_ids = {row["scene_id"] for row in source_scenes}

    visual_document = read_json(VISUAL_JOBS_FILE, required=True)
    visual_rows, visual_metadata = extract_rows(
        visual_document,
        "visual_jobs.json",
    )

    visual_manifest = read_json(
        VISUAL_JOBS_MANIFEST,
        required=True,
    )
    validate_manifest_status(
        visual_manifest,
        "visual_jobs_manifest.json",
    )

    jobs_by_id = verify_visual_jobs(
        source_scenes,
        visual_rows,
    )
    # generate_visuals.py writes visual_jobs.json as a list; its provenance
    # metadata is authoritative in visual_jobs_manifest.json.
    visual_source_fingerprint = clean(visual_manifest.get("source_fingerprint"))
    visual_config_signature = clean(visual_manifest.get("config_signature"))
    if not visual_source_fingerprint or not visual_config_signature:
        raise RuntimeError("visual_jobs_manifest.json is missing source/config fingerprints.")
    if isinstance(visual_metadata, dict) and visual_metadata:
        if (
            clean(visual_metadata.get("source_fingerprint")) != visual_source_fingerprint
            or clean(visual_metadata.get("config_signature")) != visual_config_signature
        ):
            raise RuntimeError("visual_jobs.json metadata and its manifest fingerprints disagree.")

    if isinstance(visual_metadata, dict):
        declared_format = clean(
            visual_metadata.get("format")
        ).lower()

        if declared_format and declared_format != format_name:
            raise RuntimeError(
                "visual_jobs.json format does not match Input configuration."
            )

    visual_manifest_format = clean(
        visual_manifest.get("format")
    ).lower()

    if visual_manifest_format and visual_manifest_format != format_name:
        raise RuntimeError(
            "visual_jobs_manifest format does not match Input configuration."
        )

    image_document = read_json(IMAGE_MANIFEST, required=True)
    generation_document = read_json(
        GENERATION_MANIFEST,
        required=True,
    )

    validate_manifest_status(
        image_document,
        "image_manifest.json",
    )
    validate_manifest_status(
        generation_document,
        "image_generation_manifest.json",
    )

    validate_manifest_count(
        image_document,
        len(source_scenes),
        "image_manifest.json",
    )
    validate_manifest_count(
        generation_document,
        len(source_scenes),
        "image_generation_manifest.json",
    )

    for label, document in (
        ("image_manifest.json", image_document),
        ("image_generation_manifest.json", generation_document),
    ):
        declared_format = clean(document.get("format")).lower()

        if declared_format and declared_format != format_name:
            raise RuntimeError(
                f"{label}: format does not match Input configuration."
            )

    for label, manifest_document in (
        ("image_manifest.json", image_document),
        ("image_generation_manifest.json", generation_document),
    ):
        if clean(manifest_document.get("source_fingerprint")) != visual_source_fingerprint:
            raise RuntimeError(f"{label}: source fingerprint mismatch.")
        if clean(manifest_document.get("config_signature")) != visual_config_signature:
            raise RuntimeError(f"{label}: config signature mismatch.")
    image_by_id = validate_image_manifests(
        image_document,
        generation_document,
        expected_ids,
    )

    generation_by_id = index_by_scene_id(
        manifest_rows(
            generation_document,
            "image_generation_manifest",
        ),
        "image_generation_manifest",
    )

    bible = validate_character_bible(config)
    character_map = validate_character_mapping(
        source_scenes,
        bible,
    )

    narration_document = read_json(
        NARRATION_FILE,
        required=True,
    )
    narration_records = narration_map(narration_document)

    narration_by_id = validate_narration_mapping(
        source_scenes,
        narration_records,
    )

    jobs, document = prepare_jobs(
        source_scenes=source_scenes,
        jobs_by_id=jobs_by_id,
        image_by_id=image_by_id,
        generation_by_id=generation_by_id,
        narration_by_id=narration_by_id,
        bible=bible,
        character_map=character_map,
        format_name=format_name,
        part_numbers=part_numbers,
        scene_counts=scene_counts,
        scenes_min=scenes_min,
        scenes_max=scenes_max,
    )

    VISUALS.mkdir(parents=True, exist_ok=True)
    CHECKPOINTS.mkdir(parents=True, exist_ok=True)

    previous = (
        read_json(CHECKPOINT_FILE)
        if resume_enabled
        else None
    )

    checkpoint = make_checkpoint(
        jobs=jobs,
        config_signature=document["config_signature"],
        previous=previous,
        resume_enabled=resume_enabled,
        skip_completed=skip_completed,
    )

    manifest = {
        "version": VERSION,
        "status": "completed",
        "format": format_name,
        "parts": len(part_numbers),
        "part_numbers": part_numbers,
        "total_parts": len(part_numbers),
"scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
        "total_jobs": len(jobs),
        "config_signature": document["config_signature"],
        "visual_jobs_signature": stable_hash(visual_metadata),
        "visual_jobs_manifest_signature": stable_hash(visual_manifest),
        "image_manifest_signature": stable_hash(image_document),
        "generation_manifest_signature": stable_hash(generation_document),
        "narration_signature": clean(
            narration_document.get("source_signature")
            or narration_document.get("generation_signature")
        ) or stable_hash(narration_records),
        "character_bible_signature": stable_hash({
            "characters": bible.get("characters", []),
            "scene_character_map": bible.get("scene_character_map", []),
            "world": bible.get("world", {}),
        }),
        "scene_ids": [row["scene_id"] for row in jobs],
        "measured_audio_count": len(jobs),
        "pending_audio_measurement_count": 0,
        "scene_role_counts": document["scene_role_counts"],
        "status_detail": "all_images_and_all_role_tts_audio_verified",
    }

    if len(jobs) != len(source_scenes):
        raise RuntimeError(
            "Prepared Photo Motion job count differs from source scenes."
        )

    for row in jobs:
        if not row["image_sha256"]:
            raise RuntimeError(f"{row['scene_id']}: image integrity data is missing.")
        if row["role"] == "main":
            if not row["audio_sha256"] or valid_duration(row["actual_audio_duration_seconds"]) is None:
                raise RuntimeError(f"{row['scene_id']}: measured Main TTS audio is invalid.")
        else:
            if not row["audio_sha256"] or valid_duration(row["actual_audio_duration_seconds"]) is None:
                raise RuntimeError(f"{row['scene_id']}: extra-role TTS audio is missing or unmeasured.")
            if row["voiceover_enabled"] is not True or not row["narration"]:
                raise RuntimeError(f"{row['scene_id']}: extra-role narration/voiceover is missing.")

    atomic_json(PHOTO_JOBS_FILE, document)
    atomic_json(PHOTO_MANIFEST_FILE, manifest)
    atomic_json(CHECKPOINT_FILE, checkpoint)

    written_jobs = read_json(PHOTO_JOBS_FILE, required=True)
    written_manifest = read_json(PHOTO_MANIFEST_FILE, required=True)
    written_checkpoint = read_json(CHECKPOINT_FILE, required=True)

    verify_written_outputs(
        written_jobs,
        written_manifest,
        written_checkpoint,
        len(jobs),
    )

    log("-" * 64)
    log("PHOTO MOTION JOBS PREPARED")
    log("Format:", format_name)
    log("Parts:", part_numbers)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("Total main scenes:", document["total_main_scenes"])
    log("Total extra scenes:", document["total_extra_scenes"])
    log("Total jobs:", len(jobs))

    for role in ("hook", "main", "suspense", "resolution"):
        log(role.upper() + ":", document["scene_role_counts"][role])

    log("Measured TTS audio for all roles:", document["measured_audio_count"])
    log("Visual-only Scenes without TTS:", 0)
    log("Pending TTS measurements: 0")
    log("Photo Motion jobs:", PHOTO_JOBS_FILE.relative_to(ROOT))
    log("Photo Motion manifest:", PHOTO_MANIFEST_FILE.relative_to(ROOT))
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