#!/usr/bin/env python3
"""Sentence-safe, role-aware narration preparation with verified TTS reuse."""

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
    load_and_validate,
    get_selected_parts,
    get_scenes,
    get_format,
    get_topic,
    get_story_text,
)

SCENES_FILE = ROOT / "output/scenes/scenes.json"
AI_STORY_FILE = ROOT / "output/story/ai_story.json"
OUTPUT_FILE = ROOT / "output/narration/narration.json"
CHECKPOINT_FILE = ROOT / "output/checkpoints/narration_progress.json"
TTS_MANIFEST_FILE = ROOT / "output/narration/audio_jobs.json"

SCRIPT_VERSION = 9
MIN_DURATION = 0.5
MAX_DURATION = 3600.0
MIN_AUDIO_BYTES = 1000
WORDS_PER_MINUTE = {"kids": 115, "adult": 145}

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

ROLE_ALIASES = {
    "opening": "hook",
    "intro": "hook",
    "part_hook": "hook",
    "main_scene": "main",
    "story": "main",
    "cliffhanger": "suspense",
    "ending": "resolution",
    "final": "resolution",
}

CLOSING_QUOTES = "\"'”’)]}»"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(*values: Any) -> None:
    print(*values, flush=True)


def stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, required: bool = True) -> Any:
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file not found: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


def save_json_atomic(path: Path, value: Any) -> None:
    path = Path(path)
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
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{label} must be a positive integer."
        ) from exc

    if number < 1:
        raise RuntimeError(f"{label} must be at least 1.")

    return number


def finite_duration(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be numeric.")

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be numeric.") from exc

    if not math.isfinite(number) or not MIN_DURATION <= number <= MAX_DURATION:
        raise RuntimeError(
            f"{label} must be between {MIN_DURATION} and "
            f"{MAX_DURATION} seconds."
        )

    return round(number, 3)


def count_words(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value, flags=re.UNICODE))


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
    """Split only after complete sentence punctuation; retain punctuation."""
    value = re.sub(r"\s+", " ", value).strip()

    if not value:
        return []

    abbreviations = {
        "श्री.", "डॉ.", "प्रो.", "Mr.", "Mrs.", "Dr.",
        "जैसे.", "etc.", "e.g.", "i.e.", "vs.",
    }

    units: list[str] = []
    start = 0
    pattern = re.compile(r"""[.!?।॥]+["'”’)\]}»]*(?=\s|$)""")

    for match in pattern.finditer(value):
        end = match.end()
        candidate = value[start:end].strip()

        if not candidate:
            start = end
            continue

        last_token = candidate.split()[-1]

        if last_token in abbreviations:
            continue

        units.append(candidate)
        start = end

    remainder = value[start:].strip()

    if remainder:
        units.append(remainder)

    return units or [value]


def clause_units(value: str) -> list[str]:
    """
    Build optional natural pause units without changing narration.
    Commas, semicolons, colons and Hindi clause separators remain attached.
    """
    result: list[str] = []

    for sentence in sentence_units(value):
        start = 0

        for match in re.finditer(r"[,;:，；：](?:\s+|$)", sentence):
            end = match.end()
            unit = sentence[start:end].strip()

            if unit:
                result.append(unit)

            start = end

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

    if not units or any(not unit.strip() for unit in units):
        raise RuntimeError(f"{label}: invalid sentence segmentation.")

    if " ".join(units).strip() != re.sub(r"\s+", " ", value).strip():
        raise RuntimeError(
            f"{label}: sentence segmentation changed source text."
        )


def estimate_speech_duration(
    value: str,
    audience: str,
    speed: str,
) -> float:
    """Estimate only. It is never written as measured audio duration."""
    words = count_words(value)
    base_wpm = WORDS_PER_MINUTE.get(audience, 145)

    try:
        speed_percent = int(str(speed).replace("%", "").strip())
    except (TypeError, ValueError):
        speed_percent = 0

    rate = max(40.0, base_wpm * (1.0 + speed_percent / 100.0))
    sentence_pauses = len(re.findall(r"[.!?।॥]+", value))
    clause_pauses = len(re.findall(r"[,;:]", value))

    seconds = (words / rate) * 60.0
    seconds += sentence_pauses * 0.22
    seconds += clause_pauses * 0.08

    return round(max(MIN_DURATION, seconds), 3)


