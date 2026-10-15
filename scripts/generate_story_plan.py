
#!/usr/bin/env python3
"""Create the deterministic scene plan required before story generation.

This step is intentionally offline: it does not call Gemini or any other API.
It creates output/story/story_plan.json from Input/topic.txt so that
generate_story.py can safely run after this script.
"""

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
STORY_DIR = ROOT / "output" / "story"
OUTPUT_FILE = STORY_DIR / "story_plan.json"

SCRIPT_VERSION = "offline-structural-story-plan-v1"

# Story beats are guidance for the story generator, not extra scenes.
SHORT_BEATS = (
    ("hook", "Open with an immediate question, danger, mystery, or surprise."),
    ("setup", "Establish the character, place, goal, and immediate stakes."),
    ("discovery", "Reveal a clue, unusual event, or important new information."),
    ("escalation", "Increase the pressure and make the situation harder."),
    ("obstacle", "Introduce a credible obstacle, conflict, or complication."),
    ("reversal", "Change the audience's understanding with a meaningful turn."),
    ("choice", "Force a character to make a consequential choice."),
    ("confrontation", "Bring the main conflict into direct confrontation."),
    ("reveal", "Reveal a significant truth while preserving continuity."),
    ("climax", "Deliver the strongest action or emotional turning point."),
    ("payoff", "Pay off an earlier clue or promise in the story."),
    ("suspense", "End on a compelling unresolved question or consequence."),
)

