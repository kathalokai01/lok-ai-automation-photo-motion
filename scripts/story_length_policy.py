#!/usr/bin/env python3
"""Shared, deterministic story-length and duration policy for Katha Lok AI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


POLICY_VERSION = "story-length-policy-v1"

# Hindi narration speed varies substantially by voice and punctuation.
# These values are planning targets, not guarantees of final audio duration.
SHORT_WORDS_PER_MINUTE = 135
FULL_WORDS_PER_MINUTE = 125

SHORT_MIN_WORDS_PER_MINUTE = 120
SHORT_MAX_WORDS_PER_MINUTE = 150

DEFAULT_SCENE_SECONDS = 6
MIN_SCENE_SECONDS = 3
MAX_SCENE_SECONDS = 12

DEFAULT_SCENES_PER_MINUTE = 10
MIN_PART_SECONDS = 30
MAX_AUTOMATIC_PART_SECONDS = 180


def clean_text(value: Any) -> str:
    """Normalize whitespace without removing Hindi characters."""
    text = str(value or "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def word_count(value: Any) -> int:
    """Count whitespace-separated words in Hindi, English, or Hinglish."""
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
        return default

    return max(minimum, min(maximum, number))


def normalize_format(value: Any) -> str:
    """Accept common aliases and reject ambiguous formats."""
    text = clean_text(value).lower()

    if text in {"short", "shorts", "reel", "reels", "vertical"}:
        return "short"

    if text in {
        "full",
        "long",
        "longform",
        "long-form",
        "landscape",
        "full_video",
    }:
        return "full"

    raise ValueError(
        f"Unsupported FORMAT={value!r}; use FORMAT=short or FORMAT=full."
    )


def resolve_duration_minutes(
    story_length: Any,
    format_type: Any,
    parts: Any = 1,
) -> float:
    """
    Resolve a target duration in minutes.

    A numeric STORY_LENGTH is interpreted as minutes.
    'auto' uses the selected format and requested part count.
    """
    fmt = normalize_format(format_type)
    length = clean_text(story_length).lower()

    if length in {"", "auto", "automatic", "default"}:
        part_count = parse_positive_int(parts, 1, 1, 100)

        if fmt == "short":
            return float(part_count)

        # Full format defaults to one complete long-form video.
        # Do not silently turn an explicitly requested multi-part story
        # into multiple separate full videos.
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

    if minutes <= 0 or minutes > 600:
        raise ValueError("STORY_LENGTH must be between 0 and 600 minutes.")

    return minutes


def build_length_policy(
    *,
    format_type: Any,
    story_length: Any = "auto",
    parts: Any = 1,
    scenes_per_part: Any = DEFAULT_SCENES_PER_MINUTE,
    scene_duration: Any = "auto",
    source_word_count: Any = 0,
) -> dict[str, Any]:
    """
    Build a JSON-serializable story-length plan.

    This function does not call any API, generate story text, modify files,
    or guarantee that a language model follows the targets.
    """
    fmt = normalize_format(format_type)
    part_count = parse_positive_int(parts, 1, 1, 100)
    requested_scenes = parse_positive_int(
        scenes_per_part,
        DEFAULT_SCENES_PER_MINUTE,
        1,
        100,
    )

    duration_text = clean_text(scene_duration).lower()

    if duration_text in {"", "auto", "automatic", "default"}:
        scene_seconds = DEFAULT_SCENE_SECONDS
    else:
        match = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds)?",
            duration_text,
        )
        if not match:
            raise ValueError(
                "SCENE_DURATION must be 'auto' or a duration in seconds."
            )
        scene_seconds = float(match.group(1))
        if not MIN_SCENE_SECONDS <= scene_seconds <= MAX_SCENE_SECONDS:
            raise ValueError(
                f"SCENE_DURATION must be between "
                f"{MIN_SCENE_SECONDS} and {MAX_SCENE_SECONDS} seconds."
            )

    target_minutes = resolve_duration_minutes(
        story_length,
        fmt,
        part_count,
    )

    if fmt == "short":
        words_per_minute = SHORT_WORDS_PER_MINUTE
        min_words_per_minute = SHORT_MIN_WORDS_PER_MINUTE
        max_words_per_minute = SHORT_MAX_WORDS_PER_MINUTE
    else:
        words_per_minute = FULL_WORDS_PER_MINUTE
        min_words_per_minute = 100
        max_words_per_minute = 145

    target_total_words = round(target_minutes * words_per_minute)

    # Preserve explicitly requested parts. Estimate a per-part word target
    # without pretending that every part has identical narrative complexity.
    target_words_per_part = max(
        1,
        round(target_total_words / part_count),
    )

    total_scenes = part_count * requested_scenes
    estimated_visual_minutes = (
        total_scenes * scene_seconds / 60.0
    )

    source_words = parse_positive_int(
        source_word_count,
        0,
        0,
        10_000_000,
    )

    if source_words:
        estimated_source_minutes = source_words / words_per_minute
    else:
        estimated_source_minutes = None

    return {
        "policy_version": POLICY_VERSION,
        "format": fmt,
        "story_length_input": clean_text(story_length) or "auto",
        "target_duration_minutes": round(target_minutes, 2),
        "parts_count": part_count,
        "scenes_per_part": requested_scenes,
        "total_scenes": total_scenes,
        "scene_duration_seconds": scene_seconds,
        "estimated_visual_duration_minutes": round(
            estimated_visual_minutes, 2
        ),
        "words_per_minute_target": words_per_minute,
        "words_per_minute_minimum": min_words_per_minute,
        "words_per_minute_maximum": max_words_per_minute,
        "target_total_words": target_total_words,
        "target_words_per_part": target_words_per_part,
        "source_word_count": source_words,
        "estimated_source_narration_minutes": (
            round(estimated_source_minutes, 2)
            if estimated_source_minutes is not None
            else None
        ),
        "hook_required": True,
        "suspense_at_part_end_required": True,
        "final_resolution_required": True,
        "api_calls_made": 0,
        "duration_is_estimate": True,
    }


def validate_story_length(
    narration_text: Any,
    policy: dict[str, Any],
    *,
    part_number: int = 1,
) -> dict[str, Any]:
    """Report word-count compliance without silently rewriting story text."""
    text = clean_text(narration_text)
    actual = word_count(text)

    target = parse_positive_int(
        policy.get("target_words_per_part"),
        135,
        1,
        1_000_000,
    )

    minimum_rate = parse_positive_int(
        policy.get("words_per_minute_minimum"),
        SHORT_MIN_WORDS_PER_MINUTE,
        1,
        1000,
    )
    maximum_rate = parse_positive_int(
        policy.get("words_per_minute_maximum"),
        SHORT_MAX_WORDS_PER_MINUTE,
        minimum_rate,
        1000,
    )

    # Scale the permitted range around the per-part target. This is a
    # diagnostic tolerance, not a claim that every story needs exact counts.
    lower = max(1, round(target * minimum_rate / SHORT_WORDS_PER_MINUTE))
    upper = max(lower, round(target * maximum_rate / SHORT_WORDS_PER_MINUTE))

    if actual < lower:
        status = "too_short"
    elif actual > upper:
        status = "too_long"
    else:
        status = "within_target"

    return {
        "part_number": parse_positive_int(part_number, 1, 1, 10000),
        "actual_words": actual,
        "target_words": target,
        "minimum_words": lower,
        "maximum_words": upper,
        "status": status,
        "needs_revision": status != "within_target",
        "text_modified": False,
    }


def write_policy(path: str | Path, policy: dict[str, Any]) -> None:
    """Write the policy atomically so interrupted runs do not leave partial JSON."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")

    temporary.write_text(
        json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Calculate a no-API story-length planning policy."
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

    result = build_length_policy(
        format_type=args.format,
        story_length=args.story_length,
        parts=args.parts,
        scenes_per_part=args.scenes,
        scene_duration=args.scene_duration,
        source_word_count=args.source_words,
    )

    write_policy(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))