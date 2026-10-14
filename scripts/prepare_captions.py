
#!/usr/bin/env python3
"""Prepare scene captions locally with exact mapping and resumable checkpoints."""

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

VERSION = "prepare-captions-v3-scene-checkpoints"
SUPPORTED_MODES = {"hindi", "english", "hinglish"}
SUPPORTED_FONTS = {
    "Noto Sans Devanagari",
    "Mukta",
    "Hind",
    "Tiro Devanagari Hindi",
}


def log(*args):
    print(*args, flush=True)


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
            f"Unsupported CAPTIONS={value!r}; "
            "use hindi, english, or hinglish"
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
            f"Resolved font size {size} is outside the supported range 16-64"
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


def get_scene_text(row):
    for key in ("text", "narration", "caption", "content", "story_text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value)
    return ""


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
    caption_mode = normalize_mode(config)
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

    # Per-scene fingerprints prevent one changed scene from invalidating
    # every other scene checkpoint.
    settings = {
        "version": VERSION,
        "mode": caption_mode,
        "format": video_format,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "font": font,
        "font_size": font_size,
    }

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    seen = set()
    caption_by_key = {}

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

        source_text = get_scene_text(row)
        if not source_text:
            raise RuntimeError(f"Empty narration text for {key}")

        source_hash = sha256_text(source_text)
        fingerprint = sha256_text(json.dumps(
            {
                **settings,
                "part": part,
                "scene": scene,
                "source_text_sha256": source_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
        ))

        checkpoint_file = (
            CHECKPOINT_DIR / f"part_{part:02d}_scene_{scene:02d}.json"
        )
        caption_text = source_text
        reused = False

        if checkpoint_file.is_file():
            try:
                cached = read_json(checkpoint_file)
                cached_text = cached.get("text", "")
                if (
                    cached.get("status") == "completed"
                    and cached.get("fingerprint") == fingerprint
                    and cached.get("part") == part
                    and cached.get("scene") == scene
                    and cached.get("source_text_sha256") == source_hash
                    and isinstance(cached_text, str)
                    and cached_text.strip()
                ):
                    caption_text = normalize_text(cached_text)
                    reused = True
            except (RuntimeError, AttributeError):
                # Corrupt or incompatible checkpoint: regenerate locally.
                reused = False

        # This step preserves the source narration text. It does not translate.
        checkpoint = {
            "status": "completed",
            "version": VERSION,
            "fingerprint": fingerprint,
            "part": part,
            "scene": scene,
            "source_text_sha256": source_hash,
            "caption_sha256": sha256_text(caption_text),
            "text": caption_text,
            "requested_caption_mode": caption_mode,
            "translation_performed": False,
            "caption_font": font,
            "caption_font_size": font_size,
        }
        write_json(checkpoint_file, checkpoint)

        caption_by_key[key] = {
            "part": part,
            "scene": scene,
            "text": caption_text,
            "source_text_sha256": source_hash,
            "caption_sha256": sha256_text(caption_text),
            "status": "completed",
            "checkpoint_reused": reused,
        }

        log(
            f"{'REUSED' if reused else 'PREPARED'} "
            f"{index}/{expected_count}: part={part}, scene={scene}"
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

    # Always publish captions in deterministic part/scene order.
    caption_rows = [
        caption_by_key[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    narration_source_hash = sha256_text(json.dumps(
        narration_data,
        ensure_ascii=False,
        sort_keys=True,
    ))

    result = {
        "status": "completed",
        "version": VERSION,
        "format": video_format,
        "mode": caption_mode,
        "source": "output/narration/narration.json",
        "source_sha256": narration_source_hash,
        "model": "local-existing-narration",
        "api_calls": 0,
        "translation_performed": False,
        "translation_note": (
            "CAPTIONS selects the requested mode, but this script preserves "
            "the existing narration text and does not translate it."
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
    log("CAPTIONS PREPARED")
    log("FORMAT:", video_format)
    log("REQUESTED CAPTION MODE:", caption_mode)
    log("SCENES:", expected_count)
    log("FONT:", font)
    log("RESOLVED FONT SIZE:", font_size)
    log("TRANSLATION: not performed; source text preserved")
    log("EXTERNAL API CALLS: 0")
    log("OUTPUT:", CAPTION_FILE.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        sys.exit(1)
