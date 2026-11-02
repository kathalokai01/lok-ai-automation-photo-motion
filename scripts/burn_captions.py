#!/usr/bin/env python3
"""Speech-aligned multilingual captions for all Photo Motion scene roles."""

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
from pathlib import Path
from fractions import Fraction

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
VIDEO_DIR = OUT / "videos"

RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
CAPTION_MANIFEST = CAPTION_DIR / "captions.json"
PROGRESS_FILE = CAPTION_DIR / "burn_progress.json"
ALIGNMENT_CACHE = CAPTION_DIR / "word_alignment_cache.json"
FINAL_SRT = CAPTION_DIR / "final_captions.srt"

VERSION = "burn-captions-v50-role-aware-resumable"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
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


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_data(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256_bytes(raw)


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


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(command, label, timeout=900):
    result = subprocess.run(
        [str(value) for value in command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-4000:]
        raise RuntimeError(f"{label} failed: {detail}")

    return result


def rows_from(data, *keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]

    return []


def first_path(row, keys):
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
        raise RuntimeError(f"{label}: path is outside project directory")

    if not path.is_file():
        raise RuntimeError(f"{label}: file does not exist: {path}")

    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file is too small: {path.name}")

    return path


def relative(path):
    path = Path(path).resolve()

    try:
        return path.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        raise RuntimeError(f"Path is outside project: {path}")


def media_info(path):
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration:"
            "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
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

    fps_value = None

    if video:
        fps_value = (
            video.get("avg_frame_rate")
            or video.get("r_frame_rate")
        )

    try:
        fps = float(Fraction(str(fps_value))) if fps_value else 0.0
    except (ValueError, ZeroDivisionError, TypeError):
        fps = 0.0

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "fps": fps,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def require_fps(info, expected, label):
    actual = float(info.get("fps", 0))

    if actual <= 0 or abs(actual - expected) > FPS_TOLERANCE:
        raise RuntimeError(
            f"FPS mismatch in {label}: expected {expected}, got {actual:.4f}"
        )


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_word(value):
    return re.sub(
        r"[^\w]",
        "",
        str(value or "").lower(),
        flags=re.UNICODE,
    )


def words_from(text):
    return [
        token for token in re.findall(r"\S+", clean_text(text))
        if normalize_word(token)
    ]


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


def scene_id(row, label):
    value = str(row.get("scene_id") or row.get("id") or "").strip()

    if not value:
        raise RuntimeError(f"{label}: scene_id missing")

    return value


def split_phrases(text, max_chars=38, max_words=7):
    text = clean_text(text)

    if not text:
        return []

    sentences = re.split(
        r"(?<=[.!?।…！？])\s+",
        text,
    )

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


def resolve_font(family):
    result = run(
        ["fc-match", "-f", "%{file}\n", family],
        f"Resolve font {family}",
        timeout=60,
    )

    lines = result.stdout.strip().splitlines()

    if not lines:
        raise RuntimeError(f"Caption font not found: {family}")

    path = Path(lines[0]).resolve()

    if not path.is_file():
        raise RuntimeError(f"Font file missing: {path}")

    return path


def load_caption_map(parts, scenes):
    data = read_json(CAPTION_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption preparation manifest is incomplete")

    mapping = {}

    for row in rows_from(data, "captions", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption record")

        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene", 0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption part/scene") from exc

        text = clean_text(
            row.get(
                "text",
                row.get("caption", row.get("caption_text", "")),
            )
        )

        if not text:
            raise RuntimeError(f"Empty caption for scene {(part, scene)}")

        key = (part, scene)

        if key in mapping:
            raise RuntimeError(f"Duplicate caption scene {key}")

        mapping[key] = {
            "text": text,
            "language": clean_text(
                row.get("caption_language", data.get("mode", "hindi"))
            ),
            "sha256": (
                row.get("caption_sha256")
                or sha256_bytes(text.encode("utf-8"))
            ),
            "scene_id": str(row.get("scene_id") or "").strip(),
        }

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    if set(mapping) != expected:
        raise RuntimeError(
            "Caption scene mapping mismatch; "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return mapping, data


def load_audio_map():
    data = read_json(AUDIO_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("TTS audio manifest is incomplete")

    rows = rows_from(data, "jobs", "audio_jobs", "scenes")

    if not rows:
        raise RuntimeError("TTS manifest has no audio jobs")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid TTS audio job {index}")

        sid = scene_id(row, f"TTS job {index}")

        if sid in mapping:
            raise RuntimeError(f"Duplicate TTS scene_id: {sid}")

        if str(row.get("status", "")).lower() not in {
            "completed", "reused",
        }:
            raise RuntimeError(f"TTS scene is incomplete: {sid}")

        audio_path = safe_file(
            first_path(
                row,
                ("output", "audio_path", "path", "file", "audio"),
            ),
            f"Narration {sid}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(audio_path)

        if not info["has_audio"]:
            raise RuntimeError(f"Narration audio stream missing: {sid}")

        chunks = []

        raw_chunks = row.get("chunks")

        if isinstance(raw_chunks, list):
            for position, chunk in enumerate(raw_chunks, 1):
                if not isinstance(chunk, dict):
                    continue

                try:
                    duration = float(chunk.get("duration", 0))
                except (TypeError, ValueError):
                    continue

                text = clean_text(chunk.get("text", ""))

                if math.isfinite(duration) and duration > 0 and text:
                    chunks.append({
                        "index": position,
                        "duration": duration,
                        "text": text,
                    })

        mapping[sid] = {
            "scene_id": sid,
            "part": int(row["part"]),
            "scene": int(row.get("scene", 0) or 0),
            "role": normalize_role(row),
            "path": audio_path,
            "duration": info["duration"],
            "sha256": digest_file(audio_path),
            "chunks": chunks,
            "text": clean_text(row.get("text") or row.get("narration_text")),
            "language": clean_text(
                row.get("captions") or row.get("language") or "hindi"
            ),
            "provider": row.get("provider"),
            "model": row.get("model"),
        }

    return mapping, data


def load_render_scenes(data, parts, scenes, width, height, fps):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest is incomplete")

    if str(data.get("format", "")).lower() not in {"short", "full"}:
        raise RuntimeError("Render manifest FORMAT is invalid")

    if int(data.get("width", 0)) != width:
        raise RuntimeError("Render manifest width mismatch")

    if int(data.get("height", 0)) != height:
        raise RuntimeError("Render manifest height mismatch")

    if abs(float(data.get("fps", 0)) - fps) > FPS_TOLERANCE:
        raise RuntimeError("Render manifest FPS mismatch")

    rows = (
        data.get("all_completed_scenes")
        or data.get("completed_scenes")
        or data.get("scenes")
        or []
    )

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Render manifest has no scene rows")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render scene row {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Render scene {index} is incomplete")

        sid = scene_id(row, f"Render row {index}")

        if sid in mapping:
            raise RuntimeError(f"Duplicate render scene_id: {sid}")

        part = int(row["part"])
        role = normalize_role(row)

        if not 1 <= part <= parts:
            raise RuntimeError(f"{sid}: part outside configured range")

        if role == "main":
            local_scene = int(
                row.get(
                    "local_scene",
                    row.get("scene", row.get("source_scene_number", 0)),
                )
            )

            if not 1 <= local_scene <= scenes:
                raise RuntimeError(f"{sid}: invalid main scene number")

        else:
            local_scene = int(
                row.get("local_scene", row.get("scene", 0)) or 0
            )

        path = safe_file(
            first_path(
                row,
                ("video", "output", "path"),
            ),
            f"Rendered scene {sid}",
            MIN_VIDEO_BYTES,
        )

        checksum = digest_file(path)
        declared = row.get("sha256") or row.get("video_sha256")

        if declared and declared != checksum:
            raise RuntimeError(f"Render scene checksum mismatch: {sid}")

        info = media_info(path)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Rendered scene lacks audio/video: {sid}")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Rendered scene resolution mismatch: {sid}")

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
            "photo_motion_global_scene": int(
                row.get(
                    "photo_motion_global_scene",
                    row.get("global_scene", index),
                )
                or index
            ),
        }

    expected_main = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    actual_main = {
        (row["part"], row["local_scene"])
        for row in mapping.values()
        if row["role"] == "main"
    }

    if actual_main != expected_main:
        raise RuntimeError(
            "Rendered main scene mapping mismatch; "
            f"missing={sorted(expected_main - actual_main)}; "
            f"extra={sorted(actual_main - expected_main)}"
        )

    for part in range(1, parts + 1):
        roles = [
            row["role"] for row in mapping.values()
            if row["part"] == part
        ]

        if roles.count("hook") != 1:
            raise RuntimeError(
                f"Part {part} must contain exactly one Hook"
            )

        if part < parts:
            if roles.count("suspense") != 1:
                raise RuntimeError(
                    f"Part {part} must contain exactly one Suspense"
                )
            if roles.count("resolution"):
                raise RuntimeError(
                    f"Non-final part {part} cannot contain Resolution"
                )
        else:
            if roles.count("resolution") != 1:
                raise RuntimeError(
                    "Final part must contain exactly one Resolution"
                )
            if roles.count("suspense"):
                raise RuntimeError(
                    "Final part cannot contain Suspense"
                )

    ordered = sorted(
        mapping.values(),
        key=lambda row: (
            row["part"],
            ROLE_ORDER[row["role"]],
            row["local_scene"] if row["role"] == "main"
            else row["photo_motion_global_scene"],
            row["photo_motion_global_scene"],
        ),
    )

    return mapping, ordered


def load_aligner():
    try:
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        model = os.environ.get(
            "CAPTION_ALIGNMENT_MODEL",
            "openai/whisper-tiny",
        ).strip()

        log(
            "Loading speech alignment model:",
            model,
            "device=" + ("GPU" if device == 0 else "CPU"),
        )

        return pipeline(
            "automatic-speech-recognition",
            model=model,
            device=device,
            chunk_length_s=30,
            stride_length_s=(5, 5),
        )

    except Exception as exc:
        log("Speech alignment unavailable:", str(exc)[:400])
        return None


def whisper_language(language):
    language = clean_text(language).lower()

    if language in {"english", "en", "eng"}:
        return "english"

    if language in {"hinglish", "mixed", "hi-en", "en-hi"}:
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
            if not isinstance(chunk, dict):
                continue

            timestamp = chunk.get("timestamp")
            text = clean_text(chunk.get("text", ""))

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
        log(
            "Speech recognition failed:",
            Path(audio_path).name,
            str(exc)[:250],
        )
        return []


def align_words(caption_text, recognized, duration):
    source = words_from(caption_text)

    if not source or not recognized or duration <= 0:
        return None, 0.0

    left = [normalize_word(word) for word in source]
    right = [normalize_word(item["word"]) for item in recognized]

    matcher = SequenceMatcher(None, left, right, autojunk=False)
    matched = []

    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            i = block.a + offset
            j = block.b + offset

            matched.append((
                i,
                max(0.0, min(duration, float(recognized[j]["start"]))),
                max(0.0, min(duration, float(recognized[j]["end"]))),
            ))

    similarity = len(matched) / max(1, len(source))

    if similarity < ALIGNMENT_MIN_MATCH:
        return None, similarity

    times = [None] * len(source)

    for index, start, end in matched:
        times[index] = (start, end)

    known = [
        (index, value)
        for index, value in enumerate(times)
        if value is not None
    ]

    if not known:
        return None, similarity

    first_index, first_value = known[0]

    if first_index:
        step = first_value[0] / first_index

        for index in range(first_index):
            end = step * (index + 1)
            times[index] = (max(0, end - step), end)

    for pair_index in range(len(known) - 1):
        left_index, left_value = known[pair_index]
        right_index, right_value = known[pair_index + 1]
        gap = right_index - left_index

        if gap <= 1:
            continue

        start = left_value[1]
        end = right_value[0]
        weights = [
            max(1, len(source[index]))
            for index in range(left_index + 1, right_index)
        ]
        total = sum(weights)
        cursor = start

        for offset, weight in enumerate(weights, 1):
            next_cursor = (
                end if offset == len(weights)
                else cursor + max(0, end - start) * weight / total
            )
            times[left_index + offset] = (cursor, next_cursor)
            cursor = next_cursor

    last_index, last_value = known[-1]

    if last_index < len(source) - 1:
        remaining = len(source) - last_index - 1
        step = max(0, duration - last_value[1]) / remaining

        for index in range(last_index + 1, len(source)):
            start = last_value[1] + step * (index - last_index - 1)
            times[index] = (start, min(duration, start + step))

    result = []

    for word, timing in zip(source, times):
        if timing is None:
            return None, similarity

        start, end = timing
        start = max(0.0, min(duration, start))
        end = max(start + 0.04, min(duration, end))

        if end > duration:
            end = duration
            start = min(start, end - 0.04)

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
        text = " ".join(current)

        if (
            len(text) >= 38
            or len(current) >= 7
            or re.search(r"[।.!?！？]$", item["word"])
        ):
            cues.append({
                "start": start,
                "end": max(start + 0.06, end),
                "text": text,
            })
            current, start, end = [], None, None

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
        next_cursor = (
            end if index == len(phrases) - 1
            else cursor + (end - start) * weight / total
        )

        result.append({
            "start": cursor,
            "end": next_cursor,
            "text": phrase,
        })
        cursor = next_cursor

    return result


def chunk_based_cues(text, chunks, duration):
    phrases = split_phrases(text)

    if not phrases or duration <= 0:
        raise RuntimeError("Cannot create captions from empty narration")

    valid_chunks = [
        item for item in chunks
        if float(item.get("duration", 0)) > 0
    ]

    if not valid_chunks:
        return allocate_phrases(phrases, 0, duration)

    total_duration = sum(
        float(item["duration"]) for item in valid_chunks
    )

    if total_duration <= 0:
        return allocate_phrases(phrases, 0, duration)

    total_words = sum(max(1, len(item.split())) for item in phrases)
    cursor_phrase = 0
    cursor_time = 0.0
    result = []

    for index, chunk in enumerate(valid_chunks):
        chunk_end = (
            duration if index == len(valid_chunks) - 1
            else min(
                duration,
                cursor_time
                + duration * float(chunk["duration"]) / total_duration,
            )
        )

        if cursor_phrase >= len(phrases):
            break

        if index == len(valid_chunks) - 1:
            next_phrase = len(phrases)
        else:
            target = (
                total_words
                * float(chunk["duration"])
                / total_duration
            )
            accumulated = 0
            next_phrase = cursor_phrase

            while next_phrase < len(phrases):
                if (
                    next_phrase > cursor_phrase
                    and accumulated >= target
                ):
                    break

                accumulated += max(
                    1,
                    len(phrases[next_phrase].split()),
                )
                next_phrase += 1

            next_phrase = max(cursor_phrase + 1, next_phrase)

        result.extend(
            allocate_phrases(
                phrases[cursor_phrase:next_phrase],
                cursor_time,
                chunk_end,
            )
        )

        cursor_phrase = next_phrase
        cursor_time = chunk_end

    if cursor_phrase < len(phrases):
        if result:
            result[-1]["text"] += " " + " ".join(
                phrases[cursor_phrase:]
            )
            result[-1]["end"] = duration
        else:
            result = allocate_phrases(phrases, 0, duration)

    return result


def silence_boundaries(audio_path, duration):
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats",
                "-i", str(audio_path),
                "-vn",
                "-af", "silencedetect=noise=-35dB:d=0.18",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

        if result.returncode:
            return []

        output = (result.stderr or "") + "\n" + (result.stdout or "")

        starts = [
            float(value)
            for value in re.findall(
                r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)",
                output,
            )
        ]

        ends = [
            float(value)
            for value in re.findall(
                r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)",
                output,
            )
        ]

        points = []

        for start, end in zip(starts, ends):
            middle = (start + end) / 2

            if 0.05 < middle < duration - 0.05:
                points.append(middle)

        return sorted(set(round(value, 3) for value in points))

    except Exception as exc:
        log("Silence detection unavailable:", str(exc)[:200])
        return []


def refine_silence(cues, audio_path, duration):
    boundaries = silence_boundaries(audio_path, duration)

    if not boundaries:
        return cues, "measured-audio-duration-estimate"

    refined = []

    for index, cue in enumerate(cues):
        start = float(cue["start"])
        end = float(cue["end"])

        candidates = [
            point for point in boundaries
            if abs(point - end) <= 0.45
            and point > start + 0.12
            and point < duration - 0.05
        ]

        if candidates:
            end = min(candidates, key=lambda value: abs(value - end))

        if index + 1 < len(cues):
            end = min(end, float(cues[index + 1]["start"]))

        if end - start < 0.08:
            end = min(duration, start + 0.08)

        if end > start:
            refined.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": clean_text(cue["text"]),
            })

    if not refined:
        return cues, "measured-audio-duration-estimate"

    return refined, "audio-duration-and-silence-assisted"


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
        cue for cue in clean
        if cue["end"] - cue["start"] >= 0.04
    ]

    if not clean:
        raise RuntimeError("Caption cues contain no valid time ranges")

    return clean


