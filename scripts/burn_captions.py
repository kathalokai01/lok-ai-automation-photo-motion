
#!/usr/bin/env python3
"""Burn scene captions, checkpoint captioned clips, and rebuild final videos."""

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

OUTPUT = ROOT / "output"
RENDER_DIR = OUTPUT / "scene_renders"
CAPTION_DIR = OUTPUT / "captions"
CAPTIONED_DIR = RENDER_DIR / "captioned"
PART_DIR = OUTPUT / "parts"
VIDEO_DIR = OUTPUT / "videos"

RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
CAPTION_MANIFEST = CAPTION_DIR / "captions.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"
FINAL_SRT = CAPTION_DIR / "final_captions.srt"

MIN_MEDIA_BYTES = 1000
ERRORS = []


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file does not exist: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is too small: {path}")

    return path


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def first_value(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def ffprobe_info(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-800:]}"
        )

    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    streams = data.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    if not video or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video or duration: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": audio is not None,
    }


def validate_font(font_name):
    executable = shutil.which("fc-match")
    if not executable:
        raise RuntimeError("fontconfig fc-match is not installed")

    result = subprocess.run(
        [executable, "-f", "%{family}", font_name],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"Caption font not available: {font_name}")

    actual = result.stdout.strip()
    log(f"CAPTION FONT: requested={font_name}; matched={actual}")
    return actual


