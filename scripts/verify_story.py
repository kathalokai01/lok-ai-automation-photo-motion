#!/usr/bin/env python3
"""Read-only validation of the final story and its matching scene plan."""

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
    load_and_validate,
    get_format,
    get_parts_range,
    get_scenes_range,
    get_topic,
)
from generate_story import get_complete_source
from generate_story_plan import (
    VERSION as PLAN_VERSION,
    MAPPING_VERSION,
)
from story_length_policy import (
    SHORT_MIN_WORDS_PER_PART,
    SHORT_PREFERRED_MAX_WORDS_PER_PART,
    SHORT_HARD_MAX_WORDS_PER_PART,
    SHORT_FINAL_MERGED_MAX_WORDS,
    verify_revision_evidence,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"
REPORT_FILE = ROOT / "output" / "story" / "story_verification.json"

VERSION = "story-verifier-v19-locked-text-fingerprints"

SHORT_MIN_WORDS = SHORT_MIN_WORDS_PER_PART
SHORT_PREFERRED_MAX_WORDS = SHORT_PREFERRED_MAX_WORDS_PER_PART
SHORT_HARD_MAX_WORDS = SHORT_HARD_MAX_WORDS_PER_PART
SHORT_MERGED_MAX_WORDS = SHORT_FINAL_MERGED_MAX_WORDS

MAX_TITLE_LENGTH = 180
MAX_PARTS = 1000
MAX_SCENES_PER_PART = 500
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


def fail(message: str) -> None:
    raise RuntimeError(message)


def clean(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
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


def positive_int(
    value: Any,
    field: str,
    allow_zero: bool = False,
) -> int:
    if isinstance(value, bool):
        fail(f"{field} must be an integer.")

    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(f"{field} must be an integer.")

    if result < (0 if allow_zero else 1):
        fail(f"{field} is outside its permitted range.")

    return result


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


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"Duplicate JSON key: {key!r}.")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str):
    fail(f"Non-finite JSON value is not permitted: {value!r}.")


def read_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size <= 0:
        fail(f"Required file is missing or empty: {path.relative_to(ROOT)}")

    try:
        data = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Cannot read {path.name}: {exc}")

    if not isinstance(data, dict):
        fail(f"{path.name} must contain a JSON object.")

    return data


def write_json(path: Path, data: dict) -> None:
    """Atomically write the verification report only."""
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


def verify_input() -> dict:
    if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size <= 0:
        fail("Input/topic.txt is missing or empty.")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()

    if mode not in {"short", "full"}:
        fail("FORMAT must be short or full.")

    try:
        minimum_parts, maximum_parts = get_parts_range(config)
        scenes_min, scenes_max = get_scenes_range(config)
    except (TypeError, ValueError) as exc:
        fail(f"Invalid PARTS or SCENES range: {exc}")

    if not 1 <= minimum_parts <= maximum_parts <= MAX_PARTS:
        fail("Configured PARTS range is outside supported limits.")

    if not 1 <= scenes_min <= scenes_max <= MAX_SCENES_PER_PART:
        fail("Configured SCENES range is outside supported limits.")

    if maximum_parts * scenes_max > MAX_TOTAL_SCENES:
        fail("Maximum configured PARTS × SCENES exceeds safety limit.")

    if mode == "full" and (minimum_parts, maximum_parts) != (1, 1):
        fail("FULL format must have exactly PARTS=1.")

    topic = clean(get_topic(config))
    explicit_title = clean(config.get("TITLE", ""))
    source_text = normalized_text(get_complete_source(config))

    if not topic and not explicit_title and not source_text:
        fail("Provide TOPIC, TITLE or STORY_TEXT.")

    if source_text and word_count(source_text) < 20:
        fail("STORY_TEXT contains fewer than 20 words.")

    return {
        "config": config,
        "mode": mode,
        "minimum_parts": minimum_parts,
        "maximum_parts": maximum_parts,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "topic": topic,
        "explicit_title": explicit_title,
        "source_text": source_text,
    }


def verify_selected_parts(
    story: dict,
    plan: dict,
    input_data: dict,
) -> int:
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
        fail("Story and Plan Part counts differ.")

    for field in (
        "total_parts",
        "parts_count",
        "selected_parts",
        "selected_parts_count",
    ):
        declared = story.get(field)
        if declared is not None:
            if positive_int(declared, f"story.json {field}") != selected:
                fail(f"Story {field} does not match actual Part count.")

    for field, expected in (
        ("configured_parts_min", minimum),
        ("configured_parts_max", maximum),
    ):
        declared = story.get(field)
        if declared is not None:
            if positive_int(declared, f"story.json {field}") != expected:
                fail(f"Story {field} does not match Input/topic.txt.")

    expected_specification = (
        f"{minimum}-{maximum}"
        if minimum != maximum
        else str(minimum)
    )

    specification = clean(story.get("configured_parts_specification"))
    if specification and specification != expected_specification:
        fail("Story configured_parts_specification does not match Input.")

    if input_data["mode"] == "full" and selected != 1:
        fail("FULL format must contain exactly one complete Part.")

    return selected


