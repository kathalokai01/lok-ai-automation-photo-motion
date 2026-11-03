#!/usr/bin/env python3
"""Central story-length, corrected-story part-selection and revision policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


POLICY_VERSION = "story-length-policy-v7"

SHORT_MIN_WORDS_PER_PART = 120
SHORT_TARGET_WORDS_PER_PART = 135
SHORT_PREFERRED_MAX_WORDS_PER_PART = 150
SHORT_HARD_MAX_WORDS_PER_PART = 180
SHORT_WORDS_PER_MINUTE = 135

FULL_WORDS_PER_MINUTE = 125
FULL_MIN_WORDS_PER_MINUTE = 100
FULL_MAX_WORDS_PER_MINUTE = 145

DEFAULT_SCENES_PER_MINUTE = 10
DEFAULT_SCENE_SECONDS = 6
MIN_SCENE_SECONDS = 3
MAX_SCENE_SECONDS = 12
MIN_PART_SECONDS = 30
MAX_AUTOMATIC_PART_SECONDS = 180

MAX_PARTS = 1000
MAX_STORY_WORDS = 10_000_000
MAX_REVISION_ATTEMPTS = 3


def clean_text(value: Any) -> str:
    """Normalize whitespace only. Never rewrite or paraphrase story text."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\ufeff", "")).strip()


def word_count(value: Any) -> int:
    return len(clean_text(value).split())


def sha256_text(value: Any) -> str:
    return hashlib.sha256(
        clean_text(value).encode("utf-8")
    ).hexdigest()


def parse_positive_int(
    value: Any,
    default: int,
    minimum: int = 1,
    maximum: int = MAX_STORY_WORDS,
) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, number))


def parse_parts_range(value: Any) -> tuple[int, int]:
    """Parse fixed counts and inclusive ranges such as 2-8 or 12-18."""
    if isinstance(value, bool):
        raise ValueError("PARTS must be a positive integer or valid range.")

    text = clean_text(value)
    match = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", text)
    if not match:
        raise ValueError(
            f"Invalid PARTS={value!r}. Use a fixed count such as 4 "
            "or a range such as 2-8."
        )

    minimum = int(match.group(1))
    maximum = int(match.group(2) or minimum)

    if minimum < 1 or maximum < 1:
        raise ValueError("PARTS values must be greater than zero.")
    if minimum > maximum:
        raise ValueError(
            f"Invalid reversed PARTS range {minimum}-{maximum}."
        )
    if maximum > MAX_PARTS:
        raise ValueError(f"PARTS cannot exceed {MAX_PARTS}.")

    return minimum, maximum


def normalize_format(value: Any) -> str:
    text = clean_text(value).lower()
    if text in {"short", "shorts", "reel", "reels", "vertical"}:
        return "short"
    if text in {
        "full", "long", "longform", "long-form",
        "landscape", "full_video",
    }:
        return "full"
    raise ValueError(
        f"Unsupported FORMAT={value!r}; use short or full."
    )


def _validate_total_words(total_words: Any) -> int:
    if isinstance(total_words, bool):
        raise ValueError("Story word count must be a non-negative integer.")
    try:
        total = int(total_words)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Story word count must be a non-negative integer."
        ) from exc
    if total < 0:
        raise ValueError("Story word count cannot be negative.")
    if total > MAX_STORY_WORDS:
        raise ValueError(
            f"Story exceeds {MAX_STORY_WORDS} supported words."
        )
    return total


def _validate_selected_count(
    selected: Any,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(selected, bool):
        raise ValueError("Selected part count must be an integer.")
    try:
        count = int(selected)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Selected part count must be an integer."
        ) from exc
    if not minimum <= count <= maximum:
        raise ValueError(
            f"Selected part count {count} is outside "
            f"the configured range {minimum}-{maximum}."
        )
    return count


def _resolve_part_bounds(
    parts: Any,
    parts_min: Any = None,
    parts_max: Any = None,
) -> tuple[int, int]:
    if parts_min is not None or parts_max is not None:
        if parts_min is None or parts_max is None:
            raise ValueError(
                "Both parts_min and parts_max must be supplied together."
            )
        minimum, maximum = parse_parts_range(
            f"{parts_min}-{parts_max}"
        )
        if parts not in (None, ""):
            if parse_parts_range(parts) != (minimum, maximum):
                raise ValueError(
                    "PARTS and explicit parts_min/parts_max disagree."
                )
        return minimum, maximum

    if parts in (None, ""):
        raise ValueError("A PARTS count or range is required.")
    return parse_parts_range(parts)


