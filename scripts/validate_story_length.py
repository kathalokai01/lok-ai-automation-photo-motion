#!/usr/bin/env python3
"""Read-only validation of SHORT/FULL story length and variable Scene ranges."""

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
    normalize_format as normalize_policy_format,
    sha256_text,
    verify_revision_evidence,
    word_count,
)

STORY_DIR = ROOT / "output" / "story"
STORY_FILE = STORY_DIR / "story.json"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
REPORT_FILE = STORY_DIR / "story_length_validation.json"

REPORT_VERSION = "story-length-validation-v11-variable-scenes-final-merge"

MAX_PARTS = 1000
MAX_SCENES_PER_PART = 500
MAX_TOTAL_SCENES = 1000
MAX_STORY_WORDS = 10_000_000
MAX_TITLE_LENGTH = 180

BOUNDARY_ENDINGS = (
    "।", "॥", ".", "!", "?", "…", "...",
    ",", "，", ";", "؛", ":", "—", "–",
)
CLOSING_MARKS = "\"'”’»)]}）】」』"


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
            fail(f"Required file is missing or empty: {path.relative_to(ROOT)}")
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


def extract_scene_text(scene: dict) -> str:
    for field in ("narration", "narration_text", "voiceover", "text"):
        value = scene.get(field)
        if isinstance(value, str) and normalize_text(value):
            return normalize_text(value)
    return ""


def extract_part_text(part: dict) -> str:
    for field in (
        "narration", "narration_text", "voiceover",
        "story", "text", "content",
    ):
        value = part.get(field)
        if isinstance(value, str) and normalize_text(value):
            return normalize_text(value)
    return ""


def verify_input() -> dict:
    input_file = ROOT / "Input" / "topic.txt"
    if not input_file.is_file():
        fail("Input/topic.txt is missing.")

    config = load_and_validate(str(input_file))
    mode = normalize_policy_format(get_format(config))
    minimum_parts, maximum_parts = get_parts_range(config)
    scenes_min, scenes_max = get_scenes_range(config)

    if not 1 <= minimum_parts <= maximum_parts <= MAX_PARTS:
        fail("Configured PARTS range is outside supported limits.")

    if not 1 <= scenes_min <= scenes_max <= MAX_SCENES_PER_PART:
        fail("Configured SCENES range is outside supported limits.")

    if maximum_parts * scenes_max > MAX_TOTAL_SCENES:
        fail(
            "Configured maximum PARTS multiplied by maximum SCENES "
            "exceeds the total Main Scene safety limit."
        )

    if mode == "full" and (minimum_parts, maximum_parts) != (1, 1):
        fail("FULL format must contain exactly one complete Part.")

    return {
        "config": config,
        "format": mode,
        "minimum_parts": minimum_parts,
        "maximum_parts": maximum_parts,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
    }


def verify_part_selection(
    config: dict,
    story: dict,
    actual_parts: int,
    minimum: int,
    maximum: int,
) -> dict | None:
    errors = []

    if not minimum <= actual_parts <= maximum:
        errors.append(
            f"Actual PARTS={actual_parts} is outside configured "
            f"range {minimum}-{maximum}."
        )

    if normalize_policy_format(story.get("format")) == "full":
        if actual_parts != 1:
            errors.append("FULL format must contain exactly one Part.")

    for field in (
        "total_parts", "parts_count",
        "selected_parts", "selected_parts_count",
    ):
        declared = story.get(field)
        if declared is not None and as_int(declared) != actual_parts:
            errors.append(
                f"Story metadata {field}={declared!r} does not match "
                f"actual Part count {actual_parts}."
            )

    record = get_selected_parts_record(config)

    if minimum != maximum and record is None:
        errors.append(
            "PARTS is configured as a range but no valid persisted "
            "selection record matches the current Input/topic.txt."
        )

    if record is not None:
        if as_int(record.get("selected_parts")) != actual_parts:
            errors.append("Persisted PARTS selection does not match story.json.")
        if as_int(record.get("minimum_parts")) != minimum:
            errors.append("Persisted PARTS minimum does not match Input.")
        if as_int(record.get("maximum_parts")) != maximum:
            errors.append("Persisted PARTS maximum does not match Input.")

    try:
        configured_selection = get_selected_parts(config, required=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"Invalid persisted PARTS selection: {exc}")
        configured_selection = None

    if configured_selection is not None and configured_selection != actual_parts:
        errors.append(
            "Input's selected PARTS count does not match story.json."
        )

    if errors:
        fail(" ".join(errors))

    return record