def build_scene_cues(
    text,
    narration,
    duration,
    aligner,
    alignment_cache,
    cache_key,
    language,
):
    cached = alignment_cache.get(cache_key)

    if isinstance(cached, dict) and cached.get("status") == "aligned":
        words = cached.get("words", [])

        if words and all(
            isinstance(item, dict)
            and item.get("word")
            and isinstance(item.get("start"), (int, float))
            and isinstance(item.get("end"), (int, float))
            for item in words
        ):
            cues = phrase_cues_from_words(words)

            if cues:
                try:
                    return (
                        normalize_cues(cues, duration),
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
        alignment_cache[cache_key] = {
            "status": "aligned",
            "similarity": round(similarity, 4),
            "words": aligned,
        }

        cues = phrase_cues_from_words(aligned)

        if cues:
            try:
                return (
                    normalize_cues(cues, duration),
                    "whisper-word-alignment",
                )
            except RuntimeError:
                pass

    alignment_cache[cache_key] = {
        "status": "fallback",
        "similarity": round(similarity, 4),
        "reason": "ASR unavailable or transcript did not match",
    }

    cues = chunk_based_cues(
        text,
        narration["chunks"],
        duration,
    )

    cues, method = refine_silence(
        cues,
        narration["path"],
        duration,
    )

    return normalize_cues(cues, duration), method


def srt_time(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)

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
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0, timestamp):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf",
            "crop=iw:ih*0.38:0:ih*0.62,"
            "scale=1:1:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
        timeout=90,
        check=False,
    )

    if result.returncode or not result.stdout:
        return 64

    return int(result.stdout[0])