def verify_plan_header(
    plan: dict,
    input_data: dict,
    selected_parts: int,
) -> tuple[list[dict], list[int]]:
    mode = input_data["mode"]
    topic = input_data["topic"]
    scenes_min = input_data["scenes_min"]
    scenes_max = input_data["scenes_max"]

    if clean(plan.get("status")).lower() != "planned":
        fail("Story Plan status must be planned.")

    if clean(plan.get("format")).lower() != mode:
        fail("Story Plan FORMAT does not match Input.")

    if clean(plan.get("topic")) != topic:
        fail("Story Plan topic does not match Input.")

    if clean(plan.get("script_version")) != PLAN_VERSION:
        fail("Story Plan was generated by an incompatible planner version.")

    if positive_int(plan.get("mapping_version"), "Plan mapping_version") != MAPPING_VERSION:
        fail("Story Plan mapping_version is incompatible.")

    if positive_int(plan.get("parts_count"), "Plan parts_count") != selected_parts:
        fail("Story Plan parts_count mismatch.")

    if positive_int(plan.get("total_parts"), "Plan total_parts") != selected_parts:
        fail("Story Plan total_parts mismatch.")

    if positive_int(plan.get("scenes_min"), "Plan scenes_min") != scenes_min:
        fail("Story Plan minimum SCENES does not match Input.")

    if positive_int(plan.get("scenes_max"), "Plan scenes_max") != scenes_max:
        fail("Story Plan maximum SCENES does not match Input.")

    if plan.get("scenes_range") != [scenes_min, scenes_max]:
        fail("Story Plan scenes_range does not match Input.")
    if clean(plan.get("scene_order")) != "hook_then_main_then_suspense_or_resolution":
        fail("Story Plan scene_order is invalid.")

    if clean(plan.get("main_scene_order")) != "part_then_scene":
        fail("Story Plan main_scene_order is invalid.")

    actual_counts = plan.get("actual_scene_counts")
    if not isinstance(actual_counts, list) or len(actual_counts) != selected_parts:
        fail("Story Plan actual_scene_counts is missing or invalid.")

    actual_counts = [
        positive_int(value, f"Plan actual_scene_counts[{index}]")
        for index, value in enumerate(actual_counts, 1)
    ]

    if any(not scenes_min <= count <= scenes_max for count in actual_counts):
        fail("A planned Part has a Scene count outside configured range.")

    total_main = sum(actual_counts)
    if total_main > MAX_TOTAL_SCENES:
        fail("Story Plan exceeds the total Main Scene safety limit.")

    for field in ("total_main_scenes", "total_scenes"):
        if positive_int(plan.get(field), f"Plan {field}") != total_main:
            fail(f"Story Plan {field} does not match actual Scene counts.")

    expected_role_counts = {
        "total_hook_scenes": selected_parts,
        "total_suspense_scenes": max(0, selected_parts - 1),
        "total_resolution_scenes": 1,
        "total_extra_scenes": selected_parts * 2,
    }

    for field, expected in expected_role_counts.items():
        if positive_int(
            plan.get(field),
            f"Plan {field}",
            allow_zero=True,
        ) != expected:
            fail(f"Story Plan {field} mismatch.")

    payload = plan.get("source_payload")
    if not isinstance(payload, dict):
        fail("Story Plan source_payload is missing.")

    if payload.get("mapping_version") != MAPPING_VERSION:
        fail("Story Plan source mapping_version mismatch.")

    if payload.get("format") != mode:
        fail("Story Plan source FORMAT mismatch.")

    if clean(payload.get("topic")) != topic:
        fail("Story Plan source topic mismatch.")

    if payload.get("parts_count") != selected_parts:
        fail("Story Plan source PARTS mismatch.")

    if payload.get("scenes_min") != scenes_min:
        fail("Story Plan source SCENES minimum mismatch.")

    if payload.get("scenes_max") != scenes_max:
        fail("Story Plan source SCENES maximum mismatch.")

    if payload.get("actual_scene_counts") != actual_counts:
        fail("Story Plan source Scene counts mismatch.")

    expected_fingerprint = sha256_json(payload)
    if clean(plan.get("source_fingerprint")) != expected_fingerprint:
        fail("Story Plan source_fingerprint does not match source_payload.")

    source = plan.get("source")
    if not isinstance(source, dict):
        fail("Story Plan source metadata is missing.")

    if source.get("api_calls") != 0:
        fail("Story planning must not make external API calls.")

    if source.get("planning_mode") != "final_corrected_story":
        fail("Story Plan was not built from the final corrected story.")

    if source.get("story_text_provided") is not True:
        fail("Story Plan was not built from complete story text.")

    plan_parts = plan.get("parts")
    if not isinstance(plan_parts, list) or len(plan_parts) != selected_parts:
        fail("Story Plan Part list does not match selected PARTS.")

    payload_parts = payload.get("part_texts")
    if not isinstance(payload_parts, list) or len(payload_parts) != selected_parts:
        fail("Story Plan source_payload Part texts are missing or incomplete.")
    for index, payload_text in enumerate(payload_parts, 1):
        if not isinstance(payload_text, str) or not clean(payload_text):
            fail(f"Story Plan source_payload Part {index} is empty or invalid.")

    return plan_parts, actual_counts


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
            fail(f"Part {part_number} contains unexpected {kind} record.")
        return None

    if not isinstance(record, dict):
        fail(f"Part {part_number} is missing its separate {kind} record.")

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
            fail(f"{expected_id}: invalid {field}.")

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
    declared_counts: list[int],
) -> list[dict]:
    scenes_min = input_data["scenes_min"]
    scenes_max = input_data["scenes_max"]
    mode = input_data["mode"]

    verified_parts = []
    global_scene = 0
    actual_counts = []

    for part_number, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            fail(f"Plan Part {part_number} must be an object.")

        if part.get("part") != part_number:
            fail(f"Plan Part {part_number} numbering mismatch.")

        if clean(part.get("part_id")) != f"part_{part_number:02d}":
            fail(f"Plan Part {part_number} ID mismatch.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list):
            fail(f"Plan Part {part_number} Scene list is invalid.")

        if not scenes_min <= len(scenes) <= scenes_max:
            fail(
                f"Plan Part {part_number} must contain "
                f"{scenes_min}-{scenes_max} Main Scenes."
            )

        if len(scenes) != declared_counts[part_number - 1]:
            fail(
                f"Plan actual_scene_counts mismatch in Part {part_number}."
            )

        if part.get("main_scene_count") != len(scenes):
            fail(f"Plan Part {part_number} main_scene_count mismatch.")

        if part.get("actual_scene_count") != len(scenes):
            fail(f"Plan Part {part_number} actual_scene_count mismatch.")

        if part.get("hook_required") is not True:
            fail(f"Plan Part {part_number} must have a Hook.")

        final_part = part_number == selected_parts

        if part.get("suspense_required") is not (not final_part):
            fail(f"Plan Part {part_number} Suspense requirement mismatch.")

        if part.get("final_resolution") is not final_part:
            fail(f"Plan Part {part_number} Resolution requirement mismatch.")

        if not clean(part.get("narration")):
            fail(f"Plan Part {part_number} narration is empty.")
        if part.get("text_locked") is not True:
            fail(f"Plan Part {part_number} text is not locked.")
        if part.get("locked_word_count") != word_count(part.get("narration")):
            fail(f"Plan Part {part_number} locked word count mismatch.")
        if part.get("locked_text_sha256") != sha256_text(clean(part.get("narration"))):
            fail(f"Plan Part {part_number} locked text hash mismatch.")

        if not clean(part.get("story_anchor")):
            fail(f"Plan Part {part_number} story anchor is empty.")

        part_anchors = []

        for scene_number, scene in enumerate(scenes, 1):
            global_scene += 1

            if not isinstance(scene, dict):
                fail(f"Invalid Plan Scene in Part {part_number}.")

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

            narration = clean(scene.get("narration"))
            anchor = clean(scene.get("story_anchor"))

            if not narration or narration != anchor:
                fail(f"Plan Scene narration/anchor mismatch: {scene_id}.")
            if scene.get("source_text_sha256") != sha256_text(narration):
                fail(f"Plan Scene source text hash mismatch: {scene_id}.")
            if scene.get("source_word_count") != word_count(narration):
                fail(f"Plan Scene source word count mismatch: {scene_id}.")

            part_anchors.append(anchor)

        if clean(" ".join(part_anchors)) != clean(part.get("narration")):
            fail(
                f"Plan Main Scenes do not reconstruct Part {part_number}."
            )

        if clean(" ".join(part_anchors)) != clean(part.get("story_anchor")):
            fail(
                f"Plan Part {part_number} anchor differs from Scene anchors."
            )

        hook = verify_extra_scene(
            part.get("hook_scene"), part_number, "hook",
            True, final_part, mode,
        )
        suspense = verify_extra_scene(
            part.get("suspense_scene"), part_number, "suspense",
            not final_part, False, mode,
        )
        resolution = verify_extra_scene(
            part.get("resolution_scene"), part_number, "resolution",
            final_part, True, mode,
        )

        extras = [
            row for row in (hook, suspense, resolution)
            if isinstance(row, dict)
        ]

        if part.get("extra_scenes") != extras:
            fail(
                f"Plan Part {part_number} extra_scenes "
                "does not match role records."
            )

        if clean(hook.get("story_anchor")) != part_anchors[0]:
            fail(f"Part {part_number} Hook anchor mismatch.")

        if suspense and clean(suspense.get("story_anchor")) != part_anchors[-1]:
            fail(f"Part {part_number} Suspense anchor mismatch.")

        if resolution and clean(resolution.get("story_anchor")) != part_anchors[-1]:
            fail("Final Resolution anchor mismatch.")

        actual_counts.append(len(scenes))
        verified_parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "main_scene_count": len(scenes),
            "hook": hook["scene_id"],
            "suspense": suspense["scene_id"] if suspense else None,
            "resolution": resolution["scene_id"] if resolution else None,
            "main_scene_ids": [scene["scene_id"] for scene in scenes],
        })

    if actual_counts != declared_counts:
        fail("Plan Scene counts differ from the declared actual_scene_counts.")

    if sum(actual_counts) != global_scene:
        fail("Plan total Main Scene count mismatch.")

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
    scenes_min = input_data["scenes_min"]
    scenes_max = input_data["scenes_max"]

    if clean(story.get("status")).lower() != "completed":
        fail("story.json status must be completed.")

    if clean(story.get("format")).lower() != mode:
        fail("Story FORMAT does not match Input.")

    title = clean(story.get("title"))
    if not title or len(title) > MAX_TITLE_LENGTH:
        fail("Story title is empty or exceeds 180 characters.")

    if clean(story.get("final_title")) != title:
        fail("Story final_title does not match story title.")

    if explicit_title and title != explicit_title:
        fail("Generated title differs from explicit TITLE in Input.")

    if clean(story.get("topic")) != topic:
        fail("Story topic does not match Input.")

    if normalized_text(story.get("source_text", "")) != source_text:
        fail("Story source_text differs from complete STORY_TEXT in Input.")

    if clean(story.get("scene_order")) != "part_then_scene":
        fail("Story scene_order must be part_then_scene.")

    if clean(story.get("role_order")) != "hook_then_main_then_suspense_or_resolution":
        fail("Story role_order is invalid.")

    expected_values = {
        "total_parts": selected_parts,
        "parts_count": selected_parts,
        "selected_parts": selected_parts,
        "selected_parts_count": selected_parts,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
    }

    for field, expected in expected_values.items():
        if positive_int(story.get(field), f"Story {field}") != expected:
            fail(f"Story {field} does not match expected value {expected}.")

    if story.get("scenes_range") != [scenes_min, scenes_max]:
        fail("Story scenes_range does not match Input.")

    if positive_int(
        story.get("scenes_per_part"),
        "Story scenes_per_part",
    ) != scenes_min:
        fail("Story scenes_per_part must retain configured minimum.")

    declared_source_words = positive_int(
        story.get("source_word_count"),
        "Story source_word_count",
        allow_zero=True,
    )
    if declared_source_words != word_count(source_text):
        fail("Story source_word_count does not match complete input source.")

    source_fingerprint = clean(story.get("source_fingerprint"))
    if not re.fullmatch(r"[a-fA-F0-9]{64}", source_fingerprint):
        fail("Story source_fingerprint is missing or invalid.")

    corrected_story = story.get("corrected_complete_story")
    if not isinstance(corrected_story, str) or not clean(corrected_story):
        fail("Corrected complete story is missing.")

    corrected_words = word_count(corrected_story)

    if positive_int(
        story.get("corrected_complete_story_word_count"),
        "Story corrected_complete_story_word_count",
    ) != corrected_words:
        fail("Corrected complete story word count metadata is invalid.")

    if clean(story.get("story_text")) != clean(corrected_story):
        fail("story_text and corrected_complete_story differ.")

    if positive_int(
        story.get("total_story_words"),
        "Story total_story_words",
    ) != corrected_words:
        fail("Story total_story_words metadata mismatch.")

    if not clean(story.get("generation_method")) or not clean(story.get("model")):
        fail("Story generation_method or model metadata is missing.")

    proofreading = story.get("proofreading")
    if not isinstance(proofreading, dict):
        fail("Story proofreading metadata is missing.")

    if source_text and proofreading.get("performed") is not True:
        fail("Supplied story has no confirmed proofreading record.")

    parts = story.get("parts")
    all_scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != selected_parts:
        fail("Story Part list does not match selected PARTS.")

    if not isinstance(all_scenes, list):
        fail("Story top-level Main Scene list is missing.")

    if len(all_scenes) > MAX_TOTAL_SCENES:
        fail("Story exceeds the total Main Scene safety limit.")

    actual_counts = story.get("actual_scene_counts")
    if not isinstance(actual_counts, list) or len(actual_counts) != selected_parts:
        fail("Story actual_scene_counts is missing or invalid.")

    actual_counts = [
        positive_int(value, f"Story actual_scene_counts[{index}]")
        for index, value in enumerate(actual_counts, 1)
    ]

    if any(not scenes_min <= count <= scenes_max for count in actual_counts):
        fail("Story contains a Part outside configured Scene range.")

    if sum(actual_counts) != len(all_scenes):
        fail("Story actual Scene counts do not equal top-level Scene count.")

    if positive_int(story.get("total_scenes"), "Story total_scenes") != len(all_scenes):
        fail("Story total_scenes metadata mismatch.")

    if positive_int(
        story.get("total_main_scenes"),
        "Story total_main_scenes",
    ) != len(all_scenes):
        fail("Story total_main_scenes metadata mismatch.")

    return title, parts, all_scenes


