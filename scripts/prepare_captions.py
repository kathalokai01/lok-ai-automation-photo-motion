
#!/usr/bin/env python3
"""Prepare validated, resumable scene captions without paid API calls."""

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

OUTPUT = ROOT / "output"
NARRATION_FILE = OUTPUT / "narration" / "narration.json"
CAPTION_FILE = OUTPUT / "captions" / "captions.json"
CHECKPOINT_DIR = OUTPUT / "checkpoints" / "captions"

VERSION = "prepare-captions-v4-language-validated"
SUPPORTED_MODES = {"hindi", "english", "hinglish"}
SUPPORTED_FONTS = {
    "Noto Sans Devanagari",
    "Mukta",
    "Hind",
    "Tiro Devanagari Hindi",
}

# Prefer explicitly supplied caption translations. These are separate from
# narration text, so a language selection never falsely claims translation.
CAPTION_KEYS = {
    "hindi": (
        "caption_hindi",
        "hindi_caption",
        "caption_hi",
    ),
    "english": (
        "caption_english",
        "english_caption",
        "caption_en",
    ),
    "hinglish": (
        "caption_hinglish",
        "hinglish_caption",
        "caption_mixed",
    ),
}


def log(*args):
    print(*args, flush=True)


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path}")
    return value


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def get_value(config, key, default=""):
    value = config.get(key, default)
    return default if value is None else str(value).strip()


def normalize_mode(config):
    value = get_value(config, "CAPTIONS", "hindi").lower()
    aliases = {
        "hi": "hindi",
        "हिंदी": "hindi",
        "en": "english",
        "अंग्रेज़ी": "english",
        "mixed": "hinglish",
    }
    value = aliases.get(value, value)

    if value not in SUPPORTED_MODES:
        raise RuntimeError(
            f"Unsupported CAPTIONS={value!r}; use hindi, english, or hinglish"
        )
    return value


def resolve_font_size(config, video_format):
    configured = get_caption_font_size(config)

    if str(configured).strip().lower() == "auto":
        size = 46 if video_format == "short" else 32
    else:
        try:
            size = int(configured)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "CAPTION_FONT_SIZE must be auto or an integer from 16 to 64"
            ) from exc

    if not 16 <= size <= 64:
        raise RuntimeError(
            f"Resolved caption font size {size} is outside 16-64"
        )
    return size


def validate_font(font):
    if font not in SUPPORTED_FONTS:
        raise RuntimeError(
            f"Unsupported caption font {font!r}. Supported fonts: "
            + ", ".join(sorted(SUPPORTED_FONTS))
        )


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def first_nonempty(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value), key
    return "", ""


def source_text(row):
    return first_nonempty(
        row,
        ("text", "narration", "caption", "content", "story_text"),
    )[0]


def selected_caption_text(row, mode):
    # A specifically named translation field is the strongest signal that
    # the caption was intentionally prepared in the requested language.
    value, field = first_nonempty(row, CAPTION_KEYS[mode])
    if value:
        return value, field, True

    # A generic caption field may have been explicitly prepared upstream.
    # Do not assume it is translated: it must be accompanied by a language
    # label matching the requested output language.
    generic, generic_field = first_nonempty(
        row, ("caption_text", "display_caption")
    )
    declared = str(
        row.get("caption_language", row.get("caption_lang", ""))
    ).strip().lower()

    aliases = {
        "hi": "hindi",
        "en": "english",
        "mixed": "hinglish",
        "hinglish": "hinglish",
        "hindi": "hindi",
        "english": "english",
    }
    declared = aliases.get(declared, declared)

    if generic and declared == mode:
        return generic, generic_field, True

    # Reuse narration only when its source language is explicitly known to
    # match the requested caption mode. Otherwise fail instead of lying.
    source_language = str(
        row.get("language", row.get("narration_language", ""))
    ).strip().lower()
    source_language = aliases.get(source_language, source_language)

    if source_language == mode:
        original, original_field = first_nonempty(
            row, ("text", "narration", "content", "story_text")
        )
        if original:
            return original, original_field, False

    return "", "", False


