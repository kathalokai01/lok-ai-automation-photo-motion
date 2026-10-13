#!/usr/bin/env python3
"""Burn configurable Hindi captions into rendered scenes and assemble videos."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_caption_font,
    get_caption_font_size,
)

OUTPUT = ROOT / "output"
RENDER_MANIFEST = OUTPUT / "scene_renders" / "render_manifest.json"
CAPTIONS_JSON = OUTPUT / "captions" / "captions.json"
AUDIO_JOBS = OUTPUT / "narration" / "audio_jobs.json"
CAPTION_DIR = OUTPUT / "captions"
CAPTIONED_DIR = OUTPUT / "captioned_scenes"
CHECKPOINT = CAPTIONED_DIR / "caption_checkpoint.json"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"

VERSION = 10
MIN_BYTES = 1000
MAX_LINE_CHARS = 30
MAX_LINES = 2
OUTLINE_SIZE = 2

FONT_OPTIONS = {
    "Noto Sans Devanagari": "Noto Sans Devanagari",
    "Mukta": "Mukta",
    "Hind": "Hind",
    "Tiro Devanagari Hindi": "Tiro Devanagari Hindi",
}

PALETTE = [
    (255, 225, 0),
    (0, 240, 255),
    (130, 255, 0),
    (255, 145, 0),
    (255, 50, 210),
    (190, 110, 255),
    (255, 115, 160),
    (0, 220, 120),
    (255, 255, 255),
    (0, 0, 0),
]


def log(*args):
    print(*args, flush=True)


def signature(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing path: {label}")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"Path outside repository: {label}")

    return path


def run(command, timeout=900):
    log("$", " ".join(map(str, command)))
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.stdout:
        log(result.stdout.rstrip())
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command[0]}")


def probe(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError(f"Missing or invalid media: {path}")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required.")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed: {path.name}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if (
        video is None
        or not math.isfinite(duration)
        or duration <= 0
        or int(video.get("width") or 0) < 1
        or int(video.get("height") or 0) < 1
    ):
        raise RuntimeError(f"Invalid video stream: {path}")

    return {
        "duration": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": audio is not None,
    }


def positive_int(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc
    if number < 1:
        raise RuntimeError(f"{label} must be positive.")
    return number


def clean_text(value):
    return " ".join(
        str(value or "").replace("\r", " ").replace("\n", " ").split()
    )


def verify_font(font_name):
    """Require the exact requested font family to be installed."""
    if font_name not in FONT_OPTIONS:
        raise RuntimeError(f"Unsupported caption font: {font_name!r}")

    fc_match = shutil.which("fc-match")
    if not fc_match:
        raise RuntimeError(
            "fontconfig is missing; install fontconfig before rendering."
        )

    result = subprocess.run(
        [fc_match, "-f", "%{family}\n%{file}\n", font_name],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Could not inspect caption font: {font_name}")

    lines = result.stdout.strip().splitlines()
    if len(lines) < 2 or not lines[1].strip():
        raise RuntimeError(f"Caption font unavailable: {font_name}")

    families = [part.strip().lower() for part in lines[0].split(",")]
    requested = font_name.lower()

    if requested not in families:
        raise RuntimeError(
            f"Requested font {font_name!r} did not resolve correctly. "
            f"Resolved family: {lines[0]!r}. "
            "Install the selected font before rendering."
        )

    font_file = Path(lines[1].strip())
    if not font_file.is_file():
        raise RuntimeError(f"Font file does not exist: {font_file}")

    log(f"Verified caption font: {font_name} -> {font_file}")


def resolve_caption_style(config, height):
    font_name = get_caption_font(config)
    if font_name not in FONT_OPTIONS:
        raise RuntimeError(f"Unsupported caption font: {font_name!r}")

    configured_size = get_caption_font_size(config)
    if configured_size == "auto":
        font_size = max(22, min(38, round(height * 0.030)))
    else:
        font_size = positive_int(configured_size, "CAPTION_FONT_SIZE")
        if not 16 <= font_size <= 64:
            raise RuntimeError("CAPTION_FONT_SIZE must be between 16 and 64.")

    verify_font(font_name)
    return font_name, font_size


def load_caption_map():
    data = read_json(CAPTIONS_JSON)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption preparation is not completed.")

    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Caption list is empty.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption record.")

        key = (
            positive_int(row.get("part"), "caption part"),
            positive_int(row.get("scene"), "caption scene"),
        )
        text = clean_text(row.get("text"))
        if not text:
            raise RuntimeError(f"Empty caption: {key}")
        if key in result:
            raise RuntimeError(f"Duplicate caption: {key}")
        result[key] = text

    return result


def load_audio_durations():
    data = read_json(AUDIO_JOBS)
    if not isinstance(data, dict):
        raise RuntimeError("Narration audio jobs manifest must be a JSON object.")

    rows = data.get("jobs")
    if rows is None:
        rows = data.get("audio_jobs")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("No narration jobs found in audio_jobs.json.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).lower() != "completed":
            continue

        try:
            key = (
                positive_int(row.get("part"), "audio job part"),
                positive_int(row.get("scene"), "audio job scene"),
            )
            duration = float(row.get("duration"))
        except (TypeError, ValueError, RuntimeError):
            continue

        if not math.isfinite(duration) or duration <= 0:
            continue
        if key in result:
            raise RuntimeError(f"Duplicate narration audio job for {key}.")
        result[key] = duration

    if not result:
        raise RuntimeError("No completed narration durations found.")

    return result


def wrap_text(text):
    """Wrap caption text into cues containing at most two lines."""
    words = clean_text(text).split()
    lines = []
    current = ""

    for word in words:
        if len(word) > MAX_LINE_CHARS:
            if current:
                lines.append(current)
                current = ""
            chunks = [
                word[i:i + MAX_LINE_CHARS]
                for i in range(0, len(word), MAX_LINE_CHARS)
            ]
            lines.extend(chunks[:-1])
            current = chunks[-1]
            continue

        candidate = f"{current} {word}".strip()
        if current and len(candidate) > MAX_LINE_CHARS:
            lines.append(current)
            current = word
        else:
            current = candidate

    if current:
        lines.append(current)

    return [
        r"\N".join(lines[i:i + MAX_LINES])
        for i in range(0, len(lines), MAX_LINES)
    ]


def ass_time(seconds):
    value = max(0, round(seconds * 100))
    hours, value = divmod(value, 360000)
    minutes, value = divmod(value, 6000)
    secs, value = divmod(value, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{value:02}"


def srt_time(seconds):
    value = max(0, round(seconds * 1000))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    secs, value = divmod(value, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{value:03}"


def linear_channel(value):
    value /= 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    red, green, blue = (linear_channel(c) for c in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    a, b = luminance(first), luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def ass_colour(rgb):
    red, green, blue = rgb
    return f"&H00{blue:02X}{green:02X}{red:02X}&"


def ass_escape_text(text):
    """Escape literal text but preserve our intentional ASS line breaks."""
    pieces = str(text).split(r"\N")
    escaped = []
    for piece in pieces:
        piece = piece.replace("\\", r"\\")
        piece = piece.replace("{", r"\{").replace("}", r"\}")
        escaped.append(piece)
    return r"\N".join(escaped)


def filter_path(path):
    value = str(Path(path).resolve())
    for old, new in (
        ("\\", "\\\\"),
        (":", "\\:"),
        ("'", "\\'"),
        (",", "\\,"),
        ("[", "\\["),
        ("]", "\\]"),
    ):
        value = value.replace(old, new)
    return value


def sample_background(ffmpeg, video, seconds):
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0, seconds):.3f}",
        "-i", str(video),
        "-vf",
        "crop=iw:ih*0.30:0:ih*0.65,"
        "scale=1:1:flags=area,format=rgb24",
        "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=25, check=False,
        )
        if result.returncode == 0 and len(result.stdout) >= 3:
            return tuple(result.stdout[:3])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return (45, 45, 45)


def cue_timings(text, speech_duration):
    """Spread cues over measured narration duration; this is not word alignment."""
    cues = wrap_text(text)
    if not cues:
        raise RuntimeError("Caption is empty.")

    duration = max(0.10, float(speech_duration))
    weights = [max(1, len(cue.replace(r"\N", " ").split())) for cue in cues]
    total_weight = sum(weights)
    cursor = 0.0
    result = []

    for index, (cue, weight) in enumerate(zip(cues, weights)):
        end = duration if index == len(cues) - 1 else (
            cursor + duration * weight / total_weight
        )
        end = min(duration, max(cursor + 0.01, end))
        result.append((cursor, end, cue))
        cursor = end

    return result


def create_ass(ffmpeg, scene, destination):
    width, height = scene["width"], scene["height"]
    margin_v = max(48, round(height * 0.055))
    margin_h = max(24, round(width * 0.045))
    events = []

    for start, end, cue in cue_timings(
        scene["text"], scene["speech_duration"]
    ):
        background = sample_background(
            ffmpeg,
            scene["path"],
            min((start + end) / 2, max(0, scene["duration"] - 0.05)),
        )
        colour = max(PALETTE, key=lambda rgb: contrast(rgb, background))
        outline = (
            (0, 0, 0)
            if contrast(colour, (0, 0, 0)) >= contrast(colour, (255, 255, 255))
            else (255, 255, 255)
        )
        override = (
            f"{{\\1c{ass_colour(colour)}"
            f"\\3c{ass_colour(outline)}\\bord{OUTLINE_SIZE}}}"
        )
        events.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},"
            f"Default,,0,0,0,,{override}{ass_escape_text(cue)}\n"
        )

    header = f"""[Script Info]