def read_scene_duration(
    scene: dict,
    duration_policy: str,
    label: str,
) -> float | str:
    for field in (
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds",
    ):
        value = scene.get(field)

        if value is None or str(value).strip() == "":
            continue

        if str(value).strip().lower() == "auto":
            return "auto"

        return finite_duration(value, f"{label} {field}")

    if duration_policy == "fixed":
        raise RuntimeError(
            f"{label}: fixed SCENE_DURATION requires a source duration."
        )

    return "auto"


def normalize_role(scene: dict) -> str:
    raw = str(
        scene.get("role")
        or scene.get("scene_type")
        or scene.get("type")
        or "main"
    ).strip().lower()

    role = ROLE_ALIASES.get(raw, raw)

    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported narration role: {raw!r}.")

    return role


def build_settings(config: dict) -> dict:
    mode = get_format(config)
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

    selected_parts = get_selected_parts(config, required=True)

    return {
        "format": mode,
        "parts": positive_int(selected_parts, "selected PARTS"),
        "scenes_per_part": positive_int(get_scenes(config), "SCENES"),
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
    for field in ("title", "generated_title", "story_title"):
        value = story.get(field)

        if isinstance(value, str) and value.strip():
            return value.strip()

    explicit_title = cfg_text(config, "TITLE")

    if explicit_title:
        return explicit_title

    topic = get_topic(config)

    if topic:
        return topic

    source_story = get_story_text(config).strip()

    if source_story:
        # A complete-story title must come from the story generator.
        # Do not manufacture a title from the opening sentence.
        raise RuntimeError(
            "ai_story.json has no full-story title; narration cannot "
            "use the first sentence as a substitute title."
        )

    raise RuntimeError("Story title is missing.")


def load_story_metadata() -> dict:
    value = load_json(AI_STORY_FILE, required=False)

    if value is None:
        return {}

    if not isinstance(value, dict):
        raise RuntimeError("ai_story.json must contain a JSON object.")

    return value


def extract_scene_rows(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        raise RuntimeError("scenes.json must contain a JSON object.")

    if data.get("status") != "completed":
        raise RuntimeError(
            "Scene generation is incomplete; narration stopped."
        )

    rows = data.get("scenes")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json contains no scene records.")

    return rows


def canonical_main_id(part: int, scene: int) -> str:
    return f"part_{part:02d}_scene_{scene:02d}"


def canonical_extra_id(part: int, role: str) -> str:
    return f"part_{part:02d}_{role}"


def validate_scene_collection(
    data: Any,
    parts: int,
    scenes_per_part: int,
) -> tuple[list[dict], list[dict]]:
    rows = extract_scene_rows(data)

    expected_main = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    main_map: dict[tuple[int, int], dict] = {}
    extras: list[dict] = []
    all_ids: set[str] = set()

    for index, source in enumerate(rows, 1):
        if not isinstance(source, dict):
            raise RuntimeError(f"Scene record {index} is not an object.")

        role = normalize_role(source)
        part = positive_int(source.get("part"), f"scene {index} part")

        if not 1 <= part <= parts:
            raise RuntimeError(
                f"Scene record {index} refers to unselected part {part}."
            )

        label = f"Part {part} {role.upper()}"
        item = dict(source)
        item["role"] = role
        item["part"] = part
        item["narration"] = clean_narration(
            source.get("narration"),
            label,
        )

        validate_sentence_integrity(item["narration"], label)

        if role == "main":
            try:
                scene_number = int(source.get("scene"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{label}: main scene number is missing."
                ) from exc

            key = (part, scene_number)

            if key not in expected_main:
                raise RuntimeError(
                    f"{label}: scene number {scene_number} is outside "
                    "the configured scene grid."
                )

            expected_id = canonical_main_id(part, scene_number)
            actual_id = str(source.get("scene_id") or "").strip()

            if actual_id != expected_id:
                raise RuntimeError(
                    f"{label}: scene_id must be {expected_id}, "
                    f"received {actual_id!r}."
                )

            if key in main_map:
                raise RuntimeError(f"Duplicate main scene mapping: {key}.")

            item["scene"] = scene_number
            item["scene_id"] = expected_id
            main_map[key] = item

        else:
            expected_id = canonical_extra_id(part, role)
            actual_id = str(
                source.get("scene_id") or source.get("id") or ""
            ).strip()

            if actual_id != expected_id:
                raise RuntimeError(
                    f"{label}: scene_id must be {expected_id}, "
                    f"received {actual_id!r}."
                )

            if actual_id in all_ids:
                raise RuntimeError(f"Duplicate extra scene ID: {actual_id}.")

            item["scene"] = None
            item["scene_id"] = expected_id
            extras.append(item)

        scene_id = item["scene_id"]

        if scene_id in all_ids:
            raise RuntimeError(f"Duplicate scene ID: {scene_id}.")

        all_ids.add(scene_id)

    missing = sorted(expected_main - set(main_map))

    if missing:
        raise RuntimeError(
            f"Missing main scene records ({len(missing)}): {missing[:30]}."
        )

    for part in range(1, parts + 1):
        roles = [
            row["role"] for row in extras if row["part"] == part
        ]

        if roles.count("hook") != 1:
            raise RuntimeError(
                f"Part {part} requires exactly one separate Hook."
            )

        if part < parts:
            if roles.count("suspense") != 1:
                raise RuntimeError(
                    f"Part {part} requires exactly one Suspense."
                )

            if "resolution" in roles:
                raise RuntimeError(
                    f"Non-final part {part} cannot contain Resolution."
                )
        else:
            if roles.count("resolution") != 1:
                raise RuntimeError(
                    "Final part requires exactly one Resolution."
                )

            if "suspense" in roles:
                raise RuntimeError(
                    "Final part cannot contain Suspense."
                )

    ordered_main = [
        main_map[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    extras.sort(
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
        )
    )

    return ordered_main, extras


def source_signature(
    title: str,
    settings: dict,
    main_sources: list[dict],
    extra_sources: list[dict],
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
                "source_scene_signature": row.get(
                    "source_scene_signature"
                ),
            }
            for row in rows
        ]

    return stable_hash({
        "script_version": SCRIPT_VERSION,
        "title": title,
        "settings": settings,
        "main": relevant(main_sources),
        "extras": relevant(extra_sources),
    })


def build_narration_record(
    scene: dict,
    settings: dict,
    title: str,
    role: str,
) -> dict:
    part = positive_int(scene.get("part"), "narration part")

    if role == "main":
        scene_number = positive_int(scene.get("scene"), "scene number")
        scene_id = canonical_main_id(part, scene_number)
        global_number = (
            (part - 1) * settings["scenes_per_part"] + scene_number
        )
    else:
        scene_number = None
        scene_id = canonical_extra_id(part, role)
        global_number = None

    label = f"Part {part} {role.upper()}"
    narration = clean_narration(scene.get("narration"), label)
    validate_sentence_integrity(narration, label)

    duration = read_scene_duration(
        scene,
        settings["scene_duration"],
        label,
    )

    estimated = estimate_speech_duration(
        narration,
        settings["audience"],
        settings["speed"],
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
        "global_scene_number": global_number,
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
        "narration_duration_estimate_seconds": estimated,
        "estimated_duration": estimated,
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
    """Reuse only an existing TTS file with matching measured duration and SHA."""
    if not isinstance(record, dict):
        return False

    if record.get("audio_duration_source") != "ffprobe_measured":
        return False

    value = (
        record.get("audio_path")
        or record.get("audio_file")
        or record.get("audio")
    )

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

        declared_hash = (
            record.get("audio_sha256")
            or record.get("audio_hash")
        )

        if not declared_hash or sha256_file(path) != declared_hash:
            return False

        duration = record.get("actual_audio_duration_seconds")

        if duration is None:
            duration = record.get("audio_duration")

        duration = float(duration)

        if not math.isfinite(duration) or duration <= 0:
            return False

        if not record.get("tts_fingerprint"):
            return False

        return True

    except (OSError, TypeError, ValueError):
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
                    raise RuntimeError(
                        f"Duplicate cached narration ID: {scene_id}."
                    )

                reusable[scene_id] = dict(row)

    return reusable


def reusable_record_matches(
    cached: dict,
    current: dict,
) -> bool:
    fields = (
        "scene_id",
        "part",
        "scene",
        "role",
        "text",
        "voice",
        "speed",
        "captions",
        "format",
        "audience",
        "duration",
        "story_title",
        "sentence_units",
        "clause_units",
        "word_count",
    )

    return all(
        cached.get(field) == current.get(field)
        for field in fields
    )


def preserve_tts_result(
    cached: dict,
    current: dict,
) -> dict:
    result = dict(current)

    if not validate_cached_audio(cached):
        return result

    fields = (
        "audio",
        "audio_path",
        "audio_file",
        "audio_sha256",
        "audio_hash",
        "audio_duration",
        "actual_audio_duration_seconds",
        "audio_duration_source",
        "tts_provider",
        "tts_model",
        "provider",
        "model",
        "chunks",
        "chunk_count",
        "chunk_manifest",
        "tts_fingerprint",
        "fingerprint",
        "audio_completed_at",
        "completed_at",
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
    actual_ids: list[str] = []

    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise RuntimeError(
                f"{label} record {index} is invalid."
            )

        scene_id = str(record.get("scene_id") or "").strip()

        if not scene_id:
            raise RuntimeError(
                f"{label} record {index} has no scene_id."
            )

        if scene_id in actual_ids:
            raise RuntimeError(
                f"{label}: duplicate scene_id {scene_id}."
            )

        narration = record.get("text")

        if not isinstance(narration, str) or not narration.strip():
            raise RuntimeError(
                f"{label}: empty narration for {scene_id}."
            )

        if record.get("status") != "completed":
            raise RuntimeError(
                f"{label}: incomplete narration {scene_id}."
            )

        validate_sentence_integrity(narration, scene_id)

        if record.get("word_count") != count_words(narration):
            raise RuntimeError(
                f"{scene_id}: word_count does not match narration."
            )

        if record.get("sentence_units") != sentence_units(narration):
            raise RuntimeError(
                f"{scene_id}: sentence segmentation mismatch."
            )

        if record.get("clause_units") != clause_units(narration):
            raise RuntimeError(
                f"{scene_id}: clause segmentation mismatch."
            )

        estimate = record.get("narration_duration_estimate_seconds")

        if (
            isinstance(estimate, bool)
            or not isinstance(estimate, (int, float))
            or not math.isfinite(float(estimate))
            or float(estimate) <= 0
        ):
            raise RuntimeError(
                f"{scene_id}: narration estimate is invalid."
            )

        actual = record.get("actual_audio_duration_seconds")
        source = record.get("audio_duration_source")

        if actual is None:
            if source != "not_measured_tts_pending":
                raise RuntimeError(
                    f"{scene_id}: unmeasured audio must remain pending."
                )

            if record.get("audio_duration") is not None:
                raise RuntimeError(
                    f"{scene_id}: estimate must not be stored as audio_duration."
                )

        else:
            if source != "ffprobe_measured":
                raise RuntimeError(
                    f"{scene_id}: actual duration must be ffprobe measured."
                )

            finite_duration(actual, f"{scene_id} measured audio duration")

            if record.get("audio_duration") != actual:
                raise RuntimeError(
                    f"{scene_id}: audio_duration must equal measured duration."
                )

            if not validate_cached_audio(record):
                raise RuntimeError(
                    f"{scene_id}: measured audio record failed file/SHA validation."
                )

        actual_ids.append(scene_id)

    if actual_ids != expected_ids:
        raise RuntimeError(
            f"{label} order/count mismatch. "
            f"Expected {len(expected_ids)} records; got {len(actual_ids)}."
        )


def validate_final_records(
    main_records: list[dict],
    extra_records: list[dict],
    parts: int,
    scenes_per_part: int,
) -> None:
    expected_main = [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    actual_main = [
        (int(row["part"]), int(row["scene"]))
        for row in main_records
    ]

    if actual_main != expected_main:
        raise RuntimeError(
            "Main narration part/scene order is incorrect."
        )

    validate_record_group(
        main_records,
        [row["scene_id"] for row in main_records],
        "Main narration",
    )

    expected_extra: list[tuple[int, str]] = []

    for part in range(1, parts + 1):
        expected_extra.append((part, "hook"))

        if part < parts:
            expected_extra.append((part, "suspense"))
        else:
            expected_extra.append((part, "resolution"))

    actual_extra = [
        (int(row["part"]), str(row["role"]))
        for row in extra_records
    ]

    if actual_extra != expected_extra:
        raise RuntimeError(
            "Hook/Suspense/Resolution records have incorrect mapping or order."
        )

    validate_record_group(
        extra_records,
        [row["scene_id"] for row in extra_records],
        "Extra narration",
    )

    all_ids = [
        row["scene_id"]
        for row in main_records + extra_records
    ]

    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError(
            "Duplicate scene_id exists across main and extra narration."
        )

    for row in main_records + extra_records:
        if row.get("status") != "completed":
            raise RuntimeError(
                f"Narration record incomplete: {row.get('scene_id')}."
            )


def make_output_document(
    status: str,
    title: str,
    settings: dict,
    current_signature: str,
    main_records: list[dict],
    extra_records: list[dict],
    started_at: str,
    error: str | None = None,
) -> dict:
    now = utc_now()
    all_records = main_records + extra_records

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
        "scenes_per_part": settings["scenes_per_part"],
        "total_scenes": settings["parts"] * settings["scenes_per_part"],
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
            "narration_duration": (
                "estimate_only_until_tts_audio_is_generated"
            ),
            "estimate_field": "narration_duration_estimate_seconds",
            "measured_audio_field": "actual_audio_duration_seconds",
            "measured_audio_source": "ffprobe_measured",
            "duration_must_be_measured_by_tts": True,
            "estimated_duration_is_not_actual_audio_duration": True,
            "no_arbitrary_fixed_duration": True,
        },
        "segmentation_policy": {
            "sentence_boundaries": [
                ".", "!", "?", "।", "॥"
            ],
            "clause_boundaries": [
                ",", ";", ":"
            ],
            "punctuation_retained": True,
            "no_arbitrary_sentence_truncation": True,
            "source_narration_preserved": True,
        },
        "role_policy": {
            "hook": "one separate narration record per part",
            "main": "configured main scene grid",
            "suspense": "one separate narration per non-final part",
            "resolution": "one separate narration for final part only",
        },
        "scenes": main_records,
        "extra_scenes": extra_records,
        "all_scene_ids": [
            row["scene_id"] for row in all_records
        ],
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
    started_at: str,
    expected_main: int,
    expected_extras: int,
    last_scene_id: str | None = None,
    error: str | None = None,
) -> None:
    main_records = [
        main_map[key] for key in sorted(main_map)
    ]

    extra_records = sorted(
        extra_map.values(),
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER[row["role"]],
        ),
    )

    document = make_output_document(
        status,
        title,
        settings,
        current_signature,
        main_records,
        extra_records,
        started_at,
        error,
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
    log("KATHA LOK AI — SENTENCE-SAFE NARRATION PREPARATION")
    log("=" * 64)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    settings = build_settings(config)

    parts = settings["parts"]
    scenes_per_part = settings["scenes_per_part"]
    expected_main_total = parts * scenes_per_part

    resume_enabled = cfg_bool(config, "RESUME_ENABLED", True)
    skip_completed = cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
    save_each_scene = cfg_bool(
        config,
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
        True,
    )

    story = load_story_metadata()
    title = resolve_title(config, story)

    source_data = load_json(SCENES_FILE)
    main_sources, extra_sources = validate_scene_collection(
        source_data,
        parts,
        scenes_per_part,
    )

    current_signature = source_signature(
        title,
        settings,
        main_sources,
        extra_sources,
    )

    reusable = load_reusable_records(
        current_signature,
        settings,
        resume_enabled and skip_completed,
    )

    started_at = utc_now()
    main_map: dict[tuple[int, int], dict] = {}
    extra_map: dict[str, dict] = {}

    ordered_sources = (
        [(row, "main") for row in main_sources]
        + [(row, row["role"]) for row in extra_sources]
    )

    log("FORMAT             :", settings["format"])
    log("TITLE              :", title)
    log("PARTS              :", parts)
    log("MAIN SCENES/PART   :", scenes_per_part)
    log("MAIN SCENE COUNT   :", expected_main_total)
    log("EXTRA SCENE COUNT  :", len(extra_sources))
    log("TOTAL RECORDS      :", len(ordered_sources))
    log("VOICE              :", settings["voice"])
    log("SPEED              :", settings["speed"])
    log("CAPTIONS           :", settings["captions"])
    log("RESUME             :", resume_enabled)
    log("SKIP COMPLETED     :", skip_completed)
    log("EXTERNAL API CALLS : 0")

    try:
        for index, (source, role) in enumerate(ordered_sources, 1):
            current = build_narration_record(
                source,
                settings,
                title,
                role,
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

                log(
                    f"[{index}/{len(ordered_sources)}] REUSED "
                    f"{scene_id} ({role})"
                )
            else:
                log(
                    f"[{index}/{len(ordered_sources)}] PREPARED "
                    f"{scene_id} ({role})"
                )

            if role == "main":
                key = (current["part"], current["scene"])

                if key in main_map:
                    raise RuntimeError(
                        f"Duplicate narration mapping: {key}."
                    )

                main_map[key] = current
            else:
                if scene_id in extra_map:
                    raise RuntimeError(
                        f"Duplicate extra narration ID: {scene_id}."
                    )

                extra_map[scene_id] = current

            if save_each_scene:
                save_progress(
                    "in_progress",
                    title,
                    settings,
                    current_signature,
                    main_map,
                    extra_map,
                    started_at,
                    expected_main_total,
                    len(extra_sources),
                    scene_id,
                )

        final_main = [
            main_map[(part, scene)]
            for part in range(1, parts + 1)
            for scene in range(1, scenes_per_part + 1)
        ]

        final_extras = []

        for part in range(1, parts + 1):
            required_roles = (
                ["hook", "suspense"]
                if part < parts
                else ["hook", "resolution"]
            )

            for role in required_roles:
                matches = [
                    row
                    for row in extra_map.values()
                    if row["part"] == part and row["role"] == role
                ]

                if len(matches) != 1:
                    raise RuntimeError(
                        f"Expected exactly one Part {part} {role} "
                        f"narration; found {len(matches)}."
                    )

                final_extras.append(matches[0])

        validate_final_records(
            final_main,
            final_extras,
            parts,
            scenes_per_part,
        )

        output = make_output_document(
            "completed",
            title,
            settings,
            current_signature,
            final_main,
            final_extras,
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
                "updated_at": utc_now(),
            },
        )

        saved = load_json(OUTPUT_FILE)

        if (
            not isinstance(saved, dict)
            or saved.get("status") != "completed"
            or saved.get("source_signature") != current_signature
            or len(saved.get("scenes", [])) != expected_main_total
            or len(saved.get("extra_scenes", [])) != len(final_extras)
        ):
            raise RuntimeError(
                "Narration output read-after-write validation failed."
            )

    except Exception as exc:
        save_progress(
            "failed",
            title,
            settings,
            current_signature,
            main_map,
            extra_map,
            started_at,
            expected_main_total,
            len(extra_sources),
            error=exc,
        )
        raise

    all_records = final_main + final_extras
    estimate_total = sum(
        row["narration_duration_estimate_seconds"]
        for row in all_records
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