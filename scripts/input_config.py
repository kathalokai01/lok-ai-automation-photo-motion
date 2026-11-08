#!/usr/bin/env python3
"""Validated configuration loader for the Katha Lok AI photo-motion pipeline."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALL_TTS_PROVIDERS = (
    "fish_audio", "gemini_tts", "edge_tts", "piper",
    "elevenlabs", "cartesia", "finevoice", "minimax",
)

ALL_IMAGE_PROVIDERS = (
    "huggingface", "pollinations", "gemini", "replicate",
)

PROVIDER_ALIASES = {
    "fish": "fish_audio",
    "fish audio": "fish_audio",
    "fishaudio": "fish_audio",
    "fish_audio": "fish_audio",
    "gemini": "gemini_tts",
    "gemini tts": "gemini_tts",
    "gemini_tts": "gemini_tts",
    "edge": "edge_tts",
    "edge tts": "edge_tts",
    "edge_tts": "edge_tts",
    "eleven lab": "elevenlabs",
    "eleven labs": "elevenlabs",
    "eleven_lab": "elevenlabs",
    "eleven_labs": "elevenlabs",
    "elevenlab": "elevenlabs",
    "elevenlabs": "elevenlabs",
    "mini max": "minimax",
    "mini_max": "minimax",
    "minimax": "minimax",
    "cartesia": "cartesia",
    "finevoice": "finevoice",
    "piper": "piper",
}

IMAGE_PROVIDER_ALIASES = {
    "hf": "huggingface",
    "hf_space": "huggingface",
    "hugging_face": "huggingface",
    "huggingface": "huggingface",
    "pollinations": "pollinations",
    "gemini": "gemini",
    "gemini_image": "gemini",
    "replicate": "replicate",
}

CAPTION_FONTS = {
    "noto sans devanagari": "Noto Sans Devanagari",
    "noto": "Noto Sans Devanagari",
    "noto sans": "Noto Sans Devanagari",
    "mukta": "Mukta",
    "hind": "Hind",
    "tiro devanagari hindi": "Tiro Devanagari Hindi",
    "tiro": "Tiro Devanagari Hindi",
}

SELECTION_SCHEMA_VERSION = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "FORMAT": "full",
    "AUDIENCE": "adult",
    "VOICE": "male",
    "SPEED": "+0%",
    "TOPIC": "",
    "TITLE": "",
    "STORY_TEXT": "",
    "PRESERVE_STORY_TEXT": True,

    "PARTS_SPEC": "1",
    "PARTS": 1,
    "PARTS_MIN": None,
    "PARTS_MAX": None,
    "PARTS_SELECTED": None,
    "PARTS_SELECTION_FILE": "output/parts_selection.json",

    "SCENES_SPEC": "1",
    "SCENES": 1,
    "SCENES_MIN": None,
    "SCENES_MAX": None,

    "STORY_LENGTH": "auto",
    "SCENE_DURATION": "auto",
    "CAPTIONS": "hindi",
    "CAPTION_FONT": "Hind",
    "CAPTION_FONT_SIZE": 28,

    "TTS_MAIN_1": "fish_audio",
    "TTS_MAIN_2": "gemini_tts",
    "TTS_MAIN_3": "edge_tts",
    "TTS_FALLBACK_ENABLED": True,
    "TTS_FALLBACK_ORDER": ",".join(ALL_TTS_PROVIDERS),
    "TTS_ALLOW_PAID": True,
    "TTS_CHUNK_MAX_CHARS": 500,
    "TTS_CHUNK_MIN_CHARS": 150,
    "TTS_CHUNK_CHECKPOINT": True,
    "TTS_VALIDATE_AUDIO": True,
    "TTS_MAX_PROVIDER_ATTEMPTS": 1,

    "IMAGE_PROVIDER_ORDER": ",".join(ALL_IMAGE_PROVIDERS),
    "IMAGE_FALLBACK_ENABLED": True,
    "IMAGE_ALLOW_BILLABLE": True,
    "ALLOW_BILLABLE_PROVIDERS": True,
    "HF_IMAGE_SPACE": "mrfakename/Z-Image-Turbo",
    "FREE_IMAGE_FALLBACK_SPACES": "",
    "POLLINATIONS_FREE_IMAGE_MODELS": (
        "black-forest-labs/flux.1-schnell,"
        "tongyi-mai/z-image-turbo,"
        "lykon/dreamshaper-8-lcm"
    ),
    "GEMINI_IMAGE_MODEL": "gemini-2.5-flash-image",
    "REPLICATE_IMAGE_MODEL": "black-forest-labs/flux-schnell",

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

    "PHOTO_MOTION_STYLE": "dynamic",
    "PHOTO_MOTION_PARALLAX": True,
    "PHOTO_MOTION_PARALLAX_STRENGTH": 0.045,
    "PHOTO_MOTION_SPEED": "fast",
    "PHOTO_MOTION_VARIATION": "high",
    "PHOTO_MOTION_PRESERVE_SUBJECT": True,

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

_TRUE_VALUES = {"true", "yes", "on", "1", "enabled"}
_FALSE_VALUES = {"false", "no", "off", "0", "disabled"}

_PARTS_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_PARTS_SINGLE_RE = re.compile(r"^(\d+)$")
_SCENES_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_SCENES_SINGLE_RE = re.compile(r"^(\d+)$")


def parse_bool(value: Any, key: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise ValueError(f"{key} must be a valid true/false value.")


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
        return parse_bool(value, key)

    if key in {"PARTS", "PARTS_SPEC", "SCENES", "SCENES_SPEC"}:
        return value

    if key in INT_KEYS:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer.") from exc

    if key == "PHOTO_MOTION_PARALLAX_STRENGTH":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "PHOTO_MOTION_PARALLAX_STRENGTH must be a number."
            ) from exc

    return value


def _find_assignment(line: str) -> tuple[str, str] | None:
    match = re.match(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$",
        line,
    )
    if not match:
        return None
    return match.group(1).upper(), match.group(2)


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
        return remainder[:closing], start + 1

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
            return "\n".join(collected), index + 1

        collected.append(current)

    raise ValueError(
        f"{key} multiline block is missing closing {delimiter}."
    )


def load_input_config(
    path: str | Path = "Input/topic.txt",
) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    file_path = Path(path).expanduser()

    if not file_path.is_file():
        raise FileNotFoundError(f"Input config not found: {file_path}")

    resolved_path = file_path.resolve()
    lines = resolved_path.read_text(
        encoding="utf-8-sig"
    ).splitlines()

    assigned: set[str] = set()
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
                assigned.add(key)
                continue
            if value.startswith("'''"):
                config[key], index = _read_multiline(
                    lines, index, key, "'''", value
                )
                assigned.add(key)
                continue

        config[key] = _parse_scalar(key, raw_value)
        assigned.add(key)
        index += 1

    if "PARTS" in assigned and "PARTS_SPEC" not in assigned:
        config["PARTS_SPEC"] = str(config["PARTS"]).strip()

    if "SCENES" in assigned and "SCENES_SPEC" not in assigned:
        config["SCENES_SPEC"] = str(config["SCENES"]).strip()

    config["_INPUT_CONFIG_PATH"] = str(resolved_path)

    # GitHub Actions selects SHORT or FULL per workflow dispatch. Applying
    # the override here also covers scripts that intentionally use the raw
    # loader instead of load_and_validate().
    format_override = os.environ.get("PHOTO_MOTION_FORMAT", "").strip()
    if format_override:
        config["FORMAT"] = format_override

    return config


def normalize_format(config: dict[str, Any]) -> str:
    value = str(config.get("FORMAT", "")).strip().lower()

    if value not in {"short", "full"}:
        raise ValueError(
            "FORMAT must be exactly 'short' or 'full'. "
            "Use 'short' for vertical Shorts or 'full' for full-length video."
        )

    config["FORMAT"] = value
    return value


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


def parse_parts_range(value: Any) -> tuple[int, int]:
    """Parse a fixed PARTS count or inclusive range such as 2-8."""

    if isinstance(value, bool):
        raise ValueError(
            "PARTS must be a positive integer or an inclusive range."
        )

    if isinstance(value, int):
        minimum = maximum = value
    else:
        raw = str(value).strip()
        single = _PARTS_SINGLE_RE.fullmatch(raw)
        ranged = _PARTS_RANGE_RE.fullmatch(raw)

        if single:
            minimum = maximum = int(single.group(1))
        elif ranged:
            minimum = int(ranged.group(1))
            maximum = int(ranged.group(2))
        else:
            raise ValueError(
                "Invalid PARTS value. Use a fixed number such as PARTS=4 "
                "or a range such as PARTS=2-8. Reversed ranges are invalid."
            )

    if minimum < 1 or maximum < 1:
        raise ValueError("PARTS values must be positive integers.")

    if minimum > maximum:
        raise ValueError(
            f"Invalid PARTS range {minimum}-{maximum}: "
            "the first number cannot be greater than the second."
        )

    return minimum, maximum


def parse_scenes_range(value: Any) -> tuple[int, int]:
    """Parse a fixed SCENES count or inclusive range such as 7-15."""

    if isinstance(value, bool):
        raise ValueError(
            "SCENES must be a positive integer or an inclusive range."
        )

    if isinstance(value, int):
        minimum = maximum = value
    else:
        raw = str(value).strip()
        single = _SCENES_SINGLE_RE.fullmatch(raw)
        ranged = _SCENES_RANGE_RE.fullmatch(raw)

        if single:
            minimum = maximum = int(single.group(1))
        elif ranged:
            minimum = int(ranged.group(1))
            maximum = int(ranged.group(2))
        else:
            raise ValueError(
                "Invalid SCENES value. Use a fixed number such as SCENES=7 "
                "or an inclusive range such as SCENES=7-15."
            )

    if minimum < 1 or maximum < 1:
        raise ValueError("SCENES values must be positive integers.")

    if minimum > maximum:
        raise ValueError(
            f"Invalid SCENES range {minimum}-{maximum}: "
            "the first number cannot be greater than the second."
        )

    if maximum > 500:
        raise ValueError("SCENES maximum cannot exceed 500.")

    return minimum, maximum


def _explicit_range_bound(value: Any, key: str) -> int:
    """Parse an explicitly supplied range bound without truncating values."""

    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive integer.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        result = int(value.strip())
    else:
        raise ValueError(f"{key} must be a positive integer.")
    if result < 1:
        raise ValueError(f"{key} must be greater than zero.")
    return result


def get_scenes_range(config: dict[str, Any]) -> tuple[int, int]:
    """Return validated inclusive (minimum, maximum) SCENES bounds."""

    specification = config.get("SCENES_SPEC")

    if specification in (None, ""):
        specification = config.get("SCENES", 1)

    minimum, maximum = parse_scenes_range(specification)

    explicit_min = config.get("SCENES_MIN")
    explicit_max = config.get("SCENES_MAX")

    has_explicit_min = explicit_min not in (None, "")
    has_explicit_max = explicit_max not in (None, "")
    if has_explicit_min != has_explicit_max:
        raise ValueError(
            "SCENES_MIN and SCENES_MAX must be supplied together."
        )

    if has_explicit_min and has_explicit_max:
        supplied = (
            _explicit_range_bound(explicit_min, "SCENES_MIN"),
            _explicit_range_bound(explicit_max, "SCENES_MAX"),
        )
        if supplied[0] > supplied[1]:
            raise ValueError(
                "SCENES_MIN cannot be greater than SCENES_MAX."
            )

        if supplied != (minimum, maximum):
            raise ValueError(
                "SCENES configuration mismatch: "
                f"SCENES_SPEC={minimum}-{maximum}, "
                f"SCENES_MIN={supplied[0]}, SCENES_MAX={supplied[1]}."
            )

    config["SCENES_SPEC"] = (
        f"{minimum}-{maximum}" if minimum != maximum else str(minimum)
    )
    config["SCENES_MIN"] = minimum
    config["SCENES_MAX"] = maximum
    return minimum, maximum


def get_scenes(config: dict[str, Any]) -> int:
    """Backward-compatible integer getter; returns the configured minimum.

    Variable-scene consumers must use get_scenes_range(), not treat this
    compatibility value as the actual count for every Part.
    """

    minimum, _maximum = get_scenes_range(config)
    config["SCENES"] = minimum
    return minimum


def get_scenes_max(config: dict[str, Any]) -> int:
    return get_scenes_range(config)[1]


def get_parts_range(config: dict[str, Any]) -> tuple[int, int]:
    """Return PARTS bounds; FULL mode is always exactly one Part."""

    mode = normalize_format(config)

    if mode == "full":
        config["PARTS_SPEC"] = "1"
        config["PARTS_MIN"] = 1
        config["PARTS_MAX"] = 1
        return 1, 1

    specification = config.get("PARTS_SPEC")
    if specification in (None, ""):
        specification = config.get("PARTS", 1)

    minimum, maximum = parse_parts_range(specification)

    explicit_min = config.get("PARTS_MIN")
    explicit_max = config.get("PARTS_MAX")

    has_explicit_min = explicit_min not in (None, "")
    has_explicit_max = explicit_max not in (None, "")
    if has_explicit_min != has_explicit_max:
        raise ValueError(
            "PARTS_MIN and PARTS_MAX must be supplied together."
        )

    if has_explicit_min and has_explicit_max:
        supplied = (
            _explicit_range_bound(explicit_min, "PARTS_MIN"),
            _explicit_range_bound(explicit_max, "PARTS_MAX"),
        )
        if supplied[0] > supplied[1]:
            raise ValueError(
                "PARTS_MIN cannot be greater than PARTS_MAX."
            )

        if supplied != (minimum, maximum):
            raise ValueError(
                "PARTS configuration mismatch: "
                f"PARTS_SPEC={minimum}-{maximum}, "
                f"PARTS_MIN={supplied[0]}, PARTS_MAX={supplied[1]}."
            )

    config["PARTS_MIN"] = minimum
    config["PARTS_MAX"] = maximum
    return minimum, maximum


def _project_root(config: dict[str, Any]) -> Path:
    raw = config.get("_INPUT_CONFIG_PATH")

    if raw:
        input_path = Path(str(raw)).expanduser().resolve()
        if input_path.parent.name.lower() == "input":
            return input_path.parent.parent
        return input_path.parent

    return Path.cwd().resolve()


def _selection_file(config: dict[str, Any]) -> Path:
    raw = str(
        config.get("PARTS_SELECTION_FILE")
        or DEFAULT_CONFIG["PARTS_SELECTION_FILE"]
    ).strip()

    if not raw:
        raise ValueError("PARTS_SELECTION_FILE cannot be empty.")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _project_root(config) / path

    return path.resolve()


def _parts_fingerprint(config: dict[str, Any]) -> str:
    minimum, maximum = get_parts_range(config)

    payload = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "format": normalize_format(config),
        "parts_spec": [minimum, maximum],
        "topic": str(config.get("TOPIC", "")).strip(),
        "title": str(config.get("TITLE", "")).strip(),
        "story_text": get_story_text(config).strip(),
        "input_path": str(
            Path(
                str(config.get("_INPUT_CONFIG_PATH") or Path.cwd())
            ).resolve()
        ),
    }

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def _selection_record_hash(record: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key != "record_sha256"
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_selection_record(
    config: dict[str, Any],
) -> dict[str, Any] | None:
    path = _selection_file(config)

    if not path.is_file():
        return None

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if not isinstance(record, dict):
        return None
    if record.get("schema_version") != SELECTION_SCHEMA_VERSION:
        return None
    if record.get("input_fingerprint") != _parts_fingerprint(config):
        return None
    if record.get("format") != normalize_format(config):
        return None

    minimum, maximum = get_parts_range(config)

    if record.get("minimum_parts") != minimum:
        return None
    if record.get("maximum_parts") != maximum:
        return None

    expected_spec = (
        f"{minimum}-{maximum}" if minimum != maximum else str(minimum)
    )
    if record.get("parts_spec") != expected_spec:
        return None

    raw_selected = record.get("selected_parts")
    if isinstance(raw_selected, bool):
        return None

    try:
        selected = int(raw_selected)
    except (TypeError, ValueError):
        return None

    if not minimum <= selected <= maximum:
        return None
    if record.get("record_sha256") != _selection_record_hash(record):
        return None

    return record


def get_selected_parts_record(
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Return only the valid selection record matching the current input."""

    record = _read_selection_record(config)
    return dict(record) if record is not None else None


