#!/usr/bin/env python3
"""Prepare narration jobs for variable Scene counts and separate story roles."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    get_format,
    get_parts_range,
    get_scenes_range,
    get_selected_parts,
    get_topic,
    get_story_text,
    load_and_validate,
)

SCENES_FILE = ROOT / "output/scenes/scenes.json"
AI_STORY_FILE = ROOT / "output/story/ai_story.json"
OUTPUT_FILE = ROOT / "output/narration/narration.json"
CHECKPOINT_FILE = ROOT / "output/checkpoints/narration_progress.json"
TTS_MANIFEST_FILE = ROOT / "output/narration/audio_jobs.json"

SCRIPT_VERSION = 10
MIN_DURATION = 0.5
MAX_DURATION = 3600.0
MIN_AUDIO_BYTES = 1000
MAX_PARTS = 1000
MAX_SCENES_PER_PART = 500
MAX_TOTAL_SCENES = 1000
WORDS_PER_MINUTE = {"kids": 115, "adult": 145}

ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}
ROLE_ALIASES = {
    "opening": "hook",
    "intro": "hook",
    "part_hook": "hook",
    "hook_scene": "hook",
    "main_scene": "main",
    "story": "main",
    "cliffhanger": "suspense",
    "ending": "resolution",
    "final": "resolution",
    "final_resolution": "resolution",
}

CLOSING_QUOTES = "\"'”’)]}»"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(*values: Any) -> None:
    print(*values, flush=True)


def stable_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file not found: {path.relative_to(ROOT)}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


def save_json_atomic(path: Path, value: Any) -> None:
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


def cfg_text(config: dict, key: str, default: str = "") -> str:
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    raise RuntimeError(f"{key} must be a valid boolean.")


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be a positive integer.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"{label} must be a positive integer.") from exc
    if number < 1:
        raise RuntimeError(f"{label} must be at least 1.")
    return number


def finite_duration(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be numeric.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"{label} must be numeric.") from exc
    if not math.isfinite(number) or not MIN_DURATION <= number <= MAX_DURATION:
        raise RuntimeError(
            f"{label} must be between {MIN_DURATION} and {MAX_DURATION} seconds."
        )
    return round(number, 3)


def count_words(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value, flags=re.UNICODE))


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.replace("\ufeff", "")).strip()


def clean_narration(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{label}: narration must be text.")
    value = (
        value.replace("\ufeff", "")
        .replace("\x00", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        raise RuntimeError(f"{label}: narration is empty.")
    if re.search(r"(?:^|\s)([\w'-]+)[.!?।॥]?\s+\1[.!?।॥]?$", value, re.I):
        raise RuntimeError(
            f"{label}: repeated final word detected; source narration "
            "must be corrected rather than silently rewritten."
        )
    return value


def sentence_units(value: str) -> list[str]:
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return []

    abbreviations = {
        "श्री.", "डॉ.", "प्रो.", "Mr.", "Mrs.", "Dr.",
        "जैसे.", "etc.", "e.g.", "i.e.", "vs.",
    }
    pattern = re.compile(r"""[.!?।॥]+["'”’)\]}»]*(?=\s|$)""")
    units: list[str] = []
    start = 0

    for match in pattern.finditer(value):
        candidate = value[start:match.end()].strip()
        if not candidate:
            start = match.end()
            continue
        if candidate.split()[-1] in abbreviations:
            continue
        units.append(candidate)
        start = match.end()

    remainder = value[start:].strip()
    if remainder:
        units.append(remainder)
    return units or [value]


def clause_units(value: str) -> list[str]:
    result: list[str] = []
    for sentence in sentence_units(value):
        start = 0
        for match in re.finditer(r"[,;:，；：](?:\s+|$)", sentence):
            unit = sentence[start:match.end()].strip()
            if unit:
                result.append(unit)
            start = match.end()
        tail = sentence[start:].strip()
        if tail:
            result.append(tail)
    return result or [value.strip()]


def validate_sentence_integrity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: narration is empty.")
    if "\x00" in value:
        raise RuntimeError(f"{label}: narration contains null characters.")
    units = sentence_units(value)
    normalized = re.sub(r"\s+", " ", value).strip()
    if not units or " ".join(units).strip() != normalized:
        raise RuntimeError(f"{label}: sentence segmentation changed source text.")