def meaningful_tokens(value: str) -> set[str]:
    tokens = re.findall(r"[^\W_]+", value.lower(), flags=re.UNICODE)
    return {
        token for token in tokens
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
            "Vocabulary overlap is not proof of semantic preservation, "
            "event completeness or unchanged character relationships."
        ),
    }


def verify_revision(
    part: dict,
    narration: str,
    part_number: int,
) -> dict:
    evidence = part.get("revision_evidence")
    if not isinstance(evidence, dict):
        fail(f"Part {part_number} has no structured revision evidence.")

    try:
        valid = verify_revision_evidence(
            evidence=evidence,
            revised_text=narration,
        )
    except TypeError:
        try:
            valid = verify_revision_evidence(evidence, narration)
        except Exception as exc:
            fail(
                f"Part {part_number} revision evidence "
                f"cannot be verified: {exc}"
            )
    except Exception as exc:
        fail(
            f"Part {part_number} revision evidence "
            f"cannot be verified: {exc}"
        )

    if valid is not True:
        fail(f"Part {part_number} revision evidence is not valid.")

    return evidence


def verify_story_parts(
    parts: list[dict],
    all_scenes: list[dict],
    plan_parts: list[dict],
    input_data: dict,
    selected_parts: int,
    declared_counts: list[int],
) -> tuple[list[int], list[dict]]:
    mode = input_data["mode"]
    scenes_min = input_data["scenes_min"]
    scenes_max = input_data["scenes_max"]

    seen_ids: set[str] = set()
    flattened: list[dict] = []
    word_counts: list[int] = []
    part_results: list[dict] = []
    global_scene = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            fail(f"Story Part {part_number} must be an object.")

        expected_part_id = f"part_{part_number:02d}"

        if positive_int(part.get("part"), f"Part {part_number} number") != part_number:
            fail(f"Story Part numbering mismatch: {part_number}.")

        if clean(part.get("part_id")) != expected_part_id:
            fail(f"Story Part ID mismatch: {expected_part_id}.")

        narration = clean(part.get("narration"))
        if not narration:
            fail(f"Part {part_number} narration is empty.")

        amount = word_count(narration)
        if part.get("text_locked") is not True:
            fail(f"Part {part_number} narration is not marked as locked.")
        if positive_int(
            part.get("locked_word_count"),
            f"Part {part_number} locked_word_count",
        ) != amount:
            fail(f"Part {part_number} locked word count mismatch.")
        if clean(part.get("locked_text_sha256")) != sha256_text(narration):
            fail(f"Part {part_number} locked text hash mismatch.")

        if positive_int(
            part.get("word_count"),
            f"Part {part_number} word_count",
        ) != amount:
            fail(f"Part {part_number} word_count metadata mismatch.")

        merged_final = (
            mode == "short"
            and part_number == selected_parts
            and part.get("final_part_merged") is True
        )

        if mode == "short":
            maximum = (
                SHORT_MERGED_MAX_WORDS
                if merged_final
                else SHORT_HARD_MAX_WORDS
            )

            if amount < SHORT_MIN_WORDS or amount > maximum:
                fail(
                    f"SHORT Part {part_number} has {amount} words; "
                    f"allowed range is {SHORT_MIN_WORDS}-{maximum}."
                )

            if amount > SHORT_PREFERRED_MAX_WORDS and not merged_final:
                if part.get("revision_attempted") is not True:
                    fail(
                        f"SHORT Part {part_number} exceeds "
                        f"{SHORT_PREFERRED_MAX_WORDS} words "
                        "without a recorded revision."
                    )
                verify_revision(part, narration, part_number)

            if merged_final and amount > SHORT_MERGED_MAX_WORDS:
                fail("Final merged SHORT Part exceeds 239 words.")

        if narration[-1] not in BOUNDARY_ENDINGS:
            fail(
                f"Part {part_number} ends at an incomplete "
                "sentence/clause boundary."
            )

        scenes = part.get("scenes")
        planned_scenes = plan_parts[part_number - 1].get("scenes")

        if not isinstance(scenes, list):
            fail(f"Story Part {part_number} Scene list is invalid.")

        if not scenes_min <= len(scenes) <= scenes_max:
            fail(
                f"Part {part_number} must contain "
                f"{scenes_min}-{scenes_max} Main Scenes."
            )

        if len(scenes) != declared_counts[part_number - 1]:
            fail(f"Story actual Scene count mismatch in Part {part_number}.")

        if not isinstance(planned_scenes, list) or len(planned_scenes) != len(scenes):
            fail(f"Plan/story Scene count mismatch in Part {part_number}.")

        scene_narrations = []

        for scene_number, scene in enumerate(scenes, 1):
            global_scene += 1
            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

            if not isinstance(scene, dict):
                fail(f"Story Scene {scene_id} must be an object.")

            expected_values = {
                "scene_id": scene_id,
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_scene,
                "role": "main",
            }

            for field, expected in expected_values.items():
                if scene.get(field) != expected:
                    fail(f"Story {field} mismatch: {scene_id}.")

            if scene_id in seen_ids:
                fail(f"Duplicate story Scene ID: {scene_id}.")

            seen_ids.add(scene_id)

            planned = planned_scenes[scene_number - 1]
            if not isinstance(planned, dict):
                fail(f"Plan Scene is invalid: {scene_id}.")

            for field in (
                "scene_id",
                "part",
                "scene",
                "global_scene_number",
                "role",
                "beat",
                "purpose",
                "story_anchor",
                "narration",
            ):
                if clean(scene.get(field)) != clean(planned.get(field)):
                    fail(f"Story/Plan {field} mismatch: {scene_id}.")

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    fail(f"Story {field} is missing: {scene_id}.")

            if clean(scene.get("visual")) != clean(scene.get("visual_prompt")):
                fail(f"Story visual and visual_prompt differ: {scene_id}.")

            if not isinstance(scene.get("dialogue", ""), str):
                fail(f"Story dialogue must be text: {scene_id}.")

            scene_text = clean(scene.get("narration"))

            if scene_text != clean(scene.get("story_anchor")):
                fail(f"Scene narration/anchor mismatch: {scene_id}.")
            if clean(planned.get("source_text_sha256")) != sha256_text(scene_text):
                fail(f"Scene source text hash mismatch: {scene_id}.")
            if planned.get("source_word_count") != word_count(scene_text):
                fail(f"Scene source word count mismatch: {scene_id}.")

            scene_narrations.append(scene_text)
            flattened.append(scene)

        combined = clean(" ".join(scene_narrations))

        if combined != narration:
            fail(
                f"Part {part_number} narration does not match "
                "ordered Scene narration."
            )

        if word_count(combined) != amount:
            fail(f"Part {part_number} Scene reconstruction changed word count.")

        if part.get("main_scene_count") != len(scenes):
            fail(f"Part {part_number} main_scene_count mismatch.")

        if part.get("actual_scene_count") != len(scenes):
            fail(f"Part {part_number} actual_scene_count mismatch.")

        word_counts.append(amount)
        part_results.append({
            "part": part_number,
            "part_id": expected_part_id,
            "word_count": amount,
            "scene_count": len(scenes),
            "scene_ids": [scene["scene_id"] for scene in scenes],
            "narration_sha256": sha256_text(narration),
            "natural_ending_boundary": narration[-1],
            "final_part_merged": merged_final,
            "revision_attempted": part.get("revision_attempted") is True,
            "status": "verified",
        })

    if flattened != all_scenes:
        fail("Top-level scenes do not match ordered Part Scene records.")

    if len(seen_ids) != len(all_scenes):
        fail("Top-level Scene count differs from unique Part Scene IDs.")

    actual_counts = [len(part.get("scenes", [])) for part in parts]
    if actual_counts != declared_counts:
        fail("Story actual_scene_counts does not match Part Scene lists.")

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

    if clean(story.get("corrected_complete_story")) != combined:
        fail("corrected_complete_story does not match ordered Part narrations.")
    if story.get("story_text_locked") is not True:
        fail("Corrected complete story is not marked as locked.")
    if clean(story.get("corrected_complete_story_sha256")) != sha256_text(combined):
        fail("Corrected complete story fingerprint mismatch.")

    if positive_int(
        story.get("total_story_words"),
        "Story total_story_words",
    ) != word_count(combined):
        fail("total_story_words metadata mismatch.")

    if mode == "full" and len(parts) != 1:
        fail("FULL mode must contain exactly one complete story Part.")

    return combined


