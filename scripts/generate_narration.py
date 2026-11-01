#!/usr/bin/env python3
"""Prepare validated, resumable scene-wise narration records.

This stage does not call a TTS or story-generation API. Actual audio duration
is measured later by generate_tts.py and must not be confused with estimates.
"""

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

from input_config import (
    load_input_config,
    get_parts,
    get_scenes,
    normalize_format,
    get_topic,
)

SCENES_FILE = Path("output/scenes/scenes.json")
AI_STORY_FILE = Path("output/story/ai_story.json")
OUTPUT_FILE = Path("output/narration/narration.json")

SCRIPT_VERSION = 4
MIN_SCENE_DURATION = 0.5
MAX_SCENE_DURATION = 3600.0
WORDS_PER_MINUTE = {
    "kids": 115,
    "adult": 145,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file not found: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


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

    return default


def positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be numeric.") from exc

    if not math.isfinite(number):
        raise RuntimeError(f"{label} must be finite.")

    if not MIN_SCENE_DURATION <= number <= MAX_SCENE_DURATION:
        raise RuntimeError(
            f"{label} must be between {MIN_SCENE_DURATION} "
            f"and {MAX_SCENE_DURATION} seconds."
        )

    return round(number, 3)


def scene_key(scene: dict) -> tuple[int, int] | None:
    try:
        part = int(scene["part"])
        number = int(scene["scene"])
    except (KeyError, TypeError, ValueError):
        return None

    if part < 1 or number < 1:
        return None

    return part, number


def clean_narration(value: Any, key: tuple[int, int]) -> str:
    if not isinstance(value, str):
        raise RuntimeError(
            f"Narration must be text: Part {key[0]} Scene {key[1]}."
        )

    text = value.replace("\ufeff", "").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    if not text:
        raise RuntimeError(
            f"Empty narration: Part {key[0]} Scene {key[1]}."
        )

    return text


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


def estimate_speech_duration(
    text: str,
    audience: str,
    speed: str,
) -> float:
    """A planning estimate only; never presented as measured audio duration."""
    words = count_words(text)
    base_wpm = WORDS_PER_MINUTE.get(audience, 145)

    try:
        speed_percent = int(str(speed).replace("%", "").strip())
    except (TypeError, ValueError):
        speed_percent = 0

    rate = base_wpm * (1.0 + speed_percent / 100.0)
    rate = max(40.0, rate)

    sentence_pauses = len(re.findall(r"[.!?।॥]+", text))
    comma_pauses = len(re.findall(r"[,;:]", text))

    estimate = (words / rate) * 60.0
    estimate += sentence_pauses * 0.22
    estimate += comma_pauses * 0.08

    return round(max(0.5, estimate), 3)


def read_scene_duration(scene: dict, fallback: str) -> float | str:
    """Keep configured scene duration separate from estimated speech length."""
    for field in (
        "duration",
        "scene_duration",
        "duration_seconds",
        "seconds",
    ):
        value = scene.get(field)

        if value is None or str(value).strip() == "":
            continue

        return positive_number(value, f"Scene {field}")

    if fallback == "fixed":
        raise RuntimeError(
            "SCENE_DURATION is fixed but a scene has no declared duration."
        )

    return "auto"


def load_story_metadata() -> dict:
    value = load_json(AI_STORY_FILE, required=False)
    return value if isinstance(value, dict) else {}


def resolve_title(config: dict, story: dict) -> str:
    explicit_title = cfg_text(config, "TITLE")
    topic = get_topic(config)

    if explicit_title:
        return explicit_title
    if topic:
        return topic

    for field in ("title", "generated_title", "topic"):
        value = story.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return "Untitled Story"


def validate_scene_collection(
    data: Any,
    parts: int,
    scenes_per_part: int,
) -> list[dict]:
    if not isinstance(data, dict):
        raise RuntimeError("scenes.json must contain a JSON object.")

    if data.get("status") != "completed":
        raise RuntimeError(
            "Scene generation is incomplete. Narration preparation stopped."
        )

    rows = data.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json contains no scene records.")

    expected = {
        (part, number)
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    }

    mapped: dict[tuple[int, int], dict] = {}

    for index, source in enumerate(rows, start=1):
        if not isinstance(source, dict):
            raise RuntimeError(f"Scene record {index} is not an object.")

        key = scene_key(source)
        if key is None:
            raise RuntimeError(
                f"Invalid part/scene mapping in scene record {index}."
            )

        if key not in expected:
            raise RuntimeError(f"Unexpected scene mapping: {key}.")

        if key in mapped:
            raise RuntimeError(f"Duplicate scene mapping: {key}.")

        item = dict(source)
        item["narration"] = clean_narration(
            source.get("narration"),
            key,
        )
        mapped[key] = item

    missing = sorted(expected - set(mapped))
    if missing:
        raise RuntimeError(
            f"Missing configured scenes ({len(missing)}): {missing[:25]}"
        )

    return [
        mapped[(part, number)]
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    ]


def build_settings(config: dict) -> dict:
    fmt = normalize_format(config)
    audience = cfg_text(config, "AUDIENCE", "adult").lower()

    if audience not in {"adult", "kids"}:
        raise RuntimeError("AUDIENCE must be adult or kids.")

    speed = cfg_text(config, "SPEED", "+0%")
    if not re.fullmatch(r"[+-]?\d{1,2}%", speed):
        raise RuntimeError("SPEED must be a percentage such as +0% or -10%.")

    speed_number = int(speed.replace("%", ""))
    if not -50 <= speed_number <= 50:
        raise RuntimeError("SPEED must be between -50% and +50%.")

    return {
        "format": fmt,
        "audience": audience,
        "voice": cfg_text(config, "VOICE", "male").lower(),
        "speed": speed,
        "captions": cfg_text(config, "CAPTIONS", "hindi").lower(),
        "scene_duration": cfg_text(config, "SCENE_DURATION", "auto").lower(),
        "parts": get_parts(config),
        "scenes_per_part": get_scenes(config),
        "music": cfg_bool(config, "MUSIC", True),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),
    }


