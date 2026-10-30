
#!/usr/bin/env python3
"""Story-aware, contrast-safe, resumable caption burning."""

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

VERSION = "burn-captions-v25-story-scene-adaptive"
WIDTH, HEIGHT = 1280, 720
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.0

# ASS colors use &HAABBGGRR. Alpha 00 is opaque.
STYLE_PALETTES = [
    {"name": "cinematic-white", "light": "&H00FFFFFF",
     "dark": "&H00141414", "outline": "&H00000000"},
    {"name": "warm-gold", "light": "&H0000D7FF",
     "dark": "&H00202020", "outline": "&H00000000"},
    {"name": "soft-cream", "light": "&H00E8F1FF",
     "dark": "&H00202020", "outline": "&H00000000"},
    {"name": "mint-accent", "light": "&H0098FFD8",
     "dark": "&H00141414", "outline": "&H00000000"},
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
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_data(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


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


def run(command, label, timeout=900):
    result = subprocess.run(
        command, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{label} failed: {(result.stderr or '')[-3000:]}"
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
        f"Inspect {Path(path).name}", timeout=90,
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
        (x for x in streams if x.get("codec_type") == "video"), None
    )
    audio = next(
        (x for x in streams if x.get("codec_type") == "audio"), None
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
        f"Resolve font {family}", timeout=60,
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
        (p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes + 1)
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
            find_path(row, ("output", "audio_path", "path", "file", "audio")),
            f"Narration {key}", MIN_AUDIO_BYTES,
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
        (p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes + 1)
    }
    if set(mapping) != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
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
        current = []
        for word in sentence.split():
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
    """Find actual quiet gaps; these are timing hints, not word alignment."""
    if duration <= 0:
        return []

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path),
                "-vn", "-af", "silencedetect=noise=-35dB:d=0.18",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=180, check=False,
        )
        log_text = (result.stderr or "") + "\n" + (result.stdout or "")
        if result.returncode:
            log("WARNING: silence detection failed; proportional timing used")
            return []

        starts = [
            float(x) for x in re.findall(
                r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", log_text
            )
        ]
        ends = [
            float(x) for x in re.findall(
                r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", log_text
            )
        ]

        points = []
        for start, end in zip(starts, ends):
            if end > start:
                midpoint = (start + end) / 2
                if 0.05 < midpoint < duration - 0.05:
                    points.append(midpoint)

        if len(starts) > len(ends):
            start = starts[-1]
            if duration > start:
                midpoint = (start + duration) / 2
                if 0.05 < midpoint < duration - 0.05:
                    points.append(midpoint)

        return sorted(set(round(x, 3) for x in points))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        log("WARNING: silence detection unavailable:", str(exc))
        return []


def make_cues(text, audio_duration, video_duration, audio_path):
    phrases = split_phrases(text)
    limit = min(float(audio_duration), float(video_duration))
    if not phrases or limit <= 0:
        raise RuntimeError("Cannot create captions for empty text/duration")

    weights = [max(1, len(x.split())) for x in phrases]
    total = sum(weights)
    raw = [0.0]
    running = 0

    for weight in weights[:-1]:
        running += weight
        raw.append(limit * running / total)
    raw.append(limit)

    silences = silence_boundaries(audio_path, limit)
    boundaries = [0.0]

    for target in raw[1:-1]:
        candidates = [
            point for point in silences
            if abs(point - target) <= min(0.75, max(0.25, limit * 0.06))
            and point > boundaries[-1] + 0.12
            and point < limit - 0.12
        ]
        if candidates:
            target = min(candidates, key=lambda p: abs(p - target))

        target = max(
            boundaries[-1] + 0.12,
            min(target, limit - 0.12),
        )
        boundaries.append(target)

    boundaries.append(limit)
    cues = []

    for index, phrase in enumerate(phrases):
        start, end = boundaries[index], boundaries[index + 1]
        if end > start:
            cues.append({"start": start, "end": end, "text": phrase})

    if not cues:
        raise RuntimeError("No valid caption cues were generated")

    method = (
        "silence-assisted-estimate" if silences
        else "phrase-level-proportional-estimate"
    )
    return cues, method


def srt_time(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
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


def sample_luma(video, timestamp, region="bottom"):
    # Measure the lower caption area, not the entire frame average.
    crop = (
        "crop=iw:ih*0.38:0:ih*0.62"
        if region == "bottom"
        else "crop=iw:ih*0.55:0:ih*0.20"
    )
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0, timestamp):.3f}", "-i", str(video),
            "-frames:v", "1", "-vf",
            f"{crop},scale=1:1:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True, timeout=90, check=False,
    )
    if result.returncode or not result.stdout:
        return 64
    return int(result.stdout[0])


