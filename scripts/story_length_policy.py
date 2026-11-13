#!/usr/bin/env python3
"""Central SHORT/FULL story length policy and verified revision rules."""

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

POLICY_VERSION = "story-length-policy-v12-real-final-merge-only"

SHORT_MIN_WORDS_PER_PART = 120
SHORT_TARGET_WORDS_PER_PART = 135
SHORT_PREFERRED_MAX_WORDS_PER_PART = 150
SHORT_HARD_MAX_WORDS_PER_PART = 180
SHORT_FINAL_MERGED_MAX_WORDS = 239
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
MAX_SCENES_PER_PART = 1000
MAX_REVISION_ATTEMPTS = 3


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text).strip()


def word_count(value: Any) -> int:
    return len(clean_text(value).split())


def sha256_text(value: Any) -> str:
    return hashlib.sha256(
        clean_text(value).encode("utf-8")
    ).hexdigest()


def _strict_integer(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")

    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(
        r"\s*\d+\s*", value
    ):
        number = int(value.strip())
    else:
        raise ValueError(f"{name} must be an integer.")

    if not minimum <= number <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}."
        )

    return number


def parse_positive_int(
    value: Any,
    default: int,
    minimum: int = 1,
    maximum: int = MAX_STORY_WORDS,
) -> int:
    try:
        return _strict_integer(
            value,
            name="Value",
            minimum=minimum,
            maximum=maximum,
        )
    except (TypeError, ValueError):
        return default


def parse_parts_range(value: Any) -> tuple[int, int]:
    if isinstance(value, bool):
        raise ValueError("PARTS must be a positive integer or range.")

    match = re.fullmatch(
        r"(\d+)\s*(?:-\s*(\d+))?",
        clean_text(value),
    )
    if not match:
        raise ValueError(
            f"Invalid PARTS={value!r}; use 4 or an inclusive range such as 2-8."
        )

    minimum = int(match.group(1))
    maximum = int(match.group(2) or minimum)

    if minimum < 1 or maximum < 1:
        raise ValueError("PARTS values must be positive.")
    if minimum > maximum:
        raise ValueError(
            f"Reversed PARTS range is invalid: {minimum}-{maximum}."
        )
    if maximum > MAX_PARTS:
        raise ValueError(f"PARTS cannot exceed {MAX_PARTS}.")

    return minimum, maximum


def normalize_format(value: Any) -> str:
    text = clean_text(value).lower().replace("_", "-")

    if text in {"short", "shorts", "reel", "reels", "vertical"}:
        return "short"
    if text in {
        "full", "long", "longform", "long-form",
        "landscape", "full-video",
    }:
        return "full"

    raise ValueError(f"Unsupported FORMAT={value!r}; use short or full.")


def _validate_total_words(total_words: Any) -> int:
    return _strict_integer(
        total_words,
        name="Story word count",
        minimum=0,
        maximum=MAX_STORY_WORDS,
    )


def _validate_selected_count(
    selected: Any,
    minimum: int,
    maximum: int,
) -> int:
    count = _strict_integer(
        selected,
        name="Selected part count",
        minimum=1,
        maximum=MAX_PARTS,
    )
    if not minimum <= count <= maximum:
        raise ValueError(
            f"Selected count {count} is outside {minimum}-{maximum}."
        )
    return count


def _resolve_part_bounds(
    parts: Any,
    parts_min: Any = None,
    parts_max: Any = None,
) -> tuple[int, int]:
    if parts_min is not None or parts_max is not None:
        if parts_min is None or parts_max is None:
            raise ValueError("Both parts_min and parts_max are required.")

        minimum = _strict_integer(
            parts_min, name="parts_min", minimum=1, maximum=MAX_PARTS
        )
        maximum = _strict_integer(
            parts_max, name="parts_max", minimum=1, maximum=MAX_PARTS
        )

        if minimum > maximum:
            raise ValueError(
                f"Reversed PARTS range is invalid: {minimum}-{maximum}."
            )

        if parts not in (None, ""):
            if parse_parts_range(parts) != (minimum, maximum):
                raise ValueError("PARTS and explicit bounds disagree.")

        return minimum, maximum

    if parts in (None, ""):
        raise ValueError("A PARTS count or range is required.")

    return parse_parts_range(parts)


