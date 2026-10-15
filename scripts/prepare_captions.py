#!/usr/bin/env python3
"""Prepare language-aware, validated and resumable scene captions."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_caption_font,
    get_caption_font_size,
)

CONFIG_FILE = ROOT / "Input" / "topic.txt"
NARRATION_FILE = ROOT / "output" / "narration" / "narration.json"
CAPTION_FILE = ROOT / "output" / "captions" / "captions.json"
CHECKPOINT_DIR = ROOT / "output" / "checkpoints" / "captions"

VERSION = "prepare-captions-v7-consistent-auto-font-size"

SUPPORTED_MODES = {"hindi", "english", "hinglish"}

SUPPORTED_FONTS = {
    "Noto Sans Devanagari",
    "Mukta",
    "Hind",
    "Tiro Devanagari Hindi",
}

LANGUAGE_FIELDS = {
    "hindi": ("caption_hindi", "hindi_caption", "caption_hi"),
    "english": ("caption_english", "english_caption", "caption_en"),
    "hinglish": (
        "caption_hinglish",
        "hinglish_caption",
        "caption_mixed",
    ),
}

LANGUAGE_ALIASES = {
    "hi": "hindi",
    "hin": "hindi",
    "hindi": "hindi",
    "हिंदी": "hindi",
    "en": "english",
    "eng": "english",
    "english": "english",
    "अंग्रेज़ी": "english",
    "mixed": "hinglish",
    "hinglish": "hinglish",
    "hi-en": "hinglish",
    "en-hi": "hinglish",
}


def log(*values):
    print(*values, flush=True)


def digest(value):
    if not isinstance(value, bytes):
        value = str(value).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def read_json(path, required=True):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if required:
            raise RuntimeError(f"Cannot read {path}: {exc}") from exc
        return None

    if not isinstance(data, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path}")
    return data


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_text(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_mode(config):
    raw = normalize_text(str(config.get("CAPTIONS", "hindi"))).lower()
    mode = LANGUAGE_ALIASES.get(raw, raw)
    if mode not in SUPPORTED_MODES:
        raise RuntimeError(
            f"Invalid CAPTIONS={raw!r}; choose hindi, english, or hinglish."
        )
    return mode


def resolve_font_size(config, video_format):
    """Keep auto font size consistent with burn_captions.py."""
    configured = get_caption_font_size(config)

    if str(configured).strip().lower() == "auto":
        size = 30
    else:
        try:
            size = int(configured)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "CAPTION_FONT_SIZE must be auto or an integer from 16 to 64."
            ) from exc

    if not 16 <= size <= 64:
        raise RuntimeError("CAPTION_FONT_SIZE must be between 16 and 64.")
    return size


def first_text(row, fields):
    if not isinstance(row, dict):
        return "", ""

    for field in fields:
        value = normalize_text(row.get(field))
        if value:
            return value, field

    return "", ""


def source_narration(row):
    return first_text(
        row, ("text", "narration", "content", "story_text")
    )[0]


def normalize_language(value):
    raw = normalize_text(str(value or "")).lower()
    return LANGUAGE_ALIASES.get(raw, raw)


def language_keys(mode):
    keys = {
        "hindi": ("hindi", "hi", "hin", "हिंदी"),
        "english": ("english", "en", "eng", "अंग्रेज़ी"),
        "hinglish": ("hinglish", "mixed", "hi-en", "en-hi"),
    }
    return keys[mode]


def text_from_nested_value(value, preferred_fields):
    if isinstance(value, str):
        return normalize_text(value)

    if isinstance(value, dict):
        text, _ = first_text(value, preferred_fields)
        if text:
            return text

    return ""


def find_language_entry(container, mode, preferred_fields):
    if isinstance(container, dict):
        for key in language_keys(mode):
            if key in container:
                text = text_from_nested_value(
                    container[key], preferred_fields
                )
                if text:
                    return text, key

        declared = normalize_language(
            container.get(
                "language",
                container.get("lang", container.get("language_code", "")),
            )
        )
        if declared == mode:
            text = text_from_nested_value(container, preferred_fields)
            if text:
                return text, "language-labelled-entry"

    elif isinstance(container, list):
        for index, item in enumerate(container):
            if not isinstance(item, dict):
                continue

            declared = normalize_language(
                item.get(
                    "language",
                    item.get("lang", item.get("language_code", "")),
                )
            )
            if declared != mode:
                continue

            text = text_from_nested_value(item, preferred_fields)
            if text:
                return text, f"list-entry-{index}"

    return "", ""


def nested_caption(row, mode):
    containers = (
        ("caption_texts", ("caption", "caption_text", "text", "value")),
        ("translations", ("caption", "caption_text", "text", "value")),
        ("captions", ("caption", "caption_text", "text", "value")),
    )

    for container_name, fields in containers:
        container = row.get(container_name)
        text, nested_key = find_language_entry(container, mode, fields)
        if text:
            return text, f"{container_name}.{nested_key}"

    return "", ""


def choose_caption(row, mode):
    """Select existing caption text; never silently claim translation."""
    translated, field = first_text(row, LANGUAGE_FIELDS[mode])
    if translated:
        return translated, field, "explicit_language_field"

    translated, field = nested_caption(row, mode)
    if translated:
        return translated, field, "nested_language_text"

    generic, generic_field = first_text(
        row, ("caption_text", "display_caption")
    )
    declared_language = normalize_language(
        row.get("caption_language", row.get("caption_lang", ""))
    )
    if generic and declared_language == mode:
        return generic, generic_field, "declared_generic_caption"

    if mode == "hindi":
        source_language = normalize_language(
            row.get("language", row.get("narration_language", ""))
        )
        if source_language and source_language != "hindi":
            return "", "", "source_language_mismatch"

        original, original_field = first_text(
            row, ("text", "narration", "content", "story_text")
        )
        if original:
            return original, original_field, "source_narration_default"

    return "", "", "requested_translation_missing"


def normalize_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rows = data.get("scenes")
        if isinstance(rows, list):
            return rows
    raise RuntimeError("narration.json must contain a scenes list.")


def load_existing_checkpoint(path, fingerprint, expected_text_hash):
    if not path.is_file():
        return False

    try:
        cached = read_json(path)
    except RuntimeError:
        return False

    return (
        isinstance(cached, dict)
        and cached.get("status") == "completed"
        and cached.get("fingerprint") == fingerprint
        and cached.get("caption_sha256") == expected_text_hash
    )


def main():
    config = load_and_validate(CONFIG_FILE)
    video_format = get_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if video_format not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {video_format}")
    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    mode = normalize_mode(config)
    font = get_caption_font(config)

    if font not in SUPPORTED_FONTS:
        raise RuntimeError(
            f"Unsupported CAPTION_FONT={font!r}. Choose one of: "
            + ", ".join(sorted(SUPPORTED_FONTS))
        )

    font_size = resolve_font_size(config, video_format)

    if not NARRATION_FILE.is_file():
        raise RuntimeError(f"Narration manifest missing: {NARRATION_FILE}")

    narration_data = read_json(NARRATION_FILE)
    rows = normalize_rows(narration_data)
    expected_count = parts * scenes_per_part

    if len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} narration scenes, found {len(rows)}."
        )

    settings = {
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "font": font,
        "font_size": font_size,
    }

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    mapping = {}
    missing = []
    seen = set()

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}.")

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part/scene mapping in narration row {index}."
            ) from exc

        key = (part, scene)

        if key in seen:
            raise RuntimeError(f"Duplicate narration scene: {key}.")
        seen.add(key)

        if not (1 <= part <= parts and 1 <= scene <= scenes_per_part):
            raise RuntimeError(f"Out-of-range narration scene: {key}.")

        original = source_narration(row)
        if not original:
            raise RuntimeError(f"Empty source narration for {key}.")

        text, source_field, selection_method = choose_caption(row, mode)

        if not text:
            missing.append({
                "part": part,
                "scene": scene,
                "mode": mode,
                "reason": selection_method,
            })
            continue

        source_hash = digest(original)
        caption_hash = digest(text)

        fingerprint = digest(canonical_json({
            "settings": settings,
            "part": part,
            "scene": scene,
            "source_sha256": source_hash,
            "caption_sha256": caption_hash,
            "source_field": source_field,
            "selection_method": selection_method,
        }))

        checkpoint_path = (
            CHECKPOINT_DIR / f"part_{part:02d}_scene_{scene:02d}.json"
        )

        reused = load_existing_checkpoint(
            checkpoint_path, fingerprint, caption_hash
        )

        checkpoint = {
            "status": "completed",
            "version": VERSION,
            "fingerprint": fingerprint,
            "part": part,
            "scene": scene,
            "text": text,
            "caption_sha256": caption_hash,
            "source_text_sha256": source_hash,
            "caption_mode": mode,
            "caption_source_field": source_field,
            "selection_method": selection_method,
            "translation_performed_here": False,
            "font": font,
            "font_size": font_size,
        }

        write_json(checkpoint_path, checkpoint)

        mapping[key] = {
            "part": part,
            "scene": scene,
            "text": text,
            "caption_sha256": caption_hash,
            "source_text_sha256": source_hash,
            "status": "completed",
            "checkpoint_reused": reused,
            "caption_source_field": source_field,
            "selection_method": selection_method,
            "caption_language": mode,
        }

        log(
            f"{'REUSED' if reused else 'PREPARED'} "
            f"{index}/{expected_count}: part={part}, scene={scene}, "
            f"language={mode}, source={source_field}"
        )

    if missing:
        details = ", ".join(
            f"part {item['part']} scene {item['scene']}"
            for item in missing[:20]
        )

        raise RuntimeError(
            f"CAPTIONS={mode} was requested, but matching caption text "
            f"is missing for {len(missing)} scene(s): {details}. "
            "This script does not translate. Add matching language-specific "
            "caption text, use a language-keyed translations/caption_texts "
            "entry, or explicitly label a generic caption with "
            "caption_language."
        )

    expected_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    if seen != expected_keys:
        raise RuntimeError(
            "Narration scene mapping mismatch: "
            f"missing={sorted(expected_keys - seen)}; "
            f"extra={sorted(seen - expected_keys)}"
        )

    ordered = [
        mapping[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    result = {
        "status": "completed",
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "source": "output/narration/narration.json",
        "source_sha256": digest(canonical_json(narration_data)),
        "model": "existing-caption-text",
        "api_calls": 0,
        "translation_performed": False,
        "translation_note": (
            "Selects and validates existing caption text only. Supports "
            "direct language-specific fields, language-labelled generic "
            "captions, and nested translations/caption_texts/captions. "
            "Hindi may use source narration when no conflicting source "
            "language is declared. English and Hinglish require matching "
            "text already present in the manifest."
        ),
        "total_scenes": expected_count,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "caption_font": font,
        "caption_font_size": font_size,
        "caption_font_size_setting": str(
            config.get("CAPTION_FONT_SIZE", "auto")
        ),
        "captions": ordered,
    }

    write_json(CAPTION_FILE, result)

    log("=" * 56)
    log("CAPTION PREPARATION COMPLETED")
    log("Format:", video_format)
    log("Language:", mode)
    log("Scenes:", expected_count)
    log("Font:", font)
    log("Font size:", font_size)
    log("Auto font size is aligned with burn_captions.py: 30")
    log("Automatic translation: not performed")
    log("External API calls: 0")
    log("Output:", CAPTION_FILE.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        sys.exit(1)