def verify_story_header(
    story: dict,
    mode: str,
    selected_parts: int,
    scenes_min: int,
    scenes_max: int,
) -> tuple[list[dict], list[dict], list[int]]:
    if story.get("status") != "completed":
        fail("story.json status must be 'completed'.")

    if normalize_policy_format(story.get("format")) != mode:
        fail("Story FORMAT does not match Input/topic.txt.")

    title = clean_text(story.get("title"))
    if not title or len(title) > MAX_TITLE_LENGTH:
        fail("Story title is empty or exceeds 180 characters.")

    if clean_text(story.get("final_title")) != title:
        fail("Story final_title does not match story title.")

    if clean_text(story.get("scene_order")) != "part_then_scene":
        fail("Story scene_order must be part_then_scene.")

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
            fail("Supplied story has no completed proofreading record.")

    if as_int(story.get("total_parts")) != selected_parts:
        fail("Story total_parts does not match actual Part count.")

    if as_int(story.get("parts_count")) != selected_parts:
        fail("Story parts_count does not match actual Part count.")

    if as_int(story.get("selected_parts")) != selected_parts:
        fail("Story selected_parts does not match actual Part count.")

    if as_int(story.get("selected_parts_count")) != selected_parts:
        fail("Story selected_parts_count does not match actual Part count.")

    for field, expected in (
        ("scenes_min", scenes_min),
        ("scenes_max", scenes_max),
    ):
        if as_int(story.get(field)) != expected:
            fail(f"Story {field} does not match Input/topic.txt.")

    if story.get("scenes_range") != [scenes_min, scenes_max]:
        fail("Story scenes_range does not match Input/topic.txt.")

    # scenes_per_part remains the minimum only for legacy consumers.
    if as_int(story.get("scenes_per_part")) != scenes_min:
        fail("Story scenes_per_part compatibility value is invalid.")

    source_text = story.get("source_text", "")
    if not isinstance(source_text, str):
        fail("Story source_text must be text.")

    if as_int(story.get("source_word_count"), 0) != word_count(source_text):
        fail("Story source_word_count metadata mismatch.")

    source_fingerprint = clean_text(story.get("source_fingerprint"))
    if not re.fullmatch(r"[a-fA-F0-9]{64}", source_fingerprint):
        fail("Story source_fingerprint is missing or invalid.")

    corrected = story.get("corrected_complete_story")
    if not isinstance(corrected, str) or not normalize_text(corrected):
        fail("Corrected complete story is missing.")

    corrected_words = word_count(corrected)
    if as_int(story.get("corrected_complete_story_word_count")) != corrected_words:
        fail("Corrected complete story word-count metadata mismatch.")

    if normalize_text(story.get("story_text")) != normalize_text(corrected):
        fail("story_text and corrected_complete_story differ.")

    if as_int(story.get("total_story_words")) != corrected_words:
        fail("Story total_story_words metadata mismatch.")

    parts = story.get("parts")
    scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != selected_parts:
        fail("Story Part list does not match selected PARTS.")

    if not isinstance(scenes, list):
        fail("Story top-level Main Scene list is missing.")

    if len(scenes) > MAX_TOTAL_SCENES:
        fail("Story exceeds the total Main Scene safety limit.")

    actual_counts = story.get("actual_scene_counts")
    if not isinstance(actual_counts, list) or len(actual_counts) != selected_parts:
        fail("Story actual_scene_counts is missing or invalid.")

    actual_counts = [
        as_int(value)
        for value in actual_counts
    ]

    if any(not scenes_min <= count <= scenes_max for count in actual_counts):
        fail("At least one Part has a Scene count outside configured range.")

    if sum(actual_counts) != len(scenes):
        fail("actual_scene_counts does not equal top-level Scene count.")

    if as_int(story.get("total_scenes")) != len(scenes):
        fail("Story total_scenes metadata mismatch.")

    if as_int(story.get("total_main_scenes")) != len(scenes):
        fail("Story total_main_scenes metadata mismatch.")

    return parts, scenes, actual_counts