def build_source_signature(
    title: str,
    settings: dict,
    source_scenes: list[dict],
) -> str:
    relevant_scenes = []

    for scene in source_scenes:
        relevant_scenes.append({
            "part": int(scene["part"]),
            "scene": int(scene["scene"]),
            "narration": scene["narration"],
            "duration": scene.get("duration"),
            "dialogue": scene.get("dialogue", ""),
            "character_ids": scene.get("character_ids", []),
        })

    return stable_hash({
        "script_version": SCRIPT_VERSION,
        "title": title,
        "settings": settings,
        "scenes": relevant_scenes,
    })


def build_narration_record(
    scene: dict,
    settings: dict,
    title: str,
) -> dict:
    key = scene_key(scene)
    if key is None:
        raise RuntimeError("Cannot build narration for an invalid scene.")

    text = clean_narration(scene.get("narration"), key)
    duration = read_scene_duration(
        scene,
        settings["scene_duration"],
    )

    estimated_duration = estimate_speech_duration(
        text,
        settings["audience"],
        settings["speed"],
    )

    dialogue = scene.get("dialogue", "")
    if not isinstance(dialogue, (str, list, dict)):
        dialogue = str(dialogue)

    character_ids = scene.get("character_ids", [])
    if not isinstance(character_ids, list):
        character_ids = []

    return {
        "part": key[0],
        "scene": key[1],
        "scene_id": str(
            scene.get("scene_id") or f"part_{key[0]:02d}_scene_{key[1]:03d}"
        ),
        "global_scene_number": (
            (key[0] - 1) * settings["scenes_per_part"] + key[1]
        ),
        "title": str(scene.get("title", "")).strip(),
        "story_title": title,
        "text": text,
        "word_count": count_words(text),
        "language": settings["captions"],
        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],
        "format": settings["format"],
        "audience": settings["audience"],
        "duration": duration,
        "scene_duration": duration,
        "narration_duration_estimate_seconds": estimated_duration,
        "estimated_duration": estimated_duration,
        "actual_audio_duration_seconds": None,
        "dialogue": dialogue,
        "character_ids": character_ids,
        "role": str(scene.get("role", "")).strip(),
        "status": "completed",
    }

def load_reusable_records(
    source_signature: str,
    settings: dict,
    enabled: bool,
) -> dict[tuple[int, int], dict]:
    if not enabled:
        return {}

    existing = load_json(OUTPUT_FILE, required=False)
    if not isinstance(existing, dict):
        return {}

    if existing.get("status") not in {"in_progress", "completed"}:
        return {}

    if existing.get("source_signature") != source_signature:
        print("Existing narration checkpoint does not match current scenes.")
        return {}

    if existing.get("input_config") != settings:
        print("Narration settings changed; old records will not be reused.")
        return {}

    rows = existing.get("scenes")
    if not isinstance(rows, list):
        return {}

    reusable: dict[tuple[int, int], dict] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        key = scene_key(row)
        if key is None:
            continue

        text = row.get("text")
        if (
            row.get("status") == "completed"
            and isinstance(text, str)
            and text.strip()
        ):
            reusable[key] = dict(row)

    return reusable


def reusable_record_matches(
    cached: dict,
    current: dict,
) -> bool:
    return (
        cached.get("text", "").strip() == current["text"].strip()
        and cached.get("part") == current["part"]
        and cached.get("scene") == current["scene"]
        and cached.get("voice") == current["voice"]
        and cached.get("speed") == current["speed"]
        and cached.get("captions") == current["captions"]
        and cached.get("format") == current["format"]
        and cached.get("audience") == current["audience"]
    )


