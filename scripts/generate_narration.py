#!/usr/bin/env python3
"""Prepare validated, resumable narration records for the photo-motion pipeline."""

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

from voice_profiles import profile_summary
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

SCRIPT_VERSION = 14
MIN_DURATION = 0.5
MAX_DURATION = 3600.0
MIN_AUDIO_BYTES = 1000
MAX_PARTS = 1000
MAX_SCENES_PER_PART = 500
MAX_TOTAL_RECORDS = 10000

WORDS_PER_MINUTE = {
    "kids": 115,
    "adult": 145,
}

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
    "hook_scene": "hook",
    "main_scene": "main",
    "story": "main",
    "cliffhanger": "suspense",
    "ending": "resolution",
    "final": "resolution",
    "final_resolution": "resolution",
}


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


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}.")
        result[key] = value
    return result


def reject_nonfinite_json(value: str):
    raise ValueError(f"Non-finite JSON value is not permitted: {value!r}.")


def load_json(path: Path, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            try:
                label = path.relative_to(ROOT)
            except ValueError:
                label = path
            raise RuntimeError(f"Required JSON file not found: {label}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=reject_duplicate_json_keys, parse_constant=reject_nonfinite_json)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON file {path}: {exc}") from exc


def save_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            json.dump(
                value,
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


def cfg_text(
    config: dict,
    key: str,
    default: str = "",
) -> str:
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(
    config: dict,
    key: str,
    default: bool = False,
) -> bool:
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()

    if normalized in {"true", "yes", "on", "1", "enabled"}:
        return True

    if normalized in {"false", "no", "off", "0", "disabled"}:
        return False

    raise RuntimeError(f"{key} must be a valid boolean.")


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be a positive integer.")

    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        number = int(value.strip())
    else:
        raise RuntimeError(f"{label} must be a positive integer.")

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

    if not math.isfinite(number):
        raise RuntimeError(f"{label} must be finite.")

    if not MIN_DURATION <= number <= MAX_DURATION:
        raise RuntimeError(
            f"{label} must be between "
            f"{MIN_DURATION} and {MAX_DURATION} seconds."
        )

    return round(number, 3)


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    return re.sub(
        r"\s+",
        " ",
        value.replace("\ufeff", ""),
    ).strip()


def count_words(value: str) -> int:
    """Match Story partitioning: count normalized whitespace-separated tokens."""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return len(re.findall(r"\S+", normalized))


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

    if re.search(
        r"(?:^|\s)([\w'-]+)[.!?।॥]?\s+\1[.!?।॥]?$",
        value,
        flags=re.IGNORECASE,
    ):
        raise RuntimeError(
            f"{label}: repeated final word detected. "
            "Source narration must be corrected."
        )

    return value


def sentence_units(value: str) -> list[str]:
    value = re.sub(r"\s+", " ", value).strip()

    if not value:
        return []

    abbreviations = {
        "श्री.",
        "डॉ.",
        "प्रो.",
        "Mr.",
        "Mrs.",
        "Dr.",
        "जैसे.",
        "etc.",
        "e.g.",
        "i.e.",
        "vs.",
    }

    pattern = re.compile(
        r"""[.!?।॥]+["'”’)\]}»]*(?=\s|$)"""
    )

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

        for match in re.finditer(
            r"[,;:，；：](?:\s+|$)",
            sentence,
        ):
            unit = sentence[start:match.end()].strip()

            if unit:
                result.append(unit)

            start = match.end()

        tail = sentence[start:].strip()

        if tail:
            result.append(tail)

    return result or [value.strip()]


def validate_sentence_integrity(
    value: str,
    label: str,
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: narration is empty.")

    if "\x00" in value:
        raise RuntimeError(
            f"{label}: narration contains null characters."
        )

    normalized = re.sub(r"\s+", " ", value).strip()
    units = sentence_units(value)

    if not units:
        raise RuntimeError(
            f"{label}: no complete narration units found."
        )

    reconstructed = " ".join(units).strip()

    if reconstructed != normalized:
        raise RuntimeError(
            f"{label}: sentence segmentation changed source text."
        )


def estimate_speech_duration(
    value: str,
    audience: str,
    speed: str,
) -> float:
    """Estimate narration time; this is not measured TTS audio duration."""

    words = count_words(value)
    base_wpm = WORDS_PER_MINUTE.get(audience, 145)

    try:
        speed_percent = int(
            str(speed).replace("%", "").strip()
        )
    except (TypeError, ValueError):
        speed_percent = 0

    rate = max(
        40.0,
        base_wpm * (1.0 + speed_percent / 100.0),
    )

    sentence_pauses = len(
        re.findall(r"[.!?।॥]+", value)
    )
    clause_pauses = len(
        re.findall(r"[,;:，；：]", value)
    )

    seconds = (
        (words / rate) * 60.0
        + sentence_pauses * 0.22
        + clause_pauses * 0.08
    )

    return round(max(MIN_DURATION, seconds), 3)


def read_scene_duration(
    scene: dict,
    policy: str,
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

        return finite_duration(
            value,
            f"{label} {field}",
        )

    if policy == "fixed":
        raise RuntimeError(
            f"{label}: fixed SCENE_DURATION requires "
            "a duration in the source scene."
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
        raise RuntimeError(
            f"Unsupported narration role: {raw!r}."
        )

    return role


def build_settings(config: dict) -> dict:
    mode = str(get_format(config)).strip().lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {mode!r}"
        )

    parts_min, parts_max = get_parts_range(config)
    scenes_min, scenes_max = get_scenes_range(config)
    selected_parts = get_selected_parts(
        config,
        required=True,
    )

    if selected_parts is None:
        raise RuntimeError(
            "Actual PARTS selection is not available."
        )

    selected_parts = positive_int(
        selected_parts,
        "selected PARTS",
    )

    if not 1 <= parts_min <= parts_max <= MAX_PARTS:
        raise RuntimeError(
            "Configured PARTS range is invalid."
        )

    if not parts_min <= selected_parts <= parts_max:
        raise RuntimeError(
            "Selected PARTS is outside configured PARTS range."
        )

    if mode == "full" and selected_parts != 1:
        raise RuntimeError(
            "FULL format must contain exactly one Part."
        )

    if not (
        1 <= scenes_min <= scenes_max <= MAX_SCENES_PER_PART
    ):
        raise RuntimeError(
            "Configured SCENES range is invalid."
        )

    audience = cfg_text(
        config,
        "AUDIENCE",
        "adult",
    ).lower()

    voice = cfg_text(
        config,
        "VOICE",
        "male",
    ).lower()

    speed = cfg_text(
        config,
        "SPEED",
        "+0%",
    )

    captions = cfg_text(
        config,
        "CAPTIONS",
        "hindi",
    ).lower()

    duration_policy = cfg_text(
        config,
        "SCENE_DURATION",
        "auto",
    ).lower()

    if audience not in {"adult", "kids"}:
        raise RuntimeError(
            "AUDIENCE must be adult or kids."
        )

    if voice not in {"male", "female"}:
        raise RuntimeError(
            "VOICE must be male or female."
        )

    if not re.fullmatch(r"[+-]?\d{1,2}%", speed):
        raise RuntimeError(
            "SPEED must be a percentage such as +0%."
        )

    if not -50 <= int(speed.replace("%", "")) <= 50:
        raise RuntimeError(
            "SPEED must be between -50% and +50%."
        )

    if captions not in {"hindi", "english", "hinglish"}:
        raise RuntimeError(
            "CAPTIONS must be hindi, english or hinglish."
        )

    if duration_policy not in {"auto", "fixed"}:
        raise RuntimeError(
            "SCENE_DURATION must be auto or fixed."
        )

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
        "ambient_sound": cfg_bool(
            config,
            "AMBIENT_SOUND",
            True,
        ),
        "transitions": cfg_text(
            config,
            "TRANSITIONS",
            "cinematic",
        ),
        "tts_profile": cfg_text(config, "TTS_PROFILE", "auto"),
        "character_voices": cfg_bool(config, "CHARACTER_VOICES", True),
        "tts_emotion_control": cfg_bool(config, "TTS_EMOTION_CONTROL", True),
        "tts_child_friendly": cfg_bool(config, "TTS_CHILD_FRIENDLY", True),
        "tts_pitch": float(config.get("TTS_PITCH", 1.0)),
        "tts_emotion_strength": float(config.get("TTS_EMOTION_STRENGTH", 0.65)),
    }


def resolve_title(
    config: dict,
    story: dict,
) -> str:
    for field in (
        "title",
        "generated_title",
        "story_title",
        "final_title",
    ):
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
        raise RuntimeError(
            "ai_story.json has no full-story title."
        )

    raise RuntimeError("Story title is missing.")


def canonical_main_id(
    part: int,
    scene: int,
) -> str:
    return f"part_{part:02d}_scene_{scene:02d}"


def canonical_extra_id(
    part: int,
    role: str,
) -> str:
    return f"part_{part:02d}_{role}"


def load_story_metadata() -> dict:
    value = load_json(
        AI_STORY_FILE,
        required=True,
    )

    if not isinstance(value, dict):
        raise RuntimeError(
            "ai_story.json must contain a JSON object."
        )

    return value


def validate_ai_story(
    story: dict,
    settings: dict,
) -> tuple[list[int], dict[str, str]]:
    if not story:
        raise RuntimeError("ai_story.json is empty.")

    if story.get("status") != "completed":
        raise RuntimeError(
            "ai_story.json is not completed."
        )

    if str(story.get("format", "")).lower() != settings["format"]:
        raise RuntimeError(
            "ai_story.json FORMAT does not match Input."
        )

    parts = story.get("parts")

    if (
        not isinstance(parts, list)
        or len(parts) != settings["parts"]
    ):
        raise RuntimeError(
            "ai_story.json actual PARTS count mismatch."
        )

    counts: list[int] = []
    source_texts: dict[str, str] = {}

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(
                f"ai_story.json Part {part_number} is invalid."
            )

        if positive_int(part.get("part"), f"ai_story.json Part {part_number}") != part_number:
            raise RuntimeError(
                f"ai_story.json Part {part_number} numbering mismatch."
            )

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(
                f"ai_story.json Part {part_number} has no Main Scenes."
            )

        count = len(scenes)
        locked_part_text = clean_narration(
            part.get("narration"),
            f"ai_story.json Part {part_number}",
        )
        if part.get("text_locked") is not True:
            raise RuntimeError(
                f"ai_story.json Part {part_number} narration is not locked."
            )
        if part.get("locked_word_count") != count_words(locked_part_text):
            raise RuntimeError(
                f"ai_story.json Part {part_number} locked word count mismatch."
            )
        if part.get("locked_text_sha256") != text_sha256(locked_part_text):
            raise RuntimeError(
                f"ai_story.json Part {part_number} locked text hash mismatch."
            )

        if not (
            settings["scenes_min"]
            <= count
            <= settings["scenes_max"]
        ):
            raise RuntimeError(
                f"Part {part_number} has {count} Scenes; "
                f"configured range is "
                f"{settings['scenes_min']}-{settings['scenes_max']}."
            )

        counts.append(count)

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {part_number} Scene {scene_number} is invalid."
                )

            expected_id = canonical_main_id(
                part_number,
                scene_number,
            )

            if (
                positive_int(scene.get("scene"), f"{expected_id} scene number") != scene_number
                or scene.get("scene_id") != expected_id
            ):
                raise RuntimeError(
                    f"ai_story.json Scene ID/order mismatch: {expected_id}."
                )

            if normalize_role(scene) != "main":
                raise RuntimeError(
                    f"{expected_id} must have role main."
                )

            narration = clean_narration(
                scene.get("narration"),
                expected_id,
            )

            validate_sentence_integrity(
                narration,
                expected_id,
            )
            if scene.get("source_text_sha256") != text_sha256(narration):
                raise RuntimeError(
                    f"{expected_id}: locked narration hash mismatch."
                )
            if scene.get("source_word_count") != count_words(narration):
                raise RuntimeError(
                    f"{expected_id}: locked narration word count mismatch."
                )

            source_texts[expected_id] = narration

        reconstructed_part = normalize_text(
            " ".join(
                source_texts[canonical_main_id(part_number, n)]
                for n in range(1, count + 1)
            )
        )
        if reconstructed_part != normalize_text(locked_part_text):
            raise RuntimeError(
                f"Part {part_number}: Main Scene narration does not reconstruct locked Part."
            )

        final = part_number == settings["parts"]

        required = {
            "hook_scene": "hook",
            "suspense_scene": "suspense",
            "resolution_scene": "resolution",
        }

        for field, role in required.items():
            record = part.get(field)

            should_exist = (
                role == "hook"
                or (role == "suspense" and not final)
                or (role == "resolution" and final)
            )

            if should_exist != isinstance(record, dict):
                raise RuntimeError(
                    f"Part {part_number} {field} presence is invalid."
                )

            if should_exist:
                expected_id = canonical_extra_id(
                    part_number,
                    role,
                )

                if record.get("scene_id") != expected_id:
                    raise RuntimeError(
                        f"Part {part_number} {role} scene ID mismatch."
                    )

                narration = clean_narration(
                    record.get("narration"),
                    expected_id,
                )
                validate_sentence_integrity(narration, expected_id)
                if len(narration.split()) < 8:
                    raise RuntimeError(f"{expected_id}: extra-role narration is too short.")
                if record.get("voiceover_enabled") is not True:
                    raise RuntimeError(f"{expected_id}: extra-role voiceover must be enabled.")
                if record.get("narration_source") != "ai_generated_role_narration":
                    raise RuntimeError(f"{expected_id}: extra-role narration source is invalid.")
                if record.get("source_text_sha256") != text_sha256(narration):
                    raise RuntimeError(f"{expected_id}: extra-role narration hash mismatch.")

    declared = story.get("actual_scene_counts")
    if declared is not None and (
        not isinstance(declared, list)
        or len(declared) != len(counts)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in declared)
        or declared != counts
    ):
        raise RuntimeError("ai_story.json actual_scene_counts mismatch.")

    expected_main_total = sum(counts)
    for field, expected in (
        ("total_parts", settings["parts"]),
        ("parts_count", settings["parts"]),
        ("total_scenes", expected_main_total),
        ("total_main_scenes", expected_main_total),
        ("total_extra_scenes", settings["parts"] * 2),
    ):
        value = story.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value != expected
        ):
            raise RuntimeError(f"ai_story.json {field} mismatch.")

    if story.get("scene_order") not in (None, "part_then_main_scene"):
        raise RuntimeError("ai_story.json scene_order is unsupported.")

    if story.get("extra_scene_order") not in (
        None,
        ["hook", "main_scenes", "suspense_or_resolution"],
    ):
        raise RuntimeError("ai_story.json extra_scene_order is inconsistent.")

    return counts, source_texts