def verify_revision(part: dict, narration: str, part_number: int) -> dict:
    evidence = part.get("revision_evidence")
    if not isinstance(evidence, dict):
        fail(
            f"SHORT Part {part_number} exceeds 150 words but has no "
            "structured revision evidence."
        )

    if verify_revision_evidence(evidence, narration) is not True:
        fail(
            f"SHORT Part {part_number} revision evidence is invalid: "
            "the returned text hash must differ from the original hash "
            "and match the final narration."
        )

    return evidence


def verify_short_word_count(
    part: dict,
    narration: str,
    part_number: int,
    selected_parts: int,
) -> dict:
    amount = word_count(narration)
    is_final = part_number == selected_parts
    merged_final = is_final and part.get("final_part_merged") is True

    if amount < SHORT_MIN_WORDS_PER_PART:
        fail(
            f"SHORT Part {part_number} has {amount} words. "
            "The final-Part retry/rebalance and merge fallback must be "
            "completed before this validation stage."
        )

    maximum = (
        SHORT_FINAL_MERGED_MAX_WORDS
        if merged_final
        else SHORT_HARD_MAX_WORDS_PER_PART
    )

    if amount > maximum:
        fail(
            f"SHORT Part {part_number} has {amount} words; "
            f"maximum allowed is {maximum}."
        )

    evidence = None
    if amount > SHORT_PREFERRED_MAX_WORDS_PER_PART and not merged_final:
        evidence = verify_revision(part, narration, part_number)

    if merged_final and amount > SHORT_FINAL_MERGED_MAX_WORDS:
        fail("Final merged SHORT Part exceeds 239 words.")

    return {
        "part_number": part_number,
        "word_count": amount,
        "minimum_words": SHORT_MIN_WORDS_PER_PART,
        "preferred_maximum_words": SHORT_PREFERRED_MAX_WORDS_PER_PART,
        "hard_maximum_words": maximum,
        "final_part_merged": merged_final,
        "revision_required": (
            amount > SHORT_PREFERRED_MAX_WORDS_PER_PART and not merged_final
        ),
        "revision_evidence_verified": evidence is not None,
        "passed": True,
    }

