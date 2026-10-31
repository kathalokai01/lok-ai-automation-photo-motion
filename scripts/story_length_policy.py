
#!/usr/bin/env python3
"""Katha Lok AI — dynamic short-part and full-story length policy."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

POLICY_VERSION = "story-length-policy-v3-dynamic-short-parts"

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
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default

    return max(minimum, min(maximum, number))


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


def calculate_dynamic_short_parts(total_words: Any) -> int:
    """Choose a feasible part count for 120–180 words per short part."""
    try:
        total = int(total_words)
    except (TypeError, ValueError):
        total = 0

    if total <= 0:
        return 1

    if total < SHORT_MIN_WORDS_PER_PART:
        return 1

    minimum_parts = math.ceil(
        total / SHORT_HARD_MAX_WORDS_PER_PART
    )
    maximum_parts = total // SHORT_MIN_WORDS_PER_PART

    if minimum_parts > maximum_parts:
        raise ValueError(
            f"{total} words cannot be divided into parts of "
            f"{SHORT_MIN_WORDS_PER_PART}–{SHORT_HARD_MAX_WORDS_PER_PART} "
            "words without rewriting the story."
        )

    preferred_parts = max(
        1,
        round(total / SHORT_TARGET_WORDS_PER_PART),
    )

    return max(
        minimum_parts,
        min(maximum_parts, preferred_parts),
    )


def resolve_duration_minutes(
    story_length: Any,
    format_type: Any,
    parts: Any = 1,
) -> float:
    fmt = normalize_format(format_type)
    length = clean_text(story_length).lower()

    if length in {"", "auto", "automatic", "default"}:
        part_count = parse_positive_int(parts, 1, 1, 10000)

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


def build_length_policy(
    *,
    format_type: Any,
    story_length: Any = "auto",
    parts: Any = 1,
    scenes_per_part: Any = DEFAULT_SCENES_PER_MINUTE,
    scene_duration: Any = "auto",
    source_word_count: Any = 0,
) -> dict[str, Any]:
    """Build a no-API story length policy."""
    fmt = normalize_format(format_type)

    configured_parts = parse_positive_int(parts, 1, 1, 10000)
    scene_count = parse_positive_int(
        scenes_per_part, DEFAULT_SCENES_PER_MINUTE, 1, 100
    )
    scene_seconds = parse_scene_seconds(scene_duration)

    try:
        source_words = int(source_word_count)
    except (TypeError, ValueError):
        source_words = 0

    source_words = max(0, min(source_words, 10_000_000))

    if fmt == "short":
        # When actual story text is available, ignore configured PARTS.
        # When no story exists yet, one provisional part is reported.
        part_count = (
            calculate_dynamic_short_parts(source_words)
            if source_words
            else 1
        )

        words_per_minute = SHORT_WORDS_PER_MINUTE
        minimum_per_part = SHORT_MIN_WORDS_PER_PART
        maximum_per_part = SHORT_HARD_MAX_WORDS_PER_PART
        target_words_per_part = SHORT_TARGET_WORDS_PER_PART

        minimum_total_words = part_count * minimum_per_part
        maximum_total_words = part_count * maximum_per_part
        target_total_words = part_count * target_words_per_part
    else:
        part_count = configured_parts
        words_per_minute = FULL_WORDS_PER_MINUTE
        minimum_per_part = None
        maximum_per_part = None

        target_minutes = resolve_duration_minutes(
            story_length, fmt, part_count
        )
        target_total_words = round(
            target_minutes * words_per_minute
        )
        target_words_per_part = max(
            1, round(target_total_words / part_count)
        )
        minimum_total_words = round(
            target_minutes * FULL_MIN_WORDS_PER_MINUTE
        )
        maximum_total_words = round(
            target_minutes * FULL_MAX_WORDS_PER_MINUTE
        )

    target_minutes = resolve_duration_minutes(
        story_length, fmt, part_count
    )

    total_scenes = part_count * scene_count
    visual_seconds = total_scenes * scene_seconds

    estimated_source_minutes = (
        source_words / words_per_minute if source_words else None
    )

    warnings = []

    if fmt == "short" and source_words:
        if source_words < minimum_total_words:
            warnings.append(
                "Story is too short for the minimum 120-word part. "
                "Rewrite or expand it without repeating content."
            )
        elif source_words > maximum_total_words:
            warnings.append(
                "Story exceeds the maximum 180-word-per-part capacity. "
                "Rewrite or expand the dynamic part count."
            )

    if visual_seconds < MIN_PART_SECONDS * part_count:
        warnings.append(
            "Configured scene duration may make the video too short."
        )

    if visual_seconds > MAX_AUTOMATIC_PART_SECONDS * part_count:
        warnings.append(
            "Configured scene duration may make the video unusually long."
        )

    return {
        "policy_version": POLICY_VERSION,
        "format": fmt,
        "story_length_input": clean_text(story_length) or "auto",
        "target_duration_minutes": round(target_minutes, 2),
        "parts_count": part_count,
        "configured_parts_count": configured_parts,
        "parts_count_is_dynamic": fmt == "short",
        "scenes_per_part": scene_count,
        "total_scenes": total_scenes,
        "scene_duration_seconds": scene_seconds,
        "estimated_visual_duration_minutes": round(
            visual_seconds / 60.0, 2
        ),
        "words_per_minute_target": words_per_minute,
        "target_total_words": target_total_words,
        "minimum_total_words": minimum_total_words,
        "maximum_total_words": maximum_total_words,
        "target_words_per_part": target_words_per_part,
        "minimum_words_per_part": minimum_per_part,
        "maximum_words_per_part": maximum_per_part,
        "hard_maximum_words_per_part": (
            SHORT_HARD_MAX_WORDS_PER_PART if fmt == "short" else None
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
        "suspense_at_part_end_required": True,
        "final_resolution_required": True,
        "warnings": warnings,
        "api_calls_made": 0,
        "duration_is_estimate": True,
        "note": (
            "SHORT uses dynamic part count from actual story word count. "
            "Each part targets 120–150 words and must never exceed 180. "
            "Sentence-boundary splitting and story rewriting are handled "
            "by the story-generation pipeline. FULL retains configured "
            "PARTS behavior."
        ),
    }


def validate_story_length(
    narration_text: Any,
    policy: dict[str, Any],
    *,
    part_number: int = 1,
) -> dict[str, Any]:
    """Validate an individual story part."""
    text = clean_text(narration_text)
    actual = word_count(text)
    fmt = normalize_format(policy.get("format", "short"))

    if fmt == "short":
        minimum = SHORT_MIN_WORDS_PER_PART
        maximum = SHORT_HARD_MAX_WORDS_PER_PART
        target = SHORT_TARGET_WORDS_PER_PART

        if actual < minimum:
            status = "too_short"
        elif actual > SHORT_MAX_WORDS_PER_PART:
            # 151–180 is permitted only as a sentence-boundary exception.
            status = "within_hard_limit_exception"
        else:
            status = "within_target"

        if actual > maximum:
            status = "too_long"
    else:
        target = parse_positive_int(
            policy.get("target_words_per_part"),
            FULL_WORDS_PER_MINUTE,
            1,
            1_000_000,
        )
        minimum = max(
            1,
            round(target * FULL_MIN_WORDS_PER_MINUTE /
                  FULL_WORDS_PER_MINUTE),
        )
        maximum = max(
            minimum,
            round(target * FULL_MAX_WORDS_PER_MINUTE /
                  FULL_WORDS_PER_MINUTE),
        )

        if actual < minimum:
            status = "too_short"
        elif actual > maximum:
            status = "too_long"
        else:
            status = "within_target"

    return {
        "part_number": parse_positive_int(part_number, 1, 1, 10000),
        "format": fmt,
        "actual_words": actual,
        "target_words": target,
        "minimum_words": minimum,
        "maximum_words": maximum,
        "hard_maximum_words": (
            SHORT_HARD_MAX_WORDS_PER_PART if fmt == "short" else maximum
        ),
        "status": status,
        "needs_revision": status in {"too_short", "too_long"},
        "text_modified": False,
    }


def write_policy(path: str | Path, policy: dict[str, Any]) -> None:
    """Atomically write the policy JSON file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")

    try:
        temporary.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Calculate a story-length policy."
    )
    parser.add_argument("--format", default="short")
    parser.add_argument("--story-length", default="auto")
    parser.add_argument("--parts", type=int, default=1)
    parser.add_argument("--scenes", type=int, default=11)
    parser.add_argument("--scene-duration", default="auto")
    parser.add_argument("--source-words", type=int, default=0)
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
    print(json.dumps(policy, ensure_ascii=False, indent=2))