def get_selected_parts(
    config: dict[str, Any],
    required: bool = False,
) -> int | None:
    """Return the real persisted selected count, never a range minimum."""

    minimum, maximum = get_parts_range(config)
    raw_selected = config.get("PARTS_SELECTED")

    if raw_selected not in (None, ""):
        if isinstance(raw_selected, bool):
            raise ValueError("PARTS_SELECTED must be a positive integer.")
        try:
            memory_selected = int(raw_selected)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "PARTS_SELECTED must be a positive integer."
            ) from exc
        if not minimum <= memory_selected <= maximum:
            raise ValueError(
                f"PARTS_SELECTED={memory_selected} is outside "
                f"the configured PARTS range {minimum}-{maximum}."
            )
    else:
        memory_selected = None

    record = _read_selection_record(config)

    if minimum == maximum:
        selected = minimum
        if memory_selected not in (None, selected):
            raise ValueError(
                "PARTS_SELECTED does not match the fixed PARTS setting."
            )
        if record is None:
            set_selected_parts(config, selected, source="fixed_input")
            record = _read_selection_record(config)
        if record is None:
            raise RuntimeError(
                "The fixed PARTS selection could not be persisted."
            )
        config["PARTS_SELECTED"] = selected
        config["PARTS"] = selected
        config["PARTS_SELECTION_RECORD"] = dict(record)
        return selected

    if record is not None:
        selected = int(record["selected_parts"])
        if memory_selected not in (None, selected):
            raise ValueError(
                "In-memory PARTS_SELECTED does not match the persisted "
                "selection record."
            )
        config["PARTS_SELECTED"] = selected
        config["PARTS"] = selected
        config["PARTS_SELECTION_RECORD"] = dict(record)
        return selected

    config["PARTS_SELECTED"] = None
    config["PARTS"] = None
    config.pop("PARTS_SELECTION_RECORD", None)

    if required:
        if memory_selected is not None:
            raise RuntimeError(
                "PARTS_SELECTED exists only in memory and has no matching "
                "persisted selection record. Call set_selected_parts()."
            )
        raise RuntimeError(
            "The actual PARTS count has not been selected. Choose a count "
            "within the configured range and persist it with "
            "set_selected_parts() before downstream generation."
        )

    return None