def verify_plan_header(
    plan: dict,
    story: dict,
    mode: str,
    selected_parts: int,
    scenes_min: int,
    scenes_max: int,
    actual_counts: list[int],
) -> list[dict]:
    if clean_text(plan.get("status")).lower() != "planned":
        fail("story_plan.json status must be planned.")

    if normalize_policy_format(plan.get("format")) != mode:
        fail("Story Plan FORMAT does not match Input.")

    if as_int(plan.get("parts_count")) != selected_parts:
        fail("Story Plan parts_count does not match story.json.")

    if as_int(plan.get("scenes_min")) != scenes_min:
        fail("Story Plan scenes_min does not match Input.")

    if as_int(plan.get("scenes_max")) != scenes_max:
        fail("Story Plan scenes_max does not match Input.")

    plan_counts = plan.get("actual_scene_counts")
    if not isinstance(plan_counts, list):
        fail("Story Plan actual_scene_counts is missing.")

    plan_counts = [as_int(value) for value in plan_counts]

    if plan_counts != actual_counts:
        fail("Story Plan Scene counts do not match story.json.")

    if len(plan_counts) != selected_parts:
        fail("Story Plan actual_scene_counts has an incorrect length.")

    if any(not scenes_min <= count <= scenes_max for count in plan_counts):
        fail("Story Plan contains a Part outside the configured Scene range.")

    total_main = sum(plan_counts)
    if total_main > MAX_TOTAL_SCENES:
        fail("Story Plan exceeds the total Main Scene safety limit.")

    for field in ("total_scenes", "total_main_scenes"):
        if as_int(plan.get(field)) != total_main:
            fail(f"Story Plan {field} does not match actual Scene counts.")

    if as_int(plan.get("total_hook_scenes")) != selected_parts:
        fail("Story Plan must contain one Hook per Part.")

    if as_int(plan.get("total_suspense_scenes")) != max(0, selected_parts - 1):
        fail("Story Plan Suspense count is invalid.")

    if as_int(plan.get("total_resolution_scenes")) != 1:
        fail("Story Plan must contain exactly one final Resolution.")

    if clean_text(plan.get("scene_order")) != (
        "hook_then_main_then_suspense_or_resolution"
    ):
        fail("Story Plan scene_order is invalid.")

    if clean_text(plan.get("main_scene_order")) != "part_then_scene":
        fail("Story Plan main_scene_order is invalid.")

    plan_fingerprint = clean_text(plan.get("source_fingerprint"))
    if not re.fullmatch(r"[a-fA-F0-9]{64}", plan_fingerprint):
        fail("Story Plan source_fingerprint is missing or invalid.")

    if clean_text(story.get("story_plan_fingerprint")) != plan_fingerprint:
        fail("Story and Story Plan fingerprints do not match.")

    source = plan.get("source")
    if not isinstance(source, dict):
        fail("Story Plan source metadata is missing.")

    if source.get("api_calls") != 0:
        fail("Final Story Plan must not make external API calls.")

    if source.get("planning_mode") != "final_corrected_story":
        fail("Story Plan was not built from the final corrected story.")

    if source.get("story_text_provided") is not True:
        fail("Story Plan does not confirm complete story text as input.")

    if as_int(source.get("story_word_count")) <= 0:
        fail("Story Plan source story_word_count is invalid.")

    plan_parts = plan.get("parts")
    if not isinstance(plan_parts, list) or len(plan_parts) != selected_parts:
        fail("Story Plan Part list does not match selected PARTS.")

    return plan_parts


def verify_role_record(
    record: Any,
    part_number: int,
    role: str,
    is_final: bool,
    expected_anchor: str,
) -> dict:
    if not isinstance(record, dict):
        fail(f"Part {part_number} is missing its separate {role} record.")

    expected_id = f"part_{part_number:02d}_{role}"

    expected_values = {
        "part": part_number,
        "role": role,
        "scene_id": expected_id,
        "scene": None,
        "global_scene_number": None,
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
        "is_final_part": is_final,
    }

    for field, expected in expected_values.items():
        if record.get(field) != expected:
            fail(f"{expected_id} has invalid {field} metadata.")

    if normalize_text(record.get("story_anchor")) != normalize_text(expected_anchor):
        fail(f"{expected_id} is not anchored to the correct Main Scene.")

    for field in (
        "purpose",
        "instruction",
        "continuity_rule",
        "invention_policy",
        "visual_policy",
    ):
        if not clean_text(record.get(field)):
            fail(f"{expected_id} is missing required field {field}.")

    return record


