#!/usr/bin/env python3
"""Katha Lok AI — generator-compatible strict story verification."""

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
    get_story_text,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"

SHORT_MIN_WORDS = 120
SHORT_TARGET_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180
SENTENCE_END_RE = re.compile(r"[.!?।॥…]$")

VERSION = "story-verifier-v9-dynamic-short-parts-180-hard-limit"


def fail(message: str) -> None:
    print(f"STORY VALIDATION FAILED: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def clean(value) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\S+", clean(value)))


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
        parts = positive_int(get_parts(config), "Input PARTS")
        scenes = positive_int(get_scenes(config), "Input SCENES")
        topic = clean(get_topic(config))
        source_text = clean(get_story_text(config))
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Input configuration validation failed: {exc}")

    if mode not in {"short", "full"}:
        fail("FORMAT must be short or full.")

    if not topic and not source_text:
        fail("Both TOPIC and STORY_TEXT are empty.")

    return mode, parts, scenes, topic, source_text


def verify_plan(plan, mode, configured_parts, scenes_expected, topic):
    if clean(plan.get("status")).lower() != "planned":
        fail("Story plan status is not planned.")

    if clean(plan.get("format")).lower() != mode:
        fail("Story plan FORMAT does not match Input.")

    if clean(plan.get("topic")) != topic:
        fail("Story plan topic does not match Input.")

    parts_expected = positive_int(
        plan.get("parts_count"), "Story plan parts_count"
    )

    if mode == "full" and parts_expected != configured_parts:
        fail(
            f"FULL plan must use configured PARTS={configured_parts}; "
            f"found {parts_expected}."
        )

    expected = {
        "parts_count": parts_expected,
        "scenes_per_part": scenes_expected,
        "total_scenes": parts_expected * scenes_expected,
        "mapping_version": 4,
    }

    for field, expected_value in expected.items():
        try:
            actual = int(plan.get(field, -1))
        except (TypeError, ValueError):
            actual = -1

        if actual != expected_value:
            fail(
                f"Story plan {field} mismatch: "
                f"expected {expected_value}, found {actual}."
            )

    if clean(plan.get("scene_order")) != "part_then_scene":
        fail("Story plan scene_order must be part_then_scene.")

    expected_title = topic if topic else ""
    if clean(plan.get("title")) != expected_title:
        fail("Story plan title does not match the current input.")

    plan_parts = plan.get("parts")

    if not isinstance(plan_parts, list) or len(plan_parts) != parts_expected:
        fail("Story plan part count is incorrect.")

    global_number = 0

    for p, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            fail(f"Plan part {p} must be an object.")

        if positive_int(part.get("part"), f"Plan part {p}") != p:
            fail(f"Plan part numbering mismatch: {p}.")

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            fail(f"Plan part {p} scene count is incorrect.")

        for s, scene in enumerate(scenes, 1):
            global_number += 1
            scene_id = f"part_{p:02d}_scene_{s:02d}"

            if not isinstance(scene, dict):
                fail(f"Plan scene {scene_id} must be an object.")

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Plan scene ID mismatch: {scene_id}.")

            if positive_int(scene.get("scene"), scene_id + " scene") != s:
                fail(f"Plan local scene number mismatch: {scene_id}.")

            if positive_int(
                scene.get("global_scene_number"),
                scene_id + " global number",
            ) != global_number:
                fail(f"Plan global scene number mismatch: {scene_id}.")

            if not clean(scene.get("purpose")):
                fail(f"Plan purpose is missing: {scene_id}.")

            if not clean(scene.get("beat")):
                fail(f"Plan beat is missing: {scene_id}.")

            if not clean(scene.get("role")):
                fail(f"Plan role is missing: {scene_id}.")

            if not isinstance(scene.get("story_anchor", ""), str):
                fail(f"Plan story anchor is invalid: {scene_id}.")

    return plan_parts, parts_expected