def contrast_style(video, duration, part, scene, seed, width, height):
    samples = [
        sample_luma(video, duration * fraction)
        for fraction in (0.18, 0.50, 0.82)
    ]

    average = sum(samples) / len(samples)

    token = hashlib.sha256(
        f"{seed}:{part}:{scene}".encode("utf-8")
    ).digest()

    palette = PALETTES[token[0] % len(PALETTES)]

    if average >= 145:
        primary = palette["dark"]
        outline = "&H00FFFFFF"
        mode = "dark-text-on-light-background"
    else:
        primary = palette["light"]
        outline = "&H00101010"
        mode = "light-text-on-dark-background"

    spread = max(samples) - min(samples)

    return {
        "mode": mode,
        "palette": palette["name"],
        "average_luma": round(average, 2),
        "sample_luma": samples,
        "primary": primary,
        "outline": outline,
        "outline_width": 3 if spread < 90 else 4,
        "shadow": 1,
        "alignment": 2,
        "margin_vertical": 58 if height > width else 42,
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
        f"FontName={family},FontSize={int(size)},"
        f"PrimaryColour={style['primary']},"
        f"OutlineColour={style['outline']},"
        "BackColour=&H00000000,BorderStyle=1,"
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
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
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
            f"Burn captions into {destination.name}",
            timeout=900,
        )

        require_file = temp_video.is_file() and (
            temp_video.stat().st_size >= MIN_VIDEO_BYTES
        )

        if not require_file:
            raise RuntimeError("Captioned video output is missing or empty")

        info = media_info(temp_video)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Captioned video lacks video/audio")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Captioned video dimensions changed")

        require_fps(info, fps, destination.name)

        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            raise RuntimeError("Captioned video duration mismatch")

        os.replace(temp_video, destination)
        return media_info(destination)

    finally:
        temp_srt.unlink(missing_ok=True)
        temp_video.unlink(missing_ok=True)


