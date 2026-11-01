#!/usr/bin/env python3
"""Prepare deterministic, validated and resumable captions for Photo Motion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

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

VERSION = "prepare-captions-v13-exact-parts-language-safe"

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
    "hindi": (
        "caption_hindi", "hindi_caption", "caption_hi",
        "hindi", "caption_text_hi",
    ),
    "english": (
        "caption_english", "english_caption", "caption_en",
        "english", "caption_text_en",
    ),
    "hinglish": (
        "caption_hinglish", "hinglish_caption", "caption_mixed",
        "mixed_caption", "caption_text_hinglish",
    ),
}

LANGUAGE_KEYS = {
    "hindi": ("hindi", "hi", "hin", "हिंदी"),
    "english": ("english", "en", "eng", "अंग्रेज़ी"),
    "hinglish": ("hinglish", "mixed", "hi-en", "en-hi"),
}

SOURCE_FIELDS = (
    "text", "narration", "narration_text", "voiceover",
    "content", "story_text",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\ufeff", "").replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def sha256(value: Any) -> str:
    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json_atomic(path: Path, data: Any) -> None:
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


def normalize_language(value: Any) -> str:
    raw = normalized_text(str(value or "")).lower()
    return LANGUAGE_ALIASES.get(raw, raw)


def first_text(row: dict, fields: tuple[str, ...]) -> tuple[str, str]:
    for field in fields:
        value = clean_text(row.get(field))
        if value:
            return value, field
    return "", ""


def narration_text(row: dict) -> str:
    return first_text(row, SOURCE_FIELDS)[0]


def nested_caption_text(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)

    if isinstance(value, dict):
        return first_text(
            value,
            ("caption", "caption_text", "text", "value"),
        )[0]

    return ""


def find_nested_caption(container: Any, mode: str) -> tuple[str, str]:
    if isinstance(container, dict):
        for key in LANGUAGE_KEYS[mode]:
            if key in container:
                text = nested_caption_text(container[key])
                if text:
                    return text, key

        declared = normalize_language(
            container.get(
                "language",
                container.get("lang", container.get("language_code", "")),
            )
        )

        if declared == mode:
            text = nested_caption_text(container)
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
                text = nested_caption_text(item)
                if text:
                    return text, f"list-entry-{index}"

    return "", ""


def choose_caption(row: dict, mode: str) -> tuple[str, str, str]:
    text, field = first_text(row, LANGUAGE_FIELDS[mode])
    if text:
        return text, field, "explicit-language-field"

    for container_name in (
        "caption_texts", "translations", "captions",
    ):
        text, nested_key = find_nested_caption(
            row.get(container_name), mode
        )
        if text:
            return (
                text,
                f"{container_name}.{nested_key}",
                "nested-language-caption",
            )

    generic, generic_field = first_text(
        row,
        ("caption_text", "display_caption"),
    )

    declared = normalize_language(
        row.get("caption_language", row.get("caption_lang", ""))
    )

    if generic and declared == mode:
        return generic, generic_field, "declared-generic-caption"

    source = narration_text(row)
    source_language = normalize_language(
        row.get(
            "language",
            row.get("narration_language", row.get("source_language", "")),
        )
    )

    # Hindi captions may use the original Hindi narration without translation.
    if mode == "hindi" and source:
        if not source_language or source_language == "hindi":
            return source, "source-narration", "original-hindi-narration"

    # Hinglish is a mixed-language output, not an automatic translation.
    # Reuse the narration only when its declared language is Hindi/Hinglish
    # or when the text itself contains Devanagari characters.
    if mode == "hinglish" and source:
        has_devanagari = bool(re.search(r"[\u0900-\u097F]", source))
        if source_language in {"", "hindi", "hinglish"} or has_devanagari:
            return source, "source-narration", "original-mixed-narration"

    return "", "", "requested-language-caption-not-provided"


def narration_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("scenes")
        if not isinstance(rows, list):
            rows = data.get("jobs")
    else:
        rows = None

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            "narration.json must contain a non-empty scenes list."
        )

    if not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("narration.json contains an invalid scene row.")

    return rows


def resolve_font_size(config: dict, video_format: str) -> int:
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


def validate_scene_mapping(
    rows: list[dict],
    parts: int,
    scenes_per_part: int,
) -> list[tuple[int, int]]:
    expected = [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]
    expected_set = set(expected)
    seen: set[tuple[int, int]] = set()

    for index, row in enumerate(rows, 1):
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
            raise RuntimeError(f"Unexpected narration scene: {key}.")

        seen.add(key)

    missing = sorted(expected_set - seen)
    if missing:
        raise RuntimeError(
            f"Narration is missing configured scenes: {missing}"
        )

    if len(rows) != len(expected):
        raise RuntimeError(
            f"Expected {len(expected)} narration scenes; found {len(rows)}."
        )

    return expected

def checkpoint_path(part: int, scene: int) -> Path:
    return (
        CHECKPOINT_DIR
        / f"part_{part:02d}_scene_{scene:02d}.json"
    )


def load_reusable_checkpoint(
    path: Path,
    fingerprint: str,
    caption_hash: str,
) -> dict | None:
    if not path.is_file():
        return None

    try:
        cached = read_json(path)
    except RuntimeError:
        return None

    if not isinstance(cached, dict):
        return None

    if cached.get("status") != "completed":
        return None

    if cached.get("version") != VERSION:
        return None

    if cached.get("fingerprint") != fingerprint:
        return None

    if cached.get("caption_sha256") != caption_hash:
        return None

    text = cached.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    if sha256(text) != caption_hash:
        return None

    return cached


def positive_duration(row: dict) -> float | None:
    for field in (
        "actual_audio_duration_seconds",
        "audio_duration_seconds",
        "duration",
        "scene_duration",
        "narration_duration_estimate_seconds",
        "estimated_duration",
    ):
        value = row.get(field)
        if value is None or str(value).strip() == "":
            continue

        try:
            number = float(value)
        except (TypeError, ValueError):
            continue

        if 0.1 <= number <= 3600:
            return round(number, 3)

    return None


def build_caption_record(
    row: dict,
    text: str,
    mode: str,
    source_field: str,
    selection_method: str,
    font: str,
    font_size: int,
    reused: bool,
) -> dict:
    part = int(row["part"])
    scene = int(row["scene"])

    record = {
        "part": part,
        "scene": scene,
        "text": text,
        "caption": text,
        "caption_language": mode,
        "caption_source_field": source_field,
        "selection_method": selection_method,
        "source_text_sha256": sha256(narration_text(row)),
        "caption_sha256": sha256(text),
        "font": font,
        "font_size": font_size,
        "status": "completed",
        "checkpoint_reused": reused,
    }

    scene_id = row.get("scene_id")
    if isinstance(scene_id, str) and scene_id.strip():
        record["scene_id"] = scene_id.strip()

    duration = positive_duration(row)
    if duration is not None:
        record["narration_duration_estimate_seconds"] = duration

    # Final word-level timing is aligned against the generated audio by
    # burn_captions.py. This stage must not invent audio timestamps.
    record["timing_status"] = "pending_audio_alignment"

    return record


def main() -> None:
    config = load_and_validate(CONFIG_FILE)

    video_format = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if video_format not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {video_format!r}")

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    raw_mode = normalized_text(
        str(config.get("CAPTIONS", "hindi"))
    ).lower()
    mode = normalize_language(raw_mode)

    if mode not in {"hindi", "english", "hinglish"}:
        raise RuntimeError(
            f"Invalid CAPTIONS={raw_mode!r}. "
            "Use hindi, english or hinglish."
        )

    font = str(get_caption_font(config)).strip()
    if not font:
        raise RuntimeError("CAPTION_FONT resolved to an empty value.")

    font_size = resolve_font_size(config, video_format)

    if not NARRATION_FILE.is_file():
        raise RuntimeError(
            f"Narration manifest is missing: {NARRATION_FILE}"
        )

    narration_data = read_json(NARRATION_FILE)

    if isinstance(narration_data, dict):
        status = str(narration_data.get("status", "")).lower()
        if status and status != "completed":
            raise RuntimeError(
                f"Narration manifest is not completed: {status}"
            )

        declared_parts = narration_data.get("total_parts")
        if declared_parts is not None:
            try:
                declared_parts = int(declared_parts)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Narration total_parts is invalid."
                ) from exc

            if declared_parts != configured_parts:
                raise RuntimeError(
                    "Narration part count differs from Input/topic.txt: "
                    f"configured={configured_parts}, "
                    f"narration={declared_parts}."
                )

    rows = narration_rows(narration_data)

    # PARTS and SCENES are authoritative. Do not silently recalculate
    # SHORT part counts from the number of narration rows.
    expected_order = validate_scene_mapping(
        rows,
        configured_parts,
        scenes_per_part,
    )

    row_map = {
        (int(row["part"]), int(row["scene"])): row
        for row in rows
    }

    settings = {
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "parts": configured_parts,
        "scenes_per_part": scenes_per_part,
        "font": font,
        "font_size": font_size,
    }

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    CAPTION_FILE.parent.mkdir(parents=True, exist_ok=True)

    prepared: dict[tuple[int, int], dict] = {}
    missing: list[str] = []
    reused_count = 0
    created_count = 0

    for index, key in enumerate(expected_order, 1):
        part, scene = key
        row = row_map[key]

        original = narration_text(row)
        if not original:
            raise RuntimeError(
                f"Empty narration for part {part}, scene {scene}."
            )

        caption, source_field, method = choose_caption(row, mode)

        if not caption:
            missing.append(f"part {part}, scene {scene}")
            continue

        caption = normalized_text(caption)
        source_hash = sha256(original)
        caption_hash = sha256(caption)

        fingerprint_data = {
            "settings": settings,
            "part": part,
            "scene": scene,
            "source_sha256": source_hash,
            "caption_sha256": caption_hash,
            "source_field": source_field,
            "selection_method": method,
        }
        fingerprint = canonical_hash(fingerprint_data)

        path = checkpoint_path(part, scene)
        cached = load_reusable_checkpoint(
            path,
            fingerprint,
            caption_hash,
        )

        if cached:
            selected_text = normalized_text(cached["text"])
            reused = True
            reused_count += 1
        else:
            selected_text = caption
            checkpoint = {
                "status": "completed",
                "version": VERSION,
                "fingerprint": fingerprint,
                "part": part,
                "scene": scene,
                "text": selected_text,
                "caption_sha256": caption_hash,
                "source_text_sha256": source_hash,
                "caption_language": mode,
                "caption_source_field": source_field,
                "selection_method": method,
                "translation_performed_here": False,
                "font": font,
                "font_size": font_size,
            }
            write_json_atomic(path, checkpoint)
            reused = False
            created_count += 1

        if not selected_text or sha256(selected_text) != caption_hash:
            raise RuntimeError(
                f"Caption integrity check failed: part {part}, scene {scene}."
            )

        prepared[key] = build_caption_record(
            row=row,
            text=selected_text,
            mode=mode,
            source_field=source_field,
            selection_method=method,
            font=font,
            font_size=font_size,
            reused=reused,
        )

        log(
            f"{'REUSED' if reused else 'PREPARED'} "
            f"{index}/{len(expected_order)} "
            f"part={part} scene={scene} language={mode}"
        )

    if missing:
        raise RuntimeError(
            f"CAPTIONS={mode}: caption text is unavailable for "
            f"{len(missing)} scene(s): {', '.join(missing[:20])}. "
            "This stage does not call a translation API. "
            "Supply the requested language caption fields or select "
            "the narration's actual language."
        )

    if set(prepared) != set(expected_order):
        raise RuntimeError("Caption scene mapping is incomplete.")

    ordered = [prepared[key] for key in expected_order]

    source_hash = canonical_hash(narration_data)

    result = {
        "status": "completed",
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "source": "output/narration/narration.json",
        "source_sha256": source_hash,
        "model": "existing-caption-text",
        "api_calls": 0,
        "translation_performed": False,
        "translation_note": (
            "Uses existing language-specific captions. "
            "Hindi and declared mixed-language narration may use "
            "the original narration. No translation is claimed."
        ),
        "configured_parts": configured_parts,
        "parts": configured_parts,
        "dynamic_short_parts": False,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(expected_order),
        "caption_font": font,
        "caption_font_size": font_size,
        "caption_font_size_setting": str(
            config.get("CAPTION_FONT_SIZE", "auto")
        ),
        "timing_source": (
            "Actual audio alignment is performed by burn_captions.py."
        ),
        "checkpoint_reused": reused_count,
        "checkpoint_prepared": created_count,
        "captions": ordered,
    }

    write_json_atomic(CAPTION_FILE, result)

    log("=" * 56)
    log("CAPTION PREPARATION COMPLETED")
    log("Format:", video_format)
    log("Configured parts:", configured_parts)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", len(expected_order))
    log("Language:", mode)
    log("Font:", font)
    log("Font size:", font_size)
    log("Reused checkpoints:", reused_count)
    log("New checkpoints:", created_count)
    log("Translation API calls: 0")
    log("Output:", CAPTION_FILE.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        sys.exit(1)