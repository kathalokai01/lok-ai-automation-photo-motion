#!/usr/bin/env python3
"""Burn readable, scene-aware captions into rendered videos."""

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

VERSION = "burn-captions-v27-validated-chunk-timing"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.0

PALETTES = [
    {"name": "cinematic-white", "light": "&H00FFFFFF", "dark": "&H00141414"},
    {"name": "warm-gold", "light": "&H0000D7FF", "dark": "&H00202020"},
    {"name": "soft-cream", "light": "&H00E8F1FF", "dark": "&H00202020"},
    {"name": "mint-accent", "light": "&H0098FFD8", "dark": "&H00141414"},
]


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_data(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
        raise RuntimeError(
            f"{label} failed: {(result.stderr or result.stdout or '')[-3000:]}"
        )
    return result


def media_info(path):
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        f"Inspect {Path(path).name}",
        timeout=90,
    )
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media information: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    streams = data.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"), None
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"), None
    )
    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def list_rows(data, *keys):
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
    for row in list_rows(data, "captions", "scenes"):
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row.get("scene", row.get("local_scene", 0))))
        except (KeyError, TypeError, ValueError):
            continue

        text = row.get("text", row.get("caption", row.get("caption_text", "")))
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


def load_audio_map(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Narration audio manifest is missing or incomplete")

    mapping = {}
    for row in list_rows(data, "jobs", "audio_jobs", "scenes"):
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

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

        chunks = []
        raw_chunks = row.get("chunks", [])
        if not isinstance(raw_chunks, list):
            raise RuntimeError(f"Invalid chunk manifest for scene {key}")

        for chunk in raw_chunks:
            if not isinstance(chunk, dict):
                raise RuntimeError(f"Invalid chunk record for scene {key}")
            if str(chunk.get("status", "")).lower() != "completed":
                raise RuntimeError(f"Incomplete TTS chunk for scene {key}")

            try:
                index = int(chunk["index"])
                duration = float(chunk["duration"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid TTS chunk timing for scene {key}"
                ) from exc

            if index < 1 or not math.isfinite(duration) or duration <= 0:
                raise RuntimeError(f"Invalid TTS chunk timing for scene {key}")

            chunk_text = str(chunk.get("text", "")).strip()
            if not chunk_text:
                raise RuntimeError(f"TTS chunk text missing for scene {key}")

            chunks.append({
                "index": index,
                "duration": duration,
                "text": chunk_text,
            })

        chunks.sort(key=lambda item: item["index"])
        if chunks:
            indexes = [item["index"] for item in chunks]
            if indexes != list(range(1, len(chunks) + 1)):
                raise RuntimeError(f"TTS chunk order is invalid for scene {key}")

            total = sum(item["duration"] for item in chunks)
            scale = info["duration"] / total
            for item in chunks:
                item["duration"] *= scale

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


def silence_boundaries(audio_path, duration):
    if duration <= 0:
        return []

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats",
                "-i", str(audio_path), "-vn",
                "-af", "silencedetect=noise=-35dB:d=0.18",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        log_text = (result.stderr or "") + "\n" + (result.stdout or "")
        if result.returncode:
            return []

        starts = [
            float(value) for value in re.findall(
                r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", log_text
            )
        ]
        ends = [
            float(value) for value in re.findall(
                r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", log_text
            )
        ]

        points = []
        for start, end in zip(starts, ends):
            if end > start:
                midpoint = (start + end) / 2
                if 0.05 < midpoint < duration - 0.05:
                    points.append(midpoint)

        if len(starts) > len(ends) and starts[-1] < duration:
            midpoint = (starts[-1] + duration) / 2
            if 0.05 < midpoint < duration - 0.05:
                points.append(midpoint)

        return sorted(set(round(point, 3) for point in points))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def allocate_phrases(phrases, weights, start, end):
    total = sum(weights)
    if total <= 0:
        return []

    result = []
    cursor = start
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


def make_cues(text, audio_duration, video_duration, audio_path, chunks):
    phrases = split_phrases(text)
    limit = min(float(audio_duration), float(video_duration))
    if not phrases or limit <= 0:
        raise RuntimeError("Cannot time captions for empty text or duration")

    weights = [max(1, len(phrase.split())) for phrase in phrases]
    raw_cues = []
    method = "phrase-proportional-estimate"

    usable_chunks = [
        item for item in (chunks or [])
        if isinstance(item, dict) and item.get("duration", 0) > 0
    ]

    if usable_chunks:
        method = "chunk-duration-proportional-estimate"
        total_chunk_duration = sum(item["duration"] for item in usable_chunks)
        total_words = sum(weights)
        phrase_index = 0
        elapsed = 0.0

        for chunk_index, chunk in enumerate(usable_chunks):
            chunk_duration = (
                limit * chunk["duration"] / total_chunk_duration
            )
            start = elapsed
            end = (
                limit if chunk_index == len(usable_chunks) - 1
                else min(limit, elapsed + chunk_duration)
            )
            elapsed = end

            if phrase_index >= len(phrases):
                break

            if chunk_index == len(usable_chunks) - 1:
                next_index = len(phrases)
            else:
                target_words = total_words * chunk["duration"] / total_chunk_duration
                accumulated = 0
                next_index = phrase_index

                while next_index < len(phrases):
                    if next_index > phrase_index and accumulated >= target_words:
                        break
                    accumulated += weights[next_index]
                    next_index += 1

                next_index = max(phrase_index + 1, next_index)

            selected_phrases = phrases[phrase_index:next_index]
            selected_weights = weights[phrase_index:next_index]
            raw_cues.extend(
                allocate_phrases(
                    selected_phrases, selected_weights, start, end
                )
            )
            phrase_index = next_index

        if phrase_index < len(phrases):
            if not raw_cues:
                raw_cues = allocate_phrases(phrases, weights, 0, limit)
            else:
                raw_cues[-1]["text"] += " " + " ".join(phrases[phrase_index:])
    else:
        raw_cues = allocate_phrases(phrases, weights, 0, limit)

    silences = silence_boundaries(audio_path, limit)
    cues = []

    for index, cue in enumerate(raw_cues):
        start = max(0.0, min(float(cue["start"]), limit))
        end = max(start, min(float(cue["end"]), limit))

        if silences:
            candidates = [
                point for point in silences
                if abs(point - end) <= 0.45
                and point > start + 0.12
                and point < limit - 0.05
            ]
            if candidates:
                proposed = min(candidates, key=lambda point: abs(point - end))
                next_start = (
                    float(raw_cues[index + 1]["start"])
                    if index + 1 < len(raw_cues) else limit
                )
                end = min(proposed, next_start)

        if end - start < 0.10:
            end = min(limit, start + 0.10)

        if end > start:
            cues.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": cue["text"],
            })

    if not cues:
        raise RuntimeError("No valid caption cues generated")

    if silences and usable_chunks:
        method = "chunk-duration-and-silence-assisted-estimate"
    elif silences:
        method = "silence-assisted-estimate"

    return cues, method


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
    if not shutil.which("fc-match"):
        raise RuntimeError("fontconfig fc-match is required")

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