def verify_plan_story_match(
    plan_parts: list[dict],
    story_parts: list[dict],
) -> None:
    if len(plan_parts) != len(story_parts):
        fail("Plan/story Part counts differ.")

    for part_number, (planned_part, story_part) in enumerate(
        zip(plan_parts, story_parts), 1
    ):
        if clean(planned_part.get("narration")) != clean(story_part.get("narration")):
            fail(f"Plan/story Part narration mismatch in Part {part_number}.")

        plan_scenes = planned_part.get("scenes")
        story_scenes = story_part.get("scenes")

        if not isinstance(plan_scenes, list) or not isinstance(story_scenes, list):
            fail(f"Plan/story Scene lists are invalid in Part {part_number}.")

        if len(plan_scenes) != len(story_scenes):
            fail(f"Plan/story Scene counts differ in Part {part_number}.")

        for scene_number, (planned, generated) in enumerate(
            zip(plan_scenes, story_scenes), 1
        ):
            if not isinstance(planned, dict) or not isinstance(generated, dict):
                fail(f"Invalid Plan/story record in Part {part_number}.")

            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

            for field in (
                "scene_id",
                "part",
                "scene",
                "global_scene_number",
                "role",
                "beat",
                "purpose",
                "story_anchor",
                "narration",
            ):
                if clean(planned.get(field)) != clean(generated.get(field)):
                    fail(f"Plan/story {field} mismatch: {scene_id}.")


