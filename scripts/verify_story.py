#!/usr/bin/env python3
"""Katha Lok AI — strict story, title, format and plan verification."""

from __future__ import annotations

import hashlib
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
SHORT_MAX_WORDS = 150

VERSION = "story-verifier-v5-short-count-title-plan-integrity"


def fail(message: str) -> None:
    print(f"STORY VALIDATION FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def clean(value) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", clean(text)))


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
        fail(f"Cannot read valid JSON from {path.name}: {exc}")

    if not isinstance(data, dict):
        fail(f"{path.name} must contain a JSON object.")

    return data


def verify_input():
    if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size == 0:
        fail("Input/topic.txt is missing or empty.")

    try:
        config = load_and_validate(INPUT_FILE)
        mode = clean(get_format(config)).lower()
        parts = positive_int(get_parts(config), "Input PARTS")
        scenes = positive_int(get_scenes(config), "Input SCENES")
        topic = clean(get_topic(config))
        source_text = clean(get_story_text(config))
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Cannot validate Input/topic.txt: {exc}")

    if mode not in {"short", "full"}:
        fail("Input FORMAT must be short or full.")

    if not topic and not source_text:
        fail("Both TOPIC and STORY_TEXT are empty.")

    return mode, parts, scenes, topic, source_text


def verify_plan(plan, mode, parts_expected, scenes_expected, topic):
    if clean(plan.get("status")).lower() != "planned":
        fail("story_plan.json status is not planned.")

    if clean(plan.get("format")).lower() != mode:
        fail("story_plan.json FORMAT does not match Input/topic.txt.")

    if clean(plan.get("topic")) != topic:
        fail("story_plan.json topic does not match current input.")

    # v4 planner deliberately leaves title empty when only story text exists.
    expected_plan_title = topic
    if clean(plan.get("title")) != expected_plan_title:
        fail(
            "story_plan.json title must equal the explicit topic, "
            "or remain empty when no topic was supplied."
        )

    expected_counts = {
        "parts_count": parts_expected,
        "scenes_per_part": scenes_expected,
        "total_scenes": parts_expected * scenes_expected,
        "mapping_version": 4,
    }

    for field, expected in expected_counts.items():
        try:
            actual = int(plan.get(field, -1))
        except (TypeError, ValueError):
            actual = -1

        if actual != expected:
            fail(
                f"story_plan.json {field} mismatch: "
                f"expected {expected}, found {actual}."
            )

    plan_parts = plan.get("parts")
    if not isinstance(plan_parts, list) or len(plan_parts) != parts_expected:
        fail("story_plan.json has an incorrect number of parts.")

    global_number = 0

    for p, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            fail(f"Plan part {p} must be an object.")

        if positive_int(part.get("part"), f"Plan part {p}") != p:
            fail(f"Plan part numbering mismatch at part {p}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            fail(f"Plan part {p} has an incorrect scene count.")

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

            if not isinstance(scene.get("story_anchor", ""), str):
                fail(f"Plan story_anchor is invalid: {scene_id}.")


def verify_story(
    story,
    mode,
    parts_expected,
    scenes_expected,
    topic,
    source_text,
    plan,
):
    if clean(story.get("status")).lower() != "completed":
        fail("story.json status is not completed.")

    if clean(story.get("format")).lower() != mode:
        fail("story.json FORMAT does not match Input/topic.txt.")

    title = clean(story.get("title"))
    if not title:
        fail("Story title is empty.")

    if len(title) > 180:
        fail("Story title exceeds the 180-character limit.")

    story_topic = clean(story.get("topic"))
    if topic and story_topic != topic:
        fail("story.json topic does not match Input/topic.txt.")

    if not topic and story_topic != title:
        fail(
            "When no topic is supplied, story.json topic "
            "must match its generated title."
        )

    hook = clean(story.get("hook"))
    if not hook:
        fail("Story opening hook is empty.")

    parts = story.get("parts")
    plan_parts = plan.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_expected:
        fail(
            f"Story part count mismatch: expected {parts_expected}, "
            f"found {len(parts) if isinstance(parts, list) else 0}."
        )

    if len(plan_parts) != parts_expected:
        fail("Story and plan part counts do not match.")

    total_scenes = parts_expected * scenes_expected
    seen_ids = set()
    actual_word_counts = []

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story part {p} must be an object.")

        if positive_int(part.get("part"), f"Story part {p}") != p:
            fail(f"Story part numbering mismatch at part {p}.")

        plan_part = plan_parts[p - 1]
        if not isinstance(plan_part, dict):
            fail(f"Plan part {p} must be an object.")

        scenes = part.get("scenes")
        planned_scenes = plan_part.get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            fail(f"Story part {p} has an incorrect scene count.")

        if not isinstance(planned_scenes, list) or len(planned_scenes) != scenes_expected:
            fail(f"Plan part {p} has an incorrect scene count.")

        part_words = 0

        for s, scene in enumerate(scenes, 1):
            scene_id = f"part_{p:02d}_scene_{s:02d}"
            global_number = (p - 1) * scenes_expected + s

            if not isinstance(scene, dict):
                fail(f"Story scene {scene_id} must be an object.")

            plan_scene = planned_scenes[s - 1]
            if not isinstance(plan_scene, dict):
                fail(f"Plan scene {scene_id} must be an object.")

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Story scene ID mismatch: {scene_id}.")

            if clean(plan_scene.get("scene_id")) != scene_id:
                fail(f"Plan scene ID mismatch: {scene_id}.")

            if positive_int(scene.get("scene"), scene_id + " scene") != s:
                fail(f"Story scene number mismatch: {scene_id}.")

            if positive_int(
                scene.get("global_scene_number"),
                scene_id + " global number",
            ) != global_number:
                fail(f"Story global scene number mismatch: {scene_id}.")

            if positive_int(
                plan_scene.get("scene"), scene_id + " plan scene"
            ) != s:
                fail(f"Plan scene number mismatch: {scene_id}.")

            if positive_int(
                plan_scene.get("global_scene_number"),
                scene_id + " plan global number",
            ) != global_number:
                fail(f"Plan global scene number mismatch: {scene_id}.")

            if scene_id in seen_ids:
                fail(f"Duplicate scene ID: {scene_id}.")
            seen_ids.add(scene_id)

            narration = clean(scene.get("narration"))
            visual = clean(scene.get("visual"))
            purpose = clean(scene.get("purpose"))

            if not narration:
                fail(f"Narration is missing: {scene_id}.")

            if not visual:
                fail(f"Visual description is missing: {scene_id}.")

            if not purpose:
                fail(f"Scene purpose is missing: {scene_id}.")

            part_words += word_count(narration)

        actual_word_counts.append(part_words)

    if len(seen_ids) != total_scenes:
        fail(
            f"Total scene count mismatch: expected {total_scenes}, "
            f"found {len(seen_ids)}."
        )

    # Strict independent short-format validation.
    if mode == "short":
        for p, amount in enumerate(actual_word_counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                fail(
                    f"SHORT PART {p} has {amount} words; "
                    f"required {SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}."
                )

    declared_counts = story.get("part_word_counts")
    if isinstance(declared_counts, list):
        if len(declared_counts) != parts_expected:
            fail("story.json part_word_counts has an incorrect length.")

        for p, (declared, actual) in enumerate(
            zip(declared_counts, actual_word_counts), 1
        ):
            try:
                declared = int(declared)
            except (TypeError, ValueError):
                fail(f"Invalid declared word count for part {p}.")

            if declared != actual:
                fail(
                    f"Part {p} word count mismatch: "
                    f"declared {declared}, actual {actual}."
                )

    generation = story.get("_generation", {})
    if isinstance(generation, dict):
        expected_generation = {
            "format": mode,
            "parts": parts_expected,
            "scenes_per_part": scenes_expected,
            "total_scenes": total_scenes,
        }

        for field, expected in expected_generation.items():
            if field not in generation:
                continue
            if generation[field] != expected:
                fail(f"story.json _generation.{field} mismatch.")

    # Check source text fingerprint when the field is present.
    expected_hash = hashlib.sha256(
        clean(source_text).encode("utf-8")
    ).hexdigest()

    stored_hash = clean(story.get("source_text_sha256"))
    if stored_hash and stored_hash != expected_hash:
        fail("story.json source text fingerprint does not match input.")

    # The plan must not invent a generated title when no topic was supplied.
    if not topic and clean(plan.get("title")):
        fail("Story plan contains an unexpected generated title.")

    return title, actual_word_counts, total_scenes


def verify_title_file(title):
    if not TITLE_FILE.is_file():
        print("WARNING: final_title.txt is missing.")
        return

    try:
        saved_title = clean(TITLE_FILE.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        fail(f"Cannot read final_title.txt: {exc}")

    if saved_title and saved_title != title:
        fail(
            "final_title.txt does not match story.json title. "
            "Regenerate the story title file."
        )


def main():
    print("=" * 58)
    print("KATHA LOK AI — STRICT STORY VERIFICATION")
    print("Version:", VERSION)
    print("=" * 58)

    mode, parts, scenes, topic, source_text = verify_input()

    story = load_json(STORY_FILE)
    plan = load_json(PLAN_FILE)

    verify_plan(plan, mode, parts, scenes, topic)

    title, word_counts, total = verify_story(
        story,
        mode,
        parts,
        scenes,
        topic,
        source_text,
        plan,
    )

    verify_title_file(title)

    print("STORY VALIDATION: SUCCESS")
    print("Format:", mode)
    print("Title:", title)
    print("Parts:", parts)
    print("Scenes per part:", scenes)
    print("Total scenes:", total)
    print("Words per part:", word_counts)
    print("Short word limit:", (
        f"{SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}"
        if mode == "short" else "not applicable"
    ))
    print("Verified: input, title, plan, scene IDs, numbering and word counts.")
    print("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        fail(f"Unexpected verification error: {exc}")