def extract_scene_rows(data: Any) -> list[dict]:
    if (
        not isinstance(data, dict)
        or data.get("status") != "completed"
    ):
        raise RuntimeError(
            "scenes.json is missing or not completed."
        )

    rows = data.get("scenes")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            "scenes.json contains no scene records."
        )

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
            raise RuntimeError(
                f"Scene record {index} is not an object."
            )

        role = normalize_role(source)

        part = positive_int(
            source.get("part"),
            f"scene {index} part",
        )

        if not 1 <= part <= settings["parts"]:
            raise RuntimeError(
                f"Scene record {index} refers to an unselected Part."
            )

        if source.get("status") != "completed":
            raise RuntimeError(f"Scene record {index} is not completed.")

        source_signature = source.get("source_scene_signature")
        if (
            not isinstance(source_signature, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source_signature)
        ):
            raise RuntimeError(
                f"Scene record {index} has an invalid source scene signature."
            )

        item = dict(source)
        item["part"] = part
        item["role"] = role

        scene_label = str(
            source.get("scene_id")
            or f"Part {part} {role}"
        )
        raw_narration = source.get("narration", "")
        if role == "main":
            item["narration"] = clean_narration(
                raw_narration,
                f"Part {part} {role}",
            )
            validate_sentence_integrity(item["narration"], scene_label)
        else:
            item["narration"] = clean_narration(
                raw_narration,
                f"Part {part} {role}",
            )
            validate_sentence_integrity(item["narration"], scene_label)
            if source.get("voiceover_enabled") is not True:
                raise RuntimeError(
                    f"{scene_label}: narrated extra role voiceover must be enabled."
                )
            if source.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(
                    f"{scene_label}: extra narration source metadata is invalid."
                )
            if source.get("source_text_sha256") != text_sha256(item["narration"]):
                raise RuntimeError(f"{scene_label}: extra narration hash mismatch.")
            if source.get("source_word_count") != count_words(item["narration"]):
                raise RuntimeError(f"{scene_label}: extra narration word count mismatch.")

        if role == "main":
            scene_number = positive_int(
                source.get("scene"),
                f"scene {index} number",
            )

            expected_id = canonical_main_id(
                part,
                scene_number,
            )

            if source.get("scene_id") != expected_id:
                raise RuntimeError(
                    f"Scene ID must be {expected_id}."
                )

            key = (part, scene_number)

            if key in main_map:
                raise RuntimeError(
                    f"Duplicate Main Scene: {expected_id}."
                )

            item["scene"] = scene_number
            item["scene_id"] = expected_id
            main_map[key] = item

        else:
            expected_id = canonical_extra_id(
                part,
                role,
            )

            if source.get("scene_id") != expected_id:
                raise RuntimeError(
                    f"Role Scene ID must be {expected_id}."
                )

            key = (part, role)

            if key in extras:
                raise RuntimeError(
                    f"Duplicate role record: {expected_id}."
                )

            item["scene"] = None
            item["scene_id"] = expected_id
            extras[key] = item

        if item["scene_id"] in ids:
            raise RuntimeError(
                f"Duplicate scene ID: {item['scene_id']}."
            )

        ids.add(item["scene_id"])

    ai_counts, ai_texts = validate_ai_story(
        ai_story,
        settings,
    )

    actual_counts: list[int] = []

    for part in range(1, settings["parts"] + 1):
        numbers = sorted(
            scene
            for current_part, scene in main_map
            if current_part == part
        )

        if (
            not numbers
            or numbers != list(range(1, len(numbers) + 1))
        ):
            raise RuntimeError(
                f"Part {part} Main Scene numbering is not continuous."
            )

        count = len(numbers)

        if not (
            settings["scenes_min"]
            <= count
            <= settings["scenes_max"]
        ):
            raise RuntimeError(
                f"Part {part} Scene count {count} is outside "
                "the configured range."
            )

        if (
            ai_counts is not None
            and ai_counts[part - 1] != count
        ):
            raise RuntimeError(
                f"Part {part} Scene count differs from ai_story.json."
            )

        actual_counts.append(count)

        required_roles = (
            ["hook", "suspense"]
            if part < settings["parts"]
            else ["hook", "resolution"]
        )

        actual_roles = [
            role
            for current_part, role in extras
            if current_part == part
        ]

        if sorted(actual_roles) != sorted(required_roles):
            raise RuntimeError(
                f"Part {part} requires exactly: "
                f"{', '.join(required_roles)}."
            )

    expected_keys = {
        (part, scene)
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    }

    if set(main_map) != expected_keys:
        raise RuntimeError(
            "Main Scene collection contains missing or extra Scene IDs."
        )

    ordered_main = [
        main_map[(part, scene)]
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    ]

    ordered_extras = [
        extras[(part, role)]
        for part in range(1, settings["parts"] + 1)
        for role in (
            ("hook", "suspense")
            if part < settings["parts"]
            else ("hook", "resolution")
        )
    ]

    if ai_texts:
        for row in ordered_main:
            scene_id = row["scene_id"]
            expected_text = ai_texts.get(scene_id)

            if expected_text is None:
                raise RuntimeError(
                    f"{scene_id} is missing from ai_story.json."
                )

            if normalize_text(row["narration"]) != normalize_text(
                expected_text
            ):
                raise RuntimeError(
                    f"{scene_id}: scenes.json narration differs "
                    "from ai_story.json narration."
                )

    declared_counts = data.get("actual_scene_counts")
    if declared_counts is not None and (
        not isinstance(declared_counts, list)
        or len(declared_counts) != len(actual_counts)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in declared_counts)
        or declared_counts != actual_counts
    ):
        raise RuntimeError("scenes.json actual_scene_counts mismatch.")

    for field, expected in (
        ("total_parts", settings["parts"]),
        ("total_main_scenes", sum(actual_counts)),
        ("total_scenes", sum(actual_counts) + len(ordered_extras)),
        ("total_extra_scenes", len(ordered_extras)),
    ):
        value = data.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value != expected
        ):
            raise RuntimeError(f"scenes.json {field} mismatch.")

    generation_signature = data.get("generation_signature")
    if (
        not isinstance(generation_signature, str)
        or not re.fullmatch(r"[0-9a-f]{64}", generation_signature)
    ):
        raise RuntimeError("scenes.json generation_signature is invalid.")

    if str(data.get("format", "")).strip().lower() != settings["format"]:
        raise RuntimeError("scenes.json FORMAT does not match Input.")

    if data.get("scene_order") != (
        "part_then_hook_then_main_then_suspense_or_resolution"
    ):
        raise RuntimeError("scenes.json scene_order is invalid.")

    if data.get("source_story_signature") != stable_hash(ai_story):
        raise RuntimeError(
            "scenes.json was not generated from the current ai_story.json."
        )

    total_records = (
        sum(actual_counts)
        + len(ordered_extras)
    )

    if len(ordered_main) > MAX_TOTAL_RECORDS:
        raise RuntimeError(
            "Total Main narration records exceed the supported limit."
        )

    # Hook/Suspense/Resolution remain visual scenes; they are not voiceover.
    return ordered_main, ordered_extras, actual_counts