def estimate_speech_duration(value: str, audience: str, speed: str) -> float:
    """Estimate only; never represent this value as measured audio duration."""
    words = count_words(value)
    base_wpm = WORDS_PER_MINUTE.get(audience, 145)
    try:
        speed_percent = int(str(speed).replace("%", "").strip())
    except (TypeError, ValueError):
        speed_percent = 0
    rate = max(40.0, base_wpm * (1.0 + speed_percent / 100.0))
    sentence_pauses = len(re.findall(r"[.!?।॥]+", value))
    clause_pauses = len(re.findall(r"[,;:]", value))
    seconds = (words / rate) * 60.0 + sentence_pauses * 0.22 + clause_pauses * 0.08
    return round(max(MIN_DURATION, seconds), 3)


def read_scene_duration(scene: dict, policy: str, label: str) -> float | str:
    for field in ("duration", "scene_duration", "duration_seconds", "seconds"):
        value = scene.get(field)
        if value is None or str(value).strip() == "":
            continue
        if str(value).strip().lower() == "auto":
            return "auto"
        return finite_duration(value, f"{label} {field}")
    if policy == "fixed":
        raise RuntimeError(f"{label}: fixed SCENE_DURATION requires source duration.")
    return "auto"


def normalize_role(scene: dict) -> str:
    raw = str(
        scene.get("role") or scene.get("scene_type") or scene.get("type") or "main"
    ).strip().lower()
    role = ROLE_ALIASES.get(raw, raw)
    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported narration role: {raw!r}.")
    return role


def build_settings(config: dict) -> dict:
    mode = str(get_format(config)).strip().lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode!r}")

    parts_min, parts_max = get_parts_range(config)
    scenes_min, scenes_max = get_scenes_range(config)
    selected_parts = get_selected_parts(config, required=True)

    if selected_parts is None:
        raise RuntimeError("Actual PARTS selection is not available.")
    selected_parts = positive_int(selected_parts, "selected PARTS")

    if not parts_min <= selected_parts <= parts_max:
        raise RuntimeError("Selected PARTS is outside configured PARTS range.")
    if mode == "full" and selected_parts != 1:
        raise RuntimeError("FULL format must contain exactly one Part.")
    if not 1 <= scenes_min <= scenes_max <= MAX_SCENES_PER_PART:
        raise RuntimeError("Configured SCENES range is invalid.")

    audience = cfg_text(config, "AUDIENCE", "adult").lower()
    voice = cfg_text(config, "VOICE", "male").lower()
    speed = cfg_text(config, "SPEED", "+0%")
    captions = cfg_text(config, "CAPTIONS", "hindi").lower()
    duration_policy = cfg_text(config, "SCENE_DURATION", "auto").lower()

    if audience not in {"adult", "kids"}:
        raise RuntimeError("AUDIENCE must be adult or kids.")
    if voice not in {"male", "female"}:
        raise RuntimeError("VOICE must be male or female.")
    if not re.fullmatch(r"[+-]?\d{1,2}%", speed):
        raise RuntimeError("SPEED must be a percentage such as +0%.")
    if not -50 <= int(speed.replace("%", "")) <= 50:
        raise RuntimeError("SPEED must be between -50% and +50%.")
    if captions not in {"hindi", "english", "hinglish"}:
        raise RuntimeError("CAPTIONS must be hindi, english or hinglish.")
    if duration_policy not in {"auto", "fixed"}:
        raise RuntimeError("SCENE_DURATION must be auto or fixed.")

    return {
        "format": mode,
        "parts": selected_parts,
        "parts_min": parts_min,
        "parts_max": parts_max,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "audience": audience,
        "voice": voice,
        "speed": speed,
        "captions": captions,
        "scene_duration": duration_policy,
        "music": cfg_bool(config, "MUSIC", True),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),
    }


def resolve_title(config: dict, story: dict) -> str:
    for field in ("title", "generated_title", "story_title", "final_title"):
        value = story.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    explicit = cfg_text(config, "TITLE")
    if explicit:
        return explicit
    topic = get_topic(config)
    if topic:
        return topic
    if get_story_text(config).strip():
        raise RuntimeError("ai_story.json has no full-story title.")
    raise RuntimeError("Story title is missing.")


def canonical_main_id(part: int, scene: int) -> str:
    return f"part_{part:02d}_scene_{scene:02d}"


def canonical_extra_id(part: int, role: str) -> str:
    return f"part_{part:02d}_{role}"


