#!/usr/bin/env python3
"""Prepare complete, role-aware, language-safe and resumable captions."""

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

VERSION = "prepare-captions-v14-all-roles-verified"

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

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

EXTRA_ROLES = {"hook", "suspense", "resolution"}


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
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
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


def first_text(
    row: dict,
    fields: tuple[str, ...],
) -> tuple[str, str]:
    for field in fields:
        value = clean_text(row.get(field))
        if value:
            return value, field
    return "", ""


def narration_text(row: dict) -> str:
    return first_text(row, SOURCE_FIELDS)[0]


def get_scene_id(row: dict) -> str:
    return clean_text(row.get("scene_id") or row.get("id"))


def get_role(row: dict) -> str:
    role = clean_text(
        row.get("role")
        or row.get("scene_type")
        or row.get("scene_role")
        or "main"
    ).lower()

    if role in {"extra", "extra_scene"}:
        raise RuntimeError(
            "Extra narration must declare hook, suspense or resolution role."
        )

    return role


def nested_caption_text(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)

    if isinstance(value, dict):
        return first_text(
            value,
            ("caption", "caption_text", "text", "value"),
        )[0]

    return ""


def find_nested_caption(
    container: Any,
    mode: str,
) -> tuple[str, str]:
    if isinstance(container, dict):
        for key in LANGUAGE_KEYS[mode]:
            if key in container:
                value = nested_caption_text(container[key])
                if value:
                    return value, key

        declared = normalize_language(
            container.get(
                "language",
                container.get("lang", container.get("language_code", "")),
            )
        )

        if declared == mode:
            value = nested_caption_text(container)
            if value:
                return value, "language-labelled-entry"

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
                value = nested_caption_text(item)
                if value:
                    return value, f"list-entry-{index}"

    return "", ""


def choose_caption(
    row: dict,
    mode: str,
) -> tuple[str, str, str]:
    text, field = first_text(row, LANGUAGE_FIELDS[mode])
    if text:
        return text, field, "explicit-language-field"

    for container_name in (
        "caption_texts",
        "translations",
        "captions",
    ):
        text, nested_key = find_nested_caption(
            row.get(container_name),
            mode,
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
        row.get(
            "caption_language",
            row.get("caption_lang", ""),
        )
    )

    if generic and declared == mode:
        return generic, generic_field, "declared-generic-caption"

    source = narration_text(row)
    source_language = normalize_language(
        row.get(
            "language",
            row.get(
                "narration_language",
                row.get("source_language", ""),
            ),
        )
    )

    if mode == "hindi" and source:
        if source_language in {"", "hindi"}:
            return (
                source,
                "source-narration",
                "original-hindi-narration",
            )

    if mode == "hinglish" and source:
        has_devanagari = bool(
            re.search(r"[\u0900-\u097F]", source)
        )
        if (
            source_language in {"", "hindi", "hinglish"}
            or has_devanagari
        ):
            return (
                source,
                "source-narration",
                "original-mixed-narration",
            )

    return "", "", "requested-language-caption-not-provided"


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


def narration_rows(data: Any) -> tuple[list[dict], list[dict]]:
    if not isinstance(data, dict):
        raise RuntimeError("narration.json must contain a JSON object.")

    if data.get("status") != "completed":
        raise RuntimeError(
            "Narration preparation is incomplete; captions cannot proceed."
        )

    main_rows = data.get("scenes")
    extra_rows = data.get("extra_scenes", [])

    if not isinstance(main_rows, list) or not main_rows:
        raise RuntimeError(
            "narration.json must contain a non-empty scenes list."
        )

    if not isinstance(extra_rows, list):
        raise RuntimeError(
            "narration.json extra_scenes must be a list."
        )

    if not all(isinstance(row, dict) for row in main_rows):
        raise RuntimeError("Invalid Main narration record.")

    if not all(isinstance(row, dict) for row in extra_rows):
        raise RuntimeError("Invalid extra narration record.")

    return main_rows, extra_rows


