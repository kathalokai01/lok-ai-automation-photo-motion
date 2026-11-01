#!/usr/bin/env python3
"""Strict source, story-plan and narration integrity verification."""

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
)

from generate_story import get_complete_source

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"

VERSION = "story-verifier-v13-strict-source-and-fixed-parts"

SHORT_MIN_WORDS = 120
SHORT_MAX_WORDS = 150
MAX_TITLE_LENGTH = 180


def log(*values):
    print(*values, flush=True)


def clean(value):
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def word_count(value):
    return len(re.findall(r"\S+", clean(value)))


def fail(message):
    log("STORY VALIDATION FAILED:", message)
    raise SystemExit(1)


def positive_int(value, field):
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(f"{field} must be an integer.")

    if result < 1:
        fail(f"{field} must be greater than zero.")

    return result


def read_json(path):
    if not path.is_file():
        fail(f"Required file is missing: {path.relative_to(ROOT)}")

    if path.stat().st_size == 0:
        fail(f"Required file is empty: {path.relative_to(ROOT)}")

    try:
        data = json.loads(
            path.read_text(encoding="utf-8-sig")
        )
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read {path.name}: {exc}")

    if not isinstance(data, dict):
        fail(f"{path.name} must contain a JSON object.")

    return data


def normalized_text(value):
    """Normalize whitespace only; do not discard story words."""
    return clean(value)


def text_fingerprint(value):
    return hashlib.sha256(
        normalized_text(value).encode("utf-8")
    ).hexdigest()


def verify_input():
    if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size == 0:
        fail("Input/topic.txt is missing or empty.")

    try:
        config = load_and_validate(str(INPUT_FILE))

        mode = clean(get_format(config)).lower()
        configured_parts = positive_int(
            get_parts(config),
            "Input PARTS",
        )
        scenes_expected = positive_int(
            get_scenes(config),
            "Input SCENES",
        )

        topic = clean(get_topic(config))
        explicit_title = clean(config.get("TITLE", ""))

        source_text = normalized_text(
            get_complete_source(config, INPUT_FILE)
        )

    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Input configuration validation failed: {exc}")

    if mode not in {"short", "full"}:
        fail("FORMAT must be exactly short or full.")

    if not topic and not explicit_title and not source_text:
        fail("Provide TOPIC, TITLE or STORY_TEXT.")

    if source_text and word_count(source_text) < 20:
        fail(
            "STORY_TEXT contains fewer than 20 words. "
            "Check the complete multiline input."
        )

    return {
        "config": config,
        "mode": mode,
        "parts": configured_parts,
        "scenes": scenes_expected,
        "topic": topic,
        "explicit_title": explicit_title,
        "source_text": source_text,
    }