def choose_short_parts(
    total_words: Any,
    minimum_parts: Any,
    maximum_parts: Any,
) -> int:
    """Choose count after correction of the complete story."""
    total = _validate_total_words(total_words)
    minimum, maximum = _resolve_part_bounds(
        None, minimum_parts, maximum_parts
    )

    if total <= 0:
        raise ValueError(
            "The corrected story has zero words; final part count "
            "cannot be selected."
        )

    feasible_min = max(
        minimum,
        math.ceil(total / SHORT_HARD_MAX_WORDS_PER_PART),
    )
    feasible_max = min(
        maximum,
        total // SHORT_MIN_WORDS_PER_PART,
    )

    if feasible_min > feasible_max:
        required_min = minimum * SHORT_MIN_WORDS_PER_PART
        required_max = maximum * SHORT_HARD_MAX_WORDS_PER_PART
        raise ValueError(
            f"{total} corrected story words cannot fit the configured "
            f"{minimum}-{maximum} SHORT parts. The range permits "
            f"{required_min}-{required_max} words in total, with "
            "120-180 words required in every part."
        )

    return min(
        range(feasible_min, feasible_max + 1),
        key=lambda count: (
            abs(total / count - SHORT_TARGET_WORDS_PER_PART),
            abs(count - total / SHORT_TARGET_WORDS_PER_PART),
            count,
        ),
    )


def calculate_dynamic_short_parts(
    total_words: Any,
    minimum_parts: Any = 1,
    maximum_parts: Any = MAX_PARTS,
) -> int:
    return choose_short_parts(
        total_words, minimum_parts, maximum_parts
    )


def choose_full_parts(
    total_words: Any,
    minimum_parts: Any,
    maximum_parts: Any,
    story_length: Any = "auto",
) -> int:
    """Choose a FULL count inside the user's fixed count or range."""
    total = _validate_total_words(total_words)
    minimum, maximum = _resolve_part_bounds(
        None, minimum_parts, maximum_parts
    )

    if minimum == maximum:
        return minimum
    if total <= 0:
        raise ValueError(
            "The corrected FULL story must contain words before "
            "the final part count can be selected."
        )

    length = clean_text(story_length).lower()
    if length in {"", "auto", "automatic", "default"}:
        target_words_per_part = 3 * FULL_WORDS_PER_MINUTE
    else:
        total_minutes = resolve_duration_minutes(
            story_length, "full", minimum
        )
        target_words_per_part = max(
            1,
            round(total_minutes * FULL_WORDS_PER_MINUTE / minimum),
        )

    return min(
        range(minimum, maximum + 1),
        key=lambda count: (
            abs(total / count - target_words_per_part),
            count,
        ),
    )


def choose_part_count(
    total_words: Any,
    format_type: Any,
    parts: Any,
    story_length: Any = "auto",
) -> int:
    minimum, maximum = parse_parts_range(parts)
    fmt = normalize_format(format_type)

    if fmt == "short":
        return choose_short_parts(total_words, minimum, maximum)

    return choose_full_parts(
        total_words, minimum, maximum, story_length
    )


def resolve_duration_minutes(
    story_length: Any,
    format_type: Any,
    parts: Any = 1,
) -> float:
    fmt = normalize_format(format_type)
    length = clean_text(story_length).lower()

    if length in {"", "auto", "automatic", "default"}:
        count = parse_positive_int(parts, 1, 1, MAX_PARTS)
        return float(count if fmt == "short" else count * 3)

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)?",
        length,
    )
    if not match:
        raise ValueError(
            "STORY_LENGTH must be auto or a positive duration in minutes."
        )

    minutes = float(match.group(1))
    if not 0 < minutes <= 600:
        raise ValueError(
            "STORY_LENGTH must be greater than 0 and at most 600 minutes."
        )
    return minutes


def parse_scene_seconds(value: Any) -> float:
    text = clean_text(value).lower()
    if text in {"", "auto", "automatic", "default"}:
        return float(DEFAULT_SCENE_SECONDS)

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds)?",
        text,
    )
    if not match:
        raise ValueError(
            "SCENE_DURATION must be auto or a duration in seconds."
        )

    seconds = float(match.group(1))
    if not MIN_SCENE_SECONDS <= seconds <= MAX_SCENE_SECONDS:
        raise ValueError(
            f"SCENE_DURATION must be between "
            f"{MIN_SCENE_SECONDS} and {MAX_SCENE_SECONDS} seconds."
        )
    return seconds