def load_story_metadata() -> dict:
    value = load_json(AI_STORY_FILE, required=False)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("ai_story.json must contain a JSON object.")
    return value


def validate_ai_story(story: dict, settings: dict) -> list[int] | None:
    if not story:
        return None
    if story.get("status") != "completed":
        raise RuntimeError("ai_story.json is not completed.")
    if str(story.get("format", "")).lower() != settings["format"]:
        raise RuntimeError("ai_story.json FORMAT does not match Input.")

    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != settings["parts"]:
        raise RuntimeError("ai_story.json actual PARTS count mismatch.")

    counts = []
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"ai_story.json Part {part_number} numbering mismatch.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"ai_story.json Part {part_number} has no Main Scenes.")
        count = len(scenes)
        if not settings["scenes_min"] <= count <= settings["scenes_max"]:
            raise RuntimeError(
                f"Part {part_number} has {count} Scenes; configured range is "
                f"{settings['scenes_min']}-{settings['scenes_max']}."
            )
        counts.append(count)

        for scene_number, scene in enumerate(scenes, 1):
            expected_id = canonical_main_id(part_number, scene_number)
            if scene.get("scene") != scene_number or scene.get("scene_id") != expected_id:
                raise RuntimeError(f"ai_story.json Scene ID/order mismatch: {expected_id}.")
            if normalize_role(scene) != "main":
                raise RuntimeError(f"{expected_id} must have role main.")
            validate_sentence_integrity(
                clean_narration(scene.get("narration"), expected_id), expected_id
            )

        final = part_number == settings["parts"]
        required = {
            "hook_scene": "hook",
            "suspense_scene": "suspense",
            "resolution_scene": "resolution",
        }
        for field, role in required.items():
            record = part.get(field)
            should_exist = role == "hook" or (role == "suspense" and not final) or (
                role == "resolution" and final
            )
            if should_exist != isinstance(record, dict):
                raise RuntimeError(f"Part {part_number} {field} presence is invalid.")
            if should_exist:
                if record.get("scene_id") != canonical_extra_id(part_number, role):
                    raise RuntimeError(f"Part {part_number} {role} scene ID mismatch.")
                validate_sentence_integrity(
                    clean_narration(record.get("narration"), record["scene_id"]),
                    record["scene_id"],
                )

    declared = story.get("actual_scene_counts")
    if declared is not None and declared != counts:
        raise RuntimeError("ai_story.json actual_scene_counts mismatch.")
    return counts


def extract_scene_rows(data: Any) -> list[dict]:
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("scenes.json is missing or not completed.")
    rows = data.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json contains no scene records.")
    return rows


