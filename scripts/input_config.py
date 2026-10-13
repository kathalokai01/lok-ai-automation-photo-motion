
#!/usr/bin/env python3
"""Validated central configuration for Input/topic.txt."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

ALL_TTS_PROVIDERS = [
    "fish_audio",
    "gemini_tts",
    "edge_tts",
    "piper",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
]

PROVIDER_ALIASES = {
    "fish": "fish_audio",
    "fish audio": "fish_audio",
    "gemini": "gemini_tts",
    "gemini tts": "gemini_tts",
    "edge": "edge_tts",
    "edge tts": "edge_tts",
    "eleven labs": "elevenlabs",
    "mini max": "minimax",
}

DEFAULT_CONFIG: dict[str, Any] = {
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
    "TTS_MAIN_1": "fish_audio",
    "TTS_MAIN_2": "gemini_tts",
    "TTS_MAIN_3": "edge_tts",
    "TTS_FALLBACK_ENABLED": True,
    "TTS_FALLBACK_ORDER": ",".join(ALL_TTS_PROVIDERS),
    "TTS_ALLOW_PAID": False,
    "TTS_CHUNK_MAX_CHARS": 500,
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
            except (ValueError, SyntaxError):
                return value[1:-1]
    return value


def _parse_scalar(key: str, raw_value: str) -> Any:
    value = _unquote(_strip_inline_comment(raw_value).strip())

    if key in BOOL_KEYS:
        lowered = str(value).strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
        raise ValueError(f"{key} must be true or false.")

    if key in INT_KEYS:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer.") from exc

    return value


def _find_assignment(line: str) -> tuple[str, str] | None:
    match = re.match(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$",
        line,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


def _read_multiline(
    lines: list[str],
    start: int,
    key: str,
    delimiter: str,
    raw_value: str,
) -> tuple[str, int]:
    remainder = raw_value[len(delimiter):]
    closing = remainder.find(delimiter)

    if closing >= 0:
        trailing = remainder[closing + len(delimiter):].strip()
        if trailing and not trailing.startswith("#"):
            raise ValueError(
                f"Unexpected text after closing multiline block for {key}."
            )
        return remainder[:closing].strip(), start + 1

    collected = [remainder] if remainder else []

    for index in range(start + 1, len(lines)):
        current = lines[index]
        position = current.find(delimiter)

        if position >= 0:
            before = current[:position]
            if before:
                collected.append(before)

            trailing = current[position + len(delimiter):].strip()
            if trailing and not trailing.startswith("#"):
                raise ValueError(
                    f"Unexpected text after closing multiline block for {key}."
                )
            return "\n".join(collected).strip(), index + 1

        collected.append(current)

    raise ValueError(
        f"{key} multiline block is missing closing {delimiter}."
    )


def load_input_config(
    path: str | Path = "Input/topic.txt",
) -> dict[str, Any]:
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
                config[key], index = _read_multiline(
                    lines, index, key, '"""', value
                )
                continue
            if value.startswith("'''"):
                config[key], index = _read_multiline(
                    lines, index, key, "'''", value
                )
                continue

        config[key] = _parse_scalar(key, raw_value)
        index += 1

    return config


def normalize_format(config: dict[str, Any]) -> str:
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


def get_format(config: dict[str, Any]) -> str:
    return normalize_format(config)


def _positive_int(
    config: dict[str, Any],
    key: str,
    default: int,
) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer.") from exc

    if value < 1:
        raise ValueError(f"{key} must be greater than zero.")

    return value


def get_parts(config: dict[str, Any]) -> int:
    return _positive_int(config, "PARTS", 1)


def get_scenes(config: dict[str, Any]) -> int:
    return _positive_int(config, "SCENES", 1)


def get_fps(config: dict[str, Any]) -> int:
    fps = _positive_int(config, "FPS", 24)
    if fps > 60:
        raise ValueError("FPS cannot exceed 60.")
    return fps


