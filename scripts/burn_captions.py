#!/usr/bin/env python3
"""Resume-safe captions with adaptive contrast and schema-compatible manifests."""

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
PART_DIR = OUT / "parts"
VIDEO_DIR = OUT / "videos"
CAPTION_DIR = OUT / "captions"

RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
CAPTION_MANIFEST = CAPTION_DIR / "captions.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
PROGRESS_FILE = CAPTION_DIR / "burn_progress.json"
FINAL_SRT = CAPTION_DIR / "final_captions.srt"

VERSION = "burn-captions-v22-adaptive-outline-resume"
WIDTH, HEIGHT = 1280, 720
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.0


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_data(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path is missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path is outside repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file is too small: {path.name}")

    return path


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def run(command, label, timeout=900):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{label} failed: {result.stderr[-2500:]}"
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
        raise RuntimeError(f"Invalid duration: {path}")

    streams = data.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def find_path(row, keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_font(family):
    if not shutil.which("fc-match"):
        raise RuntimeError("fontconfig fc-match is required")

    result = run(
        ["fc-match", "-f", "%{file}\n", family],
        f"Resolve font {family}",
        timeout=60,
    )
    candidates = result.stdout.strip().splitlines()
    if not candidates:
        raise RuntimeError(f"No font found for {family}")

    path = Path(candidates[0]).resolve()
    if not path.is_file():
        raise RuntimeError(f"Font file does not exist: {path}")
    return path


def load_caption_map(parts, scenes):
    data = read_json(CAPTION_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest missing or incomplete")

    mapping = {}
    for row in rows(data, "captions", "scenes"):
        if not isinstance(row, dict):
            continue
        try:
            key = (
                int(row["part"]),
                int(row.get("scene", row.get("local_scene", 0))),
            )
        except (KeyError, TypeError, ValueError):
            continue

        text = row.get(
            "text", row.get("caption", row.get("caption_text", ""))
        )
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Empty caption for scene {key}")
        if key in mapping:
            raise RuntimeError(f"Duplicate caption for scene {key}")
        mapping[key] = re.sub(r"\s+", " ", text).strip()

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }
    if set(mapping) != expected:
        raise RuntimeError(
            "Caption mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )
    return mapping


def load_audio_map(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Narration audio manifest is missing")

    mapping = {}
    for row in rows(data, "jobs", "audio_jobs", "scenes"):
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration is incomplete for {key}")

        path = safe_file(
            find_path(
                row, ("output", "audio_path", "path", "file", "audio")
            ),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {key}")

        mapping[key] = {
            "path": path,
            "duration": info["duration"],
            "sha256": digest_file(path),
        }

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }
    if set(mapping) != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )
    return mapping


def split_phrases(text):
    """Keep sentence boundaries where possible and limit caption length."""
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

    # Merge very short neighboring cues to avoid rapid caption flicker.
    merged = []
    for phrase in phrases:
        if merged and len(merged[-1].split()) <= 2:
            candidate = merged[-1] + " " + phrase
            if len(candidate) <= 44:
                merged[-1] = candidate
                continue
        merged.append(phrase)

    return merged or [text]


def make_cues(text, duration):
    """Estimated phrase timing; not word-level forced alignment."""
    phrases = split_phrases(text)
    if not phrases or duration <= 0:
        raise RuntimeError("Cannot create captions for empty text/duration")

    weights = [max(1, len(phrase.split())) for phrase in phrases]
    total = sum(weights)
    cues = []
    cursor = 0.0

    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        start = cursor
        end = (
            duration
            if index == len(phrases) - 1
            else cursor + duration * weight / total
        )
        cues.append({
            "start": start,
            "end": end,
            "text": phrase,
        })
        cursor = end

    return cues


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


def sample_luma(video, timestamp):
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0, timestamp):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", "scale=1:1:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
        timeout=90,
        check=False,
    )
    if result.returncode or not result.stdout:
        return 64
    return int(result.stdout[0])


def contrast_style(video, duration):
    samples = [
        sample_luma(video, duration * fraction)
        for fraction in (0.20, 0.50, 0.80)
    ]
    average = sum(samples) / len(samples)

    # Outline-first styling avoids the large solid black caption box.
    if average >= 145:
        return {
            "mode": "light-background",
            "average_luma": round(average, 2),
            "primary": "&H00141414",
            "outline": "&H00FFFFFF",
            "back": "&H00000000",
            "outline_width": 3,
            "shadow": 1,
        }

    return {
        "mode": "dark-background",
        "average_luma": round(average, 2),
        "primary": "&H00FFFFFF",
        "outline": "&H00101010",
        "back": "&H00000000",
        "outline_width": 3,
        "shadow": 1,
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


def caption_filter(srt_path, font_path, family, size, contrast):
    font_size = max(16, min(int(size), 32))
    margin_v = 42

    subtitle_file = escape_filter_path(srt_path)
    font_directory = escape_filter_path(font_path.parent)

    style = (
        f"FontName={family},"
        f"FontSize={font_size},"
        f"PrimaryColour={contrast['primary']},"
        f"OutlineColour={contrast['outline']},"
        f"BackColour={contrast['back']},"
        f"BorderStyle=1,"
        f"Outline={contrast['outline_width']},"
        f"Shadow={contrast['shadow']},"
        "Alignment=2,MarginL=48,MarginR=48,"
        f"MarginV={margin_v}"
    )

    return (
        f"subtitles=filename='{subtitle_file}':"
        f"fontsdir='{font_directory}':"
        f"force_style='{style}'"
    )


def burn_one(source, destination, text, duration, font_path, family, size, style):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_srt = destination.with_suffix(".srt")
    temporary_video = destination.with_name(destination.stem + ".tmp.mp4")
    temporary_video.unlink(missing_ok=True)

    try:
        cues = make_cues(text, duration)
        write_srt(temporary_srt, cues)
        vf = caption_filter(temporary_srt, font_path, family, size, style)

        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", vf,
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                "-af", "aresample=async=1:first_pts=0",
                "-t", f"{duration:.3f}",
                "-movflags", "+faststart",
                str(temporary_video),
            ],
            f"Burn captions into {destination.name}",
            timeout=900,
        )

        info = media_info(temporary_video)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Captioned output lacks audio/video: {destination}")
        if (info["width"], info["height"]) != (WIDTH, HEIGHT):
            raise RuntimeError(f"Captioned output has wrong dimensions: {destination}")
        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Captioned output duration mismatch: {destination}")

        os.replace(temporary_video, destination)
        return info, cues
    finally:
        temporary_srt.unlink(missing_ok=True)
        temporary_video.unlink(missing_ok=True)