def validate_scene_collection(
    data: Any,
    settings: dict,
    ai_story: dict,
) -> tuple[list[dict], list[dict], list[int]]:
    rows = extract_scene_rows(data)
    main_map: dict[tuple[int, int], dict] = {}
    extras: dict[tuple[int, str], dict] = {}
    ids: set[str] = set()

    for index, source in enumerate(rows, 1):
        if not isinstance(source, dict):
            raise RuntimeError(f"Scene record {index} is not an object.")
        role = normalize_role(source)
        part = positive_int(source.get("part"), f"scene {index} part")
        if not 1 <= part <= settings["parts"]:
            raise RuntimeError(f"Scene record {index} refers to unselected Part.")
        item = dict(source)
        item["part"] = part
        item["role"] = role
        item["narration"] = clean_narration(source.get("narration"), f"Part {part} {role}")
        validate_sentence_integrity(item["narration"], item["scene_id"] if item.get("scene_id") else f"Part {part}")

        if role == "main":
            scene_number = positive_int(source.get("scene"), f"scene {index} number")
            expected_id = canonical_main_id(part, scene_number)
            if source.get("scene_id") != expected_id:
                raise RuntimeError(f"Scene ID must be {expected_id}.")
            key = (part, scene_number)
            if key in main_map:
                raise RuntimeError(f"Duplicate Main Scene: {expected_id}.")
            item["scene"] = scene_number
            item["scene_id"] = expected_id
            main_map[key] = item
        else:
            expected_id = canonical_extra_id(part, role)
            if source.get("scene_id") != expected_id:
                raise RuntimeError(f"Role Scene ID must be {expected_id}.")
            key = (part, role)
            if key in extras:
                raise RuntimeError(f"Duplicate role record: {expected_id}.")
            item["scene"] = None
            item["scene_id"] = expected_id
            extras[key] = item

        if item["scene_id"] in ids:
            raise RuntimeError(f"Duplicate scene ID: {item['scene_id']}.")
        ids.add(item["scene_id"])

    ai_counts = validate_ai_story(ai_story, settings)
    actual_counts = []
    for part in range(1, settings["parts"] + 1):
        numbers = sorted(scene for p, scene in main_map if p == part)
        if not numbers or numbers != list(range(1, len(numbers) + 1)):
            raise RuntimeError(f"Part {part} Main Scene numbering is not continuous.")
        count = len(numbers)
        if not settings["scenes_min"] <= count <= settings["scenes_max"]:
            raise RuntimeError(f"Part {part} Scene count {count} is outside configured range.")
        if ai_counts is not None and ai_counts[part - 1] != count:
            raise RuntimeError(f"Part {part} Scene count differs from ai_story.json.")
        actual_counts.append(count)

        required_roles = ["hook", "suspense"] if part < settings["parts"] else ["hook", "resolution"]
        actual_roles = [role for (p, role) in extras if p == part]
        if sorted(actual_roles) != sorted(required_roles):
            raise RuntimeError(
                f"Part {part} requires exactly: {', '.join(required_roles)}."
            )

    expected_keys = {
        (part, scene)
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    }
    if set(main_map) != expected_keys:
        raise RuntimeError("Main Scene collection contains missing or extra Scene IDs.")

    ordered_main = [
        main_map[(part, scene)]
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    ]
    ordered_extras = [
        extras[(part, role)]
        for part in range(1, settings["parts"] + 1)
        for role in (
            ("hook", "suspense") if part < settings["parts"]
            else ("hook", "resolution")
        )
    ]

    declared_counts = data.get("actual_scene_counts")
    if declared_counts is not None and declared_counts != actual_counts:
        raise RuntimeError("scenes.json actual_scene_counts mismatch.")
    return ordered_main, ordered_extras, actual_counts

def source_signature(
    title: str,
    settings: dict,
    main_sources: list[dict],
    extra_sources: list[dict],
    actual_counts: list[int],
) -> str:
    def relevant(rows: list[dict]) -> list[dict]:
        return [
            {
                "part": row["part"],
                "scene": row.get("scene"),
                "scene_id": row["scene_id"],
                "role": row["role"],
                "narration": row["narration"],
                "duration": row.get("duration"),
                "dialogue": row.get("dialogue", ""),
                "character_ids": row.get("character_ids", []),
                "visual_prompt": row.get("visual_prompt", ""),
                "source_scene_signature": row.get("source_scene_signature"),
            }
            for row in rows
        ]

    return stable_hash({
        "script_version": SCRIPT_VERSION,
        "title": title,
        "settings": settings,
        "actual_scene_counts": actual_counts,
        "main": relevant(main_sources),
        "extras": relevant(extra_sources),
    })


def build_narration_record(
    scene: dict,
    settings: dict,
    title: str,
    role: str,
    global_number: int | None = None,
) -> dict:
    part = positive_int(scene.get("part"), "narration part")
    if role == "main":
        scene_number = positive_int(scene.get("scene"), "scene number")
        scene_id = canonical_main_id(part, scene_number)
    else:
        scene_number = None
        scene_id = canonical_extra_id(part, role)

    label = f"Part {part} {role.upper()}"
    narration = clean_narration(scene.get("narration"), label)
    validate_sentence_integrity(narration, label)
    duration = read_scene_duration(scene, settings["scene_duration"], label)
    estimate = estimate_speech_duration(
        narration, settings["audience"], settings["speed"]
    )

    dialogue = scene.get("dialogue", "")
    if not isinstance(dialogue, (str, list, dict)):
        dialogue = str(dialogue)
    character_ids = scene.get("character_ids", [])
    if not isinstance(character_ids, list):
        raise RuntimeError(f"{scene_id}: character_ids must be a list.")

    sentences = sentence_units(narration)
    clauses = clause_units(narration)

    return {
        "part": part,
        "scene": scene_number,
        "scene_id": scene_id,
        "global_scene_number": global_number if role == "main" else None,
        "role": role,
        "scene_type": role,
        "title": str(scene.get("title", "")).strip(),
        "story_title": title,
        "text": narration,
        "narration": narration,
        "sentence_units": sentences,
        "sentence_count": len(sentences),
        "clause_units": clauses,
        "clause_count": len(clauses),
        "word_count": count_words(narration),
        "language": settings["captions"],
        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],
        "format": settings["format"],
        "audience": settings["audience"],
        "duration": duration,
        "scene_duration": duration,
        "narration_duration_estimate_seconds": estimate,
        "estimated_duration": estimate,
        "actual_audio_duration_seconds": None,
        "audio_duration": None,
        "audio_duration_source": "not_measured_tts_pending",
        "audio": None,
        "audio_path": None,
        "audio_file": None,
        "audio_sha256": None,
        "tts_provider": None,
        "tts_model": None,
        "dialogue": dialogue,
        "character_ids": character_ids,
        "status": "completed",
    }