def sample_luma(video, timestamp, width, height):
    region = (
        "crop=iw:ih*0.38:0:ih*0.62"
        if height > width
        else "crop=iw:ih*0.38:0:ih*0.62"
    )
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0, timestamp):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", f"{region},scale=1:1:flags=area,format=gray",
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
        sample_luma(video, duration * fraction, width, height)
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
        (":", r"\:"), ("'", r"\'"), (",", r"\,"),
        ("[", r"\["), ("]", r"\]"), (";", r"\;"),
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
        f"fontsdir='{font_directory}':force_style='{ass_style}'"
    )


def burn_one(
    source, destination, caption_text, video_duration, audio_duration,
    audio_path, chunks, font_path, family, size, style, width, height,
):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_srt = destination.with_suffix(".srt")
    temp_video = destination.with_name(destination.stem + ".tmp.mp4")
    temp_video.unlink(missing_ok=True)

    try:
        cues, timing_method = make_cues(
            caption_text, audio_duration, video_duration, audio_path, chunks
        )
        write_srt(temp_srt, cues)
        video_filter = caption_filter(
            temp_srt, font_path, family, size, style
        )

        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", video_filter,
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                "-af", "aresample=async=1:first_pts=0",
                "-t", f"{video_duration:.3f}",
                "-movflags", "+faststart", str(temp_video),
            ],
            f"Burn captions into {destination.name}",
            timeout=900,
        )

        info = media_info(temp_video)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Captioned file lacks audio/video: {destination}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Captioned file dimensions mismatch: {destination}")
        if abs(info["duration"] - video_duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Captioned file duration mismatch: {destination}")

        os.replace(temp_video, destination)
        return info, cues, timing_method
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

            resolved = str(path.resolve())
            if "\n" in resolved or "\r" in resolved:
                raise RuntimeError("Invalid newline in video path")
            lines.append("file '" + resolved.replace("'", "'\\''") + "'")

        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.unlink(missing_ok=True)

        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(temporary),
            ],
            f"Merge {destination.name}",
            timeout=1800,
        )

        info = media_info(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Merged file lacks audio/video: {destination}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Merged file dimensions mismatch: {destination}")

        os.replace(temporary, destination)
        return media_info(destination)
    finally:
        list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def load_render_map(manifest, parts, scenes):
    if not isinstance(manifest, dict):
        raise RuntimeError("Render manifest is invalid")
    if manifest.get("status") not in {"completed", "in_progress"}:
        raise RuntimeError("Render manifest is not complete or resumable")

    rows = list_rows(manifest, "completed_scenes", "scenes")
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
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1
        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
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
        mapping[(part, scene)] = {**row, "source_path": source}

    return mapping


def main():
    for command in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    width, height = (720, 1280) if mode == "short" else (1280, 720)
    expected_count = parts * scenes

    family = get_caption_font(config)
    configured_size = get_caption_font_size(config)
    if str(configured_size).strip().lower() == "auto":
        size = 24 if mode == "short" else 28
    else:
        size = int(configured_size)
    size = max(16, min(size, 28 if mode == "short" else 32))

    font_path = resolve_font(family)
    font_hash = sha256_file(font_path)

    captions = load_caption_map(parts, scenes)
    audio = load_audio_map(parts, scenes)
    manifest = read_json(RENDER_MANIFEST)
    source_map = load_render_map(manifest, parts, scenes)

    if str(manifest.get("format", "")).lower() != mode:
        raise RuntimeError("Render manifest FORMAT differs from Input/topic.txt")

    story_seed = digest_data({
        "captions": captions,
        "format": mode,
        "parts": parts,
        "scenes": scenes,
    })

    progress = read_json(PROGRESS_FILE, {})
    cached = progress.get("scenes", {}) if isinstance(progress, dict) else {}
    if not isinstance(cached, dict):
        cached = {}

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    completed = {}
    final_srt_cues = []
    global_cursor = 0.0
    timing_methods = {}
    reused_count = 0
    burned_count = 0

    log("CAPTION BURN VERSION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)
    log("RESOLUTION:", f"{width}x{height}")
    log("FONT:", family, "| SIZE:", size)
    log("TIMING: measured TTS chunk durations with silence refinement")
    log("NOTE: this is not forced word-level audio alignment")

    for index in range(1, expected_count + 1):
        part = (index - 1) // scenes + 1
        scene = (index - 1) % scenes + 1
        key = (part, scene)

        row = source_map[key]
        source = row["source_path"]
        source_info = media_info(source)

        if not source_info["has_video"] or not source_info["has_audio"]:
            raise RuntimeError(f"Rendered source lacks audio/video: {key}")
        if (source_info["width"], source_info["height"]) != (width, height):
            raise RuntimeError(
                f"Scene {key} dimensions are {source_info['width']}x"
                f"{source_info['height']}; expected {width}x{height}"
            )

        narration = audio[key]
        video_duration = source_info["duration"]
        audio_duration = narration["duration"]
        chunks = narration["chunks"]

        style = contrast_style(
            source, video_duration, part, scene,
            story_seed, width, height,
        )

        destination = (
            CAPTIONED_DIR / f"part_{part:02d}_scene_{scene:03d}.mp4"
        )

        signature = digest_data({
            "version": VERSION,
            "source_sha256": sha256_file(source),
            "audio_sha256": narration["sha256"],
            "caption": captions[key],
            "font": family,
            "font_sha256": font_hash,
            "font_size": size,
            "video_duration": round(video_duration, 4),
            "audio_duration": round(audio_duration, 4),
            "chunks": chunks,
            "style": style,
        })

        previous = cached.get(f"{part}:{scene}", {})
        reused = False

        if destination.is_file() and isinstance(previous, dict):
            try:
                existing = media_info(destination)
                reused = (
                    previous.get("signature") == signature
                    and previous.get("sha256") == sha256_file(destination)
                    and existing["has_video"]
                    and existing["has_audio"]
                    and (existing["width"], existing["height"]) == (width, height)
                    and abs(existing["duration"] - video_duration)
                    <= DURATION_TOLERANCE
                )
            except Exception:
                reused = False

        if reused:
            result_info = media_info(destination)
            cues, timing_method = make_cues(
                captions[key], audio_duration, video_duration,
                narration["path"], chunks,
            )
            reused_count += 1
            log(f"REUSED {index}/{expected_count}: {destination.name}")
        else:
            result_info, cues, timing_method = burn_one(
                source, destination, captions[key],
                video_duration, audio_duration, narration["path"],
                chunks, font_path, family, size, style, width, height,
            )
            burned_count += 1
            log(
                f"BURNED {index}/{expected_count}: {destination.name}; "
                f"{style['palette']}/{style['mode']}; {timing_method}"
            )

        timing_methods[timing_method] = timing_methods.get(timing_method, 0) + 1

        completed[key] = {
            **row,
            "part": part,
            "scene": scene,
            "local_scene": scene,
            "global_scene": index,
            "status": "completed",
            "source_rendered_video": relative(source),
            "video": relative(destination),
            "duration": round(result_info["duration"], 4),
            "captions_burned": True,
            "caption_style": style,
            "caption_timing": timing_method,
            "caption_chunk_count": len(chunks),
            "signature": signature,
            "sha256": sha256_file(destination),
            "cache_reused": reused,
        }

        cached[f"{part}:{scene}"] = {
            "signature": signature,
            "sha256": sha256_file(destination),
            "path": relative(destination),
            "caption_style": style,
            "caption_timing": timing_method,
            "caption_chunk_count": len(chunks),
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
            "completed_count": index,
            "total_scenes": expected_count,
            "scenes": cached,
        })

    write_srt(FINAL_SRT, final_srt_cues)

    part_paths = []
    for part in range(1, parts + 1):
        scene_paths = [
            ROOT / completed[(part, scene)]["video"]
            for scene in range(1, scenes + 1)
        ]
        part_path = PART_DIR / f"part_{part:02d}.mp4"
        concat_videos(scene_paths, part_path, width, height)
        part_paths.append(part_path)
        log("PART READY:", relative(part_path))

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(part_paths, final_path, width, height)

    alias_name = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    alias_path = VIDEO_DIR / alias_name
    shutil.copy2(final_path, alias_path)

    opposite = VIDEO_DIR / (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite.unlink(missing_ok=True)

    final_manifest = {
        **manifest,
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "captions_burned": True,
        "caption_style": "story-scene-adaptive-contrast",
        "caption_timing": (
            "chunk-duration-and-silence-assisted-estimate"
            if any("chunk-duration" in key for key in timing_methods)
            else "phrase-level-proportional-estimate"
        ),
        "caption_timing_methods": timing_methods,
        "caption_font": family,
        "caption_font_file": str(font_path),
        "caption_font_sha256": font_hash,
        "caption_font_size": size,
        "completed_count": expected_count,
        "completed_scenes": [
            completed[(part, scene)]
            for part in range(1, parts + 1)
            for scene in range(1, scenes + 1)
        ],
        "parts": [
            {
                "part": index,
                "status": "completed",
                "path": relative(path),
                "output": relative(path),
            }
            for index, path in enumerate(part_paths, 1)
        ],
        "final_video": relative(final_path),
        "alias": relative(alias_path),
        "duration": round(final_info["duration"], 4),
        "resolution": [width, height],
        "caption_manifest": relative(CAPTION_MANIFEST),
        "final_srt": relative(FINAL_SRT),
        "captioned_new": burned_count,
        "captioned_reused": reused_count,
    }

    write_json(RENDER_MANIFEST, final_manifest)
    write_json(PROGRESS_FILE, {
        "status": "completed",
        "version": VERSION,
        "completed_count": expected_count,
        "total_scenes": expected_count,
        "final_video": relative(final_path),
        "scenes": cached,
    })

    log("CAPTION BURN COMPLETE")
    log("FORMAT:", mode)
    log("SCENES:", expected_count)
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