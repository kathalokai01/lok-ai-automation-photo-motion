
#!/usr/bin/env python3
"""Prepare validated, resumable narration records without API calls."""

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
OUTPUT_FILE = ROOT / "output" / "narration" / "narration.json"

VERSION = 5


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    if not isinstance(value, bytes):
        value = str(value).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def canonical_json(data):
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")

    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file is missing: {path}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        print(f"WARNING: Cannot reuse {path}: {exc}")
        return None

    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected a JSON object: {path}")
        return None

    return data


def parse_bool(value, default=True):
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


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
    for field in ("narration", "text"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()
    return ""


def count_words(text):
    """Count Latin words/numbers and Devanagari word groups."""
    return len(
        re.findall(
            r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*|"
            r"[\u0900-\u097F]+",
            str(text or ""),
        )
    )


def extract_ai_story(data):
    result = []
    parts = data.get("parts", [])

    if not isinstance(parts, list):
        raise RuntimeError("ai_story.json parts must be a list.")

    for expected_part, part_data in enumerate(parts, 1):
        if not isinstance(part_data, dict):
            raise RuntimeError("AI story contains an invalid part.")

        try:
            part_number = int(part_data.get("part", expected_part))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "AI story contains an invalid part number."
            ) from exc

        if part_number != expected_part:
            raise RuntimeError(f"Unexpected part number: {part_number}.")

        scenes = part_data.get("scenes", [])
        if not isinstance(scenes, list):
            raise RuntimeError("AI story part scenes must be a list.")

        for item in scenes:
            if not isinstance(item, dict):
                raise RuntimeError("AI story contains an invalid scene.")
            row = dict(item)
            row["part"] = part_number
            result.append(row)

    return result


def load_source_scenes():
    # Prefer the scene-level output when it exists.
    if SCENES_FILE.is_file():
        data = read_json(SCENES_FILE, required=False)

        if isinstance(data, dict):
            scenes = data.get("scenes", [])

            if not isinstance(scenes, list):
                raise RuntimeError("scenes.json scenes must be a list.")

            if scenes:
                if not all(isinstance(item, dict) for item in scenes):
                    raise RuntimeError(
                        "scenes.json contains an invalid scene."
                    )

                return (
                    scenes,
                    SCENES_FILE.relative_to(ROOT).as_posix(),
                )

    data = read_json(AI_STORY_FILE)

    if data.get("status") != "completed":
        raise RuntimeError("AI story is not marked completed.")

    scenes = extract_ai_story(data)

    if not scenes:
        raise RuntimeError("No scenes found in the AI story.")

    return scenes, AI_STORY_FILE.relative_to(ROOT).as_posix()


def normalize_settings(config):
    return {
        "format": get_format(config),
        "parts": int(get_parts(config)),
        "scenes_per_part": int(get_scenes(config)),
        "voice": str(config.get("VOICE", "male")).strip(),
        "speed": str(config.get("SPEED", "+0%")).strip(),
        "captions": str(config.get("CAPTIONS", "hindi")).strip().lower(),
        "audience": str(config.get("AUDIENCE", "adult")).strip(),
        "scene_duration": str(
            config.get("SCENE_DURATION", "auto")
        ).strip(),
        "story_length": str(
            config.get("STORY_LENGTH", "auto")
        ).strip(),
        "music": parse_bool(config.get("MUSIC"), True),
        "sfx": parse_bool(config.get("SFX"), True),
        "ambient_sound": parse_bool(config.get("AMBIENT_SOUND"), True),
        "transitions": str(
            config.get("TRANSITIONS", "cinematic")
        ).strip(),
    }


def validate_scenes(source, settings):
    expected = {
        (part, scene)
        for part in range(1, settings["parts"] + 1)
        for scene in range(1, settings["scenes_per_part"] + 1)
    }
    mapped = {}

    for index, item in enumerate(source, start=1):
        key = scene_key(item)

        if key is None:
            raise RuntimeError(
                f"Invalid scene mapping at source row {index}."
            )

        if key in mapped:
            raise RuntimeError(f"Duplicate scene mapping: {key}.")

        if key not in expected:
            raise RuntimeError(f"Unexpected scene mapping: {key}.")

        text = narration_text(item)

        if not text:
            raise RuntimeError(
                f"Empty narration for part {key[0]}, scene {key[1]}."
            )

        mapped[key] = item

    missing = sorted(expected - set(mapped))

    if missing:
        raise RuntimeError(
            f"Missing required narration scenes: {missing}"
        )

    return mapped


def estimate_narration_seconds(text, voice, speed):
    """Planning estimate only; final timing must come from rendered audio."""
    words = count_words(text)
    voice_key = str(voice or "").strip().lower()
    wpm = 135.0 if voice_key in {"female", "woman"} else 125.0

    match = re.fullmatch(
        r"([+-]?)\s*(\d+(?:\.\d+)?)\s*%",
        str(speed or "").strip(),
    )

    if match:
        amount = float(match.group(2)) / 100.0

        if match.group(1) == "-":
            factor = max(0.5, 1.0 - amount)
        else:
            factor = min(1.75, 1.0 + amount)

        wpm *= factor

    return round(words * 60.0 / max(wpm, 1.0), 2)


def make_record(source, settings):
    key = scene_key(source)
    text = narration_text(source)
    text_hash = digest(text)

    duration = source.get("duration")

    if duration is None or not str(duration).strip():
        duration = settings["scene_duration"]

    estimated_seconds = estimate_narration_seconds(
        text,
        settings["voice"],
        settings["speed"],
    )

    return {
        "part": key[0],
        "scene": key[1],
        "scene_id": f"part_{key[0]:02d}_scene_{key[1]:02d}",
        "text": text,
        "text_sha256": text_hash,
        "word_count": count_words(text),
        "voice": settings["voice"],
        "speed": settings["speed"],
        "captions": settings["captions"],
        "format": settings["format"],
        "audience": settings["audience"],
        "duration": duration,
        "estimated_duration": duration,
        "estimated_duration_seconds": estimated_seconds,
        "duration_estimate_note": (
            "Planning estimate only; use measured rendered audio duration "
            "for final captions and scene timing."
        ),
        "status": "completed",
    }