def get_parts(config: dict[str, Any]) -> int:
    """Return the actual persisted count, never the range minimum."""

    selected = get_selected_parts(config, required=True)
    if selected is None:
        raise RuntimeError("Actual PARTS count is not available.")
    return selected


def set_selected_parts(
    config: dict[str, Any],
    count: int,
    *,
    source: str = "story_length_policy",
    story_word_count: int | None = None,
) -> int:
    """Validate and atomically persist the selected count for all processes."""

    minimum, maximum = get_parts_range(config)

    if isinstance(count, bool):
        raise ValueError("Selected part count must be a positive integer.")

    try:
        selected = int(count)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Selected part count must be a positive integer."
        ) from exc

    if not minimum <= selected <= maximum:
        raise ValueError(
            f"Selected part count {selected} is outside "
            f"the allowed range {minimum}-{maximum}."
        )

    if story_word_count is not None:
        if isinstance(story_word_count, bool):
            raise ValueError(
                "story_word_count must be a non-negative integer."
            )
        try:
            story_word_count = int(story_word_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "story_word_count must be a non-negative integer."
            ) from exc
        if story_word_count < 0:
            raise ValueError(
                "story_word_count must be a non-negative integer."
            )

    fingerprint = _parts_fingerprint(config)
    path = _selection_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    expected_spec = (
        f"{minimum}-{maximum}" if minimum != maximum else str(minimum)
    )

    record = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "input_fingerprint": fingerprint,
        "format": normalize_format(config),
        "parts_spec": expected_spec,
        "minimum_parts": minimum,
        "maximum_parts": maximum,
        "selected_parts": selected,
        "selection_source": str(source).strip() or "unspecified",
        "story_word_count": story_word_count,
        "selected_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    record["record_sha256"] = _selection_record_hash(record)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                record,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    config["PARTS_SPEC"] = expected_spec
    config["PARTS_MIN"] = minimum
    config["PARTS_MAX"] = maximum
    config["PARTS_SELECTED"] = selected
    config["PARTS"] = selected
    config["PARTS_SELECTION_RECORD"] = dict(record)
    return selected


