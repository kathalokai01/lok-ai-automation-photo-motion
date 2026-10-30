#!/usr/bin/env python3
"""Validate story word counts and structure against Input/topic.txt."""

from __future__ import annotations

import json
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
INPUT_STORY = STORY_DIR / "story.json"
AI_STORY = STORY_DIR / "ai_story.json"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
REPORT_FILE = STORY_DIR / "story_length_validation.json"

REPORT_VERSION = "story-length-validation-v2-strict-structure"


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


def parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_story_parts(data: Any, label: str) -> list[dict]:
    if not isinstance(data, dict):
        return []

    parts = data.get("parts")
    if not isinstance(parts, list):
        log(f"WARNING: {label} has no parts list.")
        return []

    return [part for part in parts if isinstance(part, dict)]


def extract_part_text(part: dict) -> str:
    """Prefer detailed scene narration; avoid counting summaries twice."""
    scenes = part.get("scenes")

    if isinstance(scenes, list):
        scene_text = []

        for scene in scenes:
            if not isinstance(scene, dict):
                continue

            narration = clean_text(
                scene.get("narration")
                or scene.get("narration_text")
                or scene.get("text")
                or ""
            )
            dialogue = clean_text(scene.get("dialogue") or "")

            combined = " ".join(
                value for value in (narration, dialogue) if value
            )

            if combined:
                scene_text.append(combined)

        if scene_text:
            return "\n".join(scene_text)

    for key in ("narration", "story", "text", "content"):
        value = part.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def validate_part_structure(
    parts: list[dict],
    expected_parts: int,
    expected_scenes: int,
    label: str,
) -> tuple[list[dict], list[str]]:
    results = []
    errors = []
    seen_parts = set()

    if len(parts) != expected_parts:
        errors.append(
            f"{label}: expected {expected_parts} parts; found {len(parts)}."
        )

    for index, part in enumerate(parts, 1):
        number = parse_int(part.get("part"), index)

        if number in seen_parts:
            errors.append(f"{label}: duplicate part number {number}.")
        seen_parts.add(number)

        scenes = part.get("scenes")
        valid_scenes = (
            [item for item in scenes if isinstance(item, dict)]
            if isinstance(scenes, list)
            else []
        )

        if not isinstance(scenes, list):
            errors.append(f"{label}: part {number} has no scenes list.")
        elif len(valid_scenes) != expected_scenes:
            errors.append(
                f"{label}: part {number} expected {expected_scenes} "
                f"scenes; found {len(valid_scenes)}."
            )

        scene_numbers = []
        for scene_index, scene in enumerate(valid_scenes, 1):
            scene_number = parse_int(
                scene.get("scene", scene.get("local_scene")),
                scene_index,
            )
            scene_numbers.append(scene_number)

        if scene_numbers:
            expected_numbers = list(range(1, expected_scenes + 1))
            if sorted(scene_numbers) != expected_numbers:
                errors.append(
                    f"{label}: part {number} scene numbering is invalid."
                )

        results.append({
            "part_number": number,
            "scene_count": len(valid_scenes),
            "expected_scene_count": expected_scenes,
            "structure_valid": (
                isinstance(scenes, list)
                and len(valid_scenes) == expected_scenes
                and sorted(scene_numbers)
                == list(range(1, expected_scenes + 1))
            ),
        })

    if seen_parts != set(range(1, expected_parts + 1)):
        errors.append(
            f"{label}: part numbers must be exactly "
            f"1 through {expected_parts}; found {sorted(seen_parts)}."
        )

    return results, errors


def validate_word_counts(
    parts: list[dict],
    policy: dict,
    label: str,
) -> list[dict]:
    results = []

    minimum = int(policy.get("minimum_words_per_part") or 0)
    maximum = int(policy.get("maximum_words_per_part") or 0)
    target = int(policy.get("target_words_per_part") or 0)

    for index, part in enumerate(parts, 1):
        number = parse_int(part.get("part"), index)
        narration = extract_part_text(part)
        count = word_count(narration)

        if count == 0:
            status = "missing_narration"
        elif count < minimum:
            status = "too_short"
        elif count > maximum:
            status = "too_long"
        else:
            status = "within_target"

        results.append({
            "source": label,
            "part_number": number,
            "word_count": count,
            "target_words": target,
            "minimum_words": minimum,
            "maximum_words": maximum,
            "status": status,
            "needs_revision": status != "within_target",
        })

    return results