def verify_role_records(
    story: dict,
    parts: list[dict],
    selected_parts: int,
) -> dict:
    hooks = []
    suspenses = []
    resolutions = []

    for part_number, part in enumerate(parts, 1):
        is_final = part_number == selected_parts
        scenes = part.get("scenes", [])

        if not scenes:
            fail(f"Part {part_number} has no Main Scenes.")

        for kind, field, required in (
            ("hook", "hook_scene", True),
            ("suspense", "suspense_scene", not is_final),
            ("resolution", "resolution_scene", is_final),
        ):
            record = part.get(field)

            if not required:
                if record is not None:
                    fail(f"Part {part_number} has unexpected {kind} record.")
                continue

            if not isinstance(record, dict):
                fail(f"Part {part_number} is missing {kind} record.")

            expected_id = f"part_{part_number:02d}_{kind}"

            if record.get("part") != part_number or record.get("role") != kind:
                fail(f"Part {part_number} {kind} metadata mismatch.")

            if record.get("scene_id") != expected_id:
                fail(f"Part {part_number} {kind} ID mismatch.")

            if record.get("separate_from_main_scenes") is not True:
                fail(f"Part {part_number} {kind} is not a separate record.")

            if record.get("main_scene_numbering_affected") is not False:
                fail(f"Part {part_number} {kind} affects Main Scene numbering.")

            if record.get("scene") is not None or record.get("global_scene_number") is not None:
                fail(f"Part {part_number} {kind} has Main Scene numbering.")

            if record.get("is_final_part") is not is_final:
                fail(f"Part {part_number} {kind} final-Part marker mismatch.")

            for field in (
                "story_anchor",
                "purpose",
                "instruction",
                "continuity_rule",
                "invention_policy",
                "visual_policy",
            ):
                if not clean(record.get(field)):
                    fail(f"Part {part_number} {kind} is missing {field}.")

            expected_anchor = (
                clean(scenes[0].get("story_anchor"))
                if kind == "hook"
                else clean(scenes[-1].get("story_anchor"))
            )

            if clean(record.get("story_anchor")) != expected_anchor:
                fail(f"Part {part_number} {kind} anchor mismatch.")

            if kind == "hook":
                hooks.append(record)
            elif kind == "suspense":
                suspenses.append(record)
            else:
                resolutions.append(record)

        expected_extras = [
            row for row in (
                part.get("hook_scene"),
                part.get("suspense_scene"),
                part.get("resolution_scene"),
            )
            if isinstance(row, dict)
        ]

        if part.get("extra_scenes") != expected_extras:
            fail(
                f"Part {part_number} extra_scenes "
                "does not match role records."
            )

    expected_counts = {
        "total_hook_scenes": selected_parts,
        "total_suspense_scenes": max(0, selected_parts - 1),
        "total_resolution_scenes": 1,
    }

    actual_counts = {
        "total_hook_scenes": len(hooks),
        "total_suspense_scenes": len(suspenses),
        "total_resolution_scenes": len(resolutions),
    }

    for field, expected in expected_counts.items():
        if actual_counts[field] != expected or story.get(field) != expected:
            fail(f"Story {field} metadata mismatch.")

    if story.get("hook_scenes") != hooks:
        fail("Top-level Hook records differ from Part records.")

    if story.get("suspense_scenes") != suspenses:
        fail("Top-level Suspense records differ from Part records.")

    if story.get("resolution_scenes") != resolutions:
        fail("Top-level Resolution records differ from Part records.")

    return {
        "hook_count": len(hooks),
        "suspense_count": len(suspenses),
        "resolution_count": len(resolutions),
    }