def contrast_style(video, duration, part, scene, story_seed):
    samples = [
        sample_luma(video, duration * fraction)
        for fraction in (0.18, 0.50, 0.82)
    ]
    average = sum(samples) / len(samples)

    seed = hashlib.sha256(
        f"{story_seed}:{part}:{scene}".encode("utf-8")
    ).digest()
    palette = STYLE_PALETTES[seed[0] % len(STYLE_PALETTES)]

    # Use dark lettering on bright backgrounds and bright lettering on dark.
    if average >= 145:
        primary = palette["dark"]
        outline = "&H00FFFFFF"
        mode = "light-background"
    else:
        primary = palette["light"]
        outline = "&H00101010"
        mode = "dark-background"

    # A slightly stronger outline on scenes with uncertain brightness.
    spread = max(samples) - min(samples)
    outline_width = 3 if spread < 90 else 4

    return {
        "mode": mode,
        "palette": palette["name"],
        "average_luma": round(average, 2),
        "sample_luma": samples,
        "primary": primary,
        "outline": outline,
        "back": "&H00000000",
        "outline_width": outline_width,
        "shadow": 1,
        "alignment": 2,
        "margin_vertical": 48 if HEIGHT > WIDTH else 42,
    }


def escape_filter_path(path):
    value = str(Path(path).resolve()).replace("\\", "/")
    for old, new in (
        (":", r"\:"), ("'", r"\'"), (",", r"\,"),
        ("[", r"\["), ("]", r"\]"), (";", r"\;"),
    ):
        value = value.replace(old, new)
    return value


def caption_filter(srt_path, font_path, family, size, contrast):
    max_font_size = 28 if HEIGHT > WIDTH else 32
    font_size = max(16, min(int(size), max_font_size))
    subtitle_file = escape_filter_path(srt_path)
    font_directory = escape_filter_path(font_path.parent)

    style = (
        f"FontName={family},FontSize={font_size},"
        f"PrimaryColour={contrast['primary']},"
        f"OutlineColour={contrast['outline']},"
        f"BackColour={contrast['back']},"
        "BorderStyle=1,"
        f"Outline={contrast['outline_width']},"
        f"Shadow={contrast['shadow']},"
        f"Alignment={contrast['alignment']},"
        "MarginL=42,MarginR=42,"
        f"MarginV={contrast['margin_vertical']}"
    )
    return (
        f"subtitles=filename='{subtitle_file}':"
        f"fontsdir='{font_directory}':force_style='{style}'"
    )


