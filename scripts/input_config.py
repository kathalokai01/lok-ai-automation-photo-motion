

#!/usr/bin/env python3
"""Centralized configuration parser for Input/topic.txt."""

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

    # TTS provider priority
    "TTS_MAIN_1": "fish_audio",
    "TTS_MAIN_2": "gemini_tts",
    "TTS_MAIN_3": "edge_tts",
    "TTS_FALLBACK_ENABLED": True,
    "TTS_FALLBACK_ORDER": (
        "piper,elevenlabs,cartesia,finevoice,minimax"
    ),
    "TTS_ALLOW_PAID": False,

    # Chunking and resumability
    "TTS_CHUNK_MAX_CHARS": 700,
    "TTS_CHUNK_MIN_CHARS": 150,
    "TTS_CHUNK_CHECKPOINT": True,
    "TTS_VALIDATE_AUDIO": True,
    "TTS_MAX_PROVIDER_ATTEMPTS": 1,

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
    key for key, value in DEFAULT_CONFIG.items()
    if isinstance(value, bool)
}

INT_KEYS = {
    key for key, value in DEFAULT_CONFIG.items()
    if isinstance(value, int) and not isinstance(value, bool)
}

FLOAT_KEYS = set()


def _strip_inline_comment(value: str) -> str:
    quote = None
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
        elif char == "#":
            return value[:index].rstrip()

    return value.strip()


def _unquote(value: str) -> str:
    value = value.strip()

    if len(value) >= 2 and value[0] == value[-1]:
        if value[0] in ("'", '"'):
            try:
                result = ast.literal_eval(value)
                if isinstance(result, str):
                    return result
            except Exception:
                return value[1:-1]

    return value


def _parse_scalar(key: str, value: str) -> Any:
    value = value.strip()

    if not value:
        return ""

    parsed = _unquote(value)

    if key in BOOL_KEYS:
        lowered = parsed.lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
        raise ValueError(f"{key} must be true or false.")

    if key in INT_KEYS:
        try:
            return int(parsed)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer.") from exc

    if key in FLOAT_KEYS:
        try:
            return float(parsed)
        except ValueError as exc:
            raise ValueError(f"{key} must be a number.") from exc

    return parsed


def _find_assignment(line: str) -> Optional[tuple[str, str]]:
    match = re.match(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
        line,
    )
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _read_multiline_value(
    lines: List[str],
    start_index: int,
    opening: str,
    key: str,
    initial_value: str,
) -> tuple[str, int]:
    content: List[str] = []
    remainder = initial_value[len(opening):]

    if opening in remainder:
        first, _ = remainder.split(opening, 1)
        if first:
            content.append(first)
        return "\n".join(content).strip(), start_index + 1

    if remainder:
        content.append(remainder)

    index = start_index + 1

    while index < len(lines):
        current = lines[index]
        position = current.find(opening)

        if position >= 0:
            before = current[:position]
            if before:
                content.append(before)
            return "\n".join(content).strip(), index + 1

        content.append(current)
        index += 1

    raise ValueError(
        f"{key} multiline block is missing closing {opening}."
    )


def load_input_config(
    path: str | Path = "Input/topic.txt",
) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(f"Input config not found: {file_path}")

    lines = file_path.read_text(encoding="utf-8-sig").splitlines()
    index = 0

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if not stripped or stripped.startswith("#"):
            index += 1
            continue

        assignment = _find_assignment(raw)
        if assignment is None:
            index += 1
            continue

        key, raw_value = assignment

        if key == "STORY_TEXT":
            value = raw_value.strip()

            if value.startswith('"""'):
                config[key], index = _read_multiline_value(
                    lines, index, '"""', key, value
                )
                continue

            if value.startswith("'''"):
                config[key], index = _read_multiline_value(
                    lines, index, "'''", key, value
                )
                continue

        cleaned = _strip_inline_comment(raw_value)
        config[key] = _parse_scalar(key, cleaned)
        index += 1

    return config


def normalize_format(config: Dict[str, Any]) -> str:
    value = str(config.get("FORMAT", "full")).strip().lower()

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
        raise ValueError("FORMAT must be 'short' or 'full'.")

    config["FORMAT"] = aliases[value]
    return aliases[value]


def get_format(config: Dict[str, Any]) -> str:
    return normalize_format(config)


