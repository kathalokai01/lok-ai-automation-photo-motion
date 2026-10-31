#!/usr/bin/env python3
"""Prepare validated, resumable captions for Katha Lok AI."""

from __future__ import annotations

import hashlib
import json
import os
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

VERSION = "prepare-captions-v12-dynamic-short-parts"

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

LANGUAGE_FIELDS = {
    "hindi": ("caption_hindi", "hindi_caption", "caption_hi"),
    "english": ("caption_english", "english_caption", "caption_en"),
    "hinglish": (
        "caption_hinglish",
        "hinglish_caption",
        "caption_mixed",
    ),
}

LANGUAGE_KEYS = {
    "hindi": ("hindi", "hi", "hin", "हिंदी"),
    "english": ("english", "en", "eng", "अंग्रेज़ी"),
    "hinglish": ("hinglish", "mixed", "hi-en", "en-hi"),
}

SOURCE_FIELDS = ("text", "narration", "content", "story_text")


def log(*values):
    print(*values, flush=True)


def normalize_text(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


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


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_language(value):
    raw = normalize_text(str(value or "")).lower()
    return LANGUAGE_ALIASES.get(raw, raw)


def first_text(row, fields):
    if not isinstance(row, dict):
        return "", ""

    for field in fields:
        value = normalize_text(row.get(field))
        if value:
            return value, field

    return "", ""


def source_narration(row):
    return first_text(row, SOURCE_FIELDS)[0]


def nested_text(value):
    if isinstance(value, str):
        return normalize_text(value)

    if isinstance(value, dict):
        return first_text(
            value,
            ("caption", "caption_text", "text", "value"),
        )[0]

    return ""


def find_nested(container, mode):
    if isinstance(container, dict):
        for key in LANGUAGE_KEYS[mode]:
            if key in container:
                text = nested_text(container[key])
                if text:
                    return text, key

        declared = normalize_language(
            container.get(
                "language",
                container.get("lang", container.get("language_code", "")),
            )
        )

        if declared == mode:
            text = nested_text(container)
            if text:
                return text, "language-labelled-entry"

    if isinstance(container, list):
        for index, item in enumerate(container):
            if not isinstance(item, dict):
                continue

            declared = normalize_language(
                item.get(
                    "language",
                    item.get("lang", item.get("language_code", "")),
                )
            )

            if declared == mode:
                text = nested_text(item)
                if text:
                    return text, f"list-entry-{index}"

    return "", ""


def choose_caption(row, mode):
    text, field = first_text(row, LANGUAGE_FIELDS[mode])

    if text:
        return text, field, "explicit_language_field"

    for container_name in ("caption_texts", "translations", "captions"):
        text, nested_key = find_nested(row.get(container_name), mode)

        if text:
            return (
                text,
                f"{container_name}.{nested_key}",
                "nested_language_text",
            )

    generic, generic_field = first_text(
        row,
        ("caption_text", "display_caption"),
    )

    declared = normalize_language(
        row.get("caption_language", row.get("caption_lang", ""))
    )

    if generic and declared == mode:
        return generic, generic_field, "declared_generic_caption"

    if mode == "hindi":
        source_language = normalize_language(
            row.get("language", row.get("narration_language", ""))
        )

        if source_language and source_language != "hindi":
            return "", "", "source_language_mismatch"

        original, original_field = first_text(row, SOURCE_FIELDS)

        if original:
            return original, original_field, "source_narration_default"

    return "", "", "requested_translation_missing"


def normalize_rows(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict) and isinstance(data.get("scenes"), list):
        return data["scenes"]

    raise RuntimeError("narration.json must contain a scenes list.")


def resolve_font_size(config, video_format):
    configured = get_caption_font_size(config)

    if str(configured).strip().lower() == "auto":
        return 24 if video_format == "short" else 28

    try:
        size = int(configured)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "CAPTION_FONT_SIZE must be auto or an integer from 16 to 64."
        ) from exc

    if not 16 <= size <= 64:
        raise RuntimeError(
            "CAPTION_FONT_SIZE must be between 16 and 64."
        )

    return size