def normalize_scene_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("scenes", "narration", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    raise RuntimeError("Narration JSON has no recognized scene list")


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    video_format = get_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if video_format not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {video_format}")
    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must both be at least 1")

    expected_count = parts * scenes_per_part
    mode = normalize_mode(config)
    font = get_caption_font(config)
    validate_font(font)
    font_size = resolve_font_size(config, video_format)

    if not NARRATION_FILE.is_file():
        raise RuntimeError(f"Narration file missing: {NARRATION_FILE}")

    narration_data = read_json(NARRATION_FILE)
    rows = normalize_scene_rows(narration_data)

    if len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} narration scenes; found {len(rows)}"
        )

    settings = {
        "version": VERSION,
        "mode": mode,
        "format": video_format,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "font": font,
        "font_size": font_size,
    }

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    seen = set()
    caption_by_key = {}
    missing_translations = []

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Narration scene {index} is not an object")

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Narration scene {index} has invalid part/scene mapping"
            ) from exc

        key = (part, scene)
        if key in seen:
            raise RuntimeError(f"Duplicate narration scene mapping: {key}")
        seen.add(key)

        if not (1 <= part <= parts and 1 <= scene <= scenes_per_part):
            raise RuntimeError(f"Out-of-range narration mapping: {key}")

        original = source_text(row)
        if not original:
            raise RuntimeError(f"Empty narration text for {key}")

        caption_text, caption_source_field, explicitly_prepared = (
            selected_caption_text(row, mode)
        )

        if not caption_text:
            missing_translations.append({
                "part": part,
                "scene": scene,
                "requested_language": mode,
            })
            continue

        source_hash = sha256_text(original)
        caption_hash = sha256_text(caption_text)
        fingerprint = sha256_text(json.dumps(
            {
                **settings,
                "part": part,
                "scene": scene,
                "source_text_sha256": source_hash,
                "caption_text_sha256": caption_hash,
                "caption_source_field": caption_source_field,
                "explicitly_prepared": explicitly_prepared,
            },
            ensure_ascii=False,
            sort_keys=True,
        ))

        checkpoint_file = (
            CHECKPOINT_DIR / f"part_{part:02d}_scene_{scene:02d}.json"
        )

        # The checkpoint is reusable only if its fingerprint matches the
        # current input, including the requested language and actual text.
        reused = False
        if checkpoint_file.is_file():
            try:
                cached = read_json(checkpoint_file)
                reused = (
                    isinstance(cached, dict)
                    and cached.get("status") == "completed"
                    and cached.get("fingerprint") == fingerprint
                    and cached.get("part") == part
                    and cached.get("scene") == scene
                    and cached.get("caption_sha256") == caption_hash
                    and cached.get("text") == caption_text
                )
            except RuntimeError:
                reused = False

        checkpoint = {
            "status": "completed",
            "version": VERSION,
            "fingerprint": fingerprint,
            "part": part,
            "scene": scene,
            "source_text_sha256": source_hash,
            "caption_sha256": caption_hash,
            "text": caption_text,
            "requested_caption_mode": mode,
            "caption_source_field": caption_source_field,
            "caption_language_verified_by_field": explicitly_prepared,
            "translation_performed_here": False,
            "caption_font": font,
            "caption_font_size": font_size,
        }
        write_json(checkpoint_file, checkpoint)

        caption_by_key[key] = {
            "part": part,
            "scene": scene,
            "text": caption_text,
            "source_text_sha256": source_hash,
            "caption_sha256": caption_hash,
            "status": "completed",
            "checkpoint_reused": reused,
            "caption_source_field": caption_source_field,
            "language_field_verified": explicitly_prepared,
        }

        log(
            f"{'REUSED' if reused else 'PREPARED'} "
            f"{index}/{expected_count}: part={part}, scene={scene}, mode={mode}"
        )

    if missing_translations:
        preview = ", ".join(
            f"{item['part']}/{item['scene']}"
            for item in missing_translations[:20]
        )
        raise RuntimeError(
            f"CAPTIONS={mode} was requested, but no matching caption text "
            f"or explicitly matching source language was found for "
            f"{len(missing_translations)} scene(s): {preview}. "
            "No automatic translation was attempted and no completed "
            "captions manifest was published. Supply the matching "
            "caption_hindi, caption_english, or caption_hinglish field "
            "for each scene, or provide a matching caption_language label."
        )

    expected_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    if seen != expected_keys:
        raise RuntimeError(
            "Narration mapping incomplete: "
            f"missing={sorted(expected_keys - seen)}; "
            f"extra={sorted(seen - expected_keys)}"
        )

    caption_rows = [
        caption_by_key[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    source_hash = sha256_text(json.dumps(
        narration_data,
        ensure_ascii=False,
        sort_keys=True,
    ))

    result = {
        "status": "completed",
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "source": "output/narration/narration.json",
        "source_sha256": source_hash,
        "model": "local-existing-caption-text",
        "api_calls": 0,
        "translation_performed": False,
        "translation_note": (
            "This script validates and selects existing language-specific "
            "caption text. It does not translate. If the requested language "
            "is unavailable, it fails instead of silently using another "
            "language."
        ),
        "total_scenes": expected_count,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "caption_font": font,
        "caption_font_size": font_size,
        "caption_font_size_setting": get_value(
            config, "CAPTION_FONT_SIZE", "auto"
        ),
        "captions": caption_rows,
    }

    write_json(CAPTION_FILE, result)

    log("=" * 56)
    log("CAPTIONS PREPARED AND LANGUAGE FIELDS VALIDATED")
    log("FORMAT:", video_format)
    log("CAPTION MODE:", mode)
    log("SCENES:", expected_count)
    log("FONT:", font)
    log("RESOLVED FONT SIZE:", font_size)
    log("AUTOMATIC TRANSLATION: not performed")
    log("EXTERNAL API CALLS: 0")
    log("OUTPUT:", CAPTION_FILE.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        sys.exit(1)