Title: Scene {scene['global_scene']} captions
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{scene['font_name']},{scene['font_size']},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{OUTLINE_SIZE},0,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header + "".join(events), encoding="utf-8")


def read_checkpoint():
    if not CHECKPOINT.is_file():
        return {}
    try:
        data = read_json(CHECKPOINT)
        return data if isinstance(data, dict) else {}
    except Exception:
        log("NOTICE: Caption checkpoint unreadable; rebuilding scenes.")
        return {}


def render_captioned_scene(ffmpeg, scene, cache_signature, index, total):
    number = scene["global_scene"]
    destination = CAPTIONED_DIR / f"scene_{number:04d}.mp4"
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    ass_path = CAPTION_DIR / "scene_ass" / f"scene_{number:04d}.ass"
    checkpoint = read_checkpoint()
    entry = checkpoint.get(str(number), {})

    if (
        isinstance(entry, dict)
        and entry.get("signature") == cache_signature
        and destination.is_file()
        and destination.stat().st_size >= MIN_BYTES
    ):
        try:
            info = probe(destination)
            if (
                info["has_audio"]
                and (info["width"], info["height"]) == (scene["width"], scene["height"])
                and abs(info["duration"] - scene["duration"]) <= 1.0
            ):
                log(f"REUSE captioned scene {index}/{total}: {destination.name}")
                return destination, info
        except Exception as exc:
            log(f"Caption cache rejected for scene {number}: {exc}")

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)
    create_ass(ffmpeg, scene, ass_path)

    run([
        ffmpeg, "-hide_banner", "-y",
        "-i", str(scene["path"]),
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", f"ass=filename='{filter_path(ass_path)}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", str(temp),
    ])

    info = probe(temp)
    if not info["has_audio"]:
        raise RuntimeError(f"Captioned scene {number} has no audio.")
    if (info["width"], info["height"]) != (scene["width"], scene["height"]):
        raise RuntimeError(f"Captioned scene {number} resolution changed.")
    if abs(info["duration"] - scene["duration"]) > 1.0:
        raise RuntimeError(f"Captioned scene {number} duration changed.")

    temp.replace(destination)
    checkpoint[str(number)] = {
        "version": VERSION,
        "signature": cache_signature,
        "source_sha256": sha256_file(scene["path"]),
        "video": destination.relative_to(ROOT).as_posix(),
        "size_bytes": destination.stat().st_size,
        "status": "completed",
    }
    write_json(CHECKPOINT, checkpoint)
    log(f"CHECKPOINT saved: scene {index}/{total}")
    return destination, info