def main() -> int:
    input_file = ROOT / "Input" / "topic.txt"

    if not input_file.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(input_file)

    format_type = str(get_format(config)).strip().lower()
    parts_requested = int(get_parts(config))
    scenes_requested = int(get_scenes(config))

    if format_type not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {format_type}")

    if parts_requested < 1 or scenes_requested < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")

    ai_story = read_json(AI_STORY)
    source_story = read_json(INPUT_STORY)

    ai_parts = get_story_parts(ai_story, "ai_story.json")
    source_parts = get_story_parts(source_story, "story.json")

    if ai_parts:
        selected_parts = ai_parts
        selected_source = "ai_story.json"
    else:
        selected_parts = source_parts
        selected_source = "story.json"

    if not selected_parts:
        raise RuntimeError(
            "No story parts found in ai_story.json or story.json."
        )

    total_words = sum(
        word_count(extract_part_text(part))
        for part in selected_parts
    )

    policy = build_length_policy(
        format_type=format_type,
        story_length=config.get("STORY_LENGTH", "auto"),
        parts=parts_requested,
        scenes_per_part=scenes_requested,
        scene_duration=config.get("SCENE_DURATION", "auto"),
        source_word_count=total_words,
    )

    structure_results, structure_errors = validate_part_structure(
        selected_parts,
        parts_requested,
        scenes_requested,
        selected_source,
    )

    word_results = validate_word_counts(
        selected_parts,
        policy,
        selected_source,
    )

    failed_parts = [
        row for row in word_results
        if row["needs_revision"]
    ]

    actual_scenes = sum(
        row["scene_count"] for row in structure_results
    )

    report = {
        "report_version": REPORT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "selected_story_source": selected_source,
        "policy": policy,
        "summary": {
            "parts_requested": parts_requested,
            "parts_found": len(selected_parts),
            "scenes_per_part_requested": scenes_requested,
            "scenes_found": actual_scenes,
            "scenes_expected": parts_requested * scenes_requested,
            "total_words": total_words,
            "parts_needing_revision": len(failed_parts),
            "structure_errors": structure_errors,
            "valid": not failed_parts and not structure_errors,
            "api_calls_made": 0,
            "story_text_modified": False,
        },
        "structure_results": structure_results,
        "part_results": word_results,
        "source_story_results": (
            validate_word_counts(
                source_parts,
                policy,
                "story.json",
            )
            if source_parts and selected_source != "story.json"
            else []
        ),
    }

    write_json(POLICY_FILE, policy)
    write_json(REPORT_FILE, report)

    log("STORY LENGTH VALIDATION")
    log("FORMAT:", format_type)
    log("SOURCE:", selected_source)
    log("PARTS:", len(selected_parts), "/", parts_requested)
    log("SCENES:", actual_scenes, "/", parts_requested * scenes_requested)
    log("TOTAL WORDS:", total_words)
    log("WORD COUNT RANGE:",
        policy.get("minimum_words_per_part"),
        "-",
        policy.get("maximum_words_per_part"))
    log("PARTS NEEDING REVISION:", len(failed_parts))
    log("STRUCTURE ERRORS:", len(structure_errors))
    log("REPORT:", REPORT_FILE.relative_to(ROOT))

    for error in structure_errors:
        log("STRUCTURE ERROR:", error)

    for row in failed_parts:
        log(
            "WORD COUNT:",
            f"part={row['part_number']}",
            f"words={row['word_count']}",
            f"allowed={row['minimum_words']}-{row['maximum_words']}",
            f"status={row['status']}",
        )

    log("VALID:", report["summary"]["valid"])
    log("API CALLS: 0")
    log("STORY MODIFIED: false")

    # Diagnostic only: story generation/revision is handled by its own stage.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("STORY LENGTH VALIDATION FAILED:", str(exc))
        sys.exit(1)