def verify_story(
    story,
    mode,
    parts_expected,
    scenes_expected,
    topic,
    source_text,
    plan_parts,
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
        fail("Story source_text does not match current Input.")

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
                f"Story {field} mismatch: expected {expected}, "
                f"found {actual}."
            )

    fingerprint = clean(story.get("source_fingerprint"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        fail("Story source_fingerprint is missing or invalid.")

    parts = story.get("parts")
    all_scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != parts_expected:
        fail("Story part count does not match Input.")

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

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story part {p} must be an object.")

        if positive_int(part.get("part"), f"Story part {p}") != p:
            fail(f"Story part numbering mismatch: {p}.")

        part_narration = clean(part.get("narration"))

        if not part_narration:
            fail(f"Part {p} narration is empty.")

        actual_part_words = word_count(part_narration)

        try:
            declared_words = int(part.get("word_count", -1))
        except (TypeError, ValueError):
            declared_words = -1

        if declared_words != actual_part_words:
            fail(
                f"Part {p} word_count mismatch: "
                f"declared {declared_words}, actual {actual_part_words}."
            )

        if mode == "short":
            if actual_part_words < SHORT_MIN_WORDS:
                fail(
                    f"SHORT PART {p} has {actual_part_words} words; "
                    f"minimum is {SHORT_MIN_WORDS}."
                )
            if actual_part_words > SHORT_HARD_MAX_WORDS:
                fail(
                    f"SHORT PART {p} has {actual_part_words} words; "
                    f"hard maximum is {SHORT_HARD_MAX_WORDS}."
                )
            if actual_part_words > SHORT_TARGET_MAX_WORDS:
                if not SENTENCE_END_RE.search(part_narration):
                    fail(
                        f"SHORT PART {p} has {actual_part_words} words; "
                        f"151-{SHORT_HARD_MAX_WORDS} is allowed only at a "
                        "sentence boundary."
                    )

        scenes = part.get("scenes")
        planned_scenes = plan_parts[p - 1].get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            fail(f"Story part {p} scene count is incorrect.")

        if not isinstance(planned_scenes, list):
            fail(f"Plan part {p} scenes are invalid.")

        if len(planned_scenes) != scenes_expected:
            fail(f"Plan part {p} scene count is incorrect.")

        scene_narrations = []

        for s, scene in enumerate(scenes, 1):
            scene_id = f"part_{p:02d}_scene_{s:02d}"
            global_number = (p - 1) * scenes_expected + s

            if not isinstance(scene, dict):
                fail(f"Story scene {scene_id} must be an object.")

            planned = planned_scenes[s - 1]

            if not isinstance(planned, dict):
                fail(f"Planned scene {scene_id} must be an object.")

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Story scene ID mismatch: {scene_id}.")

            if positive_int(
                scene.get("part"), scene_id + " part"
            ) != p:
                fail(f"Story part number mismatch: {scene_id}.")

            if positive_int(
                scene.get("scene"), scene_id + " scene"
            ) != s:
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
                f"Part {p} narration does not match its scene narration."
            )

        actual_word_counts.append(actual_part_words)

    if len(seen_ids) != expected_total:
        fail(
            f"Unique scene count mismatch: expected {expected_total}, "
            f"found {len(seen_ids)}."
        )

    if flattened_scenes != all_scenes:
        fail("Top-level scenes do not match the ordered part scene records.")

    combined_story = "\n\n".join(
        clean(part.get("narration")) for part in parts
    )

    if clean(story.get("story_text")) != combined_story:
        fail("story_text does not match the ordered part narrations.")

    if not clean(parts[0]["scenes"][0].get("narration")):
        fail("Opening hook narration is empty.")

    if not clean(parts[-1]["scenes"][-1].get("narration")):
        fail("Final scene narration is empty.")

    generation_method = clean(story.get("generation_method"))

    if not generation_method:
        fail("Story generation_method is missing.")

    model = clean(story.get("model"))

    if not model:
        fail("Story model metadata is missing.")

    return title, actual_word_counts, expected_total, generation_method, model


def verify_title_file(title):
    if not TITLE_FILE.is_file():
        fail("final_title.txt is missing.")

    try:
        saved_title = clean(TITLE_FILE.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        fail(f"Cannot read final_title.txt: {exc}")

    if not saved_title:
        fail("final_title.txt is empty.")

    if saved_title != title:
        fail("final_title.txt does not match story.json title.")


def main():
    print("=" * 62)
    print("KATHA LOK AI — GENERATOR-COMPATIBLE STORY VERIFICATION")
    print("Version:", VERSION)
    print("=" * 62)

    mode, configured_parts, scenes, topic, source_text = verify_input()

    story = load_json(STORY_FILE)
    plan = load_json(PLAN_FILE)

    plan_parts, parts = verify_plan(
        plan,
        mode,
        configured_parts,
        scenes,
        topic,
    )

    (
        title,
        word_counts,
        total,
        generation_method,
        model,
    ) = verify_story(
        story,
        mode,
        parts,
        scenes,
        topic,
        source_text,
        plan_parts,
        parts,
    )

    verify_title_file(title)

    print("STORY VALIDATION: SUCCESS")
    print("Format:", mode)
    print("Title:", title)
    print("Configured PARTS:", configured_parts)
    print("Actual generated parts:", parts)
    print("Scenes per part:", scenes)
    print("Total scenes:", total)
    print("Words per part:", word_counts)
    print("Generation method:", generation_method)
    print("Actual model:", model)
    print(
        "Verified: input, title, source text, source fingerprint, "
        "story plan, part counts, scene counts, scene IDs, ordering, "
        "narration, visual prompts, suspense markers and word counts."
    )
    print("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Cancelled.", flush=True)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Unexpected verification error: {exc}")