def load_reusable(settings_hash, source_map, settings):
    old = read_json(OUTPUT_FILE, required=False)

    if not old or old.get("status") not in {"in_progress", "completed"}:
        return {}

    if old.get("settings_sha256") != settings_hash:
        print(
            "Settings or source changed; previous narration "
            "will not be reused."
        )
        return {}

    rows = old.get("scenes", [])

    if not isinstance(rows, list):
        return {}

    reusable = {}

    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "completed":
            continue

        key = scene_key(row)

        if key not in source_map:
            continue

        expected = make_record(source_map[key], settings)

        fields = (
            "scene_id",
            "text_sha256",
            "text",
            "voice",
            "speed",
            "captions",
            "format",
            "audience",
            "duration",
            "estimated_duration",
            "estimated_duration_seconds",
            "duration_estimate_note",
            "word_count",
        )

        if all(row.get(field) == expected.get(field) for field in fields):
            reusable[key] = expected

    return reusable


def main():
    print("=" * 56)
    print("KATHA LOK AI — NARRATION PREPARATION")
    print("Story/TTS API calls in this stage: 0")
    print("=" * 56)

    config = load_and_validate(CONFIG_FILE)
    settings = normalize_settings(config)

    if settings["parts"] < 1 or settings["scenes_per_part"] < 1:
        raise RuntimeError("PARTS and SCENES must both be positive.")

    total = settings["parts"] * settings["scenes_per_part"]
    source, source_path = load_source_scenes()
    source_map = validate_scenes(source, settings)

    topic = get_topic(config)

    if not topic:
        story_text = get_story_text(config)
        topic = story_text.splitlines()[0].strip() if story_text else ""

    if not topic:
        story = read_json(AI_STORY_FILE, required=False) or {}
        topic = str(
            story.get("title") or story.get("topic") or ""
        ).strip()

    source_fingerprint = digest(canonical_json([
        {
            "part": part,
            "scene": scene,
            "scene_id": source_map[(part, scene)].get("scene_id"),
            "text_sha256": digest(
                narration_text(source_map[(part, scene)])
            ),
        }
        for part, scene in sorted(source_map)
    ]))

    settings_hash = digest(canonical_json({
        "version": VERSION,
        "settings": settings,
        "source_fingerprint": source_fingerprint,
        "source_path": source_path,
    }))

    reusable = load_reusable(settings_hash, source_map, settings)
    records = dict(reusable)

    output = {
        "version": VERSION,
        "status": "in_progress",
        "topic": topic,
        "model": "not_required",
        "api_calls": 0,
        "total_parts": settings["parts"],
        "scenes_per_part": settings["scenes_per_part"],
        "total_scenes": total,
        "settings_sha256": settings_hash,
        "source_sha256": source_fingerprint,
        "source_file": source_path,
        "input_config": settings,
        "scenes": [records[key] for key in sorted(records)],
        "completed_scenes": len(records),
        "updated_at": now(),
    }

    save_json(OUTPUT_FILE, output)

    print(f"Format: {settings['format']}")
    print(f"Expected scenes: {total}")
    print(f"Source: {source_path}")
    print(f"Reusable scenes: {len(reusable)}")

    for index, key in enumerate(sorted(source_map), start=1):
        if key in records:
            print(
                f"[{index}/{total}] Reuse Part {key[0]} Scene {key[1]}"
            )
        else:
            records[key] = make_record(source_map[key], settings)
            print(
                f"[{index}/{total}] Prepare Part {key[0]} Scene {key[1]}"
            )

        output["scenes"] = [
            records[item] for item in sorted(records)
        ]
        output["completed_scenes"] = len(records)
        output["last_completed"] = {
            "part": key[0],
            "scene": key[1],
            "scene_id": f"part_{key[0]:02d}_scene_{key[1]:02d}",
        }
        output["updated_at"] = now()
        save_json(OUTPUT_FILE, output)

    final_rows = [records[key] for key in sorted(source_map)]
    final_keys = [scene_key(row) for row in final_rows]

    if len(final_rows) != total or len(set(final_keys)) != total:
        raise RuntimeError("Final narration mapping/count is invalid.")

    for row in final_rows:
        if not row["text"].strip():
            raise RuntimeError(
                f"Final narration is empty: {row['scene_id']}."
            )

        if row["word_count"] != count_words(row["text"]):
            raise RuntimeError(
                f"Narration word-count mismatch: {row['scene_id']}."
            )

        if row["estimated_duration_seconds"] <= 0:
            raise RuntimeError(
                f"Invalid duration estimate: {row['scene_id']}."
            )

        if row["text_sha256"] != digest(row["text"]):
            raise RuntimeError(
                f"Narration hash mismatch: {row['scene_id']}."
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
            "narration_hashes_verified": True,
            "word_counts_verified": True,
            "duration_estimates_are_not_actual_audio_timings": True,
        },
    })

    save_json(OUTPUT_FILE, output)

    print("=" * 56)
    print("NARRATION PREPARATION COMPLETED")
    print(f"Scenes prepared: {total}")
    print(
        "Words across narration: "
        f"{sum(row['word_count'] for row in final_rows)}"
    )
    print("Story/TTS API calls in this stage: 0")
    print(f"Output: {OUTPUT_FILE}")
    print("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Narration preparation cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