def build_length_policy(
    *,
    format_type: Any,
    story_length: Any = "auto",
    parts: Any = 1,
    parts_min: Any = None,
    parts_max: Any = None,
    scenes_per_part: Any = DEFAULT_SCENES_PER_MINUTE,
    scene_duration: Any = "auto",
    source_word_count: Any = 0,
    selected_parts: Any = None,
) -> dict[str, Any]:
    """Build deterministic policy without making provider/API calls."""
    fmt = normalize_format(format_type)
    minimum_parts, maximum_parts = _resolve_part_bounds(
        parts, parts_min, parts_max
    )
    scene_count = parse_positive_int(
        scenes_per_part, DEFAULT_SCENES_PER_MINUTE, 1, 100
    )
    scene_seconds = parse_scene_seconds(scene_duration)
    source_words = _validate_total_words(source_word_count)

    if selected_parts is not None:
        part_count = _validate_selected_count(
            selected_parts, minimum_parts, maximum_parts
        )
    elif source_words:
        part_count = choose_part_count(
            source_words,
            fmt,
            f"{minimum_parts}-{maximum_parts}",
            story_length,
        )
    else:
        part_count = minimum_parts

    if fmt == "short":
        words_per_minute = SHORT_WORDS_PER_MINUTE
        minimum_per_part = SHORT_MIN_WORDS_PER_PART
        target_per_part = SHORT_TARGET_WORDS_PER_PART
        preferred_max_per_part = SHORT_PREFERRED_MAX_WORDS_PER_PART
        hard_max_per_part = SHORT_HARD_MAX_WORDS_PER_PART

        minimum_total = part_count * minimum_per_part
        target_total = part_count * target_per_part
        preferred_total = part_count * preferred_max_per_part
        maximum_total = part_count * hard_max_per_part

        if source_words:
            feasible_min = max(
                minimum_parts,
                math.ceil(source_words / SHORT_HARD_MAX_WORDS_PER_PART),
            )
            feasible_max = min(
                maximum_parts,
                source_words // SHORT_MIN_WORDS_PER_PART,
            )
            if not feasible_min <= part_count <= feasible_max:
                raise ValueError(
                    f"{source_words} words cannot fit {part_count} SHORT "
                    "parts under the 120-180 words-per-part rule."
                )
    else:
        words_per_minute = FULL_WORDS_PER_MINUTE
        minimum_per_part = None
        target_minutes_for_policy = resolve_duration_minutes(
            story_length, fmt, part_count
        )
        target_total = round(
            target_minutes_for_policy * words_per_minute
        )
        minimum_total = round(
            target_minutes_for_policy * FULL_MIN_WORDS_PER_MINUTE
        )
        maximum_total = round(
            target_minutes_for_policy * FULL_MAX_WORDS_PER_MINUTE
        )
        preferred_total = maximum_total
        target_per_part = max(1, round(target_total / part_count))
        preferred_max_per_part = max(
            1, round(maximum_total / part_count)
        )
        hard_max_per_part = preferred_max_per_part

    target_minutes = resolve_duration_minutes(
        story_length, fmt, part_count
    )
    total_scenes = part_count * scene_count
    visual_seconds = total_scenes * scene_seconds
    estimated_source_minutes = (
        source_words / words_per_minute if source_words else None
    )

    warnings: list[str] = []
    if fmt == "short" and source_words:
        if source_words < minimum_total:
            warnings.append("Corrected story is below the minimum capacity.")
        elif source_words > maximum_total:
            warnings.append("Corrected story exceeds the hard maximum capacity.")
        elif source_words > preferred_total:
            warnings.append(
                "Parts above 150 words require revision; 180 is the hard limit."
            )

    if visual_seconds < MIN_PART_SECONDS * part_count:
        warnings.append("Estimated visual duration may be too short.")
    if visual_seconds > MAX_AUTOMATIC_PART_SECONDS * part_count:
        warnings.append("Estimated visual duration may be unusually long.")

    feasible_min = minimum_parts
    feasible_max = maximum_parts
    if fmt == "short" and source_words:
        feasible_min = max(
            minimum_parts,
            math.ceil(source_words / SHORT_HARD_MAX_WORDS_PER_PART),
        )
        feasible_max = min(
            maximum_parts,
            source_words // SHORT_MIN_WORDS_PER_PART,
        )

    return {
        "policy_version": POLICY_VERSION,
        "format": fmt,
        "story_length_input": clean_text(story_length) or "auto",
        "target_duration_minutes": round(target_minutes, 2),

        "parts_count": part_count,
        "configured_parts_count": part_count,
        "configured_parts_min": minimum_parts,
        "configured_parts_max": maximum_parts,
        "feasible_parts_min": feasible_min,
        "feasible_parts_max": feasible_max,
        "parts_count_is_dynamic": minimum_parts != maximum_parts,
        "parts_count_selected_from_range": (
            minimum_parts != maximum_parts and source_words > 0
        ),
        "parts_count_is_provisional": (
            source_words == 0 and minimum_parts != maximum_parts
        ),

        "scenes_per_part": scene_count,
        "total_scenes": total_scenes,
        "scene_duration_seconds": scene_seconds,
        "estimated_visual_duration_minutes": round(
            visual_seconds / 60.0, 2
        ),

        "words_per_minute_target": words_per_minute,
        "target_total_words": target_total,
        "minimum_total_words": minimum_total,
        "preferred_maximum_total_words": preferred_total,
        "maximum_total_words": maximum_total,

        "target_words_per_part": target_per_part,
        "minimum_words_per_part": minimum_per_part,
        "preferred_maximum_words_per_part": preferred_max_per_part,
        "maximum_words_per_part": hard_max_per_part,
        "hard_maximum_words_per_part": hard_max_per_part,

        "source_word_count": source_words,
        "corrected_story_word_count": source_words,
        "estimated_source_narration_minutes": (
            round(estimated_source_minutes, 2)
            if estimated_source_minutes is not None else None
        ),
        "source_word_count_within_hard_limit": (
            minimum_total <= source_words <= maximum_total
            if source_words else None
        ),

        "hook_required": True,
        "separate_hook_scene_required": True,
        "separate_suspense_scene_required": True,
        "suspense_at_nonfinal_part_end_required": True,
        "final_part_uses_resolution": True,
        "final_resolution_required": True,

        "revise_parts_above_150": fmt == "short",
        "hard_fail_below_120": fmt == "short",
        "hard_fail_above_180": fmt == "short",
        "retry_before_final_validation": True,
        "maximum_revision_attempts": MAX_REVISION_ATTEMPTS,

        "revision_attempted": False,
        "revision_outcome": "not_started",
        "revision_parts": [],
        "revision_results": [],

        "warnings": warnings,
        "api_calls_made": 0,
        "duration_is_estimate": True,
        "note": (
            "The complete story must be corrected before word counting "
            "and final part selection. SHORT parts target 120-150 words. "
            "151-180 words are accepted only after a recorded revision "
            "attempt. Below 120 or above 180 fails. FULL supports fixed "
            "counts and inclusive ranges."
        ),
    }