def clear_selected_parts(config: dict[str, Any]) -> None:
    """Clear only the selection belonging to the current input fingerprint."""

    path = _selection_file(config)
    record = _read_selection_record(config)

    if record is not None:
        path.unlink(missing_ok=True)

    config["PARTS_SELECTED"] = None
    config.pop("PARTS_SELECTION_RECORD", None)

    minimum, maximum = get_parts_range(config)
    config["PARTS"] = minimum if minimum == maximum else None


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


def get_caption_font(config: dict[str, Any]) -> str:
    raw = str(config.get("CAPTION_FONT", "Hind")).strip().lower()
    if raw not in CAPTION_FONTS:
        allowed = ", ".join(sorted(set(CAPTION_FONTS.values())))
        raise ValueError(
            f"Unsupported CAPTION_FONT: {raw!r}. Allowed fonts: {allowed}"
        )
    canonical = CAPTION_FONTS[raw]
    config["CAPTION_FONT"] = canonical
    return canonical


def get_caption_font_size(config: dict[str, Any]) -> int | str:
    raw = str(config.get("CAPTION_FONT_SIZE", 28)).strip().lower()

    if raw == "auto":
        config["CAPTION_FONT_SIZE"] = "auto"
        return "auto"

    try:
        size = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "CAPTION_FONT_SIZE must be 'auto' or an integer from 16 to 64."
        ) from exc

    if not 16 <= size <= 64:
        raise ValueError(
            "CAPTION_FONT_SIZE must be 'auto' or between 16 and 64."
        )

    config["CAPTION_FONT_SIZE"] = size
    return size


def get_topic(config: dict[str, Any]) -> str:
    return str(config.get("TOPIC", "")).strip()


def get_story_text(config: dict[str, Any]) -> str:
    value = config.get("STORY_TEXT", "")
    return value if isinstance(value, str) else str(value)


def get_title_source(config: dict[str, Any]) -> str:
    title = str(config.get("TITLE", "")).strip()
    return title or get_topic(config)


def has_story_input(config: dict[str, Any]) -> bool:
    return bool(
        get_topic(config)
        or get_story_text(config).strip()
        or str(config.get("TITLE", "")).strip()
    )


def normalize_provider(value: Any) -> str:
    provider = str(value or "").strip().lower().replace("-", "_")
    spaced = provider.replace("_", " ")
    return PROVIDER_ALIASES.get(
        spaced,
        PROVIDER_ALIASES.get(provider, provider),
    )


def normalize_image_provider(value: Any) -> str:
    provider = str(value or "").strip().lower().replace("-", "_")
    return IMAGE_PROVIDER_ALIASES.get(provider, provider)


