#!/usr/bin/env python3
"""Validate, time, burn and checkpoint adaptive captions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
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
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
CAPTION_MANIFEST = CAPTION_DIR / "captions.json"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
PROGRESS_FILE = CAPTION_DIR / "burn_progress.json"
FINAL_SRT = CAPTION_DIR / "final_captions.srt"
PART_DIR = OUT / "parts"
VIDEO_DIR = OUT / "videos"

VERSION = "burn-captions-v29-fps-enforced"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.25
FPS_TOLERANCE = 0.02
EXPECTED_FPS = 24

PALETTES = [
    {"name": "cinematic-white", "light": "&H00FFFFFF", "dark": "&H00141414"},
    {"name": "warm-gold", "light": "&H0000D7FF", "dark": "&H00202020"},
    {"name": "soft-cream", "light": "&H00E8F1FF", "dark": "&H00202020"},
    {"name": "mint-accent", "light": "&H0098FFD8", "dark": "&H00141414"},
]


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def digest_data(data):
    raw = json.dumps(
        data,
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
            raise RuntimeError(f"Required manifest missing: {path}")
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
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing file path")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path is outside project directory")

    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")

    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file is too small: {path.name}")

    return path


def run(command, label, timeout=900):
    result = subprocess.run(
        [str(item) for item in command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode:
        output = (result.stderr or result.stdout or "")[-3000:]
        raise RuntimeError(f"{label} failed: {output}")

    return result


def parse_fps(value, label):
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, TypeError):
        raise RuntimeError(f"Invalid FPS in {label}: {value!r}")

    if not math.isfinite(rate) or rate <= 0:
        raise RuntimeError(f"Invalid FPS in {label}: {value!r}")

    return rate


def require_fps(info, label):
    actual = float(info.get("fps", 0))

    if actual <= 0 or abs(actual - EXPECTED_FPS) > FPS_TOLERANCE:
        raise RuntimeError(
            f"FPS mismatch in {label}: expected {EXPECTED_FPS}, "
            f"got {actual:.4f}"
        )


def media_info(path):
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            (
                "format=duration:"
                "stream=codec_type,width,height,"
                "avg_frame_rate,r_frame_rate"
            ),
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
        raise RuntimeError(f"Invalid media information: {path}") from exc

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

    rate_value = None
    if video:
        rate_value = video.get("avg_frame_rate")
        if not rate_value or rate_value in ("0/0", "N/A"):
            rate_value = video.get("r_frame_rate")

    fps = parse_fps(rate_value, Path(path).name) if rate_value else 0.0

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "fps": fps,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


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


def load_caption_map(parts, scenes):
    data = read_json(CAPTION_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is missing or incomplete")

    mapping = {}

    for row in rows_from(data, "captions", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption manifest row")

        try:
            key = (
                int(row["part"]),
                int(row.get("scene", row.get("local_scene", 0))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid caption scene mapping") from exc

        text = row.get(
            "text",
            row.get("caption", row.get("caption_text", "")),
        )

        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Caption text is empty for scene {key}")

        if key in mapping:
            raise RuntimeError(f"Duplicate caption scene: {key}")

        mapping[key] = re.sub(r"\s+", " ", text).strip()

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    if set(mapping) != expected:
        raise RuntimeError(
            "Caption scene mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return mapping


def validate_chunk_records(raw_chunks, key, audio_duration):
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise RuntimeError(f"TTS chunk records missing for scene {key}")

    chunks = []

    for position, chunk in enumerate(raw_chunks, 1):
        if not isinstance(chunk, dict):
            raise RuntimeError(f"Invalid TTS chunk record for scene {key}")

        try:
            index = int(chunk["index"])
            duration = float(chunk["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid TTS chunk timing for scene {key}"
            ) from exc

        if index != position:
            raise RuntimeError(f"TTS chunk order is invalid for scene {key}")

        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(f"Invalid TTS chunk duration for scene {key}")

        chunk_text = chunk.get("text")

        if not isinstance(chunk_text, str) or not chunk_text.strip():
            raise RuntimeError(f"TTS chunk text missing for scene {key}")

        chunk_text = re.sub(r"\s+", " ", chunk_text).strip()

        declared_text_hash = chunk.get("text_sha256")

        if declared_text_hash and declared_text_hash != sha256_text(chunk_text):
            raise RuntimeError(
                f"TTS chunk text checksum mismatch for scene {key}"
            )

        status = str(chunk.get("status", "")).strip().lower()

        if status and status not in {"completed", "success", "ok"}:
            raise RuntimeError(
                f"TTS chunk has invalid status {status!r}: {key}"
            )

        chunks.append({
            "index": index,
            "duration": duration,
            "text": chunk_text,
            "sha256": chunk.get("sha256"),
            "output": chunk.get("output"),
            "provider": chunk.get("provider"),
        })

    total = sum(item["duration"] for item in chunks)

    if total <= 0:
        raise RuntimeError(f"TTS chunk duration total is invalid: {key}")

    scale = audio_duration / total

    for item in chunks:
        item["duration"] *= scale

    return chunks


def load_audio_map(parts, scenes):
    data = read_json(AUDIO_MANIFEST)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Narration audio manifest is missing or incomplete")

    mapping = {}

    for row in rows_from(data, "jobs", "audio_jobs", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid narration job row")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid narration scene mapping") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")

        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration is incomplete for scene {key}")

        audio_path = safe_file(
            first_path(row, ("output", "audio_path", "path", "file", "audio")),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(audio_path)

        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {key}")

        chunks = validate_chunk_records(
            row.get("chunks"),
            key,
            info["duration"],
        )

        mapping[key] = {
            "path": audio_path,
            "duration": info["duration"],
            "sha256": sha256_file(audio_path),
            "chunks": chunks,
        }

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    if set(mapping) != expected:
        raise RuntimeError(
            "Narration scene mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return mapping


def split_phrases(text):
    text = re.sub(r"\s+", " ", str(text)).strip()

    if not text:
        return []

    sentences = re.split(r"(?<=[.!?।…])\s+", text)
    phrases = []

    for sentence in sentences:
        words = sentence.split()
        current = []

        for word in words:
            candidate = " ".join(current + [word])

            if current and (len(candidate) > 38 or len(current) >= 7):
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

            if len(candidate) <= 44:
                merged[-1] = candidate
                continue

        merged.append(phrase)

    return merged or [text]


def allocate_phrases(phrases, weights, start, end):
    total = sum(weights)

    if total <= 0 or end <= start:
        return []

    result = []
    cursor = start

    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        next_cursor = (
            end
            if index == len(phrases) - 1
            else cursor + (end - start) * weight / total
        )

        result.append({
            "start": cursor,
            "end": next_cursor,
            "text": phrase,
        })

        cursor = next_cursor

    return result


def make_chunk_cues(caption_text, chunks, duration):
    """Map caption phrases to measured TTS chunk durations.

    This is chunk-level timing, not forced word-level alignment.
    """
    phrases = split_phrases(caption_text)

    if not phrases or duration <= 0:
        raise RuntimeError("Cannot time empty captions")

    chunk_total = sum(item["duration"] for item in chunks)

    if chunk_total <= 0:
        raise RuntimeError("Invalid total TTS chunk duration")

    phrase_weights = [max(1, len(item.split())) for item in phrases]
    total_words = sum(phrase_weights)

    result = []
    phrase_cursor = 0
    time_cursor = 0.0

    for chunk_index, chunk in enumerate(chunks):
        chunk_start = time_cursor

        chunk_end = (
            duration
            if chunk_index == len(chunks) - 1
            else min(
                duration,
                time_cursor + duration * chunk["duration"] / chunk_total,
            )
        )

        time_cursor = chunk_end

        if phrase_cursor >= len(phrases):
            break

        if chunk_index == len(chunks) - 1:
            next_cursor = len(phrases)
        else:
            target_words = (
                total_words * chunk["duration"] / chunk_total
            )

            accumulated = 0
            next_cursor = phrase_cursor

            while next_cursor < len(phrases):
                if (
                    next_cursor > phrase_cursor
                    and accumulated >= target_words
                ):
                    break

                accumulated += phrase_weights[next_cursor]
                next_cursor += 1

            next_cursor = max(phrase_cursor + 1, next_cursor)

        selected = phrases[phrase_cursor:next_cursor]
        weights = phrase_weights[phrase_cursor:next_cursor]

        result.extend(
            allocate_phrases(
                selected,
                weights,
                chunk_start,
                chunk_end,
            )
        )

        phrase_cursor = next_cursor

    if phrase_cursor < len(phrases):
        if not result:
            result = allocate_phrases(
                phrases,
                phrase_weights,
                0,
                duration,
            )
        else:
            result[-1]["text"] += " " + " ".join(phrases[phrase_cursor:])

    if not result:
        raise RuntimeError("No caption cues could be generated")

    clean = []

    for index, cue in enumerate(result):
        start = max(0.0, min(float(cue["start"]), duration))
        end = max(start, min(float(cue["end"]), duration))

        if index + 1 < len(result):
            next_start = float(result[index + 1]["start"])
            end = min(end, next_start)

        if end - start < 0.10:
            end = min(duration, start + 0.10)

        if end > start:
            clean.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": cue["text"],
            })

    if not clean:
        raise RuntimeError("Caption cues have no valid time ranges")

    return clean


def silence_boundaries(audio_path, duration):
    if duration <= 0:
        return []

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

        log_text = (result.stderr or "") + "\n" + (result.stdout or "")

        starts = [
            float(value)
            for value in re.findall(
                r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)",
                log_text,
            )
        ]

        ends = [
            float(value)
            for value in re.findall(
                r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)",
                log_text,
            )
        ]

        points = []

        for start, end in zip(starts, ends):
            if end > start:
                midpoint = (start + end) / 2

                if 0.05 < midpoint < duration - 0.05:
                    points.append(midpoint)

        return sorted(set(round(item, 3) for item in points))

    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def refine_with_silence(cues, audio_path, duration):
    boundaries = silence_boundaries(audio_path, duration)

    if not boundaries:
        return cues, "measured-chunk-duration-estimate"

    refined = []

    for index, cue in enumerate(cues):
        start = cue["start"]
        end = cue["end"]

        candidates = [
            point
            for point in boundaries
            if abs(point - end) <= 0.45
            and point > start + 0.12
            and point < duration - 0.05
        ]

        if candidates:
            end = min(candidates, key=lambda point: abs(point - end))

        if index + 1 < len(cues):
            end = min(end, cues[index + 1]["start"])

        if end - start < 0.10:
            end = min(duration, start + 0.10)

        if end > start:
            refined.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": cue["text"],
            })

    if not refined:
        return cues, "measured-chunk-duration-estimate"

    return refined, "chunk-duration-and-silence-assisted-estimate"


def srt_time(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)

    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


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


def resolve_font(family):
    result = run(
        ["fc-match", "-f", "%{file}\n", family],
        f"Resolve caption font {family}",
        timeout=60,
    )

    candidates = result.stdout.strip().splitlines()

    if not candidates:
        raise RuntimeError(f"Font not found: {family}")

    path = Path(candidates[0]).resolve()

    if not path.is_file():
        raise RuntimeError(f"Font file missing: {path}")

    return path


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

def contrast_style(video, duration, part, scene, story_seed, width, height):
    samples = [
        sample_luma(video, duration * fraction)
        for fraction in (0.18, 0.50, 0.82)
    ]

    average = sum(samples) / len(samples)

    seed = hashlib.sha256(
        f"{story_seed}:{part}:{scene}".encode("utf-8")
    ).digest()

    palette = PALETTES[seed[0] % len(PALETTES)]

    if average >= 145:
        primary = palette["dark"]
        outline = "&H00FFFFFF"
        mode = "light-background"
    else:
        primary = palette["light"]
        outline = "&H00101010"
        mode = "dark-background"

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
        f"FontName={family},"
        f"FontSize={int(size)},"
        f"PrimaryColour={style['primary']},"
        f"OutlineColour={style['outline']},"
        "BackColour=&H00000000,"
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
    video_duration,
    font_path,
    family,
    size,
    style,
    width,
    height,
):
    destination.parent.mkdir(parents=True, exist_ok=True)

    temp_srt = destination.with_suffix(".srt")
    temp_video = destination.with_name(destination.stem + ".tmp.mp4")
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
                "-r", str(EXPECTED_FPS),
                "-fps_mode", "cfr",
                "-c:a", "aac",
                "-b:a", "160k",
                "-ar", "44100",
                "-af", "aresample=async=1:first_pts=0",
                "-t", f"{video_duration:.3f}",
                "-movflags", "+faststart",
                str(temp_video),
            ],
            f"Burn captions into {destination.name}",
            timeout=900,
        )

        require = media_info(temp_video)

        if not require["has_video"] or not require["has_audio"]:
            raise RuntimeError(f"Captioned file lacks audio/video: {destination}")

        if (require["width"], require["height"]) != (width, height):
            raise RuntimeError(f"Captioned dimensions mismatch: {destination}")

        require_fps(require, destination.name)

        if abs(require["duration"] - video_duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Captioned duration mismatch: {destination}")

        os.replace(temp_video, destination)
        return media_info(destination)

    finally:
        temp_srt.unlink(missing_ok=True)
        temp_video.unlink(missing_ok=True)


def concat_videos(paths, destination, width, height):
    if not paths:
        raise RuntimeError(f"No videos supplied for {destination.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)

    list_file = destination.with_suffix(".concat.txt")
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    try:
        lines = []

        for path in paths:
            info = media_info(path)

            if not info["has_video"] or not info["has_audio"]:
                raise RuntimeError(f"Cannot merge incomplete video: {path}")

            if (info["width"], info["height"]) != (width, height):
                raise RuntimeError(f"Wrong resolution in merge input: {path}")

            require_fps(info, path.name)

            resolved = str(path.resolve())

            if "\n" in resolved or "\r" in resolved:
                raise RuntimeError("Invalid newline in video path")

            lines.append("file '" + resolved.replace("'", "'\\''") + "'")

        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.unlink(missing_ok=True)

        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-r", str(EXPECTED_FPS),
                "-fps_mode", "cfr",
                "-c:a", "aac",
                "-b:a", "160k",
                "-movflags", "+faststart",
                str(temporary),
            ],
            f"Merge {destination.name}",
            timeout=1800,
        )

        info = media_info(temporary)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Merged file lacks audio/video: {destination}")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Merged dimensions mismatch: {destination}")

        require_fps(info, destination.name)

        os.replace(temporary, destination)
        return media_info(destination)

    finally:
        list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def load_render_map(manifest, parts, scenes):
    if not isinstance(manifest, dict):
        raise RuntimeError("Render manifest is invalid")

    if manifest.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")

    rows = rows_from(manifest, "completed_scenes", "scenes")
    expected_count = parts * scenes

    if len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; found {len(rows)}"
        )

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render row: {index}")

        try:
            part = int(row["part"])
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid render mapping at row {index}"
            ) from exc

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1

        if (part, scene, global_scene) != (
            expected_part,
            expected_scene,
            index,
        ):
            raise RuntimeError(f"Render scene order mismatch at row {index}")

        source = safe_file(
            first_path(
                row,
                ("source_rendered_video", "source_video", "video", "output"),
            ),
            f"Rendered source {(part, scene)}",
            MIN_VIDEO_BYTES,
        )

        declared_hash = row.get("sha256")

        if declared_hash and row.get("video") == row.get("source_rendered_video"):
            if declared_hash != sha256_file(source):
                raise RuntimeError(f"Render source hash mismatch: {(part, scene)}")

        mapping[(part, scene)] = {
            **row,
            "source_path": source,
        }

    return mapping


def load_progress():
    data = read_json(PROGRESS_FILE, required=False, default={})

    if not isinstance(data, dict):
        return {}

    rows = data.get("scenes", {})
    return rows if isinstance(rows, dict) else {}


def valid_cached_caption(
    destination,
    previous,
    signature,
    width,
    height,
    expected_duration,
):
    if not destination.is_file() or not isinstance(previous, dict):
        return False

    if previous.get("signature") != signature:
        return False

    try:
        actual_hash = sha256_file(destination)

        if previous.get("sha256") != actual_hash:
            return False

        info = media_info(destination)

        if not info["has_video"] or not info["has_audio"]:
            return False

        if (info["width"], info["height"]) != (width, height):
            return False

        if abs(info["duration"] - expected_duration) > DURATION_TOLERANCE:
            return False

        require_fps(info, destination.name)
        return True

    except Exception:
        return False


def main():
    global EXPECTED_FPS

    for command in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    EXPECTED_FPS = int(get_fps(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    width, height = (
        (720, 1280)
        if mode == "short"
        else (1280, 720)
    )

    expected_count = parts * scenes

    family = get_caption_font(config)
    configured_size = get_caption_font_size(config)

    if str(configured_size).strip().lower() == "auto":
        size = 24 if mode == "short" else 28
    else:
        try:
            size = int(configured_size)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("CAPTION_FONT_SIZE must be auto or an integer") from exc

    size = max(16, min(size, 28 if mode == "short" else 32))

    font_path = resolve_font(family)
    font_hash = sha256_file(font_path)

    captions = load_caption_map(parts, scenes)
    audio = load_audio_map(parts, scenes)

    manifest = read_json(RENDER_MANIFEST)

    if str(manifest.get("format", "")).lower() != mode:
        raise RuntimeError("Render manifest FORMAT differs from Input/topic.txt")

    if int(manifest.get("width", width)) != width:
        raise RuntimeError("Render manifest width mismatch")

    if int(manifest.get("height", height)) != height:
        raise RuntimeError("Render manifest height mismatch")

    try:
        source_manifest_fps = float(manifest.get("fps"))
    except (TypeError, ValueError):
        raise RuntimeError("Render manifest FPS missing or invalid")

    if abs(source_manifest_fps - EXPECTED_FPS) > FPS_TOLERANCE:
        raise RuntimeError(
            f"Render manifest FPS mismatch: expected {EXPECTED_FPS}, "
            f"got {source_manifest_fps}"
        )

    source_map = load_render_map(manifest, parts, scenes)
    story_seed = digest_data({
        "captions": captions,
        "format": mode,
        "parts": parts,
        "scenes": scenes,
    })

    cached = load_progress()

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    CAPTION_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    completed = {}
    final_srt_cues = []
    global_cursor = 0.0
    timing_methods = {}
    reused_count = 0
    burned_count = 0

    log("CAPTION BURN VERSION:", VERSION)
    log("FORMAT:", mode)
    log("SCENES:", expected_count)
    log("RESOLUTION:", f"{width}x{height}")
    log("EXPECTED FPS:", EXPECTED_FPS)
    log("FONT:", family, "| SIZE:", size)
    log("TIMING: TTS chunk-duration mapping with optional silence refinement")
    log("LIMITATION: This is not forced word-level audio alignment")

    for index in range(1, expected_count + 1):
        part = (index - 1) // scenes + 1
        scene = (index - 1) % scenes + 1
        key = (part, scene)
        cache_key = f"{part}:{scene}"

        row = source_map[key]
        source = row["source_path"]
        source_info = media_info(source)

        if not source_info["has_video"] or not source_info["has_audio"]:
            raise RuntimeError(f"Rendered source lacks audio/video: {key}")

        if (source_info["width"], source_info["height"]) != (width, height):
            raise RuntimeError(f"Rendered source dimensions mismatch: {key}")

        require_fps(source_info, f"rendered scene {key}")

        narration = audio[key]
        video_duration = source_info["duration"]
        audio_duration = narration["duration"]
        chunks = narration["chunks"]

        cues = make_chunk_cues(
            captions[key],
            chunks,
            min(audio_duration, video_duration),
        )

        cues, timing_method = refine_with_silence(
            cues,
            narration["path"],
            min(audio_duration, video_duration),
        )

        style = contrast_style(
            source,
            video_duration,
            part,
            scene,
            story_seed,
            width,
            height,
        )

        destination = (
            CAPTIONED_DIR / f"part_{part:02d}_scene_{scene:03d}.mp4"
        )

        source_hash = sha256_file(source)

        signature = digest_data({
            "version": VERSION,
            "source_sha256": source_hash,
            "audio_sha256": narration["sha256"],
            "caption": captions[key],
            "font": family,
            "font_sha256": font_hash,
            "font_size": size,
            "expected_fps": EXPECTED_FPS,
            "video_duration": round(video_duration, 4),
            "audio_duration": round(audio_duration, 4),
            "chunks": [
                {
                    "index": item["index"],
                    "duration": round(item["duration"], 5),
                    "text": item["text"],
                    "text_sha256": sha256_text(item["text"]),
                }
                for item in chunks
            ],
            "style": style,
        })

        previous = cached.get(cache_key, {})

        reused = valid_cached_caption(
            destination,
            previous,
            signature,
            width,
            height,
            video_duration,
        )

        if reused:
            result_info = media_info(destination)
            require_fps(result_info, destination.name)
            reused_count += 1
            log(f"REUSED {index}/{expected_count}: {destination.name}")

        else:
            result_info = burn_one(
                source,
                destination,
                cues,
                video_duration,
                font_path,
                family,
                size,
                style,
                width,
                height,
            )

            burned_count += 1

            log(
                f"BURNED {index}/{expected_count}: {destination.name}; "
                f"{style['palette']}/{style['mode']}; {timing_method}"
            )

        require_fps(result_info, destination.name)

        timing_methods[timing_method] = (
            timing_methods.get(timing_method, 0) + 1
        )

        output_hash = sha256_file(destination)

        completed[key] = {
            **row,
            "part": part,
            "scene": scene,
            "local_scene": scene,
            "global_scene": index,
            "status": "completed",
            "source_rendered_video": relative(source),
            "video": relative(destination),
            "output": relative(destination),
            "duration": round(result_info["duration"], 4),
            "width": width,
            "height": height,
            "fps": round(result_info["fps"], 4),
            "captions_burned": True,
            "caption_style": style,
            "caption_timing": timing_method,
            "caption_chunk_count": len(chunks),
            "caption_cue_count": len(cues),
            "fingerprint": signature,
            "signature": signature,
            "sha256": output_hash,
            "cache_reused": reused,
        }

        cached[cache_key] = {
            "status": "completed",
            "signature": signature,
            "sha256": output_hash,
            "path": relative(destination),
            "fps": round(result_info["fps"], 4),
            "caption_style": style,
            "caption_timing": timing_method,
            "caption_chunk_count": len(chunks),
            "caption_cue_count": len(cues),
        }

        for cue in cues:
            final_srt_cues.append({
                "start": global_cursor + cue["start"],
                "end": global_cursor + cue["end"],
                "text": cue["text"],
            })

        global_cursor += result_info["duration"]

        write_json(PROGRESS_FILE, {
            "status": "in_progress",
            "version": VERSION,
            "format": mode,
            "fps": EXPECTED_FPS,
            "completed_count": index,
            "total_scenes": expected_count,
            "scenes": cached,
        })

    if len(completed) != expected_count:
        raise RuntimeError("Captioned scene count mismatch")

    ordered_cues = sorted(
        final_srt_cues,
        key=lambda item: (item["start"], item["end"]),
    )

    write_srt(FINAL_SRT, ordered_cues)

    part_paths = []

    for part in range(1, parts + 1):
        scene_paths = [
            ROOT / completed[(part, scene)]["video"]
            for scene in range(1, scenes + 1)
        ]

        part_path = PART_DIR / f"part_{part:02d}.mp4"

        concat_videos(
            scene_paths,
            part_path,
            width,
            height,
        )

        part_paths.append(part_path)
        log("PART READY:", relative(part_path))

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"

    final_info = concat_videos(
        part_paths,
        final_path,
        width,
        height,
    )

    require_fps(final_info, final_path.name)

    alias_name = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    alias_path = VIDEO_DIR / alias_name
    shutil.copy2(final_path, alias_path)

    alias_info = media_info(alias_path)
    require_fps(alias_info, alias_path.name)

    opposite = VIDEO_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    opposite.unlink(missing_ok=True)

    ordered_rows = [
        completed[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]

    final_manifest = {
        **manifest,
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "fps": EXPECTED_FPS,
        "width": width,
        "height": height,
        "captions_burned": True,
        "caption_style": "story-scene-adaptive-contrast",
        "caption_timing": (
            "chunk-duration-and-silence-assisted-estimate"
            if any("silence-assisted" in key for key in timing_methods)
            else "measured-chunk-duration-estimate"
        ),
        "caption_timing_methods": timing_methods,
        "caption_font": family,
        "caption_font_file": str(font_path),
        "caption_font_sha256": font_hash,
        "caption_font_size": size,
        "expected_scene_count": expected_count,
        "completed_count": expected_count,
        "completed_scenes": ordered_rows,
        "scenes": ordered_rows,
        "parts": [
            {
                "part": index,
                "status": "completed",
                "scene_count": scenes,
                "path": relative(path),
                "output": relative(path),
                "sha256": sha256_file(path),
                "duration": round(media_info(path)["duration"], 4),
                "fps": round(media_info(path)["fps"], 4),
            }
            for index, path in enumerate(part_paths, 1)
        ],
        "final_video": relative(final_path),
        "final_video_sha256": sha256_file(final_path),
        "final_video_fps": round(final_info["fps"], 4),
        "alias": relative(alias_path),
        "alias_sha256": sha256_file(alias_path),
        "alias_fps": round(alias_info["fps"], 4),
        "duration": round(final_info["duration"], 4),
        "resolution": [width, height],
        "caption_manifest": relative(CAPTION_MANIFEST),
        "final_srt": relative(FINAL_SRT),
        "final_srt_sha256": sha256_file(FINAL_SRT),
        "captioned_new": burned_count,
        "captioned_reused": reused_count,
    }

    write_json(RENDER_MANIFEST, final_manifest)

    write_json(PROGRESS_FILE, {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "fps": EXPECTED_FPS,
        "completed_count": expected_count,
        "total_scenes": expected_count,
        "final_video": relative(final_path),
        "final_video_sha256": sha256_file(final_path),
        "scenes": cached,
    })

    log("CAPTION BURN COMPLETE")
    log("FORMAT:", mode)
    log("SCENES:", expected_count)
    log("FPS:", EXPECTED_FPS)
    log("NEW:", burned_count, "| REUSED:", reused_count)
    log("TIMING METHODS:", timing_methods)
    log("FINAL VIDEO:", relative(final_path))
    log("FINAL SRT:", relative(FINAL_SRT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION BURN FAILED:", str(exc))
        sys.exit(1)