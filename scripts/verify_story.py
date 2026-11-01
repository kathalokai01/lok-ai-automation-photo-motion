#!/usr/bin/env python3
"""Katha Lok AI — strict verification of generated story and story plan."""

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
from generate_story import get_complete_source

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"

SHORT_MIN_WORDS = 120
SHORT_TARGET_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180

VERSION = "story-verifier-v12-source-integrity"


def log(*values):
    print(*values, flush=True)


def clean(value) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\S+", clean(value)))


def fail(message: str):
    log("STORY VALIDATION FAILED:", message)
    raise SystemExit(1)


def positive_int(value, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        fail(f"{field} must be an integer.")

    if result < 1:
        fail(f"{field} must be greater than zero.")

    return result


def load_json(path: Path) -> dict:
    if not path.is_file():
        fail(f"Required file is missing: {path.relative_to(ROOT)}")

    if path.stat().st_size == 0:
        fail(f"Required file is empty: {path.relative_to(ROOT)}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read {path.name}: {exc}")

    if not isinstance(data, dict):
        fail(f"{path.name} must contain a JSON object.")

    return data


def verify_input():
    if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size == 0:
        fail("Input/topic.txt is missing or empty.")

    try:
        config = load_and_validate(str(INPUT_FILE))

        mode = clean(get_format(config)).lower()
        configured_parts = positive_int(
            get_parts(config), "Input PARTS"
        )
        scenes_expected = positive_int(
            get_scenes(config), "Input SCENES"
        )
        topic = clean(get_topic(config))

        # Must use the same complete-story reader as generate_story.py.
        # This prevents a multiline STORY_TEXT parser mismatch.
        source_text = clean(
            get_complete_source(config, INPUT_FILE)
        )

    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Input configuration validation failed: {exc}")

    if mode not in {"short", "full"}:
        fail("FORMAT must be short or full.")

    if not topic and not source_text:
        fail("Both TOPIC and STORY_TEXT are empty.")

    if source_text and word_count(source_text) < 20:
        fail(
            "STORY_TEXT contains fewer than 20 words. "
            "Check the complete multiline input."
        )

    return mode, configured_parts, scenes_expected, topic, source_text


def verify_plan(
    plan: dict,
    mode: str,
    configured_parts: int,
    scenes_expected: int,
    topic: str,
):
    if clean(plan.get("status")).lower() != "planned":
        fail("Story plan status is not planned.")

    if clean(plan.get("format")).lower() != mode:
        fail("Story plan FORMAT does not match Input.")

    if clean(plan.get("topic")) != topic:
        fail("Story plan topic does not match Input.")

    parts_expected = positive_int(
        plan.get("parts_count"),
        "Story plan parts_count",
    )

    if mode == "full" and parts_expected != configured_parts:
        fail(
            f"FULL plan must use configured PARTS={configured_parts}; "
            f"found {parts_expected}."
        )

    if mode == "short" and parts_expected < 1:
        fail("SHORT plan must contain at least one part.")

    expected_metadata = {
        "scenes_per_part": scenes_expected,
        "total_scenes": parts_expected * scenes_expected,
        "mapping_version": 4,
    }

    for field, expected in expected_metadata.items():
        try:
            actual = int(plan.get(field, -1))
        except (TypeError, ValueError):
            actual = -1

        if actual != expected:
            fail(
                f"Story plan {field} mismatch: "
                f"expected {expected}, found {actual}."
            )

    if clean(plan.get("scene_order")) != "part_then_scene":
        fail("Story plan scene_order must be part_then_scene.")

    if clean(plan.get("title")) != topic:
        fail("Story plan title does not match the explicit topic.")

    plan_parts = plan.get("parts")

    if (
        not isinstance(plan_parts, list)
        or len(plan_parts) != parts_expected
    ):
        fail("Story plan part count is incorrect.")

    global_number = 0

    for part_number, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            fail(f"Plan part {part_number} must be an object.")

        if positive_int(
            part.get("part"), f"Plan part {part_number}"
        ) != part_number:
            fail(f"Plan part numbering mismatch: {part_number}.")

        scenes = part.get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_expected
        ):
            fail(
                f"Plan part {part_number} must contain "
                f"{scenes_expected} scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if not isinstance(scene, dict):
                fail(f"Plan scene {scene_id} must be an object.")

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Plan scene ID mismatch: {scene_id}.")

            if positive_int(
                scene.get("scene"), scene_id + " scene"
            ) != scene_number:
                fail(f"Plan local scene number mismatch: {scene_id}.")

            if positive_int(
                scene.get("global_scene_number"),
                scene_id + " global number",
            ) != global_number:
                fail(f"Plan global scene number mismatch: {scene_id}.")

            for field in ("purpose", "beat", "role"):
                if not clean(scene.get(field)):
                    fail(f"Plan {field} is missing: {scene_id}.")

            if not isinstance(scene.get("story_anchor", ""), str):
                fail(f"Plan story anchor is invalid: {scene_id}.")

    return plan_parts, parts_expected


def verify_story(
    story: dict,
    mode: str,
    parts_expected: int,
    scenes_expected: int,
    topic: str,
    source_text: str,
    plan_parts: list,
):
    if clean(story.get("status")).lower() != "completed":
        fail("story.json status is not completed.")

    if clean(story.get("format")).lower() != mode:
        fail("Story FORMAT does not match Input.")

    title = clean(story.get("title"))

    if not title:
        fail("Generated story title is empty.")

    if len(title) > 180:
        fail("Generated story title exceeds 180 characters.")

    if clean(story.get("final_title")) != title:
        fail("final_title metadata does not match story title.")

    if clean(story.get("topic")) != topic:
        fail("Story topic does not match Input.")

    if clean(story.get("source_text")) != source_text:
        fail(
            "Story source_text differs from the complete "
            "STORY_TEXT in Input/topic.txt."
        )

    if clean(story.get("scene_order")) != "part_then_scene":
        fail("Story scene_order must be part_then_scene.")

    expected_metadata = {
        "total_parts": parts_expected,
        "parts_count": parts_expected,
        "scenes_per_part": scenes_expected,
        "total_scenes": parts_expected * scenes_expected,
    }

    for field, expected in expected_metadata.items():
        try:
            actual = int(story.get(field, -1))
        except (TypeError, ValueError):
            actual = -1

        if actual != expected:
            fail(
                f"Story {field} mismatch: "
                f"expected {expected}, found {actual}."
            )

    fingerprint = clean(story.get("source_fingerprint"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        fail("Story source_fingerprint is missing or invalid.")

    try:
        source_words = int(story.get("source_word_count", -1))
    except (TypeError, ValueError):
        source_words = -1

    if source_words != word_count(source_text):
        fail(
            "Story source_word_count does not match "
            "the complete input story."
        )

    parts = story.get("parts")
    all_scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != parts_expected:
        fail("Story part count does not match the story plan.")

    if not isinstance(all_scenes, list):
        fail("Story top-level scenes must be a list.")

    expected_total = parts_expected * scenes_expected

    if len(all_scenes) != expected_total:
        fail(
            f"Story scene count mismatch: expected {expected_total}, "
            f"found {len(all_scenes)}."
        )

    actual_word_counts = []
    flattened_scenes = []
    seen_ids = set()

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story part {part_number} must be an object.")

        if positive_int(
            part.get("part"), f"Story part {part_number}"
        ) != part_number:
            fail(f"Story part numbering mismatch: {part_number}.")

        part_narration = clean(part.get("narration"))

        if not part_narration:
            fail(f"Part {part_number} narration is empty.")

        actual_words = word_count(part_narration)

        try:
            declared_words = int(part.get("word_count", -1))
        except (TypeError, ValueError):
            declared_words = -1

        if declared_words != actual_words:
            fail(
                f"Part {part_number} word_count mismatch: "
                f"declared {declared_words}, actual {actual_words}."
            )

        if mode == "short":
            if not SHORT_MIN_WORDS <= actual_words <= SHORT_HARD_MAX_WORDS:
                fail(
                    f"SHORT part {part_number} has {actual_words} words; "
                    f"allowed range is {SHORT_MIN_WORDS}–{SHORT_HARD_MAX_WORDS}."
                )

        scenes = part.get("scenes")
        planned_scenes = plan_parts[part_number - 1].get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_expected
        ):
            fail(f"Story part {part_number} scene count is incorrect.")

        if (
            not isinstance(planned_scenes, list)
            or len(planned_scenes) != scenes_expected
        ):
            fail(f"Plan part {part_number} scene count is incorrect.")

        scene_narrations = []

        for scene_number, scene in enumerate(scenes, 1):
            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )
            global_number = (
                (part_number - 1) * scenes_expected + scene_number
            )

            if not isinstance(scene, dict):
                fail(f"Story scene {scene_id} must be an object.")

            planned = planned_scenes[scene_number - 1]

            if not isinstance(planned, dict):
                fail(f"Planned scene {scene_id} must be an object.")

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Story scene ID mismatch: {scene_id}.")

            if positive_int(
                scene.get("part"), scene_id + " part"
            ) != part_number:
                fail(f"Story part number mismatch: {scene_id}.")

            if positive_int(
                scene.get("scene"), scene_id + " scene"
            ) != scene_number:
                fail(f"Story scene number mismatch: {scene_id}.")

            if positive_int(
                scene.get("global_scene_number"),
                scene_id + " global number",
            ) != global_number:
                fail(f"Story global scene number mismatch: {scene_id}.")

            if scene_id in seen_ids:
                fail(f"Duplicate scene ID: {scene_id}.")

            seen_ids.add(scene_id)

            if clean(scene.get("purpose")) != clean(planned.get("purpose")):
                fail(f"Story purpose does not match plan: {scene_id}.")

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    fail(f"Story {field} is missing: {scene_id}.")

            if clean(scene.get("visual")) != clean(scene.get("visual_prompt")):
                fail(f"Visual and visual_prompt differ: {scene_id}.")

            for field in ("dialogue", "suspense"):
                if not isinstance(scene.get(field, ""), str):
                    fail(f"Story {field} must be text: {scene_id}.")

            if clean(scene.get("suspense")) != clean(scene.get("purpose")):
                fail(f"Story suspense marker does not match plan: {scene_id}.")

            scene_narrations.append(clean(scene.get("narration")))
            flattened_scenes.append(scene)

        combined_narration = clean(" ".join(scene_narrations))

        if combined_narration != part_narration:
            fail(
                f"Part {part_number} narration does not match "
                "its ordered scene narration."
            )

        actual_word_counts.append(actual_words)

    if len(seen_ids) != expected_total:
        fail(
            f"Unique scene count mismatch: expected {expected_total}, "
            f"found {len(seen_ids)}."
        )

    if flattened_scenes != all_scenes:
        fail("Top-level scenes do not match ordered part scene records.")

    combined_story = clean(
        " ".join(clean(part.get("narration")) for part in parts)
    )

    if clean(story.get("story_text")) != combined_story:
        fail("story_text does not match ordered part narrations.")

    if mode == "full" and source_text:
        if combined_story != clean(source_text):
            fail(
                "FULL mode changed the supplied story text. "
                "Source story must be preserved."
            )

    if mode == "short":
        if clean(plan_parts[0]["scenes"][0].get("role")) != "part_opening":
            fail("First SHORT scene is not marked as the opening hook.")

        if clean(plan_parts[-1]["scenes"][-1].get("role")) not in {
            "part_ending",
            "finale",
        }:
            fail("Final SHORT scene is not marked as suspense or finale.")

    generation_method = clean(story.get("generation_method"))
    model = clean(story.get("model"))

    if not generation_method:
        fail("Story generation_method is missing.")

    if not model:
        fail("Story model metadata is missing.")

    try:
        total_story_words = int(story.get("total_story_words", -1))
    except (TypeError, ValueError):
        total_story_words = -1

    if total_story_words != word_count(combined_story):
        fail("total_story_words metadata is incorrect.")

    return title, actual_word_counts, expected_total, generation_method, model

def verify_title_file(title: str):
    if not TITLE_FILE.is_file():
        fail("final_title.txt is missing.")

    try:
        saved_title = clean(
            TITLE_FILE.read_text(encoding="utf-8-sig")
        )
    except OSError as exc:
        fail(f"Cannot read final_title.txt: {exc}")

    if not saved_title:
        fail("final_title.txt is empty.")

    if saved_title != title:
        fail("final_title.txt does not match story.json title.")


def verify_story_plan_matches_story(plan_parts, story):
    story_parts = story.get("parts")

    if not isinstance(story_parts, list):
        fail("Story parts are missing.")

    if len(story_parts) != len(plan_parts):
        fail("Story and plan part counts differ.")

    for part_index, (plan_part, story_part) in enumerate(
        zip(plan_parts, story_parts),
        1,
    ):
        plan_scenes = plan_part.get("scenes")
        story_scenes = story_part.get("scenes")

        if not isinstance(plan_scenes, list):
            fail(f"Plan scenes are invalid in part {part_index}.")

        if not isinstance(story_scenes, list):
            fail(f"Story scenes are invalid in part {part_index}.")

        if len(plan_scenes) != len(story_scenes):
            fail(
                f"Plan/story scene count differs in part {part_index}."
            )

        for scene_index, (planned, generated) in enumerate(
            zip(plan_scenes, story_scenes),
            1,
        ):
            scene_id = (
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )

            for field in ("scene_id", "scene", "global_scene_number"):
                if str(planned.get(field)) != str(generated.get(field)):
                    fail(
                        f"Plan/story {field} mismatch: {scene_id}."
                    )

            if clean(planned.get("purpose")) != clean(
                generated.get("purpose")
            ):
                fail(f"Plan/story purpose mismatch: {scene_id}.")

            if clean(planned.get("role")) != clean(
                generated.get("role")
            ):
                fail(f"Plan/story role mismatch: {scene_id}.")

            if clean(planned.get("beat")) != clean(
                generated.get("beat")
            ):
                fail(f"Plan/story beat mismatch: {scene_id}.")

            if clean(planned.get("story_anchor")) != clean(
                generated.get("story_anchor")
            ):
                fail(f"Plan/story anchor mismatch: {scene_id}.")


def main():
    log("=" * 62)
    log("KATHA LOK AI — STRICT STORY VERIFICATION")
    log("Version:", VERSION)
    log("=" * 62)

    (
        mode,
        configured_parts,
        scenes_expected,
        topic,
        source_text,
    ) = verify_input()

    story = load_json(STORY_FILE)
    plan = load_json(PLAN_FILE)

    plan_parts, parts_expected = verify_plan(
        plan,
        mode,
        configured_parts,
        scenes_expected,
        topic,
    )

    (
        title,
        word_counts,
        total_scenes,
        generation_method,
        model,
    ) = verify_story(
        story,
        mode,
        parts_expected,
        scenes_expected,
        topic,
        source_text,
        plan_parts,
    )

    verify_story_plan_matches_story(plan_parts, story)
    verify_title_file(title)

    log("STORY VALIDATION: SUCCESS")
    log("Format:", mode)
    log("Title:", title)
    log("Configured PARTS:", configured_parts)
    log("Generated PARTS:", parts_expected)
    log("Scenes per part:", scenes_expected)
    log("Total scenes:", total_scenes)
    log("Words per part:", word_counts)
    log("Source words:", word_count(source_text))
    log("Generation method:", generation_method)
    log("Actual model:", model)
    log(
        "Verified: input source, source fingerprint, title, "
        "story plan, part counts, scene counts, scene IDs, "
        "scene ordering, narration, visual prompts, plan/story "
        "consistency and word-count metadata."
    )
    log("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Unexpected verification error: {exc}")