def validate_final_records(
    records: list[dict],
    parts: int,
    scenes_per_part: int,
) -> None:
    expected = [
        (part, number)
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    ]

    actual = []

    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise RuntimeError(f"Final narration record {index} is invalid.")

        key = scene_key(record)
        if key is None:
            raise RuntimeError(f"Invalid final narration mapping at row {index}.")

        if not isinstance(record.get("text"), str) or not record["text"].strip():
            raise RuntimeError(f"Empty narration at Part {key[0]} Scene {key[1]}.")

        if record.get("status") != "completed":
            raise RuntimeError(f"Incomplete narration record: {key}.")

        actual.append(key)

    if actual != expected:
        raise RuntimeError(
            "Final narration scene order/count mismatch. "
            f"Expected {len(expected)} records; received {len(actual)}."
        )


def make_output_document(
    status: str,
    title: str,
    settings: dict,
    source_signature: str,
    records: list[dict],
    started_at: str,
) -> dict:
    now = utc_now()
    completed = status == "completed"

    return {
        "status": status,
        "script_version": SCRIPT_VERSION,
        "title": title,
        "topic": title,
        "format": settings["format"],
        "model": "not_required",
        "api_calls": 0,
        "source_file": SCENES_FILE.as_posix(),
        "source_signature": source_signature,
        "total_parts": settings["parts"],
        "scenes_per_part": settings["scenes_per_part"],
        "total_scenes": settings["parts"] * settings["scenes_per_part"],
        "completed_scenes": len(records),
        "input_config": settings,
        "duration_policy": {
            "scene_duration": (
                "configured_scene_duration"
                if settings["scene_duration"] == "fixed"
                else "source_scene_duration_or_auto"
            ),
            "narration_duration": (
                "estimate_only_until_tts_audio_is_generated"
            ),
            "actual_audio_duration_field": "actual_audio_duration_seconds",
        },
        "scenes": records,
        "started_at": started_at,
        "updated_at": now,
        "generated_at": now if completed else None,
        "completed_at": now if completed else None,
    }


def main() -> int:
    print("=" * 60)
    print("KATHA LOK AI - NARRATION PREPARATION")
    print("=" * 60)

    config = load_input_config()
    settings = build_settings(config)

    parts = settings["parts"]
    scenes_per_part = settings["scenes_per_part"]
    expected_total = parts * scenes_per_part

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
    source_scenes = validate_scene_collection(
        source_data,
        parts,
        scenes_per_part,
    )

    source_signature = build_source_signature(
        title,
        settings,
        source_scenes,
    )

    reusable = load_reusable_records(
        source_signature,
        settings,
        resume_enabled and skip_completed,
    )

    started_at = utc_now()
    narration_map: dict[tuple[int, int], dict] = {}

    print(f"FORMAT             : {settings['format']}")
    print(f"TITLE              : {title}")
    print(f"PARTS              : {parts}")
    print(f"SCENES PER PART    : {scenes_per_part}")
    print(f"TOTAL SCENES       : {expected_total}")
    print(f"VOICE              : {settings['voice']}")
    print(f"SPEED              : {settings['speed']}")
    print(f"CAPTIONS           : {settings['captions']}")
    print(f"RESUME              : {resume_enabled}")
    print(f"SKIP COMPLETED      : {skip_completed}")
    print("EXTERNAL API CALLS : 0")

    for index, source_scene in enumerate(source_scenes, start=1):
        key = scene_key(source_scene)
        if key is None:
            raise RuntimeError("Invalid scene mapping after source validation.")

        current = build_narration_record(
            source_scene,
            settings,
            title,
        )

        cached = reusable.get(key)
        if cached and reusable_record_matches(cached, current):
            # Keep the newly calculated metadata while retaining only
            # fields produced by later compatible pipeline stages.
            current["status"] = "completed"
            narration_map[key] = current
            print(
                f"[{index}/{expected_total}] Reused "
                f"Part {key[0]} Scene {key[1]}"
            )
        else:
            narration_map[key] = current
            print(
                f"[{index}/{expected_total}] Prepared "
                f"Part {key[0]} Scene {key[1]}"
            )

        if save_each_scene:
            partial = [
                narration_map[item]
                for item in sorted(narration_map)
            ]

            checkpoint = make_output_document(
                "in_progress",
                title,
                settings,
                source_signature,
                partial,
                started_at,
            )
            save_json_atomic(OUTPUT_FILE, checkpoint)

    final_records = [
        narration_map[(part, number)]
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    ]

    validate_final_records(
        final_records,
        parts,
        scenes_per_part,
    )

    output = make_output_document(
        "completed",
        title,
        settings,
        source_signature,
        final_records,
        started_at,
    )
    save_json_atomic(OUTPUT_FILE, output)

    print()
    print("=" * 60)
    print("NARRATION PREPARATION COMPLETE")
    print("=" * 60)
    print(f"Parts              : {parts}")
    print(f"Scenes             : {len(final_records)}")
    print(f"Estimated speech   : {sum(row['narration_duration_estimate_seconds'] for row in final_records):.1f}s")
    print("Measured audio     : Not available yet; generated by TTS stage.")
    print("External API calls : 0")
    print(f"Output             : {OUTPUT_FILE}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)