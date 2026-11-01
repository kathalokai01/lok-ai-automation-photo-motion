#!/usr/bin/env python3
"""Validate story length, configured part count and scene structure."""

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
    get_parts,
    get_scenes,
)
from story_length_policy import (
    POLICY_VERSION,
    build_length_policy,
    clean_text,
    word_count,
)

STORY_DIR = ROOT / "output" / "story"
STORY_FILE = STORY_DIR / "story.json"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
REPORT_FILE = STORY_DIR / "story_length_validation.json"

REPORT_VERSION = "story-length-validation-v4-configured-parts"

SHORT_MIN_WORDS = 120
SHORT_TARGET_WORDS = 135
SHORT_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180


def log(*values: Any) -> None:
    print(*values, flush=True)


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
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


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default

    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_format(value: Any) -> str:
    value = clean_text(value).lower()

    aliases = {
        "shorts": "short",
        "reel": "short",
        "reels": "short",
        "vertical": "short",
        "long": "full",
        "longform": "full",
        "long-form": "full",
        "landscape": "full",
        "full_video": "full",
    }

    return aliases.get(value, value)


def get_valid_parts(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []

    parts = data.get("parts")
    if not isinstance(parts, list):
        return []

    return [
        part for part in parts
        if isinstance(part, dict)
    ]


def extract_scene_text(scene: dict) -> str:
    """Count narration only, never visual prompts or metadata."""
    for key in (
        "narration",
        "narration_text",
        "voiceover",
        "text",
    ):
        value = scene.get(key)

        if isinstance(value, str) and clean_text(value):
            return clean_text(value)

    return ""


def extract_part_text(part: dict) -> str:
    scenes = part.get("scenes")

    if isinstance(scenes, list):
        rows = [
            extract_scene_text(scene)
            for scene in scenes
            if isinstance(scene, dict)
        ]
        rows = [row for row in rows if row]

        if rows:
            return " ".join(rows)

    for key in ("narration", "story", "text", "content"):
        value = part.get(key)

        if isinstance(value, str) and clean_text(value):
            return clean_text(value)

    return ""


def extract_story_text(story: dict, parts: list[dict]) -> str:
    value = story.get("story_text")

    if isinstance(value, str) and clean_text(value):
        return clean_text(value)

    return " ".join(
        extract_part_text(part)
        for part in parts
        if extract_part_text(part)
    )


def calculate_part_word_results(
    parts: list[dict],
    format_type: str,
    full_policy: dict,
) -> list[dict]:
    results = []

    for index, part in enumerate(parts, 1):
        number = as_int(part.get("part"), index)
        narration = extract_part_text(part)
        amount = word_count(narration)

        if format_type == "short":
            minimum = SHORT_MIN_WORDS
            target = SHORT_TARGET_WORDS
            maximum = SHORT_MAX_WORDS
            hard_maximum = SHORT_HARD_MAX_WORDS

            if amount == 0:
                status = "missing_narration"
            elif amount < minimum:
                status = "too_short"
            elif amount > hard_maximum:
                status = "too_long"
            elif amount > maximum:
                status = "within_hard_limit_exception"
            else:
                status = "within_target"

        else:
            target = as_int(
                full_policy.get("target_words_per_part"),
                125,
            )
            minimum = max(
                1,
                round(target * 100 / 125),
            )
            maximum = max(
                minimum,
                round(target * 145 / 125),
            )
            hard_maximum = maximum

            if amount == 0:
                status = "missing_narration"
            elif amount < minimum:
                status = "too_short"
            elif amount > maximum:
                status = "too_long"
            else:
                status = "within_target"

        results.append({
            "part_number": number,
            "word_count": amount,
            "target_words": target,
            "minimum_words": minimum,
            "maximum_words": maximum,
            "hard_maximum_words": hard_maximum,
            "status": status,
            "needs_revision": status in {
                "missing_narration",
                "too_short",
                "too_long",
            },
        })

    return results


def validate_structure(
    parts: list[dict],
    expected_parts: int,
    expected_scenes: int,
) -> tuple[list[dict], list[str]]:
    results = []
    errors = []

    if len(parts) != expected_parts:
        errors.append(
            f"PARTS mismatch: configured {expected_parts}; "
            f"found {len(parts)}."
        )

    seen_parts = []
    global_scene_number = 0

    for part_index, part in enumerate(parts, 1):
        number = as_int(part.get("part"), part_index)
        seen_parts.append(number)

        expected_part_id = f"part_{part_index:02d}"
        actual_part_id = clean_text(part.get("part_id"))

        if actual_part_id and actual_part_id != expected_part_id:
            errors.append(
                f"Part {part_index}: expected part_id "
                f"{expected_part_id}; got {actual_part_id}."
            )

        if number != part_index:
            errors.append(
                f"Part order error: expected {part_index}; found {number}."
            )

        raw_scenes = part.get("scenes")
        scenes = (
            [item for item in raw_scenes if isinstance(item, dict)]
            if isinstance(raw_scenes, list)
            else []
        )

        if not isinstance(raw_scenes, list):
            errors.append(f"Part {number} has no scenes list.")
        elif len(scenes) != expected_scenes:
            errors.append(
                f"Part {number}: expected {expected_scenes} scenes; "
                f"found {len(scenes)}."
            )

        scene_numbers = []
        scene_words = 0
        missing_narration = []

        for scene_index, scene in enumerate(scenes, 1):
            global_scene_number += 1

            scene_number = as_int(
                scene.get("scene", scene.get("local_scene")),
                scene_index,
            )
            scene_numbers.append(scene_number)

            if scene_number != scene_index:
                errors.append(
                    f"Part {number}: scene order error at position "
                    f"{scene_index}; found {scene_number}."
                )

            expected_scene_id = (
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )
            actual_scene_id = clean_text(scene.get("scene_id"))

            if actual_scene_id and actual_scene_id != expected_scene_id:
                errors.append(
                    f"Scene ID mismatch: expected {expected_scene_id}; "
                    f"found {actual_scene_id}."
                )

            declared_global = scene.get("global_scene_number")
            if declared_global is not None:
                if as_int(declared_global, -1) != global_scene_number:
                    errors.append(
                        f"{expected_scene_id}: global scene number mismatch."
                    )

            narration = extract_scene_text(scene)

            if not narration:
                missing_narration.append(scene_index)
            else:
                scene_words += word_count(narration)

        if missing_narration:
            errors.append(
                f"Part {number}: narration missing in scenes "
                f"{missing_narration}."
            )

        if sorted(scene_numbers) != list(range(1, expected_scenes + 1)):
            errors.append(
                f"Part {number}: scene numbers must be sequential "
                f"from 1 to {expected_scenes}."
            )

        part_words = word_count(extract_part_text(part))

        if scene_words and part_words != scene_words:
            errors.append(
                f"Part {number}: part narration count ({part_words}) "
                f"does not match scene narration count ({scene_words})."
            )

        results.append({
            "part_number": number,
            "part_id": actual_part_id or expected_part_id,
            "scene_count": len(scenes),
            "expected_scene_count": expected_scenes,
            "narration_words": part_words,
            "scene_narration_words": scene_words,
            "missing_narration_scenes": missing_narration,
            "structure_valid": (
                isinstance(raw_scenes, list)
                and len(scenes) == expected_scenes
                and sorted(scene_numbers)
                == list(range(1, expected_scenes + 1))
                and not missing_narration
            ),
        })

    if seen_parts != list(range(1, expected_parts + 1)):
        errors.append(
            "Part numbers must be unique and ordered from 1 through "
            f"{expected_parts}; found {seen_parts}."
        )

    return results, errors


def validate_story_metadata(
    story: dict,
    parts: list[dict],
    scenes_per_part: int,
) -> list[str]:
    errors = []

    actual_parts = len(parts)
    actual_scenes = sum(
        len(part.get("scenes", []))
        for part in parts
        if isinstance(part.get("scenes"), list)
    )

    metadata_parts = story.get(
        "total_parts",
        story.get("parts_count"),
    )

    if metadata_parts is not None:
        if as_int(metadata_parts, -1) != actual_parts:
            errors.append(
                f"Story metadata part count mismatch: "
                f"{metadata_parts} vs {actual_parts}."
            )

    metadata_scenes = story.get("total_scenes")

    if metadata_scenes is not None:
        if as_int(metadata_scenes, -1) != actual_scenes:
            errors.append(
                f"Story metadata scene count mismatch: "
                f"{metadata_scenes} vs {actual_scenes}."
            )

    declared_scenes_per_part = story.get("scenes_per_part")

    if declared_scenes_per_part is not None:
        if as_int(declared_scenes_per_part, -1) != scenes_per_part:
            errors.append(
                "Story metadata scenes_per_part does not match Input/topic.txt."
            )

    declared_words = story.get("total_story_words")

    if declared_words is not None:
        actual_words = word_count(
            extract_story_text(story, parts)
        )

        if as_int(declared_words, -1) != actual_words:
            errors.append(
                f"Story metadata word count mismatch: "
                f"{declared_words} vs {actual_words}."
            )

    return errors

def build_configured_policy(
    format_type: str,
    configured_parts: int,
    scenes_per_part: int,
    story_length: Any,
    scene_duration: Any,
    total_words: int,
) -> dict:
    """
    Keep the shared policy's duration calculations, but enforce the
    configured PARTS value. SHORT must not silently become dynamic.
    """
    policy = build_length_policy(
        format_type=format_type,
        story_length=story_length,
        parts=configured_parts,
        scenes_per_part=scenes_per_part,
        scene_duration=scene_duration,
        source_word_count=total_words,
    )

    if format_type == "short":
        policy.update({
            "parts_count": configured_parts,
            "configured_parts_count": configured_parts,
            "parts_count_is_dynamic": False,
            "total_scenes": configured_parts * scenes_per_part,
            "target_words_per_part": SHORT_TARGET_WORDS,
            "minimum_words_per_part": SHORT_MIN_WORDS,
            "maximum_words_per_part": SHORT_MAX_WORDS,
            "hard_maximum_words_per_part": SHORT_HARD_MAX_WORDS,
            "target_total_words": configured_parts * SHORT_TARGET_WORDS,
            "minimum_total_words": configured_parts * SHORT_MIN_WORDS,
            "maximum_total_words": configured_parts * SHORT_HARD_MAX_WORDS,
            "source_word_count_within_target": (
                configured_parts * SHORT_MIN_WORDS
                <= total_words
                <= configured_parts * SHORT_HARD_MAX_WORDS
            ),
            "note": (
                "SHORT uses the exact PARTS value configured in "
                "Input/topic.txt. Each part targets 120–150 words; "
                "151–180 words is allowed only as a hard-limit exception. "
                "A part above 180 words or below 120 words requires revision."
            ),
        })

        policy["warnings"] = [
            warning for warning in policy.get("warnings", [])
            if "dynamic part count" not in str(warning).lower()
        ]

        if total_words < configured_parts * SHORT_MIN_WORDS:
            policy["warnings"].append(
                "Generated story is too short for the configured number "
                "of SHORT parts at 120 words minimum per part."
            )

        if total_words > configured_parts * SHORT_HARD_MAX_WORDS:
            policy["warnings"].append(
                "Generated story exceeds the configured SHORT parts "
                "capacity of 180 words per part."
            )

    else:
        policy["parts_count"] = configured_parts
        policy["configured_parts_count"] = configured_parts
        policy["parts_count_is_dynamic"] = False
        policy["total_scenes"] = configured_parts * scenes_per_part

    return policy


def validate_story_text_consistency(
    story: dict,
    parts: list[dict],
) -> list[str]:
    errors = []

    part_texts = [
        extract_part_text(part)
        for part in parts
    ]

    if any(not text for text in part_texts):
        return errors

    combined_parts = clean_text(" ".join(part_texts))
    declared_story = story.get("story_text")

    if isinstance(declared_story, str) and clean_text(declared_story):
        if clean_text(declared_story) != combined_parts:
            errors.append(
                "story_text does not match the ordered narration "
                "inside the story parts."
            )

    flattened_scenes = []

    for part in parts:
        scenes = part.get("scenes", [])

        if not isinstance(scenes, list):
            continue

        for scene in scenes:
            if isinstance(scene, dict):
                narration = extract_scene_text(scene)
                if narration:
                    flattened_scenes.append(narration)

    if flattened_scenes:
        combined_scenes = clean_text(" ".join(flattened_scenes))

        if combined_scenes != combined_parts:
            errors.append(
                "Part narration does not match the ordered scene narration."
            )

    return errors


def source_comparison(story: dict, generated_text: str) -> dict:
    source_text = story.get("source_text", "")

    if not isinstance(source_text, str):
        source_text = ""

    source_text = clean_text(source_text)
    source_words = word_count(source_text)
    generated_words = word_count(generated_text)

    ratio = None
    if source_words:
        ratio = round(generated_words / source_words, 4)

    warnings = []

    if not source_words:
        warnings.append(
            "No source_text field is available for source comparison."
        )
    elif generated_words < source_words:
        warnings.append(
            "Generated narration is shorter than supplied source text. "
            "Word count alone cannot establish whether story events "
            "or meaning were preserved."
        )

    return {
        "source_word_count": source_words,
        "generated_story_word_count": generated_words,
        "generated_to_source_ratio": ratio,
        "source_text_modified_by_validator": False,
        "warnings": warnings,
        "limitation": (
            "This comparison measures length only. It does not prove "
            "semantic preservation, event coverage or factual accuracy."
        ),
    }


def main() -> int:
    input_file = ROOT / "Input" / "topic.txt"

    if not input_file.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(str(input_file))

    configured_format = normalize_format(get_format(config))

    if configured_format not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {configured_format!r}."
        )

    configured_parts = as_int(get_parts(config), 0)
    configured_scenes = as_int(get_scenes(config), 0)

    if configured_parts < 1:
        raise RuntimeError("PARTS must be a positive integer.")

    if configured_scenes < 1:
        raise RuntimeError("SCENES must be a positive integer.")

    if configured_parts > 1000 or configured_scenes > 100:
        raise RuntimeError(
            "PARTS or SCENES exceeds the configured safety limit."
        )

    story = read_json(STORY_FILE)

    if not isinstance(story, dict):
        raise RuntimeError(
            "output/story/story.json is missing or invalid."
        )

    if story.get("status") != "completed":
        raise RuntimeError(
            "story.json status is not 'completed'."
        )

    actual_format = normalize_format(story.get("format"))

    if actual_format != configured_format:
        raise RuntimeError(
            f"FORMAT mismatch: Input={configured_format}; "
            f"story.json={actual_format}."
        )

    parts = get_valid_parts(story)

    if not parts:
        raise RuntimeError(
            "story.json contains no valid story parts."
        )

    story_text = extract_story_text(story, parts)
    total_words = word_count(story_text)

    if total_words <= 0:
        raise RuntimeError(
            "Generated story contains no narration text."
        )

    scenes_per_part = as_int(
        story.get("scenes_per_part"),
        configured_scenes,
    )

    if scenes_per_part != configured_scenes:
        raise RuntimeError(
            f"SCENES mismatch: Input={configured_scenes}; "
            f"story.json={scenes_per_part}."
        )

    policy = build_configured_policy(
        format_type=configured_format,
        configured_parts=configured_parts,
        scenes_per_part=configured_scenes,
        story_length=config.get("STORY_LENGTH", "auto"),
        scene_duration=config.get("SCENE_DURATION", "auto"),
        total_words=total_words,
    )

    structure_results, structure_errors = validate_structure(
        parts,
        configured_parts,
        configured_scenes,
    )

    metadata_errors = validate_story_metadata(
        story,
        parts,
        configured_scenes,
    )

    consistency_errors = validate_story_text_consistency(
        story,
        parts,
    )

    word_results = calculate_part_word_results(
        parts,
        configured_format,
        policy,
    )

    failed_parts = [
        row for row in word_results
        if row["needs_revision"]
    ]

    source_report = source_comparison(story, story_text)

    warnings = list(policy.get("warnings", []))
    warnings.extend(source_report["warnings"])

    # This stage runs before generate_ai_story.py in the current
    # workflow. Do not treat an old ai_story.json as authoritative.
    if AI_STORY_FILE.is_file():
        warnings.append(
            "ai_story.json exists but was not used. "
            "This report validates story.json, the output of "
            "generate_story.py."
        )

    all_errors = (
        structure_errors
        + metadata_errors
        + consistency_errors
    )

    valid = not all_errors and not failed_parts

    actual_scenes = sum(
        len([
            scene for scene in part.get("scenes", [])
            if isinstance(scene, dict)
        ])
        for part in parts
        if isinstance(part.get("scenes"), list)
    )

    expected_scenes = configured_parts * configured_scenes

    report = {
        "report_version": REPORT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": configured_format,
        "validation_source": "story.json",
        "validation_mode": "read_only",
        "policy": policy,
        "source_comparison": source_report,
        "summary": {
            "configured_parts": configured_parts,
            "parts_expected": configured_parts,
            "parts_found": len(parts),
            "scenes_per_part": configured_scenes,
            "scenes_expected": expected_scenes,
            "scenes_found": actual_scenes,
            "total_words": total_words,
            "parts_needing_revision": len(failed_parts),
            "structure_errors": all_errors,
            "warnings": warnings,
            "valid": valid,
            "api_calls_made": 0,
            "story_text_modified": False,
        },
        "structure_results": structure_results,
        "part_results": word_results,
    }

    write_json(POLICY_FILE, policy)
    write_json(REPORT_FILE, report)

    log("STORY LENGTH VALIDATION")
    log("FORMAT:", configured_format)
    log("SOURCE:", "story.json")
    log("CONFIGURED PARTS:", configured_parts)
    log("PARTS FOUND:", len(parts))
    log("SCENES PER PART:", configured_scenes)
    log("SCENES FOUND:", actual_scenes)
    log("EXPECTED SCENES:", expected_scenes)
    log("TOTAL WORDS:", total_words)

    if configured_format == "short":
        log(
            "WORDS PER PART:",
            f"{SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}",
            "(hard maximum:",
            SHORT_HARD_MAX_WORDS,
            ")",
        )

    log("PARTS NEEDING REVISION:", len(failed_parts))
    log("VALID:", valid)

    for error in all_errors:
        log("ERROR:", error)

    for warning in warnings:
        log("WARNING:", warning)

    for row in failed_parts:
        log(
            "PART LENGTH:",
            f"part={row['part_number']}",
            f"words={row['word_count']}",
            f"status={row['status']}",
        )

    log("API CALLS: 0")
    log("STORY MODIFIED: false")
    log("REPORT:", REPORT_FILE.relative_to(ROOT))

    # Preserve the existing workflow contract: this script creates
    # a diagnostic report. verify_story.py remains the hard gate.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("STORY LENGTH VALIDATION FAILED:", str(exc))
        sys.exit(1)