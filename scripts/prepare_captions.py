#!/usr/bin/env python3
"""Prepare verified, language-aware captions for every narration role."""

from __future__ import annotations

import hashlib
import json
import math
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
    get_scenes_range,
    get_caption_font,
    get_caption_font_size,
)

CONFIG_FILE = ROOT / "Input" / "topic.txt"
NARRATION_FILE = ROOT / "output" / "narration" / "narration.json"
AUDIO_FILE = ROOT / "output" / "narration" / "audio_jobs.json"
CAPTION_FILE = ROOT / "output" / "captions" / "captions.json"
CHECKPOINT_DIR = ROOT / "output" / "checkpoints" / "captions"

VERSION = "prepare-captions-v17-all-narration-role-captions"
MIN_AUDIO_BYTES = 1000
DURATION_TOLERANCE = 0.15

LANGUAGE_ALIASES = {
    "hi": "hindi", "hin": "hindi", "hindi": "hindi", "हिंदी": "hindi",
    "en": "english", "eng": "english", "english": "english",
    "अंग्रेज़ी": "english",
    "mixed": "hinglish", "hinglish": "hinglish",
    "hi-en": "hinglish", "en-hi": "hinglish",
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

ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}
EXTRA_ROLES = {"hook", "suspense", "resolution"}


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\ufeff", "").replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
    raw = clean_text(str(value or "")).lower()
    return LANGUAGE_ALIASES.get(raw, raw)


def first_text(row: dict, fields: tuple[str, ...]) -> tuple[str, str]:
    for field in fields:
        value = clean_text(row.get(field))
        if value:
            return value, field
    return "", ""


def narration_text(row: dict) -> str:
    return first_text(row, SOURCE_FIELDS)[0]