def verify_plan(
    plan,
    mode,
    configured_parts,
    scenes_expected,
    topic,
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

    if parts_expected != configured_parts:
        fail(
            f"Story plan must contain exactly configured "
            f"PARTS={configured_parts}; found {parts_expected}."
        )

    if clean(plan.get("title")) != topic:
        fail(
            "Story plan title must match TOPIC. "
            "The final generated title is checked separately."
        )

    expected_metadata = {
        "scenes_per_part": scenes_expected,
        "total_scenes": parts_expected * scenes_expected,
        "mapping_version": 4,
    }

    for field, expected in expected_metadata.items():
        try:
            actual = int(plan.get(field, -1))
        except (TypeError, ValueError, OverflowError):
            actual = -1

        if actual != expected:
            fail(
                f"Story plan {field} mismatch: "
                f"expected {expected}, found {actual}."
            )

    if clean(plan.get("scene_order")) != "part_then_scene":
        fail("Story plan scene_order must be part_then_scene.")

    parts = plan.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_expected:
        fail("Story plan part count is incorrect.")

    global_number = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Plan part {part_number} must be an object.")

        if positive_int(
            part.get("part"),
            f"Plan part {part_number}",
        ) != part_number:
            fail(f"Plan part numbering mismatch: {part_number}.")

        expected_part_id = f"part_{part_number:02d}"

        if clean(part.get("part_id")) != expected_part_id:
            fail(f"Plan part ID mismatch: {expected_part_id}.")

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

            checks = {
                "scene": scene_number,
                "global_scene_number": global_number,
            }

            for field, expected in checks.items():
                if positive_int(
                    scene.get(field),
                    f"{scene_id} {field}",
                ) != expected:
                    fail(
                        f"Plan {field} mismatch: {scene_id}."
                    )

            for field in (
                "purpose",
                "beat",
                "role",
                "beat_description",
                "continuity_rule",
                "invention_policy",
                "visual_policy",
            ):
                if not clean(scene.get(field)):
                    fail(
                        f"Plan {field} is missing: {scene_id}."
                    )

            if not isinstance(scene.get("story_anchor", ""), str):
                fail(f"Plan story_anchor is invalid: {scene_id}.")

    if mode == "short":
        for part_number, part in enumerate(parts, 1):
            scenes = part["scenes"]

            if clean(scenes[0].get("role")) != "part_opening":
                fail(
                    f"SHORT part {part_number} has no opening hook."
                )

            expected_end_role = (
                "finale"
                if part_number == parts_expected
                else "part_ending"
            )

            if clean(scenes[-1].get("role")) != expected_end_role:
                fail(
                    f"SHORT part {part_number} has incorrect ending role. "
                    f"Expected {expected_end_role}."
                )

    fingerprint = clean(plan.get("source_fingerprint"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        fail("Story plan source_fingerprint is invalid.")

    source_meta = plan.get("source")

    if not isinstance(source_meta, dict):
        fail("Story plan source metadata is missing.")

    if source_meta.get("api_calls") != 0:
        fail("Story planning must not make external API calls.")

    return parts, parts_expected


def verify_story_metadata(
    story,
    mode,
    parts_expected,
    scenes_expected,
    topic,
    source_text,
    explicit_title,
):
    if clean(story.get("status")).lower() != "completed":
        fail("story.json status is not completed.")

    if clean(story.get("format")).lower() != mode:
        fail("Story FORMAT does not match Input.")

    title = clean(story.get("title"))

    if not title:
        fail("Generated story title is empty.")

    if len(title) > MAX_TITLE_LENGTH:
        fail("Generated story title exceeds 180 characters.")

    if clean(story.get("final_title")) != title:
        fail("final_title metadata does not match story title.")

    if explicit_title and title != explicit_title:
        fail(
            "Generated title differs from the explicit TITLE "
            "provided in Input/topic.txt."
        )

    if clean(story.get("topic")) != topic:
        fail("Story topic does not match Input.")

    if normalized_text(story.get("source_text", "")) != source_text:
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
        except (TypeError, ValueError, OverflowError):
            actual = -1

        if actual != expected:
            fail(
                f"Story {field} mismatch: "
                f"expected {expected}, found {actual}."
            )

    fingerprint = clean(story.get("source_fingerprint"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        fail("Story source_fingerprint is missing or invalid.")

    source_words = word_count(source_text)

    try:
        declared_source_words = int(
            story.get("source_word_count", -1)
        )
    except (TypeError, ValueError, OverflowError):
        declared_source_words = -1

    if declared_source_words != source_words:
        fail(
            "Story source_word_count does not match "
            "the complete input story."
        )

    parts = story.get("parts")
    all_scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != parts_expected:
        fail("Story part count does not match configured PARTS.")

    expected_total = parts_expected * scenes_expected

    if not isinstance(all_scenes, list):
        fail("Story top-level scenes must be a list.")

    if len(all_scenes) != expected_total:
        fail(
            f"Story scene count mismatch: expected {expected_total}, "
            f"found {len(all_scenes)}."
        )

    return title, parts, all_scenes, expected_total


def verify_story_parts(
    parts,
    all_scenes,
    plan_parts,
    mode,
    scenes_expected,
):
    word_counts = []
    flattened_scenes = []
    seen_ids = set()

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story part {part_number} must be an object.")

        if positive_int(
            part.get("part"),
            f"Story part {part_number}",
        ) != part_number:
            fail(f"Story part numbering mismatch: {part_number}.")

        expected_part_id = f"part_{part_number:02d}"

        if clean(part.get("part_id")) != expected_part_id:
            fail(f"Story part ID mismatch: {expected_part_id}.")

        narration = clean(part.get("narration"))

        if not narration:
            fail(f"Part {part_number} narration is empty.")

        amount = word_count(narration)

        try:
            declared_words = int(part.get("word_count", -1))
        except (TypeError, ValueError, OverflowError):
            declared_words = -1

        if declared_words != amount:
            fail(
                f"Part {part_number} word_count mismatch: "
                f"declared {declared_words}, actual {amount}."
            )

        if mode == "short" and not (
            SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS
        ):
            fail(
                f"SHORT part {part_number} has {amount} words; "
                f"required range is {SHORT_MIN_WORDS}–{SHORT_MAX_WORDS}."
            )

        scenes = part.get("scenes")
        planned_scenes = plan_parts[part_number - 1].get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_expected
        ):
            fail(
                f"Story part {part_number} must contain "
                f"{scenes_expected} scenes."
            )

        if (
            not isinstance(planned_scenes, list)
            or len(planned_scenes) != scenes_expected
        ):
            fail(
                f"Plan part {part_number} scene count is incorrect."
            )

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
                fail(f"Plan scene {scene_id} must be an object.")

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Story scene ID mismatch: {scene_id}.")

            expected_values = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_number,
            }

            for field, expected in expected_values.items():
                if positive_int(
                    scene.get(field),
                    f"{scene_id} {field}",
                ) != expected:
                    fail(f"Story {field} mismatch: {scene_id}.")

            if scene_id in seen_ids:
                fail(f"Duplicate scene ID: {scene_id}.")

            seen_ids.add(scene_id)

            for field in ("purpose", "role", "beat", "story_anchor"):
                if clean(scene.get(field)) != clean(planned.get(field)):
                    fail(
                        f"Story and plan {field} mismatch: {scene_id}."
                    )

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    fail(f"Story {field} is missing: {scene_id}.")

            if clean(scene.get("visual")) != clean(
                scene.get("visual_prompt")
            ):
                fail(
                    f"Visual and visual_prompt differ: {scene_id}."
                )

            if clean(scene.get("suspense")) != clean(
                planned.get("suspense")
                or planned.get("suspense_prompt")
                or planned.get("purpose")
            ):
                # The generated story uses the plan purpose as the
                # suspense fallback when the plan has no explicit field.
                if clean(scene.get("suspense")) != clean(
                    planned.get("purpose")
                ):
                    fail(
                        f"Story suspense marker mismatch: {scene_id}."
                    )

            if not isinstance(scene.get("dialogue", ""), str):
                fail(f"Story dialogue must be text: {scene_id}.")

            scene_narrations.append(clean(scene.get("narration")))
            flattened_scenes.append(scene)

        combined = clean(" ".join(scene_narrations))

        if combined != narration:
            fail(
                f"Part {part_number} narration does not match "
                "the ordered scene narration."
            )

        word_counts.append(amount)

    if len(seen_ids) != len(all_scenes):
        fail("Unique scene count does not match top-level scene count.")

    if flattened_scenes != all_scenes:
        fail(
            "Top-level scenes do not match ordered part scene records."
        )

    return word_counts


def verify_story_text(story, parts, mode, source_text):
    combined = clean(
        " ".join(clean(part.get("narration")) for part in parts)
    )

    if clean(story.get("story_text")) != combined:
        fail(
            "story_text does not match ordered part narrations."
        )

    if mode == "full" and source_text:
        if combined != clean(source_text):
            fail(
                "FULL mode changed the supplied story. "
                "Original narration must be preserved."
            )

    if mode == "short" and source_text:
        source_tokens = re.findall(
            r"[^\W_]+",
            source_text.lower(),
            flags=re.UNICODE,
        )
        generated_tokens = re.findall(
            r"[^\W_]+",
            combined.lower(),
            flags=re.UNICODE,
        )

        if len(source_tokens) >= 30 and len(generated_tokens) < 30:
            fail(
                "SHORT narration is too short to represent "
                "the supplied source story."
            )

    try:
        total_words = int(story.get("total_story_words", -1))
    except (TypeError, ValueError, OverflowError):
        total_words = -1

    if total_words != word_count(combined):
        fail("total_story_words metadata is incorrect.")

    return combined

def verify_source_coverage(source_text, generated_text):
    """Detect an unrelated replacement story using meaningful word overlap."""
    if not source_text:
        return

    def meaningful_tokens(value):
        stop_words = {
            "और", "या", "लेकिन", "क्योंकि", "इसलिए", "फिर",
            "तब", "जब", "जहाँ", "वहाँ", "यह", "वह", "एक",
            "कोई", "कुछ", "सभी", "बहुत", "था", "थी", "थे",
            "है", "हैं", "हो", "हुआ", "हुई", "हुए", "कर",
            "किया", "की", "का", "के", "में", "पर", "से",
            "तक", "लिए", "साथ", "अपने", "अपनी", "अपना",
            "नहीं", "भी", "ही", "तो", "अब", "बाद", "पहले",
            "उसने", "उसका", "उसकी", "उसके", "उनका", "उनकी",
            "जिस", "जिसने", "जिसका", "रहा", "रही", "रहे",
            "कहानी", "कथा", "बात", "तरह",
        }

        tokens = re.findall(
            r"[^\W_]+",
            value.lower(),
            flags=re.UNICODE,
        )

        return {
            token for token in tokens
            if len(token) > 2 and token not in stop_words
        }

    source_terms = meaningful_tokens(source_text)
    generated_terms = meaningful_tokens(generated_text)

    if len(source_terms) < 12:
        return

    overlap = source_terms.intersection(generated_terms)
    ratio = len(overlap) / len(source_terms)

    log(
        "Source vocabulary overlap:",
        f"{len(overlap)}/{len(source_terms)}",
        f"({ratio:.1%})",
    )

    if ratio < 0.20:
        fail(
            "Generated SHORT story has very low vocabulary overlap "
            "with the supplied source. It may have replaced the "
            "original plot with unrelated content."
        )


def verify_title_file(title):
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


def verify_plan_story_match(plan_parts, story_parts):
    if len(plan_parts) != len(story_parts):
        fail("Story and plan part counts differ.")

    for part_number, (plan_part, story_part) in enumerate(
        zip(plan_parts, story_parts),
        1,
    ):
        plan_scenes = plan_part.get("scenes")
        story_scenes = story_part.get("scenes")

        if (
            not isinstance(plan_scenes, list)
            or not isinstance(story_scenes, list)
        ):
            fail(
                f"Plan/story scenes are invalid in part {part_number}."
            )

        if len(plan_scenes) != len(story_scenes):
            fail(
                f"Plan/story scene counts differ in part {part_number}."
            )

        for scene_number, (planned, generated) in enumerate(
            zip(plan_scenes, story_scenes),
            1,
        ):
            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            for field in (
                "scene_id",
                "scene",
                "global_scene_number",
                "role",
                "beat",
                "purpose",
                "story_anchor",
            ):
                if clean(planned.get(field)) != clean(
                    generated.get(field)
                ):
                    fail(
                        f"Plan/story {field} mismatch: {scene_id}."
                    )


def main():
    log("=" * 62)
    log("KATHA LOK AI — STRICT STORY VERIFICATION")
    log("Version:", VERSION)
    log("=" * 62)

    input_data = verify_input()

    mode = input_data["mode"]
    configured_parts = input_data["parts"]
    scenes_expected = input_data["scenes"]
    topic = input_data["topic"]
    explicit_title = input_data["explicit_title"]
    source_text = input_data["source_text"]

    story = read_json(STORY_FILE)
    plan = read_json(PLAN_FILE)

    plan_parts, parts_expected = verify_plan(
        plan=plan,
        mode=mode,
        configured_parts=configured_parts,
        scenes_expected=scenes_expected,
        topic=topic,
    )

    if parts_expected != configured_parts:
        fail(
            "Generated plan does not follow configured PARTS."
        )

    (
        title,
        story_parts,
        all_scenes,
        expected_total,
    ) = verify_story_metadata(
        story=story,
        mode=mode,
        parts_expected=parts_expected,
        scenes_expected=scenes_expected,
        topic=topic,
        source_text=source_text,
        explicit_title=explicit_title,
    )

    word_counts = verify_story_parts(
        parts=story_parts,
        all_scenes=all_scenes,
        plan_parts=plan_parts,
        mode=mode,
        scenes_expected=scenes_expected,
    )

    combined_story = verify_story_text(
        story=story,
        parts=story_parts,
        mode=mode,
        source_text=source_text,
    )

    if mode == "short":
        verify_source_coverage(
            source_text,
            combined_story,
        )

    verify_plan_story_match(
        plan_parts,
        story_parts,
    )

    verify_title_file(title)

    generation_method = clean(
        story.get("generation_method")
    )
    model = clean(story.get("model"))

    if not generation_method:
        fail("Story generation_method metadata is missing.")

    if not model:
        fail("Story model metadata is missing.")

    if source_text:
        expected_source_words = word_count(source_text)
        actual_source_words = word_count(
            story.get("source_text", "")
        )

        if expected_source_words != actual_source_words:
            fail("Complete source word count mismatch.")

    log("=" * 62)
    log("STORY VALIDATION: SUCCESS")
    log("Verifier:", VERSION)
    log("Format:", mode)
    log("Title:", title)
    log("Configured PARTS:", configured_parts)
    log("Verified PARTS:", parts_expected)
    log("Scenes per part:", scenes_expected)
    log("Total scenes:", expected_total)
    log("Words per part:", word_counts)
    log("Source words:", word_count(source_text))
    log("Generated words:", word_count(combined_story))
    log("Generation method:", generation_method)
    log("Actual model:", model)
    log(
        "Verified: complete input source, source metadata, "
        "part count, scene count, scene IDs, scene order, "
        "narration integrity, SHORT word limits, title, "
        "story-plan consistency and output metadata."
    )
    log("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("STORY VALIDATION: CANCELLED")
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        fail(
            f"Unexpected verification error: "
            f"{type(exc).__name__}: {exc}"
        )