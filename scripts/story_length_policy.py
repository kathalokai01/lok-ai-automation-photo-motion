#!/usr/bin/env python3
"""Katha Lok AI — configured part-range and story-length policy."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

POLICY_VERSION = "story-length-policy-v4-configured-parts-range"

SHORT_WORDS_PER_MINUTE = 135
FULL_WORDS_PER_MINUTE = 125

SHORT_MIN_WORDS_PER_PART = 120
SHORT_TARGET_WORDS_PER_PART = 135
SHORT_MAX_WORDS_PER_PART = 150
SHORT_HARD_MAX_WORDS_PER_PART = 180

FULL_MIN_WORDS_PER_MINUTE = 100
FULL_MAX_WORDS_PER_MINUTE = 145

DEFAULT_SCENE_SECONDS = 6
MIN_SCENE_SECONDS = 3
MAX_SCENE_SECONDS = 12

DEFAULT_SCENES_PER_MINUTE = 10
MIN_PART_SECONDS = 30
MAX_AUTOMATIC_PART_SECONDS = 180

MAX_PARTS = 1000


def clean_text(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def word_count(value: Any) -> int:
    return len(clean_text(value).split())


def parse_positive_int(
    value: Any,
    default: int,
    minimum: int = 1,
    maximum: int = 10000,
) -> int:
    if isinstance(value, bool):
        return default

    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        number = default

    return max(minimum, min(maximum, number))


def parse_parts_range(value: Any) -> tuple[int, int]:
    """Accept PARTS=4 or PARTS=8-11."""
    if isinstance(value, bool):
        raise ValueError("PARTS must be a positive integer or range.")

    text = clean_text(value)

    match = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", text)

    if not match:
        raise ValueError(
            f"Invalid PARTS={value!r}. Use a fixed number "
            "such as 4 or an inclusive range such as 8-11."
        )

    minimum = int(match.group(1))
    maximum = int(match.group(2) or minimum)

    if minimum < 1 or maximum < 1:
        raise ValueError("PARTS values must be positive.")

    if minimum > maximum:
        raise ValueError(
            f"Invalid PARTS range {minimum}-{maximum}: "
            "minimum cannot exceed maximum."
        )

    if maximum > MAX_PARTS:
        raise ValueError(
            f"PARTS cannot exceed {MAX_PARTS}."
        )

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
        f"Unsupported FORMAT={value!r}; use FORMAT=short or FORMAT=full."
    )


def choose_short_parts(
    total_words: Any,
    minimum_parts: Any,
    maximum_parts: Any,
) -> int:
    """Choose a count inside the configured range, targeting 135 words."""
    total = parse_positive_int(
        total_words, 0, 0, 10_000_000
    )

    minimum, maximum = parse_parts_range(
        f"{minimum_parts}-{maximum_parts}"
    )

    if minimum == maximum:
        selected = minimum
        lower = selected * SHORT_MIN_WORDS_PER_PART
        upper = selected * SHORT_HARD_MAX_WORDS_PER_PART

        if not lower <= total <= upper:
            raise ValueError(
                f"{total} corrected story words cannot fit exactly "
                f"{selected} SHORT parts. Required total range: "
                f"{lower}-{upper} words."
            )

        return selected

    if total <= 0:
        return minimum

    feasible_minimum = max(
        minimum,
        math.ceil(total / SHORT_HARD_MAX_WORDS_PER_PART),
    )
    feasible_maximum = min(
        maximum,
        total // SHORT_MIN_WORDS_PER_PART,
    )

    if feasible_minimum > feasible_maximum:
        raise ValueError(
            f"{total} corrected story words cannot be divided into "
            f"the configured {minimum}-{maximum} SHORT parts. "
            f"Every part must contain at least "
            f"{SHORT_MIN_WORDS_PER_PART} and no more than "
            f"{SHORT_HARD_MAX_WORDS_PER_PART} words."
        )

    ideal = total / SHORT_TARGET_WORDS_PER_PART

    candidates = range(feasible_minimum, feasible_maximum + 1)

    return min(
        candidates,
        key=lambda count: (
            abs((total / count) - SHORT_TARGET_WORDS_PER_PART),
            abs(count - ideal),
            count,
        ),
    )


def calculate_dynamic_short_parts(
    total_words: Any,
    minimum_parts: Any = 1,
    maximum_parts: Any = MAX_PARTS,
) -> int:
    """Backward-compatible dynamic part selector."""
    return choose_short_parts(
        total_words,
        minimum_parts,
        maximum_parts,
    )


def resolve_duration_minutes(
    story_length: Any,
    format_type: Any,
    parts: Any = 1,
) -> float:
    fmt = normalize_format(format_type)
    length = clean_text(story_length).lower()

    if length in {"", "auto", "automatic", "default"}:
        part_count = parse_positive_int(parts, 1, 1, MAX_PARTS)

        if fmt == "short":
            return float(part_count)

        return float(max(1, part_count) * 3)

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)?",
        length,
    )

    if not match:
        raise ValueError(
            "STORY_LENGTH must be 'auto' or a positive duration in minutes."
        )

    minutes = float(match.group(1))

    if not 0 < minutes <= 600:
        raise ValueError(
            "STORY_LENGTH must be greater than 0 and at most 600."
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
    """Resolve explicit range arguments or the PARTS input value."""
    if parts_min is not None or parts_max is not None:
        if parts_min is None or parts_max is None:
            raise ValueError(
                "Both parts_min and parts_max must be supplied together."
            )

        return parse_parts_range(f"{parts_min}-{parts_max}")

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
) -> dict[str, Any]:
    """Build a deterministic policy without making model/API calls."""
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

    source_words = parse_positive_int(
        source_word_count,
        0,
        0,
        10_000_000,
    )

    if fmt == "short":
        part_count = choose_short_parts(
            source_words,
            minimum_parts,
            maximum_parts,
        )

        words_per_minute = SHORT_WORDS_PER_MINUTE
        minimum_per_part = SHORT_MIN_WORDS_PER_PART
        maximum_per_part = SHORT_HARD_MAX_WORDS_PER_PART
        target_words_per_part = SHORT_TARGET_WORDS_PER_PART

        minimum_total_words = (
            part_count * minimum_per_part
        )
        maximum_total_words = (
            part_count * maximum_per_part
        )
        target_total_words = (
            part_count * target_words_per_part
        )

    else:
        if minimum_parts != maximum_parts:
            raise ValueError(
                "PARTS ranges are supported for SHORT only. "
                "Use one fixed PARTS number for FULL."
            )

        part_count = minimum_parts
        words_per_minute = FULL_WORDS_PER_MINUTE
        minimum_per_part = None
        maximum_per_part = None

        target_minutes = resolve_duration_minutes(
            story_length,
            fmt,
            part_count,
        )

        target_total_words = round(
            target_minutes * words_per_minute
        )

        target_words_per_part = max(
            1,
            round(target_total_words / part_count),
        )

        minimum_total_words = round(
            target_minutes * FULL_MIN_WORDS_PER_MINUTE
        )

        maximum_total_words = round(
            target_minutes * FULL_MAX_WORDS_PER_MINUTE
        )

    target_minutes = resolve_duration_minutes(
        story_length,
        fmt,
        part_count,
    )

    total_scenes = part_count * scene_count
    visual_seconds = total_scenes * scene_seconds

    estimated_source_minutes = (
        source_words / words_per_minute
        if source_words else None
    )

    warnings: list[str] = []

    if fmt == "short" and source_words:
        if source_words < minimum_total_words:
            warnings.append(
                "Corrected story is below the minimum word capacity "
                "for the selected part count. Expand or rewrite it."
            )

        elif source_words > maximum_total_words:
            warnings.append(
                "Corrected story exceeds the hard maximum capacity "
                "for the selected part count. Rewrite or select a "
                "larger permitted part count."
            )

    if visual_seconds < MIN_PART_SECONDS * part_count:
        warnings.append(
            "Configured scene duration may make the video too short."
        )

    if visual_seconds > MAX_AUTOMATIC_PART_SECONDS * part_count:
        warnings.append(
            "Configured scene duration may make the video unusually long."
        )

    feasible_minimum = (
        max(
            minimum_parts,
            math.ceil(
                source_words / SHORT_HARD_MAX_WORDS_PER_PART
            ),
        )
        if fmt == "short" and source_words else minimum_parts
    )

    feasible_maximum = (
        min(
            maximum_parts,
            source_words // SHORT_MIN_WORDS_PER_PART,
        )
        if fmt == "short" and source_words else maximum_parts
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
        "feasible_parts_min": feasible_minimum,
        "feasible_parts_max": feasible_maximum,
        "parts_count_is_dynamic": (
            fmt == "short" and minimum_parts != maximum_parts
        ),
        "parts_count_selected_from_range": (
            fmt == "short" and minimum_parts != maximum_parts
        ),

        "scenes_per_part": scene_count,
        "total_scenes": total_scenes,
        "scene_duration_seconds": scene_seconds,
        "estimated_visual_duration_minutes": round(
            visual_seconds / 60.0,
            2,
        ),

        "words_per_minute_target": words_per_minute,
        "target_total_words": target_total_words,
        "minimum_total_words": minimum_total_words,
        "maximum_total_words": maximum_total_words,
        "target_words_per_part": target_words_per_part,
        "minimum_words_per_part": minimum_per_part,
        "maximum_words_per_part": (
            SHORT_MAX_WORDS_PER_PART if fmt == "short"
            else maximum_per_part
        ),
        "hard_maximum_words_per_part": (
            SHORT_HARD_MAX_WORDS_PER_PART if fmt == "short"
            else maximum_per_part
        ),

        "source_word_count": source_words,
        "estimated_source_narration_minutes": (
            round(estimated_source_minutes, 2)
            if estimated_source_minutes is not None else None
        ),
        "source_word_count_within_target": (
            minimum_total_words <= source_words <= maximum_total_words
            if source_words else None
        ),

        "hook_required": True,
        "separate_hook_scene_required": True,
        "separate_suspense_scene_required": True,
        "suspense_at_part_end_required": True,
        "final_part_uses_resolution": True,
        "final_resolution_required": True,

        "retry_parts_above_target": True,
        "retry_parts_below_minimum": True,
        "hard_fail_below_minimum_after_retry": True,
        "hard_fail_above_hard_maximum_after_retry": True,

        "warnings": warnings,
        "api_calls_made": 0,
        "duration_is_estimate": True,

        "note": (
            "SHORT uses the corrected full-story word count to select "
            "a part count inside the configured PARTS range. Each part "
            "targets 120–150 words. Parts from 151–180 words are "
            "permitted only as a hard-limit exception after revision. "
            "Parts below 120 or above 180 words must fail final validation. "
            "FULL uses its fixed configured PARTS count."
        ),
    }

def choose_short_parts(
    total_words: Any,
    minimum_parts: Any,
    maximum_parts: Any,
) -> int:
    """Select a valid count; an empty source uses the minimum provisionally."""
    total = parse_positive_int(
        total_words, 0, 0, 10_000_000
    )

    minimum, maximum = parse_parts_range(
        f"{minimum_parts}-{maximum_parts}"
    )

    if total == 0:
        return minimum

    if minimum == maximum:
        lower = minimum * SHORT_MIN_WORDS_PER_PART
        upper = minimum * SHORT_HARD_MAX_WORDS_PER_PART

        if not lower <= total <= upper:
            raise ValueError(
                f"{total} corrected story words cannot fit exactly "
                f"{minimum} SHORT parts. Required total range: "
                f"{lower}-{upper} words."
            )

        return minimum

    feasible_minimum = max(
        minimum,
        math.ceil(total / SHORT_HARD_MAX_WORDS_PER_PART),
    )
    feasible_maximum = min(
        maximum,
        total // SHORT_MIN_WORDS_PER_PART,
    )

    if feasible_minimum > feasible_maximum:
        raise ValueError(
            f"{total} corrected story words cannot be divided into "
            f"the configured {minimum}-{maximum} SHORT parts. "
            f"Every part must contain 120–180 words."
        )

    ideal = total / SHORT_TARGET_WORDS_PER_PART

    return min(
        range(feasible_minimum, feasible_maximum + 1),
        key=lambda count: (
            abs((total / count) - SHORT_TARGET_WORDS_PER_PART),
            abs(count - ideal),
            count,
        ),
    )


def validate_story_length(
    narration_text: Any,
    policy: dict[str, Any],
    *,
    part_number: int = 1,
) -> dict[str, Any]:
    """Validate one narration part using the shared policy."""
    text = clean_text(narration_text)
    actual = word_count(text)
    fmt = normalize_format(policy.get("format", "short"))

    if fmt == "short":
        minimum = SHORT_MIN_WORDS_PER_PART
        target = SHORT_TARGET_WORDS_PER_PART
        preferred_maximum = SHORT_MAX_WORDS_PER_PART
        hard_maximum = SHORT_HARD_MAX_WORDS_PER_PART

        if actual == 0:
            status = "missing_narration"
        elif actual < minimum:
            status = "too_short"
        elif actual > hard_maximum:
            status = "too_long"
        elif actual > preferred_maximum:
            status = "within_hard_limit_exception"
        else:
            status = "within_target"

        needs_revision = status in {
            "missing_narration",
            "too_short",
            "too_long",
        }

    else:
        target = parse_positive_int(
            policy.get("target_words_per_part"),
            FULL_WORDS_PER_MINUTE,
            1,
            1_000_000,
        )

        minimum = max(
            1,
            round(
                target * FULL_MIN_WORDS_PER_MINUTE
                / FULL_WORDS_PER_MINUTE
            ),
        )

        hard_maximum = max(
            minimum,
            round(
                target * FULL_MAX_WORDS_PER_MINUTE
                / FULL_WORDS_PER_MINUTE
            ),
        )

        preferred_maximum = hard_maximum

        if actual == 0:
            status = "missing_narration"
        elif actual < minimum:
            status = "too_short"
        elif actual > hard_maximum:
            status = "too_long"
        else:
            status = "within_target"

        needs_revision = status in {
            "missing_narration",
            "too_short",
            "too_long",
        }

    return {
        "part_number": parse_positive_int(
            part_number, 1, 1, MAX_PARTS
        ),
        "format": fmt,
        "actual_words": actual,
        "target_words": target,
        "minimum_words": minimum,
        "preferred_maximum_words": preferred_maximum,
        "maximum_words": hard_maximum,
        "hard_maximum_words": hard_maximum,
        "status": status,
        "needs_revision": needs_revision,
        "text_modified": False,
        "retry_required": needs_revision,
        "final_validation_passed": not needs_revision,
    }


def validate_all_parts(
    parts: Any,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Validate every part and enforce the selected part count."""
    if not isinstance(parts, list):
        raise TypeError("Story parts must be supplied as a list.")

    expected = parse_positive_int(
        policy.get("parts_count"),
        1,
        1,
        MAX_PARTS,
    )

    results = []

    for index, part in enumerate(parts, 1):
        if isinstance(part, dict):
            narration = (
                part.get("narration")
                or part.get("story")
                or part.get("text")
                or part.get("content")
                or ""
            )
        else:
            narration = part

        results.append(
            validate_story_length(
                narration,
                policy,
                part_number=index,
            )
        )

    errors = []

    if len(parts) != expected:
        errors.append(
            f"Selected part count is {expected}, "
            f"but generated part count is {len(parts)}."
        )

    for result in results:
        if result["needs_revision"]:
            errors.append(
                f"Part {result['part_number']} has "
                f"{result['actual_words']} words "
                f"(required {result['minimum_words']}-"
                f"{result['maximum_words']})."
            )

    return {
        "policy_version": POLICY_VERSION,
        "expected_parts": expected,
        "actual_parts": len(parts),
        "valid": not errors,
        "parts": results,
        "errors": errors,
    }


def write_policy(
    path: str | Path,
    policy: dict[str, Any],
) -> None:
    """Atomically write the policy JSON."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

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
        description="Calculate and save the story-length policy."
    )

    parser.add_argument(
        "--format",
        default="short",
    )
    parser.add_argument(
        "--story-length",
        default="auto",
    )
    parser.add_argument(
        "--parts",
        default="1",
        help="Fixed count such as 4 or inclusive range such as 8-11.",
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
    )

    write_policy(args.output, policy)

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