def validate_story_length(
    narration_text: Any,
    policy: dict[str, Any],
    *,
    part_number: int = 1,
    revision_attempted: bool = False,
) -> dict[str, Any]:
    """Validate one main narration part; does not alter its text."""
    text = clean_text(narration_text)
    actual = word_count(text)
    fmt = normalize_format(policy.get("format", "short"))
    number = _validate_selected_count(
        part_number, 1, MAX_PARTS
    )
    attempted = bool(revision_attempted)

    if fmt == "short":
        minimum = SHORT_MIN_WORDS_PER_PART
        target = SHORT_TARGET_WORDS_PER_PART
        preferred_maximum = SHORT_PREFERRED_MAX_WORDS_PER_PART
        hard_maximum = SHORT_HARD_MAX_WORDS_PER_PART

        if actual == 0:
            status = "missing_narration"
            needs_revision = True
            passed = False
        elif actual < minimum:
            status = "below_minimum"
            needs_revision = True
            passed = False
        elif actual > hard_maximum:
            status = "above_hard_maximum"
            needs_revision = True
            passed = False
        elif actual > preferred_maximum and not attempted:
            status = "revision_required_above_150"
            needs_revision = True
            passed = False
        elif actual > preferred_maximum and attempted:
            status = "within_hard_limit_after_revision"
            needs_revision = False
            passed = True
        else:
            status = "within_target"
            needs_revision = False
            passed = True
    else:
        target = parse_positive_int(
            policy.get("target_words_per_part"),
            FULL_WORDS_PER_MINUTE,
            1,
            MAX_STORY_WORDS,
        )
        minimum = max(
            1,
            round(target * FULL_MIN_WORDS_PER_MINUTE
                  / FULL_WORDS_PER_MINUTE),
        )
        hard_maximum = max(
            minimum,
            round(target * FULL_MAX_WORDS_PER_MINUTE
                  / FULL_WORDS_PER_MINUTE),
        )
        preferred_maximum = hard_maximum

        if actual == 0:
            status = "missing_narration"
            needs_revision = True
            passed = False
        elif actual < minimum:
            status = "below_target_range"
            needs_revision = True
            passed = False
        elif actual > hard_maximum:
            status = "above_target_range"
            needs_revision = True
            passed = False
        else:
            status = "within_target"
            needs_revision = False
            passed = True

    return {
        "part_number": number,
        "format": fmt,
        "actual_words": actual,
        "target_words": target,
        "minimum_words": minimum,
        "preferred_maximum_words": preferred_maximum,
        "maximum_words": hard_maximum,
        "hard_maximum_words": hard_maximum,
        "revision_attempted": attempted,
        "status": status,
        "needs_revision": needs_revision,
        "retry_required": needs_revision,
        "final_validation_passed": passed,
        "text_modified": False,
    }