def _split_provider_list(raw: Any) -> list[str]:
    if raw is None:
        return []

    if isinstance(raw, (list, tuple)):
        values = list(raw)
    elif isinstance(raw, set):
        values = sorted(raw, key=lambda item: str(item).lower())
    elif isinstance(raw, str):
        value = raw.strip()
        if not value:
            return []
        if value.startswith(("[", "(")):
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, (list, tuple, set)):
                values = list(parsed)
            else:
                values = re.split(r"[,;]", value)
        else:
            values = re.split(r"[,;]", value)
    else:
        values = [raw]

    result: list[str] = []
    for item in values:
        if item is None:
            continue
        text_value = str(item).strip().strip("'\"").strip()
        if text_value:
            result.append(text_value)
    return result

def get_tts_provider_order(config: dict[str, Any]) -> list[str]:
    """Return three primary providers followed by configured fallbacks."""

    main_keys = ("TTS_MAIN_1", "TTS_MAIN_2", "TTS_MAIN_3")
    main = [
        normalize_provider(config.get(key, ""))
        for key in main_keys
    ]

    if any(not provider for provider in main):
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

    for key, provider in zip(main_keys, main):
        config[key] = provider

    fallback_enabled = parse_bool(
        config.get("TTS_FALLBACK_ENABLED", True),
        "TTS_FALLBACK_ENABLED",
    )
    config["TTS_FALLBACK_ENABLED"] = fallback_enabled

    raw_order = config.get(
        "TTS_FALLBACK_ORDER",
        ",".join(ALL_TTS_PROVIDERS),
    )

    configured_fallback: list[str] = []
    for item in _split_provider_list(raw_order):
        provider = normalize_provider(item)
        if provider not in allowed:
            raise ValueError(
                f"Unsupported TTS fallback provider: {item!r}"
            )
        if provider not in configured_fallback:
            configured_fallback.append(provider)

    config["TTS_FALLBACK_ORDER"] = ",".join(configured_fallback)
    fallback = configured_fallback if fallback_enabled else []

    result: list[str] = []
    for provider in main + fallback:
        if provider not in result:
            result.append(provider)

    return result


def get_image_provider_order(config: dict[str, Any]) -> list[str]:
    raw = config.get(
        "IMAGE_PROVIDER_ORDER",
        ",".join(ALL_IMAGE_PROVIDERS),
    )
    requested = _split_provider_list(raw)

    if not requested:
        raise ValueError("IMAGE_PROVIDER_ORDER cannot be empty.")

    allowed = set(ALL_IMAGE_PROVIDERS)
    result: list[str] = []

    for item in requested:
        provider = normalize_image_provider(item)
        if provider not in allowed:
            raise ValueError(
                f"Unsupported image provider: {item!r}. "
                f"Allowed: {', '.join(ALL_IMAGE_PROVIDERS)}"
            )
        if provider not in result:
            result.append(provider)

    config["IMAGE_PROVIDER_ORDER"] = list(result)
    return result


def fallback_enabled_value(config: dict[str, Any]) -> bool:
    return parse_bool(
        config.get("IMAGE_FALLBACK_ENABLED", True),
        "IMAGE_FALLBACK_ENABLED",
    )


def get_global_billable_permission(config: dict[str, Any]) -> bool:
    configured = parse_bool(
        config.get("ALLOW_BILLABLE_PROVIDERS", True),
        "ALLOW_BILLABLE_PROVIDERS",
    )
    environment_value = os.environ.get("ALLOW_BILLABLE_PROVIDERS")

    if environment_value is None:
        return configured

    return configured and parse_bool(
        environment_value,
        "ALLOW_BILLABLE_PROVIDERS environment variable",
    )


def get_image_provider_settings(
    config: dict[str, Any],
) -> dict[str, Any]:
    order = get_image_provider_order(config)

    raw_hf_spaces = (
        str(config.get("HF_IMAGE_SPACE", "mrfakename/Z-Image-Turbo"))
        + ","
        + ",".join(
            _split_provider_list(
                config.get("FREE_IMAGE_FALLBACK_SPACES", "")
            )
        )
    )
    hf_spaces = _split_provider_list(raw_hf_spaces)

    pollinations_models = _split_provider_list(
        config.get(
            "POLLINATIONS_FREE_IMAGE_MODELS",
            DEFAULT_CONFIG["POLLINATIONS_FREE_IMAGE_MODELS"],
        )
    )

    gemini_model = str(
        config.get(
            "GEMINI_IMAGE_MODEL",
            DEFAULT_CONFIG["GEMINI_IMAGE_MODEL"],
        )
    ).strip()

    replicate_model = str(
        config.get(
            "REPLICATE_IMAGE_MODEL",
            DEFAULT_CONFIG["REPLICATE_IMAGE_MODEL"],
        )
    ).strip()

    if not hf_spaces:
        raise ValueError(
            "HF_IMAGE_SPACE must contain a Hugging Face Space."
        )
    if not pollinations_models:
        raise ValueError(
            "POLLINATIONS_FREE_IMAGE_MODELS cannot be empty."
        )
    if not gemini_model:
        raise ValueError("GEMINI_IMAGE_MODEL cannot be empty.")
    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        replicate_model,
    ):
        raise ValueError(
            "REPLICATE_IMAGE_MODEL must use owner/model-name format."
        )

    global_billable = get_global_billable_permission(config)
    input_image_billable = parse_bool(
        config.get("IMAGE_ALLOW_BILLABLE", True),
        "IMAGE_ALLOW_BILLABLE",
    )

    return {
        "IMAGE_PROVIDER_ORDER": order,
        "IMAGE_FALLBACK_ENABLED": fallback_enabled_value(config),
        "ALLOW_BILLABLE_PROVIDERS": global_billable,
        "IMAGE_ALLOW_BILLABLE": (
            global_billable and input_image_billable
        ),
        "HF_IMAGE_SPACE": hf_spaces[0],
        "FREE_IMAGE_FALLBACK_SPACES": hf_spaces[1:],
        "POLLINATIONS_FREE_IMAGE_MODELS": pollinations_models,
        "GEMINI_IMAGE_MODEL": gemini_model,
        "REPLICATE_IMAGE_MODEL": replicate_model,
    }