def verify_plan_parts(
    plan_parts: list[dict],
    actual_counts: list[int],
    selected_parts: int,
) -> list[dict]:
    verified = []
    global_scene = 0

    for part_number, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            fail(f"Story Plan Part {part_number} must be an object.")

        if as_int(part.get("part")) != part_number:
            fail(f"Story Plan Part {part_number} numbering mismatch.")

        expected_part_id = f"part_{part_number:02d}"
        if clean_text(part.get("part_id")) != expected_part_id:
            fail(f"Story Plan Part ID mismatch: {expected_part_id}.")

        scenes = part.get("scenes")
        expected_count = actual_counts[part_number - 1]

        if not isinstance(scenes, list) or len(scenes) != expected_count:
            fail(f"Story Plan Scene count mismatch in Part {part_number}.")

        if as_int(part.get("main_scene_count")) != expected_count:
            fail(f"Story Plan main_scene_count mismatch in Part {part_number}.")

        if part.get("hook_required") is not True:
            fail(f"Part {part_number} must have a separate Hook.")

        if part.get("suspense_required") is not (part_number < selected_parts):
            fail(f"Part {part_number} Suspense requirement is invalid.")

        if part.get("final_resolution") is not (part_number == selected_parts):
            fail(f"Part {part_number} Resolution requirement is invalid.")

        if not clean_text(part.get("story_anchor")):
            fail(f"Story Plan Part {part_number} has no story anchor.")

        scene_anchors = []

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                fail(f"Invalid Story Plan Scene in Part {part_number}.")

            global_scene += 1
            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

            expected_values = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_scene,
                "scene_id": scene_id,
                "role": "main",
            }

            for field, expected in expected_values.items():
                if scene.get(field) != expected:
                    fail(f"Story Plan {scene_id} has invalid {field}.")

            narration = normalize_text(scene.get("narration"))
            anchor = normalize_text(scene.get("story_anchor"))

            if not narration or narration != anchor:
                fail(f"Story Plan {scene_id} narration/anchor mismatch.")

            for field in (
                "beat",
                "purpose",
                "continuity_rule",
                "invention_policy",
                "visual_policy",
            ):
                if not clean_text(scene.get(field)):
                    fail(f"Story Plan {scene_id} is missing {field}.")

            scene_anchors.append(anchor)

        combined_anchor = normalize_text(" ".join(scene_anchors))
        if combined_anchor != normalize_text(part.get("story_anchor")):
            fail(
                f"Story Plan Part {part_number} anchor differs from "
                "its ordered Scene anchors."
            )

        is_final = part_number == selected_parts

        hook = verify_role_record(
            part.get("hook_scene"),
            part_number,
            "hook",
            is_final,
            scene_anchors[0],
        )

        suspense = None
        if not is_final:
            suspense = verify_role_record(
                part.get("suspense_scene"),
                part_number,
                "suspense",
                False,
                scene_anchors[-1],
            )
        elif part.get("suspense_scene") is not None:
            fail("Final Part must not contain a Suspense record.")

        resolution = None
        if is_final:
            resolution = verify_role_record(
                part.get("resolution_scene"),
                part_number,
                "resolution",
                True,
                scene_anchors[-1],
            )
        elif part.get("resolution_scene") is not None:
            fail("A non-final Part must not contain a Resolution record.")

        expected_extras = [
            row for row in (hook, suspense, resolution)
            if isinstance(row, dict)
        ]

        if part.get("extra_scenes") != expected_extras:
            fail(f"Story Plan Part {part_number} extra_scenes mismatch.")

        verified.append({
            "part_number": part_number,
            "part_id": expected_part_id,
            "main_scene_count": expected_count,
            "main_scene_ids": [
                f"part_{part_number:02d}_scene_{number:02d}"
                for number in range(1, expected_count + 1)
            ],
            "hook_scene_id": hook["scene_id"],
            "suspense_scene_id": suspense["scene_id"] if suspense else None,
            "resolution_scene_id": resolution["scene_id"] if resolution else None,
        })

    return verified