def source_signature(
    title: str,
    settings: dict,
    main_sources: list[dict],
    extra_sources: list[dict],
    actual_counts: list[int],
) -> str:
    def relevant(rows: list[dict]) -> list[dict]:
        result = []

        for row in rows:
            result.append({
                "part": row["part"],
                "scene": row.get("scene"),
                "scene_id": row["scene_id"],
                "role": row["role"],
                "narration": row["narration"],
                "duration": row.get("duration"),
                "dialogue": row.get("dialogue", ""),
                "character_ids": row.get("character_ids", []),
                "speaker": row.get("speaker") or row.get("speaker_name") or "",
                "speaker_id": row.get("speaker_id") or "",
                "emotion": row.get("emotion") or row.get("emotion_tag") or "",
                "music_mood": row.get("music_mood") or "",
                "visual_prompt": row.get("visual_prompt", ""),
                "source_scene_signature": row.get(
                    "source_scene_signature"
                ),
            })

        return result

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
    part = positive_int(
        scene.get("part"),
        "narration part",
    )

    if role == "main":
        scene_number = positive_int(
            scene.get("scene"),
            "scene number",
        )

        scene_id = canonical_main_id(
            part,
            scene_number,
        )
    else:
        scene_number = None
        scene_id = canonical_extra_id(
            part,
            role,
        )

    label = f"Part {part} {role.upper()}"

    narration = clean_narration(
        scene.get("narration"),
        label,
    )

    validate_sentence_integrity(
        narration,
        label,
    )

    duration = read_scene_duration(
        scene,
        settings["scene_duration"],
        label,
    )

    estimate = estimate_speech_duration(
        narration,
        settings["audience"],
        settings["speed"],
    )

    dialogue = scene.get("dialogue", "")

    if not isinstance(dialogue, (str, list, dict)):
        dialogue = str(dialogue)

    character_ids = scene.get("character_ids", [])

    if not isinstance(character_ids, list):
        raise RuntimeError(
            f"{scene_id}: character_ids must be a list."
        )

    sentences = sentence_units(narration)
    clauses = clause_units(narration)
    digest = text_sha256(narration)

    return {
        "part": part,
        "scene": scene_number,
        "scene_id": scene_id,
        "global_scene_number": (
            global_number if role == "main" else None
        ),
        "role": role,
        "scene_type": role,
        "voiceover_enabled": True,
        "narration_source": (
            "locked_part_text" if role == "main"
            else "ai_generated_role_narration"
        ),
        "source_text_sha256": digest,
        "source_word_count": count_words(narration),
        "title": str(
            scene.get("title", "")
        ).strip(),
        "story_title": title,
        "text": narration,
        "narration": narration,
        "text_sha256": digest,
        "narration_sha256": digest,
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
        "tts_fingerprint": None,
        "dialogue": dialogue,
        "character_ids": character_ids,
        "speaker_id": str(scene.get("speaker_id") or "narrator"),
        "speaker": str(scene.get("speaker") or scene.get("speaker_name") or "Narrator"),
        "emotion": str(scene.get("emotion") or scene.get("emotion_tag") or {
            "hook": "curiosity", "suspense": "suspense",
            "resolution": "warm", "main": "neutral",
        }.get(role, "neutral")),
        "music_mood": str(scene.get("music_mood") or {
            "hook": "intriguing cinematic",
            "suspense": "tense suspense",
            "resolution": "warm emotional resolution",
            "main": "story-matched cinematic",
        }.get(role, "story-matched cinematic")),
        "voice_profile": profile_summary({
            "AUDIENCE": settings["audience"],
            "VOICE": settings["voice"],
            "TTS_PROFILE": settings.get("tts_profile", "auto"),
            "TTS_PITCH": settings.get("tts_pitch", 1.0),
            "TTS_EMOTION_STRENGTH": settings.get("tts_emotion_strength", 0.65),
            "TTS_CHILD_FRIENDLY": settings.get("tts_child_friendly", True),
            "CHARACTER_VOICES": settings.get("character_voices", True),
        }, {
            "role": role,
            "speaker_id": str(scene.get("speaker_id") or "narrator"),
            "speaker": str(scene.get("speaker") or scene.get("speaker_name") or "Narrator"),
            "emotion": str(scene.get("emotion") or scene.get("emotion_tag") or {
                "hook": "curiosity", "suspense": "suspense",
                "resolution": "warm", "main": "neutral",
            }.get(role, "neutral")),
        }),
        "status": "completed",
    }