def _validate_story_settings(config: dict[str, Any]) -> None:
    story_length = str(
        config.get("STORY_LENGTH", "auto")
    ).strip().lower()

    if story_length not in {"auto", "short", "medium", "long"}:
        raise ValueError(
            "STORY_LENGTH must be auto, short, medium or long."
        )
    config["STORY_LENGTH"] = story_length

    scene_duration = str(
        config.get("SCENE_DURATION", "auto")
    ).strip().lower()

    if scene_duration not in {"auto", "fixed"}:
        raise ValueError("SCENE_DURATION must be auto or fixed.")
    config["SCENE_DURATION"] = scene_duration

    preserve = parse_bool(
        config.get("PRESERVE_STORY_TEXT", True),
        "PRESERVE_STORY_TEXT",
    )
    config["PRESERVE_STORY_TEXT"] = preserve

    if get_story_text(config).strip() and not preserve:
        raise ValueError(
            "STORY_TEXT is supplied but PRESERVE_STORY_TEXT is false. "
            "Set it to true to protect the supplied story."
        )


def _validate_voice_and_audience(config: dict[str, Any]) -> None:
    voice = str(config.get("VOICE", "male")).strip().lower()
    if voice not in {"male", "female"}:
        raise ValueError("VOICE must be 'male' or 'female'.")
    config["VOICE"] = voice

    audience = str(
        config.get("AUDIENCE", "adult")
    ).strip().lower()
    if audience not in {"adult", "kids"}:
        raise ValueError("AUDIENCE must be 'adult' or 'kids'.")
    config["AUDIENCE"] = audience

    speed = str(config.get("SPEED", "+0%")).strip()
    if not re.fullmatch(r"[+-]?\d{1,2}%", speed):
        raise ValueError(
            "SPEED must use a percentage such as -10%, +0% or +10%."
        )

    speed_number = int(speed.replace("%", ""))
    if not -50 <= speed_number <= 50:
        raise ValueError("SPEED must be between -50% and +50%.")
    config["SPEED"] = speed


def _validate_tts_settings(config: dict[str, Any]) -> None:
    for key in (
        "TTS_CHUNK_MAX_CHARS",
        "TTS_CHUNK_MIN_CHARS",
        "TTS_MAX_PROVIDER_ATTEMPTS",
    ):
        config[key] = _positive_int(
            config,
            key,
            DEFAULT_CONFIG[key],
        )

    if config["TTS_CHUNK_MIN_CHARS"] > config["TTS_CHUNK_MAX_CHARS"]:
        raise ValueError(
            "TTS_CHUNK_MIN_CHARS cannot exceed TTS_CHUNK_MAX_CHARS."
        )

    if config["TTS_MAX_PROVIDER_ATTEMPTS"] > 3:
        raise ValueError(
            "TTS_MAX_PROVIDER_ATTEMPTS cannot exceed 3."
        )

    config["TTS_FALLBACK_ENABLED"] = parse_bool(
        config.get("TTS_FALLBACK_ENABLED", True),
        "TTS_FALLBACK_ENABLED",
    )
    config["TTS_ALLOW_PAID"] = parse_bool(
        config.get("TTS_ALLOW_PAID", True),
        "TTS_ALLOW_PAID",
    )
    config["TTS_PROVIDER_ORDER"] = get_tts_provider_order(config)
    config["TTS_BILLABLE_ALLOWED"] = (
        get_global_billable_permission(config)
        and config["TTS_ALLOW_PAID"]
    )


def _validate_caption_settings(config: dict[str, Any]) -> None:
    captions = str(
        config.get("CAPTIONS", "hindi")
    ).strip().lower()

    if captions not in {"hindi", "english", "hinglish"}:
        raise ValueError(
            "CAPTIONS must be hindi, english or hinglish."
        )

    config["CAPTIONS"] = captions
    get_caption_font(config)
    get_caption_font_size(config)


def _validate_photo_motion_settings(
    config: dict[str, Any],
) -> None:
    style = str(
        config.get("PHOTO_MOTION_STYLE", "dynamic")
    ).strip().lower()

    allowed_styles = {
        "mixed", "zoom", "pan", "parallax", "dynamic", "static",
    }
    if style not in allowed_styles:
        raise ValueError(
            "PHOTO_MOTION_STYLE must be one of: "
            + ", ".join(sorted(allowed_styles))
        )
    config["PHOTO_MOTION_STYLE"] = style

    speed = str(
        config.get("PHOTO_MOTION_SPEED", "fast")
    ).strip().lower()
    if speed not in {"slow", "normal", "fast", "very_fast"}:
        raise ValueError(
            "PHOTO_MOTION_SPEED must be slow, normal, fast or very_fast."
        )
    config["PHOTO_MOTION_SPEED"] = speed

    variation = str(
        config.get("PHOTO_MOTION_VARIATION", "high")
    ).strip().lower()
    if variation not in {"low", "medium", "high"}:
        raise ValueError(
            "PHOTO_MOTION_VARIATION must be low, medium or high."
        )
    config["PHOTO_MOTION_VARIATION"] = variation

    config["PHOTO_MOTION_PARALLAX"] = parse_bool(
        config.get("PHOTO_MOTION_PARALLAX", True),
        "PHOTO_MOTION_PARALLAX",
    )
    config["PHOTO_MOTION_PRESERVE_SUBJECT"] = parse_bool(
        config.get("PHOTO_MOTION_PRESERVE_SUBJECT", True),
        "PHOTO_MOTION_PRESERVE_SUBJECT",
    )

    try:
        strength = float(
            config.get("PHOTO_MOTION_PARALLAX_STRENGTH", 0.045)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "PHOTO_MOTION_PARALLAX_STRENGTH must be a number."
        ) from exc

    if not 0.0 <= strength <= 0.055:
        raise ValueError(
            "PHOTO_MOTION_PARALLAX_STRENGTH must be between 0 and 0.055."
        )
    config["PHOTO_MOTION_PARALLAX_STRENGTH"] = strength


