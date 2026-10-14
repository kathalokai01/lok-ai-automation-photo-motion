#!/usr/bin/env python3
"""Validate story word counts against the shared Katha Lok AI length policy."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

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

REPORT_VERSION = "story-length-validation-v1"


def log(message: str) -> None:
    print(message, flush=True)


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_source_config() -> dict:
    config = load_and_validate()
    if not isinstance(config, dict):
        raise RuntimeError("Input/topic.txt did not produce a valid configuration.")
    return config


def get_story_parts(data: Any, label: str) -> list[dict]:
    if not isinstance(data, dict):
        return []

    parts = data.get("parts")
    if not isinstance(parts, list):
        log(f"WARNING: {label} does not contain a parts list.")
        return []

    return [part for part in parts if isinstance(part, dict)]


def extract_part_text(part: dict) -> str:
    """Collect narration and dialogue while avoiding duplicate scene text."""
    chunks: list[str] = []

    for key in ("narration", "story", "text", "summary"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
            break

    scenes = part.get("scenes", [])
    if isinstance(scenes, list):
        scene_chunks: list[str] = []

        for scene in scenes:
            if not isinstance(scene, dict):
                continue

            narration = clean_text(scene.get("narration", ""))
            dialogue = clean_text(scene.get("dialogue", ""))

            combined = " ".join(
                item for item in (narration, dialogue) if item
            )
            if combined:
                scene_chunks.append(combined)

        # Prefer scene narration when present, avoiding double-counting
        # a part-level synopsis and the detailed scene narration.
        if scene_chunks:
            return "\n".join(scene_chunks)

    return "\n".join(chunks)


def scene_count(parts: list[dict]) -> int:
    total = 0

    for part in parts:
        scenes = part.get("scenes", [])
        if isinstance(scenes, list):
            total += len(scenes)

    return total


def validate_parts(
    parts: list[dict],
    policy: dict,
    label: str,
) -> list[dict]:
    expected_parts = parse_int(policy.get("parts_count"), 1)
    target_words = max(
        1,
        parse_int(policy.get("target_words_per_part"), 135),
    )

    minimum_rate = max(
        1,
        parse_int(policy.get("words_per_minute_minimum"), 120),
    )
    maximum_rate = max(
        minimum_rate,
        parse_int(policy.get("words_per_minute_maximum"), 150),
    )

    baseline = 135 if policy.get("format") == "short" else 125

    minimum_words = max(
        1,
        round(target_words * minimum_rate / baseline),
    )
    maximum_words = max(
        minimum_words,
        round(target_words * maximum_rate / baseline),
    )

    results: list[dict] = []

    for index, part in enumerate(parts, start=1):
        text = extract_part_text(part)
        count = word_count(text)

        if count == 0:
            status = "missing_narration"
        elif count < minimum_words:
            status = "too_short"
        elif count > maximum_words:
            status = "too_long"
        else:
            status = "within_target"

        scenes = part.get("scenes", [])
        valid_scenes = [
            scene for scene in scenes if isinstance(scene, dict)
        ] if isinstance(scenes, list) else []

        results.append(
            {
                "source": label,
                "part_number": parse_int(part.get("part"), index),
                "word_count": count,
                "target_words": target_words,
                "minimum_words": minimum_words,
                "maximum_words": maximum_words,
                "scene_count": len(valid_scenes),
                "status": status,
                "needs_revision": status != "within_target",
            }
        )

    if len(parts) != expected_parts:
        log(
            f"WARNING: {label} has {len(parts)} parts; "
            f"the input requests {expected_parts}."
        )

    return results


def main() -> int:
    config = get_source_config()

    format_type = get_format(config)
    parts_requested = get_parts(config)
    scenes_requested = get_scenes(config)

    story = read_json(INPUT_STORY)
    ai_story = read_json(AI_STORY)

    source_parts = get_story_parts(story, "story.json")
    ai_parts = get_story_parts(ai_story, "ai_story.json")

    # Prefer the more detailed AI story for primary validation, but also
    # report source-story results when available.
    selected_parts = ai_parts if ai_parts else source_parts
    selected_source = "ai_story.json" if ai_parts else "story.json"

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

    # If a policy file already exists, replace it atomically with the
    # policy calculated from the current input and story.
    write_json(POLICY_FILE, policy)

    selected_results = validate_parts(
        selected_parts,
        policy,
        selected_source,
    )

    source_results = (
        validate_parts(source_parts, policy, "story.json")
        if source_parts and selected_source != "story.json"
        else []
    )

    failed_parts = [
        result for result in selected_results
        if result["needs_revision"]
    ]

    actual_parts = len(selected_parts)
    actual_scenes = scene_count(selected_parts)

    expected_parts = parse_int(policy.get("parts_count"), 1)
    expected_scenes_per_part = parse_int(
        policy.get("scenes_per_part"), 1
    )
    expected_total_scenes = expected_parts * expected_scenes_per_part

    structure_errors: list[str] = []

    if actual_parts != expected_parts:
        structure_errors.append(
            f"Expected {expected_parts} parts but found {actual_parts}."
        )

    if actual_scenes and actual_scenes != expected_total_scenes:
        structure_errors.append(
            f"Expected {expected_total_scenes} total scenes based on "
            f"Input/topic.txt but found {actual_scenes}."
        )

    report = {
        "report_version": REPORT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "selected_story_source": selected_source,
        "policy": policy,
        "summary": {
            "parts_requested": expected_parts,
            "parts_found": actual_parts,
            "scenes_expected": expected_total_scenes,
            "scenes_found": actual_scenes,
            "total_words": total_words,
            "parts_needing_revision": len(failed_parts),
            "structure_errors": structure_errors,
            "valid": not failed_parts and not structure_errors,
            "api_calls_made": 0,
            "story_text_modified": False,
        },
        "part_results": selected_results,
        "source_story_results": source_results,
    }

    write_json(REPORT_FILE, report)

    log(f"Format: {format_type}")
    log(f"Story source: {selected_source}")
    log(f"Parts: {actual_parts}/{expected_parts}")
    log(f"Scenes: {actual_scenes}/{expected_total_scenes}")
    log(f"Words: {total_words}")
    log(f"Parts needing revision: {len(failed_parts)}")
    log(f"Report: {REPORT_FILE}")

    if structure_errors:
        for error in structure_errors:
            log(f"STRUCTURE WARNING: {error}")

    for result in failed_parts:
        log(
            "WORD-COUNT WARNING: "
            f"part {result['part_number']}: "
            f"{result['word_count']} words; "
            f"target {result['target_words']} "
            f"(allowed {result['minimum_words']}-"
            f"{result['maximum_words']}). "
            f"Status={result['status']}."
        )

    # The report is diagnostic. Do not fail the workflow merely because
    # an AI-generated story needs revision; callers can decide whether
    # to retry, revise, or stop.
    log("Story length validation finished.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)