def validate_cached_audio(record: dict) -> bool:
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

        if (
            not path.is_file()
            or path.stat().st_size < MIN_AUDIO_BYTES
        ):
            return False

        declared_hash = (
            record.get("audio_sha256")
            or record.get("audio_hash")
        )

        if not declared_hash:
            return False

        if sha256_file(path) != declared_hash:
            return False

        duration = record.get(
            "actual_audio_duration_seconds"
        )

        if duration is None:
            duration = record.get("audio_duration")

        duration = float(duration)

        if (
            not math.isfinite(duration)
            or not MIN_DURATION <= duration <= MAX_DURATION
        ):
            return False

        fingerprint = record.get("tts_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}",
            fingerprint,
        ):
            return False

        provider = record.get("tts_provider") or record.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            return False

        declared_size = record.get("audio_size_bytes")
        if (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size != path.stat().st_size
        ):
            return False

        return True

    except (
        OSError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return False


def load_reusable_records(
    current_signature: str,
    settings: dict,
    enabled: bool,
) -> dict[str, dict]:
    if not enabled:
        return {}

    document = load_json(
        OUTPUT_FILE,
        required=False,
    )

    if not isinstance(document, dict):
        return {}

    # Failed runs are included so completed records survive interruptions.
    if document.get("status") not in {
        "in_progress",
        "completed",
        "failed",
    }:
        return {}

    if document.get("source_signature") != current_signature:
        log(
            "Narration cache rejected: source signature changed."
        )
        return {}

    if document.get("input_config") != settings:
        log(
            "Narration cache rejected: settings changed."
        )
        return {}

    reusable: dict[str, dict] = {}

    for field in ("scenes", "extra_scenes"):
        rows = document.get(field, [])

        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue

            scene_id = str(
                row.get("scene_id") or ""
            ).strip()

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
        "text_sha256",
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
        "speaker_id",
        "speaker",
        "emotion",
        "voice_profile",
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
        "audio_size_bytes",
        "audio_duration",
        "actual_audio_duration_seconds",
        "audio_duration_source",
        "duration_source",
        "tts_provider",
        "tts_providers",
        "tts_model",
        "provider",
        "providers",
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

        scene_id = str(
            record.get("scene_id") or ""
        ).strip()

        if not scene_id:
            raise RuntimeError(
                f"{label} record {index} has no scene_id."
            )

        if scene_id in actual_ids:
            raise RuntimeError(
                f"{label}: duplicate scene_id {scene_id}."
            )

        narration = record.get("text")

        if (
            not isinstance(narration, str)
            or not narration.strip()
        ):
            raise RuntimeError(
                f"{label}: empty narration for {scene_id}."
            )

        if record.get("status") != "completed":
            raise RuntimeError(
                f"{label}: incomplete narration {scene_id}."
            )

        validate_sentence_integrity(
            narration,
            scene_id,
        )

        digest = text_sha256(narration)

        if record.get("text_sha256") != digest:
            raise RuntimeError(
                f"{scene_id}: text SHA-256 mismatch."
            )

        if record.get("narration_sha256") != digest:
            raise RuntimeError(
                f"{scene_id}: narration SHA-256 mismatch."
            )

        if record.get("word_count") != count_words(narration):
            raise RuntimeError(
                f"{scene_id}: word_count metadata mismatch."
            )

        if record.get("sentence_units") != sentence_units(narration):
            raise RuntimeError(
                f"{scene_id}: sentence segmentation mismatch."
            )

        if record.get("clause_units") != clause_units(narration):
            raise RuntimeError(
                f"{scene_id}: clause segmentation mismatch."
            )

        estimate = record.get(
            "narration_duration_estimate_seconds"
        )

        if (
            isinstance(estimate, bool)
            or not isinstance(estimate, (int, float))
            or not math.isfinite(float(estimate))
            or float(estimate) <= 0
        ):
            raise RuntimeError(
                f"{scene_id}: narration estimate is invalid."
            )

        actual = record.get(
            "actual_audio_duration_seconds"
        )

        source = record.get(
            "audio_duration_source"
        )

        if actual is None:
            if source != "not_measured_tts_pending":
                raise RuntimeError(
                    f"{scene_id}: unmeasured audio must remain pending."
                )

            if record.get("audio_duration") is not None:
                raise RuntimeError(
                    f"{scene_id}: estimated duration cannot be "
                    "stored as actual audio_duration."
                )

        else:
            if source != "ffprobe_measured":
                raise RuntimeError(
                    f"{scene_id}: actual duration must be ffprobe measured."
                )

            measured = finite_duration(
                actual,
                f"{scene_id} measured audio duration",
            )

            if record.get("audio_duration") != measured:
                raise RuntimeError(
                    f"{scene_id}: audio_duration must equal measured duration."
                )

            if not validate_cached_audio(record):
                raise RuntimeError(
                    f"{scene_id}: measured audio failed file/SHA validation."
                )

        actual_ids.append(scene_id)

    if actual_ids != expected_ids:
        raise RuntimeError(
            f"{label} order/count mismatch: expected "
            f"{len(expected_ids)}, received {len(actual_ids)}."
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

    actual_main = [
        (int(row["part"]), int(row["scene"]))
        for row in main_records
    ]

    if actual_main != expected_main:
        raise RuntimeError(
            "Main narration Part/Scene order is incorrect."
        )

    expected_main_ids = [
        canonical_main_id(part, scene)
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    ]

    validate_record_group(
        main_records,
        expected_main_ids,
        "Main narration",
    )

    expected_extra_ids = [
        canonical_extra_id(part, role)
        for part in range(1, parts + 1)
        for role in (("hook", "suspense") if part < parts else ("hook", "resolution"))
    ]
    actual_extra_ids = [row.get("scene_id") for row in extra_records]
    if actual_extra_ids != expected_extra_ids:
        raise RuntimeError("Hook/Suspense/Resolution narration order/count is incorrect.")
    validate_record_group(extra_records, expected_extra_ids, "Extra-role narration")

    all_ids = [
        row["scene_id"]
        for row in main_records + extra_records
    ]

    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError(
            "Duplicate scene_id across Main and extra narration."
        )


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

    main_by_key = {
        (int(row["part"]), int(row["scene"])): row
        for row in main_records
    }
    extra_by_key = {
        (int(row["part"]), str(row["role"])): row
        for row in extra_records
    }
    narration_order = []

    for part in range(1, settings["parts"] + 1):
        narration_order.append(extra_by_key[(part, "hook")]["scene_id"])
        for scene_number in range(1, actual_counts[part - 1] + 1):
            main = main_by_key.get((part, scene_number))
            if main is not None:
                narration_order.append(main["scene_id"])
        ending_role = "resolution" if part == settings["parts"] else "suspense"
        narration_order.append(extra_by_key[(part, ending_role)]["scene_id"])

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
        "scenes_range": [
            settings["scenes_min"],
            settings["scenes_max"],
        ],
        "actual_scene_counts": actual_counts,
        "scenes_per_part": None,
        "total_scenes": len(all_records),
        "total_main_scenes": len(main_records),
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
            "estimate_field": (
                "narration_duration_estimate_seconds"
            ),
            "measured_audio_field": (
                "actual_audio_duration_seconds"
            ),
            "measured_audio_source": "ffprobe_measured",
            "duration_must_be_measured_by_tts": True,
            "estimated_duration_is_not_actual_audio_duration": True,
            "no_arbitrary_fixed_duration": True,
        },
        "segmentation_policy": {
            "sentence_boundaries": [
                ".",
                "!",
                "?",
                "।",
                "॥",
            ],
            "clause_boundaries": [
                ",",
                ";",
                ":",
                "，",
                "；",
                "：",
            ],
            "punctuation_retained": True,
            "no_arbitrary_sentence_truncation": True,
            "source_narration_preserved": True,
            "text_sha256_per_record": True,
        },
        "role_policy": {
            "hook": "spoken AI-generated narration with TTS audio",
            "main": (
                "only voiceover source; exact locked Part text "
                "split across variable Main Scenes"
            ),
            "suspense": "spoken AI-generated narration with TTS audio",
            "resolution": "spoken AI-generated narration with TTS audio",
        },
        "scenes": main_records,
        "extra_scenes": extra_records,
        "all_scene_ids": [
            row["scene_id"]
            for row in all_records
        ],
        "narration_order": narration_order,
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
    main_records = [
        main_map[key]
        for key in sorted(main_map)
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
        actual_counts,
        started_at,
        error,
    )

    save_json_atomic(
        OUTPUT_FILE,
        document,
    )

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
            "actual_scene_counts": actual_counts,
            "updated_at": utc_now(),
            "last_error": (
                str(error)[:2000]
                if error
                else None
            ),
        },
    )


