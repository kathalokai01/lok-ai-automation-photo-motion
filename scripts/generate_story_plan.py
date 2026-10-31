#!/usr/bin/env python3
"""Katha Lok AI — dynamic SHORT story planning."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
)

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_FILE = ROOT / "output" / "story" / "story_plan.json"

VERSION = "offline-structural-story-plan-v5-dynamic-short"

SHORT_MIN_WORDS = 120
SHORT_TARGET_WORDS = 135
SHORT_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180

SHORT_BEATS = (
    ("hook", "Open with immediate story-specific tension."),
    ("setup", "Establish character, location, goal and stakes."),
    ("discovery", "Introduce a meaningful clue."),
    ("escalation", "Increase pressure through story consequences."),
    ("obstacle", "Present a credible obstacle."),
    ("reversal", "Change the audience's understanding."),
    ("choice", "Show a consequential decision."),
    ("confrontation", "Bring the central conflict into focus."),
    ("reveal", "Reveal a meaningful established story fact."),
    ("climax", "Deliver a decisive turning point."),
    ("payoff", "Pay off an earlier clue or choice."),
    ("suspense", "End on a meaningful unresolved consequence."),
)

FULL_BEATS = (
    ("hook", "Open with strong story-specific tension."),
    ("establishing", "Establish setting, characters and stakes."),
    ("inciting_event", "Show the event that disrupts ordinary life."),
    ("goal", "Clarify the protagonist's goal."),
    ("investigation", "Develop clues and motivations."),
    ("first_obstacle", "Introduce credible opposition."),
    ("complication", "Raise the stakes."),
    ("relationship", "Develop relationships through action."),
    ("discovery", "Reveal information that changes the situation."),
    ("midpoint", "Create a meaningful reversal."),
    ("pressure", "Increase urgency."),
    ("setback", "Deliver a believable setback."),
    ("reassessment", "Force a change of approach."),
    ("preparation", "Prepare for confrontation."),
    ("approach", "Move toward the central conflict."),
    ("confrontation", "Bring opposing goals into conflict."),
    ("crisis", "Force a consequential choice."),
    ("climax", "Resolve the central struggle."),
    ("aftermath", "Show consequences."),
    ("resolution", "Provide narrative closure."),
    ("final_suspense", "Use suspense only when supported by the story."),
)


def log(*values: Any, file=None) -> None:
    print(*values, file=file or sys.stdout, flush=True)


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", clean_text(text)))


def split_sentences(text: str) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    return [
        item.strip()
        for item in re.split(r"(?<=[।!?！？.!])\s+", text)
        if item.strip()
    ]


def calculate_dynamic_short_parts(total_words: int) -> int:
    """Choose the smallest feasible count near 135 words per part."""
    if total_words < SHORT_MIN_WORDS:
        raise RuntimeError(
            f"SHORT story has only {total_words} words; "
            f"at least {SHORT_MIN_WORDS} are required."
        )

    minimum_parts = (
        total_words + SHORT_HARD_MAX_WORDS - 1
    ) // SHORT_HARD_MAX_WORDS

    maximum_parts = total_words // SHORT_MIN_WORDS

    if minimum_parts > maximum_parts:
        raise RuntimeError(
            f"{total_words} words cannot be divided into parts of "
            f"{SHORT_MIN_WORDS}–{SHORT_HARD_MAX_WORDS} words. "
            "Story expansion or rewrite is required."
        )

    preferred = round(total_words / SHORT_TARGET_WORDS)
    return max(minimum_parts, min(preferred, maximum_parts))


def read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def story_anchors(
    story_text: str,
    topic: str,
    total_scenes: int,
) -> list[str]:
    sentences = split_sentences(story_text)
    if not sentences:
        return [clean_text(topic)] if clean_text(topic) else []

    if len(sentences) <= total_scenes:
        return sentences

    selected = []
    for index in range(total_scenes):
        position = round(
            index * (len(sentences) - 1) / max(1, total_scenes - 1)
        )
        selected.append(sentences[position])
    return selected


def select_beat(format_type: str, index: int, total: int):
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS
    if total <= 1:
        return beats[0]
    position = round(index * (len(beats) - 1) / (total - 1))
    return beats[max(0, min(position, len(beats) - 1))]


def make_scene_purpose(
    role: str,
    description: str,
    anchor: str,
    topic: str,
) -> str:
    subject = anchor or topic

    if role == "part_opening":
        return (
            f"{description} Story-grounded starting point: {subject}. "
            "Do not invent events or characters."
            if subject else description
        )

    if role == "part_ending":
        return (
            f"{description} Build suspense from this established detail: "
            f"{subject}. Do not fabricate a plot twist."
            if subject else description
        )

    if role == "finale":
        return (
            f"{description} Connect the ending to established events: "
            f"{subject}. Do not invent an unsupported solution."
            if subject else description
        )

    if anchor:
        return (
            f"{description} Relevant source-story anchor: {anchor}. "
            "Preserve chronology and causal continuity."
        )
    return description


def build_plan(
    config: dict,
    format_type: str,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
    dynamic_short: bool,
) -> dict:
    total = parts_count * scenes_per_part
    part_hook = as_bool(config, "PART_HOOK", True)
    part_suspense = as_bool(config, "PART_SUSPENSE", True)
    final_resolution = as_bool(config, "FINAL_RESOLUTION", True)

    anchors = story_anchors(story_text, topic, total)
    parts = []
    global_number = 0

    for part_number in range(1, parts_count + 1):
        scenes = []

        for scene_number in range(1, scenes_per_part + 1):
            anchor = (
                anchors[global_number]
                if global_number < len(anchors)
                else ""
            )
            beat, description = select_beat(
                format_type, global_number, total
            )

            is_first = scene_number == 1
            is_last = scene_number == scenes_per_part
            is_final_part = part_number == parts_count
            is_final_scene = is_last and is_final_part

            if is_final_scene and final_resolution:
                role = "finale"
                beat = "finale"
                description = (
                    "Resolve the central conflict with believable "
                    "consequences. Do not fabricate missing facts."
                )
            elif is_first and (part_number == 1 or part_hook):
                role = "part_opening"
                beat = "hook" if part_number == 1 else "part_hook"
                description = (
                    "Open with immediate story-specific tension. "
                    "Connect later parts naturally to the previous part."
                )
            elif is_last and part_suspense:
                role = "part_ending"
                beat = "suspense"
                description = (
                    "End on a meaningful unresolved consequence, "
                    "discovery or decision grounded in the story."
                )
            else:
                role = "story_progression"

            purpose = make_scene_purpose(
                role, description, anchor, topic
            )

            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )
            scenes.append({
                "scene": scene_number,
                "global_scene_number": global_number + 1,
                "scene_id": scene_id,
                "role": role,
                "beat": beat,
                "beat_description": description,
                "purpose": purpose,
                "story_anchor": anchor,
                "continuity_rule": (
                    "Preserve character identity, clothing, location, "
                    "objects and causal continuity."
                ),
                "invention_policy": (
                    "Do not invent unsupported characters, motives, "
                    "relationships, events or twists."
                ),
            })
            global_number += 1

        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "scenes": scenes,
        })

    source = {
        "script_version": VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
        "dynamic_short_parts": dynamic_short,
        "anchors": anchors,
        "parts": parts,
    }

    return {
        "status": "planned",
        "script_version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "topic": topic,
        "title": topic or "",
        "title_source": (
            "explicit_topic" if topic
            else "deferred_to_full_story_generation"
        ),
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
        "scene_order": "part_then_scene",
        "mapping_version": 4,
        "dynamic_short_parts": dynamic_short,
        "source_fingerprint": fingerprint(source),
        "source": {
            "input_file": "Input/topic.txt",
            "story_text_provided": bool(story_text),
            "planning_mode": "offline_story_grounded",
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
        "mapping_version": 4,
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
            raise RuntimeError(
                f"Invalid part numbering at part {part_number}."
            )

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
                raise RuntimeError(
                    f"Incorrect local scene number: {expected_id}."
                )
            if scene.get("global_scene_number") != global_number:
                raise RuntimeError(
                    f"Incorrect global scene number: {expected_id}."
                )
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(
                    f"Incorrect scene ID: {expected_id}."
                )
            if not str(scene.get("purpose", "")).strip():
                raise RuntimeError(
                    f"Missing scene purpose: {expected_id}."
                )
            if not isinstance(scene.get("story_anchor", ""), str):
                raise RuntimeError(
                    f"Invalid story anchor: {expected_id}."
                )

    expected_title = topic if topic else ""
    if plan.get("title", "") != expected_title:
        raise RuntimeError(
            "Story plan title must be the explicit topic or empty "
            "until full-story title generation."
        )


def main() -> None:
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input file is missing: {INPUT_FILE}")

    config = load_and_validate(str(INPUT_FILE))
    format_type = str(get_format(config)).strip().lower()
    topic = clean_text(get_topic(config))
    story_text = clean_text(get_story_text(config))
    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if format_type not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {format_type}")
    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")
    if not topic and not story_text:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    dynamic_short = format_type == "short" and bool(story_text)

    if dynamic_short:
        parts_count = calculate_dynamic_short_parts(
            word_count(story_text)
        )
        log(
            "SHORT dynamic part count from supplied story:",
            parts_count,
        )
    else:
        parts_count = configured_parts
        if format_type == "short":
            log(
                "SHORT topic-only input: provisional part count:",
                parts_count,
            )

    plan = build_plan(
        config=config,
        format_type=format_type,
        topic=topic,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        dynamic_short=dynamic_short,
    )

    validate_plan(
        plan,
        format_type,
        topic,
        parts_count,
        scenes_per_part,
    )

    old_plan = read_json(OUTPUT_FILE)
    if (
        isinstance(old_plan, dict)
        and old_plan.get("source_fingerprint")
        == plan["source_fingerprint"]
        and old_plan.get("script_version") == VERSION
    ):
        try:
            validate_plan(
                old_plan,
                format_type,
                topic,
                parts_count,
                scenes_per_part,
            )
            log("Matching valid story-plan checkpoint preserved.")
            log("External API calls: 0")
            return
        except (RuntimeError, TypeError, ValueError) as exc:
            log("Existing plan is invalid; rebuilding:", exc)

    write_json(OUTPUT_FILE, plan)

    log("STORY PLAN: SUCCESS")
    log("Version:", VERSION)
    log("Format:", format_type)
    log("Topic:", topic or "(story text only)")
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", parts_count * scenes_per_part)
    log("Dynamic SHORT:", dynamic_short)
    log("Story-grounded anchors:", sum(
        bool(scene.get("story_anchor"))
        for part in plan["parts"]
        for scene in part["scenes"]
    ))
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