def _positive_int(config: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer.") from exc

    if value < 1:
        raise ValueError(f"{key} must be greater than zero.")

    return value


def get_parts(config: Dict[str, Any]) -> int:
    return _positive_int(config, "PARTS", 1)


def get_scenes(config: Dict[str, Any]) -> int:
    return _positive_int(config, "SCENES", 1)


def get_fps(config: Dict[str, Any]) -> int:
    return _positive_int(config, "FPS", 24)


def get_max_retries(config: Dict[str, Any]) -> int:
    value = int(config.get("MAX_RETRIES", 3))
    if value < 0:
        raise ValueError("MAX_RETRIES cannot be negative.")
    return value


def get_topic(config: Dict[str, Any]) -> str:
    return str(config.get("TOPIC", "")).strip()


def get_story_text(config: Dict[str, Any]) -> str:
    return str(config.get("STORY_TEXT", "")).strip()


def get_title_source(config: Dict[str, Any]) -> str:
    return get_topic(config) or get_story_text(config)


def has_story_input(config: Dict[str, Any]) -> bool:
    return bool(get_title_source(config))


def get_tts_provider_order(config: Dict[str, Any]) -> List[str]:
    """Return unique providers in the configured priority order."""
    main = [
        str(config.get("TTS_MAIN_1", "fish_audio")).strip().lower(),
        str(config.get("TTS_MAIN_2", "gemini_tts")).strip().lower(),
        str(config.get("TTS_MAIN_3", "edge_tts")).strip().lower(),
    ]

    fallback_enabled = bool(config.get("TTS_FALLBACK_ENABLED", True))
    fallback_raw = str(config.get("TTS_FALLBACK_ORDER", ""))

    fallback = (
        [item.strip().lower() for item in fallback_raw.split(",") if item.strip()]
        if fallback_enabled else []
    )

    allowed = {
        "fish_audio", "gemini_tts", "edge_tts", "piper",
        "elevenlabs", "cartesia", "finevoice", "minimax",
    }

    result: List[str] = []

    for provider in main + fallback:
        if provider not in allowed:
            raise ValueError(f"Unsupported TTS provider: {provider}")
        if provider not in result:
            result.append(provider)

    if len(main) != 3 or any(not item for item in main):
        raise ValueError("All three main TTS providers must be configured.")

    return result


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    normalize_format(config)
    config["PARTS"] = get_parts(config)
    config["SCENES"] = get_scenes(config)
    config["FPS"] = get_fps(config)
    config["MAX_RETRIES"] = get_max_retries(config)

    if not has_story_input(config):
        raise ValueError("Either TOPIC or STORY_TEXT must be provided.")

    voice = str(config.get("VOICE", "male")).strip().lower()
    if voice not in {"male", "female"}:
        raise ValueError("VOICE must be 'male' or 'female'.")
    config["VOICE"] = voice

    for key in (
        "TTS_CHUNK_MAX_CHARS",
        "TTS_CHUNK_MIN_CHARS",
        "TTS_MAX_PROVIDER_ATTEMPTS",
    ):
        config[key] = _positive_int(config, key, DEFAULT_CONFIG[key])

    if config["TTS_CHUNK_MIN_CHARS"] > config["TTS_CHUNK_MAX_CHARS"]:
        raise ValueError(
            "TTS_CHUNK_MIN_CHARS cannot exceed TTS_CHUNK_MAX_CHARS."
        )

    if config["TTS_MAX_PROVIDER_ATTEMPTS"] > 3:
        raise ValueError("TTS_MAX_PROVIDER_ATTEMPTS cannot exceed 3.")

    config["TTS_PROVIDER_ORDER"] = get_tts_provider_order(config)

    config["TTS_ALLOW_PAID"] = bool(config.get("TTS_ALLOW_PAID", False))
    config["TTS_FALLBACK_ENABLED"] = bool(
        config.get("TTS_FALLBACK_ENABLED", True)
    )

    failure_policy = str(
        config.get("FAILURE_POLICY", "retry_then_checkpoint")
    ).strip().lower()

    if failure_policy not in {
        "retry_then_checkpoint", "skip", "stop"
    }:
        raise ValueError(
            "FAILURE_POLICY must be retry_then_checkpoint, skip, or stop."
        )

    config["FAILURE_POLICY"] = failure_policy
    config["CAPTIONS"] = str(config.get("CAPTIONS", "hindi")).strip().lower()
    config["QUALITY"] = str(config.get("QUALITY", "high")).strip().lower()
    config["VISUAL_STYLE"] = str(
        config.get("VISUAL_STYLE", "cinematic_realistic")
    ).strip()
    config["STORY_LENGTH"] = str(
        config.get("STORY_LENGTH", "auto")
    ).strip()
    config["SCENE_DURATION"] = str(
        config.get("SCENE_DURATION", "auto")
    ).strip()

    return config


def load_and_validate(
    path: str | Path = "Input/topic.txt",
) -> Dict[str, Any]:
    return validate_config(load_input_config(path))


def get_config_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "FORMAT": get_format(config),
        "TOPIC": get_topic(config),
        "HAS_STORY_TEXT": bool(get_story_text(config)),
        "TITLE_SOURCE_AVAILABLE": bool(get_title_source(config)),
        "PARTS": get_parts(config),
        "SCENES": get_scenes(config),
        "FPS": get_fps(config),
        "VOICE": str(config.get("VOICE", "")),
        "AUDIENCE": str(config.get("AUDIENCE", "")),
        "CAPTIONS": str(config.get("CAPTIONS", "")),
        "STORY_LENGTH": str(config.get("STORY_LENGTH", "")),
        "SCENE_DURATION": str(config.get("SCENE_DURATION", "")),
        "FAILURE_POLICY": str(config.get("FAILURE_POLICY", "")),
        "RESUME_ENABLED": bool(config.get("RESUME_ENABLED", True)),
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE": bool(
            config.get("SAVE_CHECKPOINT_AFTER_EACH_SCENE", True)
        ),
        "SKIP_COMPLETED_SCENES": bool(
            config.get("SKIP_COMPLETED_SCENES", True)
        ),
        "TTS_PROVIDER_ORDER": get_tts_provider_order(config),
        "TTS_ALLOW_PAID": bool(config.get("TTS_ALLOW_PAID", False)),
        "TTS_CHUNK_MAX_CHARS": int(config.get("TTS_CHUNK_MAX_CHARS", 700)),
        "TTS_CHUNK_CHECKPOINT": bool(config.get("TTS_CHUNK_CHECKPOINT", True)),
    }


def print_config_summary(config: Dict[str, Any]) -> None:
    print("==============================================")
    print("INPUT CONFIG")
    print("==============================================")

    for key, value in get_config_summary(config).items():
        print(f"{key} = {value}")

    print("==============================================")


if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "Input/topic.txt"

    try:
        config = load_and_validate(config_path)
        print_config_summary(config)
        print("INPUT CONFIG VALIDATION: OK")
    except Exception as exc:
        print(f"INPUT CONFIG VALIDATION: FAILED: {exc}")
        raise