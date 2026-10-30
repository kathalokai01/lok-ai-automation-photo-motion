#!/usr/bin/env python3
"""Katha Lok AI — strict generator-compatible story verification."""

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

VERSION = "story-verifier-v7-exact-generator-wordcount-checkpoint"


def fail(message: str) -> None:
    print(f"STORY VALIDATION FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def clean(value) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def word_count(value: str) -> int:
    """Exactly match generate_story.py word_count()."""
    return len(re.findall(r"\S+", clean(value)))


def sha256_text(value: str) -> str:
    return hashlib.sha256(
        clean(value).encode("utf-8")
    ).hexdigest()


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
        data = json.loads(
            path.read_text(encoding="utf-8-sig")
        )
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read valid JSON from {path.name}: {exc}")

    if not isinstance(data, dict):
        fail(f"{path.name} must contain a JSON object.")

    return data


def sentence_list(value: str) -> list[str]:
    text = clean(value)

    if not text:
        return []

    return [
        item.strip()
        for item in re.split(
            r"(?<=[।.!?！？])\s+",
            text,
        )
        if item.strip()
    ]


def verify_input():
    if not INPUT_FILE.is_file():
        fail("Input/topic.txt is missing.")

    if INPUT_FILE.stat().st_size == 0:
        fail("Input/topic.txt is empty.")

    try:
        config = load_and_validate(INPUT_FILE)

        mode = clean(get_format(config)).lower()
        parts = positive_int(
            get_parts(config),
            "Input PARTS",
        )
        scenes = positive_int(
            get_scenes(config),
            "Input SCENES",
        )

        topic = clean(get_topic(config))
        source_text = clean(get_story_text(config))

    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Cannot validate Input/topic.txt: {exc}")

    if mode not in {"short", "full"}:
        fail("FORMAT must be short or full.")

    if not topic and not source_text:
        fail("Both TOPIC and STORY_TEXT are empty.")

    return mode, parts, scenes, topic, source_text


def verify_plan(
    plan: dict,
    mode: str,
    parts_expected: int,
    scenes_expected: int,
    topic: str,
):
    if clean(plan.get("status")).lower() != "planned":
        fail("story_plan.json status is not planned.")

    if clean(plan.get("format")).lower() != mode:
        fail("Story plan FORMAT does not match Input.")

    if clean(plan.get("topic")) != topic:
        fail("Story plan topic does not match Input.")

    expected_title = topic if topic else ""

    if clean(plan.get("title")) != expected_title:
        fail(
            "Story plan title must be the explicit topic "
            "or empty for pasted-story-only input."
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

    plan_parts = plan.get("parts")

    if (
        not isinstance(plan_parts, list)
        or len(plan_parts) != parts_expected
    ):
        fail("Story plan has an incorrect number of parts.")

    global_number = 0

    for p, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            fail(f"Plan part {p} must be an object.")

        if positive_int(
            part.get("part"),
            f"Plan part {p}",
        ) != p:
            fail(f"Plan part numbering mismatch at part {p}.")

        scenes = part.get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_expected
        ):
            fail(f"Plan part {p} has an incorrect scene count.")

        for s, scene in enumerate(scenes, 1):
            global_number += 1
            scene_id = f"part_{p:02d}_scene_{s:02d}"

            if not isinstance(scene, dict):
                fail(f"Plan scene {scene_id} must be an object.")

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Plan scene ID mismatch: {scene_id}.")

            if positive_int(
                scene.get("scene"),
                scene_id + " local scene",
            ) != s:
                fail(f"Plan local scene number mismatch: {scene_id}.")

            if positive_int(
                scene.get("global_scene_number"),
                scene_id + " global scene",
            ) != global_number:
                fail(f"Plan global scene number mismatch: {scene_id}.")

            if not clean(scene.get("purpose")):
                fail(f"Plan purpose is missing: {scene_id}.")

            if not isinstance(scene.get("story_anchor", ""), str):
                fail(f"Plan story_anchor is invalid: {scene_id}.")

            if not clean(scene.get("beat")):
                fail(f"Plan beat is missing: {scene_id}.")

            if not clean(scene.get("role")):
                fail(f"Plan role is missing: {scene_id}.")

    return plan_parts


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
        fail("Story title exceeds 180 characters.")

    story_topic = clean(story.get("topic"))

    if topic and story_topic != topic:
        fail("Story topic does not match explicit Input topic.")

    if not topic and story_topic != title:
        fail(
            "Story topic must equal generated title "
            "when Input has no explicit topic."
        )

    if clean(story.get("source_text")) != source_text:
        fail("Story source_text does not match current Input.")

    expected_hash = sha256_text(source_text)
    stored_hash = clean(story.get("source_text_sha256"))

    if not stored_hash:
        fail("Story source_text_sha256 is missing.")

    if stored_hash != expected_hash:
        fail("Story source text fingerprint mismatch.")

    try:
        mapping_version = int(story.get("mapping_version", -1))
    except (TypeError, ValueError):
        mapping_version = -1

    if mapping_version != 5:
        fail(
            "Story mapping_version mismatch: "
            f"expected 5, found {mapping_version}."
        )

    generation = story.get("_generation")

    if not isinstance(generation, dict):
        fail("Story _generation metadata is missing or invalid.")

    generation_expected = {
        "format": mode,
        "parts": parts_expected,
        "scenes_per_part": scenes_expected,
        "total_scenes": parts_expected * scenes_expected,
        "mapping_version": 5,
        "scene_order": "part_then_scene",
    }

    for field, expected_value in generation_expected.items():
        if generation.get(field) != expected_value:
            fail(
                f"Story _generation.{field} mismatch: "
                f"expected {expected_value!r}, "
                f"found {generation.get(field)!r}."
            )

    parts = story.get("parts")

    if (
        not isinstance(parts, list)
        or len(parts) != parts_expected
    ):
        fail(
            f"Story part count mismatch: expected {parts_expected}, "
            f"found {len(parts) if isinstance(parts, list) else 0}."
        )

    if len(plan_parts) != parts_expected:
        fail("Story and plan part counts do not match.")

    actual_word_counts = []
    seen_ids = set()
    suspense_count = 0

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story part {p} must be an object.")

        if positive_int(
            part.get("part"),
            f"Story part {p}",
        ) != p:
            fail(f"Story part numbering mismatch at part {p}.")

        plan_part = plan_parts[p - 1]

        scenes = part.get("scenes")
        planned_scenes = plan_part.get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_expected
        ):
            fail(f"Story part {p} has an incorrect scene count.")

        if (
            not isinstance(planned_scenes, list)
            or len(planned_scenes) != scenes_expected
        ):
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

            if positive_int(
                scene.get("scene"),
                scene_id + " scene",
            ) != s:
                fail(f"Story scene number mismatch: {scene_id}.")

            if positive_int(
                scene.get("global_scene_number"),
                scene_id + " global number",
            ) != global_number:
                fail(f"Story global scene number mismatch: {scene_id}.")

            if positive_int(
                plan_scene.get("scene"),
                scene_id + " plan scene",
            ) != s:
                fail(f"Plan scene number mismatch: {scene_id}.")

            if positive_int(
                plan_scene.get("global_scene_number"),
                scene_id + " plan global number",
            ) != global_number:
                fail(f"Plan global number mismatch: {scene_id}.")

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

            if purpose != clean(plan_scene.get("purpose")):
                fail(
                    f"Story purpose does not match story plan: "
                    f"{scene_id}."
                )

            for field in ("dialogue", "suspense"):
                if not isinstance(scene.get(field, ""), str):
                    fail(
                        f"Story scene {scene_id} field "
                        f"{field} must be text."
                    )

            part_words += word_count(narration)

        actual_word_counts.append(part_words)

        last_scene = scenes[-1]
        suspense = clean(last_scene.get("suspense"))
        ending_narration = clean(last_scene.get("narration"))

        if p < parts_expected:
            if not suspense:
                fail(f"Part {p} has no ending suspense marker.")

            ending_sentences = sentence_list(ending_narration)

            if not ending_sentences:
                fail(f"Part {p} has no ending sentence.")

            if suspense != ending_sentences[-1]:
                fail(
                    f"Part {p} suspense marker must exactly match "
                    "the final sentence of its last scene."
                )

            suspense_count += 1

        else:
            if clean(suspense) != (
                "Resolve the central story conflict using established events."
            ):
                fail(
                    "Final scene must carry the generator's "
                    "resolution marker, not a cliffhanger."
                )

    expected_total = parts_expected * scenes_expected

    if len(seen_ids) != expected_total:
        fail(
            f"Total scene count mismatch: expected {expected_total}, "
            f"found {len(seen_ids)}."
        )

    if mode == "short":
        for p, amount in enumerate(actual_word_counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                fail(
                    f"SHORT PART {p} has {amount} words; "
                    f"required {SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}."
                )

    declared_counts = story.get("part_word_counts")

    if (
        not isinstance(declared_counts, list)
        or len(declared_counts) != parts_expected
    ):
        fail("Story part_word_counts is missing or invalid.")

    for p, (declared, actual) in enumerate(
        zip(declared_counts, actual_word_counts),
        1,
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

    if mode == "short":
        limits = story.get("short_word_target")

        if not isinstance(limits, dict):
            fail("Short word target metadata is missing.")

        expected_limits = {
            "minimum": SHORT_MIN_WORDS,
            "maximum": SHORT_MAX_WORDS,
            "target": 135,
            "validation": "strict_120_150_words_per_part",
        }

        for field, expected_value in expected_limits.items():
            if limits.get(field) != expected_value:
                fail(f"Invalid short_word_target.{field}.")

    hook = clean(story.get("hook"))

    if not hook:
        fail("Story opening hook is empty.")

    first_scene = parts[0]["scenes"][0]
    first_narration = clean(first_scene.get("narration"))
    first_sentences = sentence_list(first_narration)

    if not first_sentences:
        fail("First scene has no usable opening sentence.")

    if hook != first_sentences[0]:
        fail(
            "Story hook must exactly match the first sentence "
            "of the first scene."
        )

    if clean(story.get("ending_type")).lower() != "resolution":
        fail("Final story ending_type must be resolution.")

    final_scene = parts[-1]["scenes"][-1]

    if not clean(final_scene.get("narration")):
        fail("Final story ending narration is empty.")

    validation = story.get("validation")

    if not isinstance(validation, dict):
        fail("Story validation metadata is missing.")

    required_validation = {
        "exact_part_count": True,
        "exact_scene_count": True,
        "unique_scene_ids": True,
        "opening_hook_present": True,
        "part_end_suspense_markers_present": True,
        "final_narration_present": True,
        "short_word_limits_verified": mode == "short",
    }

    for field, expected_value in required_validation.items():
        if validation.get(field) is not expected_value:
            fail(
                f"Story validation flag {field} is missing "
                "or incorrect."
            )

    return title, actual_word_counts, expected_total, suspense_count


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
        fail(
            "final_title.txt does not match story.json title."
        )


def main():
    print("=" * 62)
    print("KATHA LOK AI — STRICT STORY VERIFICATION")
    print("Version:", VERSION)
    print("=" * 62)

    mode, parts, scenes, topic, source_text = verify_input()

    story = load_json(STORY_FILE)
    plan = load_json(PLAN_FILE)

    plan_parts = verify_plan(
        plan,
        mode,
        parts,
        scenes,
        topic,
    )

    (
        title,
        word_counts,
        total,
        suspense_count,
    ) = verify_story(
        story,
        mode,
        parts,
        scenes,
        topic,
        source_text,
        plan_parts,
    )

    verify_title_file(title)

    print("STORY VALIDATION: SUCCESS")
    print("Format:", mode)
    print("Title:", title)
    print("Parts:", parts)
    print("Scenes per part:", scenes)
    print("Total scenes:", total)
    print("Words per part:", word_counts)
    print("Non-final suspense markers:", suspense_count)
    print(
        "Short word limit:",
        f"{SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}"
        if mode == "short"
        else "not applicable",
    )
    print(
        "Verified: input, title, source fingerprint, plan mapping, "
        "scene IDs, numbering, narration, visual prompts, hook, "
        "suspense markers, ending and word counts."
    )
    print("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        fail(f"Unexpected verification error: {exc}")