def _extract_part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part or "")

    for key in (
        "narration", "story", "text", "content",
        "main_story", "main_narration",
    ):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _part_revision_status(
    part: Any,
    index: int,
    revision_attempted: Any,
) -> bool:
    if isinstance(revision_attempted, dict):
        return bool(
            revision_attempted.get(
                index,
                revision_attempted.get(str(index), False),
            )
        )

    if isinstance(revision_attempted, (list, tuple)):
        return (
            bool(revision_attempted[index - 1])
            if index - 1 < len(revision_attempted)
            else False
        )

    if isinstance(part, dict):
        return bool(
            part.get("revision_attempted")
            or part.get("revision_completed")
            or part.get("word_limit_revision_attempted")
        )

    return bool(revision_attempted)


def validate_all_parts(
    parts: Any,
    policy: dict[str, Any],
    *,
    revision_attempted: Any = False,
) -> dict[str, Any]:
    """Validate every part and require recorded revision for 151-180 words."""
    if not isinstance(parts, list):
        raise TypeError("Story parts must be supplied as a list.")

    expected = parse_positive_int(
        policy.get("parts_count"), 1, 1, MAX_PARTS
    )
    minimum_parts = parse_positive_int(
        policy.get("configured_parts_min"), expected, 1, MAX_PARTS
    )
    maximum_parts = parse_positive_int(
        policy.get("configured_parts_max"),
        expected,
        minimum_parts,
        MAX_PARTS,
    )

    errors: list[str] = []
    retry_parts: list[int] = []
    results: list[dict[str, Any]] = []

    if not minimum_parts <= expected <= maximum_parts:
        errors.append(
            f"Selected count {expected} is outside "
            f"configured range {minimum_parts}-{maximum_parts}."
        )

    if len(parts) != expected:
        errors.append(
            f"Selected part count is {expected}, "
            f"but generated part count is {len(parts)}."
        )

    for index, part in enumerate(parts, 1):
        attempted = _part_revision_status(
            part, index, revision_attempted
        )
        result = validate_story_length(
            _extract_part_text(part),
            policy,
            part_number=index,
            revision_attempted=attempted,
        )
        results.append(result)

        if result["needs_revision"]:
            retry_parts.append(index)
            errors.append(
                f"Part {index}: {result['actual_words']} words; "
                f"required {result['minimum_words']}-"
                f"{result['maximum_words']}; "
                f"status={result['status']}."
            )

    return {
        "policy_version": POLICY_VERSION,
        "format": normalize_format(policy.get("format", "short")),
        "expected_parts": expected,
        "configured_parts_min": minimum_parts,
        "configured_parts_max": maximum_parts,
        "actual_parts": len(parts),
        "valid": not errors,
        "retry_required": bool(retry_parts),
        "retry_parts": retry_parts,
        "parts": results,
        "errors": errors,
    }