def verify_story_parts(
    parts: list[dict],
    all_scenes: list[dict],
    plan_parts: list[dict],
    actual_counts: list[int],
    mode: str,
    selected_parts: int,
) -> tuple[list[dict], list[str]]:
    results = []
    errors = []
    flattened_scenes = []
    flattened_narration = []
    global_scene = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story Part {part_number} must be an object.")

        expected_part_id = f"part_{part_number:02d}"

        if as_int(part.get("part")) != part_number:
            fail(f"Story Part {part_number} numbering mismatch.")

        if clean_text(part.get("part_id")) != expected_part_id:
            fail(f"Story Part ID mismatch: {expected_part_id}.")

        narration = normalize_text(part.get("narration"))
        if not narration:
            fail(f"Story Part {part_number} narration is empty.")

        amount = word_count(narration)

        if as_int(part.get("word_count")) != amount:
            fail(f"Story Part {part_number} word_count metadata mismatch.")

        if normalize_text(part.get("story_anchor")) != narration:
            fail(f"Story Part {part_number} story_anchor mismatch.")

        if mode == "short":
            word_result = verify_short_word_count(
                part,
                narration,
                part_number,
                selected_parts,
            )
        else:
            if amount <= 0:
                fail("FULL story narration is empty.")

            word_result = {
                "part_number": part_number,
                "word_count": amount,
                "minimum_words": None,
                "preferred_maximum_words": None,
                "hard_maximum_words": None,
                "final_part_merged": False,
                "revision_required": False,
                "revision_evidence_verified": False,
                "passed": True,
                "unrestricted_full_story": True,
            }

        scenes = part.get("scenes")
        planned_scenes = plan_parts[part_number - 1].get("scenes")
        expected_count = actual_counts[part_number - 1]

        if not isinstance(scenes, list) or len(scenes) != expected_count:
            fail(f"Story Part {part_number} actual Scene count mismatch.")

        if not isinstance(planned_scenes, list) or len(planned_scenes) != expected_count:
            fail(f"Story Plan Scene count mismatch in Part {part_number}.")

        if as_int(part.get("main_scene_count")) != expected_count:
            fail(f"Part {part_number} main_scene_count metadata mismatch.")

        if as_int(part.get("actual_scene_count")) != expected_count:
            fail(f"Part {part_number} actual_scene_count metadata mismatch.")

        scene_texts = []
        scene_ids = []

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                fail(f"Part {part_number} contains an invalid Scene record.")

            global_scene += 1
            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

            expected_values = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_scene,
                "scene_id": scene_id,
                "role": "main",
            }

            for field, expected in expected_values.items():
                if scene.get(field) != expected:
                    fail(f"Story Scene {scene_id} has invalid {field}.")

            narration_text = extract_scene_text(scene)
            if not narration_text:
                fail(f"Story Scene {scene_id} has no narration.")

            planned = planned_scenes[scene_number - 1]
            if not isinstance(planned, dict):
                fail(f"Planned Scene {scene_id} is invalid.")

            if clean_text(planned.get("scene_id")) != scene_id:
                fail(f"Planned Scene ID mismatch: {scene_id}.")

            if normalize_text(planned.get("narration")) != narration_text:
                fail(f"Story/Plan narration mismatch for {scene_id}.")

            if not clean_text(scene.get("visual")):
                fail(f"Story Scene {scene_id} has no visual prompt.")

            if not clean_text(scene.get("visual_prompt")):
                fail(f"Story Scene {scene_id} has no visual_prompt.")

            if normalize_text(scene.get("visual")) != normalize_text(
                scene.get("visual_prompt")
            ):
                fail(f"Story Scene {scene_id} visual fields disagree.")

            scene_texts.append(narration_text)
            scene_ids.append(scene_id)
            flattened_scenes.append(scene)
            flattened_narration.append(narration_text)

        if normalize_text(" ".join(scene_texts)) != narration:
            fail(
                f"Ordered Scene narration does not reconstruct "
                f"Part {part_number} narration."
            )

        is_final = part_number == selected_parts

        hook = verify_role_record(
            part.get("hook_scene"),
            part_number,
            "hook",
            is_final,
            scene_texts[0],
        )

        suspense = None
        if not is_final:
            suspense = verify_role_record(
                part.get("suspense_scene"),
                part_number,
                "suspense",
                False,
                scene_texts[-1],
            )
        elif part.get("suspense_scene") is not None:
            fail("Final Part must not contain a Suspense record.")

        resolution = None
        if is_final:
            resolution = verify_role_record(
                part.get("resolution_scene"),
                part_number,
                "resolution",
                True,
                scene_texts[-1],
            )
        elif part.get("resolution_scene") is not None:
            fail("Non-final Part must not contain a Resolution record.")

        expected_extras = [
            row for row in (hook, suspense, resolution)
            if isinstance(row, dict)
        ]

        if part.get("extra_scenes") != expected_extras:
            fail(f"Part {part_number} extra_scenes metadata mismatch.")

        results.append({
            **word_result,
            "part_id": expected_part_id,
            "scene_count": expected_count,
            "scene_ids": scene_ids,
            "narration_sha256": sha256_text(narration),
            "natural_boundary_valid": narration.rstrip()[-1] in BOUNDARY_ENDINGS,
        })

        if narration.rstrip()[-1] not in BOUNDARY_ENDINGS:
            errors.append(
                f"Part {part_number} ends without a complete sentence "
                "or natural clause boundary."
            )

    if flattened_scenes != all_scenes:
        fail(
            "Top-level Main Scenes do not match ordered Part Scene records."
        )

    complete_story = normalize_text(
        "\n\n".join(extract_part_text(part) for part in parts)
    )

    story_text = normalize_text(
        "\n\n".join(flattened_narration)
    )

    if complete_story != story_text:
        fail("Flattened Scene narration does not reconstruct complete story.")

    return results, errors


