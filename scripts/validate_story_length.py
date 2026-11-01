
#!/usr/bin/env python3
"""Validate generated story length, part structure and source coverage."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes
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

REPORT_VERSION = "story-length-validation-v3-source-aware"


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
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def as_int(value: Any, default: int = 0) -> int:
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


def get_parts(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []
    parts = data.get("parts")
    if not isinstance(parts, list):
        return []
    return [part for part in parts if isinstance(part, dict)]


def extract_scene_text(scene: dict) -> str:
    """Read narration fields without counting visual descriptions."""
    for key in ("narration", "narration_text", "voiceover", "text"):
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
            return "\n".join(rows)

    for key in ("narration", "story", "text", "content"):
        value = part.get(key)
        if isinstance(value, str) and clean_text(value):
            return clean_text(value)
    return ""


def extract_story_text(story: dict, parts: list[dict]) -> str:
    value = story.get("story_text")
    if isinstance(value, str) and clean_text(value):
        return clean_text(value)
    return "\n\n".join(
        extract_part_text(part) for part in parts
        if extract_part_text(part)
    )


def validate_part_structure(
    parts: list[dict],
    expected_parts: int,
    expected_scenes: int,
) -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    errors: list[str] = []
    seen_parts: list[int] = []

    if len(parts) != expected_parts:
        errors.append(
            f"Expected {expected_parts} parts; found {len(parts)}."
        )

    for index, part in enumerate(parts, 1):
        number = as_int(part.get("part"), index)
        seen_parts.append(number)

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

        numbers = []
        for scene_index, scene in enumerate(scenes, 1):
            numbers.append(as_int(
                scene.get("scene", scene.get("local_scene")),
                scene_index,
            ))

        expected_numbers = list(range(1, expected_scenes + 1))
        if numbers and sorted(numbers) != expected_numbers:
            errors.append(
                f"Part {number}: scene numbers are not "
                f"1 through {expected_scenes}."
            )

        narration_words = word_count(extract_part_text(part))
        results.append({
            "part_number": number,
            "scene_count": len(scenes),
            "expected_scene_count": expected_scenes,
            "narration_words": narration_words,
            "structure_valid": (
                isinstance(raw_scenes, list)
                and len(scenes) == expected_scenes
                and sorted(numbers) == expected_numbers
            ),
        })

    if sorted(seen_parts) != list(range(1, expected_parts + 1)):
        errors.append(
            "Part numbers must be unique and sequential from 1 "
            f"through {expected_parts}; found {seen_parts}."
        )

    return results, errors


def validate_word_counts(
    parts: list[dict],
    policy: dict,
) -> list[dict]:
    results = []

    minimum = as_int(policy.get("minimum_words_per_part"))
    maximum = as_int(policy.get("maximum_words_per_part"))
    target = as_int(policy.get("target_words_per_part"))
    hard_maximum = as_int(
        policy.get("hard_maximum_words_per_part"),
        maximum,
    )

    for index, part in enumerate(parts, 1):
        number = as_int(part.get("part"), index)
        amount = word_count(extract_part_text(part))

        if amount == 0:
            status = "missing_narration"
        elif minimum and amount < minimum:
            status = "too_short"
        elif hard_maximum and amount > hard_maximum:
            status = "too_long"
        elif maximum and amount > maximum:
            status = "within_hard_limit_exception"
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
                "missing_narration", "too_short", "too_long"
            },
        })

    return results


def main() -> int:
    input_file = ROOT / "Input" / "topic.txt"
    if not input_file.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(input_file)
    configured_format = normalize_format(get_format(config))

    if configured_format not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {configured_format!r}"
        )

    configured_parts = max(1, as_int(get_parts(config), 1))
    configured_scenes = max(1, as_int(get_scenes(config), 1))

    # story.json is the authoritative output of generate_story.py.
    # ai_story.json is created later in the workflow and may be stale
    # when this validation stage runs.
    story = read_json(STORY_FILE)
    if not isinstance(story, dict):
        raise RuntimeError(
            "output/story/story.json is missing or is not a JSON object."
        )

    if story.get("status") != "completed":
        raise RuntimeError(
            "story.json status is not 'completed'."
        )

    actual_format = normalize_format(story.get("format"))
    if actual_format != configured_format:
        raise RuntimeError(
            f"FORMAT mismatch: Input={configured_format}, "
            f"story.json={actual_format}."
        )

    parts = get_parts(story)
    if not parts:
        raise RuntimeError("story.json contains no valid story parts.")

    story_text = extract_story_text(story, parts)
    total_words = word_count(story_text)
    if total_words <= 0:
        raise RuntimeError("Generated story contains no narration text.")

    actual_parts = len(parts)
    scenes_per_part = as_int(
        story.get("scenes_per_part"),
        configured_scenes,
    )
    if scenes_per_part <= 0:
        raise RuntimeError("Invalid scenes_per_part in story.json.")

    metadata_parts = as_int(
        story.get("total_parts", story.get("parts_count")),
        actual_parts,
    )
    metadata_scenes = as_int(
        story.get("total_scenes"),
        actual_parts * scenes_per_part,
    )

    metadata_errors = []
    if metadata_parts != actual_parts:
        metadata_errors.append(
            f"Story metadata says {metadata_parts} parts, "
            f"but {actual_parts} parts were found."
        )

    actual_scene_total = sum(
        len([
            scene for scene in part.get("scenes", [])
            if isinstance(scene, dict)
        ])
        for part in parts
        if isinstance(part.get("scenes"), list)
    )

    if metadata_scenes != actual_scene_total:
        metadata_errors.append(
            f"Story metadata says {metadata_scenes} scenes, "
            f"but {actual_scene_total} were found."
        )

    # SHORT parts are calculated from the actual completed story.
    # Do not compare them with configured PARTS: the short pipeline
    # intentionally calculates a dynamic count.
    policy = build_length_policy(
        format_type=configured_format,
        story_length=config.get("STORY_LENGTH", "auto"),
        parts=configured_parts,
        scenes_per_part=scenes_per_part,
        scene_duration=config.get("SCENE_DURATION", "auto"),
        source_word_count=total_words,
    )

    expected_parts = (
        as_int(policy.get("parts_count"), actual_parts)
        if configured_format == "short"
        else configured_parts
    )

    structure_results, structure_errors = validate_part_structure(
        parts,
        expected_parts,
        scenes_per_part,
    )

    word_results = validate_word_counts(parts, policy)

    # For SHORT, 120–150 words is the target range. 151–180 is
    # reported separately as a sentence-boundary exception.
    if configured_format == "short":
        for row in word_results:
            amount = row["word_count"]
            if 150 < amount <= 180:
                row["status"] = "within_hard_limit_exception"
                row["needs_revision"] = False
            elif amount > 180:
                row["status"] = "too_long"
                row["needs_revision"] = True

    source_text = clean_text(story.get("source_text", ""))
    source_words = word_count(source_text)
    generated_words = word_count(story_text)

    source_coverage = None
    if source_words:
        source_coverage = round(
            min(generated_words, source_words) / source_words,
            4,
        )

    warnings = []
    if source_words and generated_words < source_words:
        warnings.append(
            "Generated narration is shorter than the supplied source. "
            "Review source preservation before publishing."
        )

    if configured_format == "short":
        if actual_parts != expected_parts:
            warnings.append(
                "Dynamic SHORT part count differs from the expected "
                "count calculated from generated narration."
            )
        if configured_parts != actual_parts:
            warnings.append(
                f"Input PARTS={configured_parts} is a configured value; "
                f"the generated SHORT contains {actual_parts} dynamic parts."
            )

    if STORY_FILE.is_file() and AI_STORY_FILE.is_file():
        warnings.append(
            "ai_story.json exists, but was not used: this validation "
            "stage runs before the Generate AI story workflow step."
        )

    failed_parts = [
        row for row in word_results if row["needs_revision"]
    ]

    all_structure_errors = structure_errors + metadata_errors
    valid = (
        not all_structure_errors
        and not failed_parts
    )

    report = {
        "report_version": REPORT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": configured_format,
        "validation_source": "story.json",
        "policy": policy,
        "source_comparison": {
            "source_word_count": source_words,
            "generated_story_word_count": generated_words,
            "generated_to_source_ratio": source_coverage,
            "source_text_modified_by_validator": False,
            "warning": (
                "Word-count difference is diagnostic; it does not "
                "prove that story events or meaning were preserved."
                if source_words else
                "No source_text field was available for comparison."
            ),
        },
        "summary": {
            "configured_parts": configured_parts,
            "parts_expected": expected_parts,
            "parts_found": actual_parts,
            "scenes_per_part": scenes_per_part,
            "scenes_found": actual_scene_total,
            "scenes_expected": expected_parts * scenes_per_part,
            "total_words": total_words,
            "parts_needing_revision": len(failed_parts),
            "structure_errors": all_structure_errors,
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
    log("SOURCE WORDS:", source_words)
    log("GENERATED WORDS:", generated_words)
    log("PARTS:", actual_parts, "/", expected_parts)
    log("SCENES:", actual_scene_total, "/", expected_parts * scenes_per_part)
    log(
        "WORDS PER PART:",
        policy.get("minimum_words_per_part"),
        "-",
        policy.get("maximum_words_per_part"),
    )
    log("PARTS NEEDING REVISION:", len(failed_parts))
    log("STRUCTURE ERRORS:", len(all_structure_errors))

    for error in all_structure_errors:
        log("STRUCTURE ERROR:", error)

    for warning in warnings:
        log("WARNING:", warning)

    for row in failed_parts:
        log(
            "WORD COUNT:",
            f"part={row['part_number']}",
            f"words={row['word_count']}",
            f"status={row['status']}",
        )

    log("VALID:", valid)
    log("API CALLS: 0")
    log("STORY MODIFIED: false")
    log("REPORT:", REPORT_FILE.relative_to(ROOT))

    # This stage is diagnostic. Story verification and downstream
    # final-output validation remain responsible for stopping a run.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("STORY LENGTH VALIDATION FAILED:", str(exc))
        sys.exit(1)