def concat_videos(paths, destination):
    if not paths:
        raise RuntimeError(f"No videos to merge for {destination.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    list_file = destination.with_suffix(".concat.txt")
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    try:
        lines = []
        for path in paths:
            info = media_info(path)
            if not info["has_video"] or not info["has_audio"]:
                raise RuntimeError(f"Cannot merge incomplete video: {path}")
            if (info["width"], info["height"]) != (WIDTH, HEIGHT):
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
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(temporary),
            ],
            f"Merge {destination.name}",
            timeout=1800,
        )

        info = media_info(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Merged video lacks audio/video: {destination}")
        if (info["width"], info["height"]) != (WIDTH, HEIGHT):
            raise RuntimeError(f"Merged video has wrong dimensions: {destination}")

        os.replace(temporary, destination)
        return media_info(destination)
    finally:
        list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def main():
    for command in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    family = get_caption_font(config)
    configured_size = get_caption_font_size(config)
    size = 28 if str(configured_size).strip().lower() == "auto" else int(configured_size)
    size = max(16, min(size, 32))

    font_path = resolve_font(family)
    font_hash = digest_file(font_path)
    captions = load_caption_map(parts, scenes)
    audio = load_audio_map(parts, scenes)

    manifest = read_json(RENDER_MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("status") not in (
        "completed", "in_progress"
    ):
        raise RuntimeError("Render manifest is missing or not usable")
    if str(manifest.get("format", "")).lower() != mode:
        raise RuntimeError("Render manifest format does not match Input/topic.txt")

    source_rows = rows(manifest, "completed_scenes", "scenes")
    expected_count = parts * scenes
    if len(source_rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; found {len(source_rows)}"
        )

    source_map = {}
    for index, row in enumerate(source_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render manifest row {index}")

        try:
            part = int(row["part"])
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1
        if (part, scene, global_scene) != (expected_part, expected_scene, index):
            raise RuntimeError(f"Wrong scene order in render manifest at row {index}")
        source_map[(part, scene)] = row

    progress = read_json(PROGRESS_FILE, {})
    cached = progress.get("scenes", {}) if isinstance(progress, dict) else {}
    if not isinstance(cached, dict):
        cached = {}

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    completed = {}
    final_srt_cues = []
    global_cursor = 0.0
    cue_number = 1

    log("CAPTION VERSION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)
    log("FONT:", family, "| SIZE:", size)
    log("STYLE: adaptive black/white text with outline, no solid caption box")
    log("TIMING: estimated phrase-level timing; not forced alignment")

    for index in range(1, expected_count + 1):
        part = (index - 1) // scenes + 1
        scene = (index - 1) % scenes + 1
        key = (part, scene)
        row = source_map[key]

        source_value = find_path(
            row,
            ("source_rendered_video", "source_video", "video", "output"),
        )
        source = safe_file(source_value, f"Rendered source {key}", MIN_VIDEO_BYTES)
        info = media_info(source)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Rendered source lacks audio/video: {key}")
        if (info["width"], info["height"]) != (WIDTH, HEIGHT):
            raise RuntimeError(f"Rendered source is not 1280x720: {key}")

        duration = min(info["duration"], audio[key]["duration"])
        style = contrast_style(source, duration)
        destination = CAPTIONED_DIR / f"part_{part:02d}_scene_{scene:03d}.mp4"

        signature = digest_data({
            "version": VERSION,
            "source_sha256": digest_file(source),
            "narration_sha256": audio[key]["sha256"],
            "caption": captions[key],
            "font_family": family,
            "font_sha256": font_hash,
            "size": size,
            "duration": round(duration, 4),
            "style": style,
        })

        previous = cached.get(f"{part}:{scene}", {})
        reused = False

        if destination.is_file() and isinstance(previous, dict):
            try:
                existing = media_info(destination)
                reused = (
                    previous.get("signature") == signature
                    and previous.get("sha256") == digest_file(destination)
                    and existing["has_video"]
                    and existing["has_audio"]
                    and (existing["width"], existing["height"]) == (WIDTH, HEIGHT)
                    and abs(existing["duration"] - duration) <= DURATION_TOLERANCE
                )
            except Exception:
                reused = False

        if reused:
            result_info = media_info(destination)
            cues = make_cues(captions[key], duration)
            log(f"REUSED {index}/{expected_count}: {destination.name}")
        else:
            result_info, cues = burn_one(
                source, destination, captions[key], duration,
                font_path, family, size, style,
            )
            log(
                f"BURNED {index}/{expected_count}: {destination.name}; "
                f"style={style['mode']}"
            )

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
            "caption_timing": "phrase-level-proportional-estimate",
            "signature": signature,
            "sha256": digest_file(destination),
            "cache_reused": reused,
        }

        cached[f"{part}:{scene}"] = {
            "signature": signature,
            "sha256": digest_file(destination),
            "path": relative(destination),
            "caption_style": style,
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

    numbered_cues = []
    for cue in final_srt_cues:
        numbered_cues.append({
            **cue,
            "number": cue_number,
        })
        cue_number += 1

    srt_blocks = []
    for cue in numbered_cues:
        srt_blocks.extend([
            str(cue["number"]),
            f"{srt_time(cue['start'])} --> {srt_time(cue['end'])}",
            cue["text"],
            "",
        ])
    CAPTION_DIR.mkdir(parents=True, exist_ok=True)
    temp_srt = FINAL_SRT.with_suffix(".srt.tmp")
    temp_srt.write_text("\n".join(srt_blocks), encoding="utf-8")
    os.replace(temp_srt, FINAL_SRT)

    part_paths = []
    for part in range(1, parts + 1):
        scene_paths = [
            ROOT / completed[(part, scene)]["video"]
            for scene in range(1, scenes + 1)
        ]
        part_path = PART_DIR / f"part_{part:02d}.mp4"
        concat_videos(scene_paths, part_path)
        part_paths.append(part_path)
        log("PART READY:", relative(part_path))

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    concat_videos(part_paths, final_path)

    alias_name = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    alias_path = VIDEO_DIR / alias_name
    shutil.copy2(final_path, alias_path)

    opposite = VIDEO_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite.unlink(missing_ok=True)

    final_info = media_info(final_path)

    final_manifest = {
        **manifest,
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "captions_burned": True,
        "caption_style": "adaptive-contrast-outline-no-solid-box",
        "caption_timing": "phrase-level-proportional-estimates",
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
            }
            for index, path in enumerate(part_paths, 1)
        ],
        "final_video": relative(final_path),
        "alias": relative(alias_path),
        "duration": round(final_info["duration"], 4),
        "resolution": [WIDTH, HEIGHT],
        "caption_manifest": relative(CAPTION_MANIFEST),
        "final_srt": relative(FINAL_SRT),
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
    log("FINAL VIDEO:", relative(final_path))
    log("FINAL SRT:", relative(FINAL_SRT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION BURN FAILED:", str(exc))
        sys.exit(1)