def concat_videos(paths, destination, width, height, fps):
    if not paths:
        raise RuntimeError(f"No videos supplied for {destination.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(
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
                    raise RuntimeError(f"Incomplete merge input: {path}")

                if (info["width"], info["height"]) != (width, height):
                    raise RuntimeError(f"Merge resolution mismatch: {path}")

                require_fps(info, fps, path.name)

                resolved = str(path.resolve())

                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in merge path")

                escaped = resolved.replace("'", "'\\''")
                stream.write(f"file '{escaped}'\n")

        temporary.unlink(missing_ok=True)

        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0",
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
                str(temporary),
            ],
            f"Merge {destination.name}",
            timeout=1800,
        )

        if (
            not temporary.is_file()
            or temporary.stat().st_size < MIN_VIDEO_BYTES
        ):
            raise RuntimeError("Merged video output is missing or empty")

        info = media_info(temporary)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Merged video lacks audio/video")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Merged video resolution mismatch")

        require_fps(info, fps, destination.name)

        os.replace(temporary, destination)
        return media_info(destination)

    finally:
        if list_file is not None:
            list_file.unlink(missing_ok=True)

        temporary.unlink(missing_ok=True)


def load_progress():
    data = read_json(
        PROGRESS_FILE,
        required=False,
        default={},
    )

    if not isinstance(data, dict):
        return {}

    scenes = data.get("scenes", {})

    return scenes if isinstance(scenes, dict) else {}


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
        if previous.get("sha256") != digest_file(path):
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