def scene_identity(row: dict) -> tuple[str, int, str, int | None]:
    if not isinstance(row, dict):
        raise RuntimeError("Narration record must be an object.")

    try:
        part = int(row["part"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Narration record has invalid part.") from exc

    role = clean_text(
        row.get("role") or row.get("scene_type") or row.get("scene_role") or "main"
    ).lower()

    if part < 1:
        raise RuntimeError("Narration part must be positive.")

    if role == "main":
        try:
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Main narration record has invalid scene.") from exc

        if scene < 1:
            raise RuntimeError("Main scene number must be positive.")

        expected_id = f"part_{part:02d}_scene_{scene:02d}"
        if clean_text(row.get("scene_id") or row.get("id")) != expected_id:
            raise RuntimeError(
                f"Main scene ID mismatch: expected {expected_id}."
            )
        return expected_id, part, role, scene

    if role not in EXTRA_ROLES:
        raise RuntimeError(f"Invalid narration role: {role!r}.")

    expected_id = f"part_{part:02d}_{role}"
    if clean_text(row.get("scene_id") or row.get("id")) != expected_id:
        raise RuntimeError(
            f"Extra scene ID mismatch: expected {expected_id}."
        )
    return expected_id, part, role, None


def nested_caption_text(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return first_text(
            value, ("caption", "caption_text", "text", "value")
        )[0]
    return ""


def find_nested_caption(container: Any, mode: str) -> tuple[str, str]:
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


def choose_caption(row: dict, mode: str) -> tuple[str, str, str]:
    text, field = first_text(row, LANGUAGE_FIELDS[mode])
    if text:
        return text, field, "explicit-language-field"

    for container_name in ("caption_texts", "translations", "captions"):
        text, nested_key = find_nested_caption(row.get(container_name), mode)
        if text:
            return (
                text,
                f"{container_name}.{nested_key}",
                "nested-language-caption",
            )

    generic, generic_field = first_text(
        row, ("caption_text", "display_caption")
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

    if mode == "hindi" and source and source_language in {"", "hindi"}:
        return source, "source-narration", "original-hindi-narration"

    if mode == "hinglish" and source:
        has_devanagari = bool(re.search(r"[\u0900-\u097F]", source))
        if source_language in {"", "hindi", "hinglish"} or has_devanagari:
            return source, "source-narration", "original-mixed-narration"

    return "", "", "requested-language-caption-not-provided"


def narration_rows(data: Any) -> tuple[list[dict], list[dict]]:
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Narration preparation is not completed.")

    main_rows = data.get("scenes")
    extra_rows = data.get("extra_scenes")

    if not isinstance(main_rows, list) or not main_rows:
        raise RuntimeError("Narration manifest has no Main scenes.")
    if not isinstance(extra_rows, list):
        raise RuntimeError("Narration extra_scenes must be a list.")
    if not all(isinstance(row, dict) for row in main_rows + extra_rows):
        raise RuntimeError("Narration manifest contains invalid records.")

    return main_rows, extra_rows


def validate_narration_records(
    main_rows: list[dict],
    extra_rows: list[dict],
    parts: int,
    actual_counts: list[int],
) -> tuple[list[dict], list[dict]]:
    if len(actual_counts) != parts:
        raise RuntimeError("Narration actual_scene_counts does not match PARTS.")
    expected_main = {
        (part, scene)
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    }
    main_map: dict[tuple[int, int], dict] = {}
    extra_map: dict[str, dict] = {}
    all_ids: set[str] = set()

    for row in main_rows:
        sid, part, role, scene = scene_identity(row)
        if role != "main":
            raise RuntimeError(f"{sid}: Main list contains a non-Main role.")
        key = (part, int(scene))
        if key not in expected_main or key in main_map:
            raise RuntimeError(f"Unexpected or duplicate Main mapping: {key}.")
        if sid in all_ids:
            raise RuntimeError(f"Duplicate narration scene ID: {sid}.")
        if row.get("status") != "completed" or not narration_text(row):
            raise RuntimeError(f"Main narration is incomplete: {sid}.")
        main_map[key] = row
        all_ids.add(sid)

    if set(main_map) != expected_main:
        raise RuntimeError(
            "Main narration mapping mismatch: "
            f"missing={sorted(expected_main-set(main_map))}, "
            f"extra={sorted(set(main_map)-expected_main)}."
        )

    expected_extra_roles = [
        (part, role)
        for part in range(1, parts + 1)
        for role in (("hook", "suspense") if part < parts else ("hook", "resolution"))
    ]
    for row in extra_rows:
        sid, part, role, scene = scene_identity(row)
        if role not in EXTRA_ROLES or (part, role) not in expected_extra_roles or scene not in (None, 0):
            raise RuntimeError(f"{sid}: invalid extra-role caption mapping.")
        if sid in all_ids or row.get("status") != "completed" or not narration_text(row):
            raise RuntimeError(f"{sid}: extra-role narration is incomplete or duplicated.")
        if row.get("audio_duration_source") != "ffprobe_measured":
            raise RuntimeError(f"{sid}: extra-role measured TTS audio is missing.")
        extra_map[(part, role)] = row
        all_ids.add(sid)
    if set(extra_map) != set(expected_extra_roles):
        raise RuntimeError("Hook/Suspense/Resolution narration records are incomplete.")

    ordered_main = [
        main_map[(part, scene)]
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    ]
    ordered_extras = [extra_map[key] for key in expected_extra_roles]
    return ordered_main, ordered_extras


def validate_audio_manifest(
    narration_data: dict,
    expected_rows: list[dict],
) -> tuple[dict[str, dict], str]:
    if not AUDIO_FILE.is_file():
        raise RuntimeError(f"TTS audio manifest is missing: {AUDIO_FILE}")

    audio_data = read_json(AUDIO_FILE)
    if not isinstance(audio_data, dict) or audio_data.get("status") != "completed":
        raise RuntimeError("TTS audio manifest is not completed.")

    jobs = audio_data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("TTS audio manifest contains no jobs.")

    expected = {}
    for row in expected_rows:
        sid, part, role, scene = scene_identity(row)
        expected[sid] = (part, role, scene, narration_text(row))

    audio_map: dict[str, dict] = {}
    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Invalid TTS audio job {index}.")

        sid = clean_text(job.get("scene_id"))
        if not sid or sid in audio_map:
            raise RuntimeError(f"Missing or duplicate TTS scene ID: {sid!r}.")

        if sid not in expected:
            raise RuntimeError(f"Unexpected TTS audio job: {sid}.")

        part, role, scene, source_text = expected[sid]
        try:
            job_part = int(job["part"])
            job_role = clean_text(job.get("role")).lower()
            job_scene = (
                int(job["scene"])
                if job.get("scene") is not None
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"{sid}: invalid TTS part/role/scene.") from exc

        if (job_part, job_role, job_scene) != (part, role, scene):
            raise RuntimeError(f"{sid}: TTS role mapping does not match narration.")

        if job.get("status") != "completed":
            raise RuntimeError(f"{sid}: TTS audio is not completed.")

        output = clean_text(
            job.get("output") or job.get("output_path")
            or job.get("audio_path") or job.get("audio")
        )
        if not output:
            raise RuntimeError(f"{sid}: TTS output path is missing.")

        audio_path = Path(output)
        if not audio_path.is_absolute():
            audio_path = ROOT / audio_path
        audio_path = audio_path.resolve()

        if not audio_path.is_relative_to(ROOT.resolve()):
            raise RuntimeError(f"{sid}: TTS audio path escapes project root.")
        if not audio_path.is_file() or audio_path.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError(f"{sid}: TTS audio file is missing or too small.")

        declared_hash = clean_text(
            job.get("sha256") or job.get("audio_sha256") or job.get("audio_hash")
        )
        actual_hash = sha256_file(audio_path)
        if not declared_hash or declared_hash != actual_hash:
            raise RuntimeError(f"{sid}: TTS audio SHA-256 mismatch.")

        raw_duration = (
            job.get("duration") or job.get("actual_audio_duration_seconds")
            or job.get("audio_duration")
        )
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{sid}: TTS measured duration is invalid.") from exc

        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(f"{sid}: TTS measured duration is invalid.")
        if job.get("duration_source") != "ffprobe_measured":
            raise RuntimeError(f"{sid}: TTS duration is not ffprobe-measured.")

        fingerprint = clean_text(
            job.get("fingerprint") or job.get("tts_fingerprint")
        )
        if not fingerprint:
            raise RuntimeError(f"{sid}: TTS fingerprint is missing.")

        text_hash = sha256(source_text)
        declared_text_hash = clean_text(
            job.get("text_sha256") or job.get("source_text_sha256")
        )
        job_text = clean_text(job.get("text"))
        if job_text != source_text:
            raise RuntimeError(f"{sid}: TTS text differs from locked narration.")
        if declared_text_hash and declared_text_hash != text_hash:
            raise RuntimeError(f"{sid}: TTS locked source-text hash mismatch.")
        if not declared_text_hash:
            chunk_records = job.get("chunks")
            if not isinstance(chunk_records, list) or not chunk_records:
                raise RuntimeError(f"{sid}: TTS source-text integrity record is missing.")
            chunk_hashes = [
                clean_text(item.get("text_sha256"))
                for item in chunk_records
                if isinstance(item, dict)
            ]
            if len(chunk_hashes) != len(chunk_records) or any(
                not re.fullmatch(r"[a-f0-9]{64}", value)
                for value in chunk_hashes
            ):
                raise RuntimeError(f"{sid}: TTS chunk source-text hashes are invalid.")

        audio_map[sid] = {
            "path": audio_path.relative_to(ROOT).as_posix(),
            "sha256": actual_hash,
            "text_sha256": text_hash,
            "duration": round(duration, 5),
            "provider": job.get("provider"),
            "model": job.get("model"),
            "fingerprint": fingerprint,
            "size_bytes": audio_path.stat().st_size,
        }

    if set(audio_map) != set(expected):
        raise RuntimeError(
            "TTS job mapping mismatch: "
            f"missing={sorted(set(expected)-set(audio_map))}, "
            f"unexpected={sorted(set(audio_map)-set(expected))}."
        )

    narration_audio_status = narration_data.get("tts_status")
    if narration_audio_status != "completed":
        raise RuntimeError("Narration manifest TTS status is not completed.")

    for row in expected_rows:
        sid, _, _, _ = scene_identity(row)
        audio = audio_map[sid]
        if row.get("audio_duration_source") != "ffprobe_measured":
            raise RuntimeError(f"{sid}: narration audio timing is not measured.")
        if row.get("audio_sha256") != audio["sha256"]:
            raise RuntimeError(f"{sid}: narration/TTS audio hashes disagree.")
        try:
            narration_duration = float(row.get("actual_audio_duration_seconds"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{sid}: narration measured duration is missing.") from exc
        if abs(narration_duration - audio["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError(f"{sid}: narration/TTS durations disagree.")

    return audio_map, canonical_hash(audio_data)


def resolve_font_size(config: dict, video_format: str) -> int:
    configured = get_caption_font_size(config)
    if str(configured).strip().lower() == "auto":
        return 24 if video_format == "short" else 28
    try:
        size = int(configured)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CAPTION_FONT_SIZE must be auto or 16–64.") from exc
    if not 16 <= size <= 64:
        raise RuntimeError("CAPTION_FONT_SIZE must be between 16 and 64.")
    return size


def safe_checkpoint_name(row: dict) -> str:
    sid, part, role, scene = scene_identity(row)
    if role == "main":
        return f"part_{part:02d}_scene_{scene:02d}.json"
    return f"part_{part:02d}_{role}.json"


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


def positive_estimate(row: dict) -> float | None:
    for field in ("narration_duration_estimate_seconds", "estimated_duration"):
        value = row.get(field)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and 0.1 <= number <= 3600:
            return round(number, 3)
    return None


def prepare_one(
    row: dict,
    mode: str,
    settings: dict,
    audio: dict,
) -> tuple[dict, bool]:
    sid, part, role, scene = scene_identity(row)
    source = clean_text(narration_text(row))
    if not source:
        raise RuntimeError(f"{sid}: source narration is empty.")
    source_hash = sha256(source)
    if audio.get("text_sha256") != source_hash:
        raise RuntimeError(f"{sid}: caption source text differs from locked TTS text.")

    caption, source_field, method = choose_caption(row, mode)
    if not caption:
        raise RuntimeError(
            f"CAPTIONS={mode}: no matching language caption for {sid}. "
            "No translation is performed by this stage."
        )

    caption = clean_text(caption)
    if not caption:
        raise RuntimeError(f"{sid}: caption text is empty.")

    fingerprint_data = {
        "version": VERSION,
        "settings": settings,
        "scene_id": sid,
        "part": part,
        "scene": scene,
        "role": role,
        "source_sha256": source_hash,
        "caption_sha256": sha256(caption),
        "source_field": source_field,
        "selection_method": method,
        "audio_sha256": audio["sha256"],
        "audio_fingerprint": audio["fingerprint"],
        "audio_duration": audio["duration"],
    }
    fingerprint_value = canonical_hash(fingerprint_data)
    caption_hash = sha256(caption)

    cached = load_reusable_checkpoint(
        checkpoint_path(row), fingerprint_value, caption_hash
    )

    reused = cached is not None
    selected_text = clean_text(cached["text"]) if reused else caption
    if sha256(selected_text) != caption_hash:
        raise RuntimeError(f"{sid}: cached caption integrity failure.")

    if not reused:
        checkpoint = {
            "status": "completed",
            "version": VERSION,
            "fingerprint": fingerprint_value,
            "part": part,
            "scene": scene,
            "scene_id": sid,
            "role": role,
            "text": selected_text,
            "caption_sha256": caption_hash,
            "source_text_sha256": source_hash,
            "audio_sha256": audio["sha256"],
            "audio_fingerprint": audio["fingerprint"],
            "caption_language": mode,
            "caption_source_field": source_field,
            "selection_method": method,
            "translation_performed_here": False,
            "font": settings["font"],
            "font_size": settings["font_size"],
        }
        write_json_atomic(checkpoint_path(row), checkpoint)

    record = {
        "part": part,
        "scene": scene,
        "scene_id": sid,
        "role": role,
        "scene_type": role,
        "text": selected_text,
        "caption": selected_text,
        "caption_language": mode,
        "caption_source_field": source_field,
        "selection_method": method,
        "source_text_sha256": source_hash,
        "caption_sha256": caption_hash,
        "audio_path": audio["path"],
        "audio_sha256": audio["sha256"],
        "audio_duration_seconds": audio["duration"],
        "audio_fingerprint": audio["fingerprint"],
        "audio_provider": audio["provider"],
        "audio_model": audio["model"],
        "font": settings["font"],
        "font_size": settings["font_size"],
        "status": "completed",
        "checkpoint_reused": reused,
        "timing_status": "audio_verified_word_alignment_pending",
    }

    estimate = positive_estimate(row)
    if estimate is not None:
        record["narration_duration_estimate_seconds"] = estimate

    return record, reused

def validate_prepared_records(
    main_records: list[dict],
    extra_records: list[dict],
    audio_map: dict[str, dict],
    parts: int,
    actual_counts: list[int],
) -> None:
    if len(actual_counts) != parts:
        raise RuntimeError("Prepared caption actual Scene counts do not match PARTS.")
    expected_main = {
        (part, scene)
        for part, count in enumerate(actual_counts, 1)
        for scene in range(1, count + 1)
    }
    actual_main = set()
    all_ids: set[str] = set()

    for row in main_records:
        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Prepared Main caption has invalid mapping.") from exc

        key = (part, scene)
        sid = clean_text(row.get("scene_id"))

        if key in actual_main:
            raise RuntimeError(f"Duplicate Main caption mapping: {key}.")
        if not sid or sid in all_ids:
            raise RuntimeError(f"Missing or duplicate caption ID: {sid!r}.")
        if row.get("role") != "main":
            raise RuntimeError(f"{sid}: Main caption role mismatch.")
        if not clean_text(row.get("text")):
            raise RuntimeError(f"{sid}: caption text is empty.")
        if row.get("caption_sha256") != sha256(row["text"]):
            raise RuntimeError(f"{sid}: caption SHA-256 mismatch.")
        if sid not in audio_map:
            raise RuntimeError(f"{sid}: verified TTS audio mapping is missing.")
        if row.get("audio_sha256") != audio_map[sid]["sha256"]:
            raise RuntimeError(f"{sid}: caption/audio hash mapping mismatch.")

        actual_main.add(key)
        all_ids.add(sid)

    if actual_main != expected_main:
        raise RuntimeError(
            "Prepared Main caption mapping mismatch: "
            f"missing={sorted(expected_main-actual_main)}, "
            f"extra={sorted(actual_main-expected_main)}."
        )

    for row in extra_records:
        sid = clean_text(row.get("scene_id"))
        if not sid or sid in all_ids or row.get("role") not in EXTRA_ROLES:
            raise RuntimeError(f"{sid}: invalid or duplicate extra caption.")
        if not clean_text(row.get("text")) or row.get("caption_sha256") != sha256(row["text"]):
            raise RuntimeError(f"{sid}: extra caption text/hash is invalid.")
        if sid not in audio_map or row.get("audio_sha256") != audio_map[sid]["sha256"]:
            raise RuntimeError(f"{sid}: extra caption/TTS audio mapping mismatch.")
        all_ids.add(sid)

    if all_ids != set(audio_map):
        raise RuntimeError(
            "Caption/TTS record set mismatch: "
            f"missing captions={sorted(set(audio_map)-all_ids)}, "
            f"unexpected captions={sorted(all_ids-set(audio_map))}."
        )


def main() -> int:
    config = load_and_validate(CONFIG_FILE)

    video_format = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes_min, scenes_max = get_scenes_range(config)

    if video_format not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {video_format!r}.")
    if parts < 1 or scenes_min < 1 or scenes_max < scenes_min:
        raise RuntimeError("PARTS or SCENES min-max range is invalid.")

    mode = normalize_language(config.get("CAPTIONS", "hindi"))
    if mode not in {"hindi", "english", "hinglish"}:
        raise RuntimeError(
            f"Invalid CAPTIONS={mode!r}; use hindi, english or hinglish."
        )

    font = clean_text(str(get_caption_font(config)))
    if not font:
        raise RuntimeError("CAPTION_FONT resolved to an empty value.")

    font_size = resolve_font_size(config, video_format)

    if not NARRATION_FILE.is_file():
        raise RuntimeError(f"Narration manifest is missing: {NARRATION_FILE}.")

    narration_data = read_json(NARRATION_FILE)
    if not isinstance(narration_data, dict):
        raise RuntimeError("Narration manifest must be a JSON object.")

    declared_parts = narration_data.get("total_parts")
    if declared_parts is not None and int(declared_parts) != parts:
        raise RuntimeError(
            f"Narration total_parts mismatch: configured={parts}, narration={declared_parts}."
        )
    try:
        source_min = int(narration_data["scenes_min"])
        source_max = int(narration_data["scenes_max"])
        raw_counts = narration_data["actual_scene_counts"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Narration SCENES range/actual_scene_counts metadata is missing.") from exc
    if (source_min, source_max) != (scenes_min, scenes_max):
        raise RuntimeError(
            f"Narration SCENES range {source_min}-{source_max} differs from input "
            f"{scenes_min}-{scenes_max}."
        )
    if not isinstance(raw_counts, list) or len(raw_counts) != parts:
        raise RuntimeError("Narration actual_scene_counts does not match selected PARTS.")
    actual_counts = []
    for part_number, raw_count in enumerate(raw_counts, 1):
        if isinstance(raw_count, bool):
            raise RuntimeError(f"Part {part_number} has invalid actual Scene count.")
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Part {part_number} has invalid actual Scene count.") from exc
        if not scenes_min <= count <= scenes_max:
            raise RuntimeError(
                f"Part {part_number} has {count} Scenes; allowed range is {scenes_min}-{scenes_max}."
            )
        actual_counts.append(count)

    main_rows, extra_rows = narration_rows(narration_data)
    ordered_main, ordered_extras = validate_narration_records(
        main_rows,
        extra_rows,
        parts,
        actual_counts,
    )

    all_rows = ordered_main + ordered_extras
    audio_map, audio_manifest_hash = validate_audio_manifest(
        narration_data,
        all_rows,
    )

    settings = {
        "version": VERSION,
        "format": video_format,
        "mode": mode,
        "parts": parts,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": actual_counts,
        "font": font,
        "font_size": font_size,
    }

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    CAPTION_FILE.parent.mkdir(parents=True, exist_ok=True)

    prepared_main = []
    prepared_extras = []
    reused_count = 0
    created_count = 0

    ordered_rows = sorted(
        all_rows,
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER[scene_identity(row)[2]],
            int(scene_identity(row)[3] or 0),
        ),
    )

    main_ids = {scene_identity(row)[0] for row in ordered_main}

    for index, row in enumerate(ordered_rows, 1):
        sid, _, _, _ = scene_identity(row)
        record, reused = prepare_one(
            row,
            mode,
            settings,
            audio_map[sid],
        )

        if sid in main_ids:
            prepared_main.append(record)
        else:
            prepared_extras.append(record)

        if reused:
            reused_count += 1
        else:
            created_count += 1

        log(
            f"{'REUSED' if reused else 'PREPARED'} "
            f"{index}/{len(ordered_rows)} "
            f"part={row['part']} "
            f"role={scene_identity(row)[2]} "
            f"id={sid} language={mode}"
        )

    prepared_main.sort(
        key=lambda row: (int(row["part"]), int(row["scene"]))
    )
    prepared_extras.sort(
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER[row["role"]],
        )
    )

    validate_prepared_records(
        prepared_main,
        prepared_extras,
        audio_map,
        parts,
        actual_counts,
    )

    narration_hash = canonical_hash(narration_data)
    role_counts = {
        role: sum(
            1 for row in prepared_main + prepared_extras
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
        "audio_source": "output/narration/audio_jobs.json",
        "audio_source_sha256": audio_manifest_hash,
        "model": "existing-caption-text",
        "api_calls": 0,
        "translation_performed": False,
        "translation_note": (
            "Uses existing language-specific caption fields or the original "
            "narration when its language matches. No translation is claimed."
        ),
        "configured_parts": parts,
        "parts": parts,
        "dynamic_short_parts": video_format == "short",
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": actual_counts,
        "total_scenes": len(prepared_main) + len(prepared_extras),
        "total_extra_scenes": len(prepared_extras),
        "visual_only_extra_scenes": 0,
        "total_caption_records": len(prepared_main) + len(prepared_extras),
        "role_counts": role_counts,
        "caption_font": font,
        "caption_font_size": font_size,
        "caption_font_size_setting": str(
            config.get("CAPTION_FONT_SIZE", "auto")
        ),
        "timing_source": (
            "Verified ffprobe audio durations; word-level alignment is "
            "performed by burn_captions.py."
        ),
        "timing_status": "audio_verified_word_alignment_pending",
        "checkpoint_reused": reused_count,
        "checkpoint_prepared": created_count,
        "captions": prepared_main,
        "extra_captions": prepared_extras,
    }

    write_json_atomic(CAPTION_FILE, result)

    verified = read_json(CAPTION_FILE)
    if not isinstance(verified, dict) or verified.get("status") != "completed":
        raise RuntimeError("Caption manifest read-back status failed.")
    if verified.get("source_sha256") != narration_hash:
        raise RuntimeError("Caption manifest source hash mismatch.")
    if verified.get("audio_source_sha256") != audio_manifest_hash:
        raise RuntimeError("Caption manifest audio source hash mismatch.")
    if len(verified.get("captions", [])) != len(prepared_main):
        raise RuntimeError("Caption manifest Main count mismatch.")
    if len(verified.get("extra_captions", [])) != len(prepared_extras):
        raise RuntimeError("Caption manifest extra-role count mismatch.")

    log("=" * 56)
    log("CAPTION PREPARATION COMPLETED")
    log("Format:", video_format)
    log("Parts:", parts)
    log("SCENES range:", f"{scenes_min}-{scenes_max}")
    log("Actual Scene counts:", actual_counts)
    log("Main captions:", len(prepared_main))
    log("Extra-role captions:", len(prepared_extras))
    log("Role counts:", role_counts)
    log("Language:", mode)
    log("Font:", font)
    log("Font size:", font_size)
    log("Verified Main TTS audio records:", len(audio_map))
    log("Reused checkpoints:", reused_count)
    log("New checkpoints:", created_count)
    log("Translation API calls: 0")
    log("Word timing: pending burn_captions.py alignment")
    log("Output:", CAPTION_FILE.relative_to(ROOT))
    log("=" * 56)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("CAPTION PREPARATION FAILED:", str(exc))
        raise SystemExit(1)