#!/usr/bin/env python3
"""Strict, read-only validation of story length, selected parts and scenes."""

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
    get_scenes,
    load_and_validate,
)
from story_length_policy import (
    POLICY_VERSION,
    build_length_policy,
    clean_text,
    normalize_format as normalize_policy_format,
    parse_parts_range,
    validate_story_length,
    word_count,
)

STORY_DIR = ROOT / "output" / "story"
STORY_FILE = STORY_DIR / "story.json"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
REPORT_FILE = STORY_DIR / "story_length_validation.json"

REPORT_VERSION = "story-length-validation-v9-selected-parts-and-natural-boundaries"

SHORT_MIN_WORDS = 120
SHORT_TARGET_WORDS = 135
SHORT_PREFERRED_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180

MAX_PARTS = 1000
MAX_SCENES_PER_PART = 100
MAX_STORY_WORDS = 10_000_000

BOUNDARY_ENDINGS = (
    "।", "॥", ".", "!", "?", "…", "...",
    ",", "，", ";", "؛", ":", "—", "–",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")


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
    try:
        return normalize_policy_format(value)
    except ValueError as exc:
        fail(str(exc))


def normalize_narration(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    value = value.replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", value).strip()


def extract_scene_text(scene: dict) -> str:
    """Read narration only; never count prompts, titles or metadata."""
    for key in (
        "narration",
        "narration_text",
        "voiceover",
        "text",
    ):
        value = scene.get(key)

        if isinstance(value, str) and normalize_narration(value):
            return normalize_narration(value)

    return ""


def extract_part_text(part: dict) -> str:
    for key in (
        "narration",
        "narration_text",
        "voiceover",
        "story",
        "text",
        "content",
    ):
        value = part.get(key)

        if isinstance(value, str) and normalize_narration(value):
            return normalize_narration(value)

    scenes = part.get("scenes")

    if isinstance(scenes, list):
        texts = [
            extract_scene_text(scene)
            for scene in scenes
            if isinstance(scene, dict)
        ]
        texts = [item for item in texts if item]

        if texts:
            return " ".join(texts)

    return ""


def get_story_parts(story: dict) -> list[dict]:
    parts = story.get("parts")

    if not isinstance(parts, list):
        fail("story.json must contain a parts list.")

    if not parts:
        fail("story.json contains no story parts.")

    if any(not isinstance(item, dict) for item in parts):
        fail("story.json contains an invalid Part record.")

    return parts


def get_story_text(story: dict, parts: list[dict]) -> str:
    declared = story.get("story_text")

    if isinstance(declared, str) and normalize_narration(declared):
        return normalize_narration(declared)

    return " ".join(
        extract_part_text(part)
        for part in parts
        if extract_part_text(part)
    )


def story_fingerprint(story: dict) -> str:
    payload = {
        "title": story.get("title"),
        "format": story.get("format"),
        "story_text": story.get("story_text"),
        "parts": story.get("parts"),
        "total_parts": story.get("total_parts"),
        "total_scenes": story.get("total_scenes"),
    }

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def validate_part_selection(
    config: dict,
    story: dict,
    actual_parts: int,
) -> tuple[int, int, list[str]]:
    errors = []

    try:
        minimum, maximum = get_parts_range(config)
    except (TypeError, ValueError) as exc:
        fail(f"Invalid configured PARTS range: {exc}")

    if not 1 <= minimum <= maximum <= MAX_PARTS:
        fail("Configured PARTS range is outside supported limits.")

    if not minimum <= actual_parts <= maximum:
        errors.append(
            f"Selected PARTS mismatch: configured range "
            f"{minimum}-{maximum}; story contains {actual_parts}."
        )

    declared_parts = story.get(
        "total_parts",
        story.get("parts_count"),
    )

    if declared_parts is not None:
        if as_int(declared_parts, -1) != actual_parts:
            errors.append(
                f"Story metadata part count mismatch: "
                f"declared {declared_parts}; actual {actual_parts}."
            )

    selected_metadata = story.get("selected_parts")

    if selected_metadata is not None:
        if as_int(selected_metadata, -1) != actual_parts:
            errors.append(
                "Story selected_parts metadata does not match "
                "the actual number of story parts."
            )

    return minimum, maximum, errors


def validate_part_numbering(
    parts: list[dict],
) -> tuple[list[int], list[str]]:
    errors = []
    numbers = []

    for index, part in enumerate(parts, 1):
        number = as_int(part.get("part"), -1)
        expected_id = f"part_{index:02d}"
        actual_id = clean_text(part.get("part_id"))

        if number != index:
            errors.append(
                f"Part numbering error at position {index}: "
                f"found {part.get('part')!r}."
            )

        if actual_id and actual_id != expected_id:
            errors.append(
                f"Part ID mismatch: expected {expected_id}; "
                f"found {actual_id}."
            )

        numbers.append(number)

    if numbers != list(range(1, len(parts) + 1)):
        errors.append(
            "Part numbers must be unique, sequential and start at 1."
        )

    return numbers, errors


def validate_part_scene_records(
    parts: list[dict],
    expected_scenes: int,
) -> tuple[list[dict], list[str]]:
    results = []
    errors = []
    global_scene_number = 0

    for part_index, part in enumerate(parts, 1):
        raw_scenes = part.get("scenes")

        if not isinstance(raw_scenes, list):
            errors.append(f"Part {part_index} has no scenes list.")
            raw_scenes = []

        scenes = [
            item for item in raw_scenes
            if isinstance(item, dict)
        ]

        if len(scenes) != len(raw_scenes):
            errors.append(
                f"Part {part_index} contains non-object scene records."
            )

        if len(scenes) != expected_scenes:
            errors.append(
                f"Part {part_index}: expected {expected_scenes} Main scenes; "
                f"found {len(scenes)}."
            )

        seen_scene_numbers = set()
        scene_word_total = 0
        missing_narration = []
        scene_ids = []

        for scene_index, scene in enumerate(scenes, 1):
            global_scene_number += 1

            scene_number = as_int(
                scene.get("scene", scene.get("local_scene")),
                -1,
            )

            if scene_number != scene_index:
                errors.append(
                    f"Part {part_index}: expected scene {scene_index}; "
                    f"found {scene_number}."
                )

            if scene_number in seen_scene_numbers:
                errors.append(
                    f"Part {part_index}: duplicate scene number "
                    f"{scene_number}."
                )

            seen_scene_numbers.add(scene_number)

            expected_id = (
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )
            actual_id = clean_text(scene.get("scene_id"))

            if actual_id != expected_id:
                errors.append(
                    f"Scene ID mismatch: expected {expected_id}; "
                    f"found {actual_id or '(missing)'}."
                )

            scene_ids.append(actual_id)

            declared_part = scene.get("part")

            if declared_part is not None:
                if as_int(declared_part, -1) != part_index:
                    errors.append(
                        f"{expected_id}: scene part metadata mismatch."
                    )

            declared_global = scene.get("global_scene_number")

            if declared_global is not None:
                if as_int(declared_global, -1) != global_scene_number:
                    errors.append(
                        f"{expected_id}: global scene number mismatch."
                    )

            narration = extract_scene_text(scene)

            if not narration:
                missing_narration.append(scene_index)
            else:
                scene_word_total += word_count(narration)

        if missing_narration:
            errors.append(
                f"Part {part_index}: narration missing in scenes "
                f"{missing_narration}."
            )

        part_text = extract_part_text(part)
        part_word_total = word_count(part_text)

        if part_word_total <= 0:
            errors.append(f"Part {part_index} narration is empty.")

        if scene_word_total and part_word_total != scene_word_total:
            errors.append(
                f"Part {part_index}: part narration has "
                f"{part_word_total} words but its scene narration "
                f"has {scene_word_total} words."
            )

        if scenes and not missing_narration:
            combined_scene_text = " ".join(
                extract_scene_text(scene) for scene in scenes
            )

            if normalize_narration(combined_scene_text) != part_text:
                errors.append(
                    f"Part {part_index}: scene narration does not "
                    "exactly match the ordered Part narration."
                )

        declared_words = part.get("word_count")

        if declared_words is not None:
            if as_int(declared_words, -1) != part_word_total:
                errors.append(
                    f"Part {part_index}: word_count metadata mismatch."
                )

        results.append({
            "part_number": part_index,
            "part_id": clean_text(part.get("part_id"))
            or f"part_{part_index:02d}",
            "scene_count": len(scenes),
            "expected_scene_count": expected_scenes,
            "narration_words": part_word_total,
            "scene_narration_words": scene_word_total,
            "missing_narration_scenes": missing_narration,
            "scene_ids": scene_ids,
            "structure_valid": (
                len(scenes) == expected_scenes
                and not missing_narration
                and len(seen_scene_numbers) == expected_scenes
            ),
        })

    return results, errors


def validate_flat_scene_index(
    story: dict,
    parts: list[dict],
    expected_scenes: int,
) -> list[str]:
    errors = []
    flat_scenes = story.get("scenes")

    if not isinstance(flat_scenes, list):
        errors.append("story.json has no flat scenes list.")
        return errors

    expected_total = len(parts) * expected_scenes

    if len(flat_scenes) != expected_total:
        errors.append(
            f"Flat scene count mismatch: expected {expected_total}; "
            f"found {len(flat_scenes)}."
        )

    expected_ids = []

    for part_index in range(1, len(parts) + 1):
        for scene_index in range(1, expected_scenes + 1):
            expected_ids.append(
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )

    actual_ids = []

    for index, scene in enumerate(flat_scenes, 1):
        if not isinstance(scene, dict):
            errors.append(f"Flat scene record {index} is invalid.")
            continue

        scene_id = clean_text(scene.get("scene_id"))
        actual_ids.append(scene_id)

        expected_part = ((index - 1) // expected_scenes) + 1
        expected_local_scene = ((index - 1) % expected_scenes) + 1

        if scene_id != expected_ids[index - 1]:
            errors.append(
                f"Flat scene order mismatch at index {index}: "
                f"expected {expected_ids[index - 1]}; "
                f"found {scene_id or '(missing)'}."
            )

        if as_int(scene.get("part"), -1) != expected_part:
            errors.append(
                f"{scene_id}: flat scene part number mismatch."
            )

        if as_int(scene.get("scene"), -1) != expected_local_scene:
            errors.append(
                f"{scene_id}: flat scene local number mismatch."
            )

    if len(actual_ids) != len(set(actual_ids)):
        errors.append("Flat scenes contain duplicate scene IDs.")

    return errors


def validate_story_metadata(
    story: dict,
    parts: list[dict],
    scenes_per_part: int,
    total_words: int,
) -> list[str]:
    errors = []

    expected_parts = len(parts)
    expected_total_scenes = expected_parts * scenes_per_part

    metadata_scenes = story.get("total_scenes")

    if metadata_scenes is not None:
        if as_int(metadata_scenes, -1) != expected_total_scenes:
            errors.append(
                f"Story total_scenes mismatch: expected "
                f"{expected_total_scenes}; found {metadata_scenes}."
            )

    declared_scenes_per_part = story.get("scenes_per_part")

    if declared_scenes_per_part is not None:
        if as_int(declared_scenes_per_part, -1) != scenes_per_part:
            errors.append(
                "Story scenes_per_part does not match Input/topic.txt."
            )

    declared_words = story.get("total_story_words")

    if declared_words is not None:
        if as_int(declared_words, -1) != total_words:
            errors.append(
                f"Story total_story_words mismatch: declared "
                f"{declared_words}; actual {total_words}."
            )

    source_count = story.get("source_word_count")

    if source_count is not None:
        if as_int(source_count, -1) < 0:
            errors.append("Story source_word_count metadata is invalid.")

    if not clean_text(story.get("title")):
        errors.append("Generated story title is missing.")

    return errors


def validate_story_text_consistency(
    story: dict,
    parts: list[dict],
    story_text: str,
) -> list[str]:
    errors = []

    part_texts = [extract_part_text(part) for part in parts]

    if any(not item for item in part_texts):
        return errors

    combined_parts = normalize_narration(" ".join(part_texts))

    if normalize_narration(story_text) != combined_parts:
        errors.append(
            "Complete story_text does not match the ordered Part narration."
        )

    flattened = []

    for part in parts:
        scenes = part.get("scenes", [])

        if isinstance(scenes, list):
            for scene in scenes:
                if isinstance(scene, dict):
                    text = extract_scene_text(scene)
                    if text:
                        flattened.append(text)

    if flattened:
        combined_scenes = normalize_narration(" ".join(flattened))

        if combined_scenes != combined_parts:
            errors.append(
                "Flattened scene narration does not match complete "
                "Part narration."
            )

    return errors


def validate_natural_part_boundaries(
    parts: list[dict],
) -> tuple[list[dict], list[str]]:
    """Reject arbitrary mid-thought Part endings without rewriting text."""
    results = []
    errors = []

    for index, part in enumerate(parts, 1):
        narration = extract_part_text(part).rstrip()

        if not narration:
            results.append({
                "part_number": index,
                "boundary_valid": False,
                "ending": "",
            })
            continue

        ending = narration[-1]
        valid = ending in BOUNDARY_ENDINGS

        if not valid:
            errors.append(
                f"Part {index} ends without a recognized sentence or "
                "natural clause boundary. Arbitrary mid-sentence splits "
                "are not accepted."
            )

        results.append({
            "part_number": index,
            "boundary_valid": valid,
            "ending": ending,
            "boundary_type": (
                "sentence_or_clause"
                if valid
                else "unrecognized_or_incomplete"
            ),
        })

    return results, errors


def validate_part_word_counts(
    parts: list[dict],
    format_type: str,
    policy: dict,
) -> list[dict]:
    results = []

    for index, part in enumerate(parts, 1):
        narration = extract_part_text(part)
        amount = word_count(narration)

        revision_attempted = bool(
            part.get("revision_attempted")
            or part.get("word_count_revision_attempted")
            or part.get("revision", {}).get("attempted")
            if isinstance(part.get("revision", {}), dict)
            else part.get("revision_attempted")
        )

        result = validate_story_length(
            narration,
            policy,
            part_number=index,
            revision_attempted=revision_attempted,
        )

        if format_type == "short":
            minimum = SHORT_MIN_WORDS
            target = SHORT_TARGET_WORDS
            preferred_maximum = SHORT_PREFERRED_MAX_WORDS
            hard_maximum = SHORT_HARD_MAX_WORDS
        else:
            target = as_int(
                policy.get("target_words_per_part"),
                125,
            )
            minimum = max(1, round(target * 100 / 125))
            hard_maximum = max(
                minimum,
                round(target * 145 / 125),
            )
            preferred_maximum = hard_maximum

        results.append({
            "part_number": index,
            "word_count": amount,
            "target_words": target,
            "minimum_words": minimum,
            "preferred_maximum_words": preferred_maximum,
            "hard_maximum_words": hard_maximum,
            "status": result.get("status"),
            "passed": bool(result.get("passed")),
            "needs_revision": bool(result.get("needs_revision")),
            "revision_attempted": revision_attempted,
        })

    return results


def build_policy(
    format_type: str,
    parts_specification: str,
    selected_parts: int,
    scenes_per_part: int,
    story_length: Any,
    scene_duration: Any,
    total_words: int,
) -> dict:
    try:
        policy = build_length_policy(
            format_type=format_type,
            story_length=story_length,
            parts=parts_specification,
            scenes_per_part=scenes_per_part,
            scene_duration=scene_duration,
            source_word_count=total_words,
            selected_parts=selected_parts,
        )
    except (TypeError, ValueError) as exc:
        fail(f"Cannot build story length policy: {exc}")

    if format_type == "short":
        policy.update({
            "parts_count": selected_parts,
            "configured_parts_count": selected_parts,
            "selected_parts": selected_parts,
            "parts_count_is_dynamic": (
                parse_parts_range(parts_specification)[0]
                != parse_parts_range(parts_specification)[1]
            ),
            "total_scenes": selected_parts * scenes_per_part,
            "target_words_per_part": SHORT_TARGET_WORDS,
            "minimum_words_per_part": SHORT_MIN_WORDS,
            "preferred_maximum_words_per_part": SHORT_PREFERRED_MAX_WORDS,
            "maximum_words_per_part": SHORT_HARD_MAX_WORDS,
            "hard_maximum_words_per_part": SHORT_HARD_MAX_WORDS,
            "target_total_words": selected_parts * SHORT_TARGET_WORDS,
            "minimum_total_words": selected_parts * SHORT_MIN_WORDS,
            "preferred_maximum_total_words": (
                selected_parts * SHORT_PREFERRED_MAX_WORDS
            ),
            "maximum_total_words": selected_parts * SHORT_HARD_MAX_WORDS,
            "source_word_count_within_hard_limit": (
                selected_parts * SHORT_MIN_WORDS
                <= total_words
                <= selected_parts * SHORT_HARD_MAX_WORDS
            ),
            "revise_parts_above_150": True,
            "hard_fail_below_120": True,
            "hard_fail_above_180": True,
            "api_calls_made": 0,
        })

    else:
        policy["parts_count"] = selected_parts
        policy["configured_parts_count"] = selected_parts
        policy["selected_parts"] = selected_parts
        policy["total_scenes"] = selected_parts * scenes_per_part

    policy["configured_parts_specification"] = parts_specification
    policy["selected_parts_are_authoritative"] = True
    policy["story_word_count"] = total_words

    return policy


def source_comparison(story: dict, generated_text: str) -> dict:
    source_text = story.get("source_text", "")

    if not isinstance(source_text, str):
        source_text = ""

    source_text = normalize_narration(source_text)
    generated_text = normalize_narration(generated_text)

    source_words = word_count(source_text)
    generated_words = word_count(generated_text)

    ratio = (
        round(generated_words / source_words, 4)
        if source_words
        else None
    )

    warnings = []

    if not source_words:
        warnings.append(
            "No source_text field is available for source comparison."
        )
    elif generated_words < source_words:
        warnings.append(
            "Generated narration is shorter than supplied source text. "
            "Length comparison alone cannot establish whether all story "
            "events and meaning were preserved."
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
        fail("Input/topic.txt is missing.")

    config = load_and_validate(str(input_file))

    format_type = normalize_format(get_format(config))

    try:
        minimum_parts, maximum_parts = get_parts_range(config)
    except (TypeError, ValueError) as exc:
        fail(f"Invalid PARTS setting: {exc}")

    scenes_per_part = as_int(get_scenes(config), 0)

    if scenes_per_part < 1:
        fail("SCENES must be a positive integer.")

    if (
        maximum_parts > MAX_PARTS
        or scenes_per_part > MAX_SCENES_PER_PART
    ):
        fail("PARTS or SCENES exceeds the supported safety limit.")

    story = read_json(STORY_FILE)

    if not isinstance(story, dict):
        fail("output/story/story.json is missing or invalid.")

    if story.get("status") != "completed":
        fail("story.json status is not 'completed'.")

    story_format = normalize_format(story.get("format"))

    if story_format != format_type:
        fail(
            f"FORMAT mismatch: Input={format_type}; "
            f"story.json={story_format}."
        )

    parts = get_story_parts(story)
    actual_parts = len(parts)

    _, _, part_selection_errors = validate_part_selection(
        config,
        story,
        actual_parts,
    )

    numbering, numbering_errors = validate_part_numbering(parts)

    story_text = get_story_text(story, parts)
    total_words = word_count(story_text)

    if total_words <= 0:
        fail("Generated story contains no narration text.")

    if total_words > MAX_STORY_WORDS:
        fail("Generated story exceeds the maximum supported word count.")

    scenes_metadata = as_int(
        story.get("scenes_per_part"),
        scenes_per_part,
    )

    if scenes_metadata != scenes_per_part:
        part_selection_errors.append(
            f"SCENES mismatch: Input={scenes_per_part}; "
            f"story.json={scenes_metadata}."
        )

    parts_specification = (
        f"{minimum_parts}-{maximum_parts}"
        if minimum_parts != maximum_parts
        else str(minimum_parts)
    )

    policy = build_policy(
        format_type=format_type,
        parts_specification=parts_specification,
        selected_parts=actual_parts,
        scenes_per_part=scenes_per_part,
        story_length=config.get("STORY_LENGTH", "auto"),
        scene_duration=config.get("SCENE_DURATION", "auto"),
        total_words=total_words,
    )

    structure_results, scene_errors = validate_part_scene_records(
        parts,
        scenes_per_part,
    )

    flat_scene_errors = validate_flat_scene_index(
        story,
        parts,
        scenes_per_part,
    )

    metadata_errors = validate_story_metadata(
        story,
        parts,
        scenes_per_part,
        total_words,
    )

    consistency_errors = validate_story_text_consistency(
        story,
        parts,
        story_text,
    )

    boundary_results, boundary_errors = validate_natural_part_boundaries(
        parts,
    )

    word_results = validate_part_word_counts(
        parts,
        format_type,
        policy,
    )

    failed_parts = [
        row for row in word_results
        if row["needs_revision"] or not row["passed"]
    ]

    source_report = source_comparison(story, story_text)

    structure_errors = (
        part_selection_errors
        + numbering_errors
        + scene_errors
        + flat_scene_errors
        + metadata_errors
        + consistency_errors
        + boundary_errors
    )

    warnings = list(policy.get("warnings", []))
    warnings.extend(source_report["warnings"])

    all_errors = list(structure_errors)

    for row in failed_parts:
        all_errors.append(
            f"Part {row['part_number']} has {row['word_count']} words; "
            f"status={row['status']}."
        )

    valid = not all_errors

    expected_scenes = actual_parts * scenes_per_part
    actual_scenes = sum(
        len(part.get("scenes", []))
        for part in parts
        if isinstance(part.get("scenes"), list)
    )

    fingerprint = story_fingerprint(story)

    report = {
        "report_version": REPORT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "validation_source": "output/story/story.json",
        "validation_mode": "read_only",
        "story_fingerprint": fingerprint,
        "policy": policy,
        "source_comparison": source_report,
        "summary": {
            "configured_parts_min": minimum_parts,
            "configured_parts_max": maximum_parts,
            "configured_parts": (
                minimum_parts
                if minimum_parts == maximum_parts
                else None
            ),
            "selected_parts": actual_parts,
            "parts_expected": actual_parts,
            "parts_found": len(parts),
            "scenes_per_part": scenes_per_part,
            "scenes_expected": expected_scenes,
            "scenes_found": actual_scenes,
            "total_words": total_words,
            "parts_needing_revision": len(failed_parts),
            "structure_errors": all_errors,
            "warnings": warnings,
            "valid": valid,
            "api_calls_made": 0,
            "story_text_modified": False,
            "natural_part_boundaries_valid": not boundary_errors,
        },
        "part_numbering": numbering,
        "structure_results": structure_results,
        "part_results": word_results,
        "boundary_results": boundary_results,
    }

    # Write the report even when validation fails, so the workflow can
    # show the exact failure rather than treating incomplete output as valid.
    write_json(POLICY_FILE, policy)
    write_json(REPORT_FILE, report)

    log("=" * 60)
    log("STORY LENGTH VALIDATION")
    log("FORMAT:", format_type)
    log("CONFIGURED PARTS RANGE:", parts_specification)
    log("SELECTED PARTS:", actual_parts)
    log("PARTS FOUND:", len(parts))
    log("SCENES PER PART:", scenes_per_part)
    log("SCENES FOUND:", actual_scenes)
    log("EXPECTED SCENES:", expected_scenes)
    log("TOTAL WORDS:", total_words)

    if format_type == "short":
        log(
            "SHORT WORDS PER PART:",
            f"{SHORT_MIN_WORDS}-{SHORT_PREFERRED_MAX_WORDS}",
            "(hard maximum:",
            SHORT_HARD_MAX_WORDS,
            ")",
        )

    log("PARTS NEEDING REVISION:", len(failed_parts))
    log("NATURAL BOUNDARIES VALID:", not boundary_errors)
    log("VALID:", valid)

    for error in all_errors:
        log("ERROR:", error)

    for warning in warnings:
        log("WARNING:", warning)

    for row in word_results:
        log(
            "PART:",
            row["part_number"],
            "WORDS:",
            row["word_count"],
            "STATUS:",
            row["status"],
        )

    log("API CALLS: 0")
    log("STORY MODIFIED: false")
    log("REPORT:", REPORT_FILE.relative_to(ROOT))
    log("=" * 60)

    # Preserve the workflow contract: this stage writes a diagnostic
    # report. The workflow's summary.valid check is the hard gate.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("STORY LENGTH VALIDATION FAILED:", str(exc))
        sys.exit(1)