def get_max_retries(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("MAX_RETRIES", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("MAX_RETRIES must be an integer.") from exc

    if value < 0:
        raise ValueError("MAX_RETRIES cannot be negative.")

    return value


def get_topic(config: dict[str, Any]) -> str:
    return str(config.get("TOPIC", "")).strip()


def get_story_text(config: dict[str, Any]) -> str:
    return str(config.get("STORY_TEXT", "")).strip()


def get_title_source(config: dict[str, Any]) -> str:
    topic = get_topic(config)
    if topic:
        return topic

    story = get_story_text(config)
    for line in story.splitlines():
        cleaned = line.strip().strip("#").strip()
        if cleaned:
            return cleaned[:180]

    return ""


def has_story_input(config: dict[str, Any]) -> bool:
    return bool(get_topic(config) or get_story_text(config))


def normalize_provider(value: Any) -> str:
    provider = str(value or "").strip().lower().replace("-", "_")
    spaced = provider.replace("_", " ")
    provider = PROVIDER_ALIASES.get(
        spaced,
        PROVIDER_ALIASES.get(provider, provider),
    )
    return provider


def get_tts_provider_order(config: dict[str, Any]) -> list[str]:
    main = [
        normalize_provider(config.get("TTS_MAIN_1", "")),
        normalize_provider(config.get("TTS_MAIN_2", "")),
        normalize_provider(config.get("TTS_MAIN_3", "")),
    ]

    if any(not item for item in main):
        raise ValueError(
            "TTS_MAIN_1, TTS_MAIN_2 and TTS_MAIN_3 "
            "must all contain provider names."
        )

    if len(set(main)) != 3:
        raise ValueError("The three main TTS providers must be different.")

    allowed = set(ALL_TTS_PROVIDERS)
    for provider in main:
        if provider not in allowed:
            raise ValueError(
                f"Unsupported TTS provider: {provider}. "
                f"Allowed: {', '.join(ALL_TTS_PROVIDERS)}"
            )

    fallback_enabled = config.get("TTS_FALLBACK_ENABLED", True)
    if isinstance(fallback_enabled, str):
        fallback_enabled = fallback_enabled.strip().lower() in {
            "true", "yes", "on", "1"
        }

    fallback: list[str] = []
    if fallback_enabled:
        raw_order = str(
            config.get(
                "TTS_FALLBACK_ORDER",
                ",".join(ALL_TTS_PROVIDERS),
            )
        )
        for item in re.split(r"[,;]", raw_order):
            provider = normalize_provider(item)
            if not provider:
                continue
            if provider not in allowed:
                raise ValueError(
                    f"Unsupported TTS fallback provider: {provider}"
                )
            fallback.append(provider)

    result: list[str] = []
    for provider in main + fallback:
        if provider not in result:
            result.append(provider)

    return result


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
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

    config["TTS_ALLOW_PAID"] = bool(config.get("TTS_ALLOW_PAID", False))
    config["TTS_FALLBACK_ENABLED"] = bool(
        config.get("TTS_FALLBACK_ENABLED", True)
    )
    config["TTS_PROVIDER_ORDER"] = get_tts_provider_order(config)

    policy = str(
        config.get("FAILURE_POLICY", "retry_then_checkpoint")
    ).strip().lower()
    if policy not in {"retry_then_checkpoint", "skip", "stop"}:
        raise ValueError(
            "FAILURE_POLICY must be retry_then_checkpoint, skip, or stop."
        )
    config["FAILURE_POLICY"] = policy

    captions = str(config.get("CAPTIONS", "hindi")).strip().lower()
    if captions not in {"hindi", "english", "hinglish"}:
        raise ValueError("CAPTIONS must be hindi, english, or hinglish.")
    config["CAPTIONS"] = captions

    config["QUALITY"] = str(config.get("QUALITY", "high")).strip().lower()
    config["VISUAL_STYLE"] = str(
        config.get("VISUAL_STYLE", "cinematic_realistic")
    ).strip()
    config["STORY_LENGTH"] = str(
        config.get("STORY_LENGTH", "auto")
    ).strip().lower()
    config["SCENE_DURATION"] = str(
        config.get("SCENE_DURATION", "auto")
    ).strip().lower()

    return config


def load_and_validate(
    path: str | Path = "Input/topic.txt",
) -> dict[str, Any]:
    return validate_config(load_input_config(path))


def get_config_summary(config: dict[str, Any]) -> dict[str, Any]:
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
        "TTS_CHUNK_MAX_CHARS": int(
            config.get("TTS_CHUNK_MAX_CHARS", 500)
        ),
        "TTS_CHUNK_CHECKPOINT": bool(
            config.get("TTS_CHUNK_CHECKPOINT", True)
        ),
    }


def print_config_summary(config: dict[str, Any]) -> None:
    print("==============================================")
    print("INPUT CONFIG")
    print("==============================================")
    for key, value in get_config_summary(config).items():
        print(f"{key} = {value}")
    print("==============================================")


if __name__ == "__main__":
    import sys

    config_path = (
        sys.argv[1] if len(sys.argv) > 1 else "Input/topic.txt"
    )
    try:
        parsed = load_and_validate(config_path)
        print_config_summary(parsed)
        print("INPUT CONFIG VALIDATION: OK")
    except Exception as exc:
        print(f"INPUT CONFIG VALIDATION: FAILED: {exc}")
        raise
