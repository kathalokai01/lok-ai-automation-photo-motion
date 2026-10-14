
#!/usr/bin/env python3
"""Prepare validated, resumable scene captions from existing narration."""

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

OUTPUT = ROOT / "output"
NARRATION_FILE = OUTPUT / "narration" / "narration.json"
CAPTION_FILE = OUTPUT / "captions" / "captions.json"
CHECKPOINT_DIR = OUTPUT / "checkpoints" / "captions"

SUPPORTED_MODES = {"hindi", "english", "hinglish"}


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_value(config, key, default=""):
    value = config.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def normalize_mode(config):
    value = get_value(config, "CAPTIONS", "hindi").lower()
    aliases = {
        "hi": "hindi",
        "हिंदी": "hindi",
        "en": "english",
        "अंग्रेज़ी": "english",
        "hinglish": "hinglish",
        "mixed": "hinglish",
    }
    value = aliases.get(value, value)
    if value not in SUPPORTED_MODES:
        raise RuntimeError(
            f"Unsupported CAPTIONS={value!r}; use hindi, english, or hinglish"
        )
    return value


def get_scene_text(row):
    for key in ("text", "narration", "caption", "content", "story_text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()
    return ""


def normalize_scene_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("scenes", "narration", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    raise RuntimeError("Narration JSON has no recognized scene list")


def validate_font_config(font, size):
    allowed = {
        "Noto Sans Devanagari",
        "Mukta",
        "Hind",
        "Tiro Devanagari Hindi",
    }
    if font not in allowed:
        raise RuntimeError(
            f"Unsupported CAPTION_FONT={font!r}; supported: "
            + ", ".join(sorted(allowed))
        )
    if not isinstance(size, int) or not 16 <= size <= 64:
        raise RuntimeError("CAPTION_FONT_SIZE must resolve to an integer from 16 to 64")


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    expected_count = parts * scenes_per_part

    caption_mode = normalize_mode(config)
    font = get_caption_font(config)
    font_size = int(get_caption_font_size(config))
    validate_font_config(font, font_size)

    if not NARRATION_FILE.is_file():
        raise RuntimeError(f"Narration file missing: {NARRATION_FILE}")

    narration_data = read_json(NARRATION_FILE)
    rows = normalize_scene_rows(narration_data)
    if len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} narration scenes; found {len(rows)}"
        )

    source_hash = sha256_text(
        json.dumps(narration_data, ensure_ascii=False, sort_keys=True)
    )
    fingerprint = sha256_text(
        f"{source_hash}|{caption_mode}|{font}|{font_size}|{mode}|{parts}|{scenes_per_part}"
    )

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    caption_rows = []
    seen = set()
    api_calls = 0

    # Captions are prepared locally from the existing narration.
    # This deliberately avoids external translation/API calls and charges.
    for index, row in enumerate(rows, 1):
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

        text = get_scene_text(row)
        if not text:
            raise RuntimeError(f"Empty narration text for {key}")

        checkpoint_file = (
            CHECKPOINT_DIR / f"part_{part:02d}_scene_{scene:02d}.json"
        )
        caption_text = text

        # Hindi mode uses the original narration. English/Hinglish mode
        # does not falsely claim to translate; it keeps the source text.
        # This guarantees stable offline captions until a real translator
        # is intentionally configured.
        if checkpoint_file.is_file():
            try:
                cached = read_json(checkpoint_file)
                if (
                    cached.get("fingerprint") == fingerprint
                    and cached.get("part") == part
                    and cached.get("scene") == scene
                    and isinstance(cached.get("text"), str)
                    and cached["text"].strip()
                ):
                    caption_text = cached["text"].strip()
            except RuntimeError:
                pass

        checkpoint = {
            "status": "completed",
            "fingerprint": fingerprint,
            "source_sha256": source_hash,
            "part": part,
            "scene": scene,
            "text": caption_text,
            "mode": caption_mode,
        }
        write_json(checkpoint_file, checkpoint)

        caption_rows.append(
            {
                "part": part,
                "scene": scene,
                "text": caption_text,
                "source_text_sha256": sha256_text(text),
                "caption_sha256": sha256_text(caption_text),
                "status": "completed",
            }
        )
        log(f"CAPTION PREPARED {index}/{expected_count}: {key}")

    expected_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    if seen != expected_keys:
        missing = sorted(expected_keys - seen)
        extra = sorted(seen - expected_keys)
        raise RuntimeError(
            f"Narration scene mapping incomplete; missing={missing}, extra={extra}"
        )

    result = {
        "status": "completed",
        "format": mode,
        "mode": caption_mode,
        "source": "output/narration/narration.json",
        "source_sha256": source_hash,
        "fingerprint": fingerprint,
        "model": "local-existing-narration",
        "api_calls": api_calls,
        "total_scenes": expected_count,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "caption_font": font,
        "caption_font_size": font_size,
        "captions": caption_rows,
    }
    write_json(CAPTION_FILE, result)

    log("=" * 58)
    log("CAPTIONS PREPARED")
    log(f"FORMAT: {mode}")
    log(f"CAPTION MODE: {caption_mode}")
    log(f"SCENES: {expected_count}")
    log(f"FONT: {font} / {font_size}")
    log("EXTERNAL API CALLS: 0")
    log(f"OUTPUT: {CAPTION_FILE.relative_to(ROOT)}")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        sys.exit(1)