def caption_for_scene(row, caption_map, narration):
    role = row["role"]

    if role == "main":
        key = (row["part"], row["local_scene"])

        if key not in caption_map:
            raise RuntimeError(f"Caption missing for main scene {key}")

        caption = caption_map[key]

        if caption.get("scene_id") and (
            caption["scene_id"] != row["scene_id"]
        ):
            raise RuntimeError(
                f"Caption scene_id mismatch for {row['scene_id']}"
            )

        return caption["text"], caption["language"], caption["sha256"]

    text = narration.get("text", "")

    if not text:
        raise RuntimeError(
            f"Hook/Suspense/Resolution narration text missing: "
            f"{row['scene_id']}"
        )

    language = narration.get("language") or "hindi"

    return (
        text,
        language,
        sha256_bytes(text.encode("utf-8")),
    )


def make_scene_signature(
    row,
    narration,
    caption_hash,
    font,
    font_hash,
    size,
    fps,
    cues,
    style,
    timing_method,
):
    return digest_data({
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
        "timing_method": timing_method,
    })


def make_captioned_row(
    row,
    narration,
    destination,
    info,
    checksum,
    signature,
    caption_text,
    language,
    style,
    timing_method,
    cues,
    reused,
):
    result = dict(row)

    result.update({
        "scene_id": row["scene_id"],
        "role": row["role"],
        "scene_type": row["role"],
        "part": row["part"],
        "local_scene": row["local_scene"],
        "scene": row["local_scene"],
        "status": "completed",
        "source_rendered_video": relative(row["source_path"]),
        "source_photo_motion_video": row.get(
            "source_photo_motion_video"
        ),
        "video": relative(destination),
        "output": relative(destination),
        "duration": round(info["duration"], 4),
        "width": info["width"],
        "height": info["height"],
        "fps": round(info["fps"], 4),
        "captions_burned": True,
        "caption_text_sha256": sha256_bytes(
            caption_text.encode("utf-8")
        ),
        "caption_language": language,
        "caption_style": style,
        "caption_timing": timing_method,
        "caption_cue_count": len(cues),
        "fingerprint": signature,
        "signature": signature,
        "sha256": checksum,
        "cache_reused": reused,
    })

    return result