def choose_short_parts(
    total_words: Any,
    minimum_parts: Any,
    maximum_parts: Any,
) -> int:
    """Choose a count that can fit the final-Part merge exception."""
    total = _validate_total_words(total_words)
    minimum, maximum = _resolve_part_bounds(
        None, minimum_parts, maximum_parts
    )

    if total <= 0:
        raise ValueError("The corrected SHORT story is empty.")

    feasible = []

    for count in range(minimum, maximum + 1):
        if count == 1:
            lower = SHORT_MIN_WORDS_PER_PART
            upper = SHORT_FINAL_MERGED_MAX_WORDS
        else:
            lower = (
                (count - 1) * SHORT_MIN_WORDS_PER_PART + 1
            )
            upper = (
                (count - 1) * SHORT_HARD_MAX_WORDS_PER_PART
                + SHORT_FINAL_MERGED_MAX_WORDS
            )

        if lower <= total <= upper:
            feasible.append(count)

    if not feasible:
        raise ValueError(
            f"{total} words cannot fit PARTS range {minimum}-{maximum} "
            "under the 120-180 normal Part and 239-word final merge rules."
        )

    return min(
        feasible,
        key=lambda count: (
            abs(total / count - SHORT_TARGET_WORDS_PER_PART),
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
    minimum_parts: Any = 1,
    maximum_parts: Any = 1,
    story_length: Any = "auto",
) -> int:
    """FULL is always one complete story Part, without length limits."""
    _validate_total_words(total_words)
    minimum, maximum = _resolve_part_bounds(
        None, minimum_parts, maximum_parts
    )

    if minimum != 1 or maximum != 1:
        raise ValueError(
            "FULL mode requires exactly one Part; set PARTS=1."
        )

    return 1


def choose_part_count(
    total_words: Any,
    format_type: Any,
    parts: Any,
    story_length: Any = "auto",
) -> int:
    fmt = normalize_format(format_type)
    minimum, maximum = parse_parts_range(parts)

    if fmt == "full":
        return choose_full_parts(
            total_words, minimum, maximum, story_length
        )

    return choose_short_parts(total_words, minimum, maximum)


def resolve_duration_minutes(
    story_length: Any,
    format_type: Any,
    parts: Any = 1,
) -> float:
    """Compatibility estimate; FULL duration is never validated or capped."""
    fmt = normalize_format(format_type)
    length = clean_text(story_length).lower()

    if fmt == "full":
        return 0.0

    if length in {"", "auto", "automatic", "default"}:
        count = _strict_integer(
            parts, name="PARTS", minimum=1, maximum=MAX_PARTS
        )
        return float(count)

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)?",
        length,
    )
    if not match:
        raise ValueError(
            "SHORT STORY_LENGTH must be auto or a duration in minutes."
        )

    minutes = float(match.group(1))
    if not math.isfinite(minutes) or not 0 < minutes <= 600:
        raise ValueError("SHORT STORY_LENGTH must be greater than 0 and at most 600.")
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
        raise ValueError("SCENE_DURATION must be auto or seconds.")

    seconds = float(match.group(1))
    if (
        not math.isfinite(seconds)
        or not MIN_SCENE_SECONDS <= seconds <= MAX_SCENE_SECONDS
    ):
        raise ValueError(
            f"SCENE_DURATION must be {MIN_SCENE_SECONDS}-{MAX_SCENE_SECONDS} seconds."
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
    fmt = normalize_format(format_type)
    minimum_parts, maximum_parts = _resolve_part_bounds(
        parts, parts_min, parts_max
    )

    if fmt == "full" and (minimum_parts, maximum_parts) != (1, 1):
        raise ValueError("FULL mode requires exactly one Part.")

    scene_count = _strict_integer(
        scenes_per_part,
        name="SCENES",
        minimum=1,
        maximum=MAX_SCENES_PER_PART,
    )
    scene_seconds = parse_scene_seconds(scene_duration)
    source_words = _validate_total_words(source_word_count)

    if selected_parts is not None:
        part_count = _validate_selected_count(
            selected_parts, minimum_parts, maximum_parts
        )
    elif fmt == "full":
        part_count = 1
    elif source_words:
        part_count = choose_short_parts(
            source_words, minimum_parts, maximum_parts
        )
    else:
        part_count = minimum_parts

    if fmt == "full" and part_count != 1:
        raise ValueError("FULL mode must contain exactly one Part.")

    if fmt == "short":
        minimum_per_part = SHORT_MIN_WORDS_PER_PART
        target_per_part = SHORT_TARGET_WORDS_PER_PART
        preferred_max_per_part = SHORT_PREFERRED_MAX_WORDS_PER_PART
        hard_max_per_part = SHORT_HARD_MAX_WORDS_PER_PART
        words_per_minute = SHORT_WORDS_PER_MINUTE

        minimum_total = (
            (part_count - 1) * SHORT_MIN_WORDS_PER_PART + 1
        )
        maximum_total = (
            (part_count - 1) * SHORT_HARD_MAX_WORDS_PER_PART
            + SHORT_FINAL_MERGED_MAX_WORDS
        )
        preferred_total = part_count * SHORT_PREFERRED_MAX_WORDS_PER_PART
        target_total = part_count * SHORT_TARGET_WORDS_PER_PART
    else:
        minimum_per_part = None
        target_per_part = None
        preferred_max_per_part = None
        hard_max_per_part = None
        words_per_minute = FULL_WORDS_PER_MINUTE
        minimum_total = None
        maximum_total = None
        preferred_total = None
        target_total = None

    estimated_minutes = (
        source_words / words_per_minute if source_words and words_per_minute else None
    )

    warnings = []
    if fmt == "short" and source_words:
        if source_words < minimum_total or source_words > maximum_total:
            warnings.append(
                "Corrected story needs rebalancing or a valid PARTS selection."
            )
        if source_words > preferred_total:
            warnings.append(
                "Parts above 150 words require a real revision attempt; 180 is the normal hard limit."
            )

    return {
        "policy_version": POLICY_VERSION,
        "format": fmt,
        "story_length_input": clean_text(story_length) or "auto",
        "target_duration_minutes": (
            round(estimated_minutes, 2) if estimated_minutes is not None else None
        ),
        "parts_count": part_count,
        "configured_parts_count": part_count,
        "configured_parts_min": minimum_parts,
        "configured_parts_max": maximum_parts,
        "feasible_parts_min": minimum_parts,
        "feasible_parts_max": maximum_parts,
        "parts_count_is_dynamic": minimum_parts != maximum_parts,
        "parts_count_selected_from_range": (
            minimum_parts != maximum_parts and source_words > 0
        ),
        "parts_count_is_provisional": (
            source_words == 0 and minimum_parts != maximum_parts
        ),
        "scenes_per_part": scene_count,
        "total_scenes": part_count * scene_count,
        "scene_duration_seconds": scene_seconds,
        "estimated_visual_duration_minutes": (
            round(part_count * scene_count * scene_seconds / 60, 2)
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
        "final_part_minimum_words": SHORT_MIN_WORDS_PER_PART if fmt == "short" else None,
        "final_part_merged_maximum_words": SHORT_FINAL_MERGED_MAX_WORDS if fmt == "short" else None,
        "source_word_count": source_words,
        "corrected_story_word_count": source_words,
        "estimated_source_narration_minutes": (
            round(estimated_minutes, 2) if estimated_minutes is not None else None
        ),
        "source_word_count_within_hard_limit": (
            minimum_total <= source_words <= maximum_total
            if fmt == "short" and source_words else None
        ),
        "hook_required": True,
        "separate_hook_scene_required": True,
        "separate_suspense_scene_required": True,
        "suspense_at_nonfinal_part_end_required": True,
        "final_part_uses_resolution": True,
        "final_resolution_required": True,
        "revise_parts_above_150": fmt == "short",
        "hard_fail_below_120": False,
        "hard_fail_above_180": fmt == "short",
        "retry_before_final_validation": True,
        "maximum_revision_attempts": MAX_REVISION_ATTEMPTS,
        "final_part_retry_then_merge_required": fmt == "short",
        "preceding_part_merge_maximum_words": (
            SHORT_FINAL_MERGED_MAX_WORDS if fmt == "short" else None
        ),
        "revision_attempted": False,
        "revision_outcome": "not_started",
        "revision_parts": [],
        "revision_results": [],
        "revision_errors": [],
        "final_part_validation_passed": False,
        "warnings": warnings,
        "api_calls_made": 0,
        "duration_is_estimate": fmt == "short",
        "note": (
            "SHORT: 120-150 target; 151-180 accepted only after verified revision. "
            "If the final Part remains below 120 after retry/rebalance, merge all "
            "remaining final text into the preceding Part, maximum 239 words. "
            "FULL: exactly one complete Part; no word-count or duration limit."
        ),
    }


def validate_story_quality_report(report: Any, minimum_score: int = 8) -> tuple[bool, list[str]]:
    """Validate the persisted deterministic Story quality report."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return False, ["Story quality report is missing or invalid."]
    if report.get("passed") is not True or report.get("status") != "passed":
        errors.extend(
            str(item) for item in report.get("issues", [])
            if str(item).strip()
        )
        if not errors:
            errors.append("Story quality report did not pass.")
    try:
        score = float(report.get("score"))
    except (TypeError, ValueError):
        score = 0.0
    if score < minimum_score:
        errors.append(
            f"Story quality score {score:g} is below required {minimum_score}."
        )
    parts = report.get("parts")
    expected = report.get("part_count")
    validated = report.get("validated_part_count")
    if (
        not isinstance(parts, list)
        or not isinstance(expected, int)
        or isinstance(expected, bool)
        or not isinstance(validated, int)
        or isinstance(validated, bool)
        or expected < 1
        or validated != expected
        or len(parts) != expected
    ):
        errors.append("Story quality report Part counts are inconsistent.")
    elif any(
        not isinstance(item, dict)
        or item.get("passed") is not True
        or item.get("status") != "passed"
        for item in parts
    ):
        errors.append("One or more Story Parts failed quality validation.")
    return not errors, errors


def validate_story_length(
    narration_text: Any,
    policy: dict[str, Any],
    *,
    part_number: int = 1,
    revision_attempted: bool = False,
    revision_evidence: dict[str, Any] | None = None,
    is_final_part: bool = False,
    final_part_merged: bool = False,
) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise TypeError("Story length policy must be a dictionary.")

    text = clean_text(narration_text)
    actual = word_count(text)
    fmt = normalize_format(policy.get("format", "short"))
    number = _strict_integer(
        part_number,
        name="part_number",
        minimum=1,
        maximum=MAX_PARTS,
    )

    if fmt == "full":
        passed = bool(text)
        return {
            "part_number": 1,
            "format": "full",
            "actual_words": actual,
            "target_words": None,
            "minimum_words": None,
            "preferred_maximum_words": None,
            "maximum_words": None,
            "hard_maximum_words": None,
            "revision_attempted": False,
            "revision_evidence_verified": False,
            "final_part_merged": False,
            "status": "valid_full_story" if passed else "missing_narration",
            "needs_revision": not passed,
            "retry_required": not passed,
            "passed": passed,
            "final_validation_passed": passed,
            "text_modified": False,
        }

    evidence_ok = verify_revision_evidence(
        revision_evidence,
        text,
    )

    if not text:
        status = "missing_narration"
        passed = False
        needs_revision = True

    elif final_part_merged:
        if not is_final_part:
            status = "merge_exception_not_final_part"
            passed = False
            needs_revision = True
        elif SHORT_MIN_WORDS_PER_PART <= actual <= SHORT_FINAL_MERGED_MAX_WORDS:
            status = "valid_final_merge_exception"
            passed = True
            needs_revision = False
        else:
            status = "final_merge_outside_120_239"
            passed = False
            needs_revision = True

    elif actual < SHORT_MIN_WORDS_PER_PART:
        status = (
            "final_part_below_minimum"
            if is_final_part
            else "below_minimum"
        )
        passed = False
        needs_revision = True

    elif actual > SHORT_HARD_MAX_WORDS_PER_PART:
        status = "above_hard_maximum"
        passed = False
        needs_revision = True

    elif actual > SHORT_PREFERRED_MAX_WORDS_PER_PART:
        status = (
            "within_hard_limit_after_verified_revision"
            if evidence_ok
            else "revision_required_above_150"
        )
        passed = evidence_ok
        needs_revision = not evidence_ok

    else:
        status = "within_target"
        passed = True
        needs_revision = False

    return {
        "part_number": number,
        "format": fmt,
        "actual_words": actual,
        "target_words": SHORT_TARGET_WORDS_PER_PART,
        "minimum_words": SHORT_MIN_WORDS_PER_PART,
        "preferred_maximum_words": SHORT_PREFERRED_MAX_WORDS_PER_PART,
        "maximum_words": (
            SHORT_FINAL_MERGED_MAX_WORDS
            if final_part_merged
            else SHORT_HARD_MAX_WORDS_PER_PART
        ),
        "hard_maximum_words": (
            SHORT_FINAL_MERGED_MAX_WORDS
            if final_part_merged
            else SHORT_HARD_MAX_WORDS_PER_PART
        ),
        "revision_attempted": bool(revision_attempted or evidence_ok),
        "revision_evidence_verified": evidence_ok,
        "final_part_merged": bool(final_part_merged),
        "status": status,
        "needs_revision": needs_revision,
        "retry_required": needs_revision,
        "passed": passed,
        "final_validation_passed": passed,
        "text_modified": False,
    }


def verify_revision_evidence(
    evidence: Any,
    revised_text: Any,
) -> bool:
    """Require changed text, matching hashes and semantic-preservation proof."""
    if not isinstance(evidence, dict):
        return False

    original_hash = str(
        evidence.get("original_sha256") or ""
    ).strip().lower()

    revised_hash = str(
        evidence.get("revised_sha256") or ""
    ).strip().lower()

    try:
        attempts = int(evidence.get("attempt_count", 0))
    except (TypeError, ValueError):
        return False

    actual_hash = sha256_text(revised_text)

    return bool(
        attempts >= 1
        and re.fullmatch(r"[0-9a-f]{64}", original_hash)
        and re.fullmatch(r"[0-9a-f]{64}", revised_hash)
        and original_hash != revised_hash
        and revised_hash == actual_hash
        and evidence.get("returned_text_verified") is True
        and evidence.get("semantic_preservation_verified") is True
    )

def _extract_part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part or "")

    for key in (
        "narration", "narration_text", "voiceover",
        "story", "text", "content", "main_story",
        "main_narration",
    ):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value

    return ""


def _part_revision_evidence(part: Any, index: int, supplied: Any) -> dict | None:
    if isinstance(supplied, dict):
        candidate = supplied.get(index, supplied.get(str(index)))
        if isinstance(candidate, dict):
            return candidate

    if isinstance(part, dict):
        candidate = part.get("revision_evidence")
        if isinstance(candidate, dict):
            return candidate

    return None


def _part_revision_status(
    part: Any,
    index: int,
    revision_attempted: Any,
) -> bool:
    evidence = _part_revision_evidence(
        part, index, revision_attempted
    )
    return verify_revision_evidence(
        evidence, _extract_part_text(part)
    )


def plan_final_part_merge(parts: Any) -> dict[str, Any]:
    """
    Return an exact merge proposal after the final Part has already
    undergone its retry/rebalance attempts.

    This function never deletes, rewrites, reorders or duplicates text.
    """
    if not isinstance(parts, list) or not parts:
        raise ValueError("At least one SHORT Part is required.")

    texts = [_extract_part_text(part) for part in parts]
    if any(not clean_text(text) for text in texts):
        raise ValueError("Cannot merge a story containing an empty Part.")

    counts = [word_count(text) for text in texts]
    final_count = counts[-1]

    if final_count >= SHORT_MIN_WORDS_PER_PART:
        return {
            "required": False,
            "status": "final_part_meets_minimum",
            "parts": texts,
            "word_counts": counts,
            "merged_part_number": None,
            "merged_word_count": None,
            "preserved_text_sha256": sha256_text("\n\n".join(texts)),
        }

    if len(texts) == 1:
        return {
            "required": False,
            "status": "single_part_below_minimum_requires_story_rebalance",
            "parts": texts,
            "word_counts": counts,
            "merged_part_number": None,
            "merged_word_count": None,
            "error": (
                "A single final Part cannot be merged into a preceding Part. "
                "Rebalance the complete story before final validation."
            ),
        }

    previous_count = counts[-2]
    merged_count = previous_count + final_count

    if merged_count > SHORT_FINAL_MERGED_MAX_WORDS:
        return {
            "required": False,
            "status": "merge_exceeds_239_words",
            "parts": texts,
            "word_counts": counts,
            "merged_part_number": len(texts) - 1,
            "merged_word_count": merged_count,
            "error": (
                f"Merging the final Part would create {merged_count} words "
                f"in the preceding Part; maximum is "
                f"{SHORT_FINAL_MERGED_MAX_WORDS}."
            ),
        }

    merged_text = (
        texts[-2].rstrip() + "\n\n" + texts[-1].lstrip()
    ).strip()

    proposed = texts[:-2] + [merged_text]

    if word_count(merged_text) != merged_count:
        raise RuntimeError("Final-Part merge changed the word count.")

    original_combined = clean_text(" ".join(texts))
    merged_combined = clean_text(" ".join(proposed))

    if original_combined != merged_combined:
        raise RuntimeError(
            "Final-Part merge changed, omitted or reordered story text."
        )

    return {
        "required": True,
        "status": "merge_ready",
        "parts": proposed,
        "word_counts": [word_count(text) for text in proposed],
        "merged_part_number": len(proposed),
        "merged_word_count": merged_count,
        "removed_final_part_number": len(texts),
        "original_part_count": len(texts),
        "final_part_count": len(proposed),
        "preserved_text_sha256": sha256_text(merged_combined),
        "text_preserved_exactly": True,
    }


def validate_all_parts(
    parts: Any,
    policy: dict[str, Any],
    *,
    revision_attempted: Any = False,
) -> dict[str, Any]:
    if not isinstance(parts, list):
        raise TypeError("Story parts must be supplied as a list.")
    if not isinstance(policy, dict):
        raise TypeError("Story length policy must be a dictionary.")

    fmt = normalize_format(policy.get("format", "short"))

    expected = _strict_integer(
        policy.get("parts_count", 1),
        name="policy.parts_count",
        minimum=1,
        maximum=MAX_PARTS,
    )

    minimum_parts = _strict_integer(
        policy.get("configured_parts_min", expected),
        name="configured_parts_min",
        minimum=1,
        maximum=MAX_PARTS,
    )

    maximum_parts = _strict_integer(
        policy.get("configured_parts_max", expected),
        name="configured_parts_max",
        minimum=minimum_parts,
        maximum=MAX_PARTS,
    )

    errors = []
    retry_parts = []
    results = []
    final_part_action = None
    merge_proposal = None

    if fmt == "full":
        if expected != 1 or len(parts) != 1:
            errors.append(
                "FULL mode must contain exactly one complete story Part."
            )

        for index, part in enumerate(parts, 1):
            result = validate_story_length(
                _extract_part_text(part),
                policy,
                part_number=index,
            )
            results.append(result)

            if not result["passed"]:
                errors.append("FULL story narration is empty.")

        valid = not errors

        return {
            "policy_version": POLICY_VERSION,
            "format": fmt,
            "expected_parts": 1,
            "configured_parts_min": 1,
            "configured_parts_max": 1,
            "actual_parts": len(parts),
            "valid": valid,
            "passed": valid,
            "final_validation_passed": valid,
            "retry_required": not valid,
            "retry_parts": [],
            "parts": results,
            "errors": errors,
            "final_part_exception": None,
            "final_part_merge_proposal": None,
        }

    if not minimum_parts <= expected <= maximum_parts:
        errors.append(
            f"Selected count {expected} is outside "
            f"{minimum_parts}-{maximum_parts}."
        )

    if len(parts) != expected:
        errors.append(
            f"Expected {expected} Parts, received {len(parts)}."
        )

    for index, part in enumerate(parts, 1):
        is_final = index == len(parts)

        final_part_merged = bool(
            isinstance(part, dict)
            and part.get("final_part_merged") is True
        )

        evidence = _part_revision_evidence(
            part,
            index,
            revision_attempted,
        )

        result = validate_story_length(
            _extract_part_text(part),
            policy,
            part_number=index,
            revision_attempted=False,
            revision_evidence=evidence,
            is_final_part=is_final,
            final_part_merged=final_part_merged,
        )

        results.append(result)

        if result["status"] == "final_part_below_minimum":
            final_part_action = {
                "part_number": index,
                "word_count": result["actual_words"],
                "required_first_action": "retry_or_rebalance_final_part",
                "fallback_action": (
                    "merge_all_remaining_final_text_into_preceding_part"
                ),
                "preceding_part_maximum_words": (
                    SHORT_FINAL_MERGED_MAX_WORDS
                ),
                "must_not_fail_only_for_final_part_under_120": True,
            }
            continue

        if result["needs_revision"]:
            retry_parts.append(index)
            errors.append(
                f"Part {index}: {result['actual_words']} words; "
                f"required {result['minimum_words']}-"
                f"{result['maximum_words']}; status={result['status']}."
            )

    if final_part_action is not None:
        merge_proposal = plan_final_part_merge(parts)
        final_part_action["merge_proposal"] = merge_proposal
        final_part_action["retry_attempts_allowed"] = MAX_REVISION_ATTEMPTS
        final_part_action["rebalance_required_if_merge_exceeds_limit"] = True

        if merge_proposal.get("required"):
            final_part_action["status"] = "retry_then_merge_if_retry_fails"
        else:
            final_part_action["status"] = (
                "retry_then_rebalance_required"
                if merge_proposal.get("error")
                else "retry_required"
            )

    valid = not errors and final_part_action is None

    return {
        "policy_version": POLICY_VERSION,
        "format": fmt,
        "expected_parts": expected,
        "configured_parts_min": minimum_parts,
        "configured_parts_max": maximum_parts,
        "actual_parts": len(parts),
        "valid": valid,
        "passed": valid,
        "final_validation_passed": valid,
        "retry_required": bool(
            retry_parts or final_part_action
        ),
        "retry_attempt_limit": MAX_REVISION_ATTEMPTS,
        "short_target_words_per_part": SHORT_TARGET_WORDS_PER_PART,
        "short_preferred_maximum_words_per_part": SHORT_PREFERRED_MAX_WORDS_PER_PART,
        "short_hard_maximum_words_per_part": SHORT_HARD_MAX_WORDS_PER_PART,
        "final_merged_maximum_words": SHORT_FINAL_MERGED_MAX_WORDS,
        "retry_parts": retry_parts,
        "parts": results,
        "errors": errors,
        "final_part_exception": final_part_action,
        "final_part_merge_proposal": merge_proposal,
    }



def select_final_part_count(
    *,
    total_corrected_words: Any,
    format_type: Any,
    parts_specification: Any,
    story_length: Any = "auto",
    config: dict[str, Any] | None = None,
) -> int:
    minimum, maximum = parse_parts_range(parts_specification)
    fmt = normalize_format(format_type)
    total = _validate_total_words(total_corrected_words)

    if fmt == "full":
        if (minimum, maximum) != (1, 1):
            raise ValueError("FULL mode requires PARTS=1.")
        selected = 1
    else:
        selected = choose_short_parts(total, minimum, maximum)

    if config is not None:
        config["PARTS_SPEC"] = (
            str(minimum)
            if minimum == maximum
            else f"{minimum}-{maximum}"
        )
        config["PARTS_MIN"] = minimum
        config["PARTS_MAX"] = maximum

        try:
            from input_config import set_selected_parts
        except ImportError:
            from scripts.input_config import set_selected_parts

        persisted = set_selected_parts(
            config,
            selected,
            source="story_length_policy",
            story_word_count=total,
        )

        if persisted != selected:
            raise RuntimeError(
                "Persisted PARTS count differs from selected count."
            )

        config["PARTS_SELECTED"] = selected
        config["PARTS"] = selected

    return selected


def update_policy_with_corrected_story(
    policy: dict[str, Any],
    corrected_story: Any,
    *,
    selected_parts: Any = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise TypeError("Existing story policy must be a dictionary.")

    corrected_text = clean_text(corrected_story)
    total_words = word_count(corrected_text)

    if total_words <= 0:
        raise ValueError("Corrected complete story is empty.")

    minimum = _strict_integer(
        policy.get("configured_parts_min", 1),
        name="configured_parts_min",
        minimum=1,
        maximum=MAX_PARTS,
    )
    maximum = _strict_integer(
        policy.get("configured_parts_max", minimum),
        name="configured_parts_max",
        minimum=minimum,
        maximum=MAX_PARTS,
    )
    fmt = normalize_format(policy.get("format", "short"))
    story_length = policy.get("story_length_input", "auto")

    if fmt == "full":
        if (minimum, maximum) != (1, 1):
            raise ValueError("FULL mode requires PARTS=1.")
        selected_parts = 1
    elif selected_parts is None:
        selected_parts = choose_short_parts(
            total_words, minimum, maximum
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

    updated.update({
        "corrected_story_word_count": total_words,
        "corrected_story_sha256": sha256_text(corrected_text),
        "story_was_corrected_before_counting": True,
        "selection_persisted": config is not None,
        "selection_source": "corrected_complete_story",
        "selected_parts": selected_parts,
        "actual_parts": selected_parts,
        "selection_feasibility": {
            "minimum_parts": minimum,
            "maximum_parts": maximum,
            "selected_parts": selected_parts,
            "minimum_total_words": updated.get("minimum_total_words"),
            "maximum_total_words": updated.get("maximum_total_words"),
            "within_configured_range": minimum <= selected_parts <= maximum,
        },
    })

    return updated


def record_revision_results(
    policy: dict[str, Any],
    original_parts: Any,
    revised_parts: Any,
    *,
    attempted_parts: Any,
    revision_evidence: Any = None,
) -> dict[str, Any]:
    """Record revisions without fabricating semantic-verification evidence.

    A final Part below 120 words is a recoverable state: it remains pending
    retry/rebalance and then an exact merge proposal, not a successful
    validation and not an immediate hard failure.
    """
    if not isinstance(policy, dict):
        raise TypeError("Story length policy must be a dictionary.")
    if not isinstance(original_parts, list) or not isinstance(revised_parts, list):
        raise TypeError("Original and revised Parts must both be lists.")
    if len(original_parts) != len(revised_parts):
        raise ValueError("Revision cannot silently change Part count.")

    if isinstance(attempted_parts, dict):
        attempted = {
            _strict_integer(
                key, name="revision part number",
                minimum=1, maximum=MAX_PARTS
            )
            for key, value in attempted_parts.items()
            if value
        }
    elif isinstance(attempted_parts, (list, tuple, set)):
        attempted = {
            _strict_integer(
                item, name="revision part number",
                minimum=1, maximum=MAX_PARTS
            )
            for item in attempted_parts
        }
    elif attempted_parts in (None, False, ""):
        attempted = set()
    else:
        raise TypeError(
            "attempted_parts must be a list, tuple, set or dictionary."
        )

    expected = _strict_integer(
        policy.get("parts_count", 0),
        name="policy.parts_count",
        minimum=1,
        maximum=MAX_PARTS,
    )
    if len(revised_parts) != expected:
        raise ValueError(
            f"Revision produced {len(revised_parts)} Parts; expected {expected}."
        )
    if any(number > expected for number in attempted):
        raise ValueError("Revision record contains an invalid Part number.")
    if revision_evidence is not None and not isinstance(revision_evidence, dict):
        raise TypeError("revision_evidence must map Part numbers to evidence objects.")

    results = []
    errors = []
    final_part_pending = None
    evidence_by_part = {}

    for index, (original, revised) in enumerate(
        zip(original_parts, revised_parts), 1
    ):
        old_text = _extract_part_text(original)
        new_text = _extract_part_text(revised)
        old_hash = sha256_text(old_text)
        new_hash = sha256_text(new_text)
        was_attempted = index in attempted

        supplied = None
        if isinstance(revision_evidence, dict):
            supplied = revision_evidence.get(
                index, revision_evidence.get(str(index))
            )
        if supplied is None and isinstance(revised, dict):
            supplied = revised.get("revision_evidence")

        # Hash-only evidence is deliberately insufficient to prove semantic
        # preservation. Only caller-supplied structured evidence can pass.
        evidence = supplied if isinstance(supplied, dict) else {
            "original_sha256": old_hash,
            "revised_sha256": new_hash,
            "attempt_count": 1 if was_attempted else 0,
            "returned_text_verified": bool(
                was_attempted and old_hash != new_hash and new_text
            ),
            "semantic_preservation_verified": False,
        }
        evidence_by_part[index] = evidence

        merged_final = bool(
            isinstance(revised, dict)
            and revised.get("final_part_merged") is True
        )
        result = validate_story_length(
            new_text,
            policy,
            part_number=index,
            revision_attempted=was_attempted,
            revision_evidence=evidence,
            is_final_part=index == len(revised_parts),
            final_part_merged=merged_final,
        )

        result.update({
            "original_words": word_count(old_text),
            "revised_words": word_count(new_text),
            "revision_attempted": was_attempted,
            "text_changed": old_hash != new_hash,
            "original_sha256": old_hash,
            "revised_sha256": new_hash,
            "revision_evidence": evidence,
        })

        if result["status"] == "final_part_below_minimum":
            final_part_pending = {
                "part_number": index,
                "word_count": result["actual_words"],
                "required_first_action": "retry_or_rebalance_final_part",
                "fallback_action": "merge_all_remaining_final_text_into_preceding_part",
                "preceding_part_maximum_words": SHORT_FINAL_MERGED_MAX_WORDS,
                "must_not_fail_only_for_final_part_under_120": True,
            }
        elif result["needs_revision"]:
            errors.append(
                f"Part {index} remains invalid: {result['status']} "
                f"({result['actual_words']} words)."
            )

        results.append(result)

    merge_proposal = (
        plan_final_part_merge(revised_parts)
        if final_part_pending is not None
        else None
    )
    if final_part_pending is not None:
        final_part_pending["merge_proposal"] = merge_proposal
        final_part_pending["status"] = (
            "retry_then_merge_if_retry_fails"
            if merge_proposal and merge_proposal.get("required")
            else "retry_or_rebalance_required"
        )

    valid = not errors and final_part_pending is None
    policy["revision_attempted"] = bool(attempted)
    policy["revision_parts"] = sorted(attempted)
    policy["revision_results"] = results
    policy["revision_evidence_by_part"] = evidence_by_part
    policy["revision_errors"] = errors
    policy["revision_outcome"] = (
        "pending_final_part_recovery"
        if final_part_pending is not None
        else ("passed" if valid else "failed")
    )
    policy["revision_completed_at_utc"] = datetime.now(
        timezone.utc
    ).isoformat()
    policy["final_part_validation_passed"] = valid
    policy["final_part_exception"] = final_part_pending
    policy["final_part_merge_proposal"] = merge_proposal

    if errors:
        raise ValueError(
            "Story revision validation failed: " + " ".join(errors)
        )

    return {
        "valid": valid,
        "passed": valid,
        "final_validation_passed": valid,
        "retry_required": final_part_pending is not None,
        "revision_attempted": bool(attempted),
        "revision_parts": sorted(attempted),
        "parts": results,
        "errors": errors,
        "final_part_exception": final_part_pending,
        "final_part_merge_proposal": merge_proposal,
    }


def validate_corrected_story_parts(
    parts: Any,
    policy: dict[str, Any],
    *,
    revision_attempted: Any = False,
) -> dict[str, Any]:
    result = validate_all_parts(
        parts, policy, revision_attempted=revision_attempted
    )

    if not result["valid"]:
        # A short final Part is an actionable recovery state, not a terminal
        # validation failure. The caller must retry/rebalance it and then
        # apply the exact merge proposal when the retry cannot reach 120.
        if (
            result.get("final_part_exception") is not None
            and not result.get("errors")
        ):
            return result

        raise ValueError(
            "Final story part validation failed: "
            + " ".join(result["errors"])
        )

    return result


def write_policy(
    path: str | Path,
    policy: dict[str, Any],
) -> None:
    if not isinstance(policy, dict):
        raise TypeError("Story length policy must be a dictionary.")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None

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
                policy, handle, ensure_ascii=False,
                indent=2, sort_keys=True
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, destination)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_policy(path: str | Path) -> dict[str, Any]:
    source = Path(path)

    try:
        value = json.loads(
            source.read_text(encoding="utf-8-sig")
        )
    except (
        OSError, UnicodeError, json.JSONDecodeError
    ) as exc:
        raise ValueError(
            f"Cannot read story length policy: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise ValueError("Story length policy JSON must contain an object.")

    if value.get("policy_version") != POLICY_VERSION:
        raise ValueError(
            "Story length policy version mismatch; regenerate the policy."
        )

    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate corrected-story length and Part-selection policy."
    )
    parser.add_argument(
        "--format", default="short", choices=("short", "full")
    )
    parser.add_argument("--story-length", default="auto")
    parser.add_argument(
        "--parts", default="1",
        help="Fixed count or inclusive range: 4, 2-8, 4-6 or 12-18.",
    )
    parser.add_argument("--selected-parts", type=int, default=None)
    parser.add_argument("--scenes", type=int, default=DEFAULT_SCENES_PER_MINUTE)
    parser.add_argument("--scene-duration", default="auto")
    parser.add_argument("--source-words", type=int, default=0)
    parser.add_argument("--corrected-story-file", default="")
    parser.add_argument(
        "--output", default="output/story/story_length_policy.json"
    )
    args = parser.parse_args()

    corrected_text = ""
    source_words = args.source_words

    if args.corrected_story_file:
        story_path = Path(args.corrected_story_file)
        if not story_path.is_file():
            raise FileNotFoundError(
                f"Corrected story file not found: {story_path}"
            )

        corrected_text = story_path.read_text(encoding="utf-8-sig")
        measured = word_count(corrected_text)

        if source_words and source_words != measured:
            raise ValueError(
                "--source-words does not match corrected story file."
            )
        source_words = measured

    source_words = _validate_total_words(source_words)
    minimum, maximum = parse_parts_range(args.parts)
    fmt = normalize_format(args.format)

    if fmt == "full" and (minimum, maximum) != (1, 1):
        raise ValueError("FULL mode requires --parts 1.")

    selected = args.selected_parts

    if selected is not None:
        selected = _validate_selected_count(
            selected, minimum, maximum
        )

    if source_words:
        calculated = choose_part_count(
            source_words,
            fmt,
            f"{minimum}-{maximum}",
            args.story_length,
        )
        if selected is not None and selected != calculated:
            raise ValueError(
                f"--selected-parts={selected} conflicts with calculated count {calculated}."
            )
        selected = calculated

    policy = build_length_policy(
        format_type=fmt,
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
    policy["selection_source"] = (
        "command_line_calculation" if selected is not None else "provisional"
    )

    write_policy(args.output, policy)
    print(json.dumps(policy, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
