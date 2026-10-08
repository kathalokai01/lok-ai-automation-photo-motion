#!/usr/bin/env python3

"""
Centralized parser and validator for Input/topic.txt.

Supports:
- KEY = VALUE
- quoted values
- inline comments
- multiline STORY_TEXT
- booleans
- integers
- floats
- FORMAT normalization
- title source selection
- configuration validation
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "FORMAT": "full",
    "AUDIENCE": "adult",
    "VOICE": "male",
    "SPEED": "+0%",
    "TOPIC": "",
    "STORY_TEXT": "",
    "PARTS": 1,
    "SCENES": 1,
    "STORY_LENGTH": "auto",
    "SCENE_DURATION": "auto",
    "CAPTIONS": "hindi",
    "PART_HOOK": True,
    "PART_SUSPENSE": True,
    "FINAL_RESOLUTION": True,
    "VISUAL_STYLE": "cinematic_realistic",
    "REALISM": "high",
    "CHARACTER_BIBLE": True,
    "CHARACTER_CONSISTENCY": True,
    "WORLD_CONSISTENCY": True,
    "SCENE_CONTINUITY": True,
    "CINEMATIC_CAMERA": True,
    "CAMERA_STYLE": "cinematic",
    "LIGHTING": "cinematic",
    "MOOD": "dramatic",
    "QUALITY": "high",
    "FPS": 24,
    "NEGATIVE_PROMPT": True,
    "AVOID_CARTOON_LOOK": True,
    "AVOID_NEON": True,
    "AVOID_GLITCH_EFFECTS": True,
    "NATURAL_MOTION": True,
    "REALISTIC_LIGHTING": True,
    "MUSIC": True,
    "MUSIC_STYLE": "cinematic",
    "SFX": True,
    "AMBIENT_SOUND": True,
    "TRANSITIONS": "cinematic",
    "WATERMARK": False,
    "LOGO": False,
    "RESUME_ENABLED": True,
    "MAX_RETRIES": 3,
    "SAVE_CHECKPOINT_AFTER_EACH_SCENE": True,
    "SKIP_COMPLETED_SCENES": True,
    "FAILURE_POLICY": "retry_then_checkpoint",
}


BOOL_KEYS = {
    "PART_HOOK",
    "PART_SUSPENSE",
    "FINAL_RESOLUTION",
    "CHARACTER_BIBLE",
    "CHARACTER_CONSISTENCY",
    "WORLD_CONSISTENCY",
    "SCENE_CONTINUITY",
    "CINEMATIC_CAMERA",
    "NEGATIVE_PROMPT",
    "AVOID_CARTOON_LOOK",
    "AVOID_NEON",
    "AVOID_GLITCH_EFFECTS",
    "NATURAL_MOTION",
    "REALISTIC_LIGHTING",
    "MUSIC",
    "SFX",
    "AMBIENT_SOUND",
    "WATERMARK",
    "LOGO",
    "RESUME_ENABLED",
    "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
    "SKIP_COMPLETED_SCENES",
}


INT_KEYS = {
    "PARTS",
    "SCENES",
    "FPS",
    "MAX_RETRIES",
}


FLOAT_KEYS = set()


def _strip_inline_comment(value: str) -> str:
    """
    Remove comments beginning with # while preserving # inside quotes.
    """

    quote: Optional[str] = None
    escaped = False

    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if quote:
            if char == quote:
                quote = None
            continue

        if char in ("'", '"'):
            quote = char
            continue

        if char == "#":
            return value[:index].rstrip()

    return value.strip()


def _unquote(value: str) -> str:
    """
    Remove matching outer quotes safely.
    """

    value = value.strip()

    if len(value) >= 2:
        if value[0] == value[-1] and value[0] in ("'", '"'):
            try:
                parsed = ast.literal_eval(value)

                if isinstance(parsed, str):
                    return parsed

            except Exception:
                return value[1:-1]

    return value


def _parse_scalar(key: str, value: str) -> Any:
    """
    Convert a normal KEY=VALUE value into the appropriate type.
    """

    value = value.strip()

    if value == "":
        return ""

    unquoted = _unquote(value)

    if key in BOOL_KEYS:
        lowered = unquoted.strip().lower()

        if lowered in {
            "true",
            "yes",
            "on",
            "1",
        }:
            return True

        if lowered in {
            "false",
            "no",
            "off",
            "0",
        }:
            return False

        raise ValueError(
            f"{key} must be true or false. Received: {value}"
        )

    if key in INT_KEYS:
        try:
            return int(unquoted.strip())

        except ValueError:
            raise ValueError(
                f"{key} must be an integer. Received: {value}"
            )

    if key in FLOAT_KEYS:
        try:
            return float(unquoted.strip())

        except ValueError:
            raise ValueError(
                f"{key} must be a number. Received: {value}"
            )

    return unquoted


def _find_assignment(
    line: str,
) -> Optional[tuple[str, str]]:
    """
    Find KEY = VALUE assignments.
    """

    match = re.match(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
        line,
    )

    if not match:
        return None

    key = match.group(1).strip()
    value = match.group(2).strip()

    return key, value


def _read_multiline_value(
    lines: List[str],
    start_index: int,
    opening: str,
    key: str,
    initial_value: str,
) -> tuple[str, int]:
    """
    Read a multiline value safely.

    Supports both triple-double-quote and triple-single-quote blocks.
    """

    content: List[str] = []

    remainder = initial_value[len(opening):]

    if opening in remainder:
        first_part, _ = remainder.split(
            opening,
            1,
        )

        if first_part:
            content.append(first_part)

        return (
            "\n".join(content).strip(),
            start_index + 1,
        )

    if remainder:
        content.append(remainder)

    index = start_index + 1

    while index < len(lines):
        current = lines[index]

        closing_position = current.find(opening)

        if closing_position >= 0:
            before_close = current[
                :closing_position
            ]

            if before_close:
                content.append(before_close)

            return (
                "\n".join(content).strip(),
                index + 1,
            )

        content.append(current)
        index += 1

    raise ValueError(
        f"{key} starts a multiline block but "
        f"the closing {opening} was not found."
    )


def load_input_config(
    path: str | Path = "Input/topic.txt",
) -> Dict[str, Any]:
    """
    Load and parse the complete Input/topic.txt file.

    STORY_TEXT supports multiline content.

    The input file is parsed as configuration text.
    It is never executed as Python code.
    """

    config: Dict[str, Any] = dict(
        DEFAULT_CONFIG
    )

    file_path = Path(path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Input config not found: {file_path}"
        )

    text = file_path.read_text(
        encoding="utf-8-sig"
    )

    lines = text.splitlines()

    index = 0

    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("#"):
            index += 1
            continue

        assignment = _find_assignment(
            raw_line
        )

        if assignment is None:
            index += 1
            continue

        key, raw_value = assignment

        # ------------------------------------------------------
        # STORY_TEXT
        # ------------------------------------------------------

        if key == "STORY_TEXT":
            value = raw_value.strip()

            if value.startswith('"""'):
                parsed_value, next_index = (
                    _read_multiline_value(
                        lines,
                        index,
                        '"""',
                        key,
                        value,
                    )
                )

                config[key] = parsed_value
                index = next_index
                continue

            if value.startswith("'''"):
                parsed_value, next_index = (
                    _read_multiline_value(
                        lines,
                        index,
                        "'''",
                        key,
                        value,
                    )
                )

                config[key] = parsed_value
                index = next_index
                continue

            cleaned = _strip_inline_comment(
                value
            )

            config[key] = _unquote(
                cleaned
            )

            index += 1
            continue

        # ------------------------------------------------------
        # NORMAL VALUES
        # ------------------------------------------------------

        cleaned_value = _strip_inline_comment(
            raw_value
        )

        config[key] = _parse_scalar(
            key,
            cleaned_value,
        )

        index += 1

    return config


