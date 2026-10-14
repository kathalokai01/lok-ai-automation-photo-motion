#!/usr/bin/env python3
"""Burn readable captions into scene videos and rebuild final deliverables."""

from __future__ import annotations

import hashlib
import json
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
RENDER_MANIFEST = OUT / "scene_renders" / "render_manifest.json"
CAPTION_MANIFEST = OUT / "captions" / "captions.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
PROGRESS_FILE = OUT / "captions" / "burn_progress.json"
FINAL_SRT = OUT / "captions" / "final_captions.srt"

VERSION = "burn-captions-v16"
MIN_VIDEO_BYTES = 1000
DURATION_TOLERANCE = 0.8
CAPTION_STYLE = (
    "bottom-centered;white-text;black-outline;"
    "translucent-dark-box;maximum-two-lines"
)


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def signature(data):
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing file path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is too small: {path.name}")

    return path


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def run(command, label):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300,
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
        f"ffprobe {path.name}",
    )

    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    streams = data.get("streams", [])

    video = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )

    if duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def get_rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def find_path(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def load_caption_mapping(parts, scenes):
    data = read_json(CAPTION_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is missing or incomplete")

    mapping = {}
    for row in get_rows(data, "captions"):
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, ValueError, TypeError):
            continue

        text = str(row.get("text", "")).strip()
        if not text:
            raise RuntimeError(f"Empty caption text: {key}")
        if key in mapping:
            raise RuntimeError(f"Duplicate caption mapping: {key}")
        mapping[key] = text

    expected = {
        (p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes + 1)
    }
    if set(mapping) != expected:
        raise RuntimeError(
            "Caption scene mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return mapping


def load_audio_mapping(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Narration manifest is missing")

    mapping = {}
    for row in get_rows(data, "jobs", "audio_jobs"):
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, ValueError, TypeError):
            continue

        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration incomplete: {key}")

        value = find_path(
            row, ("output", "audio_path", "path", "file", "audio")
        )
        audio = safe_file(value, f"Narration {key}", 100)

        info = media_info(audio)
        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {key}")

        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")

        mapping[key] = {
            "path": audio,
            "duration": info["duration"],
            "sha256": sha256_file(audio),
        }

    expected = {
        (p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes + 1)
    }
    if set(mapping) != expected:
        raise RuntimeError("Narration mapping does not match configured scenes")

    return mapping


def wrap_caption(text, max_chars=38):
    """Wrap caption into no more than two lines."""
    clean = re.sub(r"\s+", " ", str(text)).strip()
    if not clean:
        return ""

    words = clean.split()
    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word

        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

        if len(lines) == 2:
            break

    if current and len(lines) < 2:
        lines.append(current)

    consumed = " ".join(lines).split()
    if len(consumed) < len(words):
        remaining = " ".join(words[len(consumed):])
        if remaining:
            while len(lines) < 2:
                lines.append("")
            lines[1] = (lines[1].rstrip() + " …").strip()

    return r"\N".join(line for line in lines[:2] if line)


def srt_time(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def escape_filter_path(path):
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    return value


def caption_filter(font, size, width, height):
    font_path = escape_filter_path(font)
    # Margin is proportional to output height to protect phone UI areas.
    margin_v = max(42, int(height * 0.055))
    font_size = max(12, min(int(size), int(height * 0.045)))

    return (
        "subtitles={srt}:fontsdir={fonts}:"
        "force_style='FontName=DejaVu Sans,"
        "FontSize={size},"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BackColour=&H70000000,"
        "BorderStyle=3,"
        "Outline=2,"
        "Shadow=0,"
        "Alignment=2,"
        "MarginL=36,"
        "MarginR=36,"
        "MarginV={margin}'"
    ).format(
        srt="{srt}",
        fonts=font_path,
        size=font_size,
        margin=margin_v,
    )


def burn_scene(source, output, text, font, size, duration, resolution):
    width, height = resolution
    temporary_srt = output.with_suffix(".srt")

    # Compute replacement before the f-string to remain Python 3.11-safe.
    wrapped = wrap_caption(text)
    subtitle_text = wrapped.replace(r"\N", "\n")
    srt_content = (
        "1\n"
        f"{srt_time(0)} --> {srt_time(duration)}\n"
        f"{subtitle_text}\n"
    )
    temporary_srt.write_text(srt_content, encoding="utf-8")

    filter_template = caption_filter(font, size, width, height)
    subtitle_path = escape_filter_path(temporary_srt)
    filter_value = filter_template.format(srt=subtitle_path)

    temp_output = output.with_suffix(".tmp.mp4")
    temp_output.unlink(missing_ok=True)

    try:
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", filter_value,
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "160k",
                "-ar", "44100",
                "-af", "aresample=async=1:first_pts=0",
                "-t", f"{duration:.3f}",
                "-movflags", "+faststart",
                str(temp_output),
            ],
            f"Caption burn {output.name}",
        )

        info = media_info(temp_output)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Caption output lacks audio/video: {output.name}")

        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Unexpected caption output resolution: {output.name}")

        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Caption output duration mismatch: {output.name}")

        temp_output.replace(output)

    finally:
        temporary_srt.unlink(missing_ok=True)
        temp_output.unlink(missing_ok=True)


def concat_videos(paths, output):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list")

    list_file = output.with_suffix(".concat.txt")
    temp_output = output.with_suffix(".tmp.mp4")

    lines = []
    for path in paths:
        escaped = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")

    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_output.unlink(missing_ok=True)

    try:
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "160k",
                "-movflags", "+faststart",
                str(temp_output),
            ],
            f"Video merge {output.name}",
        )

        info = media_info(temp_output)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Merged video invalid: {output.name}")

        temp_output.replace(output)

    finally:
        list_file.unlink(missing_ok=True)
        temp_output.unlink(missing_ok=True)


