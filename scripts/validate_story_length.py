#!/usr/bin/env python3
"""Validate final story, Part lengths, Scene mapping and Story Plan."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    get_format,
    get_parts_range,
    get_scenes_range,
    get_selected_parts,
    get_selected_parts_record,
    load_and_validate,
)
from story_length_policy import (
    POLICY_VERSION,
    SHORT_MIN_WORDS_PER_PART,
    SHORT_PREFERRED_MAX_WORDS_PER_PART,
    SHORT_HARD_MAX_WORDS_PER_PART,
    SHORT_FINAL_MERGED_MAX_WORDS,
    clean_text,
    normalize_format,
    sha256_text,
    verify_revision_evidence,
    word_count,
)

STORY_DIR = ROOT / "output" / "story"
STORY_FILE = STORY_DIR / "story.json"
PLAN_FILE = STORY_DIR / "story_plan.json"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
REPORT_FILE = STORY_DIR / "story_length_validation.json"

REPORT_VERSION = "story-length-validation-v14-natural-boundary-reporting"

MAX_PARTS = 1000
MAX_SCENES_PER_PART = 500
MAX_TOTAL_SCENES = 1000
MAX_STORY_WORDS = 10_000_000
MAX_TITLE_LENGTH = 180

BOUNDARY_ENDINGS = (
    "।", "॥", ".", "!", "?", "…", "...",
    ",", "，", ";", "؛", ":", "—", "–",
)
BOUNDARY_CLOSERS = frozenset(
    "\"'”’»›」』】〕）)]}>"
)


def ends_at_natural_boundary(value: Any) -> bool:
    """Accept punctuation followed by closing quotes/brackets, not dangling text."""
    text = clean_text(value).rstrip()
    while text and text[-1] in BOUNDARY_CLOSERS:
        text = text[:-1].rstrip()
    return bool(text) and any(
        text.endswith(ending) for ending in BOUNDARY_ENDINGS
    )


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def as_int(value: Any, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", value).strip()


def fingerprint(data: Any) -> str:
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path, required: bool = True) -> Any:
    if not path.is_file() or path.stat().st_size <= 0:
        if required:
            fail(f"Required file missing or empty: {path.relative_to(ROOT)}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Cannot read {path.name}: {exc}")


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


def extract_text(record: dict, fields: tuple[str, ...]) -> str:
    for field in fields:
        value = record.get(field)
        if isinstance(value, str) and normalize_text(value):
            return normalize_text(value)
    return ""


def scene_text(scene: dict) -> str:
    return extract_text(
        scene,
        ("narration", "narration_text", "voiceover", "text"),
    )


def part_text(part: dict) -> str:
    return extract_text(
        part,
        ("narration", "narration_text", "voiceover", "story", "text", "content"),
    )


def verify_input() -> dict:
    input_file = ROOT / "Input" / "topic.txt"

    if not input_file.is_file():
        fail("Input/topic.txt is missing.")

    config = load_and_validate(str(input_file))
    mode = normalize_format(get_format(config))
    minimum_parts, maximum_parts = get_parts_range(config)
    scenes_min, scenes_max = get_scenes_range(config)

    if not 1 <= minimum_parts <= maximum_parts <= MAX_PARTS:
        fail("Configured PARTS range is invalid.")

    if not 1 <= scenes_min <= scenes_max <= MAX_SCENES_PER_PART:
        fail("Configured SCENES range is invalid.")

    if maximum_parts * scenes_max > MAX_TOTAL_SCENES:
        fail("Configured maximum PARTS x SCENES exceeds safety limit.")

    if mode == "full" and (minimum_parts, maximum_parts) != (1, 1):
        fail("FULL format must contain exactly one Part.")

    return {
        "config": config,
        "mode": mode,
        "minimum_parts": minimum_parts,
        "maximum_parts": maximum_parts,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
    }


def verify_selection(
    config: dict,
    story: dict,
    actual_parts: int,
    minimum: int,
    maximum: int,
) -> dict | None:
    if not minimum <= actual_parts <= maximum:
        fail(
            f"Actual PARTS={actual_parts} is outside "
            f"configured range {minimum}-{maximum}."
        )

    if normalize_format(story.get("format")) == "full" and actual_parts != 1:
        fail("FULL format must contain one Part.")

    for field in (
        "total_parts",
        "parts_count",
        "selected_parts",
        "selected_parts_count",
    ):
        value = story.get(field)
        if value is not None and as_int(value) != actual_parts:
            fail(f"Story metadata {field} does not match actual Parts.")

    record = get_selected_parts_record(config)

    if minimum != maximum and not isinstance(record, dict):
        fail("Persisted PARTS selection record is missing for range selection.")

    if isinstance(record, dict):
        if as_int(record.get("selected_parts")) != actual_parts:
            fail("Persisted selected PARTS does not match story.json.")
        if as_int(record.get("minimum_parts")) != minimum:
            fail("Persisted PARTS minimum does not match Input.")
        if as_int(record.get("maximum_parts")) != maximum:
            fail("Persisted PARTS maximum does not match Input.")

    selected = get_selected_parts(config, required=False)

    if selected is not None and as_int(selected) != actual_parts:
        fail("Input selected PARTS does not match story.json.")

    return record


def verify_story_header(
    story: dict,
    mode: str,
    parts_count: int,
    scenes_min: int,
    scenes_max: int,
) -> tuple[list[dict], list[dict], list[int]]:
    if story.get("status") != "completed":
        fail("story.json status must be completed.")

    if normalize_format(story.get("format")) != mode:
        fail("Story FORMAT does not match Input/topic.txt.")

    title = clean_text(story.get("title"))

    if not title or len(title) > MAX_TITLE_LENGTH:
        fail("Story title is empty or too long.")

    if clean_text(story.get("final_title")) != title:
        fail("Story final_title does not match title.")

    if clean_text(story.get("scene_order")) != "part_then_scene":
        fail("Story scene_order metadata is invalid.")

    if clean_text(story.get("role_order")) != (
        "hook_then_main_then_suspense_or_resolution"
    ):
        fail("Story role_order metadata is invalid.")

    if not clean_text(story.get("generation_method")):
        fail("Story generation_method is missing.")

    if not clean_text(story.get("model")):
        fail("Story model metadata is missing.")

    proofreading = story.get("proofreading")

    if not isinstance(proofreading, dict):
        fail("Story proofreading metadata is missing.")

    if clean_text(story.get("source_text")):
        if proofreading.get("performed") is not True:
            fail("Supplied story proofreading is not marked completed.")

    for field in (
        "total_parts",
        "parts_count",
        "selected_parts",
        "selected_parts_count",
    ):
        if as_int(story.get(field)) != parts_count:
            fail(f"Story {field} does not match actual Part count.")

    for field, expected in (
        ("scenes_min", scenes_min),
        ("scenes_max", scenes_max),
        ("scenes_per_part", scenes_min),
    ):
        if as_int(story.get(field)) != expected:
            fail(f"Story {field} does not match Input.")

    if story.get("scenes_range") != [scenes_min, scenes_max]:
        fail("Story scenes_range does not match Input.")

    source_text = story.get("source_text", "")

    if not isinstance(source_text, str):
        fail("Story source_text must be text.")

    if as_int(story.get("source_word_count"), 0) != word_count(source_text):
        fail("Story source_word_count mismatch.")

    if not re.fullmatch(
        r"[a-fA-F0-9]{64}",
        clean_text(story.get("source_fingerprint")),
    ):
        fail("Story source_fingerprint is invalid.")

    corrected = story.get("corrected_complete_story")

    if not isinstance(corrected, str) or not normalize_text(corrected):
        fail("Corrected complete story is missing.")

    corrected_words = word_count(corrected)

    if as_int(story.get("corrected_complete_story_word_count")) != corrected_words:
        fail("Corrected complete story word count mismatch.")

    if normalize_text(story.get("story_text")) != normalize_text(corrected):
        fail("story_text differs from corrected_complete_story.")

    if as_int(story.get("total_story_words")) != corrected_words:
        fail("Story total_story_words mismatch.")

    parts = story.get("parts")
    scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != parts_count:
        fail("Story Parts list does not match selected PARTS.")

    if not isinstance(scenes, list):
        fail("Story top-level Main Scenes list is missing.")

    if len(scenes) > MAX_TOTAL_SCENES:
        fail("Story exceeds total Main Scene safety limit.")

    counts = story.get("actual_scene_counts")

    if not isinstance(counts, list) or len(counts) != parts_count:
        fail("Story actual_scene_counts is invalid.")

    counts = [as_int(value) for value in counts]

    if any(not scenes_min <= value <= scenes_max for value in counts):
        fail("A Part has a Scene count outside configured range.")

    if sum(counts) != len(scenes):
        fail("actual_scene_counts does not match top-level Scene list.")

    if as_int(story.get("total_scenes")) != len(scenes):
        fail("Story total_scenes mismatch.")

    if as_int(story.get("total_main_scenes")) != len(scenes):
        fail("Story total_main_scenes mismatch.")

    return parts, scenes, counts


def verify_short_part(
    part: dict,
    narration: str,
    part_number: int,
    parts_count: int,
) -> dict:
    amount = word_count(narration)
    final_part = part_number == parts_count
    merged = final_part and part.get("final_part_merged") is True

    if amount < SHORT_MIN_WORDS_PER_PART:
        fail(
            f"SHORT Part {part_number} has {amount} words; "
            f"minimum is {SHORT_MIN_WORDS_PER_PART}."
        )

    maximum = (
        SHORT_FINAL_MERGED_MAX_WORDS
        if merged
        else SHORT_HARD_MAX_WORDS_PER_PART
    )

    if amount > maximum:
        fail(
            f"SHORT Part {part_number} has {amount} words; "
            f"maximum is {maximum}."
        )

    evidence_verified = False

    if amount > SHORT_PREFERRED_MAX_WORDS_PER_PART and not merged:
        evidence = part.get("revision_evidence")

        if not isinstance(evidence, dict):
            fail(
                f"SHORT Part {part_number} exceeds preferred "
                "150 words without revision evidence."
            )

        if verify_revision_evidence(evidence, narration) is not True:
            fail(f"SHORT Part {part_number} revision evidence is invalid.")

        evidence_verified = True

    return {
        "minimum_words": SHORT_MIN_WORDS_PER_PART,
        "preferred_maximum_words": SHORT_PREFERRED_MAX_WORDS_PER_PART,
        "hard_maximum_words": maximum,
        "final_part_merged": merged,
        "revision_evidence_verified": evidence_verified,
    }


def verify_role(
    record: Any,
    part_number: int,
    role: str,
    final_part: bool,
    expected_anchor: str,
) -> dict:
    if not isinstance(record, dict):
        fail(f"Part {part_number} is missing {role} record.")

    expected_id = f"part_{part_number:02d}_{role}"

    expected_fields = {
        "part": part_number,
        "role": role,
        "scene_id": expected_id,
        "scene": None,
        "global_scene_number": None,
        "is_final_part": final_part,
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
    }

    for field, expected in expected_fields.items():
        if record.get(field) != expected:
            fail(f"{expected_id} has invalid {field}.")

    if normalize_text(record.get("story_anchor")) != normalize_text(expected_anchor):
        fail(f"{expected_id} story anchor mismatch.")

    for field in (
        "purpose",
        "instruction",
        "continuity_rule",
        "invention_policy",
        "visual_policy",
    ):
        if not clean_text(record.get(field)):
            fail(f"{expected_id} is missing {field}.")

    return record


def verify_plan(
    plan: dict,
    story: dict,
    mode: str,
    parts_count: int,
    scenes_min: int,
    scenes_max: int,
    actual_counts: list[int],
) -> list[dict]:
    if not isinstance(plan, dict):
        fail("story_plan.json must contain a JSON object.")

    if plan.get("status") != "planned":
        fail("Story Plan status must be planned.")

    if normalize_format(plan.get("format")) != mode:
        fail("Story Plan FORMAT mismatch.")

    if clean_text(plan.get("topic")) != clean_text(story.get("topic")):
        fail("Story Plan topic does not match story.json.")

    if as_int(plan.get("parts_count")) != parts_count:
        fail("Story Plan parts_count mismatch.")

    if as_int(plan.get("scenes_min")) != scenes_min:
        fail("Story Plan scenes_min mismatch.")

    if as_int(plan.get("scenes_max")) != scenes_max:
        fail("Story Plan scenes_max mismatch.")

    if plan.get("scenes_range") != [scenes_min, scenes_max]:
        fail("Story Plan scenes_range mismatch.")

    if plan.get("actual_scene_counts") != actual_counts:
        fail("Story Plan actual Scene counts differ from story.json.")

    if as_int(plan.get("total_main_scenes")) != sum(actual_counts):
        fail("Story Plan total_main_scenes mismatch.")

    if as_int(plan.get("total_scenes")) != sum(actual_counts):
        fail("Story Plan total_scenes mismatch.")

    if as_int(plan.get("total_hook_scenes")) != parts_count:
        fail("Story Plan must contain one Hook per Part.")

    if as_int(plan.get("total_suspense_scenes")) != max(0, parts_count - 1):
        fail("Story Plan Suspense count mismatch.")

    if as_int(plan.get("total_resolution_scenes")) != 1:
        fail("Story Plan must contain one final Resolution.")

    plan_hash = clean_text(plan.get("source_fingerprint"))

    if not re.fullmatch(r"[a-fA-F0-9]{64}", plan_hash):
        fail("Story Plan source_fingerprint is invalid.")

    if clean_text(story.get("story_plan_fingerprint")) != plan_hash:
        fail("Story and Story Plan fingerprints do not match.")

    payload = plan.get("source_payload")

    if not isinstance(payload, dict):
        fail("Story Plan source_payload is missing.")

    if fingerprint(payload) != plan_hash:
        fail("Story Plan source fingerprint verification failed.")

    if normalize_text(payload.get("story_text")) != normalize_text(
        story.get("corrected_complete_story")
    ):
        fail("Story Plan was not built from the final corrected story.")

    if normalize_format(payload.get("format")) != mode:
        fail("Story Plan source FORMAT mismatch.")

    if as_int(payload.get("parts_count")) != parts_count:
        fail("Story Plan source PARTS count mismatch.")

    if as_int(payload.get("scenes_min")) != scenes_min:
        fail("Story Plan source SCENES minimum mismatch.")

    if as_int(payload.get("scenes_max")) != scenes_max:
        fail("Story Plan source SCENES maximum mismatch.")

    source = plan.get("source")

    if not isinstance(source, dict):
        fail("Story Plan source metadata is missing.")

    if as_int(source.get("story_word_count")) != word_count(
        story.get("corrected_complete_story")
    ):
        fail("Story Plan source story_word_count mismatch.")

    plan_parts = plan.get("parts")

    if not isinstance(plan_parts, list) or len(plan_parts) != parts_count:
        fail("Story Plan Parts list is invalid.")

    return plan_parts

def verify_plan_part(
    plan_part: dict,
    part_number: int,
    expected_count: int,
    parts_count: int,
) -> list[dict]:
    if not isinstance(plan_part, dict):
        fail(f"Story Plan Part {part_number} is invalid.")

    if as_int(plan_part.get("part")) != part_number:
        fail(f"Story Plan Part {part_number} numbering mismatch.")

    if clean_text(plan_part.get("part_id")) != f"part_{part_number:02d}":
        fail(f"Story Plan Part {part_number} ID mismatch.")

    scenes = plan_part.get("scenes")

    if not isinstance(scenes, list) or len(scenes) != expected_count:
        fail(f"Story Plan Part {part_number} Scene count mismatch.")

    if as_int(plan_part.get("main_scene_count")) != expected_count:
        fail(f"Story Plan Part {part_number} main_scene_count mismatch.")

    anchors = []

    for scene_number, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            fail(f"Invalid Story Plan Scene in Part {part_number}.")

        expected_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

        if scene.get("scene_id") != expected_id:
            fail(f"Story Plan Scene ID mismatch: {expected_id}.")

        if scene.get("role") != "main":
            fail(f"Story Plan Scene role mismatch: {expected_id}.")

        if as_int(scene.get("part")) != part_number:
            fail(f"Story Plan Scene Part mismatch: {expected_id}.")

        if as_int(scene.get("scene")) != scene_number:
            fail(f"Story Plan Scene number mismatch: {expected_id}.")

        anchor = normalize_text(scene.get("story_anchor"))

        if not anchor or normalize_text(scene.get("narration")) != anchor:
            fail(f"Story Plan Scene narration mismatch: {expected_id}.")

        for field in (
            "beat",
            "purpose",
            "continuity_rule",
            "invention_policy",
            "visual_policy",
        ):
            if not clean_text(scene.get(field)):
                fail(f"Story Plan Scene {expected_id} is missing {field}.")

        anchors.append(anchor)

    combined = normalize_text(" ".join(anchors))

    if normalize_text(plan_part.get("narration")) != combined:
        fail(f"Story Plan Part {part_number} narration mismatch.")

    if normalize_text(plan_part.get("story_anchor")) != combined:
        fail(f"Story Plan Part {part_number} story_anchor mismatch.")

    final_part = part_number == parts_count

    hook = verify_role(
        plan_part.get("hook_scene"),
        part_number,
        "hook",
        final_part,
        anchors[0],
    )

    suspense = None

    if final_part:
        if plan_part.get("suspense_scene") is not None:
            fail("Final Part must not contain Suspense.")
    else:
        suspense = verify_role(
            plan_part.get("suspense_scene"),
            part_number,
            "suspense",
            False,
            anchors[-1],
        )

    resolution = None

    if final_part:
        resolution = verify_role(
            plan_part.get("resolution_scene"),
            part_number,
            "resolution",
            True,
            anchors[-1],
        )
    elif plan_part.get("resolution_scene") is not None:
        fail("Non-final Part must not contain Resolution.")

    expected_extras = [
        item for item in (hook, suspense, resolution)
        if isinstance(item, dict)
    ]

    if plan_part.get("extra_scenes") != expected_extras:
        fail(f"Story Plan Part {part_number} extra_scenes mismatch.")

    return scenes


def verify_story_parts(
    parts: list[dict],
    all_scenes: list[dict],
    plan_parts: list[dict],
    actual_counts: list[int],
    mode: str,
) -> tuple[list[dict], list[str]]:
    results = []
    boundary_errors = []
    flattened_scenes = []
    flattened_text = []
    global_scene = 0
    parts_count = len(parts)

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story Part {part_number} is invalid.")

        if as_int(part.get("part")) != part_number:
            fail(f"Story Part {part_number} numbering mismatch.")

        expected_part_id = f"part_{part_number:02d}"

        if clean_text(part.get("part_id")) != expected_part_id:
            fail(f"Story Part ID mismatch: {expected_part_id}.")

        narration = part_text(part)

        if not narration:
            fail(f"Story Part {part_number} narration is empty.")

        amount = word_count(narration)

        if as_int(part.get("word_count")) != amount:
            fail(f"Story Part {part_number} word_count mismatch.")

        if normalize_text(part.get("story_anchor")) != narration:
            fail(f"Story Part {part_number} story_anchor mismatch.")

        if mode == "short":
            length_result = verify_short_part(
                part,
                narration,
                part_number,
                parts_count,
            )
        else:
            length_result = {
                "minimum_words": None,
                "preferred_maximum_words": None,
                "hard_maximum_words": None,
                "final_part_merged": False,
                "revision_evidence_verified": False,
            }

        scenes = part.get("scenes")
        expected_count = actual_counts[part_number - 1]

        plan_scenes = verify_plan_part(
            plan_parts[part_number - 1],
            part_number,
            expected_count,
            parts_count,
        )

        if not isinstance(scenes, list) or len(scenes) != expected_count:
            fail(f"Story Part {part_number} Scene count mismatch.")

        if as_int(part.get("main_scene_count")) != expected_count:
            fail(f"Part {part_number} main_scene_count mismatch.")

        if as_int(part.get("actual_scene_count")) != expected_count:
            fail(f"Part {part_number} actual_scene_count mismatch.")

        scene_texts = []
        scene_ids = []

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                fail(f"Part {part_number} has an invalid Scene.")

            global_scene += 1

            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

            expected = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_scene,
                "scene_id": scene_id,
                "role": "main",
            }

            for field, value in expected.items():
                if scene.get(field) != value:
                    fail(f"Story Scene {scene_id} has invalid {field}.")

            narration_text = scene_text(scene)

            if not narration_text:
                fail(f"Story Scene {scene_id} has no narration.")

            planned = plan_scenes[scene_number - 1]

            if normalize_text(planned.get("narration")) != narration_text:
                fail(f"Story/Plan narration mismatch for {scene_id}.")

            if normalize_text(planned.get("story_anchor")) != narration_text:
                fail(f"Story/Plan anchor mismatch for {scene_id}.")

            visual = clean_text(scene.get("visual"))
            visual_prompt = clean_text(scene.get("visual_prompt"))

            if not visual or not visual_prompt:
                fail(f"Story Scene {scene_id} has no visual prompt.")

            if normalize_text(visual) != normalize_text(visual_prompt):
                fail(f"Story Scene {scene_id} visual fields disagree.")

            scene_texts.append(narration_text)
            scene_ids.append(scene_id)
            flattened_scenes.append(scene)

        if normalize_text(" ".join(scene_texts)) != narration:
            fail(f"Scene narration does not reconstruct Part {part_number}.")

        final_part = part_number == parts_count

        hook = verify_role(
            part.get("hook_scene"),
            part_number,
            "hook",
            final_part,
            scene_texts[0],
        )

        suspense = None

        if final_part:
            if part.get("suspense_scene") is not None:
                fail("Final Part must not have a Suspense Scene.")
        else:
            suspense = verify_role(
                part.get("suspense_scene"),
                part_number,
                "suspense",
                False,
                scene_texts[-1],
            )

        resolution = None

        if final_part:
            resolution = verify_role(
                part.get("resolution_scene"),
                part_number,
                "resolution",
                True,
                scene_texts[-1],
            )
        elif part.get("resolution_scene") is not None:
            fail("Non-final Part must not have a Resolution Scene.")

        expected_extras = [
            item for item in (hook, suspense, resolution)
            if isinstance(item, dict)
        ]

        if part.get("extra_scenes") != expected_extras:
            fail(f"Story Part {part_number} extra_scenes mismatch.")

        boundary_valid = ends_at_natural_boundary(narration)

        if not boundary_valid:
            boundary_errors.append(
                f"Part {part_number} ends without a complete "
                "sentence or natural clause boundary."
            )

        results.append({
            "part_number": part_number,
            "part_id": expected_part_id,
            "word_count": amount,
            "scene_count": expected_count,
            "scene_ids": scene_ids,
            "narration_sha256": sha256_text(narration),
            "natural_boundary_valid": boundary_valid,
            "passed": boundary_valid,
            **length_result,
        })

        flattened_text.append(narration)

    if flattened_scenes != all_scenes:
        fail("Top-level Scenes do not match ordered Part Scene records.")

    reconstructed = normalize_text(" ".join(flattened_text))
    corrected = normalize_text(story_text_from_parts(parts))

    if reconstructed != corrected:
        fail("Flattened Part narration does not reconstruct complete story.")

    return results, boundary_errors


def story_text_from_parts(parts: list[dict]) -> str:
    return " ".join(part_text(part) for part in parts)


def main() -> int:
    input_data = verify_input()

    config = input_data["config"]
    mode = input_data["mode"]
    minimum_parts = input_data["minimum_parts"]
    maximum_parts = input_data["maximum_parts"]
    scenes_min = input_data["scenes_min"]
    scenes_max = input_data["scenes_max"]

    story = read_json(STORY_FILE)

    if not isinstance(story, dict):
        fail("story.json must contain a JSON object.")

    parts = story.get("parts")

    if not isinstance(parts, list) or not parts:
        fail("story.json must contain a non-empty Parts list.")

    parts_count = len(parts)

    selection_record = verify_selection(
        config,
        story,
        parts_count,
        minimum_parts,
        maximum_parts,
    )

    parts, all_scenes, actual_counts = verify_story_header(
        story,
        mode,
        parts_count,
        scenes_min,
        scenes_max,
    )

    if sum(actual_counts) > MAX_TOTAL_SCENES:
        fail("Total Main Scene count exceeds safety limit.")

    plan = read_json(PLAN_FILE)

    if not isinstance(plan, dict):
        fail("story_plan.json must contain a JSON object.")

    plan_parts = verify_plan(
        plan,
        story,
        mode,
        parts_count,
        scenes_min,
        scenes_max,
        actual_counts,
    )

    part_results, boundary_errors = verify_story_parts(
        parts,
        all_scenes,
        plan_parts,
        actual_counts,
        mode,
    )

    ordered_parts = normalize_text(" ".join(part_text(p) for p in parts))
    corrected_story = normalize_text(story.get("corrected_complete_story"))

    if ordered_parts != corrected_story:
        fail("Ordered Parts do not reconstruct corrected_complete_story.")

    if normalize_text(story.get("story_text")) != ordered_parts:
        fail("story_text does not match ordered Part narration.")

    total_words = word_count(ordered_parts)

    if not 0 < total_words <= MAX_STORY_WORDS:
        fail("Story word count is outside supported limits.")

    if as_int(story.get("total_story_words")) != total_words:
        fail("total_story_words metadata mismatch.")

    policy = story.get("story_length_policy")

    if not isinstance(policy, dict):
        policy = read_json(POLICY_FILE, required=False)

    if not isinstance(policy, dict):
        fail("Story length policy metadata/file is missing.")

    if policy.get("policy_version") != POLICY_VERSION:
        fail("Story length policy version mismatch.")

    if normalize_format(policy.get("format")) != mode:
        fail("Story length policy FORMAT mismatch.")

    if as_int(policy.get("selected_parts")) != parts_count:
        fail("Story length policy selected_parts mismatch.")

    policy_counts = policy.get("actual_scene_counts")

    if policy_counts is not None and policy_counts != actual_counts:
        fail("Story length policy Scene counts mismatch.")

    boundary_valid = not boundary_errors
    valid = boundary_valid and all(
        row.get("passed") is True for row in part_results
    )

    report = {
        "report_version": REPORT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": mode,
        "validation_source": "output/story/story.json",
        "validation_mode": "read_only",
        "story_fingerprint": fingerprint(story),
        "story_plan_fingerprint": clean_text(plan.get("source_fingerprint")),
        "selection_record": selection_record,
        "policy": policy,
        "summary": {
            "configured_parts_min": minimum_parts,
            "configured_parts_max": maximum_parts,
            "selected_parts": parts_count,
            "parts_expected": parts_count,
            "parts_found": len(parts),
            "configured_scenes_min": scenes_min,
            "configured_scenes_max": scenes_max,
            "actual_scene_counts": actual_counts,
            "scenes_expected": sum(actual_counts),
            "scenes_found": len(all_scenes),
            "total_words": total_words,
            "parts_needing_revision": sum(
                1 for row in part_results if row.get("passed") is not True
            ),
            "structure_errors": boundary_errors,
            "valid": valid,
            "passed": valid,
            "final_validation_passed": valid,
            "api_calls_made": 0,
            "story_text_modified": False,
            "natural_part_boundaries_valid": boundary_valid,
            "full_mode_single_part_valid": (
                mode != "full" or parts_count == 1
            ),
            "variable_scene_counts_valid": all(
                scenes_min <= count <= scenes_max
                for count in actual_counts
            ),
        },
        "part_numbering": list(range(1, parts_count + 1)),
        "part_results": part_results,
    }

    write_json(REPORT_FILE, report)

    log("=" * 64)
    log("STORY LENGTH AND SCENE VALIDATION")
    log("FORMAT:", mode)
    log("PARTS RANGE:", f"{minimum_parts}-{maximum_parts}")
    log("ACTUAL PARTS:", parts_count)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", actual_counts)
    log("TOTAL MAIN SCENES:", len(all_scenes))
    log("TOTAL STORY WORDS:", total_words)

    if mode == "short":
        log(
            "SHORT WORD LIMIT:",
            f"{SHORT_MIN_WORDS_PER_PART}-{SHORT_HARD_MAX_WORDS_PER_PART}",
        )
        log("PREFERRED MAXIMUM:", SHORT_PREFERRED_MAX_WORDS_PER_PART)
        log("FINAL MERGED MAXIMUM:", SHORT_FINAL_MERGED_MAX_WORDS)
    else:
        log("FULL MODE: one complete Part.")

    log("NATURAL PART BOUNDARIES:", boundary_valid)
    log("VALID:", valid)

    for row in part_results:
        log(
            "PART:",
            row["part_number"],
            "WORDS:",
            row["word_count"],
            "SCENES:",
            row["scene_count"],
            "FINAL MERGED:",
            row.get("final_part_merged", False),
            "PASSED:",
            row["passed"],
        )

    for error in boundary_errors:
        log("STRUCTURE ERROR:", error)

    log("API CALLS: 0")
    log("STORY MODIFIED: false")
    log("REPORT:", REPORT_FILE.relative_to(ROOT))
    log("=" * 64)

    if not valid:
        log("STORY LENGTH VALIDATION: FAILED")
        return 1

    log("STORY LENGTH VALIDATION: SUCCESS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("STORY LENGTH VALIDATION: FAILED")
        log(type(exc).__name__ + ":", str(exc))
        raise SystemExit(1)