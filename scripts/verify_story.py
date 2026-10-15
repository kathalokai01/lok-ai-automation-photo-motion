#!/usr/bin/env python3
"""Validate Katha Lok AI story.json using its actual nested scene schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

STORY_FILE = Path("output/story/story.json")
PLAN_FILE = Path("output/story/story_plan.json")


def fail(message: str) -> None:
    print(f"STORY VALIDATION FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path):
    if not path.is_file():
        fail(f"Required file is missing: {path}")

    if path.stat().st_size == 0:
        fail(f"Required file is empty: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Invalid JSON in {path}: {exc}")


def positive_int(value, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        fail(f"{field} must be an integer.")

    if result < 1:
        fail(f"{field} must be greater than zero.")

    return result


def main() -> None:
    story = load_json(STORY_FILE)
    plan = load_json(PLAN_FILE)

    if not isinstance(story, dict):
        fail("story.json root must be a JSON object.")

    if story.get("status") != "completed":
        fail("Story status is not 'completed'.")

    story_format = str(story.get("format", "")).strip().lower()
    if story_format not in {"short", "full"}:
        fail("Story format must be 'short' or 'full'.")

    parts = story.get("parts")
    plan_parts = plan.get("parts") if isinstance(plan, dict) else None

    if not isinstance(parts, list) or not parts:
        fail("Story has no parts.")

    if not isinstance(plan_parts, list) or not plan_parts:
        fail("Story plan has no parts.")

    if len(parts) != len(plan_parts):
        fail(
            f"Part count mismatch: story={len(parts)}, "
            f"plan={len(plan_parts)}."
        )

    expected_scenes_per_part = None
    total_scenes = 0
    seen_ids = set()

    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            fail(f"Story part {part_index} must be an object.")

        expected_part_number = positive_int(
            part.get("part"), f"story part {part_index}.part"
        )

        if expected_part_number != part_index:
            fail(f"Incorrect part numbering at part {part_index}.")

        plan_part = plan_parts[part_index - 1]
        if not isinstance(plan_part, dict):
            fail(f"Plan part {part_index} must be an object.")

        plan_number = positive_int(
            plan_part.get("part"), f"plan part {part_index}.part"
        )

        if plan_number != part_index:
            fail(f"Incorrect plan part numbering at part {part_index}.")

        scenes = part.get("scenes")
        planned_scenes = plan_part.get("scenes")

        if not isinstance(scenes, list) or not scenes:
            fail(f"Story part {part_index} has no scene list.")

        if not isinstance(planned_scenes, list) or not planned_scenes:
            fail(f"Plan part {part_index} has no scene list.")

        if len(scenes) != len(planned_scenes):
            fail(
                f"Scene count mismatch in part {part_index}: "
                f"story={len(scenes)}, plan={len(planned_scenes)}."
            )

        if expected_scenes_per_part is None:
            expected_scenes_per_part = len(scenes)
        elif len(scenes) != expected_scenes_per_part:
            fail("Scene count is inconsistent across story parts.")

        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                fail(
                    f"Part {part_index}, scene {scene_index} "
                    "must be an object."
                )

            plan_scene = planned_scenes[scene_index - 1]
            if not isinstance(plan_scene, dict):
                fail(
                    f"Plan part {part_index}, scene {scene_index} "
                    "must be an object."
                )

            expected_id = (
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )

            if scene.get("scene_id") != expected_id:
                fail(f"Incorrect story scene ID: expected {expected_id}.")

            if plan_scene.get("scene_id") != expected_id:
                fail(f"Incorrect plan scene ID: expected {expected_id}.")

            if scene.get("scene") != scene_index:
                fail(f"Incorrect local scene number: {expected_id}.")

            global_number = (
                (part_index - 1) * expected_scenes_per_part + scene_index
            )

            if scene.get("global_scene_number") != global_number:
                fail(f"Incorrect global scene number: {expected_id}.")

            if plan_scene.get("scene") != scene_index:
                fail(f"Plan local scene number mismatch: {expected_id}.")

            if plan_scene.get("global_scene_number") != global_number:
                fail(f"Plan global scene number mismatch: {expected_id}.")

            if expected_id in seen_ids:
                fail(f"Duplicate scene ID: {expected_id}.")

            seen_ids.add(expected_id)

            narration = scene.get("narration")
            if not isinstance(narration, str) or not narration.strip():
                fail(f"Missing narration in {expected_id}.")

            visual = scene.get("visual")
            if not isinstance(visual, str) or not visual.strip():
                fail(f"Missing visual description in {expected_id}.")

            purpose = scene.get("purpose")
            if not isinstance(purpose, str) or not purpose.strip():
                fail(f"Missing scene purpose in {expected_id}.")

            total_scenes += 1

    generation = story.get("_generation", {})
    if isinstance(generation, dict):
        declared_total = generation.get("total_scenes")
        if declared_total is not None:
            try:
                declared_total = int(declared_total)
            except (TypeError, ValueError):
                fail("_generation.total_scenes is not an integer.")

            if declared_total != total_scenes:
                fail(
                    f"Declared scene total {declared_total} "
                    f"does not match actual total {total_scenes}."
                )

    if not str(story.get("title", "")).strip():
        fail("Story title is empty.")

    if not str(story.get("hook", "")).strip():
        fail("Story hook is empty.")

    print("STORY VALIDATION: SUCCESS")
    print(f"Format: {story_format}")
    print(f"Parts: {len(parts)}")
    print(f"Scenes per part: {expected_scenes_per_part}")
    print(f"Total scenes: {total_scenes}")
    print("Verified: nested scenes, narration, visuals, IDs and plan mapping.")


if __name__ == "__main__":
    main()