def main():
    for command in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(command):
            raise RuntimeError(
                f"Required command not installed: {command}"
            )

    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    configured_parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    fps = int(get_fps(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    if configured_parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    if not 1 <= fps <= 60:
        raise RuntimeError("FPS must be between 1 and 60")

    width, height = (
        (720, 1280) if mode == "short"
        else (1280, 720)
    )

    render_data = read_json(RENDER_MANIFEST)

    if str(render_data.get("format", "")).lower() != mode:
        raise RuntimeError(
            "Render manifest FORMAT differs from Input/topic.txt"
        )

    if mode == "full":
        parts = configured_parts
    else:
        parts = int(
            render_data.get(
                "total_parts",
                render_data.get("parts_count", 0),
            )
        )

    if parts < 1:
        raise RuntimeError("Render manifest has invalid actual part count")

    render_by_id, ordered = load_render_scenes(
        render_data,
        parts,
        scenes,
        width,
        height,
        fps,
    )

    narration_by_id, narration_data = load_audio_map()

    if set(render_by_id) != set(narration_by_id):
        raise RuntimeError(
            "Rendered scenes and narration scene IDs differ; "
            f"missing_audio={sorted(set(render_by_id) - set(narration_by_id))[:20]}; "
            f"missing_video={sorted(set(narration_by_id) - set(render_by_id))[:20]}"
        )

    for sid, row in render_by_id.items():
        audio = narration_by_id[sid]

        if row["part"] != audio["part"]:
            raise RuntimeError(f"{sid}: narration part mismatch")

        if row["role"] != audio["role"]:
            raise RuntimeError(f"{sid}: narration role mismatch")

    captions, caption_data = load_caption_map(parts, scenes)

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
        raise RuntimeError("CAPTION_FONT_SIZE must be between 16 and 64")

    font_path = resolve_font(family)
    font_hash = digest_file(font_path)

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    CAPTION_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    alignment_cache = read_json(
        ALIGNMENT_CACHE,
        required=False,
        default={},
    )

    if not isinstance(alignment_cache, dict):
        alignment_cache = {}

    cached = load_progress()
    aligner = load_aligner()

    seed = digest_data({
        "captions": {
            f"{part}:{scene}": captions[(part, scene)]["sha256"]
            for part in range(1, parts + 1)
            for scene in range(1, scenes + 1)
        },
        "format": mode,
        "parts": parts,
        "scenes": scenes,
        "narration_source": narration_data.get("source_signature"),
    })

    completed = {}
    final_cues = []
    global_cursor = 0.0
    methods = {}
    reused_count = 0
    burned_count = 0
    aligned_count = 0
    fallback_count = 0

    write_json(PROGRESS_FILE, {
        "status": "in_progress",
        "version": VERSION,
        "format": mode,
        "total_parts": parts,
        "scenes_per_part": scenes,
        "total_scenes": len(ordered),
        "completed_count": 0,
        "scenes": cached,
    })

    log("CAPTION BURN VERSION:", VERSION)
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("MAIN SCENES:", parts * scenes)
    log("ALL SCENE ROLES:", len(ordered))
    log("FONT:", family, "SIZE:", size)
    log("FPS:", fps)

    for index, row in enumerate(ordered, 1):
        sid = row["scene_id"]
        narration = narration_by_id[sid]

        source = row["source_path"]
        source_info = media_info(source)

        if not source_info["has_video"] or not source_info["has_audio"]:
            raise RuntimeError(
                f"Rendered scene has no audio/video: {sid}"
            )

        if (source_info["width"], source_info["height"]) != (
            width, height
        ):
            raise RuntimeError(
                f"Rendered scene resolution mismatch: {sid}"
            )

        require_fps(source_info, fps, sid)

        caption_text, language, caption_hash = caption_for_scene(
            row,
            captions,
            narration,
        )

        cue_duration = min(
            source_info["duration"],
            narration["duration"],
        )

        if cue_duration <= 0:
            raise RuntimeError(f"Invalid caption duration: {sid}")

        alignment_key = digest_data({
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

        # A caption must not disappear if the audio ends slightly before video.
        if source_info["duration"] > cue_duration and cues:
            cues[-1]["end"] = min(
                source_info["duration"],
                max(cues[-1]["end"], cue_duration),
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
        )

        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", sid).strip("_")

        if not safe_id:
            raise RuntimeError(f"Unsafe scene_id for output filename: {sid}")

        destination = (
            CAPTIONED_DIR
            / f"part_{row['part']:02d}_{safe_id}.mp4"
        )

        signature = make_scene_signature(
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

        cache_key = sid
        previous = cached.get(cache_key, {})

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
                f"{sid} | {row['role']} | {timing_method}"
            )

        require_fps(info, fps, destination.name)

        output_hash = digest_file(destination)

        completed[sid] = make_captioned_row(
            row,
            narration,
            destination,
            info,
            output_hash,
            signature,
            caption_text,
            language,
            style,
            timing_method,
            cues,
            reused,
        )

        cached[cache_key] = {
            "status": "completed",
            "signature": signature,
            "sha256": output_hash,
            "path": relative(destination),
            "caption_style": style,
            "caption_timing": timing_method,
            "caption_alignment_key": alignment_key,
            "caption_cue_count": len(cues),
            "scene_id": sid,
            "role": row["role"],
            "part": row["part"],
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

        write_json(PROGRESS_FILE, {
            "status": "in_progress",
            "version": VERSION,
            "format": mode,
            "total_parts": parts,
            "scenes_per_part": scenes,
            "fps": fps,
            "completed_count": index,
            "total_scenes": len(ordered),
            "scenes": cached,
        })

    if len(completed) != len(ordered):
        raise RuntimeError(
            f"Captioned {len(completed)}/{len(ordered)} scene clips"
        )

    # Final SRT uses the same complete role-aware timeline as the video.
    write_srt(
        FINAL_SRT,
        sorted(final_cues, key=lambda item: item["start"]),
    )

    part_paths = []
    part_manifest = []

    for part in range(1, parts + 1):
        part_rows = [
            completed[row["scene_id"]]
            for row in ordered
            if row["part"] == part
        ]

        if not part_rows:
            raise RuntimeError(f"No captioned clips for part {part}")

        paths = [
            safe_file(
                row["video"],
                f"Captioned scene {row['scene_id']}",
                MIN_VIDEO_BYTES,
            )
            for row in part_rows
        ]

        part_path = PART_DIR / f"part_{part:02d}.mp4"

        part_info = concat_videos(
            paths,
            part_path,
            width,
            height,
            fps,
        )

        part_paths.append(part_path)

        part_manifest.append({
            "part": part,
            "status": "completed",
            "scene_count": sum(
                row["role"] == "main" for row in part_rows
            ),
            "total_clip_count": len(part_rows),
            "roles": [row["role"] for row in part_rows],
            "path": relative(part_path),
            "output": relative(part_path),
            "sha256": digest_file(part_path),
            "duration": round(part_info["duration"], 4),
            "fps": round(part_info["fps"], 4),
            "width": width,
            "height": height,
        })

        log("PART READY:", relative(part_path))

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"

    final_info = concat_videos(
        part_paths,
        final_path,
        width,
        height,
        fps,
    )

    alias_name = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    alias_path = VIDEO_DIR / alias_name
    temporary_alias = alias_path.with_name(
        alias_path.stem + ".tmp.mp4"
    )

    shutil.copy2(final_path, temporary_alias)
    os.replace(temporary_alias, alias_path)

    alias_info = media_info(alias_path)

    require_fps(final_info, fps, final_path.name)
    require_fps(alias_info, fps, alias_path.name)

    if digest_file(final_path) != digest_file(alias_path):
        raise RuntimeError("Final video and format alias differ")

    opposite = VIDEO_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    opposite.unlink(missing_ok=True)

    ordered_rows = [
        completed[row["scene_id"]]
        for row in ordered
    ]

    main_rows = [
        row for row in ordered_rows
        if row["role"] == "main"
    ]

    extra_rows = [
        row for row in ordered_rows
        if row["role"] != "main"
    ]

    expected_main_count = parts * scenes

    if len(main_rows) != expected_main_count:
        raise RuntimeError("Captioned main scene count mismatch")

    expected_extras = parts * 2

    if len(extra_rows) != expected_extras:
        raise RuntimeError("Captioned Hook/Suspense/Resolution count mismatch")

    # Caption consumers use the main-scene list; all role clips are
    # retained separately and in global timeline order.
    main_rows.sort(
        key=lambda row: (
            int(row["part"]),
            int(row["local_scene"]),
        )
    )

    final_manifest = {
        **render_data,
        "status": "completed",
        "version": VERSION,
        "caption_version": VERSION,
        "format": mode,
        "total_parts": parts,
        "parts_count": parts,
        "scenes_per_part": scenes,
        "fps": fps,
        "width": width,
        "height": height,
        "captions_burned": True,
        "caption_style": "adaptive-scene-contrast",
        "caption_timing": (
            "mixed-whisper-and-audio-estimate"
            if aligned_count and fallback_count
            else "whisper-word-alignment"
            if aligned_count
            else "audio-duration-fallback"
        ),
        "caption_timing_methods": methods,
        "caption_alignment_scenes": aligned_count,
        "caption_fallback_scenes": fallback_count,
        "caption_font": family,
        "caption_font_file": relative(font_path),
        "caption_font_sha256": font_hash,
        "caption_font_size": size,
        "expected_scene_count": expected_main_count,
        "completed_count": expected_main_count,
        "all_completed_scene_count": len(ordered_rows),
        "extra_scene_count": len(extra_rows),
        "completed_scenes": main_rows,
        "scenes": main_rows,
        "extra_scenes": extra_rows,
        "all_completed_scenes": ordered_rows,
        "parts": part_manifest,
        "final_video": relative(final_path),
        "final_video_sha256": digest_file(final_path),
        "final_video_fps": round(final_info["fps"], 4),
        "alias": relative(alias_path),
        "alias_sha256": digest_file(alias_path),
        "alias_fps": round(alias_info["fps"], 4),
        "duration": round(final_info["duration"], 4),
        "resolution": [width, height],
        "caption_manifest": relative(CAPTION_MANIFEST),
        "final_srt": relative(FINAL_SRT),
        "final_srt_sha256": digest_file(FINAL_SRT),
        "captioned_new": burned_count,
        "captioned_reused": reused_count,
        "hook_count": sum(
            row["role"] == "hook" for row in extra_rows
        ),
        "suspense_count": sum(
            row["role"] == "suspense" for row in extra_rows
        ),
        "resolution_count": sum(
            row["role"] == "resolution" for row in extra_rows
        ),
        "caption_timeline": {
            "includes_hook": True,
            "includes_main": True,
            "includes_suspense": True,
            "includes_resolution": True,
            "global_srt": True,
            "scene_order": "hook-main-suspense-per-part-final-resolution",
        },
    }

    write_json(RENDER_MANIFEST, final_manifest)

    write_json(PROGRESS_FILE, {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "total_parts": parts,
        "scenes_per_part": scenes,
        "fps": fps,
        "completed_count": len(ordered_rows),
        "total_scenes": len(ordered_rows),
        "final_video": relative(final_path),
        "final_video_sha256": digest_file(final_path),
        "alignment_scenes": aligned_count,
        "fallback_scenes": fallback_count,
        "scenes": cached,
    })

    log("=" * 60)
    log("CAPTION BURN COMPLETED")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("MAIN SCENES:", expected_main_count)
    log("ALL SCENE CLIPS:", len(ordered_rows))
    log("FPS:", fps)
    log("NEW:", burned_count, "| REUSED:", reused_count)
    log("WORD-ALIGNED:", aligned_count)
    log("FALLBACK:", fallback_count)
    log("TIMING METHODS:", methods)
    log("FINAL VIDEO:", relative(final_path))
    log("FINAL SRT:", relative(FINAL_SRT))
    log("MANIFEST:", relative(RENDER_MANIFEST))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("CAPTION BURN CANCELLED; completed scene checkpoints retained.")
        sys.exit(130)
    except Exception as exc:
        log("CAPTION BURN FAILED:", str(exc))
        sys.exit(1)