def verify_title_file(title: str) -> None:
    if not TITLE_FILE.is_file():
        fail("final_title.txt is missing.")

    try:
        saved_title = clean(TITLE_FILE.read_text(encoding="utf-8-sig"))
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
    selected_parts = verify_selected_parts(story, plan, input_data)

    plan_parts, plan_counts = verify_plan_header(
        plan,
        input_data,
        selected_parts,
    )

    plan_results = verify_plan_parts(
        plan_parts,
        input_data,
        selected_parts,
        plan_counts,
    )

    flattened_plan_scenes = [
        scene for part in plan_parts for scene in part["scenes"]
    ]
    if plan.get("scenes") != flattened_plan_scenes:
        fail("Top-level Plan Scenes do not match ordered Part Scene records.")

    payload_parts = plan.get("source_payload", {}).get("part_texts", [])
    for part_number, (payload_text, planned_part) in enumerate(
        zip(payload_parts, plan_parts), 1
    ):
        if normalized_text(payload_text) != normalized_text(planned_part.get("narration")):
            fail(f"Plan source_payload text differs from planned Part {part_number}.")

    title, story_parts, all_scenes = verify_story_header(
        story,
        input_data,
        selected_parts,
    )

    story_counts = story.get("actual_scene_counts")
    if story_counts != plan_counts:
        fail(
            "Story/Plan actual Scene counts differ: "
            f"Story={story_counts}, Plan={plan_counts}."
        )

    word_counts, part_results = verify_story_parts(
        story_parts,
        all_scenes,
        plan_parts,
        input_data,
        selected_parts,
        plan_counts,
    )

    combined_story = verify_story_text(
        story,
        story_parts,
        input_data["mode"],
    )

    role_counts = verify_role_records(
        story,
        story_parts,
        selected_parts,
    )

    verify_plan_story_match(plan_parts, story_parts)
    verify_title_file(title)

    # generate_story.py stores the Plan source fingerprint, not
    # a hash of the entire story_plan.json object.
    story_plan_fingerprint = clean(story.get("story_plan_fingerprint"))
    plan_source_fingerprint = clean(plan.get("source_fingerprint"))

    if not story_plan_fingerprint:
        fail("Story story_plan_fingerprint is missing.")

    if story_plan_fingerprint != plan_source_fingerprint:
        fail("Story/Plan source fingerprint mismatch.")
    if story.get("story_plan_version") not in (None, PLAN_VERSION):
        fail("Story records an incompatible Story Plan version.")
    if story.get("story_plan_mapping_version") not in (None, MAPPING_VERSION):
        fail("Story records an incompatible Story Plan mapping version.")

    source_text = input_data["source_text"]

    if source_text:
        if normalized_text(story.get("source_text")) != normalized_text(source_text):
            fail("Complete source story was changed in story metadata.")

        if word_count(story.get("source_text")) != word_count(source_text):
            fail("Complete source word count was not preserved.")

    source_coverage = verify_source_coverage(
        source_text,
        combined_story,
        input_data["mode"],
    )

    policy = story.get("story_length_policy")
    if not isinstance(policy, dict):
        fail("Story length policy metadata is missing.")

    if policy.get("actual_parts") != selected_parts:
        fail("Story length policy actual_parts mismatch.")

    if policy.get("actual_scene_counts") != story_counts:
        fail("Story length policy Scene counts mismatch.")
    if policy.get("format") not in (None, input_data["mode"]):
        fail("Story length policy FORMAT mismatch.")
    if policy.get("revision_outcome") == "pending_final_part_recovery":
        fail("Story still has an unresolved final-Part retry/merge recovery.")
    if policy.get("final_part_validation_passed") is False:
        fail("Story length policy final validation is not passed.")

    selection = story.get("parts_selection")
    if not isinstance(selection, dict):
        fail("Story parts_selection metadata is missing.")

    if selection.get("selected_parts") != selected_parts:
        fail("Story parts_selection selected_parts mismatch.")

    if selection.get("minimum_parts") != input_data["minimum_parts"]:
        fail("Story parts_selection minimum does not match Input.")

    if selection.get("maximum_parts") != input_data["maximum_parts"]:
        fail("Story parts_selection maximum does not match Input.")
    if selection.get("selected_parts") != selection.get("actual_parts", selected_parts):
        fail("Story parts_selection selected/actual Part counts differ.")
    if selection.get("selection_persisted") is False:
        fail("Story selected PARTS were not persisted in Input configuration.")

    merged_parts = [
        part_number
        for part_number, part in enumerate(story_parts, 1)
        if part.get("final_part_merged") is True
    ]

    if merged_parts and merged_parts != [selected_parts]:
        fail("Only the final Part may use the final-merge exception.")

    if policy.get("final_part_merged") is not bool(merged_parts):
        fail("Story final_part_merged policy metadata mismatch.")

    if input_data["mode"] == "short":
        for part_number, part in enumerate(story_parts, 1):
            amount = word_count(part.get("narration"))
            merged = part_number == selected_parts and bool(merged_parts)

            maximum = (
                SHORT_MERGED_MAX_WORDS
                if merged
                else SHORT_HARD_MAX_WORDS
            )

            if not SHORT_MIN_WORDS <= amount <= maximum:
                fail(
                    f"SHORT Part {part_number} violates "
                    "its word-count limits."
                )

            if amount > SHORT_PREFERRED_MAX_WORDS and not merged:
                verify_revision(
                    part,
                    clean(part.get("narration")),
                    part_number,
                )

    part_text_hashes = [
        sha256_text(clean(part.get("narration")))
        for part in story_parts
    ]
    scene_text_hashes = [
        sha256_text(clean(scene.get("narration")))
        for scene in all_scenes
    ]
    duplicate_scene_text_hashes = sorted({
        item for item in scene_text_hashes
        if scene_text_hashes.count(item) > 1
    })

    return {
        "status": "completed",
        "verification_version": VERSION,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": input_data["mode"],
        "title": title,
        "topic": input_data["topic"],
        "story_fingerprint": sha256_json(story),
        "plan_fingerprint": sha256_json(plan),
        "plan_source_fingerprint": plan_source_fingerprint,
        "summary": {
            "configured_parts_min": input_data["minimum_parts"],
            "configured_parts_max": input_data["maximum_parts"],
            "selected_parts": selected_parts,
            "scenes_min": input_data["scenes_min"],
            "scenes_max": input_data["scenes_max"],
            "actual_scene_counts": story_counts,
            "total_main_scenes": len(all_scenes),
            "total_story_words": word_count(combined_story),
            "words_per_part": word_counts,
            "final_part_merged": bool(merged_parts),
            "hook_count": role_counts["hook_count"],
            "suspense_count": role_counts["suspense_count"],
            "resolution_count": role_counts["resolution_count"],
            "source_story_provided": bool(source_text),
            "source_coverage": source_coverage,
            "part_text_sha256": part_text_hashes,
            "scene_text_sha256": scene_text_hashes,
            "duplicate_scene_text_hashes": duplicate_scene_text_hashes,
        },
        "parts": part_results,
        "plan_parts": plan_results,
        "checks": {
            "input_format": True,
            "configured_parts_range": True,
            "selected_parts_in_range": True,
            "configured_scenes_range": True,
            "actual_scene_counts": True,
            "story_plan_scene_counts_match": True,
            "scene_ids_and_global_order": True,
            "sentence_carry_forward_plan": True,
            "hook_suspense_resolution_roles": True,
            "story_part_ids_and_order": True,
            "scene_narration_integrity": True,
            "top_level_scene_consistency": True,
            "short_word_limits": True,
            "verified_revision_evidence": True,
            "final_part_merge_exception": True,
            "full_single_part_rule": True,
            "natural_part_boundaries": True,
            "story_title": True,
            "title_file": True,
            "source_text_integrity": True,
            "story_plan_fingerprint": True,
            "plan_source_payload_matches_parts": True,
            "plan_top_level_scenes_match_parts": True,
            "strict_json_duplicate_key_check": True,
            "source_vocabulary_coverage": source_coverage["status"],
            "no_external_api_calls": plan.get("source", {}).get("api_calls") == 0,
        },
        "api_calls_made": 0,
        "story_modified_by_verifier": False,
        "plan_modified_by_verifier": False,
        "semantic_preservation_proven": False,
        "semantic_preservation_limitation": (
            "The verifier checks source integrity, vocabulary coverage, "
            "ordered narration reconstruction and revision evidence. "
            "Lexical overlap alone does not prove semantic equivalence."
        ),
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

    # Only the verification report is written. Story and Plan are read-only.
    write_json(REPORT_FILE, report)

    summary = report["summary"]

    log("VALIDATION: SUCCESS")
    log("FORMAT:", report["format"])
    log("TITLE:", report["title"])
    log(
        "CONFIGURED PARTS RANGE:",
        f"{summary['configured_parts_min']}-{summary['configured_parts_max']}",
    )
    log("SELECTED PARTS:", summary["selected_parts"])
    log(
        "CONFIGURED SCENES RANGE:",
        f"{summary['scenes_min']}-{summary['scenes_max']}",
    )
    log("ACTUAL SCENE COUNTS:", summary["actual_scene_counts"])
    log("MAIN SCENES:", summary["total_main_scenes"])
    log("HOOKS:", summary["hook_count"])
    log("SUSPENSE RECORDS:", summary["suspense_count"])
    log("RESOLUTION RECORDS:", summary["resolution_count"])
    log("TOTAL STORY WORDS:", summary["total_story_words"])
    log("WORDS PER PART:", summary["words_per_part"])
    log("FINAL PART MERGED:", summary["final_part_merged"])
    log("SOURCE COVERAGE:", summary["source_coverage"]["status"])
    log("API CALLS: 0")
    log("STORY MODIFIED: false")
    log("PLAN MODIFIED: false")
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