def write_srt(scenes):
    entries = []
    offset = 0.0
    cue_number = 1

    for scene in scenes:
        for start, end, cue in cue_timings(
            scene["text"], scene["speech_duration"]
        ):
            entries.append(
                f"{cue_number}\n"
                f"{srt_time(offset + start)} --> {srt_time(offset + end)}\n"
                f"{cue.replace(r'\\N', chr(10))}\n"
            )
            cue_number += 1
        offset += scene["duration"]

    if not entries:
        raise RuntimeError("No SRT captions generated.")

    destination = CAPTION_DIR / "final_captions.srt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    temp.write_text("\n".join(entries) + "\n", encoding="utf-8-sig")
    temp.replace(destination)


def concatenate(ffmpeg, paths, destination, list_file, resolution):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list.")

    for path in paths:
        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(f"Audio missing from concat input: {path}")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Resolution mismatch in concat input: {path}")

    list_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in paths:
        value = str(Path(path).resolve())
        if "\n" in value or "\r" in value:
            raise RuntimeError("Invalid newline in media path.")
        lines.append("file '" + value.replace("'", "'\\''") + "'")

    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)

    try:
        run([
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", str(temp),
        ])
        info = probe(temp)
        if not info["has_audio"]:
            raise RuntimeError(f"Output has no audio: {destination}")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Output resolution mismatch: {destination}")
        temp.replace(destination)
        return probe(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        list_file.unlink(missing_ok=True)


def main():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required.")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    requested_parts = positive_int(get_parts(config), "PARTS")
    resolution = (1280, 720) if mode == "full" else (720, 1280)

    selected_font = get_caption_font(config)
    verify_font(selected_font)

    manifest = read_json(RENDER_MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        raise RuntimeError("Scene render manifest is not completed.")
    if manifest.get("format") != mode:
        raise RuntimeError("Render manifest FORMAT does not match input.")

    render_rows = manifest.get("completed_scenes")
    if not isinstance(render_rows, list) or not render_rows:
        raise RuntimeError("No completed rendered scenes found.")

    captions = load_caption_map()
    audio_durations = load_audio_durations()
    scenes = []
    seen_global = set()
    seen_keys = set()

    for index, row in enumerate(render_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render manifest row {index}.")

        part = positive_int(row.get("part"), "scene part")
        local = positive_int(row.get("local_scene"), "local scene")
        global_scene = positive_int(
            row.get("global_scene", row.get("scene", index)), "global scene"
        )
        key = (part, local)

        if key in seen_keys or global_scene in seen_global:
            raise RuntimeError(f"Duplicate scene mapping: {key}")
        seen_keys.add(key)
        seen_global.add(global_scene)

        if part > requested_parts:
            raise RuntimeError(f"Unexpected part number: {part}")

        path = safe_path(row.get("video"), f"rendered scene {global_scene}")
        info = probe(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Rendered scene {global_scene} has no audio.")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Scene {global_scene} resolution mismatches FORMAT.")
        if key not in captions:
            raise RuntimeError(f"Missing caption for part/scene {key}.")
        if key not in audio_durations:
            raise RuntimeError(f"Missing narration duration for part/scene {key}.")

        font_name, font_size = resolve_caption_style(config, info["height"])
        speech_duration = min(info["duration"], audio_durations[key])

        scenes.append({
            "part": part,
            "local_scene": local,
            "global_scene": global_scene,
            "path": path,
            "duration": info["duration"],
            "speech_duration": max(0.10, speech_duration),
            "width": info["width"],
            "height": info["height"],
            "text": captions[key],
            "font_name": font_name,
            "font_size": font_size,
            "row": dict(row),
        })

    scenes.sort(key=lambda item: item["global_scene"])

    if [s["global_scene"] for s in scenes] != list(range(1, len(scenes) + 1)):
        raise RuntimeError("Global scene numbering is not continuous.")
    if len(captions) != len(scenes):
        raise RuntimeError("Caption count does not match rendered scenes.")
    if {s["part"] for s in scenes} != set(range(1, requested_parts + 1)):
        raise RuntimeError("Rendered parts do not match configured PARTS.")

    for part in range(1, requested_parts + 1):
        local_ids = sorted(s["local_scene"] for s in scenes if s["part"] == part)
        if not local_ids or local_ids != list(range(1, len(local_ids) + 1)):
            raise RuntimeError(f"Part {part} has missing or unordered scenes.")

    captioned_paths = []
    updated_rows = []

    for index, scene in enumerate(scenes, 1):
        cache_signature = signature({
            "version": VERSION,
            "source_sha256": sha256_file(scene["path"]),
            "caption": scene["text"],
            "duration": round(scene["duration"], 4),
            "speech_duration": round(scene["speech_duration"], 4),
            "font_name": scene["font_name"],
            "font_size": scene["font_size"],
            "width": scene["width"],
            "height": scene["height"],
            "max_line_chars": MAX_LINE_CHARS,
            "max_lines": MAX_LINES,
            "outline_size": OUTLINE_SIZE,
        })

        path, info = render_captioned_scene(
            ffmpeg, scene, cache_signature, index, len(scenes)
        )
        captioned_paths.append(path)

        updated = dict(scene["row"])
        updated["source_rendered_video"] = scene["path"].relative_to(ROOT).as_posix()
        updated["video"] = path.relative_to(ROOT).as_posix()
        updated["duration"] = round(info["duration"], 6)
        updated["captions_burned"] = True
        updated["caption_font"] = scene["font_name"]
        updated["caption_font_size"] = scene["font_size"]
        updated["caption_speech_duration"] = round(scene["speech_duration"], 6)
        updated["caption_signature"] = cache_signature
        updated["status"] = "completed"
        updated_rows.append(updated)

    write_srt(scenes)

    grouped = defaultdict(list)
    for scene, path in zip(scenes, captioned_paths):
        grouped[scene["part"]].append((scene["local_scene"], path))

    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    part_paths = []

    for part in range(1, requested_parts + 1):
        group = sorted(grouped[part], key=lambda item: item[0])
        destination = PARTS_DIR / f"part_{part:02d}.mp4"
        concatenate(
            ffmpeg,
            [path for _, path in group],
            destination,
            CAPTION_DIR / f"concat_captioned_part_{part:02d}.txt",
            resolution,
        )
        part_paths.append(destination)
        log("CAPTIONED PART READY:", destination.relative_to(ROOT))

    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    final_info = concatenate(
        ffmpeg,
        captioned_paths,
        final_video,
        CAPTION_DIR / "concat_captioned_final.txt",
        resolution,
    )

    alias = VIDEOS_DIR / (
        "katha_lok_ai_full.mp4" if mode == "full" else "katha_lok_ai_short.mp4"
    )
    temp_alias = alias.with_name(alias.stem + ".tmp.mp4")
    try:
        shutil.copy2(final_video, temp_alias)
        alias_info = probe(temp_alias)
        if not alias_info["has_audio"]:
            raise RuntimeError("Final alias has no audio.")
        if (alias_info["width"], alias_info["height"]) != resolution:
            raise RuntimeError("Final alias resolution is incorrect.")
        temp_alias.replace(alias)
    finally:
        temp_alias.unlink(missing_ok=True)

    opposite = VIDEOS_DIR / (
        "katha_lok_ai_short.mp4" if mode == "full" else "katha_lok_ai_full.mp4"
    )
    opposite.unlink(missing_ok=True)

    manifest.update({
        "version": VERSION,
        "format": mode,
        "status": "completed",
        "captions_burned": True,
        "completed_scenes": updated_rows,
        "parts": [p.relative_to(ROOT).as_posix() for p in part_paths],
        "final_video": final_video.relative_to(ROOT).as_posix(),
        "alias": alias.relative_to(ROOT).as_posix(),
        "duration": round(final_info["duration"], 6),
        "width": final_info["width"],
        "height": final_info["height"],
        "caption_font": scenes[0]["font_name"],
        "caption_font_size": scenes[0]["font_size"],
        "updated_at": datetime.now().astimezone().isoformat(),
    })
    write_json(RENDER_MANIFEST, manifest)

    log("=" * 48)
    log("CAPTIONS AND FINAL VIDEO COMPLETE")
    log(f"Format: {mode} | Scenes: {len(scenes)} | Parts: {len(part_paths)}")
    log(f"Caption font: {scenes[0]['font_name']} | Size: {scenes[0]['font_size']}")
    log("Final:", final_video.relative_to(ROOT))
    log("Alias:", alias.relative_to(ROOT))
    log("SRT:", (CAPTION_DIR / "final_captions.srt").relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION/ASSEMBLY FAILED:", exc)
        sys.exit(1)