def clean_caption(text):
    text = str(text or "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError("Caption text is empty")
    return text


def wrap_caption(text, max_chars=36, max_lines=2):
    """Wrap caption text to at most two lines without dropping words."""
    words = clean_caption(text).split()
    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    if len(lines) > max_lines:
        # Keep every word. Use a slightly longer line width before failing.
        return wrap_caption(text, max_chars=max_chars + 12, max_lines=max_lines)

    return r"\N".join(lines)


def srt_time(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def caption_signature(source_hash, text, font, size, duration):
    payload = {
        "source_sha256": source_hash,
        "text": text,
        "font": font,
        "font_size": size,
        "duration": round(duration, 3),
        "renderer": "burn-captions-v12",
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def escape_filter_path(path):
    """Escape a filesystem path for FFmpeg subtitles=filename syntax."""
    value = str(path.resolve()).replace("\\", "\\\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    return value


def burn_one_scene(
    source,
    destination,
    caption_text,
    duration,
    font_name,
    font_size,
    signature,
):
    sidecar = destination.with_suffix(".caption.json")
    if destination.is_file() and sidecar.is_file():
        try:
            cache = read_json(sidecar)
            if (
                cache.get("signature") == signature
                and destination.stat().st_size >= MIN_MEDIA_BYTES
            ):
                info = ffprobe_info(destination)
                if info["has_audio"] and info["duration"] > 0:
                    log("RESUME: reusing captioned scene", destination.name)
                    return info
        except Exception:
            pass

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    cue_text = wrap_caption(caption_text)
    local_srt = destination.with_suffix(".srt")
    local_srt.write_text(
        "1\n"
        f"{srt_time(0)} --> {srt_time(max(0.1, duration))}\n"
        f"{cue_text}\n",
        encoding="utf-8",
    )

    subtitle_path = escape_filter_path(local_srt)
    font = font_name.replace("'", "").replace(":", "")
    force_style = (
        f"FontName='{font}',"
        f"FontSize={int(font_size)},"
        "Bold=0,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00101010,"
        "BackColour=&H60000000,"
        "BorderStyle=1,"
        "Outline=2,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginL=48,"
        "MarginR=48,"
        "MarginV=72,"
        "WrapStyle=2"
    )
    video_filter = (
        f"subtitles=filename='{subtitle_path}':"
        f"force_style='{force_style}'"
    )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vf", video_filter,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "2048",
        str(temporary),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Caption burn failed for {source.name}: "
                f"{result.stderr[-1800:]}"
            )

        if not temporary.is_file() or temporary.stat().st_size < MIN_MEDIA_BYTES:
            raise RuntimeError(f"Captioned output is empty: {temporary.name}")

        info = ffprobe_info(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Captioned scene has no audio: {temporary.name}")

        os.replace(temporary, destination)
        write_json(
            sidecar,
            {
                "status": "completed",
                "signature": signature,
                "source": relative(source),
                "output": relative(destination),
                "caption_text": caption_text,
                "font": font_name,
                "font_size": int(font_size),
                "duration": info["duration"],
                "captions_burned": True,
            },
        )
        return info

    finally:
        for path in (temporary, local_srt):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def load_audio_jobs():
    data = read_json(AUDIO_MANIFEST)
    jobs = data.get("jobs", data.get("audio_jobs", [])) if isinstance(data, dict) else []
    mapping = {}

    for row in jobs:
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue
        if str(row.get("status", "")).lower() != "completed":
            continue
        mapping[key] = row

    return mapping


def load_captions():
    data = read_json(CAPTION_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")

    rows = data.get("captions")
    if not isinstance(rows, list):
        raise RuntimeError("Caption manifest has no captions list")

    mapping = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue
        text = clean_caption(row.get("text", ""))
        if key in mapping:
            raise RuntimeError(f"Duplicate caption mapping: {key}")
        mapping[key] = text

    return data, mapping


def make_concat_file(path, videos):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for video in videos:
        resolved = str(video.resolve())
        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Newline in video path cannot be concatenated")
        escaped = resolved.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_videos(videos, destination):
    if not videos:
        raise RuntimeError(f"No videos to concatenate for {destination.name}")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    destination.parent.mkdir(parents=True, exist_ok=True)
    concat_file = destination.with_suffix(".concat.txt")
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    make_concat_file(concat_file, videos)

    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "19",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Video merge failed for {destination.name}: "
                f"{result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_MEDIA_BYTES:
            raise RuntimeError(f"Merged video is empty: {destination.name}")

        info = ffprobe_info(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Merged video has no audio: {destination.name}")

        os.replace(temporary, destination)
        return info
    finally:
        for path in (concat_file, temporary):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Both ffmpeg and ffprobe must be installed")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    expected_count = parts * scenes_per_part
    font_name = get_caption_font(config)
    font_size = int(get_caption_font_size(config))

    if font_size < 16 or font_size > 64:
        raise RuntimeError("CAPTION_FONT_SIZE must resolve to 16–64")

    validate_font(font_name)

    render_data = read_json(RENDER_MANIFEST)
    if not isinstance(render_data, dict):
        raise RuntimeError("Render manifest must be a JSON object")
    if render_data.get("status") != "completed":
        raise RuntimeError("Scene rendering must finish before caption burning")

    scene_rows = render_data.get("completed_scenes")
    if not isinstance(scene_rows, list) or len(scene_rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; "
            f"found {len(scene_rows) if isinstance(scene_rows, list) else 0}"
        )

    caption_data, caption_map = load_captions()
    audio_map = load_audio_jobs()

    if len(caption_map) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} caption mappings; got {len(caption_map)}"
        )

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    CAPTION_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    resolution = (1280, 720) if mode == "full" else (720, 1280)
    sorted_rows = sorted(
        scene_rows,
        key=lambda row: int(row.get("global_scene", row.get("scene", 0))),
    )

    seen = set()
    captioned_rows = []
    srt_entries = []
    global_offset = 0.0

    for index, row in enumerate(sorted_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid rendered scene row {index}")

        part = int(row["part"])
        local_scene = int(row["local_scene"])
        global_scene = int(row.get("global_scene", row.get("scene", index)))
        key = (part, local_scene)

        if key in seen:
            raise RuntimeError(f"Duplicate rendered scene mapping: {key}")
        seen.add(key)

        if not 1 <= part <= parts:
            raise RuntimeError(f"Unexpected part number: {part}")
        if not 1 <= local_scene <= scenes_per_part:
            raise RuntimeError(f"Unexpected local scene number: {local_scene}")
        if global_scene != index:
            raise RuntimeError("Global scene order is not continuous")

        source_value = first_value(row, ("source_rendered_video", "video", "output"))
        if not source_value:
            raise RuntimeError(f"Rendered scene {global_scene} has no video path")

        source = safe_path(
            source_value, f"Rendered scene {global_scene}", MIN_MEDIA_BYTES
        )
        source_info = ffprobe_info(source)

        if (source_info["width"], source_info["height"]) != resolution:
            raise RuntimeError(
                f"Scene {global_scene} resolution mismatch: "
                f"{source_info['width']}x{source_info['height']}"
            )
        if not source_info["has_audio"]:
            raise RuntimeError(f"Rendered scene {global_scene} has no narration/audio")

        if key not in caption_map:
            raise RuntimeError(f"Missing caption for part/scene {key}")
        if key not in audio_map:
            raise RuntimeError(f"Missing completed narration job for {key}")

        audio_row = audio_map[key]
        try:
            narration_duration = float(audio_row.get("duration", 0))
        except (TypeError, ValueError):
            narration_duration = 0

        # Scene render duration is authoritative for the available video span.
        # Narration duration is checked and used to avoid captions extending
        # beyond the actual scene.
        if not math.isfinite(narration_duration) or narration_duration <= 0:
            narration_duration = source_info["duration"]

        cue_duration = min(source_info["duration"], narration_duration)
        if cue_duration <= 0:
            raise RuntimeError(f"Invalid caption duration for {key}")

        text = caption_map[key]
        destination = CAPTIONED_DIR / f"scene_{global_scene:04d}.mp4"
        signature = caption_signature(
            sha256_file(source), text, font_name, font_size, cue_duration
        )

        output_info = burn_one_scene(
            source=source,
            destination=destination,
            caption_text=text,
            duration=cue_duration,
            font_name=font_name,
            font_size=font_size,
            signature=signature,
        )

        if (output_info["width"], output_info["height"]) != resolution:
            raise RuntimeError(f"Captioned scene {global_scene} resolution changed")

        updated = dict(row)
        updated.update(
            {
                "scene": global_scene,
                "global_scene": global_scene,
                "part": part,
                "local_scene": local_scene,
                "source_rendered_video": relative(source),
                "video": relative(destination),
                "output": relative(destination),
                "caption_signature": signature,
                "caption_text": text,
                "caption_font": font_name,
                "caption_font_size": font_size,
                "captions_burned": True,
                "status": "completed",
            }
        )
        captioned_rows.append(updated)

        srt_entries.append(
            f"{len(srt_entries) + 1}\n"
            f"{srt_time(global_offset)} --> "
            f"{srt_time(global_offset + cue_duration)}\n"
            f"{wrap_caption(text)}\n"
        )
        global_offset += output_info["duration"]

        # Save progress after each successful scene. A failed later scene
        # does not invalidate the captioned files already checkpointed.
        checkpoint = dict(render_data)
        checkpoint["completed_scenes"] = captioned_rows + [
            item for item in sorted_rows[len(captioned_rows):]
            if int(item.get("global_scene", item.get("scene", 0))) > global_scene
        ]
        checkpoint["captions_burned"] = False
        checkpoint["caption_progress"] = {
            "completed_scenes": len(captioned_rows),
            "expected_scenes": expected_count,
            "last_global_scene": global_scene,
        }
        write_json(RENDER_MANIFEST, checkpoint)
        log(
            f"CAPTIONED {global_scene}/{expected_count}: "
            f"part={part}, scene={local_scene}"
        )

    if len(captioned_rows) != expected_count:
        raise RuntimeError("Not all scenes were captioned")

    # Build each part in the exact part/local-scene order.
    part_paths = []
    for part in range(1, parts + 1):
        part_rows = sorted(
            [row for row in captioned_rows if int(row["part"]) == part],
            key=lambda row: int(row["local_scene"]),
        )
        if len(part_rows) != scenes_per_part:
            raise RuntimeError(
                f"Part {part} scene count mismatch: "
                f"expected {scenes_per_part}, found {len(part_rows)}"
            )

        part_videos = [
            safe_path(row["video"], f"Captioned part {part} scene")
            for row in part_rows
        ]
        part_path = PART_DIR / f"part_{part:02d}.mp4"
        info = concat_videos(part_videos, part_path)
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Part {part} resolution mismatch")

        part_paths.append(relative(part_path))
        log(f"MERGED PART {part}/{parts}: {info['duration']:.2f}s")

    # The final video is assembled from the captioned scenes in global order.
    final_scene_paths = [
        safe_path(row["video"], "Final captioned scene")
        for row in captioned_rows
    ]
    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(final_scene_paths, final_path)

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
    if opposite.exists():
        opposite.unlink()

    FINAL_SRT.write_text(
        "\n".join(srt_entries).rstrip() + "\n",
        encoding="utf-8",
    )

    final_data = dict(render_data)
    final_data.update(
        {
            "status": "completed",
            "format": mode,
            "captions_burned": True,
            "completed_scenes": captioned_rows,
            "parts": part_paths,
            "final_video": relative(final_path),
            "alias": relative(alias_path),
            "duration": final_info["duration"],
            "resolution": {
                "width": final_info["width"],
                "height": final_info["height"],
            },
            "caption_font": font_name,
            "caption_font_size": font_size,
            "caption_manifest": relative(CAPTION_MANIFEST),
            "final_srt": relative(FINAL_SRT),
            "caption_progress": {
                "completed_scenes": expected_count,
                "expected_scenes": expected_count,
                "last_global_scene": expected_count,
            },
        }
    )
    write_json(RENDER_MANIFEST, final_data)

    log("=" * 58)
    log("CAPTIONS BURNED AND FINAL VIDEO REBUILT")
    log(f"FORMAT: {mode}")
    log(f"SCENES: {expected_count}")
    log(f"PARTS: {parts}")
    log(f"FINAL: {relative(final_path)}")
    log(f"ALIAS: {relative(alias_path)}")
    log(f"SRT: {relative(FINAL_SRT)}")
    log(f"FONT: {font_name} / {font_size}")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PIPELINE FAILED:", str(exc))
        sys.exit(1)
