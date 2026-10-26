
#!/usr/bin/env python3
"""Validate story.json, story_plan.json and current input configuration."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_topic,
)

STORY_FILE = ROOT / "output" / "story" / "story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"


def fail(message: str) -> None:
    print(f"STORY VALIDATION FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path) -> dict:
    if not path.is_file():
        fail(f"Required file is missing: {path.relative_to(ROOT)}")

    if path.stat().st_size == 0:
        fail(f"Required file is empty: {path.relative_to(ROOT)}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read valid JSON from {path.name}: {exc}")

    if not isinstance(data, dict):
        fail(f"{path.name} must contain a JSON object.")

    return data


def clean(value) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def positive_int(value, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        fail(f"{field} must be an integer.")

    if result < 1:
        fail(f"{field} must be greater than zero.")

    return result


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text.strip()))


def main() -> None:
    input_file = ROOT / "Input" / "topic.txt"

    if not input_file.is_file() or input_file.stat().st_size == 0:
        fail("Input/topic.txt is missing or empty.")

    try:
        config = load_and_validate(input_file)
        input_format = clean(get_format(config)).lower()
        input_parts = positive_int(get_parts(config), "Input PARTS")
        input_scenes = positive_int(get_scenes(config), "Input SCENES")
        input_topic = clean(get_topic(config))
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Cannot validate Input/topic.txt: {exc}")

    if input_format not in {"short", "full"}:
        fail("Input FORMAT must be short or full.")

    story = load_json(STORY_FILE)
    plan = load_json(PLAN_FILE)

    if clean(story.get("status")).lower() != "completed":
        fail("story.json status is not completed.")

    if clean(plan.get("status")).lower() != "planned":
        fail("story_plan.json status is not planned.")

    story_format = clean(story.get("format")).lower()
    plan_format = clean(plan.get("format")).lower()

    if story_format != input_format:
        fail(
            f"FORMAT mismatch: Input={input_format}, "
            f"story.json={story_format or 'missing'}."
        )

    if plan_format != input_format:
        fail(
            f"FORMAT mismatch: Input={input_format}, "
            f"story_plan.json={plan_format or 'missing'}."
        )

    story_topic = clean(story.get("topic"))
    plan_topic = clean(plan.get("topic"))

    if input_topic and story_topic and input_topic != story_topic:
        fail("story.json topic does not match Input/topic.txt.")

    if input_topic and plan_topic and input_topic != plan_topic:
        fail("story_plan.json topic does not match Input/topic.txt.")

    title = clean(story.get("title"))
    hook = clean(story.get("hook"))

    if not title:
        fail("Story title is empty.")

    if not hook:
        fail("Story opening hook is empty.")

    parts = story.get("parts")
    plan_parts = plan.get("parts")

    if not isinstance(parts, list) or not parts:
        fail("story.json has no parts.")

    if not isinstance(plan_parts, list) or not plan_parts:
        fail("story_plan.json has no parts.")

    if len(parts) != input_parts:
        fail(
            f"Story part count mismatch: expected {input_parts}, "
            f"found {len(parts)}."
        )

    if len(plan_parts) != input_parts:
        fail(
            f"Plan part count mismatch: expected {input_parts}, "
            f"found {len(plan_parts)}."
        )

    expected_total = input_parts * input_scenes

    for filename, data in (
        ("story.json", story),
        ("story_plan.json", plan),
    ):
        for field, expected in (
            ("parts_count", input_parts),
            ("scenes_per_part", input_scenes),
            ("total_scenes", expected_total),
        ):
            if field in data:
                actual = positive_int(data[field], f"{filename} {field}")
                if actual != expected:
                    fail(
                        f"{filename} {field} mismatch: "
                        f"expected {expected}, found {actual}."
                    )

    seen_ids = set()
    total_scenes = 0
    part_word_counts = []

    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            fail(f"Story part {part_index} must be an object.")

        if positive_int(part.get("part"), f"Story part {part_index}") != part_index:
            fail(f"Incorrect story part numbering at part {part_index}.")

        plan_part = plan_parts[part_index - 1]
        if not isinstance(plan_part, dict):
            fail(f"Plan part {part_index} must be an object.")

        if positive_int(
            plan_part.get("part"), f"Plan part {part_index}"
        ) != part_index:
            fail(f"Incorrect plan part numbering at part {part_index}.")

        scenes = part.get("scenes")
        planned_scenes = plan_part.get("scenes")

        if not isinstance(scenes, list) or len(scenes) != input_scenes:
            actual = len(scenes) if isinstance(scenes, list) else 0
            fail(
                f"Story part {part_index} has {actual} scenes; "
                f"expected {input_scenes}."
            )

        if (
            not isinstance(planned_scenes, list)
            or len(planned_scenes) != input_scenes
        ):
            fail(f"Plan part {part_index} has an incorrect scene count.")

        part_words = 0

        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                fail(
                    f"Story part {part_index}, scene {scene_index} "
                    "must be an object."
                )

            plan_scene = planned_scenes[scene_index - 1]
            if not isinstance(plan_scene, dict):
                fail(
                    f"Plan part {part_index}, scene {scene_index} "
                    "must be an object."
                )

            scene_id = f"part_{part_index:02d}_scene_{scene_index:02d}"
            global_number = (
                (part_index - 1) * input_scenes + scene_index
            )

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Story scene ID mismatch: expected {scene_id}.")

            if clean(plan_scene.get("scene_id")) != scene_id:
                fail(f"Plan scene ID mismatch: expected {scene_id}.")

            if positive_int(
                scene.get("scene"), f"{scene_id} scene number"
            ) != scene_index:
                fail(f"Incorrect local scene number: {scene_id}.")

            if positive_int(
                scene.get("global_scene_number"),
                f"{scene_id} global scene number",
            ) != global_number:
                fail(f"Incorrect global scene number: {scene_id}.")

            if positive_int(
                plan_scene.get("scene"), f"{scene_id} plan scene number"
            ) != scene_index:
                fail(f"Plan local scene number mismatch: {scene_id}.")

            if positive_int(
                plan_scene.get("global_scene_number"),
                f"{scene_id} plan global scene number",
            ) != global_number:
                fail(f"Plan global scene number mismatch: {scene_id}.")

            if scene_id in seen_ids:
                fail(f"Duplicate scene ID: {scene_id}.")
            seen_ids.add(scene_id)

            narration = clean(scene.get("narration"))
            visual = clean(scene.get("visual"))
            purpose = clean(scene.get("purpose"))

            if not narration:
                fail(f"Missing narration in {scene_id}.")

            if not visual:
                fail(f"Missing visual description in {scene_id}.")

            if not purpose:
                fail(f"Missing scene purpose in {scene_id}.")

            part_words += word_count(narration)
            total_scenes += 1

        part_word_counts.append(part_words)

    if total_scenes != expected_total:
        fail(
            f"Total scene count mismatch: expected {expected_total}, "
            f"found {total_scenes}."
        )

    generation = story.get("_generation", {})
    if isinstance(generation, dict):
        declared_total = generation.get("total_scenes")
        if declared_total is not None:
            if positive_int(
                declared_total, "_generation.total_scenes"
            ) != expected_total:
                fail("_generation.total_scenes does not match actual scenes.")

    print("STORY VALIDATION: SUCCESS")
    print(f"Format: {input_format}")
    print(f"Title: {title}")
    print(f"Parts: {input_parts}")
    print(f"Scenes per part: {input_scenes}")
    print(f"Total scenes: {total_scenes}")
    print(f"Words per part: {part_word_counts}")
    print("Verified: input format, topic, plan, scene IDs and numbering.")


if __name__ == "__main__":
    main()