FULL_BEATS = (
    ("hook", "Open with a strong cinematic hook and clear story tension."),
    ("establishing", "Establish the world, characters, relationships, and stakes."),
    ("inciting_event", "Introduce the event that disrupts ordinary life."),
    ("goal", "Clarify the protagonist's goal and what is at risk."),
    ("investigation", "Develop clues, motivations, and relevant story context."),
    ("first_obstacle", "Introduce the first meaningful obstacle or opposition."),
    ("complication", "Make the original goal harder and raise the stakes."),
    ("relationship", "Develop character relationships through consequential action."),
    ("discovery", "Reveal new information that changes the situation."),
    ("midpoint", "Create a major reversal or shift in the conflict."),
    ("pressure", "Increase urgency and the cost of failure."),
    ("setback", "Deliver a believable setback with lasting consequences."),
    ("reassessment", "Force the protagonist to reconsider their approach."),
    ("preparation", "Prepare the characters for the decisive confrontation."),
    ("approach", "Move the story toward its central confrontation."),
    ("confrontation", "Bring opposing goals into direct conflict."),
    ("crisis", "Force a difficult choice with meaningful consequences."),
    ("climax", "Resolve the central struggle through decisive action."),
    ("aftermath", "Show the immediate consequences of the climax."),
    ("resolution", "Resolve the central story and provide emotional closure."),
    ("final_suspense", "If the input calls for a continuation, end with a story-relevant question."),
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def write_json(path: Path, data: dict) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def sha256_data(data: Any) -> str:
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def select_beat(format_type: str, scene_number: int, total_scenes: int):
    """Spread story beats across any configured scene count."""
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS

    if total_scenes <= 1:
        return beats[0]

    # Evenly distribute the available beats over the requested scene count.
    index = round(
        (scene_number - 1) * (len(beats) - 1) / (total_scenes - 1)
    )
    index = max(0, min(index, len(beats) - 1))
    return beats[index]


def build_plan(
    config: dict,
    format_type: str,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    total_scenes = parts_count * scenes_per_part
    parts = []
    global_scene_number = 0

    for part_number in range(1, parts_count + 1):
        part_scenes = []

        for scene_number in range(1, scenes_per_part + 1):
            global_scene_number += 1
            beat_name, beat_description = select_beat(
                format_type,
                global_scene_number,
                total_scenes,
            )

            if scene_number == 1 and (
                part_number == 1
                or bool(config.get("PART_HOOK", True))
            ):
                role = "part_opening"
                beat_name = "hook" if part_number == 1 else "part_hook"
                beat_description = (
                    "Start with an immediate, story-specific hook. "
                    "For later parts, connect the hook to the previous part."
                )
            elif (
                scene_number == scenes_per_part
                and bool(config.get("PART_SUSPENSE", True))
            ):
                role = "part_ending"
                beat_name = "suspense"
                beat_description = (
                    "End with a meaningful story-specific suspense beat "
                    "that encourages the viewer to continue."
                )
            else:
                role = "story_progression"

            if (
                part_number == parts_count
                and scene_number == scenes_per_part
                and bool(config.get("FINAL_RESOLUTION", True))
            ):
                role = "finale"
                beat_name = "finale"
                beat_description = (
                    "Resolve the central conflict and provide a satisfying "
                    "ending. Preserve any suspense only if it does not "
                    "undermine the main resolution."
                )

            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            part_scenes.append({
                "scene": scene_number,
                "global_scene_number": global_scene_number,
                "scene_id": scene_id,
                "role": role,
                "beat": beat_name,
                "beat_description": beat_description,
                "purpose": beat_description,
            })

        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "scenes": part_scenes,
        })

    source = {
        "script_version": SCRIPT_VERSION,
        "input_file": str(INPUT_FILE.relative_to(ROOT)),
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total_scenes,
        "part_hook": bool(config.get("PART_HOOK", True)),
        "part_suspense": bool(config.get("PART_SUSPENSE", True)),
        "final_resolution": bool(config.get("FINAL_RESOLUTION", True)),
        "parts": parts,
    }

    return {
        "status": "planned",
        "script_version": SCRIPT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "topic": topic,
        "title": get_title_source(config),
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total_scenes,
        "scene_order": "part_then_scene",
        "mapping_version": 3,
        "source_fingerprint": sha256_data(source),
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
    if plan.get("format") != format_type:
        raise RuntimeError("Generated plan format mismatch.")

    if str(plan.get("topic", "")).strip() != topic:
        raise RuntimeError("Generated plan topic mismatch.")

    if int(plan.get("parts_count", -1)) != parts_count:
        raise RuntimeError("Generated plan part count mismatch.")

    if int(plan.get("scenes_per_part", -1)) != scenes_per_part:
        raise RuntimeError("Generated plan scenes-per-part mismatch.")

    if int(plan.get("total_scenes", -1)) != parts_count * scenes_per_part:
        raise RuntimeError("Generated plan total scene count mismatch.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Generated plan has an invalid parts list.")

    expected_global = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid plan part {part_number}.")

        if int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Incorrect part number at part {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must contain exactly "
                f"{scenes_per_part} scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            expected_global += 1

            if not isinstance(scene, dict):
                raise RuntimeError("Plan contains an invalid scene.")

            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if int(scene.get("scene", -1)) != scene_number:
                raise RuntimeError(f"Scene numbering mismatch: {expected_id}.")

            if int(scene.get("global_scene_number", -1)) != expected_global:
                raise RuntimeError(
                    f"Global scene numbering mismatch: {expected_id}."
                )

            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Scene ID mismatch: {expected_id}.")

            if not str(scene.get("purpose", "")).strip():
                raise RuntimeError(f"Missing purpose for {expected_id}.")


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
        config=config,
        format_type=format_type,
        topic=topic,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
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
        and old_plan.get("source_fingerprint") == plan["source_fingerprint"]
        and old_plan.get("format") == format_type
        and old_plan.get("topic", "") == topic
        and old_plan.get("parts_count") == parts_count
        and old_plan.get("scenes_per_part") == scenes_per_part
    ):
        log("Matching story plan already exists; preserving its checkpoint.")
        log("No API request was made.")
        return

    write_json(OUTPUT_FILE, plan)

    log("STORY PLAN: SUCCESS")
    log(f"Format: {format_type}")
    log(f"Topic: {topic or '(story text only)'}")
    log(f"Parts: {parts_count}")
    log(f"Scenes per part: {scenes_per_part}")
    log(f"Total scenes: {parts_count * scenes_per_part}")
    log(f"Saved: {OUTPUT_FILE}")
    log("Planning mode: offline; API calls: 0")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