def normalize_format(
    config: Dict[str, Any],
) -> str:
    """
    Normalize FORMAT to short or full.
    """

    value = str(
        config.get("FORMAT", "")
    ).strip().lower()

    aliases = {
        "short": "short",
        "shorts": "short",
        "vertical": "short",
        "reels": "short",
        "reel": "short",
        "youtube_short": "short",
        "youtube-shorts": "short",

        "full": "full",
        "long": "full",
        "landscape": "full",
        "youtube": "full",
        "youtube_full": "full",
        "youtube-long": "full",
    }

    if value not in aliases:
        raise ValueError(
            f"Unsupported FORMAT: {value!r}. "
            "Use 'short' or 'full'."
        )

    normalized = aliases[value]

    config["FORMAT"] = normalized

    return normalized


def get_format(
    config: Dict[str, Any],
) -> str:
    return normalize_format(config)


def get_parts(
    config: Dict[str, Any],
) -> int:
    value = int(
        config.get("PARTS", 1)
    )

    if value < 1:
        raise ValueError(
            "PARTS must be >= 1."
        )

    return value


def get_scenes(
    config: Dict[str, Any],
) -> int:
    value = int(
        config.get("SCENES", 1)
    )

    if value < 1:
        raise ValueError(
            "SCENES must be >= 1."
        )

    return value


def get_fps(
    config: Dict[str, Any],
) -> int:
    value = int(
        config.get("FPS", 24)
    )

    if value <= 0:
        raise ValueError(
            "FPS must be greater than 0."
        )

    return value


def get_max_retries(
    config: Dict[str, Any],
) -> int:
    value = int(
        config.get("MAX_RETRIES", 3)
    )

    if value < 0:
        raise ValueError(
            "MAX_RETRIES cannot be negative."
        )

    return value


def get_topic(
    config: Dict[str, Any],
) -> str:
    """
    Return TOPIC as the preferred title source.
    """

    return str(
        config.get("TOPIC", "")
    ).strip()


def get_story_text(
    config: Dict[str, Any],
) -> str:
    return str(
        config.get("STORY_TEXT", "")
    ).strip()


