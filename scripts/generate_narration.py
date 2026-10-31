#!/usr/bin/env python3
"""Validated, resumable narration preparation for Katha Lok AI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_parts,
    get_scenes,
    get_format,
    get_topic,
    get_story_text,
)

CONFIG_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = ROOT / "output" / "scenes" / "scenes.json"
AI_STORY_FILE = ROOT / "output" / "story" / "ai_story.json"
SOURCE_STORY_FILE = ROOT / "output" / "story" / "story.json"

OUTPUT_DIR = ROOT / "output" / "narration"
OUTPUT_FILE = OUTPUT_DIR / "narration.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "narration_progress.json"

VERSION = "narration-v8-validated-scene-checkpoints"

CAPTION_FIELDS = (
    "caption_hindi", "hindi_caption", "caption_hi",
    "caption_english", "english_caption", "caption_en",
    "caption_hinglish", "hinglish_caption", "caption_mixed",
    "caption_text", "display_caption", "caption_language",
    "caption_lang", "language", "narration_language",
    "translations", "caption_texts",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def log(*values):
    print("[NARRATION]", *values, flush=True)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def digest(value):
    if not isinstance(value, bytes):
        value = str(value).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def save_json(path, data):
    path = Path(path)
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
        temporary.unlink(missing_ok=True)


def read_json(path, required=True):
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON: {path}: {exc}") from exc
        return None

    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected JSON object: {path}")
        return None

    return data


def truthy(value, default=True):
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def scene_key(item):
    try:
        part = int(item["part"])
        scene = int(item.get("scene", item.get("local_scene")))
    except (KeyError, TypeError, ValueError):
        return None

    if part < 1 or scene < 1:
        return None

    return part, scene


def narration_text(item):
    for field in ("narration", "text", "dialogue"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return clean(value)
    return ""


def count_words(value):
    return len(re.findall(
        r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*|[\u0900-\u097F]+",
        str(value or ""),
    ))


def normalize_settings(config):
    return {
        "format": clean(get_format(config)).lower(),
        "parts": int(get_parts(config)),
        "scenes_per_part": int(get_scenes(config)),
        "voice": clean(config.get("VOICE", "male")),
        "speed": clean(config.get("SPEED", "+0%")),
        "captions": clean(config.get("CAPTIONS", "hindi")).lower(),
        "audience": clean(config.get("AUDIENCE", "adult")),
        "scene_duration": clean(config.get("SCENE_DURATION", "auto")),
        "story_length": clean(config.get("STORY_LENGTH", "auto")),
        "music": truthy(config.get("MUSIC"), True),
        "sfx": truthy(config.get("SFX"), True),
        "ambient_sound": truthy(config.get("AMBIENT_SOUND"), True),
        "transitions": clean(config.get("TRANSITIONS", "cinematic")),
    }


def extract_story_scenes(data):
    parts = data.get("parts")

    if not isinstance(parts, list):
        raise RuntimeError("Story parts must be a list.")

    rows = []

    for expected_part, part_data in enumerate(parts, 1):
        if not isinstance(part_data, dict):
            raise RuntimeError("Invalid story part.")

        try:
            part_number = int(part_data.get("part", expected_part))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid story part number.") from exc

        if part_number != expected_part:
            raise RuntimeError(
                f"Story part order mismatch: expected {expected_part}, "
                f"found {part_number}."
            )

        scenes = part_data.get("scenes")

        if not isinstance(scenes, list):
            raise RuntimeError(
                f"Scenes list missing in story part {part_number}."
            )

        for scene in scenes:
            if not isinstance(scene, dict):
                raise RuntimeError("Invalid story scene record.")

            row = dict(scene)
            row["part"] = part_number
            rows.append(row)

    return rows


def load_source_scenes(format_type):
    if SCENES_FILE.is_file():
        data = read_json(SCENES_FILE, required=False)

        if isinstance(data, dict):
            if data.get("status") == "completed":
                source_format = clean(data.get("format")).lower()

                if source_format and source_format != format_type:
                    raise RuntimeError(
                        "scenes.json FORMAT does not match Input/topic.txt."
                    )

                rows = data.get("scenes")

                if not isinstance(rows, list) or not rows:
                    raise RuntimeError(
                        "Completed scenes.json has no scene records."
                    )

                if not all(isinstance(row, dict) for row in rows):
                    raise RuntimeError(
                        "scenes.json contains invalid scene records."
                    )

                return rows, SCENES_FILE.relative_to(ROOT).as_posix()

            log("Ignoring scenes.json because it is not completed.")

    for path in (AI_STORY_FILE, SOURCE_STORY_FILE):
        data = read_json(path, required=False)

        if not isinstance(data, dict):
            continue

        if data.get("status") != "completed":
            continue

        story_format = clean(data.get("format")).lower()

        if story_format != format_type:
            continue

        rows = extract_story_scenes(data)

        if rows:
            return rows, path.relative_to(ROOT).as_posix()

    raise RuntimeError(
        "No completed scene/story source matches the selected FORMAT."
    )


def validate_scenes(source, settings):
    expected = {
        (part, scene)
        for part in range(1, settings["parts"] + 1)
        for scene in range(1, settings["scenes_per_part"] + 1)
    }

    mapped = {}

    for index, item in enumerate(source, 1):
        key = scene_key(item)

        if key is None:
            raise RuntimeError(
                f"Invalid part/scene mapping in source row {index}."
            )

        if key not in expected:
            raise RuntimeError(f"Unexpected scene mapping: {key}.")

        if key in mapped:
            raise RuntimeError(f"Duplicate scene mapping: {key}.")

        if not narration_text(item):
            raise RuntimeError(
                f"Narration missing: Part {key[0]}, Scene {key[1]}."
            )

        mapped[key] = item

    missing = sorted(expected - set(mapped))

    if missing:
        raise RuntimeError(f"Required scenes missing: {missing}")

    return mapped


def estimate_duration(text, voice, speed):
    """Estimate only; actual caption timing must use rendered audio."""
    words = count_words(text)
    wpm = 135.0 if voice.lower() in {"female", "woman"} else 125.0

    match = re.fullmatch(
        r"([+-]?)\s*(\d+(?:\.\d+)?)\s*%",
        speed.strip(),
    )

    if match:
        amount = float(match.group(2)) / 100.0

        if match.group(1) == "-":
            factor = max(0.5, 1.0 - amount)
        else:
            factor = min(1.75, 1.0 + amount)

        wpm *= factor

    return round(words * 60.0 / max(wpm, 1.0), 2)


def caption_metadata(source):
    result = {}

    for field in CAPTION_FIELDS:
        if field not in source:
            continue

        value = source[field]

        if isinstance(value, str):
            value = clean(value)

        result[field] = value

    return result


def make_record(source, settings):
    key = scene_key(source)
    text = narration_text(source)

    duration = source.get("duration")

    if duration is None or not clean(duration):
        duration = settings["scene_duration"]

    metadata = caption_metadata(source)
    estimated = estimate_duration(
        text, settings["voice"], settings["speed"]
    )

    record = {
        "part": key[0],
        "scene": key[1],
        "scene_id": f"part_{key[0]:02d}_scene_{key[1]:02d}",
        "text": text,
        "narration": text,
        "text_sha256": digest(text),
        "word_count": count_words(text),
        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],
        "format": settings["format"],
        "audience": settings["audience"],
        "duration": duration,
        "estimated_duration": estimated,
        "estimated_duration_seconds": estimated,
        "duration_estimate_note": (
            "Estimate only. Final timing must use measured audio duration."
        ),
        "caption_fields_sha256": digest(canonical(metadata)),
        "status": "completed",
    }

    record.update(metadata)
    return record


def source_fingerprint(source_map):
    ordered = []

    for key in sorted(source_map):
        ordered.append({
            "part": key[0],
            "scene": key[1],
            "source": source_map[key],
        })

    return digest(canonical(ordered))


def load_reusable(settings_hash, source_map, settings):
    old = read_json(OUTPUT_FILE, required=False)

    if not isinstance(old, dict):
        return {}

    if old.get("settings_sha256") != settings_hash:
        log("Previous narration cache does not match current input.")
        return {}

    rows = old.get("scenes")

    if not isinstance(rows, list):
        return {}

    reusable = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        if row.get("status") != "completed":
            continue

        key = scene_key(row)

        if key not in source_map:
            continue

        expected = make_record(source_map[key], settings)

        if row == expected:
            reusable[key] = expected

    return reusable


def resolve_title(config):
    explicit_topic = clean(get_topic(config))

    if explicit_topic:
        return explicit_topic

    for path in (AI_STORY_FILE, SOURCE_STORY_FILE):
        data = read_json(path, required=False)

        if not isinstance(data, dict):
            continue

        for field in (
            "final_title", "generated_title",
            "story_title", "title", "topic",
        ):
            title = clean(data.get(field))
            if title:
                return title

    # Never use the first sentence of a pasted story as its title.
    return ""


def checkpoint(output, records, ordered_keys, last_key=None):
    output["scenes"] = [
        records[key] for key in ordered_keys if key in records
    ]
    output["completed_scenes"] = len(output["scenes"])
    output["updated_at"] = now()

    if last_key:
        output["last_completed"] = {
            "part": last_key[0],
            "scene": last_key[1],
            "scene_id": (
                f"part_{last_key[0]:02d}_scene_{last_key[1]:02d}"
            ),
        }

    save_json(OUTPUT_FILE, output)

    save_json(CHECKPOINT_FILE, {
        "status": "in_progress",
        "version": VERSION,
        "settings_sha256": output["settings_sha256"],
        "source_sha256": output["source_sha256"],
        "total_scenes": output["total_scenes"],
        "completed_count": output["completed_scenes"],
        "completed_scene_ids": [
            row["scene_id"] for row in output["scenes"]
        ],
        "last_completed": output.get("last_completed"),
        "updated_at": now(),
    })


def main():
    log("=" * 55)
    log("KATHA LOK AI — NARRATION PREPARATION")
    log("External API calls: 0")
    log("=" * 55)

    config = load_and_validate(CONFIG_FILE)
    settings = normalize_settings(config)

    if settings["format"] not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")

    if settings["parts"] < 1 or settings["scenes_per_part"] < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")

    total = settings["parts"] * settings["scenes_per_part"]

    source, source_path = load_source_scenes(settings["format"])
    source_map = validate_scenes(source, settings)

    title = resolve_title(config)
    story_text = clean(get_story_text(config))

    source_hash = source_fingerprint(source_map)

    settings_hash = digest(canonical({
        "version": VERSION,
        "settings": settings,
        "title": title,
        "source_hash": source_hash,
        "source_path": source_path,
        "story_text": story_text,
    }))

    ordered_keys = sorted(source_map)
    reusable = load_reusable(settings_hash, source_map, settings)
    records = dict(reusable)

    output = {
        "version": VERSION,
        "status": "in_progress",
        "topic": title,
        "title": title,
        "format": settings["format"],
        "model": "not_required",
        "api_calls": 0,
        "total_parts": settings["parts"],
        "scenes_per_part": settings["scenes_per_part"],
        "total_scenes": total,
        "settings_sha256": settings_hash,
        "source_sha256": source_hash,
        "source_file": source_path,
        "input_config": settings,
        "scenes": [],
        "completed_scenes": 0,
        "updated_at": now(),
    }

    checkpoint(output, records, ordered_keys)

    log("FORMAT:", settings["format"])
    log("PARTS:", settings["parts"])
    log("SCENES PER PART:", settings["scenes_per_part"])
    log("TOTAL SCENES:", total)
    log("SOURCE:", source_path)
    log("REUSED:", len(reusable))

    for index, key in enumerate(ordered_keys, 1):
        if key in records:
            log(
                f"[{index}/{total}] Reusing "
                f"Part {key[0]} Scene {key[1]}"
            )
        else:
            records[key] = make_record(source_map[key], settings)
            log(
                f"[{index}/{total}] Prepared "
                f"Part {key[0]} Scene {key[1]}"
            )

        checkpoint(output, records, ordered_keys, key)

    final_rows = [records[key] for key in ordered_keys]

    if len(final_rows) != total:
        raise RuntimeError("Final narration scene count mismatch.")

    actual_keys = [scene_key(row) for row in final_rows]

    if len(set(actual_keys)) != total or actual_keys != ordered_keys:
        raise RuntimeError("Final narration scene mapping is invalid.")

    for row in final_rows:
        if not row["text"]:
            raise RuntimeError(f"Empty narration: {row['scene_id']}")

        if row["text_sha256"] != digest(row["text"]):
            raise RuntimeError(f"Text hash mismatch: {row['scene_id']}")

        if row["word_count"] != count_words(row["text"]):
            raise RuntimeError(f"Word count mismatch: {row['scene_id']}")

        if row["estimated_duration_seconds"] <= 0:
            raise RuntimeError(
                f"Invalid duration estimate: {row['scene_id']}"
            )

        metadata = caption_metadata(row)

        if row["caption_fields_sha256"] != digest(canonical(metadata)):
            raise RuntimeError(
                f"Caption metadata mismatch: {row['scene_id']}"
            )

    output.update({
        "status": "completed",
        "scenes": final_rows,
        "completed_scenes": total,
        "api_calls": 0,
        "completed_at": now(),
        "updated_at": now(),
        "validation": {
            "exact_scene_count": True,
            "unique_part_scene_mapping": True,
            "sequential_scene_order": True,
            "narration_hashes_verified": True,
            "word_counts_verified": True,
            "caption_fields_preserved": True,
            "duration_is_estimate_not_audio_measurement": True,
            "generated_title_preferred_over_story_first_line": True,
        },
    })

    save_json(OUTPUT_FILE, output)

    save_json(CHECKPOINT_FILE, {
        "status": "completed",
        "version": VERSION,
        "settings_sha256": settings_hash,
        "source_sha256": source_hash,
        "total_scenes": total,
        "completed_count": total,
        "completed_scene_ids": [
            row["scene_id"] for row in final_rows
        ],
        "completed_at": now(),
    })

    log("NARRATION PREPARATION: SUCCESS")
    log("Scenes:", total)
    log("Reused:", len(reusable))
    log("API calls: 0")
    log("Output:", OUTPUT_FILE)
    log("Checkpoint:", CHECKPOINT_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("ERROR:", exc)
        raise SystemExit(1)