def burn_one(
    source, destination, text, video_duration, audio_duration, audio_path,
    font_path, family, size, style,
):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_srt = destination.with_suffix(".srt")
    temp_video = destination.with_name(destination.stem + ".tmp.mp4")
    temp_video.unlink(missing_ok=True)

    try:
        cues, timing_method = make_cues(
            text, audio_duration, video_duration, audio_path
        )
        write_srt(temp_srt, cues)
        vf = caption_filter(temp_srt, font_path, family, size, style)

        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", vf,
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                "-af", "aresample=async=1:first_pts=0",
                "-t", f"{video_duration:.3f}",
                "-movflags", "+faststart", str(temp_video),
            ],
            f"Burn captions into {destination.name}", timeout=900,
        )

        info = media_info(temp_video)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(
                f"Captioned output lacks audio/video: {destination}"
            )
        if (info["width"], info["height"]) != (WIDTH, HEIGHT):
            raise RuntimeError(
                f"Captioned output has wrong dimensions: {destination}"
            )
        if abs(info["duration"] - video_duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Captioned output duration mismatch: {destination}"
            )

        os.replace(temp_video, destination)
        return info, cues, timing_method
    finally:
        temp_srt.unlink(missing_ok=True)
        temp_video.unlink(missing_ok=True)


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
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(temporary),
            ],
            f"Merge {destination.name}", timeout=1800,
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
    global WIDTH, HEIGHT

    for command in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")

    WIDTH, HEIGHT = (
        (720, 1280) if mode == "short" else (1280, 720)
    )

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    family = get_caption_font(config)
    configured_size = get_caption_font_size(config)
    auto_size = 24 if mode == "short" else 28
    max_size = 28 if mode == "short" else 32
    size = (
        auto_size
        if str(configured_size).strip().lower() == "auto"
        else int(configured_size)
    )
    size = max(16, min(size, max_size))

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
        raise RuntimeError(
            "Render manifest format does not match Input/topic.txt"
        )

    source_rows = rows(manifest, "completed_scenes", "scenes")
    expected_count = parts * scenes
    if len(source_rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; "
            f"found {len(source_rows)}"
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
            raise RuntimeError(
                f"Invalid scene mapping at row {index}"
            ) from exc

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1
        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(
                f"Wrong scene order in render manifest at row {index}"
            )
        source_map[(part, scene)] = row

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
    cue_number = 1
    timing_methods = {}

    log("CAPTION VERSION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)
    log("FONT:", family, "| SIZE:", size)
    log("STYLE: scene-aware contrast, story-seeded palette, outline")
    log("TIMING: narration-duration and silence-assisted estimates")
    log("NOTE: no forced word-level alignment model is used")

    for index in range(1, expected_count + 1):
        part = (index - 1) // scenes + 1
        scene = (index - 1) % scenes + 1
        key = (part, scene)
        row = source_map[key]

        source = safe_file(
            find_path(
                row,
                ("source_rendered_video", "source_video", "video", "output"),
            ),
            f"Rendered source {key}", MIN_VIDEO_BYTES,
        )
        info = media_info(source)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(
                f"Rendered source lacks audio/video: {key}"
            )
        if (info["width"], info["height"]) != (WIDTH, HEIGHT):
            raise RuntimeError(
                f"Rendered source dimensions mismatch for FORMAT={mode}: "
                f"expected {WIDTH}x{HEIGHT}, found "
                f"{info['width']}x{info['height']} at scene {key}. "
                "The upstream photo-motion renderer must produce "
                "the selected format dimensions."
            )

        video_duration = info["duration"]
        audio_duration = audio[key]["duration"]
        style = contrast_style(
            source, video_duration, part, scene, story_seed
        )
        destination = (
            CAPTIONED_DIR
            / f"part_{part:02d}_scene_{scene:03d}.mp4"
        )

        signature = digest_data({
            "version": VERSION,
            "source_sha256": digest_file(source),
            "narration_sha256": audio[key]["sha256"],
            "caption": captions[key],
            "font_family": family,
            "font_sha256": font_hash,
            "size": size,
            "video_duration": round(video_duration, 4),
            "audio_duration": round(audio_duration, 4),
            "style": style,
            "story_seed": story_seed,
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
                    and (existing["width"], existing["height"])
                    == (WIDTH, HEIGHT)
                    and abs(existing["duration"] - video_duration)
                    <= DURATION_TOLERANCE
                )
            except Exception:
                reused = False

        if reused:
            result_info = media_info(destination)
            cues, timing_method = make_cues(
                captions[key], audio_duration,
                video_duration, audio[key]["path"],
            )
            log(f"REUSED {index}/{expected_count}: {destination.name}")
        else:
            result_info, cues, timing_method = burn_one(
                source, destination, captions[key],
                video_duration, audio_duration, audio[key]["path"],
                font_path, family, size, style,
            )
            log(
                f"BURNED {index}/{expected_count}: {destination.name}; "
                f"style={style['palette']}/{style['mode']}; "
                f"timing={timing_method}"
            )

        timing_methods[timing_method] = (
            timing_methods.get(timing_method, 0) + 1
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
            "caption_timing": timing_method,
            "signature": signature,
            "sha256": digest_file(destination),
            "cache_reused": reused,
        }

        cached[f"{part}:{scene}"] = {
            "signature": signature,
            "sha256": digest_file(destination),
            "path": relative(destination),
            "caption_style": style,
            "caption_timing": timing_method,
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

    srt_blocks = []
    for cue in final_srt_cues:
        srt_blocks.extend([
            str(cue_number),
            f"{srt_time(cue['start'])} --> {srt_time(cue['end'])}",
            cue["text"],
            "",
        ])
        cue_number += 1

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

    final_info = media_info(final_path)
    final_manifest = {
        **manifest,
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "captions_burned": True,
        "caption_style": "story-scene-adaptive-contrast",
        "caption_timing": (
            "silence-assisted-estimate"
            if timing_methods.get("silence-assisted-estimate", 0)
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
    log("TIMING METHODS:", timing_methods)
    log("FINAL VIDEO:", relative(final_path))
    log("FINAL SRT:", relative(FINAL_SRT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION BURN FAILED:", str(exc))
        sys.exit(1)