def _validate_general_settings(config: dict[str, Any]) -> None:
    quality = str(
        config.get("QUALITY", "high")
    ).strip().lower()
    if quality not in {"draft", "standard", "high"}:
        raise ValueError("QUALITY must be draft, standard or high.")
    config["QUALITY"] = quality

    failure_policy = str(
        config.get("FAILURE_POLICY", "retry_then_checkpoint")
    ).strip().lower()
    if failure_policy not in {
        "retry_then_checkpoint", "skip", "stop",
    }:
        raise ValueError(
            "FAILURE_POLICY must be "
            "retry_then_checkpoint, skip or stop."
        )
    config["FAILURE_POLICY"] = failure_policy

    for key in (
        "VISUAL_STYLE",
        "REALISM",
        "CAMERA_STYLE",
        "LIGHTING",
        "MOOD",
        "MUSIC_STYLE",
        "TRANSITIONS",
    ):
        value = str(
            config.get(key, DEFAULT_CONFIG.get(key, ""))
        ).strip()
        if not value:
            raise ValueError(f"{key} cannot be empty.")
        config[key] = value

    config["ALLOW_BILLABLE_PROVIDERS"] = (
        get_global_billable_permission(config)
    )


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise TypeError("Configuration must be a dictionary.")

    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)

    if config.get("PARTS_SPEC") in (None, ""):
        config["PARTS_SPEC"] = str(
            config.get("PARTS", 1)
        ).strip()

    if config.get("SCENES_SPEC") in (None, ""):
        config["SCENES_SPEC"] = str(
            config.get("SCENES", 1)
        ).strip()

    mode = normalize_format(config)

    for key in BOOL_KEYS:
        config[key] = parse_bool(
            config.get(key, DEFAULT_CONFIG[key]),
            key,
        )

    # FULL हमेशा एक ही Part में चलेगा।
    # Input में PARTS का कोई दूसरा मान हो तो उसे FULL के लिए लागू नहीं किया जाएगा।
    if mode == "full":
        config["PARTS_SPEC"] = "1"
        config["PARTS"] = 1
        config["PARTS_MIN"] = 1
        config["PARTS_MAX"] = 1
        config["PARTS_SELECTED"] = 1

    minimum_parts, maximum_parts = get_parts_range(config)
    config["PARTS_MIN"] = minimum_parts
    config["PARTS_MAX"] = maximum_parts

    raw_selected = config.get("PARTS_SELECTED")
    selected_parts: int | None = None

    if raw_selected not in (None, ""):
        if isinstance(raw_selected, bool):
            raise ValueError(
                "PARTS_SELECTED must be a positive integer."
            )
        try:
            selected_parts = int(raw_selected)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "PARTS_SELECTED must be an integer."
            ) from exc
        if not minimum_parts <= selected_parts <= maximum_parts:
            raise ValueError(
                f"PARTS_SELECTED={selected_parts} is outside "
                f"the configured PARTS range "
                f"{minimum_parts}-{maximum_parts}."
            )

    record = _read_selection_record(config)

    if minimum_parts == maximum_parts:
        if selected_parts not in (None, minimum_parts):
            raise ValueError(
                "PARTS_SELECTED does not match the fixed PARTS setting."
            )

        selected_parts = minimum_parts
        if record is None:
            set_selected_parts(
                config,
                selected_parts,
                source="fixed_input",
            )
            record = _read_selection_record(config)

        if record is None:
            raise RuntimeError(
                "The fixed PARTS selection could not be persisted."
            )

    elif record is not None:
        persisted_selected = int(record["selected_parts"])
        if selected_parts not in (None, persisted_selected):
            raise ValueError(
                "PARTS_SELECTED does not match the persisted selection."
            )
        selected_parts = persisted_selected
    else:
        selected_parts = None

    config["PARTS_SELECTED"] = selected_parts
    config["PARTS"] = selected_parts

    if record is not None:
        config["PARTS_SELECTION_RECORD"] = dict(record)
    else:
        config.pop("PARTS_SELECTION_RECORD", None)

    # SCENES अब fixed integer नहीं, inclusive Min-Max range है।
    scenes_min, scenes_max = get_scenes_range(config)
    config["SCENES_MIN"] = scenes_min
    config["SCENES_MAX"] = scenes_max

    # पुरानी integer आधारित consumers के लिए get_scenes() compatibility।
    # वास्तविक Range हमेशा SCENES_MIN / SCENES_MAX से प्राप्त होगी।
    config["SCENES"] = get_scenes(config)

    config["FPS"] = get_fps(config)
    config["MAX_RETRIES"] = get_max_retries(config)

    if scenes_max > 500:
        raise ValueError("SCENES maximum cannot exceed 500.")
    if maximum_parts > 1000:
        raise ValueError("PARTS cannot exceed 1000.")
    if maximum_parts * scenes_max > 1000:
        raise ValueError(
            "Configured maximum PARTS multiplied by maximum SCENES "
            "cannot exceed 1000."
        )

    if not has_story_input(config):
        raise ValueError(
            "Either TOPIC, TITLE or STORY_TEXT must be provided."
        )

    _validate_story_settings(config)
    _validate_voice_and_audience(config)
    _validate_caption_settings(config)
    _validate_tts_settings(config)
    _validate_photo_motion_settings(config)
    _validate_general_settings(config)

    config.update(get_image_provider_settings(config))
    return config


def load_and_validate(
    path: str | Path = "Input/topic.txt",
) -> dict[str, Any]:
    return validate_config(load_input_config(path))


