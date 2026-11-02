#!/usr/bin/env python3
"""Katha Lok AI — story length, part selection and validation policy."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


POLICY_VERSION = "story-length-policy-v5"

# SHORT narration limits.
SHORT_MIN_WORDS_PER_PART = 120
SHORT_TARGET_WORDS_PER_PART = 135
SHORT_PREFERRED_MAX_WORDS_PER_PART = 150
SHORT_HARD_MAX_WORDS_PER_PART = 180

SHORT_WORDS_PER_MINUTE = 135

# FULL narration planning.
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


def clean_text(value: Any) -> str:
    """Normalize whitespace without changing story wording."""
    if value is None:
        return ""

    text = str(value).replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def word_count(value: Any) -> int:
    """Count whitespace-separated narration words."""
    return len(clean_text(value).split())


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
    """
    Parse a fixed PARTS count or an inclusive range.

    Examples:
        4       -> (4, 4)
        2-8     -> (2, 8)
        4-6     -> (4, 6)
        8-11    -> (8, 11)
        12-18   -> (12, 18)

    There is no fixed 8-11 restriction.
    """

    if isinstance(value, bool):
        raise ValueError(
            "PARTS must be a positive integer or a valid range."
        )

    text = clean_text(value)

    match = re.fullmatch(
        r"(\d+)\s*(?:-\s*(\d+))?",
        text,
    )

    if not match:
        raise ValueError(
            f"Invalid PARTS={value!r}. "
            "Use a fixed number such as 4 or a range such as 2-8."
        )

    minimum = int(match.group(1))
    maximum = int(match.group(2) or minimum)

    if minimum < 1 or maximum < 1:
        raise ValueError(
            "PARTS values must be positive integers greater than zero."
        )

    if minimum > maximum:
        raise ValueError(
            f"Invalid PARTS range {minimum}-{maximum}: "
            "the first number cannot exceed the second."
        )

    if maximum > MAX_PARTS:
        raise ValueError(
            f"PARTS cannot exceed {MAX_PARTS}."
        )

    return minimum, maximum


def normalize_format(value: Any) -> str:
    text = clean_text(value).lower()

    if text in {
        "short", "shorts", "reel", "reels", "vertical"
    }:
        return "short"

    if text in {
        "full", "long", "longform", "long-form",
        "landscape", "full_video",
    }:
        return "full"

    raise ValueError(
        f"Unsupported FORMAT={value!r}; "
        "use FORMAT=short or FORMAT=full."
    )


def _validate_total_words(total_words: Any) -> int:
    if isinstance(total_words, bool):
        raise ValueError(
            "Story word count must be a non-negative integer."
        )

    try:
        total = int(total_words)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Story word count must be a non-negative integer."
        ) from exc

    if total < 0:
        raise ValueError(
            "Story word count cannot be negative."
        )

    if total > MAX_STORY_WORDS:
        raise ValueError(
            f"Story exceeds the maximum supported count "
            f"of {MAX_STORY_WORDS} words."
        )

    return total


def _validate_selected_count(
    selected: Any,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(selected, bool):
        raise ValueError(
            "Selected part count must be an integer."
        )

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


def choose_short_parts(
    total_words: Any,
    minimum_parts: Any,
    maximum_parts: Any,
) -> int:
    """
    Select the SHORT part count after the complete story is corrected.

    Fixed count:
        The exact configured count is retained.

    Range:
        Select a count inside the configured range that keeps each
        part as close as possible to 135 words.

    Every selected part must be capable of containing 120-180 words.
    """

    total = _validate_total_words(total_words)

    minimum, maximum = parse_parts_range(
        f"{minimum_parts}-{maximum_parts}"
    )

    if minimum == maximum:
        lower = minimum * SHORT_MIN_WORDS_PER_PART
        upper = minimum * SHORT_HARD_MAX_WORDS_PER_PART

        if not lower <= total <= upper:
            raise ValueError(
                f"{total} corrected story words cannot fit exactly "
                f"{minimum} SHORT parts. Required total: "
                f"{lower}-{upper} words."
            )

        return minimum

    if total == 0:
        raise ValueError(
            "The corrected story has zero words. "
            "Part count cannot be selected before story generation."
        )

    feasible_minimum = max(
        minimum,
        math.ceil(
            total / SHORT_HARD_MAX_WORDS_PER_PART
        ),
    )

    feasible_maximum = min(
        maximum,
        total // SHORT_MIN_WORDS_PER_PART,
    )

    if feasible_minimum > feasible_maximum:
        raise ValueError(
            f"{total} corrected story words cannot fit the configured "
            f"{minimum}-{maximum} SHORT part range. "
            f"Each part must contain 120-180 words."
        )

    candidates = range(
        feasible_minimum,
        feasible_maximum + 1,
    )

    return min(
        candidates,
        key=lambda count: (
            abs(
                (total / count)
                - SHORT_TARGET_WORDS_PER_PART
            ),
            abs(
                count
                - (total / SHORT_TARGET_WORDS_PER_PART)
            ),
            count,
        ),
    )


def calculate_dynamic_short_parts(
    total_words: Any,
    minimum_parts: Any = 1,
    maximum_parts: Any = MAX_PARTS,
) -> int:
    """Backward-compatible SHORT part selector."""
    return choose_short_parts(
        total_words,
        minimum_parts,
        maximum_parts,
    )


def choose_full_parts(
    total_words: Any,
    minimum_parts: Any,
    maximum_parts: Any,
    story_length: Any = "auto",
) -> int:
    """
    Select FULL part count inside the configured fixed count or range.

    Unlike the old implementation, FULL ranges are not rejected.
    The configured duration and corrected story size guide selection.
    """

    total = _validate_total_words(total_words)

    minimum, maximum = parse_parts_range(
        f"{minimum_parts}-{maximum_parts}"
    )

    if minimum == maximum:
        return minimum

    if total <= 0:
        raise ValueError(
            "The corrected FULL story must contain words "
            "before its part count can be selected."
        )

    target_minutes = resolve_duration_minutes(
        story_length,
        "full",
        minimum,
    )

    target_total = max(
        total,
        round(target_minutes * FULL_WORDS_PER_MINUTE),
    )

    ideal_count = max(
        minimum,
        min(
            maximum,
            round(
                target_total
                / max(1, target_minutes * FULL_WORDS_PER_MINUTE / minimum)
            ),
        ),
    )

    # Choose a count that distributes the actual story reasonably.
    # No fixed 8-11 range is imposed.
    return min(
        range(minimum, maximum + 1),
        key=lambda count: (
            abs(
                (total / count)
                - (FULL_WORDS_PER_MINUTE * target_minutes / minimum)
            ),
            abs(count - ideal_count),
            count,
        ),
    )


def choose_part_count(
    total_words: Any,
    format_type: Any,
    parts: Any,
    story_length: Any = "auto",
) -> int:
    """Common selector for SHORT and FULL."""
    minimum, maximum = parse_parts_range(parts)
    fmt = normalize_format(format_type)

    if fmt == "short":
        return choose_short_parts(
            total_words,
            minimum,
            maximum,
        )

    return choose_full_parts(
        total_words,
        minimum,
        maximum,
        story_length,
    )


def resolve_duration_minutes(
    story_length: Any,
    format_type: Any,
    parts: Any = 1,
) -> float:
    fmt = normalize_format(format_type)
    length = clean_text(story_length).lower()

    if length in {"", "auto", "automatic", "default"}:
        part_count = parse_positive_int(
            parts,
            1,
            1,
            MAX_PARTS,
        )

        return float(
            part_count if fmt == "short"
            else part_count * 3
        )

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*"
        r"(m|min|mins|minute|minutes)?",
        length,
    )

    if not match:
        raise ValueError(
            "STORY_LENGTH must be 'auto' or a positive duration in minutes."
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
        r"(\d+(?:\.\d+)?)\s*"
        r"(s|sec|secs|second|seconds)?",
        text,
    )

    if not match:
        raise ValueError(
            "SCENE_DURATION must be 'auto' or a duration in seconds."
        )

    seconds = float(match.group(1))

    if not MIN_SCENE_SECONDS <= seconds <= MAX_SCENE_SECONDS:
        raise ValueError(
            f"SCENE_DURATION must be between "
            f"{MIN_SCENE_SECONDS} and {MAX_SCENE_SECONDS} seconds."
        )

    return seconds


def _resolve_part_bounds(
    parts: Any,
    parts_min: Any = None,
    parts_max: Any = None,
) -> tuple[int, int]:
    """Resolve PARTS input or explicit minimum/maximum arguments."""
    if parts_min is not None or parts_max is not None:
        if parts_min is None or parts_max is None:
            raise ValueError(
                "Both parts_min and parts_max must be supplied together."
            )

        minimum, maximum = parse_parts_range(
            f"{parts_min}-{parts_max}"
        )

        # If PARTS also contains a specification, ensure it agrees.
        if parts not in (None, ""):
            supplied_min, supplied_max = parse_parts_range(parts)

            if (supplied_min, supplied_max) != (minimum, maximum):
                raise ValueError(
                    "PARTS and explicit parts_min/parts_max disagree."
                )

        return minimum, maximum

    return parse_parts_range(parts)


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
    """Build deterministic story policy without model/API calls."""

    fmt = normalize_format(format_type)

    minimum_parts, maximum_parts = _resolve_part_bounds(
        parts,
        parts_min,
        parts_max,
    )

    scene_count = parse_positive_int(
        scenes_per_part,
        DEFAULT_SCENES_PER_MINUTE,
        1,
        100,
    )

    scene_seconds = parse_scene_seconds(scene_duration)
    source_words = _validate_total_words(source_word_count)

    if selected_parts is not None:
        part_count = _validate_selected_count(
            selected_parts,
            minimum_parts,
            maximum_parts,
        )

        if fmt == "short":
            lower = part_count * SHORT_MIN_WORDS_PER_PART
            upper = part_count * SHORT_HARD_MAX_WORDS_PER_PART

            if source_words and not lower <= source_words <= upper:
                raise ValueError(
                    f"{source_words} words cannot fit exactly "
                    f"{part_count} SHORT parts. Required total: "
                    f"{lower}-{upper} words."
                )

    elif source_words:
        part_count = choose_part_count(
            source_words,
            fmt,
            f"{minimum_parts}-{maximum_parts}",
            story_length,
        )

    elif minimum_parts == maximum_parts:
        part_count = minimum_parts

    else:
        # Provisional policy before the corrected story is available.
        # The final count must be recalculated after correction.
        part_count = minimum_parts

    if fmt == "short":
        words_per_minute = SHORT_WORDS_PER_MINUTE
        minimum_per_part = SHORT_MIN_WORDS_PER_PART
        target_per_part = SHORT_TARGET_WORDS_PER_PART
        preferred_maximum_per_part = SHORT_PREFERRED_MAX_WORDS_PER_PART
        hard_maximum_per_part = SHORT_HARD_MAX_WORDS_PER_PART

        minimum_total_words = (
            part_count * minimum_per_part
        )
        target_total_words = (
            part_count * target_per_part
        )
        preferred_maximum_total_words = (
            part_count * preferred_maximum_per_part
        )
        maximum_total_words = (
            part_count * hard_maximum_per_part
        )

    else:
        words_per_minute = FULL_WORDS_PER_MINUTE
        minimum_per_part = None
        target_per_part = None
        preferred_maximum_per_part = None
        hard_maximum_per_part = None

        target_minutes = resolve_duration_minutes(
            story_length,
            fmt,
            part_count,
        )

        target_total_words = round(
            target_minutes * words_per_minute
        )

        minimum_total_words = round(
            target_minutes * FULL_MIN_WORDS_PER_MINUTE
        )

        maximum_total_words = round(
            target_minutes * FULL_MAX_WORDS_PER_MINUTE
        )

        preferred_maximum_total_words = maximum_total_words

        target_per_part = max(
            1,
            round(target_total_words / part_count),
        )

    target_minutes = resolve_duration_minutes(
        story_length,
        fmt,
        part_count,
    )

    total_scenes = part_count * scene_count
    estimated_visual_seconds = total_scenes * scene_seconds

    estimated_source_minutes = (
        source_words / words_per_minute
        if source_words else None
    )

    warnings: list[str] = []

    if fmt == "short" and source_words:
        if source_words < minimum_total_words:
            warnings.append(
                "Corrected story is below the minimum word capacity "
                "for the selected part count."
            )

        elif source_words > maximum_total_words:
            warnings.append(
                "Corrected story exceeds the hard maximum capacity. "
                "Select a larger permitted part count or revise the story."
            )

        elif source_words > preferred_maximum_total_words:
            warnings.append(
                "Some parts may exceed the 150-word target and require "
                "a revision attempt. No part may exceed 180 words."
            )

    if estimated_visual_seconds < MIN_PART_SECONDS * part_count:
        warnings.append(
            "Estimated visual duration may be too short."
        )

    if estimated_visual_seconds > MAX_AUTOMATIC_PART_SECONDS * part_count:
        warnings.append(
            "Estimated visual duration may be unusually long."
        )

    feasible_minimum = minimum_parts
    feasible_maximum = maximum_parts

    if fmt == "short" and source_words:
        feasible_minimum = max(
            minimum_parts,
            math.ceil(
                source_words / SHORT_HARD_MAX_WORDS_PER_PART
            ),
        )

        feasible_maximum = min(
            maximum_parts,
            source_words // SHORT_MIN_WORDS_PER_PART,
        )

    return {
        "policy_version": POLICY_VERSION,
        "format": fmt,
        "story_length_input": clean_text(story_length) or "auto",

        "target_duration_minutes": round(
            target_minutes,
            2,
        ),

        "parts_count": part_count,
        "configured_parts_count": part_count,
        "configured_parts_min": minimum_parts,
        "configured_parts_max": maximum_parts,
        "feasible_parts_min": feasible_minimum,
        "feasible_parts_max": feasible_maximum,

        "parts_count_is_dynamic": (
            minimum_parts != maximum_parts
        ),
        "parts_count_selected_from_range": (
            minimum_parts != maximum_parts
            and source_words > 0
        ),
        "parts_count_is_provisional": (
            source_words == 0
            and minimum_parts != maximum_parts
        ),

        "scenes_per_part": scene_count,
        "total_scenes": total_scenes,
        "scene_duration_seconds": scene_seconds,
        "estimated_visual_duration_minutes": round(
            estimated_visual_seconds / 60.0,
            2,
        ),

        "words_per_minute_target": words_per_minute,
        "target_total_words": target_total_words,
        "minimum_total_words": minimum_total_words,
        "preferred_maximum_total_words": preferred_maximum_total_words,
        "maximum_total_words": maximum_total_words,

        "target_words_per_part": target_per_part,
        "minimum_words_per_part": minimum_per_part,
        "preferred_maximum_words_per_part": preferred_maximum_per_part,
        "maximum_words_per_part": hard_maximum_per_part,
        "hard_maximum_words_per_part": hard_maximum_per_part,

        "source_word_count": source_words,
        "estimated_source_narration_minutes": (
            round(estimated_source_minutes, 2)
            if estimated_source_minutes is not None
            else None
        ),

        "source_word_count_within_hard_limit": (
            minimum_total_words <= source_words <= maximum_total_words
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

        "warnings": warnings,
        "api_calls_made": 0,
        "duration_is_estimate": True,

        "note": (
            "The original fixed count or any valid inclusive PARTS range "
            "is preserved. SHORT selects its final count after the complete "
            "corrected story is counted. Each SHORT part targets 120-150 "
            "words. Parts above 150 require revision; 180 is the hard "
            "maximum. Parts below 120 or above 180 fail validation. "
            "FULL also accepts fixed counts and configured ranges."
        ),
    }

def validate_story_length(
    narration_text: Any,
    policy: dict[str, Any],
    *,
    part_number: int = 1,
    revision_attempted: bool = False,
) -> dict[str, Any]:
    """
    Validate one part.

    SHORT:
      120-150: pass
      151-180: revision required; pass only after revision was attempted
      below 120: fail
      above 180: fail

    FULL:
      Uses the policy's estimated per-part word target.
    """

    text = clean_text(narration_text)
    actual = word_count(text)
    fmt = normalize_format(
        policy.get("format", "short")
    )

    number = parse_positive_int(
        part_number,
        1,
        1,
        MAX_PARTS,
    )

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

        elif actual > preferred_maximum:
            if revision_attempted:
                status = "within_hard_limit_after_revision"
                needs_revision = False
                passed = True
            else:
                status = "revision_required_above_150"
                needs_revision = True
                passed = False

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
            round(
                target
                * FULL_MIN_WORDS_PER_MINUTE
                / FULL_WORDS_PER_MINUTE
            ),
        )

        hard_maximum = max(
            minimum,
            round(
                target
                * FULL_MAX_WORDS_PER_MINUTE
                / FULL_WORDS_PER_MINUTE
            ),
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
        "revision_attempted": bool(revision_attempted),
        "status": status,
        "needs_revision": needs_revision,
        "retry_required": needs_revision,
        "final_validation_passed": passed,
        "text_modified": False,
    }


def _extract_part_text(part: Any) -> str:
    """Read narration text from supported part record formats."""
    if isinstance(part, str):
        return part

    if not isinstance(part, dict):
        return str(part or "")

    for key in (
        "narration",
        "story",
        "text",
        "content",
        "main_story",
        "main_narration",
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
    """Resolve whether a specific part has already had a revision attempt."""

    if isinstance(revision_attempted, dict):
        return bool(
            revision_attempted.get(
                index,
                revision_attempted.get(str(index), False),
            )
        )

    if isinstance(revision_attempted, (list, tuple)):
        if index - 1 < len(revision_attempted):
            return bool(revision_attempted[index - 1])

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
    """
    Validate all main story parts and enforce the final selected count.

    revision_attempted may be:
      - True/False for all parts
      - a list of per-part booleans
      - a dictionary keyed by part number
    """

    if not isinstance(parts, list):
        raise TypeError(
            "Story parts must be supplied as a list."
        )

    expected = parse_positive_int(
        policy.get("parts_count"),
        1,
        1,
        MAX_PARTS,
    )

    minimum_parts = parse_positive_int(
        policy.get("configured_parts_min", expected),
        expected,
        1,
        MAX_PARTS,
    )

    maximum_parts = parse_positive_int(
        policy.get("configured_parts_max", expected),
        expected,
        minimum_parts,
        MAX_PARTS,
    )

    errors: list[str] = []
    retry_parts: list[int] = []
    results: list[dict[str, Any]] = []

    if not minimum_parts <= expected <= maximum_parts:
        errors.append(
            f"Selected part count {expected} is outside "
            f"the configured range {minimum_parts}-{maximum_parts}."
        )

    if len(parts) != expected:
        errors.append(
            f"Selected part count is {expected}, "
            f"but generated part count is {len(parts)}."
        )

    for index, part in enumerate(parts, 1):
        narration = _extract_part_text(part)

        attempted = _part_revision_status(
            part,
            index,
            revision_attempted,
        )

        result = validate_story_length(
            narration,
            policy,
            part_number=index,
            revision_attempted=attempted,
        )

        results.append(result)

        if result["needs_revision"]:
            retry_parts.append(index)

            errors.append(
                f"Part {index} has {result['actual_words']} words. "
                f"Required: {result['minimum_words']}-"
                f"{result['maximum_words']}. "
                f"Status: {result['status']}."
            )

    return {
        "policy_version": POLICY_VERSION,
        "format": normalize_format(
            policy.get("format", "short")
        ),
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
    """
    Select the final count after the complete story has been corrected.

    If config is supplied, update its original range and selected count
    without destroying the configured minimum and maximum.
    """

    minimum, maximum = parse_parts_range(
        parts_specification
    )

    selected = choose_part_count(
        total_corrected_words,
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
        config["PARTS_SELECTED"] = selected
        config["PARTS"] = selected

    return selected


def update_policy_with_corrected_story(
    policy: dict[str, Any],
    corrected_story: Any,
    *,
    selected_parts: Any = None,
) -> dict[str, Any]:
    """
    Recalculate policy from the corrected complete story.

    This must be called after story correction, not with the raw
    uncorrected input word count.
    """

    total_words = word_count(corrected_story)

    minimum = parse_positive_int(
        policy.get("configured_parts_min"),
        1,
        1,
        MAX_PARTS,
    )

    maximum = parse_positive_int(
        policy.get("configured_parts_max"),
        minimum,
        minimum,
        MAX_PARTS,
    )

    fmt = normalize_format(
        policy.get("format", "short")
    )

    if selected_parts is None:
        selected_parts = choose_part_count(
            total_words,
            fmt,
            f"{minimum}-{maximum}",
            policy.get("story_length_input", "auto"),
        )
    else:
        selected_parts = _validate_selected_count(
            selected_parts,
            minimum,
            maximum,
        )

    updated = build_length_policy(
        format_type=fmt,
        story_length=policy.get(
            "story_length_input",
            "auto",
        ),
        parts=f"{minimum}-{maximum}",
        scenes_per_part=policy.get(
            "scenes_per_part",
            DEFAULT_SCENES_PER_MINUTE,
        ),
        scene_duration=policy.get(
            "scene_duration_seconds",
            "auto",
        ),
        source_word_count=total_words,
        selected_parts=selected_parts,
    )

    updated["corrected_story_word_count"] = total_words
    updated["story_was_corrected_before_counting"] = True

    return updated


def write_policy(
    path: str | Path,
    policy: dict[str, Any],
) -> None:
    """Write policy JSON atomically."""
    destination = Path(path)
    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    try:
        temporary.write_text(
            json.dumps(
                policy,
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        temporary.replace(destination)

    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Calculate Katha Lok AI story length and part policy."
        )
    )

    parser.add_argument(
        "--format",
        default="short",
        choices=("short", "full"),
    )

    parser.add_argument(
        "--story-length",
        default="auto",
    )

    parser.add_argument(
        "--parts",
        default="1",
        help=(
            "Fixed count such as 4 or any valid inclusive range "
            "such as 2-8, 4-6, 8-11 or 12-18."
        ),
    )

    parser.add_argument(
        "--selected-parts",
        type=int,
        default=None,
        help="Optional final count already selected by the story policy.",
    )

    parser.add_argument(
        "--scenes",
        type=int,
        default=DEFAULT_SCENES_PER_MINUTE,
    )

    parser.add_argument(
        "--scene-duration",
        default="auto",
    )

    parser.add_argument(
        "--source-words",
        type=int,
        default=0,
        help="Word count of the corrected complete story.",
    )

    parser.add_argument(
        "--output",
        default="output/story/story_length_policy.json",
    )

    args = parser.parse_args()

    policy = build_length_policy(
        format_type=args.format,
        story_length=args.story_length,
        parts=args.parts,
        scenes_per_part=args.scenes,
        scene_duration=args.scene_duration,
        source_word_count=args.source_words,
        selected_parts=args.selected_parts,
    )

    write_policy(
        args.output,
        policy,
    )

    print(
        json.dumps(
            policy,
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())