def select_final_part_count(
    *,
    total_corrected_words: Any,
    format_type: Any,
    parts_specification: Any,
    story_length: Any = "auto",
    config: dict[str, Any] | None = None,
) -> int:
    """Select count from the complete corrected story and persist it."""
    minimum, maximum = parse_parts_range(parts_specification)
    total = _validate_total_words(total_corrected_words)

    selected = choose_part_count(
        total,
        format_type,
        f"{minimum}-{maximum}",
        story_length,
    )

    if config is not None:
        config["PARTS_SPEC"] = (
            str(minimum)
            if minimum == maximum
            else f"{minimum}-{maximum}"
        )
        config["PARTS_MIN"] = minimum
        config["PARTS_MAX"] = maximum

        # The resolver record is the shared cross-process source of truth.
        try:
            from input_config import set_selected_parts
        except ImportError:
            from scripts.input_config import set_selected_parts

        set_selected_parts(
            config,
            selected,
            source="story_length_policy",
            story_word_count=total,
        )

    return selected


def update_policy_with_corrected_story(
    policy: dict[str, Any],
    corrected_story: Any,
    *,
    selected_parts: Any = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild policy from the corrected complete story, not source draft."""
    corrected_text = clean_text(corrected_story)
    total_words = word_count(corrected_text)
    if total_words <= 0:
        raise ValueError(
            "Corrected complete story is empty; length policy cannot proceed."
        )

    minimum = parse_positive_int(
        policy.get("configured_parts_min"), 1, 1, MAX_PARTS
    )
    maximum = parse_positive_int(
        policy.get("configured_parts_max"),
        minimum,
        minimum,
        MAX_PARTS,
    )
    fmt = normalize_format(policy.get("format", "short"))
    story_length = policy.get("story_length_input", "auto")

    if selected_parts is None:
        selected_parts = choose_part_count(
            total_words,
            fmt,
            f"{minimum}-{maximum}",
            story_length,
        )
    else:
        selected_parts = _validate_selected_count(
            selected_parts, minimum, maximum
        )

    if config is not None:
        selected_parts = select_final_part_count(
            total_corrected_words=total_words,
            format_type=fmt,
            parts_specification=f"{minimum}-{maximum}",
            story_length=story_length,
            config=config,
        )

    updated = build_length_policy(
        format_type=fmt,
        story_length=story_length,
        parts=f"{minimum}-{maximum}",
        scenes_per_part=policy.get(
            "scenes_per_part", DEFAULT_SCENES_PER_MINUTE
        ),
        scene_duration=policy.get(
            "scene_duration_seconds", "auto"
        ),
        source_word_count=total_words,
        selected_parts=selected_parts,
    )

    updated["corrected_story_word_count"] = total_words
    updated["corrected_story_sha256"] = sha256_text(corrected_text)
    updated["story_was_corrected_before_counting"] = True
    updated["selection_persisted"] = config is not None
    updated["selection_source"] = "corrected_complete_story"
    return updated


def record_revision_results(
    policy: dict[str, Any],
    original_parts: Any,
    revised_parts: Any,
    *,
    attempted_parts: Any,
) -> dict[str, Any]:
    """Record attempted revisions and validate the resulting part records."""
    if not isinstance(original_parts, list) or not isinstance(revised_parts, list):
        raise TypeError("Original and revised parts must both be lists.")

    if len(original_parts) != len(revised_parts):
        raise ValueError(
            "Revision cannot change the selected number of story parts."
        )

    if isinstance(attempted_parts, dict):
        attempted = {
            int(key) for key, value in attempted_parts.items()
            if bool(value)
        }
    elif isinstance(attempted_parts, (list, tuple, set)):
        attempted = {int(item) for item in attempted_parts}
    else:
        attempted = set()

    expected = int(policy.get("parts_count", 0))
    if len(revised_parts) != expected:
        raise ValueError(
            f"Revision produced {len(revised_parts)} parts; "
            f"expected {expected}."
        )

    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for index, (original, revised) in enumerate(
        zip(original_parts, revised_parts), 1
    ):
        old_text = _extract_part_text(original)
        new_text = _extract_part_text(revised)
        was_attempted = index in attempted

        result = validate_story_length(
            new_text,
            policy,
            part_number=index,
            revision_attempted=was_attempted,
        )

        result["original_words"] = word_count(old_text)
        result["revised_words"] = word_count(new_text)
        result["revision_attempted"] = was_attempted
        result["text_changed"] = (
            clean_text(old_text) != clean_text(new_text)
        )
        result["original_sha256"] = sha256_text(old_text)
        result["revised_sha256"] = sha256_text(new_text)

        if result["needs_revision"]:
            errors.append(
                f"Part {index} remains invalid after revision handling: "
                f"{result['status']} ({result['actual_words']} words)."
            )

        if (
            normalize_format(policy.get("format", "short")) == "short"
            and 150 < result["actual_words"] <= 180
            and not was_attempted
        ):
            errors.append(
                f"Part {index} has {result['actual_words']} words; "
                "a revision attempt is mandatory for 151-180 words."
            )

        results.append(result)

    policy["revision_attempted"] = bool(attempted)
    policy["revision_parts"] = sorted(attempted)
    policy["revision_results"] = results
    policy["revision_outcome"] = (
        "passed" if not errors else "failed"
    )
    policy["revision_errors"] = errors
    policy["revision_completed_at_utc"] = (
        datetime.now(timezone.utc).isoformat()
    )
    policy["final_part_validation_passed"] = not errors

    if errors:
        raise ValueError("Story revision validation failed: " + " ".join(errors))

    return {
        "valid": True,
        "revision_attempted": bool(attempted),
        "revision_parts": sorted(attempted),
        "parts": results,
        "errors": [],
    }


def validate_corrected_story_parts(
    parts: Any,
    policy: dict[str, Any],
    *,
    revision_attempted: Any = False,
) -> dict[str, Any]:
    """Final validation entry point; incomplete SHORT parts always fail."""
    result = validate_all_parts(
        parts,
        policy,
        revision_attempted=revision_attempted,
    )

    if not result["valid"]:
        raise ValueError(
            "Final story part validation failed: "
            + " ".join(result["errors"])
        )

    return result


def write_policy(
    path: str | Path,
    policy: dict[str, Any],
) -> None:
    """Write policy JSON atomically."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                policy,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def read_policy(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read story length policy: {exc}") from exc

    if not isinstance(value, dict):
        raise ValueError("Story length policy JSON must contain an object.")
    if value.get("policy_version") != POLICY_VERSION:
        raise ValueError(
            "Story length policy version mismatch; regenerate the policy."
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate and validate story length and part policy."
    )
    parser.add_argument(
        "--format",
        default="short",
        choices=("short", "full"),
    )
    parser.add_argument("--story-length", default="auto")
    parser.add_argument(
        "--parts",
        default="1",
        help="Fixed count or range, e.g. 4, 2-8, 4-6, 12-18.",
    )
    parser.add_argument(
        "--selected-parts",
        type=int,
        default=None,
        help="Final count selected after corrected-story word counting.",
    )
    parser.add_argument(
        "--scenes",
        type=int,
        default=DEFAULT_SCENES_PER_MINUTE,
    )
    parser.add_argument("--scene-duration", default="auto")
    parser.add_argument(
        "--source-words",
        type=int,
        default=0,
        help="Word count of the corrected complete story.",
    )
    parser.add_argument(
        "--corrected-story-file",
        default="",
        help="Optional UTF-8 file containing the corrected complete story.",
    )
    parser.add_argument(
        "--output",
        default="output/story/story_length_policy.json",
    )

    args = parser.parse_args()

    source_words = args.source_words
    corrected_text = ""

    if args.corrected_story_file:
        story_path = Path(args.corrected_story_file)
        if not story_path.is_file():
            raise FileNotFoundError(
                f"Corrected story file not found: {story_path}"
            )
        corrected_text = story_path.read_text(encoding="utf-8-sig")
        measured_words = word_count(corrected_text)

        if source_words and source_words != measured_words:
            raise ValueError(
                "--source-words does not match the corrected story file."
            )
        source_words = measured_words

    minimum, maximum = parse_parts_range(args.parts)

    if source_words:
        selected = args.selected_parts
        if selected is None:
            selected = choose_part_count(
                source_words,
                args.format,
                f"{minimum}-{maximum}",
                args.story_length,
            )
    else:
        selected = args.selected_parts

    policy = build_length_policy(
        format_type=args.format,
        story_length=args.story_length,
        parts=f"{minimum}-{maximum}",
        scenes_per_part=args.scenes,
        scene_duration=args.scene_duration,
        source_word_count=source_words,
        selected_parts=selected,
    )

    if corrected_text:
        policy["corrected_story_sha256"] = sha256_text(corrected_text)
        policy["story_was_corrected_before_counting"] = True

    policy["selection_persisted"] = False
    write_policy(args.output, policy)

    print(json.dumps(policy, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())