def get_config_summary(config: dict[str, Any]) -> dict[str, Any]:
    image_settings = get_image_provider_settings(config)
    minimum_parts, maximum_parts = get_parts_range(config)
    selected_parts = get_selected_parts(
        config,
        required=False,
    )
    selection_record = get_selected_parts_record(config)
    scenes_min, scenes_max = get_scenes_range(config)

    return {
        "FORMAT": get_format(config),
        "TOPIC": get_topic(config),
        "TITLE": str(config.get("TITLE", "")).strip(),
        "HAS_STORY_TEXT": bool(get_story_text(config).strip()),
        "PRESERVE_STORY_TEXT": parse_bool(
            config.get("PRESERVE_STORY_TEXT", True),
            "PRESERVE_STORY_TEXT",
        ),
        "TITLE_SOURCE_AVAILABLE": bool(get_title_source(config)),

        "PARTS_SPEC": str(config.get("PARTS_SPEC", "")),
        "PARTS": selected_parts,
        "PARTS_MIN": minimum_parts,
        "PARTS_MAX": maximum_parts,
        "PARTS_SELECTED": selected_parts,
        "PARTS_SELECTION_PERSISTED": selection_record is not None,
        "PARTS_SELECTION_FILE": str(_selection_file(config)),

        "SCENES_SPEC": str(config.get("SCENES_SPEC", "")),
        "SCENES": get_scenes(config),
        "SCENES_MIN": scenes_min,
        "SCENES_MAX": scenes_max,
        "SCENES_RANGE": [scenes_min, scenes_max],

        "FPS": get_fps(config),
        "VOICE": str(config.get("VOICE", "")),
        "AUDIENCE": str(config.get("AUDIENCE", "")),
        "SPEED": str(config.get("SPEED", "+0%")),
        "CAPTIONS": str(config.get("CAPTIONS", "")),
        "CAPTION_FONT": get_caption_font(config),
        "CAPTION_FONT_SIZE": get_caption_font_size(config),
        "STORY_LENGTH": str(config.get("STORY_LENGTH", "")),
        "SCENE_DURATION": str(config.get("SCENE_DURATION", "")),
        "FAILURE_POLICY": str(config.get("FAILURE_POLICY", "")),

        "PHOTO_MOTION_STYLE": config["PHOTO_MOTION_STYLE"],
        "PHOTO_MOTION_SPEED": config["PHOTO_MOTION_SPEED"],
        "PHOTO_MOTION_VARIATION": config["PHOTO_MOTION_VARIATION"],
        "PHOTO_MOTION_PARALLAX": config["PHOTO_MOTION_PARALLAX"],
        "PHOTO_MOTION_PARALLAX_STRENGTH": (
            config["PHOTO_MOTION_PARALLAX_STRENGTH"]
        ),
        "PHOTO_MOTION_PRESERVE_SUBJECT": (
            config["PHOTO_MOTION_PRESERVE_SUBJECT"]
        ),

        "RESUME_ENABLED": parse_bool(
            config.get("RESUME_ENABLED", True),
            "RESUME_ENABLED",
        ),
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE": parse_bool(
            config.get("SAVE_CHECKPOINT_AFTER_EACH_SCENE", True),
            "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
        ),
        "SKIP_COMPLETED_SCENES": parse_bool(
            config.get("SKIP_COMPLETED_SCENES", True),
            "SKIP_COMPLETED_SCENES",
        ),

        "TTS_MAIN_1": config["TTS_MAIN_1"],
        "TTS_MAIN_2": config["TTS_MAIN_2"],
        "TTS_MAIN_3": config["TTS_MAIN_3"],
        "TTS_PROVIDER_ORDER": get_tts_provider_order(config),
        "TTS_ALLOW_PAID": parse_bool(
            config.get("TTS_ALLOW_PAID", True),
            "TTS_ALLOW_PAID",
        ),
        "TTS_BILLABLE_ALLOWED": bool(
            config.get("TTS_BILLABLE_ALLOWED", False)
        ),
        "TTS_FALLBACK_ENABLED": parse_bool(
            config.get("TTS_FALLBACK_ENABLED", True),
            "TTS_FALLBACK_ENABLED",
        ),
        "TTS_FALLBACK_ORDER": config["TTS_FALLBACK_ORDER"],
        "TTS_CHUNK_MAX_CHARS": int(
            config.get("TTS_CHUNK_MAX_CHARS", 500)
        ),
        "TTS_CHUNK_MIN_CHARS": int(
            config.get("TTS_CHUNK_MIN_CHARS", 150)
        ),
        "TTS_CHUNK_CHECKPOINT": parse_bool(
            config.get("TTS_CHUNK_CHECKPOINT", True),
            "TTS_CHUNK_CHECKPOINT",
        ),

        "IMAGE_PROVIDER_ORDER": image_settings["IMAGE_PROVIDER_ORDER"],
        "IMAGE_FALLBACK_ENABLED": image_settings[
            "IMAGE_FALLBACK_ENABLED"
        ],
        "ALLOW_BILLABLE_PROVIDERS": image_settings[
            "ALLOW_BILLABLE_PROVIDERS"
        ],
        "IMAGE_ALLOW_BILLABLE": image_settings[
            "IMAGE_ALLOW_BILLABLE"
        ],
        "IMAGE_HF_SPACES": (
            [image_settings["HF_IMAGE_SPACE"]]
            + image_settings["FREE_IMAGE_FALLBACK_SPACES"]
        ),
        "IMAGE_POLLINATIONS_MODELS": image_settings[
            "POLLINATIONS_FREE_IMAGE_MODELS"
        ],
        "IMAGE_GEMINI_MODEL": image_settings["GEMINI_IMAGE_MODEL"],
        "IMAGE_REPLICATE_MODEL": image_settings["REPLICATE_IMAGE_MODEL"],
    }


def print_config_summary(config: dict[str, Any]) -> None:
    print("==============================================")
    print("KATHA LOK AI INPUT CONFIG")
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

        minimum, maximum = get_parts_range(parsed)
        selected = get_selected_parts(parsed, required=False)

        if minimum != maximum and selected is None:
            print(
                "PARTS SELECTION: pending; the actual count must be "
                "selected and persisted before downstream generation."
            )

        scene_minimum, scene_maximum = get_scenes_range(parsed)
        print(
            "SCENES RANGE:",
            f"{scene_minimum}-{scene_maximum}",
        )

        if get_format(parsed) == "full":
            print("FULL MODE: exactly one Part is configured.")

        print("INPUT CONFIG VALIDATION: OK")

    except Exception as exc:
        print(f"INPUT CONFIG VALIDATION: FAILED: {exc}")
        raise