def checkpoint_reusable(path, fingerprint, caption_hash):
    if not path.is_file():
        return None

    try:
        cached = read_json(path)
    except RuntimeError:
        return None

    if not isinstance(cached, dict):
        return None

    if (
        cached.get("status") != "completed"
        or cached.get("version") != VERSION
        or cached.get("fingerprint") != fingerprint
        or cached.get("caption_sha256") != caption_hash
        or not isinstance(cached.get("text"), str)
        or not cached["text"].strip()
    ):
        return None

    if digest(cached["text"]) != caption_hash:
        return None

    return cached


def resolve_actual_parts(narration_data, narration_rows, video_format,
                         configured_parts, scenes_per_part):
    """Determine part count from completed narration for SHORT."""
    if video_format == "full":
        return configured_parts

    declared = None

    if isinstance(narration_data, dict):
        try:
            value = narration_data.get("total_parts")
            if value is not None:
                declared = int(value)
        except (TypeError, ValueError):
            raise RuntimeError(
                "narration.json contains invalid total_parts."
            )

    mapped_parts = set()

    for index, row in enumerate(narration_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}.")

        try:
            part = int(row["part"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part number in narration row {index}."
            ) from exc

        if part < 1:
            raise RuntimeError("Narration part numbers must be positive.")

        mapped_parts.add(part)

    if not mapped_parts:
        raise RuntimeError("Narration contains no part records.")

    actual_parts = max(mapped_parts)

    if mapped_parts != set(range(1, actual_parts + 1)):
        raise RuntimeError("Narration part numbering is not continuous.")

    if declared is not None and declared != actual_parts:
        raise RuntimeError(
            "Narration total_parts does not match scene mappings."
        )

    if len(narration_rows) != actual_parts * scenes_per_part:
        raise RuntimeError(
            "Narration scene count does not match actual SHORT parts "
            "and configured SCENES per part."
        )

    return actual_parts


def validate_scene_mapping(scene_rows, parts, scenes_per_part):
    expected = [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    expected_set = set(expected)
    seen = set()

    for index, row in enumerate(scene_rows, start=1):
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

        if key not in expected_set:
            raise RuntimeError(f"Out-of-range narration scene: {key}.")

        seen.add(key)

    missing = sorted(expected_set - seen)

    if missing:
        raise RuntimeError(
            f"Narration manifest is missing scenes: {missing}"
        )

    if len(scene_rows) != len(expected):
        raise RuntimeError(
            f"Expected {len(expected)} narration scenes; "
            f"found {len(scene_rows)}."
        )

    return expected

def main():
    config = load_and_validate(CONFIG_FILE)

    video_format = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if video_format not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {video_format}")

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    raw_mode = normalize_text(
        str(config.get("CAPTIONS", "hindi"))
    ).lower()

    mode = LANGUAGE_ALIASES.get(raw_mode, raw_mode)

    if mode not in {"hindi", "english", "hinglish"}:
        raise RuntimeError(
            f"Invalid CAPTIONS={raw_mode!r}; "
            "choose hindi, english, or hinglish."
        )

    font = get_caption_font(config)
    font_size = resolve_font_size(config, video_format)

    if not NARRATION_FILE.is_file():
        raise RuntimeError(
            f"Narration manifest missing: {NARRATION_FILE}"
        )

    narration_data = read_json(NARRATION_FILE)
    narration_rows = normalize_rows(narration_data)

    parts = resolve_actual_parts(
        narration_data,
        narration_rows,
        video_format,
        configured_parts,
        scenes_per_part,
    )

    expected_count = parts * scenes_per_part
    expected_order = validate_scene_mapping(
        narration_rows,
        parts,
        scenes_per_part,
    )

    settings = {
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "configured_parts": configured_parts,
        "actual_parts": parts,
        "scenes_per_part": scenes_per_part,
        "font": font,
        "font_size": font_size,
    }

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    mapping = {}
    missing = []
    reused_count = 0
    prepared_count = 0

    for index, row in enumerate(narration_rows, start=1):
        part = int(row["part"])
        scene = int(row["scene"])
        key = (part, scene)

        original = source_narration(row)

        if not original:
            raise RuntimeError(
                f"Empty source narration for part {part}, scene {scene}."
            )

        text, source_field, method = choose_caption(row, mode)

        if not text:
            missing.append(f"part {part} scene {scene}")
            continue

        source_hash = digest(original)
        caption_hash = digest(text)

        fingerprint_data = {
            "settings": settings,
            "part": part,
            "scene": scene,
            "source_sha256": source_hash,
            "caption_sha256": caption_hash,
            "source_field": source_field,
            "selection_method": method,
        }

        fingerprint = digest(canonical_json(fingerprint_data))

        checkpoint_path = (
            CHECKPOINT_DIR
            / f"part_{part:02d}_scene_{scene:02d}.json"
        )

        cached = checkpoint_reusable(
            checkpoint_path,
            fingerprint,
            caption_hash,
        )

        if cached:
            checkpoint = cached
            reused = True
            reused_count += 1
        else:
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
                "selection_method": method,
                "translation_performed_here": False,
                "font": font,
                "font_size": font_size,
            }

            write_json(checkpoint_path, checkpoint)
            reused = False
            prepared_count += 1

        selected_text = normalize_text(checkpoint.get("text"))

        if not selected_text or digest(selected_text) != caption_hash:
            raise RuntimeError(
                f"Caption checkpoint integrity failure: {key}"
            )

        mapping[key] = {
            "part": part,
            "scene": scene,
            "text": selected_text,
            "caption_sha256": caption_hash,
            "source_text_sha256": source_hash,
            "status": "completed",
            "checkpoint_reused": reused,
            "caption_source_field": source_field,
            "selection_method": method,
            "caption_language": mode,
        }

        log(
            f"{'REUSED' if reused else 'PREPARED'} "
            f"{index}/{expected_count}: "
            f"part={part}, scene={scene}, language={mode}"
        )

    if missing:
        raise RuntimeError(
            f"CAPTIONS={mode}: matching caption text is missing for "
            f"{len(missing)} scene(s): {', '.join(missing[:20])}. "
            "This stage does not translate text. "
            "Provide language-specific caption fields or select Hindi "
            "when the narration itself is Hindi."
        )

    if set(mapping) != set(expected_order):
        raise RuntimeError("Final caption scene mapping is incomplete.")

    ordered = [mapping[key] for key in expected_order]

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
            "Selects and validates existing language-specific caption "
            "text. It does not perform translation."
        ),
        "configured_parts": configured_parts,
        "parts": parts,
        "dynamic_short_parts": video_format == "short",
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_count,
        "caption_font": font,
        "caption_font_size": font_size,
        "caption_font_size_setting": str(
            config.get("CAPTION_FONT_SIZE", "auto")
        ),
        "checkpoint_reused": reused_count,
        "checkpoint_prepared": prepared_count,
        "captions": ordered,
    }

    write_json(CAPTION_FILE, result)

    log("=" * 56)
    log("CAPTION PREPARATION COMPLETED")
    log("Format:", video_format)
    log("Configured parts:", configured_parts)
    log("Actual parts:", parts)
    log("Language:", mode)
    log("Scenes:", expected_count)
    log("Font:", font)
    log("Font size:", font_size)
    log("Reused checkpoints:", reused_count)
    log("New checkpoints:", prepared_count)
    log("Translation performed: no")
    log("External API calls: 0")
    log("Output:", CAPTION_FILE.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        sys.exit(1)