def verify_saved_output(
    current_signature: str,
    actual_counts: list[int],
    expected_main: int,
    expected_extras: int,
    expected_texts: dict[str, str],
    settings: dict,
) -> None:
    saved = load_json(OUTPUT_FILE)

    if not isinstance(saved, dict):
        raise RuntimeError(
            "Narration output read-after-write validation failed."
        )

    if saved.get("status") != "completed":
        raise RuntimeError(
            "Narration output is not marked completed."
        )

    if saved.get("script_version") != SCRIPT_VERSION:
        raise RuntimeError("Narration output script version mismatch.")

    if saved.get("input_config") != settings:
        raise RuntimeError("Narration output settings mismatch.")

    if saved.get("source_signature") != current_signature:
        raise RuntimeError(
            "Narration output source signature mismatch."
        )

    if saved.get("actual_scene_counts") != actual_counts:
        raise RuntimeError(
            "Narration output actual scene counts mismatch."
        )

    if len(saved.get("scenes", [])) != expected_main:
        raise RuntimeError(
            "Narration output Main Scene count mismatch."
        )

    if len(saved.get("extra_scenes", [])) != expected_extras:
        raise RuntimeError("Narrated extra-role record count mismatch.")

    if len(saved.get("all_scene_ids", [])) != expected_main + expected_extras:
        raise RuntimeError(
            "Narration output scene ID count mismatch."
        )

    if (
        not isinstance(saved.get("scenes"), list)
        or not isinstance(saved.get("extra_scenes"), list)
    ):
        raise RuntimeError("Narration output record groups are invalid.")

    all_records = saved["scenes"] + saved["extra_scenes"]
    all_ids = [
        row.get("scene_id")
        for row in all_records
        if isinstance(row, dict)
    ]

    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("Narration output contains duplicate scene IDs.")

    if saved.get("all_scene_ids") != all_ids:
        raise RuntimeError("Narration output all_scene_ids order mismatch.")

    main_by_key = {
        (int(row["part"]), int(row["scene"])): row
        for row in saved["scenes"]
    }
    extra_by_key = {
        (int(row["part"]), str(row["role"])): row
        for row in saved["extra_scenes"]
    }
    expected_order = []
    for part, count in enumerate(actual_counts, 1):
        expected_order.append(f"part_{part:02d}_hook")
        expected_order.extend(
            f"part_{part:02d}_scene_{scene_number:02d}"
            for scene_number in range(1, count + 1)
        )
        ending_role = "resolution" if part == settings["parts"] else "suspense"
        expected_order.append(f"part_{part:02d}_{ending_role}")

    if saved.get("narration_order") != expected_order:
        raise RuntimeError("Narration Part/Scene timeline order mismatch.")

    for record in all_records:
        if not isinstance(record, dict):
            raise RuntimeError("Narration output contains an invalid record.")
        scene_id = record.get("scene_id")
        expected_text = expected_texts.get(scene_id)
        if expected_text is None or record.get("text") != expected_text:
            raise RuntimeError(f"Narration read-back text mismatch: {scene_id}.")

    validate_final_records(
        saved["scenes"],
        saved["extra_scenes"],
        actual_counts,
        settings["parts"],
    )


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — VARIABLE-SCENE NARRATION PREPARATION")
    log("=" * 64)

    config = load_and_validate(
        ROOT / "Input" / "topic.txt"
    )

    settings = build_settings(config)
    parts = settings["parts"]

    resume_enabled = cfg_bool(
        config,
        "RESUME_ENABLED",
        True,
    )

    skip_completed = cfg_bool(
        config,
        "SKIP_COMPLETED_SCENES",
        True,
    )

    story = load_story_metadata()
    title = resolve_title(config, story)

    source_data = load_json(SCENES_FILE)

    (
        main_sources,
        extra_sources,
        actual_counts,
    ) = validate_scene_collection(
        source_data,
        settings,
        story,
    )

    expected_main_total = sum(actual_counts)
    expected_extras_total = settings["parts"] * 2

    if expected_main_total > MAX_TOTAL_RECORDS:
        raise RuntimeError(
            "Total narration record count exceeds supported limit."
        )

    current_signature = source_signature(
        title,
        settings,
        main_sources,
        extra_sources,
        actual_counts,
    )

    reusable = load_reusable_records(
        current_signature,
        settings,
        resume_enabled and skip_completed,
    )

    started_at = utc_now()

    main_map: dict[tuple[int, int], dict] = {}
    extra_map: dict[str, dict] = {}

    ordered_sources: list[tuple[dict, str, int | None]] = []
    main_by_key = {
        (row["part"], row["scene"]): row
        for row in main_sources
    }
    global_number = 0

    extra_by_key = {
        (row["part"], row["role"]): row for row in extra_sources
    }
    for part in range(1, parts + 1):
        ordered_sources.append((extra_by_key[(part, "hook")], "hook", None))
        for scene_number in range(1, actual_counts[part - 1] + 1):
            global_number += 1
            ordered_sources.append((
                main_by_key[(part, scene_number)],
                "main",
                global_number,
            ))
        ending_role = "resolution" if part == parts else "suspense"
        ordered_sources.append((extra_by_key[(part, ending_role)], ending_role, None))

    log("FORMAT             :", settings["format"])
    log("TITLE              :", title)
    log("PARTS              :", parts)
    log(
        "SCENES RANGE       :",
        f'{settings["scenes_min"]}-{settings["scenes_max"]}',
    )
    log("ACTUAL SCENE COUNTS:", actual_counts)
    log("MAIN SCENE COUNT   :", expected_main_total)
    log("EXTRA SCENE COUNT  :", expected_extras_total)
    log("TOTAL RECORDS      :", len(ordered_sources))
    log("VOICE              :", settings["voice"])
    log("SPEED              :", settings["speed"])
    log("CAPTIONS           :", settings["captions"])
    log("RESUME             :", resume_enabled)
    log("SKIP COMPLETED     :", skip_completed)
    log("CHECKPOINT         :", "after every record")
    log("EXTERNAL API CALLS :", 0)

    try:
        for index, (
            source,
            role,
            global_scene_number,
        ) in enumerate(ordered_sources, 1):

            current = build_narration_record(
                source,
                settings,
                title,
                role,
                global_scene_number,
            )

            scene_id = current["scene_id"]
            cached = reusable.get(scene_id)

            if (
                resume_enabled
                and skip_completed
                and cached
                and reusable_record_matches(
                    cached,
                    current,
                )
            ):
                current = preserve_tts_result(
                    cached,
                    current,
                )

                action = (
                    "REUSED TTS AUDIO"
                    if validate_cached_audio(current)
                    else "REUSED TEXT"
                )

            else:
                action = "PREPARED"

            log(
                f"[{index}/{len(ordered_sources)}] "
                f"{action}: {scene_id}"
            )

            if role == "main":
                key = (
                    current["part"],
                    current["scene"],
                )

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

            # A successful record is checkpointed immediately.
            # This is deliberately not disabled by configuration.
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
                expected_extras_total,
                scene_id,
            )

        final_main = [
            main_map[(part, scene)]
            for part, count in enumerate(
                actual_counts,
                1,
            )
            for scene in range(1, count + 1)
        ]

        final_extras = [
            extra_map[canonical_extra_id(part, role)]
            for part in range(1, parts + 1)
            for role in (("hook", "suspense") if part < parts else ("hook", "resolution"))
        ]

        validate_final_records(
            final_main,
            final_extras,
            actual_counts,
            parts,
        )

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

        save_json_atomic(
            OUTPUT_FILE,
            output,
        )

        save_json_atomic(
            CHECKPOINT_FILE,
            {
                "status": "completed",
                "script_version": SCRIPT_VERSION,
                "source_signature": current_signature,
                "completed_main": len(final_main),
                "expected_main": expected_main_total,
                "completed_extras": len(final_extras),
                "expected_extras": expected_extras_total,
                "total_records": (
                    len(final_main) + len(final_extras)
                ),
                "actual_scene_counts": actual_counts,
                "updated_at": utc_now(),
            },
        )

        expected_texts = {
            row["scene_id"]: row["narration"]
            for row in main_sources + extra_sources
        }

        verify_saved_output(
            current_signature,
            actual_counts,
            expected_main_total,
            expected_extras_total,
            expected_texts,
            settings,
        )

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
            expected_extras_total,
            error=exc,
        )

        raise

    all_records = final_extras + final_main

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
    log("ACTUAL SCENE COUNTS:", actual_counts)
    log("MAIN SCENES        :", len(final_main))
    log("HOOK/SUSPENSE/END  :", len(final_extras))
    log("TOTAL RECORDS      :", len(all_records))
    log("ESTIMATED SPEECH   :", f"{estimate_total:.1f}s")
    log(
        "MEASURED TTS AUDIO :",
        measured_count,
        "/",
        len(all_records),
    )
    log("EXTERNAL API CALLS :", 0)
    log("OUTPUT             :", OUTPUT_FILE.relative_to(ROOT))
    log("CHECKPOINT         :", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 64)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except KeyboardInterrupt:
        log(
            "NARRATION PREPARATION INTERRUPTED; "
            "completed scene checkpoints retained."
        )
        raise SystemExit(130)

    except Exception as exc:
        log("NARRATION PREPARATION FAILED:", exc)
        raise SystemExit(1)