def validate_part_metadata(
    data: dict,
    parts: int,
    scenes_per_part: int,
) -> None:
    for field, expected in (
        ("total_parts", parts),
        ("scenes_per_part", scenes_per_part),
    ):
        declared = data.get(field)
        if declared is None:
            continue

        try:
            declared = int(declared)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Narration {field} is invalid."
            ) from exc

        if declared != expected:
            raise RuntimeError(
                f"Narration {field} mismatch: "
                f"configured={expected}, narration={declared}."
            )


def validate_narration_records(
    main_rows: list[dict],
    extra_rows: list[dict],
    parts: int,
    scenes_per_part: int,
) -> tuple[list[dict], list[dict]]:
    expected_main = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    main_map = {}
    extra_map = {}
    all_ids = set()

    for index, row in enumerate(main_rows, 1):
        role = get_role(row)
        if role != "main":
            raise RuntimeError(
                f"Main narration record {index} has role {role!r}."
            )

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid Main part/scene in record {index}."
            ) from exc

        key = (part, scene)
        if key not in expected_main:
            raise RuntimeError(f"Unexpected Main scene mapping: {key}.")

        if key in main_map:
            raise RuntimeError(f"Duplicate Main scene mapping: {key}.")

        sid = get_scene_id(row)
        if not sid:
            raise RuntimeError(f"Main scene {key} has no scene_id.")

        if sid in all_ids:
            raise RuntimeError(f"Duplicate scene_id: {sid}")

        text = narration_text(row)
        if not text:
            raise RuntimeError(f"Main scene {sid} has empty narration.")

        if row.get("status") != "completed":
            raise RuntimeError(f"Main narration is incomplete: {sid}")

        all_ids.add(sid)
        main_map[key] = row

    missing_main = sorted(expected_main - set(main_map))
    if missing_main:
        raise RuntimeError(
            f"Missing Main narration scenes: {missing_main[:30]}"
        )

    if len(main_rows) != len(expected_main):
        raise RuntimeError(
            f"Main narration count mismatch: expected {len(expected_main)}, "
            f"found {len(main_rows)}."
        )

    for index, row in enumerate(extra_rows, 1):
        role = get_role(row)
        if role not in EXTRA_ROLES:
            raise RuntimeError(
                f"Extra narration record {index} has invalid role {role!r}."
            )

        try:
            part = int(row["part"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part in extra narration record {index}."
            ) from exc

        if not 1 <= part <= parts:
            raise RuntimeError(
                f"Extra narration part is outside configured range: {part}."
            )

        sid = get_scene_id(row)
        if not sid:
            raise RuntimeError(
                f"Part {part} {role} record has no scene_id."
            )

        if sid in all_ids:
            raise RuntimeError(f"Duplicate scene_id: {sid}")

        if row.get("status") != "completed":
            raise RuntimeError(
                f"Extra narration is incomplete: {sid}"
            )

        if not narration_text(row):
            raise RuntimeError(
                f"Extra narration has empty text: {sid}"
            )

        all_ids.add(sid)
        extra_map[sid] = row

    for part in range(1, parts + 1):
        part_roles = [
            get_role(row)
            for row in extra_rows
            if int(row["part"]) == part
        ]

        if part_roles.count("hook") != 1:
            raise RuntimeError(
                f"Part {part} must contain exactly one Hook."
            )

        if part < parts:
            if part_roles.count("suspense") != 1:
                raise RuntimeError(
                    f"Non-final part {part} must contain exactly one Suspense."
                )
            if "resolution" in part_roles:
                raise RuntimeError(
                    f"Non-final part {part} cannot contain Resolution."
                )
        else:
            if part_roles.count("resolution") != 1:
                raise RuntimeError(
                    "Final part must contain exactly one Resolution."
                )
            if "suspense" in part_roles:
                raise RuntimeError(
                    "Final part cannot contain Suspense."
                )

    expected_extra_count = parts + max(0, parts - 1) + 1
    if len(extra_rows) != expected_extra_count:
        raise RuntimeError(
            "Extra narration count mismatch: "
            f"expected {expected_extra_count}, found {len(extra_rows)}."
        )

    ordered_main = [
        main_map[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    ordered_extra = sorted(
        extra_rows,
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER[get_role(row)],
            get_scene_id(row),
        ),
    )

    return ordered_main, ordered_extra


def positive_duration(row: dict) -> float | None:
    """
    Return an existing duration only as metadata.

    This stage runs before TTS, so it never declares estimated time
    to be actual measured audio duration.
    """
    for field in (
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


def safe_checkpoint_name(row: dict) -> str:
    role = get_role(row)
    part = int(row["part"])

    if role == "main":
        scene = int(row["scene"])
        return f"part_{part:02d}_scene_{scene:02d}.json"

    sid = get_scene_id(row)
    safe_sid = re.sub(r"[^A-Za-z0-9_-]+", "_", sid).strip("_")
    if not safe_sid:
        raise RuntimeError("Unsafe extra scene_id for checkpoint.")
    return f"part_{part:02d}_{safe_sid}.json"


def checkpoint_path(row: dict) -> Path:
    return CHECKPOINT_DIR / safe_checkpoint_name(row)


def load_reusable_checkpoint(
    path: Path,
    expected_fingerprint: str,
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

    if cached.get("fingerprint") != expected_fingerprint:
        return None

    if cached.get("caption_sha256") != caption_hash:
        return None

    text = cached.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    if sha256(text) != caption_hash:
        return None

    return cached


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
    role = get_role(row)
    part = int(row["part"])
    sid = get_scene_id(row)

    record = {
        "part": part,
        "scene": int(row["scene"]) if role == "main" else None,
        "scene_id": sid,
        "role": role,
        "scene_type": role,
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
        "timing_status": "pending_audio_alignment",
    }

    duration = positive_duration(row)
    if duration is not None:
        record["narration_duration_estimate_seconds"] = duration

    return record


def make_checkpoint(
    row: dict,
    text: str,
    mode: str,
    source_field: str,
    method: str,
    font: str,
    font_size: int,
    fingerprint_value: str,
) -> dict:
    return {
        "status": "completed",
        "version": VERSION,
        "fingerprint": fingerprint_value,
        "part": int(row["part"]),
        "scene": (
            int(row["scene"])
            if get_role(row) == "main"
            else None
        ),
        "scene_id": get_scene_id(row),
        "role": get_role(row),
        "text": text,
        "caption_sha256": sha256(text),
        "source_text_sha256": sha256(narration_text(row)),
        "caption_language": mode,
        "caption_source_field": source_field,
        "selection_method": method,
        "translation_performed_here": False,
        "font": font,
        "font_size": font_size,
    }


def prepare_one(
    row: dict,
    mode: str,
    settings: dict,
) -> tuple[dict, bool]:
    sid = get_scene_id(row)
    original = normalized_text(narration_text(row))

    if not original:
        raise RuntimeError(f"Empty narration for {sid}.")

    caption, source_field, method = choose_caption(row, mode)
    if not caption:
        raise RuntimeError(
            f"CAPTIONS={mode}: no matching language caption for {sid}. "
            "No translation API is called by this file."
        )

    caption = normalized_text(caption)
    if not caption:
        raise RuntimeError(f"Empty caption text for {sid}.")

    source_hash = sha256(original)
    caption_hash = sha256(caption)

    fingerprint_data = {
        "settings": settings,
        "scene_id": sid,
        "part": int(row["part"]),
        "scene": (
            int(row["scene"])
            if get_role(row) == "main"
            else None
        ),
        "role": get_role(row),
        "source_sha256": source_hash,
        "caption_sha256": caption_hash,
        "source_field": source_field,
        "selection_method": method,
    }
    fingerprint_value = canonical_hash(fingerprint_data)

    path = checkpoint_path(row)
    cached = load_reusable_checkpoint(
        path,
        fingerprint_value,
        caption_hash,
    )

    if cached:
        selected_text = normalized_text(cached["text"])
        if sha256(selected_text) != caption_hash:
            raise RuntimeError(
                f"Cached caption integrity failure: {sid}"
            )
        reused = True
    else:
        selected_text = caption
        checkpoint = make_checkpoint(
            row=row,
            text=selected_text,
            mode=mode,
            source_field=source_field,
            method=method,
            font=settings["font"],
            font_size=settings["font_size"],
            fingerprint_value=fingerprint_value,
        )
        write_json_atomic(path, checkpoint)
        reused = False

    record = build_caption_record(
        row=row,
        text=selected_text,
        mode=mode,
        source_field=source_field,
        selection_method=method,
        font=settings["font"],
        font_size=settings["font_size"],
        reused=reused,
    )

    return record, reused


def validate_prepared_records(
    main_records: list[dict],
    extra_records: list[dict],
    parts: int,
    scenes_per_part: int,
) -> None:
    expected_main = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    actual_main = set()
    ids = set()

    for row in main_records:
        key = (int(row["part"]), int(row["scene"]))
        if key in actual_main:
            raise RuntimeError(f"Duplicate prepared Main caption: {key}.")
        actual_main.add(key)

        sid = get_scene_id(row)
        if not sid or sid in ids:
            raise RuntimeError(f"Duplicate or missing caption ID: {sid!r}")
        ids.add(sid)

        if row.get("role") != "main":
            raise RuntimeError(f"Main caption role mismatch: {sid}")

        if not row.get("text"):
            raise RuntimeError(f"Empty prepared caption: {sid}")

        if row.get("caption_sha256") != sha256(row["text"]):
            raise RuntimeError(f"Caption hash mismatch: {sid}")

    if actual_main != expected_main:
        raise RuntimeError(
            "Prepared Main caption mapping mismatch: "
            f"missing={sorted(expected_main - actual_main)}, "
            f"extra={sorted(actual_main - expected_main)}"
        )

    expected_extra_roles = []
    for part in range(1, parts + 1):
        expected_extra_roles.append((part, "hook"))
        if part < parts:
            expected_extra_roles.append((part, "suspense"))
        else:
            expected_extra_roles.append((part, "resolution"))

    actual_extra_roles = []

    for row in extra_records:
        sid = get_scene_id(row)
        if not sid or sid in ids:
            raise RuntimeError(f"Duplicate or missing extra caption ID: {sid!r}")
        ids.add(sid)

        role = get_role(row)
        if role not in EXTRA_ROLES:
            raise RuntimeError(f"Invalid extra caption role: {role}")

        actual_extra_roles.append((int(row["part"]), role))

        if row.get("scene") is not None:
            raise RuntimeError(
                f"Extra caption must not occupy Main scene number: {sid}"
            )

        if not row.get("text"):
            raise RuntimeError(f"Empty extra caption: {sid}")

        if row.get("caption_sha256") != sha256(row["text"]):
            raise RuntimeError(f"Extra caption hash mismatch: {sid}")

    if sorted(actual_extra_roles) != sorted(expected_extra_roles):
        raise RuntimeError(
            "Extra caption role mapping mismatch: "
            f"expected={sorted(expected_extra_roles)}, "
            f"found={sorted(actual_extra_roles)}"
        )


def main() -> int:
    config = load_and_validate(CONFIG_FILE)

    video_format = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if video_format not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {video_format!r}")

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    mode = normalize_language(config.get("CAPTIONS", "hindi"))
    if mode not in {"hindi", "english", "hinglish"}:
        raise RuntimeError(
            f"Invalid CAPTIONS={mode!r}; use hindi, english or hinglish."
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
    if not isinstance(narration_data, dict):
        raise RuntimeError("Narration manifest is not a JSON object.")

    validate_part_metadata(
        narration_data,
        configured_parts,
        scenes_per_part,
    )

    main_rows, extra_rows = narration_rows(narration_data)

    ordered_main, ordered_extra = validate_narration_records(
        main_rows,
        extra_rows,
        configured_parts,
        scenes_per_part,
    )

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

    prepared_main = []
    prepared_extras = []
    reused_count = 0
    created_count = 0

    all_rows = sorted(
        ordered_main + ordered_extra,
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER[get_role(row)],
            int(row["scene"]) if get_role(row) == "main" else 0,
            get_scene_id(row),
        ),
    )

    main_ids = {get_scene_id(row) for row in ordered_main}

    for index, row in enumerate(all_rows, 1):
        record, reused = prepare_one(row, mode, settings)

        if get_scene_id(row) in main_ids:
            prepared_main.append(record)
        else:
            prepared_extras.append(record)

        if reused:
            reused_count += 1
        else:
            created_count += 1

        log(
            f"{'REUSED' if reused else 'PREPARED'} "
            f"{index}/{len(all_rows)} "
            f"part={row['part']} "
            f"role={get_role(row)} "
            f"id={get_scene_id(row)} "
            f"language={mode}"
        )

    prepared_main.sort(
        key=lambda row: (
            int(row["part"]),
            int(row["scene"]),
        )
    )
    prepared_extras.sort(
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER[row["role"]],
            row["scene_id"],
        )
    )

    validate_prepared_records(
        prepared_main,
        prepared_extras,
        configured_parts,
        scenes_per_part,
    )

    narration_hash = canonical_hash(narration_data)

    role_counts = {
        role: sum(
            1
            for row in prepared_main + prepared_extras
            if row["role"] == role
        )
        for role in ROLE_ORDER
    }

    result = {
        "status": "completed",
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "source": "output/narration/narration.json",
        "source_sha256": narration_hash,
        "model": "existing-caption-text",
        "api_calls": 0,
        "translation_performed": False,
        "translation_note": (
            "This stage uses existing language-specific caption fields. "
            "It does not claim to translate narration. "
            "Actual word-level audio timing is performed by burn_captions.py."
        ),
        "configured_parts": configured_parts,
        "parts": configured_parts,
        "dynamic_short_parts": False,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(prepared_main),
        "total_extra_scenes": len(prepared_extras),
        "total_caption_records": (
            len(prepared_main) + len(prepared_extras)
        ),
        "role_counts": role_counts,
        "caption_font": font,
        "caption_font_size": font_size,
        "caption_font_size_setting": str(
            config.get("CAPTION_FONT_SIZE", "auto")
        ),
        "timing_source": (
            "Actual TTS audio alignment is performed by burn_captions.py."
        ),
        "timing_status": "pending_audio_alignment",
        "checkpoint_reused": reused_count,
        "checkpoint_prepared": created_count,

        # burn_captions.py requires this list to contain Main scenes only.
        # Extra records have scene=None and are therefore kept separately.
        "captions": prepared_main,

        # Extra-role caption metadata is retained without breaking the
        # existing Main scene (part, scene) mapping consumed by burn_captions.
        "extra_captions": prepared_extras,
    }

    write_json_atomic(CAPTION_FILE, result)

    verified = read_json(CAPTION_FILE)
    if verified.get("status") != "completed":
        raise RuntimeError("Caption manifest read-back status failed.")

    if verified.get("source_sha256") != narration_hash:
        raise RuntimeError("Caption manifest source hash mismatch.")

    if len(verified.get("captions", [])) != len(prepared_main):
        raise RuntimeError("Caption manifest Main count mismatch.")

    if len(verified.get("extra_captions", [])) != len(prepared_extras):
        raise RuntimeError("Caption manifest extra count mismatch.")

    log("=" * 56)
    log("CAPTION PREPARATION COMPLETED")
    log("Format:", video_format)
    log("Configured parts:", configured_parts)
    log("Scenes per part:", scenes_per_part)
    log("Main captions:", len(prepared_main))
    log("Extra-role captions:", len(prepared_extras))
    log("Role counts:", role_counts)
    log("Language:", mode)
    log("Font:", font)
    log("Font size:", font_size)
    log("Reused checkpoints:", reused_count)
    log("New checkpoints:", created_count)
    log("Translation API calls: 0")
    log("Timing:", "pending actual TTS audio alignment")
    log("Output:", CAPTION_FILE.relative_to(ROOT))
    log("=" * 56)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        raise SystemExit(1)