def get_title_source(
    config: Dict[str, Any],
) -> str:
    """
    Title source priority:

    1. TOPIC
    2. STORY_TEXT
    3. empty
    """

    topic = get_topic(config)

    if topic:
        return topic

    story_text = get_story_text(
        config
    )

    if story_text:
        return story_text

    return ""


def has_story_input(
    config: Dict[str, Any],
) -> bool:
    return bool(
        get_topic(config)
        or get_story_text(config)
    )


def validate_config(
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Validate and normalize the complete configuration.
    """

    normalize_format(config)

    config["PARTS"] = get_parts(
        config
    )

    config["SCENES"] = get_scenes(
        config
    )

    config["FPS"] = get_fps(
        config
    )

    config["MAX_RETRIES"] = get_max_retries(
        config
    )

    if not has_story_input(config):
        raise ValueError(
            "Either TOPIC or STORY_TEXT must be provided."
        )

    voice = str(
        config.get("VOICE", "male")
    ).strip().lower()

    if voice not in {
        "male",
        "female",
    }:
        raise ValueError(
            "VOICE must be 'male' or 'female'."
        )

    config["VOICE"] = voice

    audience = str(
        config.get(
            "AUDIENCE",
            "adult",
        )
    ).strip()

    if not audience:
        audience = "adult"

    config["AUDIENCE"] = audience

    failure_policy = str(
        config.get(
            "FAILURE_POLICY",
            "retry_then_checkpoint",
        )
    ).strip().lower()

    allowed_failure_policies = {
        "retry_then_checkpoint",
        "skip",
        "stop",
    }

    if (
        failure_policy
        not in allowed_failure_policies
    ):
        raise ValueError(
            "FAILURE_POLICY must be one of: "
            "retry_then_checkpoint, skip, stop."
        )

    config["FAILURE_POLICY"] = (
        failure_policy
    )

    config["CAPTIONS"] = str(
        config.get(
            "CAPTIONS",
            "hindi",
        )
    ).strip().lower()

    config["QUALITY"] = str(
        config.get(
            "QUALITY",
            "high",
        )
    ).strip().lower()

    config["VISUAL_STYLE"] = str(
        config.get(
            "VISUAL_STYLE",
            "cinematic_realistic",
        )
    ).strip()

    config["STORY_LENGTH"] = str(
        config.get(
            "STORY_LENGTH",
            "auto",
        )
    ).strip()

    config["SCENE_DURATION"] = str(
        config.get(
            "SCENE_DURATION",
            "auto",
        )
    ).strip()

    return config


def load_and_validate(
    path: str | Path = "Input/topic.txt",
) -> Dict[str, Any]:
    """
    Load and validate configuration.
    """

    config = load_input_config(
        path
    )

    return validate_config(
        config
    )


def get_config_summary(
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Return a compact configuration summary.
    """

    return {
        "FORMAT": get_format(config),

        "TOPIC": get_topic(config),

        "HAS_STORY_TEXT": bool(
            get_story_text(config)
        ),

        "TITLE_SOURCE_AVAILABLE": bool(
            get_title_source(config)
        ),

        "PARTS": get_parts(config),

        "SCENES": get_scenes(config),

        "FPS": get_fps(config),

        "VOICE": str(
            config.get("VOICE", "")
        ),

        "AUDIENCE": str(
            config.get("AUDIENCE", "")
        ),

        "CAPTIONS": str(
            config.get("CAPTIONS", "")
        ),

        "STORY_LENGTH": str(
            config.get(
                "STORY_LENGTH",
                "",
            )
        ),

        "SCENE_DURATION": str(
            config.get(
                "SCENE_DURATION",
                "",
            )
        ),

        "FAILURE_POLICY": str(
            config.get(
                "FAILURE_POLICY",
                "",
            )
        ),

        "RESUME_ENABLED": bool(
            config.get(
                "RESUME_ENABLED",
                False,
            )
        ),

        "SAVE_CHECKPOINT_AFTER_EACH_SCENE": bool(
            config.get(
                "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
                False,
            )
        ),

        "SKIP_COMPLETED_SCENES": bool(
            config.get(
                "SKIP_COMPLETED_SCENES",
                False,
            )
        ),
    }


def print_config_summary(
    config: Dict[str, Any],
) -> None:
    """
    Print a human-readable configuration summary.
    """

    summary = get_config_summary(
        config
    )

    print(
        "=============================================="
    )

    print("INPUT CONFIG")

    print(
        "=============================================="
    )

    for key, value in summary.items():
        print(
            f"{key} = {value}"
        )

    print(
        "=============================================="
    )


if __name__ == "__main__":
    import sys

    config_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "Input/topic.txt"
    )

    try:
        config = load_and_validate(
            config_path
        )

        print_config_summary(
            config
        )

        print(
            "INPUT CONFIG VALIDATION: OK"
        )

    except Exception as exc:
        print(
            "INPUT CONFIG VALIDATION: FAILED: "
            f"{exc}"
        )

        raise