def validate_cached_audio(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("audio_duration_source") != "ffprobe_measured":
        return False

    value = record.get("audio_path") or record.get("audio_file") or record.get("audio")
    if not isinstance(value, str) or not value.strip():
        return False

    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()
        if not path.is_relative_to(ROOT.resolve()):
            return False
        if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
            return False

        declared_hash = record.get("audio_sha256") or record.get("audio_hash")
        if not declared_hash or sha256_file(path) != declared_hash:
            return False

        duration = record.get("actual_audio_duration_seconds")
        if duration is None:
            duration = record.get("audio_duration")
        duration = float(duration)
        if not math.isfinite(duration) or not MIN_DURATION <= duration <= MAX_DURATION:
            return False

        if not record.get("tts_fingerprint"):
            return False
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def load_reusable_records(
    current_signature: str,
    settings: dict,
    enabled: bool,
) -> dict[str, dict]:
    if not enabled:
        return {}

    document = load_json(OUTPUT_FILE, required=False)
    if not isinstance(document, dict):
        return {}
    if document.get("status") not in {"in_progress", "completed"}:
        return {}
    if document.get("source_signature") != current_signature:
        log("Narration cache rejected: source signature changed.")
        return {}
    if document.get("input_config") != settings:
        log("Narration cache rejected: settings changed.")
        return {}

    reusable: dict[str, dict] = {}
    for field in ("scenes", "extra_scenes"):
        rows = document.get(field, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            scene_id = str(row.get("scene_id") or "").strip()
            if (
                scene_id
                and row.get("status") == "completed"
                and isinstance(row.get("text"), str)
                and row["text"].strip()
            ):
                if scene_id in reusable:
                    raise RuntimeError(f"Duplicate cached narration ID: {scene_id}.")
                reusable[scene_id] = dict(row)
    return reusable


def reusable_record_matches(cached: dict, current: dict) -> bool:
    fields = (
        "scene_id", "part", "scene", "role", "text", "voice", "speed",
        "captions", "format", "audience", "duration", "story_title",
        "sentence_units", "clause_units", "word_count",
    )
    return all(cached.get(field) == current.get(field) for field in fields)


def preserve_tts_result(cached: dict, current: dict) -> dict:
    result = dict(current)
    if not validate_cached_audio(cached):
        return result

    fields = (
        "audio", "audio_path", "audio_file", "audio_sha256", "audio_hash",
        "audio_duration", "actual_audio_duration_seconds",
        "audio_duration_source", "tts_provider", "tts_model", "provider",
        "model", "chunks", "chunk_count", "chunk_manifest",
        "tts_fingerprint", "fingerprint", "audio_completed_at", "completed_at",
    )
    for field in fields:
        if field in cached:
            result[field] = cached[field]
    return result


def validate_record_group(
    records: list[dict],
    expected_ids: list[str],
    label: str,
) -> None:
    actual_ids = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise RuntimeError(f"{label} record {index} is invalid.")
        scene_id = str(record.get("scene_id") or "").strip()
        if not scene_id:
            raise RuntimeError(f"{label} record {index} has no scene_id.")
        if scene_id in actual_ids:
            raise RuntimeError(f"{label}: duplicate scene_id {scene_id}.")

        narration = record.get("text")
        if not isinstance(narration, str) or not narration.strip():
            raise RuntimeError(f"{label}: empty narration for {scene_id}.")
        if record.get("status") != "completed":
            raise RuntimeError(f"{label}: incomplete narration {scene_id}.")

        validate_sentence_integrity(narration, scene_id)
        if record.get("word_count") != count_words(narration):
            raise RuntimeError(f"{scene_id}: word_count metadata mismatch.")
        if record.get("sentence_units") != sentence_units(narration):
            raise RuntimeError(f"{scene_id}: sentence segmentation mismatch.")
        if record.get("clause_units") != clause_units(narration):
            raise RuntimeError(f"{scene_id}: clause segmentation mismatch.")

        estimate = record.get("narration_duration_estimate_seconds")
        if (
            isinstance(estimate, bool)
            or not isinstance(estimate, (int, float))
            or not math.isfinite(float(estimate))
            or float(estimate) <= 0
        ):
            raise RuntimeError(f"{scene_id}: narration estimate is invalid.")

        actual = record.get("actual_audio_duration_seconds")
        source = record.get("audio_duration_source")
        if actual is None:
            if source != "not_measured_tts_pending":
                raise RuntimeError(f"{scene_id}: unmeasured audio must remain pending.")
            if record.get("audio_duration") is not None:
                raise RuntimeError(f"{scene_id}: estimate cannot be stored as audio_duration.")
        else:
            if source != "ffprobe_measured":
                raise RuntimeError(f"{scene_id}: actual duration must be ffprobe measured.")
            measured = finite_duration(actual, f"{scene_id} measured audio duration")
            if record.get("audio_duration") != measured:
                raise RuntimeError(f"{scene_id}: audio_duration must equal measured duration.")
            if not validate_cached_audio(record):
                raise RuntimeError(f"{scene_id}: measured audio failed file/SHA validation.")

        actual_ids.append(scene_id)

    if actual_ids != expected_ids:
        raise RuntimeError(
            f"{label} order/count mismatch: expected {len(expected_ids)}, "
            f"received {len(actual_ids)}."
        )


def validate_final_records(
    main_records: list[dict],
    extra_records: list[dict],
    actual_counts: list[int],
    parts: int,
) -> None:
    expected_main = [
        (part, scene)
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    ]
    actual_main = [(int(row["part"]), int(row["scene"])) for row in main_records]
    if actual_main != expected_main:
        raise RuntimeError("Main narration Part/Scene order is incorrect.")

    validate_record_group(
        main_records, [row["scene_id"] for row in main_records], "Main narration"
    )

    expected_extra = []
    for part in range(1, parts + 1):
        expected_extra.append((part, "hook"))
        expected_extra.append((part, "suspense" if part < parts else "resolution"))

    actual_extra = [(int(row["part"]), str(row["role"])) for row in extra_records]
    if actual_extra != expected_extra:
        raise RuntimeError("Hook/Suspense/Resolution mapping or order is incorrect.")

    validate_record_group(
        extra_records, [row["scene_id"] for row in extra_records], "Extra narration"
    )

    all_ids = [row["scene_id"] for row in main_records + extra_records]
    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("Duplicate scene_id across Main and extra narration.")


def make_output_document(
    status: str,
    title: str,
    settings: dict,
    current_signature: str,
    main_records: list[dict],
    extra_records: list[dict],
    actual_counts: list[int],
    started_at: str,
    error: str | None = None,
) -> dict:
    now = utc_now()
    all_records = main_records + extra_records
    total_main = len(main_records)

    document = {
        "status": status,
        "script_version": SCRIPT_VERSION,
        "title": title,
        "topic": title,
        "format": settings["format"],
        "model": "not_required",
        "api_calls": 0,
        "source_file": SCENES_FILE.relative_to(ROOT).as_posix(),
        "source_signature": current_signature,
        "total_parts": settings["parts"],
        "parts_count": settings["parts"],
        "selected_parts": settings["parts"],
        "scenes_min": settings["scenes_min"],
        "scenes_max": settings["scenes_max"],
        "scenes_range": [settings["scenes_min"], settings["scenes_max"]],
        "actual_scene_counts": actual_counts,
        "scenes_per_part": None,
        "total_scenes": total_main,
        "total_main_scenes": total_main,
        "total_extra_scenes": len(extra_records),
        "total_narration_records": len(all_records),
        "completed_scenes": len(main_records),
        "completed_extra_scenes": len(extra_records),
        "input_config": settings,
        "duration_policy": {
            "scene_duration": (
                "configured_fixed_duration"
                if settings["scene_duration"] == "fixed"
                else "source_duration_or_auto"
            ),
            "narration_duration": "estimate_only_until_tts_audio_is_generated",
            "estimate_field": "narration_duration_estimate_seconds",
            "measured_audio_field": "actual_audio_duration_seconds",
            "measured_audio_source": "ffprobe_measured",
            "duration_must_be_measured_by_tts": True,
            "estimated_duration_is_not_actual_audio_duration": True,
            "no_arbitrary_fixed_duration": True,
        },
        "segmentation_policy": {
            "sentence_boundaries": [".", "!", "?", "।", "॥"],
            "clause_boundaries": [",", ";", ":"],
            "punctuation_retained": True,
            "no_arbitrary_sentence_truncation": True,
            "source_narration_preserved": True,
        },
        "role_policy": {
            "hook": "one separate narration record per Part",
            "main": "variable actual Main Scene count within configured range",
            "suspense": "one separate narration per non-final Part",
            "resolution": "one separate narration for final Part only",
        },
        "scenes": main_records,
        "extra_scenes": extra_records,
        "all_scene_ids": [row["scene_id"] for row in all_records],
        "started_at": started_at,
        "updated_at": now,
        "generated_at": now if status == "completed" else None,
        "completed_at": now if status == "completed" else None,
    }
    if error:
        document["last_error"] = str(error)[:2000]
    return document


def save_progress(
    status: str,
    title: str,
    settings: dict,
    current_signature: str,
    main_map: dict,
    extra_map: dict,
    actual_counts: list[int],
    started_at: str,
    expected_main: int,
    expected_extras: int,
    last_scene_id: str | None = None,
    error: str | None = None,
) -> None:
    main_records = [main_map[key] for key in sorted(main_map)]
    extra_records = sorted(
        extra_map.values(),
        key=lambda row: (int(row["part"]), ROLE_ORDER[row["role"]]),
    )
    document = make_output_document(
        status, title, settings, current_signature,
        main_records, extra_records, actual_counts, started_at, error,
    )
    save_json_atomic(OUTPUT_FILE, document)
    save_json_atomic(
        CHECKPOINT_FILE,
        {
            "status": status,
            "script_version": SCRIPT_VERSION,
            "source_signature": current_signature,
            "completed_main": len(main_records),
            "expected_main": expected_main,
            "completed_extras": len(extra_records),
            "expected_extras": expected_extras,
            "last_scene_id": last_scene_id,
            "updated_at": utc_now(),
            "last_error": str(error)[:2000] if error else None,
        },
    )


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — VARIABLE-SCENE NARRATION PREPARATION")
    log("=" * 64)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    settings = build_settings(config)
    parts = settings["parts"]

    resume_enabled = cfg_bool(config, "RESUME_ENABLED", True)
    skip_completed = cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
    save_each_scene = cfg_bool(config, "SAVE_CHECKPOINT_AFTER_EACH_SCENE", True)

    story = load_story_metadata()
    title = resolve_title(config, story)
    source_data = load_json(SCENES_FILE)
    main_sources, extra_sources, actual_counts = validate_scene_collection(
        source_data, settings, story
    )
    expected_main_total = sum(actual_counts)
    if expected_main_total > MAX_TOTAL_SCENES:
        raise RuntimeError("Actual Main Scene total exceeds supported limit.")

    current_signature = source_signature(
        title, settings, main_sources, extra_sources, actual_counts
    )
    reusable = load_reusable_records(
        current_signature, settings, resume_enabled and skip_completed
    )

    started_at = utc_now()
    main_map: dict[tuple[int, int], dict] = {}
    extra_map: dict[str, dict] = {}

    global_number = 0
    ordered_sources = []
    for row in main_sources:
        global_number += 1
        ordered_sources.append((row, "main", global_number))
    for row in extra_sources:
        ordered_sources.append((row, row["role"], None))

    log("FORMAT             :", settings["format"])
    log("TITLE              :", title)
    log("PARTS              :", parts)
    log("SCENES RANGE       :", f'{settings["scenes_min"]}-{settings["scenes_max"]}')
    log("ACTUAL SCENE COUNTS:", actual_counts)
    log("MAIN SCENE COUNT   :", expected_main_total)
    log("EXTRA SCENE COUNT  :", len(extra_sources))
    log("TOTAL RECORDS      :", len(ordered_sources))
    log("VOICE              :", settings["voice"])
    log("SPEED              :", settings["speed"])
    log("CAPTIONS           :", settings["captions"])
    log("RESUME             :", resume_enabled)
    log("SKIP COMPLETED     :", skip_completed)
    log("EXTERNAL API CALLS :", 0)

    try:
        for index, (source, role, global_scene_number) in enumerate(
            ordered_sources, 1
        ):
            current = build_narration_record(
                source, settings, title, role, global_scene_number
            )
            scene_id = current["scene_id"]
            cached = reusable.get(scene_id)

            if (
                resume_enabled
                and skip_completed
                and cached
                and reusable_record_matches(cached, current)
            ):
                current = preserve_tts_result(cached, current)
                action = "REUSED TTS AUDIO" if validate_cached_audio(current) else "REUSED TEXT"
            else:
                action = "PREPARED"

            log(f"[{index}/{len(ordered_sources)}] {action}: {scene_id}")

            if role == "main":
                key = (current["part"], current["scene"])
                if key in main_map:
                    raise RuntimeError(f"Duplicate narration mapping: {key}.")
                main_map[key] = current
            else:
                if scene_id in extra_map:
                    raise RuntimeError(f"Duplicate extra narration ID: {scene_id}.")
                extra_map[scene_id] = current

            if save_each_scene:
                save_progress(
                    "in_progress",
                    title,
                    settings,
                    current_signature,
                    main_map,
                    extra_map,
                    actual_counts,
                    started_at,
                    expected_main_total,
                    len(extra_sources),
                    scene_id,
                )

        final_main = [
            main_map[(part, scene)]
            for part, count in enumerate(actual_counts, 1)
            for scene in range(1, count + 1)
        ]

        final_extras = []
        for part in range(1, parts + 1):
            roles = ("hook", "suspense") if part < parts else ("hook", "resolution")
            for role in roles:
                scene_id = canonical_extra_id(part, role)
                if scene_id not in extra_map:
                    raise RuntimeError(f"Missing narration record: {scene_id}.")
                final_extras.append(extra_map[scene_id])

        validate_final_records(final_main, final_extras, actual_counts, parts)

        output = make_output_document(
            "completed",
            title,
            settings,
            current_signature,
            final_main,
            final_extras,
            actual_counts,
            started_at,
        )
        save_json_atomic(OUTPUT_FILE, output)

        save_json_atomic(
            CHECKPOINT_FILE,
            {
                "status": "completed",
                "script_version": SCRIPT_VERSION,
                "source_signature": current_signature,
                "completed_main": len(final_main),
                "expected_main": expected_main_total,
                "completed_extras": len(final_extras),
                "expected_extras": len(final_extras),
                "total_records": len(final_main) + len(final_extras),
                "actual_scene_counts": actual_counts,
                "updated_at": utc_now(),
            },
        )

        saved = load_json(OUTPUT_FILE)
        if (
            not isinstance(saved, dict)
            or saved.get("status") != "completed"
            or saved.get("source_signature") != current_signature
            or saved.get("actual_scene_counts") != actual_counts
            or len(saved.get("scenes", [])) != expected_main_total
            or len(saved.get("extra_scenes", [])) != len(final_extras)
        ):
            raise RuntimeError("Narration output read-after-write validation failed.")

    except Exception as exc:
        save_progress(
            "failed",
            title,
            settings,
            current_signature,
            main_map,
            extra_map,
            actual_counts,
            started_at,
            expected_main_total,
            len(extra_sources),
            error=exc,
        )
        raise

    all_records = final_main + final_extras
    estimate_total = sum(
        row["narration_duration_estimate_seconds"] for row in all_records
    )
    measured_count = sum(
        row.get("audio_duration_source") == "ffprobe_measured"
        for row in all_records
    )

    log()
    log("=" * 64)
    log("NARRATION PREPARATION COMPLETE")
    log("=" * 64)
    log("FORMAT             :", settings["format"])
    log("PARTS              :", parts)
    log("ACTUAL SCENE COUNTS:", actual_counts)
    log("MAIN SCENES        :", len(final_main))
    log("HOOK/SUSPENSE/END  :", len(final_extras))
    log("TOTAL RECORDS      :", len(all_records))
    log("ESTIMATED SPEECH   :", f"{estimate_total:.1f}s")
    log("MEASURED TTS AUDIO :", measured_count, "/", len(all_records))
    log("EXTERNAL API CALLS :", 0)
    log("OUTPUT             :", OUTPUT_FILE.relative_to(ROOT))
    log("CHECKPOINT         :", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("NARRATION PREPARATION INTERRUPTED")
        raise SystemExit(130)
    except Exception as exc:
        log("NARRATION PREPARATION FAILED:", exc)
        raise SystemExit(1)