def build_srt(captions, audio, parts, scenes, render_rows):
    lines = []
    cue_number = 1
    cursor = 0.0

    for part in range(1, parts + 1):
        for scene in range(1, scenes + 1):
            key = (part, scene)
            render = render_rows[key]
            duration = min(
                render["duration"],
                audio[key]["duration"],
            )

            if duration <= 0:
                raise RuntimeError(f"Invalid caption duration: {key}")

            caption_text = wrap_caption(captions[key])
            plain_text = caption_text.replace(r"\N", "\n")

            lines.extend([
                str(cue_number),
                f"{srt_time(cursor)} --> {srt_time(cursor + duration)}",
                plain_text,
                "",
            ])
            cue_number += 1
            cursor += render["duration"]

    FINAL_SRT.parent.mkdir(parents=True, exist_ok=True)
    temporary = FINAL_SRT.with_suffix(".srt.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(FINAL_SRT)


def main():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} is required but not installed")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")

    resolution = (1280, 720) if mode == "full" else (720, 1280)
    font = Path(str(get_caption_font(config)))
    if not font.is_absolute():
        font = ROOT / font
    if not font.exists():
        # FFmpeg can resolve a family name through installed fonts.
        font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

    font_size = int(get_caption_font_size(config))
    if font_size < 8 or font_size > 160:
        raise RuntimeError("Caption font size must be between 8 and 160")

    captions = load_caption_mapping(parts, scenes)
    audio = load_audio_mapping(parts, scenes)

    manifest = read_json(RENDER_MANIFEST)
    if not isinstance(manifest, dict):
        raise RuntimeError("Render manifest is missing")

    rows = get_rows(manifest, "completed_scenes")
    expected_count = parts * scenes
    if len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes, found {len(rows)}"
        )

    render_rows = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render row {index}")

        part = int(row.get("part", 0))
        scene = int(row.get("local_scene", row.get("scene", 0)))
        global_scene = int(row.get("global_scene", index))

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1
        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(f"Render scene order mismatch at {index}")

        render_rows[(part, scene)] = row

    progress = read_json(PROGRESS_FILE, {})
    cached = progress.get("scenes", {}) if isinstance(progress, dict) else {}
    if not isinstance(cached, dict):
        cached = {}

    output_dir = OUT / "scene_renders" / "captioned"
    output_dir.mkdir(parents=True, exist_ok=True)

    completed = {}
    total = expected_count

    for index in range(1, total + 1):
        part = (index - 1) // scenes + 1
        scene = (index - 1) % scenes + 1
        key = (part, scene)
        row = render_rows[key]

        source_value = find_path(
            row, ("source_rendered_video", "video", "output")
        )
        source = safe_file(
            source_value, f"Source scene {key}", MIN_VIDEO_BYTES
        )

        source_info = media_info(source)
        if not source_info["has_video"]:
            raise RuntimeError(f"Source has no video stream: {key}")
        if (source_info["width"], source_info["height"]) != resolution:
            raise RuntimeError(f"Source resolution mismatch: {key}")

        narration_duration = audio[key]["duration"]
        duration = min(source_info["duration"], narration_duration)
        if duration <= 0:
            raise RuntimeError(f"Invalid source/narration duration: {key}")

        target = output_dir / f"part_{part:02d}_scene_{scene:03d}.mp4"
        scene_sig = signature({
            "version": VERSION,
            "style": CAPTION_STYLE,
            "source_sha256": sha256_file(source),
            "narration_sha256": audio[key]["sha256"],
            "caption": captions[key],
            "font": str(font),
            "font_size": font_size,
            "duration": round(duration, 4),
            "resolution": resolution,
        })

        old = cached.get(f"{part}:{scene}", {})
        reused = False

        if target.is_file() and isinstance(old, dict):
            try:
                info = media_info(target)
                if (
                    old.get("signature") == scene_sig
                    and old.get("sha256") == sha256_file(target)
                    and info["has_video"]
                    and info["has_audio"]
                    and (info["width"], info["height"]) == resolution
                    and abs(info["duration"] - duration) <= DURATION_TOLERANCE
                ):
                    reused = True
            except Exception:
                reused = False

        if not reused:
            burn_scene(
                source, target, captions[key], font, font_size,
                duration, resolution,
            )

        digest = sha256_file(target)
        info = media_info(target)

        completed[key] = {
            "part": part,
            "scene": scene,
            "global_scene": index,
            "status": "completed",
            "source_rendered_video": relative(source),
            "video": relative(target),
            "duration": round(info["duration"], 4),
            "captions_burned": True,
            "signature": scene_sig,
            "sha256": digest,
            "cache_reused": reused,
        }

        cached[f"{part}:{scene}"] = {
            "signature": scene_sig,
            "sha256": digest,
            "path": relative(target),
        }

        write_json(PROGRESS_FILE, {
            "status": "in_progress",
            "version": VERSION,
            "completed_count": len(completed),
            "total_scenes": total,
            "scenes": cached,
        })

        log(
            f"{'REUSED' if reused else 'BURNED'}: "
            f"part={part} scene={scene} ({len(completed)}/{total})"
        )

    # Build each part in strict scene order.
    part_paths = []
    parts_dir = OUT / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    for part in range(1, parts + 1):
        ordered = [
            Path(completed[(part, scene)]["video"])
            if Path(completed[(part, scene)]["video"]).is_absolute()
            else ROOT / completed[(part, scene)]["video"]
            for scene in range(1, scenes + 1)
        ]
        part_output = parts_dir / f"part_{part:02d}.mp4"
        concat_videos(ordered, part_output)
        part_paths.append(part_output)
        log("PART READY:", relative(part_output))

    videos_dir = OUT / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    final_path = videos_dir / "katha_lok_ai_final.mp4"
    concat_videos(part_paths, final_path)

    alias_name = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    alias_path = videos_dir / alias_name
    shutil.copy2(final_path, alias_path)

    opposite = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    (videos_dir / opposite).unlink(missing_ok=True)

    build_srt(captions, audio, parts, scenes, render_rows)

    final_info = media_info(final_path)
    final_manifest = {
        **manifest,
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "captions_burned": True,
        "caption_style": CAPTION_STYLE,
        "completed_count": total,
        "completed_scenes": [
            completed[(part, scene)]
            for part in range(1, parts + 1)
            for scene in range(1, scenes + 1)
        ],
        "parts": [relative(path) for path in part_paths],
        "final_video": relative(final_path),
        "alias": relative(alias_path),
        "duration": round(final_info["duration"], 4),
        "resolution": list(resolution),
        "fps": manifest.get("fps"),
        "caption_manifest": relative(CAPTION_MANIFEST),
        "final_srt": relative(FINAL_SRT),
    }
    write_json(RENDER_MANIFEST, final_manifest)

    write_json(PROGRESS_FILE, {
        "status": "completed",
        "version": VERSION,
        "completed_count": total,
        "total_scenes": total,
        "final_video": relative(final_path),
        "scenes": cached,
    })

    log("CAPTION BURN COMPLETE")
    log("Scenes:", total)
    log("Format:", mode)
    log("Final video:", relative(final_path))
    log("Alias:", relative(alias_path))
    log("SRT:", relative(FINAL_SRT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION BURN FAILED:", str(exc))
        sys.exit(1)