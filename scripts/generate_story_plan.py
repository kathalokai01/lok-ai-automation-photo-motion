#!/usr/bin/env python3
"""Deterministic offline story planning with validated checkpoint reuse."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_topic,
    get_story_text,
    get_title_source,
)

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_FILE = ROOT / "output" / "story" / "story_plan.json"
VERSION = "offline-structural-story-plan-v2"

SHORT_BEATS = (
    ("hook", "Open with a compelling story-specific mystery or surprise."),
    ("setup", "Establish character, place, goal and stakes."),
    ("discovery", "Reveal a meaningful clue or new information."),
    ("escalation", "Increase pressure and consequences."),
    ("obstacle", "Introduce a credible obstacle."),
    ("reversal", "Change the audience's understanding."),
    ("choice", "Force a consequential decision."),
    ("confrontation", "Bring the central conflict into focus."),
    ("reveal", "Reveal a significant truth."),
    ("climax", "Deliver the strongest turning point."),
    ("payoff", "Pay off an earlier clue or promise."),
    ("suspense", "Create a compelling continuation question."),
)

FULL_BEATS = (
    ("hook", "Open with strong cinematic tension."),
    ("establishing", "Establish the world, characters and stakes."),
    ("inciting_event", "Introduce the event disrupting ordinary life."),
    ("goal", "Clarify the protagonist's goal."),
    ("investigation", "Develop clues and motivations."),
    ("first_obstacle", "Introduce meaningful opposition."),
    ("complication", "Raise the stakes."),
    ("relationship", "Develop relationships through consequential action."),
    ("discovery", "Reveal information that changes the situation."),
    ("midpoint", "Create a major reversal."),
    ("pressure", "Increase urgency."),
    ("setback", "Deliver a believable setback."),
    ("reassessment", "Force a change of approach."),
    ("preparation", "Prepare for the decisive confrontation."),
    ("approach", "Move toward the central conflict."),
    ("confrontation", "Bring opposing goals into direct conflict."),
    ("crisis", "Force a difficult choice."),
    ("climax", "Resolve the central struggle through decisive action."),
    ("aftermath", "Show the consequences."),
    ("resolution", "Provide emotional closure."),
    ("final_suspense", "Add continuation suspense only when appropriate."),
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(data: Any) -> str:
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def as_bool(config: dict, key: str, default: bool = True) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    value = str(value).strip().lower()
    if value in {"true", "yes", "on", "1", "enabled"}:
        return True
    if value in {"false", "no", "off", "0", "disabled", ""}:
        return False
    return default


def select_beat(format_type: str, index: int, total: int):
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS
    if total <= 1:
        return beats[0]
    position = round(index * (len(beats) - 1) / (total - 1))
    return beats[max(0, min(position, len(beats) - 1))]


def build_plan(
    config: dict,
    format_type: str,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    total = parts_count * scenes_per_part
    part_hook = as_bool(config, "PART_HOOK", True)
    part_suspense = as_bool(config, "PART_SUSPENSE", True)
    final_resolution = as_bool(config, "FINAL_RESOLUTION", True)
    parts = []
    global_number = 0

    for part_number in range(1, parts_count + 1):
        scenes = []

        for scene_number in range(1, scenes_per_part + 1):
            global_number += 1
            beat, description = select_beat(
                format_type, global_number - 1, total
            )
            is_first = scene_number == 1
            is_last = scene_number == scenes_per_part
            is_final_part = part_number == parts_count
            is_final_scene = is_last and is_final_part

            # Final resolution wins over generic part-ending suspense.
            if is_final_scene and final_resolution:
                role = "finale"
                beat = "finale"
                description = (
                    "Resolve the central conflict, answer the main story "
                    "question, and provide a satisfying, believable ending."
                )
            elif is_first and (part_number == 1 or part_hook):
                role = "part_opening"
                beat = "hook" if part_number == 1 else "part_hook"
                description = (
                    "Open with an immediate story-specific hook. Later parts "
                    "must connect naturally to the previous part."
                )
            elif is_last and part_suspense:
                role = "part_ending"
                beat = "suspense"
                description = (
                    "End on meaningful story-specific suspense that encourages "
                    "continuation without inventing unsupported plot details."
                )
            else:
                role = "story_progression"

            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )
            scenes.append({
                "scene": scene_number,
                "global_scene_number": global_number,
                "scene_id": scene_id,
                "role": role,
                "beat": beat,
                "beat_description": description,
                "purpose": description,
            })

        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "scenes": scenes,
        })

    source = {
        "script_version": VERSION,
        "input_file": "Input/topic.txt",
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
        "part_hook": part_hook,
        "part_suspense": part_suspense,
        "final_resolution": final_resolution,
        "parts": parts,
    }

    return {
        "status": "planned",
        "script_version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "topic": topic,
        "title": get_title_source(config),
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
        "scene_order": "part_then_scene",
        "mapping_version": 4,
        "source_fingerprint": fingerprint(source),
        "source": {
            "input_file": "Input/topic.txt",
            "story_text_provided": bool(story_text),
            "planning_mode": "offline_structural",
            "api_calls": 0,
        },
        "parts": parts,
    }


def validate_plan(
    plan: dict,
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> None:
    if not isinstance(plan, dict):
        raise RuntimeError("Story plan must be a JSON object.")

    expected_total = parts_count * scenes_per_part
    expected_values = {
        "format": format_type,
        "topic": topic,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "scene_order": "part_then_scene",
    }
    for key, expected in expected_values.items():
        if plan.get(key) != expected:
            raise RuntimeError(
                f"Story plan mismatch for {key}: "
                f"expected {expected!r}, got {plan.get(key)!r}."
            )

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has an incorrect number of parts.")

    global_number = 0
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(f"Invalid part numbering at part {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must contain exactly "
                f"{scenes_per_part} scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene: {expected_id}.")
            if scene.get("scene") != scene_number:
                raise RuntimeError(f"Incorrect local scene number: {expected_id}.")
            if scene.get("global_scene_number") != global_number:
                raise RuntimeError(f"Incorrect global scene number: {expected_id}.")
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Incorrect scene ID: {expected_id}.")
            if not str(scene.get("purpose", "")).strip():
                raise RuntimeError(f"Missing scene purpose: {expected_id}.")


def main() -> None:
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input file is missing: {INPUT_FILE}")

    config = load_and_validate(str(INPUT_FILE))
    format_type = get_format(config)
    topic = get_topic(config)
    story_text = get_story_text(config)
    parts_count = get_parts(config)
    scenes_per_part = get_scenes(config)

    if not topic and not story_text:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    plan = build_plan(
        config,
        format_type,
        topic,
        story_text,
        parts_count,
        scenes_per_part,
    )
    validate_plan(
        plan, format_type, topic, parts_count, scenes_per_part
    )

    old_plan = read_json(OUTPUT_FILE)
    if (
        isinstance(old_plan, dict)
        and old_plan.get("source_fingerprint") == plan["source_fingerprint"]
        and old_plan.get("script_version") == VERSION
    ):
        try:
            validate_plan(
                old_plan, format_type, topic, parts_count, scenes_per_part
            )
            log("Matching valid story-plan checkpoint preserved.")
            log("External API calls: 0")
            return
        except (RuntimeError, TypeError, ValueError) as exc:
            log("Existing plan is invalid; rebuilding:", exc)

    write_json(OUTPUT_FILE, plan)
    log("STORY PLAN: SUCCESS")
    log("Format:", format_type)
    log("Topic:", topic or "(story text only)")
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", parts_count * scenes_per_part)
    log("Output:", OUTPUT_FILE)
    log("External API calls: 0")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
