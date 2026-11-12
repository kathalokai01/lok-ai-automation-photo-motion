#!/usr/bin/env python3
"""Validate, align, burn and assemble captions for every video role."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_fps,
    get_caption_font,
    get_caption_font_size,
)

OUT = ROOT / "output"
RENDER_DIR = OUT / "scene_renders"
CAPTIONED_DIR = RENDER_DIR / "captioned"
CAPTION_DIR = OUT / "captions"
PART_DIR = OUT / "parts"

RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
CAPTION_MANIFEST = CAPTION_DIR / "captions.json"
PROGRESS_FILE = CAPTION_DIR / "burn_progress.json"
ALIGNMENT_CACHE = CAPTION_DIR / "word_alignment_cache.json"
FINAL_SRT = CAPTION_DIR / "final_captions.srt"

VERSION = "burn-captions-v64-json-path-safe-manifest"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 1000
FPS_TOLERANCE = 0.03
DURATION_TOLERANCE = 1.25
ALIGNMENT_MIN_MATCH = 0.48
AUDIO_RATE = 48000

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

PALETTES = [
    {"name": "cinematic-white", "light": "&H00FFFFFF", "dark": "&H00141414"},
    {"name": "warm-gold", "light": "&H0000D7FF", "dark": "&H00202020"},
    {"name": "soft-cream", "light": "&H00E8F1FF", "dark": "&H00202020"},
    {"name": "mint-accent", "light": "&H0098FFD8", "dark": "&H00141414"},
    {"name": "sky-blue", "light": "&H00FFD28A", "dark": "&H00141414"},
    {"name": "rose-accent", "light": "&H00B8B5FF", "dark": "&H00141414"},
]


def log(*args):
    print(*args, flush=True)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def fingerprint(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, required=True, default=None):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        if required:
            raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc
        return default


def json_path_default(value):
    """Serialize filesystem paths as stable strings; reject other unknown types."""
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")

    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(
                data,
                stream,
                ensure_ascii=False,
                indent=2,
                default=json_path_default,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def run(command, label, timeout=900):
    result = subprocess.run(
        [str(x) for x in command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"{label} failed: "
            f"{(result.stderr or result.stdout or '')[-4000:]}"
        )

    return result


def rows_from(data, *keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]

    return []


def first_value(row, keys):
    if not isinstance(row, dict):
        return None

    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path missing")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path outside project")

    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: missing or too-small file: {path}")

    return path


def relative(path):
    try:
        return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Path outside project: {path}") from exc


def media_info(path):
    result = run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of", "json",
            str(path),
        ],
        f"Inspect {Path(path).name}",
        timeout=90,
    )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except Exception as exc:
        raise RuntimeError(f"Cannot inspect media: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    streams = data.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    fps_value = (
        (video or {}).get("avg_frame_rate")
        or (video or {}).get("r_frame_rate")
    )

    try:
        fps = float(Fraction(str(fps_value))) if fps_value else 0.0
    except (ValueError, ZeroDivisionError, TypeError):
        fps = 0.0

    return {
        "duration": duration,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "fps": fps,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def require_fps(info, expected, label):
    actual = float(info.get("fps", 0))

    if actual <= 0 or abs(actual - expected) > FPS_TOLERANCE:
        raise RuntimeError(
            f"FPS mismatch {label}: expected {expected}, got {actual:.4f}"
        )


def normalize_role(row):
    role = str(
        row.get("role")
        or row.get("scene_type")
        or row.get("type")
        or "main"
    ).strip().lower()

    aliases = {
        "opening": "hook",
        "intro": "hook",
        "part_hook": "hook",
        "story": "main",
        "main_scene": "main",
        "cliffhanger": "suspense",
        "ending": "resolution",
        "final": "resolution",
    }

    role = aliases.get(role, role)

    if role not in ROLE_ORDER:
        raise RuntimeError(f"Unknown scene role: {role}")

    return role


def get_scene_id(row, label):
    value = str(row.get("scene_id") or row.get("id") or "").strip()

    if not value:
        raise RuntimeError(f"{label}: scene_id missing")

    return value


def expected_roles(part_numbers):
    result = {}

    for index, part in enumerate(part_numbers):
        result[part] = ["hook", "main"]

        if index < len(part_numbers) - 1:
            result[part].append("suspense")
        else:
            result[part].append("resolution")

    return result


def validate_part_numbers(values):
    if not isinstance(values, list) or not values:
        raise RuntimeError("Render manifest has no part_numbers")

    parts = []

    for value in values:
        try:
            part = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid part number") from exc

        if part < 1:
            raise RuntimeError("Part number must be positive")

        parts.append(part)

    if len(parts) != len(set(parts)) or parts != sorted(parts):
        raise RuntimeError("Part numbers must be unique and ascending")

    return parts


def parse_part_range(config, manifest):
    actual = manifest.get("part_numbers")

    if isinstance(actual, list) and actual:
        return validate_part_numbers(actual)

    first = manifest.get("first_part", manifest.get("start_part", 1))

    try:
        first = int(first)
        count = int(
            manifest.get("total_parts")
            or manifest.get("parts_count")
            or 0
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid render part metadata") from exc

    if first < 1 or count < 1:
        raise RuntimeError("Render manifest has invalid part range")

    return list(range(first, first + count))


def split_phrases(text, max_chars=38, max_words=7):
    text = clean_text(text)

    if not text:
        return []

    # Sentence boundaries are preserved before phrase length splitting.
    sentences = re.split(r"(?<=[.!?।…！？])\s+", text)
    phrases = []

    for sentence in sentences:
        current = []

        for word in sentence.split():
            candidate = " ".join(current + [word])

            if current and (
                len(candidate) > max_chars
                or len(current) >= max_words
            ):
                phrases.append(" ".join(current))
                current = [word]
            else:
                current.append(word)

        if current:
            phrases.append(" ".join(current))

    merged = []

    for phrase in phrases:
        if merged and len(merged[-1].split()) <= 2:
            candidate = merged[-1] + " " + phrase

            if len(candidate) <= max_chars + 8:
                merged[-1] = candidate
                continue

        merged.append(phrase)

    return merged or [text]


def load_caption_map(part_numbers, scene_counts):
    data = read_json(CAPTION_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption preparation manifest is incomplete")

    mapping = {}
    extras = {}

    caption_rows = rows_from(data, "captions", "scenes")
    extra_caption_rows = rows_from(data, "extra_captions")
    for row in caption_rows + extra_caption_rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption record")

        role = normalize_role(row)

        try:
            part = int(row["part"])
            scene = int(row.get("scene") or row.get("local_scene") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption part/scene") from exc

        text = clean_text(
            row.get("text")
            or row.get("caption")
            or row.get("caption_text")
        )

        if not text:
            raise RuntimeError(f"Empty caption for {(part, scene)}")

        key = (part, scene) if role == "main" else str(row.get("scene_id") or "").strip()

        target = mapping if role == "main" else extras
        if key in target:
            raise RuntimeError(f"Duplicate caption mapping {key}")

        declared_hash = row.get("caption_sha256")
        actual_hash = sha256_text(text)

        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Caption checksum mismatch: {key}")

        source_text_hash = clean_text(row.get("source_text_sha256"))
        if len(source_text_hash) != 64 or any(ch not in "0123456789abcdef" for ch in source_text_hash):
            raise RuntimeError(f"Caption source text hash is invalid: {key}")

        target[key] = {
            "text": text,
            "language": clean_text(
                row.get("caption_language") or data.get("mode") or "hindi"
            ),
            "sha256": actual_hash,
            "source_text_sha256": source_text_hash,
            "scene_id": str(row.get("scene_id") or "").strip(),
        }

    expected = {
        (part, scene)
        for part in part_numbers
        for scene in range(1, scene_counts[part] + 1)
    }

    if set(mapping) != expected:
        raise RuntimeError(
            "Main caption mapping mismatch: "
            f"missing={sorted(expected-set(mapping))}; "
            f"extra={sorted(set(mapping)-expected)}"
        )

    expected_extra_ids = {
        f"part_{part:02d}_{role}"
        for part in part_numbers
        for role in (("hook", "suspense") if part < part_numbers[-1] else ("hook", "resolution"))
    }
    if set(extras) != expected_extra_ids:
        raise RuntimeError("Hook/Suspense/Resolution caption mapping is incomplete.")

    return mapping, extras, data


def load_audio_map():
    data = read_json(AUDIO_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("TTS audio manifest is incomplete")

    rows = rows_from(data, "jobs", "audio_jobs", "scenes")

    if not rows:
        raise RuntimeError("TTS manifest contains no audio jobs")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid TTS job {index}")

        sid = get_scene_id(row, f"TTS job {index}")

        if sid in mapping:
            raise RuntimeError(f"Duplicate TTS scene_id: {sid}")

        if str(row.get("status", "")).lower() not in {
            "completed", "reused"
        }:
            raise RuntimeError(f"TTS scene is incomplete: {sid}")

        path = safe_file(
            first_value(
                row,
                ("output", "audio_path", "audio_file", "path", "file", "audio"),
            ),
            f"Narration {sid}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Audio stream missing: {sid}")

        actual_hash = sha256_file(path)
        declared_hash = row.get("sha256") or row.get("audio_sha256")

        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Narration checksum mismatch: {sid}")

        try:
            part = int(row["part"])
            scene = int(row.get("scene") or row.get("local_scene") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid TTS part/scene: {sid}") from exc

        role = normalize_role(row)
        if role == "main" and scene < 1:
            raise RuntimeError(f"Invalid Main audio scene number: {sid}")
        if role != "main" and scene != 0:
            raise RuntimeError(f"Extra role has Main scene number: {sid}")

        chunks = []

        for chunk in (
            row.get("chunks", [])
            if isinstance(row.get("chunks"), list)
            else []
        ):
            if not isinstance(chunk, dict):
                continue

            try:
                seconds = float(
                    chunk.get("duration")
                    or chunk.get("duration_seconds")
                    or 0
                )
            except (TypeError, ValueError):
                continue

            text = clean_text(
                chunk.get("text") or chunk.get("narration") or ""
            )

            if math.isfinite(seconds) and seconds > 0 and text:
                chunks.append({
                    "duration": seconds,
                    "text": text,
                })

        text = clean_text(
            row.get("text") or row.get("narration_text") or row.get("narration")
        )
        text_hash = sha256_text(text)
        if not text or row.get("text_sha256") != text_hash:
            raise RuntimeError(f"{sid}: locked TTS text hash mismatch.")

        mapping[sid] = {
            "scene_id": sid,
            "part": part,
            "scene": scene,
            "role": role,
            "path": path,
            "duration": info["duration"],
            "sha256": actual_hash,
            "text_sha256": text_hash,
            "chunks": chunks,
            "text": text,
            "language": clean_text(
                row.get("caption_language")
                or row.get("language")
                or "hindi"
            ),
            "provider": row.get("provider"),
            "model": row.get("model"),
        }

    return mapping, data


def load_render_scenes(data, part_numbers, scene_counts, width, height, fps):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest is incomplete")

    if str(data.get("format", "")).lower() not in {"short", "full"}:
        raise RuntimeError("Render manifest FORMAT is invalid")

    if int(data.get("width", width)) != width:
        raise RuntimeError("Render manifest width mismatch")

    if int(data.get("height", height)) != height:
        raise RuntimeError("Render manifest height mismatch")

    if abs(float(data.get("fps", fps)) - fps) > FPS_TOLERANCE:
        raise RuntimeError("Render manifest FPS mismatch")

    rows = (
        data.get("all_completed_scenes")
        or data.get("completed_scenes")
        or data.get("scenes")
        or []
    )

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Render manifest has no scene records")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Invalid/incomplete render record {index}")

        sid = get_scene_id(row, f"Render record {index}")

        if sid in mapping:
            raise RuntimeError(f"Duplicate render scene_id: {sid}")

        try:
            part = int(row["part"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"{sid}: invalid part number") from exc

        role = normalize_role(row)

        if part not in part_numbers:
            raise RuntimeError(f"{sid}: part outside selected range")

        try:
            local_scene = int(
                row.get("local_scene")
                or (row.get("scene") if role == "main" else 0)
                or row.get("source_scene_number")
                or 0
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{sid}: invalid scene number") from exc

        if role == "main" and not 1 <= local_scene <= scene_counts[part]:
            raise RuntimeError(f"{sid}: invalid Main scene number")

        if role != "main" and local_scene != 0:
            raise RuntimeError(f"{sid}: extra role has Main scene number")

        expected_id = (
            f"part_{part:02d}_scene_{local_scene:02d}"
            if role == "main"
            else f"part_{part:02d}_{role}"
        )

        if sid != expected_id:
            raise RuntimeError(
                f"{sid}: expected scene ID {expected_id}"
            )

        path = safe_file(
            first_value(row, ("video", "output", "path")),
            f"Rendered scene {sid}",
            MIN_VIDEO_BYTES,
        )

        checksum = sha256_file(path)
        declared = row.get("sha256") or row.get("video_sha256")

        if declared and declared != checksum:
            raise RuntimeError(f"Rendered video hash mismatch: {sid}")

        info = media_info(path)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Rendered clip lacks video/audio: {sid}")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Rendered resolution mismatch: {sid}")

        require_fps(info, fps, sid)

        mapping[sid] = {
            **row,
            "scene_id": sid,
            "part": part,
            "role": role,
            "local_scene": local_scene,
            "source_path": path,
            "source_sha256": checksum,
            "source_duration": info["duration"],
            "global_order": int(
                row.get("photo_motion_global_scene")
                or row.get("global_scene")
                or index
            ),
        }

    expected_main = {
        (part, scene)
        for part in part_numbers
        for scene in range(1, scene_counts[part] + 1)
    }

    actual_main = {
        (row["part"], row["local_scene"])
        for row in mapping.values()
        if row["role"] == "main"
    }

    if actual_main != expected_main:
        raise RuntimeError(
            "Rendered Main grid mismatch: "
            f"missing={sorted(expected_main-actual_main)}; "
            f"extra={sorted(actual_main-expected_main)}"
        )

    role_plan = expected_roles(part_numbers)

    for part, expected in role_plan.items():
        roles = [
            row["role"]
            for row in mapping.values()
            if row["part"] == part
        ]

        for role in expected:
            if roles.count(role) != (scene_counts[part] if role == "main" else 1):
                raise RuntimeError(
                    f"Part {part} requires correct {role} count"
                )

        for role in set(roles) - set(expected):
            raise RuntimeError(f"Part {part} has invalid role {role}")

    ordered = sorted(
        mapping.values(),
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
            row["local_scene"] if row["role"] == "main" else 0,
            row["global_order"],
        ),
    )

    return mapping, ordered

def resolve_font(family):
    result = run(
        ["fc-match", "-f", "%{family}|%{file}\\n", family],
        f"Resolve font {family}",
        60,
    )
    lines = result.stdout.strip().splitlines()
    if not lines or "|" not in lines[0]:
        raise RuntimeError(f"Caption font not found: {family}")
    resolved_family, raw_path = lines[0].split("|", 1)
    resolved_family = clean_text(resolved_family.split(",", 1)[0])
    path = Path(raw_path).resolve()
    if not resolved_family or not path.is_file():
        raise RuntimeError(f"Caption font fallback could not be resolved: {family}")
    return resolved_family, path


def normalize_word(value):
    return re.sub(
        r"[^\w]",
        "",
        str(value or "").lower(),
        flags=re.UNICODE,
    )


def words_from(text):
    return [
        word
        for word in re.findall(r"\S+", clean_text(text))
        if normalize_word(word)
    ]


def load_aligner():
    try:
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        model = os.environ.get(
            "CAPTION_ALIGNMENT_MODEL",
            "openai/whisper-tiny",
        ).strip()

        log("Loading speech alignment model:", model)

        return pipeline(
            "automatic-speech-recognition",
            model=model,
            device=device,
            chunk_length_s=30,
            stride_length_s=(5, 5),
        )

    except Exception as exc:
        log("Whisper alignment unavailable:", str(exc)[:300])
        return None


def whisper_language(language):
    value = clean_text(language).lower()

    if value in {"english", "en", "eng"}:
        return "english"

    if value in {"hinglish", "mixed", "hi-en", "en-hi"}:
        return None

    return "hindi"


def recognize_words(aligner, audio_path, language):
    if aligner is None:
        return []

    try:
        kwargs = {"task": "transcribe"}
        lang = whisper_language(language)

        if lang:
            kwargs["language"] = lang

        result = aligner(
            str(audio_path),
            generate_kwargs=kwargs,
            return_timestamps="word",
        )

        output = []

        for chunk in result.get("chunks", []):
            timestamp = chunk.get("timestamp")
            text = clean_text(chunk.get("text"))

            if (
                not isinstance(timestamp, (tuple, list))
                or len(timestamp) != 2
                or not text
            ):
                continue

            try:
                start, end = map(float, timestamp)
            except (TypeError, ValueError):
                continue

            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
            ):
                continue

            for token in words_from(text):
                output.append({
                    "word": token,
                    "start": start,
                    "end": end,
                })

        return output

    except Exception as exc:
        log("Word recognition failed:", str(exc)[:250])
        return []


def align_words(caption_text, recognized, duration):
    source = words_from(caption_text)

    if not source or not recognized or duration <= 0:
        return None, 0.0

    left = [normalize_word(word) for word in source]
    right = [normalize_word(item["word"]) for item in recognized]

    matcher = SequenceMatcher(
        None,
        left,
        right,
        autojunk=False,
    )

    times = [None] * len(source)
    matched = []

    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            i = block.a + offset
            j = block.b + offset

            start = max(
                0.0,
                min(duration, float(recognized[j]["start"])),
            )
            end = max(
                0.0,
                min(duration, float(recognized[j]["end"])),
            )

            if end > start:
                times[i] = (start, end)
                matched.append(i)

    similarity = len(matched) / max(1, len(source))

    if similarity < ALIGNMENT_MIN_MATCH:
        return None, similarity

    known = [
        (index, value)
        for index, value in enumerate(times)
        if value is not None
    ]

    if not known:
        return None, similarity

    first_index, first_time = known[0]

    if first_index:
        step = first_time[0] / first_index

        for index in range(first_index):
            times[index] = (
                step * index,
                step * (index + 1),
            )

    for (left_i, left_t), (right_i, right_t) in zip(
        known,
        known[1:],
    ):
        gap = right_i - left_i

        if gap <= 1:
            continue

        start = left_t[1]
        end = right_t[0]

        weights = [
            max(1, len(source[index]))
            for index in range(left_i + 1, right_i)
        ]

        total = sum(weights)
        cursor = start

        for offset, weight in enumerate(weights, 1):
            next_time = (
                end
                if offset == len(weights)
                else cursor + max(0.0, end - start) * weight / total
            )

            times[left_i + offset] = (cursor, next_time)
            cursor = next_time

    last_index, last_time = known[-1]

    if last_index < len(source) - 1:
        count = len(source) - last_index - 1
        step = max(0.0, duration - last_time[1]) / count

        for index in range(last_index + 1, len(source)):
            start = last_time[1] + step * (index - last_index - 1)
            times[index] = (
                start,
                min(duration, start + step),
            )

    result = []

    for word, pair in zip(source, times):
        if pair is None:
            return None, similarity

        start, end = pair
        start = max(0.0, min(duration, start))
        end = max(start + 0.04, min(duration, end))

        if end > duration:
            end = duration
            start = max(0.0, end - 0.04)

        result.append({
            "word": word,
            "start": round(start, 3),
            "end": round(end, 3),
        })

    return result, similarity


def phrase_cues_from_words(words):
    cues = []
    current = []
    start = end = None

    for item in words:
        if not current:
            start = item["start"]

        current.append(item["word"])
        end = item["end"]

        boundary = (
            len(" ".join(current)) >= 38
            or len(current) >= 7
            or bool(re.search(r"[।.!?！？]$", item["word"]))
        )

        if boundary:
            cues.append({
                "start": start,
                "end": max(start + 0.06, end),
                "text": " ".join(current),
            })

            current = []
            start = end = None

    if current:
        cues.append({
            "start": start,
            "end": max(start + 0.06, end),
            "text": " ".join(current),
        })

    return cues


def allocate_phrases(phrases, start, end):
    if not phrases or end <= start:
        return []

    weights = [max(1, len(item.split())) for item in phrases]
    total = sum(weights)
    cursor = start
    result = []

    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        next_time = (
            end
            if index == len(phrases) - 1
            else cursor + (end - start) * weight / total
        )

        result.append({
            "start": cursor,
            "end": next_time,
            "text": phrase,
        })

        cursor = next_time

    return result


def chunk_based_cues(text, chunks, duration):
    phrases = split_phrases(text)

    if not phrases or duration <= 0:
        raise RuntimeError("Cannot caption empty narration")

    valid = [
        item
        for item in chunks
        if float(item.get("duration", 0)) > 0
    ]

    if not valid:
        return allocate_phrases(phrases, 0, duration)

    total_duration = sum(float(item["duration"]) for item in valid)
    phrase_index = 0
    cursor = 0.0
    result = []

    for index, chunk in enumerate(valid):
        chunk_end = (
            duration
            if index == len(valid) - 1
            else min(
                duration,
                cursor + duration * float(chunk["duration"]) / total_duration,
            )
        )

        chunk_phrases = split_phrases(chunk["text"])

        if not chunk_phrases:
            cursor = chunk_end
            continue

        remaining = len(phrases) - phrase_index

        if remaining <= 0:
            break

        if index == len(valid) - 1:
            take = remaining
        else:
            proportion = float(chunk["duration"]) / total_duration
            take = max(
                1,
                round(len(phrases) * proportion),
            )
            take = min(take, remaining)

        selected = phrases[phrase_index:phrase_index + take]

        if selected:
            result.extend(
                allocate_phrases(selected, cursor, chunk_end)
            )

        phrase_index += take
        cursor = chunk_end

    if phrase_index < len(phrases):
        remaining = phrases[phrase_index:]

        if result:
            start = result[-1]["end"]
        else:
            start = 0.0

        if duration > start:
            result.extend(
                allocate_phrases(remaining, start, duration)
            )

    return result


def normalize_cues(cues, duration):
    clean = []

    for cue in cues:
        try:
            start = max(0.0, min(duration, float(cue["start"])))
            end = max(0.0, min(duration, float(cue["end"])))
        except (KeyError, TypeError, ValueError):
            continue

        text = clean_text(cue.get("text"))

        if not text or end <= start:
            continue

        if end - start < 0.08:
            end = min(duration, start + 0.08)

        if end > start:
            clean.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
            })

    clean.sort(key=lambda item: (item["start"], item["end"]))

    for index in range(len(clean) - 1):
        clean[index]["end"] = min(
            clean[index]["end"],
            clean[index + 1]["start"],
        )

    clean = [
        item
        for item in clean
        if item["end"] - item["start"] >= 0.04
    ]

    if not clean:
        raise RuntimeError("No valid caption cue timing")

    return clean


def build_scene_cues(
    text,
    narration,
    duration,
    aligner,
    cache,
    cache_key,
    language,
):
    cached = cache.get(cache_key)

    if isinstance(cached, dict) and cached.get("status") == "aligned":
        words = cached.get("words", [])

        if words and all(
            isinstance(item, dict)
            and item.get("word")
            and isinstance(item.get("start"), (int, float))
            and isinstance(item.get("end"), (int, float))
            for item in words
        ):
            try:
                return (
                    normalize_cues(
                        phrase_cues_from_words(words),
                        duration,
                    ),
                    "whisper-word-alignment",
                )
            except RuntimeError:
                pass

    recognized = recognize_words(
        aligner,
        narration["path"],
        language,
    )

    aligned, similarity = align_words(
        text,
        recognized,
        duration,
    )

    if aligned:
        cache[cache_key] = {
            "status": "aligned",
            "similarity": round(similarity, 4),
            "words": aligned,
        }

        try:
            return (
                normalize_cues(
                    phrase_cues_from_words(aligned),
                    duration,
                ),
                "whisper-word-alignment",
            )
        except RuntimeError:
            pass

    cache[cache_key] = {
        "status": "fallback",
        "similarity": round(similarity, 4),
        "reason": "ASR unavailable or transcript mismatch",
    }

    cues = chunk_based_cues(
        text,
        narration["chunks"],
        duration,
    )

    return normalize_cues(cues, duration), "measured-duration-fallback"


def srt_time(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(milliseconds, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds_part, millis = divmod(rem, 1000)

    return (
        f"{hours:02}:{minutes:02}:"
        f"{seconds_part:02},{millis:03}"
    )


def write_srt(path, cues):
    blocks = []

    for index, cue in enumerate(cues, 1):
        blocks.extend([
            str(index),
            f"{srt_time(cue['start'])} --> {srt_time(cue['end'])}",
            cue["text"],
            "",
        ])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks), encoding="utf-8")


def sample_luma(video, timestamp):
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-ss", f"{max(0, timestamp):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf",
            "crop=iw:ih*0.38:0:ih*0.62,"
            "scale=1:1:flags=area,format=gray",
            "-f", "rawvideo",
            "-",
        ],
        capture_output=True,
        timeout=90,
        check=False,
    )

    return (
        int(result.stdout[0])
        if result.returncode == 0 and result.stdout
        else 64
    )


def contrast_style(
    video,
    duration,
    part,
    scene,
    seed,
    width,
    height,
    text,
):
    samples = [
        sample_luma(video, duration * factor)
        for factor in (0.18, 0.50, 0.82)
    ]

    average = sum(samples) / len(samples)

    token = hashlib.sha256(
        f"{seed}:{part}:{scene}".encode()
    ).digest()

    palette = PALETTES[token[0] % len(PALETTES)]

    if average >= 145:
        primary = palette["dark"]
        outline = "&H00FFFFFF"
        mode = "dark-on-light"
    else:
        primary = palette["light"]
        outline = "&H00101010"
        mode = "light-on-dark"

    spread = max(samples) - min(samples)
    length_factor = 0.90 if len(text) > 90 else 1.0

    return {
        "mode": mode,
        "palette": palette["name"],
        "average_luma": round(average, 2),
        "samples": samples,
        "primary": primary,
        "outline": outline,
        "outline_width": 3 if spread < 90 else 4,
        "shadow": 1,
        "alignment": 2,
        "margin_vertical": 64 if height > width else 42,
        "length_factor": length_factor,
    }


def escape_filter_path(path):
    value = str(Path(path).resolve()).replace("\\", "/")

    for old, new in (
        (":", r"\:"),
        ("'", r"\'"),
        (",", r"\,"),
        ("[", r"\["),
        ("]", r"\]"),
        (";", r"\;"),
    ):
        value = value.replace(old, new)

    return value


def caption_filter(srt_path, font_path, family, size, style):
    subtitle_file = escape_filter_path(srt_path)
    font_directory = escape_filter_path(font_path.parent)

    ass_style = (
        f"FontName={family},"
        f"FontSize={int(size * style['length_factor'])},"
        f"PrimaryColour={style['primary']},"
        f"OutlineColour={style['outline']},"
        "BackColour=&HFF000000,"
        "BorderStyle=1,"
        f"Outline={style['outline_width']},"
        f"Shadow={style['shadow']},"
        f"Alignment={style['alignment']},"
        "MarginL=42,MarginR=42,"
        f"MarginV={style['margin_vertical']}"
    )

    return (
        f"subtitles=filename='{subtitle_file}':"
        f"fontsdir='{font_directory}':"
        f"force_style='{ass_style}'"
    )


def burn_one(
    source,
    destination,
    cues,
    duration,
    font_path,
    family,
    size,
    style,
    width,
    height,
    fps,
):
    destination.parent.mkdir(parents=True, exist_ok=True)

    temp_srt = destination.with_suffix(".srt")
    temp_video = destination.with_name(
        destination.stem + ".rendering.mp4"
    )

    temp_video.unlink(missing_ok=True)

    try:
        write_srt(temp_srt, cues)

        video_filter = caption_filter(
            temp_srt,
            font_path,
            family,
            size,
            style,
        )

        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(source),
                "-vf", video_filter,
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-r", str(fps),
                "-fps_mode", "cfr",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", str(AUDIO_RATE),
                "-af", "aresample=async=1:first_pts=0",
                "-t", f"{duration:.3f}",
                "-movflags", "+faststart",
                str(temp_video),
            ],
            f"Burn captions {destination.name}",
            timeout=900,
        )

        if (
            not temp_video.is_file()
            or temp_video.stat().st_size < MIN_VIDEO_BYTES
        ):
            raise RuntimeError("Captioned clip missing/empty")

        info = media_info(temp_video)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Captioned clip missing audio/video")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Captioned clip resolution changed")

        require_fps(info, fps, destination.name)

        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            raise RuntimeError("Captioned clip duration mismatch")

        os.replace(temp_video, destination)

        return media_info(destination)

    finally:
        temp_srt.unlink(missing_ok=True)
        temp_video.unlink(missing_ok=True)


def cached_video_valid(
    path,
    previous,
    signature,
    width,
    height,
    duration,
    fps,
):
    if not path.is_file() or not isinstance(previous, dict):
        return False

    if previous.get("signature") != signature:
        return False

    try:
        if previous.get("sha256") != sha256_file(path):
            return False

        info = media_info(path)

        if not info["has_video"] or not info["has_audio"]:
            return False

        if (info["width"], info["height"]) != (width, height):
            return False

        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            return False

        require_fps(info, fps, path.name)

        return True

    except Exception:
        return False


def caption_for_scene(
    row,
    main_captions,
    extra_captions,
    narration,
):
    if row["role"] == "main":
        key = (row["part"], row["local_scene"])

        if key not in main_captions:
            raise RuntimeError(f"Main caption missing: {key}")

        caption = main_captions[key]

        if (
            caption["scene_id"]
            and caption["scene_id"] != row["scene_id"]
        ):
            raise RuntimeError(
                f"Main caption scene_id mismatch: {row['scene_id']}"
            )

        if caption.get("source_text_sha256") != narration["text_sha256"]:
            raise RuntimeError(f"{row['scene_id']}: caption source text hash mismatch.")
        return (
            caption["text"],
            caption["language"],
            caption["sha256"],
        )

    caption = extra_captions.get(row["scene_id"])

    if not caption:
        raise RuntimeError(
            f"Extra-role caption missing: {row['scene_id']}"
        )

    return (
        caption["text"],
        caption["language"],
        caption["sha256"],
    )


def scene_signature(
    row,
    narration,
    caption_hash,
    font,
    font_hash,
    size,
    fps,
    cues,
    style,
    timing,
):
    return fingerprint({
        "version": VERSION,
        "scene_id": row["scene_id"],
        "role": row["role"],
        "part": row["part"],
        "source_sha256": row["source_sha256"],
        "audio_sha256": narration["sha256"],
        "caption_sha256": caption_hash,
        "font": font,
        "font_sha256": font_hash,
        "font_size": size,
        "fps": fps,
        "duration": round(row["source_duration"], 4),
        "cues": cues,
        "style": style,
        "timing": timing,
    })


def concat_part(paths, destination, width, height, fps):
    if not paths:
        raise RuntimeError(f"No clips for {destination.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)

    temp = destination.with_name(
        destination.stem + ".joining.mp4"
    )

    list_file = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".concat.txt",
            prefix="caption_concat_",
            encoding="utf-8",
            delete=False,
        ) as stream:
            list_file = Path(stream.name)

            for path in paths:
                info = media_info(path)

                if not info["has_video"] or not info["has_audio"]:
                    raise RuntimeError(
                        f"Incomplete merge input: {path}"
                    )

                if (info["width"], info["height"]) != (width, height):
                    raise RuntimeError(
                        f"Merge resolution mismatch: {path}"
                    )

                require_fps(info, fps, path.name)

                resolved = str(path.resolve())

                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid merge path")

                stream.write(
                    "file '" + resolved.replace("'", "'\\''") + "'\n"
                )

        temp.unlink(missing_ok=True)

        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-r", str(fps),
                "-fps_mode", "cfr",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", str(AUDIO_RATE),
                "-movflags", "+faststart",
                str(temp),
            ],
            f"Join {destination.name}",
            timeout=1800,
        )

        if not temp.is_file() or temp.stat().st_size < MIN_VIDEO_BYTES:
            raise RuntimeError("Part video missing/empty")

        info = media_info(temp)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Part video missing audio/video")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Part video resolution mismatch")

        require_fps(info, fps, destination.name)

        os.replace(temp, destination)

        return media_info(destination)

    finally:
        if list_file:
            list_file.unlink(missing_ok=True)

        temp.unlink(missing_ok=True)


def load_progress():
    data = read_json(
        PROGRESS_FILE,
        required=False,
        default={},
    )

    if (
        isinstance(data, dict)
        and isinstance(data.get("scenes"), dict)
    ):
        return data["scenes"]

    return {}


def validate_manifest_role_sets(
    render_by_id,
    narration_by_id,
    part_numbers,
    scene_counts,
):
    expected_main_ids = {
        f"part_{part:02d}_scene_{scene:02d}"
        for part in part_numbers
        for scene in range(1, scene_counts[part] + 1)
    }
    expected_render_ids = set()
    for part in part_numbers:
        expected_render_ids.add(f"part_{part:02d}_hook")
        expected_render_ids.add(
            f"part_{part:02d}_" + ("resolution" if part == part_numbers[-1] else "suspense")
        )
        expected_render_ids.update(
            f"part_{part:02d}_scene_{scene:02d}"
            for scene in range(1, scene_counts[part] + 1)
        )

    expected_all_audio_ids = set(expected_main_ids) | {
        f"part_{part:02d}_{role}"
        for part in part_numbers
        for role in (("hook", "suspense") if part < part_numbers[-1] else ("hook", "resolution"))
    }
    if set(narration_by_id) != expected_all_audio_ids:
        raise RuntimeError("TTS manifest must map Hook, Main, Suspense and Resolution narration.")
    if set(render_by_id) != expected_render_ids:
        raise RuntimeError(
            "Rendered visual Scene IDs differ from the selected plan: "
            f"missing={sorted(expected_render_ids-set(render_by_id))[:15]}; "
            f"extra={sorted(set(render_by_id)-expected_render_ids)[:15]}"
        )

    for sid, audio in narration_by_id.items():
        row = render_by_id[sid]
        if row["part"] != audio["part"] or row["role"] != audio["role"]:
            raise RuntimeError(f"{sid}: narration/render role mapping mismatch.")
        if row["role"] == "main" and row["local_scene"] != audio["scene"]:
            raise RuntimeError(f"{sid}: Main narration/render scene mapping mismatch.")
        if row.get("source_text_sha256") != audio["text_sha256"]:
            raise RuntimeError(f"{sid}: rendered narration source text hash mismatch.")
        if row.get("voiceover_enabled") is not True:
            raise RuntimeError(f"{sid}: rendered narration voiceover is disabled.")
    if set(render_by_id) != expected_render_ids:
        raise RuntimeError("Rendered role/scene IDs do not match the selected plan.")


def main():
    for command in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command missing: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes_min = int(config.get("SCENES_MIN", get_scenes(config)))
    scenes_max = int(config.get("SCENES_MAX", get_scenes(config)))
    fps = int(get_fps(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    if configured_parts < 1 or scenes_min < 1 or scenes_max < scenes_min or not 1 <= fps <= 60:
        raise RuntimeError("Invalid PARTS, SCENES or FPS")

    width, height = (
        (720, 1280)
        if mode == "short"
        else (1280, 720)
    )

    render_data = read_json(RENDER_MANIFEST)

    if str(render_data.get("format", "")).lower() != mode:
        raise RuntimeError(
            "Render manifest FORMAT differs from Input/topic.txt"
        )

    if mode == "full":
        part_numbers = list(range(1, configured_parts + 1))
    else:
        part_numbers = parse_part_range(config, render_data)

    if len(part_numbers) != configured_parts:
        raise RuntimeError(
            "Selected part count differs from configured PARTS"
        )

    raw_counts = render_data.get("actual_scene_counts")
    if not isinstance(raw_counts, dict):
        raise RuntimeError("Render manifest actual_scene_counts is missing")
    scene_counts = {}
    for part in part_numbers:
        raw = raw_counts.get(str(part), raw_counts.get(part))
        if isinstance(raw, bool):
            raise RuntimeError(f"Part {part}: invalid actual Scene count")
        try:
            count = int(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Part {part}: missing actual Scene count") from exc
        if not scenes_min <= count <= scenes_max:
            raise RuntimeError(f"Part {part}: {count} Scenes outside configured range {scenes_min}-{scenes_max}")
        scene_counts[part] = count

    render_by_id, ordered = load_render_scenes(
        render_data, part_numbers, scene_counts, width, height, fps
    )

    narration_by_id, narration_data = load_audio_map()

    validate_manifest_role_sets(
        render_by_id,
        narration_by_id,
        part_numbers,
        scene_counts,
    )

    main_captions, extra_captions, caption_data = load_caption_map(
        part_numbers,
        scene_counts,
    )
    if str(caption_data.get("format", "")).lower() != mode:
        raise RuntimeError("Caption preparation FORMAT differs from Input")
    if caption_data.get("scenes_range") != [scenes_min, scenes_max]:
        raise RuntimeError("Caption preparation SCENES range differs from Input")
    prepared_counts = caption_data.get("actual_scene_counts")
    if not isinstance(prepared_counts, list) or len(prepared_counts) != len(part_numbers):
        raise RuntimeError("Caption preparation actual Scene counts are missing")
    for part, prepared_count in zip(part_numbers, prepared_counts):
        if int(prepared_count) != scene_counts[part]:
            raise RuntimeError(f"Part {part}: caption/render actual Scene count mismatch")

    family = str(get_caption_font(config)).strip()

    if not family:
        raise RuntimeError("CAPTION_FONT is empty")

    configured_size = get_caption_font_size(config)

    if str(configured_size).strip().lower() == "auto":
        size = 24 if mode == "short" else 28
    else:
        try:
            size = int(configured_size)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("CAPTION_FONT_SIZE is invalid") from exc

    if not 16 <= size <= 64:
        raise RuntimeError("CAPTION_FONT_SIZE must be 16..64")

    if caption_data.get("caption_font"):
        if str(caption_data["caption_font"]).strip() != family:
            raise RuntimeError(
                "Caption preparation font differs from configured font"
            )

    if caption_data.get("caption_font_size") is not None:
        if int(caption_data["caption_font_size"]) != size:
            raise RuntimeError(
                "Caption preparation font size differs from configured size"
            )

    requested_family = family
    family, font_path = resolve_font(requested_family)
    font_hash = sha256_file(font_path)
    if family.lower() != requested_family.lower():
        log("CAPTION FONT FALLBACK:", requested_family, "->", family, relative(font_path))

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    CAPTION_DIR.mkdir(parents=True, exist_ok=True)

    alignment_cache = read_json(
        ALIGNMENT_CACHE,
        required=False,
        default={},
    )

    if not isinstance(alignment_cache, dict):
        alignment_cache = {}

    cached = load_progress()
    aligner = load_aligner()

    seed = fingerprint({
        "format": mode,
        "parts": part_numbers,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": scene_counts,
        "main_caption_hashes": {
            f"{part}:{scene}": main_captions[(part, scene)]["sha256"]
            for part in part_numbers
            for scene in range(1, scene_counts[part] + 1)
        },
        "extra_caption_hashes": {
            key: value["sha256"]
            for key, value in sorted(extra_captions.items())
        },
        "narration_manifest": sha256_file(AUDIO_MANIFEST),
    })

    completed = {}
    final_cues = []
    global_cursor = 0.0
    methods = {}

    reused_count = 0
    burned_count = 0
    aligned_count = 0
    fallback_count = 0

    write_json(
        PROGRESS_FILE,
        {
            "status": "in_progress",
            "version": VERSION,
            "format": mode,
            "part_numbers": part_numbers,
            "total_parts": len(part_numbers),
            "scenes_range": [scenes_min, scenes_max],
            "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
            "total_scenes": len(ordered),
            "completed_count": 0,
            "scenes": cached,
        },
    )

    log("CAPTION BURN VERSION:", VERSION)
    log("FORMAT:", mode, "PARTS:", part_numbers)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", scene_counts)
    log("ROLE CLIPS:", len(ordered))
    log("FONT:", family, "SIZE:", size, "FPS:", fps)

    for index, row in enumerate(ordered, 1):
        sid = row["scene_id"]
        narration = narration_by_id.get(sid)
        source = row["source_path"]

        source_info = media_info(source)

        if not source_info["has_video"] or not source_info["has_audio"]:
            raise RuntimeError(f"Rendered scene lacks audio/video: {sid}")

        if (source_info["width"], source_info["height"]) != (
            width,
            height,
        ):
            raise RuntimeError(
                f"Rendered resolution mismatch: {sid}"
            )

        require_fps(source_info, fps, sid)

        if narration is None:
            raise RuntimeError(f"{sid}: TTS narration is missing for role {row['role']}.")

        caption_text, language, caption_hash = caption_for_scene(
            row,
            main_captions,
            extra_captions,
            narration,
        )

        audio_duration = narration["duration"]

        if abs(source_info["duration"] - audio_duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"{sid}: rendered clip/audio duration mismatch "
                f"({source_info['duration']:.3f}s vs "
                f"{audio_duration:.3f}s)"
            )

        cue_duration = min(
            source_info["duration"],
            audio_duration,
        )

        alignment_key = fingerprint({
            "version": VERSION,
            "audio_sha256": narration["sha256"],
            "caption_sha256": caption_hash,
            "language": language,
        })

        cues, timing_method = build_scene_cues(
            caption_text,
            narration,
            cue_duration,
            aligner,
            alignment_cache,
            alignment_key,
            language,
        )

        write_json(ALIGNMENT_CACHE, alignment_cache)

        if timing_method == "whisper-word-alignment":
            aligned_count += 1
        else:
            fallback_count += 1

        methods[timing_method] = methods.get(timing_method, 0) + 1

        style = contrast_style(
            source,
            source_info["duration"],
            row["part"],
            row["local_scene"],
            seed,
            width,
            height,
            caption_text,
        )

        safe_id = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            sid,
        ).strip("_")

        if not safe_id:
            raise RuntimeError(f"Unsafe scene ID: {sid}")

        destination = (
            CAPTIONED_DIR
            / f"part_{row['part']:02d}_{safe_id}.mp4"
        )

        signature = scene_signature(
            row,
            narration,
            caption_hash,
            family,
            font_hash,
            size,
            fps,
            cues,
            style,
            timing_method,
        )

        previous = cached.get(sid, {})

        reused = cached_video_valid(
            destination,
            previous,
            signature,
            width,
            height,
            source_info["duration"],
            fps,
        )

        if reused:
            info = media_info(destination)
            reused_count += 1
            log(f"REUSED {index}/{len(ordered)}: {sid}")
        else:
            info = burn_one(
                source,
                destination,
                cues,
                source_info["duration"],
                font_path,
                family,
                size,
                style,
                width,
                height,
                fps,
            )

            burned_count += 1
            log(
                f"BURNED {index}/{len(ordered)}: "
                f"{sid} [{row['role']}]"
            )

        output_hash = sha256_file(destination)

        completed[sid] = {
            **row,
            "status": "completed",
            "scene_id": sid,
            "role": row["role"],
            "scene_type": row["role"],
            "part": row["part"],
            "local_scene": row["local_scene"],
            "scene": row["local_scene"],
            "source_rendered_video": relative(source),
            "video": relative(destination),
            "output": relative(destination),
            "duration": round(info["duration"], 4),
            "width": info["width"],
            "height": info["height"],
            "fps": round(info["fps"], 4),
            "captions_burned": True,
            "caption_text_sha256": sha256_text(caption_text),
            "caption_language": language,
            "caption_style": style,
            "caption_timing": timing_method,
            "caption_cue_count": len(cues),
            "fingerprint": signature,
            "signature": signature,
            "sha256": output_hash,
            "cache_reused": reused,
        }

        cached[sid] = {
            "status": "completed",
            "signature": signature,
            "sha256": output_hash,
            "path": relative(destination),
            "scene_id": sid,
            "role": row["role"],
            "part": row["part"],
            "caption_style": style,
            "caption_timing": timing_method,
            "caption_cue_count": len(cues),
        }

        for cue in cues:
            final_cues.append({
                "start": global_cursor + cue["start"],
                "end": global_cursor + cue["end"],
                "text": cue["text"],
                "scene_id": sid,
                "part": row["part"],
                "role": row["role"],
            })

        global_cursor += info["duration"]

        write_json(
            PROGRESS_FILE,
            {
                "status": "in_progress",
                "version": VERSION,
                "format": mode,
                "part_numbers": part_numbers,
                "total_parts": len(part_numbers),
                "scenes_range": [scenes_min, scenes_max],
                "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
                "fps": fps,
                "completed_count": index,
                "total_scenes": len(ordered),
                "scenes": cached,
            },
        )

    if len(completed) != len(ordered):
        raise RuntimeError(
            f"Captioned {len(completed)}/{len(ordered)} clips"
        )

    write_srt(
        FINAL_SRT,
        sorted(final_cues, key=lambda item: item["start"]),
    )

    part_manifest = []

    for part in part_numbers:
        part_rows = [
            completed[row["scene_id"]]
            for row in ordered
            if row["part"] == part
        ]

        if not part_rows:
            raise RuntimeError(f"No clips for part {part}")

        paths = [
            safe_file(
                row["video"],
                f"Captioned scene {row['scene_id']}",
                MIN_VIDEO_BYTES,
            )
            for row in part_rows
        ]

        part_path = PART_DIR / f"part_{part:02d}.mp4"

        part_info = concat_part(
            paths,
            part_path,
            width,
            height,
            fps,
        )

        part_manifest.append({
            "part": part,
            "status": "completed",
            "scene_count": sum(
                row["role"] == "main"
                for row in part_rows
            ),
            "total_clip_count": len(part_rows),
            "roles": [row["role"] for row in part_rows],
            "path": relative(part_path),
            "output": relative(part_path),
            "sha256": sha256_file(part_path),
            "duration": round(part_info["duration"], 4),
            "fps": round(part_info["fps"], 4),
            "width": width,
            "height": height,
        })

        log("PART READY:", relative(part_path))

    main_rows = sorted(
        [
            completed[row["scene_id"]]
            for row in ordered
            if row["role"] == "main"
        ],
        key=lambda row: (
            row["part"],
            row["local_scene"],
        ),
    )

    extra_rows = [
        completed[row["scene_id"]]
        for row in ordered
        if row["role"] != "main"
    ]

    expected_main_count = sum(scene_counts.values())

    for part in part_numbers:
        actual_part_count = sum(row["part"] == part for row in main_rows)
        if actual_part_count != scene_counts[part]:
            raise RuntimeError(f"Part {part}: expected {scene_counts[part]} Main captions, got {actual_part_count}")

    if len(main_rows) != expected_main_count:
        raise RuntimeError("Final Main caption count mismatch")

    expected_extra_count = len(part_numbers) * 2

    if len(extra_rows) != expected_extra_count:
        raise RuntimeError(
            "Final Hook/Suspense/Resolution count mismatch"
        )

    for part in part_numbers:
        part_roles = [
            row["role"]
            for row in extra_rows
            if row["part"] == part
        ]

        if part_roles.count("hook") != 1:
            raise RuntimeError(f"Part {part} Hook output missing")

        if part == part_numbers[-1]:
            if part_roles.count("resolution") != 1:
                raise RuntimeError("Final Resolution output missing")
            if "suspense" in part_roles:
                raise RuntimeError("Final part cannot have Suspense")
        else:
            if part_roles.count("suspense") != 1:
                raise RuntimeError(f"Part {part} Suspense output missing")
            if "resolution" in part_roles:
                raise RuntimeError(
                    f"Non-final part {part} cannot have Resolution"
                )

    final_manifest = {
        **render_data,
        "status": "completed",
        "version": VERSION,
        "caption_version": VERSION,
        "format": mode,
        "part_numbers": part_numbers,
        "total_parts": len(part_numbers),
        "parts_count": len(part_numbers),
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
        "fps": fps,
        "width": width,
        "height": height,
        "captions_burned": True,
        "caption_style": "adaptive-scene-contrast",
        "caption_timing_methods": methods,
        "caption_alignment_scenes": aligned_count,
        "caption_fallback_scenes": fallback_count,
        "caption_font": family,
        "caption_font_file": str(font_path),
        "caption_font_requested": requested_family,
        "caption_font_sha256": font_hash,
        "caption_font_size": size,
        "expected_scene_count": len(ordered),
        "completed_count": len(completed),
        "all_completed_scene_count": len(completed),
        "extra_scene_count": len(extra_rows),
        "completed_scenes": main_rows,
        "scenes": main_rows,
        "extra_scenes": extra_rows,
        "all_completed_scenes": [
            completed[row["scene_id"]]
            for row in ordered
        ],
        "parts": part_manifest,
        "part_outputs_only": True,
        "final_video": None,
        "alias": None,
        "duration": round(
            sum(row["duration"] for row in part_manifest),
            4,
        ),
        "resolution": [width, height],
        "caption_manifest": relative(CAPTION_MANIFEST),
        "final_srt": relative(FINAL_SRT),
        "final_srt_sha256": sha256_file(FINAL_SRT),
        "captioned_new": burned_count,
        "captioned_reused": reused_count,
        "hook_count": sum(
            row["role"] == "hook"
            for row in extra_rows
        ),
        "suspense_count": sum(
            row["role"] == "suspense"
            for row in extra_rows
        ),
        "resolution_count": sum(
            row["role"] == "resolution"
            for row in extra_rows
        ),
        "captions_burned_main_scenes": True,
        "captions_burned_all_roles": True,
        "visual_only_role_scenes_captioned": False,
        "narrated_extra_roles_captioned": True,
        "caption_timeline": {
            "includes_hook": True,
            "includes_main": True,
            "includes_suspense": True,
            "includes_resolution": True,
            "global_srt": True,
            "scene_order": "hook-main-suspense-or-resolution",
        },
    }

    write_json(RENDER_MANIFEST, final_manifest)

    write_json(
        PROGRESS_FILE,
        {
            "status": "completed",
            "version": VERSION,
            "format": mode,
            "part_numbers": part_numbers,
            "total_parts": len(part_numbers),
            "scenes_range": [scenes_min, scenes_max],
            "actual_scene_counts": {str(part): scene_counts[part] for part in part_numbers},
            "fps": fps,
            "completed_count": len(completed),
            "total_scenes": len(completed),
            "parts": part_manifest,
            "scenes": cached,
            "alignment_scenes": aligned_count,
            "fallback_scenes": fallback_count,
        },
    )

    log("=" * 60)
    log("CAPTION BURN COMPLETED")
    log("FORMAT:", mode, "PARTS:", part_numbers)
    log("MAIN CLIPS:", expected_main_count)
    log("ALL ROLE CLIPS:", len(completed))
    log("NEW:", burned_count, "REUSED:", reused_count)
    log("WORD-ALIGNED:", aligned_count, "FALLBACK:", fallback_count)
    log("PART OUTPUTS:", len(part_manifest))
    log("MANIFEST:", relative(RENDER_MANIFEST))
    log("=" * 60)




# _PHOTO_MOTION_EVENT_WRAPPED: preserve original stage behavior and record factual outcomes.
_photo_motion_original_main = main
def main():
    import time as _pm_time
    _pm_started = _pm_time.monotonic()
    try:
        try:
            from production_events import record_event as _pm_record
        except ImportError:
            from scripts.production_events import record_event as _pm_record
        _pm_record("burn_captions", "running")
    except Exception:
        _pm_record = None
    try:
        _pm_result = _photo_motion_original_main()
        if _pm_record:
            _pm_record("burn_captions", "completed" if _pm_result in (None, 0) else "failed", metadata={"return_code": _pm_result}, started_at=_pm_started)
        return _pm_result
    except BaseException as _pm_exc:
        if _pm_record:
            _pm_record("burn_captions", "failed", error=f"{type(_pm_exc).__name__}: {_pm_exc}", started_at=_pm_started)
        raise

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("CAPTION BURN CANCELLED; completed checkpoints retained.")
        sys.exit(130)
    except Exception as exc:
        log("CAPTION BURN FAILED:", str(exc))
        sys.exit(1)