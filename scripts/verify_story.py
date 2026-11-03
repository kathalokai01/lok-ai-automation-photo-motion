#!/usr/bin/env python3
"""Verify source integrity, selected parts, story plan and narration records."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts_range,
    get_scenes,
    get_topic,
)
from generate_story import get_complete_source
from generate_story_plan import (
    VERSION as PLAN_VERSION,
    MAPPING_VERSION,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"
REPORT_FILE = ROOT / "output" / "story" / "story_verification.json"

VERSION = "story-verifier-v14-selected-parts-role-and-source-integrity"

SHORT_MIN_WORDS = 120
SHORT_PREFERRED_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180
MAX_TITLE_LENGTH = 180
MAX_PARTS = 1000
MAX_SCENES_PER_PART = 100
MAX_TOTAL_SCENES = 1000

BOUNDARY_ENDINGS = (
    "।", "॥", ".", "!", "?", "…", "...",
    ",", "，", ";", "؛", ":", "—", "–",
)

STOP_WORDS = {
    "और", "या", "लेकिन", "मगर", "क्योंकि", "इसलिए", "फिर",
    "तब", "जब", "जहाँ", "वहाँ", "यह", "वह", "एक", "कोई",
    "कुछ", "सभी", "बहुत", "था", "थी", "थे", "है", "हैं",
    "हो", "हुआ", "हुई", "हुए", "कर", "किया", "की", "का",
    "के", "में", "पर", "से", "तक", "लिए", "साथ", "अपने",
    "अपनी", "अपना", "नहीं", "नही", "भी", "ही", "तो", "अब",
    "बाद", "पहले", "उसने", "उसका", "उसकी", "उसके", "उनका",
    "उनकी", "जिस", "जिसने", "जिसका", "रहा", "रही", "रहे",
    "कहानी", "कथा", "बात", "तरह", "इन", "इसे", "उसे",
    "तथा", "अथवा", "केवल", "जबकि", "जिससे", "जिसके",
}


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalized_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", value).strip()


def word_count(value: Any) -> int:
    return len(re.findall(r"\S+", normalized_text(value)))


def positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        fail(f"{field} must be a positive integer.")

    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(f"{field} must be a positive integer.")

    if result < 1:
        fail(f"{field} must be greater than zero.")

    return result


def fail(message: str) -> None:
    raise RuntimeError(message)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            import os
            os.fsync(handle.fileno())

        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    if not path.is_file():
        fail(f"Required file is missing: {path.relative_to(ROOT)}")

    if path.stat().st_size <= 0:
        fail(f"Required file is empty: {path.relative_to(ROOT)}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Cannot read {path.name}: {exc}")

    if not isinstance(data, dict):
        fail(f"{path.name} must contain a JSON object.")

    return data


def verify_input() -> dict:
    if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size <= 0:
        fail("Input/topic.txt is missing or empty.")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()

    if mode not in {"short", "full"}:
        fail("FORMAT must be short or full.")

    try:
        minimum_parts, maximum_parts = get_parts_range(config)
    except (TypeError, ValueError) as exc:
        fail(f"Invalid PARTS range: {exc}")

    scenes_expected = positive_int(
        get_scenes(config),
        "Input SCENES",
    )

    if not 1 <= minimum_parts <= maximum_parts <= MAX_PARTS:
        fail("Configured PARTS range is outside supported limits.")

    if scenes_expected > MAX_SCENES_PER_PART:
        fail("SCENES exceeds the supported per-part limit.")

    if minimum_parts * scenes_expected > MAX_TOTAL_SCENES:
        fail("Minimum configured scene count exceeds safety limit.")

    topic = clean(get_topic(config))
    explicit_title = clean(config.get("TITLE", ""))

    source_text = normalized_text(
        get_complete_source(config, INPUT_FILE)
    )

    if not topic and not explicit_title and not source_text:
        fail("Provide TOPIC, TITLE or STORY_TEXT.")

    if source_text and word_count(source_text) < 20:
        fail(
            "STORY_TEXT contains fewer than 20 words. "
            "Verify the complete multiline input."
        )

    return {
        "config": config,
        "mode": mode,
        "minimum_parts": minimum_parts,
        "maximum_parts": maximum_parts,
        "scenes": scenes_expected,
        "topic": topic,
        "explicit_title": explicit_title,
        "source_text": source_text,
    }


def verify_selected_parts(
    story: dict,
    plan: dict,
    input_data: dict,
) -> tuple[int, int]:
    minimum = input_data["minimum_parts"]
    maximum = input_data["maximum_parts"]

    parts = story.get("parts")
    plan_parts = plan.get("parts")

    if not isinstance(parts, list) or not parts:
        fail("story.json must contain a non-empty parts list.")

    if not isinstance(plan_parts, list) or not plan_parts:
        fail("story_plan.json must contain a non-empty parts list.")

    selected = len(parts)

    if not minimum <= selected <= maximum:
        fail(
            f"Selected PARTS={selected} is outside configured "
            f"range {minimum}-{maximum}."
        )

    if len(plan_parts) != selected:
        fail(
            f"Story plan has {len(plan_parts)} parts; "
            f"story.json has {selected}."
        )

    for field in (
        "total_parts",
        "parts_count",
        "selected_parts_count",
    ):
        declared = story.get(field)

        if declared is not None:
            if positive_int(declared, f"story.json {field}") != selected:
                fail(
                    f"Story {field} does not match selected part count."
                )

    for field, expected in (
        ("configured_parts_min", minimum),
        ("configured_parts_max", maximum),
    ):
        declared = story.get(field)

        if declared is not None:
            if positive_int(declared, f"story.json {field}") != expected:
                fail(
                    f"Story {field} does not match Input/topic.txt."
                )

    specification = clean(
        story.get("configured_parts_specification")
    )

    expected_specification = (
        f"{minimum}-{maximum}"
        if minimum != maximum
        else str(minimum)
    )

    if specification and specification != expected_specification:
        fail(
            "Story configured_parts_specification does not match Input."
        )

    return selected, len(plan_parts)


def verify_plan_header(
    plan: dict,
    input_data: dict,
    selected_parts: int,
) -> list[dict]:
    mode = input_data["mode"]
    topic = input_data["topic"]
    scenes_expected = input_data["scenes"]

    if clean(plan.get("status")).lower() != "planned":
        fail("Story plan status must be planned.")

    if clean(plan.get("format")).lower() != mode:
        fail("Story plan FORMAT does not match Input.")

    if clean(plan.get("topic")) != topic:
        fail("Story plan topic does not match Input.")

    if clean(plan.get("title")) != topic:
        fail("Story plan title metadata does not match configured TOPIC.")

    if clean(plan.get("script_version")) != PLAN_VERSION:
        fail(
            "Story plan was generated by an incompatible planner version."
        )

    if positive_int(
        plan.get("mapping_version"),
        "Story plan mapping_version",
    ) != MAPPING_VERSION:
        fail("Story plan mapping_version is incompatible.")

    expected_metadata = {
        "parts_count": selected_parts,
        "scenes_per_part": scenes_expected,
        "total_scenes": selected_parts * scenes_expected,
        "total_main_scenes": selected_parts * scenes_expected,
        "total_hook_scenes": selected_parts,
        "total_suspense_scenes": max(0, selected_parts - 1),
        "total_resolution_scenes": 1,
        "total_extra_scenes": selected_parts + max(0, selected_parts - 1) + 1,
    }

    for field, expected in expected_metadata.items():
        actual = positive_int(
            plan.get(field),
            f"Story plan {field}",
        )
        if actual != expected:
            fail(
                f"Story plan {field} mismatch: "
                f"expected {expected}, found {actual}."
            )

    if clean(plan.get("scene_order")) != (
        "hook_then_main_then_suspense_or_resolution"
    ):
        fail("Story plan scene_order is invalid.")

    if clean(plan.get("main_scene_order")) != "part_then_scene":
        fail("Story plan main_scene_order is invalid.")

    fingerprint = clean(plan.get("source_fingerprint"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        fail("Story plan source_fingerprint is invalid.")

    source = plan.get("source")

    if not isinstance(source, dict):
        fail("Story plan source metadata is missing.")

    if source.get("api_calls") != 0:
        fail("Story planning must not make external API calls.")

    if source.get("planning_mode") != "offline_story_grounded":
        fail("Story plan planning_mode is invalid.")

    if source.get("story_text_provided") is not True:
        fail("Story plan was not built from complete story text.")

    if positive_int(
        source.get("story_word_count"),
        "Story plan source story_word_count",
    ) <= 0:
        fail("Story plan source story_word_count is invalid.")

    parts = plan.get("parts")

    if not isinstance(parts, list) or len(parts) != selected_parts:
        fail("Story plan part count does not match selected PARTS.")

    return parts


def verify_extra_scene(
    record: Any,
    part_number: int,
    kind: str,
    required: bool,
    final_part: bool,
    mode: str,
) -> dict | None:
    if not required:
        if record is not None:
            fail(
                f"Part {part_number} contains unexpected {kind} record."
            )
        return None

    if not isinstance(record, dict):
        fail(
            f"Part {part_number} is missing its separate {kind} record."
        )

    expected_id = f"part_{part_number:02d}_{kind}"

    expected_values = {
        "scene_id": expected_id,
        "role": kind,
        "kind": kind,
        "part": part_number,
        "scene": None,
        "global_scene_number": None,
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
        "is_final_part": final_part,
        "source_format": mode,
    }

    for field, expected in expected_values.items():
        if record.get(field) != expected:
            fail(
                f"{expected_id}: {field} mismatch; "
                f"expected {expected!r}, found {record.get(field)!r}."
            )

    for field in (
        "title",
        "beat",
        "beat_description",
        "purpose",
        "story_anchor",
        "instruction",
        "continuity_rule",
        "invention_policy",
        "visual_policy",
    ):
        if not clean(record.get(field)):
            fail(f"{expected_id}: required field {field} is missing.")

    return record


def verify_plan_parts(
    plan_parts: list[dict],
    input_data: dict,
    selected_parts: int,
) -> list[dict]:
    scenes_expected = input_data["scenes"]
    mode = input_data["mode"]
    verified_parts = []
    global_scene = 0
    all_anchors = []

    for part_number, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            fail(f"Plan part {part_number} must be an object.")

        expected_part_values = {
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "main_scene_count": scenes_expected,
            "hook_required": True,
            "suspense_required": part_number < selected_parts,
            "final_resolution": part_number == selected_parts,
        }

        for field, expected in expected_part_values.items():
            if part.get(field) != expected:
                fail(
                    f"Plan part {part_number} {field} mismatch."
                )

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            fail(
                f"Plan part {part_number} must contain exactly "
                f"{scenes_expected} main scenes."
            )

        if not clean(part.get("story_anchor")):
            fail(f"Plan part {part_number} has no story anchor.")

        for scene_number, scene in enumerate(scenes, 1):
            global_scene += 1

            if not isinstance(scene, dict):
                fail(
                    f"Plan main scene part {part_number}, "
                    f"scene {scene_number} is invalid."
                )

            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            expected_values = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_scene,
                "scene_id": scene_id,
                "role": "main",
            }

            for field, expected in expected_values.items():
                if scene.get(field) != expected:
                    fail(f"Plan {field} mismatch: {scene_id}.")

            for field in (
                "beat",
                "beat_description",
                "purpose",
                "story_anchor",
                "continuity_rule",
                "invention_policy",
                "visual_policy",
            ):
                if not clean(scene.get(field)):
                    fail(f"Plan {field} missing: {scene_id}.")

            all_anchors.append(clean(scene.get("story_anchor")))

        part_anchor = clean(" ".join(
            clean(scene.get("story_anchor")) for scene in scenes
        ))

        if part_anchor != clean(part.get("story_anchor")):
            fail(
                f"Plan part {part_number} story_anchor does not match "
                "its ordered main scene anchors."
            )

        hook = verify_extra_scene(
            part.get("hook_scene"),
            part_number,
            "hook",
            True,
            part_number == selected_parts,
            mode,
        )

        suspense = verify_extra_scene(
            part.get("suspense_scene"),
            part_number,
            "suspense",
            part_number < selected_parts,
            False,
            mode,
        )

        resolution = verify_extra_scene(
            part.get("resolution_scene"),
            part_number,
            "resolution",
            part_number == selected_parts,
            True,
            mode,
        )

        extras = part.get("extra_scenes")

        expected_extras = [hook]
        if suspense:
            expected_extras.append(suspense)
        if resolution:
            expected_extras.append(resolution)

        if not isinstance(extras, list):
            fail(f"Plan part {part_number} extra_scenes must be a list.")

        if extras != expected_extras:
            fail(
                f"Plan part {part_number} extra_scenes does not match "
                "its Hook/Suspense/Resolution records."
            )

        verified_parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "main_scene_count": len(scenes),
            "hook": hook["scene_id"],
            "suspense": suspense["scene_id"] if suspense else None,
            "resolution": resolution["scene_id"] if resolution else None,
            "main_scene_ids": [
                scene["scene_id"] for scene in scenes
            ],
        })

    return verified_parts


def verify_story_header(
    story: dict,
    input_data: dict,
    selected_parts: int,
) -> tuple[str, list[dict], list[dict]]:
    mode = input_data["mode"]
    topic = input_data["topic"]
    explicit_title = input_data["explicit_title"]
    source_text = input_data["source_text"]
    scenes_expected = input_data["scenes"]

    if clean(story.get("status")).lower() != "completed":
        fail("story.json status must be completed.")

    if clean(story.get("format")).lower() != mode:
        fail("Story FORMAT does not match Input.")

    title = clean(story.get("title"))

    if not title:
        fail("Generated story title is empty.")

    if len(title) > MAX_TITLE_LENGTH:
        fail("Generated story title exceeds 180 characters.")

    if clean(story.get("final_title")) != title:
        fail("Story final_title does not match story title.")

    if explicit_title and title != explicit_title:
        fail("Generated title differs from explicit TITLE in Input.")

    if clean(story.get("topic")) != topic:
        fail("Story topic does not match Input.")

    if normalized_text(story.get("source_text", "")) != source_text:
        fail(
            "Story source_text differs from complete STORY_TEXT "
            "in Input/topic.txt."
        )

    if clean(story.get("scene_order")) != "part_then_scene":
        fail("Story scene_order must be part_then_scene.")

    expected_values = {
        "total_parts": selected_parts,
        "parts_count": selected_parts,
        "scenes_per_part": scenes_expected,
        "total_scenes": selected_parts * scenes_expected,
        "selected_parts_count": selected_parts,
    }

    for field, expected in expected_values.items():
        if field not in story:
            fail(f"Story metadata field {field} is missing.")

        actual = positive_int(story.get(field), f"Story {field}")

        if actual != expected:
            fail(
                f"Story {field} mismatch: expected {expected}; "
                f"found {actual}."
            )

    source_words = word_count(source_text)

    if positive_int(
        story.get("source_word_count"),
        "Story source_word_count",
    ) != source_words:
        fail("Story source_word_count does not match complete input source.")

    fingerprint = clean(story.get("source_fingerprint"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        fail("Story source_fingerprint is missing or invalid.")

    if positive_int(
        story.get("corrected_complete_story_word_count"),
        "Story corrected_complete_story_word_count",
    ) <= 0:
        fail("Corrected complete story word count is invalid.")

    if not clean(story.get("generation_method")):
        fail("Story generation_method metadata is missing.")

    if not clean(story.get("model")):
        fail("Story model metadata is missing.")

    proofreading = story.get("proofreading")

    if not isinstance(proofreading, dict):
        fail("Story proofreading metadata is missing.")

    if source_text and proofreading.get("performed") is not True:
        fail(
            "Supplied story has no confirmed proofreading record."
        )

    parts = story.get("parts")
    all_scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != selected_parts:
        fail("Story part list does not match selected PARTS.")

    expected_total_scenes = selected_parts * scenes_expected

    if (
        not isinstance(all_scenes, list)
        or len(all_scenes) != expected_total_scenes
    ):
        fail(
            f"Story top-level scenes must contain "
            f"{expected_total_scenes} Main scenes."
        )

    return title, parts, all_scenes


def meaningful_tokens(value: str) -> set[str]:
    tokens = re.findall(
        r"[^\W_]+",
        value.lower(),
        flags=re.UNICODE,
    )

    return {
        token
        for token in tokens
        if len(token) > 2 and token not in STOP_WORDS
    }


def verify_source_coverage(
    source_text: str,
    generated_text: str,
    mode: str,
) -> dict:
    if not source_text:
        return {
            "performed": False,
            "source_terms": 0,
            "matched_terms": 0,
            "coverage_ratio": None,
            "status": "no_supplied_story",
        }

    source_terms = meaningful_tokens(source_text)
    generated_terms = meaningful_tokens(generated_text)

    if len(source_terms) < 12:
        return {
            "performed": False,
            "source_terms": len(source_terms),
            "matched_terms": len(source_terms.intersection(generated_terms)),
            "coverage_ratio": None,
            "status": "insufficient_distinctive_source_terms",
        }

    matched = source_terms.intersection(generated_terms)
    ratio = len(matched) / len(source_terms)

    # SHORT may condense a long source, so the lexical threshold is
    # deliberately lower than FULL. This is a warning detector, not
    # a semantic proof.
    threshold = 0.12 if mode == "short" else 0.20

    if ratio < threshold:
        fail(
            "Generated story has very low meaningful-word overlap "
            f"with supplied source: {ratio:.1%}; minimum {threshold:.0%}. "
            "Possible unrelated replacement story."
        )

    return {
        "performed": True,
        "source_terms": len(source_terms),
        "matched_terms": len(matched),
        "coverage_ratio": round(ratio, 4),
        "minimum_ratio": threshold,
        "status": "passed",
        "limitation": (
            "Vocabulary overlap cannot prove semantic preservation, "
            "event completeness or unchanged character relationships."
        ),
    }


def verify_story_parts(
    parts: list[dict],
    all_scenes: list[dict],
    plan_parts: list[dict],
    input_data: dict,
    selected_parts: int,
) -> tuple[list[int], list[dict]]:
    mode = input_data["mode"]
    scenes_expected = input_data["scenes"]

    seen_ids = set()
    flattened = []
    word_counts = []
    part_results = []

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story part {part_number} must be an object.")

        expected_part_id = f"part_{part_number:02d}"

        if positive_int(
            part.get("part"),
            f"Story part {part_number} number",
        ) != part_number:
            fail(f"Story part numbering mismatch: {part_number}.")

        if clean(part.get("part_id")) != expected_part_id:
            fail(f"Story part ID mismatch: {expected_part_id}.")

        narration = clean(part.get("narration"))

        if not narration:
            fail(f"Part {part_number} narration is empty.")

        amount = word_count(narration)

        if positive_int(
            part.get("word_count"),
            f"Part {part_number} word_count",
        ) != amount:
            fail(f"Part {part_number} word_count metadata mismatch.")

        if mode == "short":
            if not SHORT_MIN_WORDS <= amount <= SHORT_PREFERRED_MAX_WORDS:
                fail(
                    f"SHORT part {part_number} has {amount} words; "
                    f"required range is 120–150."
                )

            if amount > SHORT_HARD_MAX_WORDS:
                fail(
                    f"SHORT part {part_number} exceeds hard limit 180."
                )

        if not narration[-1] in BOUNDARY_ENDINGS:
            fail(
                f"Part {part_number} ends at an incomplete or "
                "unrecognized sentence/clause boundary."
            )

        scenes = part.get("scenes")
        planned_scenes = plan_parts[part_number - 1].get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            fail(
                f"Part {part_number} must contain exactly "
                f"{scenes_expected} Main scenes."
            )

        if not isinstance(planned_scenes, list) or len(planned_scenes) != scenes_expected:
            fail(f"Plan scene count mismatch in part {part_number}.")

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

            if clean(scene.get("scene_id")) != scene_id:
                fail(f"Story scene ID mismatch: {scene_id}.")

            expected_numbers = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_number,
            }

            for field, expected in expected_numbers.items():
                if positive_int(
                    scene.get(field),
                    f"{scene_id} {field}",
                ) != expected:
                    fail(f"Story {field} mismatch: {scene_id}.")

            if scene_id in seen_ids:
                fail(f"Duplicate story scene ID: {scene_id}.")

            seen_ids.add(scene_id)

            planned = planned_scenes[scene_number - 1]

            if not isinstance(planned, dict):
                fail(f"Plan scene is invalid: {scene_id}.")

            for field in (
                "scene_id",
                "part",
                "scene",
                "global_scene_number",
                "role",
                "beat",
                "purpose",
                "story_anchor",
            ):
                if clean(scene.get(field)) != clean(planned.get(field)):
                    fail(
                        f"Story/plan {field} mismatch: {scene_id}."
                    )

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    fail(f"Story {field} is missing: {scene_id}.")

            if clean(scene.get("visual")) != clean(
                scene.get("visual_prompt")
            ):
                fail(f"Story visual and visual_prompt differ: {scene_id}.")

            expected_suspense = clean(
                planned.get("suspense")
                or planned.get("suspense_prompt")
                or planned.get("purpose")
            )

            if clean(scene.get("suspense")) != expected_suspense:
                fail(f"Story suspense marker mismatch: {scene_id}.")

            if not isinstance(scene.get("dialogue", ""), str):
                fail(f"Story dialogue must be text: {scene_id}.")

            scene_text = clean(scene.get("narration"))
            if not scene_text:
                fail(f"Scene narration is empty: {scene_id}.")

            scene_narrations.append(scene_text)
            flattened.append(scene)

        combined = clean(" ".join(scene_narrations))

        if combined != narration:
            fail(
                f"Part {part_number} narration does not match "
                "its ordered scene narration."
            )

        if word_count(combined) != amount:
            fail(
                f"Part {part_number} scene reconstruction changed "
                "the narration word count."
            )

        word_counts.append(amount)

        part_results.append({
            "part": part_number,
            "part_id": expected_part_id,
            "word_count": amount,
            "scene_count": len(scenes),
            "scene_ids": [
                scene["scene_id"] for scene in scenes
            ],
            "narration_sha256": sha256_text(narration),
            "natural_ending_boundary": narration[-1],
            "status": "verified",
        })

    if len(seen_ids) != len(all_scenes):
        fail("Top-level scene count differs from unique Part scenes.")

    if flattened != all_scenes:
        fail(
            "Top-level scenes do not match the ordered Part scene records."
        )

    return word_counts, part_results


def verify_story_text(
    story: dict,
    parts: list[dict],
    mode: str,
) -> str:
    combined = clean(
        " ".join(clean(part.get("narration")) for part in parts)
    )

    if not combined:
        fail("Combined story narration is empty.")

    if clean(story.get("story_text")) != combined:
        fail("story_text does not match ordered Part narrations.")

    declared_total = positive_int(
        story.get("total_story_words"),
        "Story total_story_words",
    )

    actual_total = word_count(combined)

    if declared_total != actual_total:
        fail(
            f"total_story_words mismatch: declared {declared_total}; "
            f"actual {actual_total}."
        )

    if mode == "short":
        if any(
            not SHORT_MIN_WORDS <= word_count(part.get("narration"))
            <= SHORT_PREFERRED_MAX_WORDS
            for part in parts
        ):
            fail("At least one SHORT part violates 120–150 word target.")

    return combined


def verify_plan_story_match(
    plan_parts: list[dict],
    story_parts: list[dict],
) -> None:
    if len(plan_parts) != len(story_parts):
        fail("Plan/story part counts differ.")

    for part_number, (planned_part, story_part) in enumerate(
        zip(plan_parts, story_parts),
        1,
    ):
        plan_scenes = planned_part.get("scenes")
        story_scenes = story_part.get("scenes")

        if not isinstance(plan_scenes, list) or not isinstance(story_scenes, list):
            fail(f"Plan/story scene lists are invalid in part {part_number}.")

        if len(plan_scenes) != len(story_scenes):
            fail(f"Plan/story scene counts differ in part {part_number}.")

        for scene_number, (planned, generated) in enumerate(
            zip(plan_scenes, story_scenes),
            1,
        ):
            if not isinstance(planned, dict) or not isinstance(generated, dict):
                fail(f"Invalid plan/story record in part {part_number}.")

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
                if clean(planned.get(field)) != clean(generated.get(field)):
                    fail(f"Plan/story {field} mismatch: {scene_id}.")


def verify_title_file(title: str) -> None:
    if not TITLE_FILE.is_file():
        fail("final_title.txt is missing.")

    try:
        saved_title = clean(
            TITLE_FILE.read_text(encoding="utf-8-sig")
        )
    except (OSError, UnicodeError) as exc:
        fail(f"Cannot read final_title.txt: {exc}")

    if not saved_title:
        fail("final_title.txt is empty.")

    if saved_title != title:
        fail("final_title.txt does not match story.json title.")


def verify_story(
    story: dict,
    plan: dict,
    input_data: dict,
) -> dict:
    selected_parts, _ = verify_selected_parts(
        story,
        plan,
        input_data,
    )

    plan_parts = verify_plan_header(
        plan,
        input_data,
        selected_parts,
    )

    plan_results = verify_plan_parts(
        plan_parts,
        input_data,
        selected_parts,
    )

    title, story_parts, all_scenes = verify_story_header(
        story,
        input_data,
        selected_parts,
    )

    word_counts, part_results = verify_story_parts(
        story_parts,
        all_scenes,
        plan_parts,
        input_data,
        selected_parts,
    )

    combined_story = verify_story_text(
        story,
        story_parts,
        input_data["mode"],
    )

    source_coverage = verify_source_coverage(
        input_data["source_text"],
        combined_story,
        input_data["mode"],
    )

    verify_plan_story_match(plan_parts, story_parts)
    verify_title_file(title)

    if input_data["explicit_title"] and title != input_data["explicit_title"]:
        fail("Explicit TITLE was not preserved.")

    source_text = input_data["source_text"]

    if source_text:
        if normalized_text(story.get("source_text")) != normalized_text(source_text):
            fail("Complete source story was changed in story metadata.")

        if word_count(story.get("source_text")) != word_count(source_text):
            fail("Complete source word count was not preserved.")

    return {
        "status": "completed",
        "verification_version": VERSION,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": input_data["mode"],
        "title": title,
        "topic": input_data["topic"],
        "story_fingerprint": sha256_json(story),
        "plan_fingerprint": sha256_json(plan),
        "summary": {
            "configured_parts_min": input_data["minimum_parts"],
            "configured_parts_max": input_data["maximum_parts"],
            "selected_parts": selected_parts,
            "scenes_per_part": input_data["scenes"],
            "total_main_scenes": len(all_scenes),
            "total_story_words": word_count(combined_story),
            "words_per_part": word_counts,
            "hook_count": selected_parts,
            "suspense_count": max(0, selected_parts - 1),
            "resolution_count": 1,
            "source_story_provided": bool(source_text),
            "source_coverage": source_coverage,
        },
        "parts": part_results,
        "plan_parts": plan_results,
        "checks": {
            "input_format": True,
            "configured_parts_range": True,
            "selected_parts_in_range": True,
            "story_plan_version": True,
            "story_plan_mapping_version": True,
            "story_plan_source_metadata": True,
            "main_scene_ids_and_order": True,
            "hook_suspense_resolution_roles": True,
            "story_part_ids_and_order": True,
            "scene_narration_integrity": True,
            "top_level_scene_consistency": True,
            "short_word_limits": input_data["mode"] != "short" or True,
            "natural_part_boundaries": True,
            "story_title": True,
            "title_file": True,
            "source_text_integrity": True,
            "source_vocabulary_coverage": source_coverage["status"],
            "no_external_api_calls": plan.get("source", {}).get("api_calls") == 0,
        },
        "api_calls_made": 0,
        "story_modified_by_verifier": False,
        "semantic_preservation_proven": False,
    }


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — STRICT STORY VERIFICATION")
    log("Verifier:", VERSION)
    log("=" * 64)

    input_data = verify_input()
    story = read_json(STORY_FILE)
    plan = read_json(PLAN_FILE)

    report = verify_story(story, plan, input_data)
    write_json(REPORT_FILE, report)

    summary = report["summary"]

    log("VALIDATION: SUCCESS")
    log("FORMAT:", report["format"])
    log("TITLE:", report["title"])
    log(
        "CONFIGURED PARTS RANGE:",
        f"{summary['configured_parts_min']}-"
        f"{summary['configured_parts_max']}",
    )
    log("SELECTED PARTS:", summary["selected_parts"])
    log("SCENES PER PART:", summary["scenes_per_part"])
    log("MAIN SCENES:", summary["total_main_scenes"])
    log("HOOKS:", summary["hook_count"])
    log("SUSPENSE RECORDS:", summary["suspense_count"])
    log("RESOLUTION RECORDS:", summary["resolution_count"])
    log("TOTAL STORY WORDS:", summary["total_story_words"])
    log("WORDS PER PART:", summary["words_per_part"])
    log("SOURCE COVERAGE:", summary["source_coverage"]["status"])
    log("API CALLS: 0")
    log("STORY MODIFIED: false")
    log("REPORT:", REPORT_FILE.relative_to(ROOT))
    log("=" * 64)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("STORY VALIDATION: CANCELLED")
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        log(
            "STORY VALIDATION: FAILED",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)