def source_comparison(story: dict, generated_text: str) -> dict:
    source_text = normalize_text(story.get("source_text"))
    generated_text = normalize_text(generated_text)

    source_words = word_count(source_text)
    generated_words = word_count(generated_text)

    return {
        "source_word_count": source_words,
        "generated_story_word_count": generated_words,
        "generated_to_source_word_ratio": (
            round(generated_words / source_words, 4)
            if source_words else None
        ),
        "source_text_modified_by_validator": False,
        "semantic_preservation_claimed": False,
        "limitation": (
            "Word counts and text fingerprints do not prove semantic "
            "preservation. The validator does not claim that they establish "
            "complete event coverage or factual accuracy."
        ),
    }


def main() -> int:
    input_data = verify_input()
    config = input_data["config"]
    mode = input_data["format"]
    minimum_parts = input_data["minimum_parts"]
    maximum_parts = input_data["maximum_parts"]
    scenes_min = input_data["scenes_min"]
    scenes_max = input_data["scenes_max"]

    story = read_json(STORY_FILE)
    if not isinstance(story, dict):
        fail("story.json must contain a JSON object.")

    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        fail("story.json must contain a non-empty parts list.")

    selected_parts = len(parts)

    selection_record = verify_part_selection(
        config,
        story,
        selected_parts,
        minimum_parts,
        maximum_parts,
    )

    parts, all_scenes, actual_counts = verify_story_header(
        story,
        mode,
        selected_parts,
        scenes_min,
        scenes_max,
    )

    if sum(actual_counts) > MAX_TOTAL_SCENES:
        fail("Actual Main Scene total exceeds the supported safety limit.")

    plan = read_json(PLAN_FILE)
    if not isinstance(plan, dict):
        fail("story_plan.json must contain a JSON object.")

    plan_parts = verify_plan_header(
        plan,
        story,
        mode,
        selected_parts,
        scenes_min,
        scenes_max,
        actual_counts,
    )

    verified_plan_parts = verify_plan_parts(
        plan_parts,
        actual_counts,
        selected_parts,
    )

    part_results, boundary_errors = verify_story_parts(
        parts,
        all_scenes,
        plan_parts,
        actual_counts,
        mode,
        selected_parts,
    )

    corrected_story = normalize_text(story.get("corrected_complete_story"))
    ordered_parts = normalize_text(
        "\n\n".join(extract_part_text(part) for part in parts)
    )

    if corrected_story != ordered_parts:
        fail(
            "Ordered Part narration does not exactly reconstruct "
            "corrected_complete_story."
        )

    if normalize_text(story.get("story_text")) != ordered_parts:
        fail("story_text does not match ordered Part narration.")

    total_words = word_count(ordered_parts)

    if total_words <= 0:
        fail("Story contains no narration words.")

    if total_words > MAX_STORY_WORDS:
        fail("Story exceeds the maximum supported word count.")

    if as_int(story.get("total_story_words")) != total_words:
        fail("total_story_words does not match actual narration.")

    actual_scenes = sum(actual_counts)

    if actual_scenes != len(all_scenes):
        fail("Actual Scene count does not match top-level Scene list.")

    policy = story.get("story_length_policy")
    if not isinstance(policy, dict):
        policy = read_json(POLICY_FILE, required=False)

    if not isinstance(policy, dict):
        fail("Story length policy metadata/file is missing.")

    if policy.get("policy_version") != POLICY_VERSION:
        fail("Story length policy version is incompatible.")

    if normalize_policy_format(policy.get("format")) != mode:
        fail("Story length policy FORMAT does not match Input.")

    if as_int(policy.get("selected_parts")) != selected_parts:
        fail("Story length policy selected_parts mismatch.")

    policy_counts = policy.get("actual_scene_counts")
    if policy_counts is not None and policy_counts != actual_counts:
        fail("Story length policy Scene counts do not match story.json.")

    source_report = source_comparison(story, ordered_parts)

    all_errors = list(boundary_errors)
    valid = not all_errors

    report = {
        "report_version": REPORT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": mode,
        "validation_source": "output/story/story.json",
        "validation_mode": "read_only",
        "story_fingerprint": fingerprint(story),
        "story_plan_fingerprint": clean_text(
            plan.get("source_fingerprint")
        ),
        "selection_record": selection_record,
        "policy": policy,
        "source_comparison": source_report,
        "summary": {
            "configured_parts_min": minimum_parts,
            "configured_parts_max": maximum_parts,
            "selected_parts": selected_parts,
            "parts_expected": selected_parts,
            "parts_found": len(parts),
            "configured_scenes_min": scenes_min,
            "configured_scenes_max": scenes_max,
            "actual_scene_counts": actual_counts,
            "scenes_expected": actual_scenes,
            "scenes_found": len(all_scenes),
            "total_words": total_words,
            "parts_needing_revision": 0,
            "structure_errors": all_errors,
            "valid": valid,
            "passed": valid,
            "final_validation_passed": valid,
            "api_calls_made": 0,
            "story_text_modified": False,
            "natural_part_boundaries_valid": not boundary_errors,
            "full_mode_single_part_valid": (
                mode != "full" or selected_parts == 1
            ),
            "variable_scene_counts_valid": all(
                scenes_min <= count <= scenes_max
                for count in actual_counts
            ),
        },
        "part_numbering": list(range(1, selected_parts + 1)),
        "plan_results": verified_plan_parts,
        "part_results": part_results,
    }

    write_json(REPORT_FILE, report)

    log("=" * 64)
    log("STORY LENGTH AND SCENE VALIDATION")
    log("FORMAT:", mode)
    log("CONFIGURED PARTS RANGE:", f"{minimum_parts}-{maximum_parts}")
    log("ACTUAL PARTS:", selected_parts)
    log("CONFIGURED SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", actual_counts)
    log("TOTAL MAIN SCENES:", actual_scenes)
    log("TOTAL STORY WORDS:", total_words)

    if mode == "short":
        log(
            "SHORT NORMAL PART WORDS:",
            f"{SHORT_MIN_WORDS_PER_PART}-{SHORT_HARD_MAX_WORDS_PER_PART}",
        )
        log(
            "SHORT PREFERRED MAXIMUM:",
            SHORT_PREFERRED_MAX_WORDS_PER_PART,
        )
        log(
            "FINAL MERGED PART MAXIMUM:",
            SHORT_FINAL_MERGED_MAX_WORDS,
        )
    else:
        log("FULL MODE: one complete Part; no word-count limit.")

    log("NATURAL PART BOUNDARIES VALID:", not boundary_errors)
    log("VALID:", valid)

    for error in all_errors:
        log("ERROR:", error)

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