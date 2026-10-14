
#!/usr/bin/env python3
"""Resume-safe caption burning, SRT timing, and final video assembly."""

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

OUTPUT = ROOT / "output"
RENDER_DIR = OUTPUT / "scene_renders"
CAPTION_DIR = OUTPUT / "captions"
CAPTIONED_DIR = RENDER_DIR / "captioned"
PART_DIR = OUTPUT / "parts"
VIDEO_DIR = OUTPUT / "videos"

RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
CAPTION_MANIFEST = CAPTION_DIR / "captions.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"
PROGRESS_MANIFEST = CAPTION_DIR / "burn_progress.json"
FINAL_SRT = CAPTION_DIR / "final_captions.srt"

MIN_BYTES = 1000
DURATION_TOLERANCE = 0.75
VERSION = "burn-captions-v14"


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


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
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file missing: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file too small: {path}")

    return path


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def first_value(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def get_rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def probe(path, require_audio=False):
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
        timeout=60,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    streams = data.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    if video is None or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video or duration: {path.name}")
    if require_audio and audio is None:
        raise RuntimeError(f"Audio stream missing: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": audio is not None,
    }


def audio_duration(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(f"Audio probe failed: {path.name}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid audio duration: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid audio duration: {path.name}")
    if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise RuntimeError(f"No audio stream: {path.name}")

    return duration


def validate_font(font_name):
    executable = shutil.which("fc-match")
    if not executable:
        raise RuntimeError("fontconfig fc-match is not installed")

    result = subprocess.run(
        [executable, "-f", "%{family}", font_name],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"Caption font unavailable: {font_name}")

    actual = result.stdout.strip()
    log(f"CAPTION FONT: requested={font_name}; matched={actual}")
    return actual


def clean_caption(value):
    text = re.sub(r"\s+", " ", str(value or "").replace("\r", " ")).strip()
    if not text:
        raise RuntimeError("Caption text is empty")
    return text


def wrap_caption(text, max_chars=36, max_lines=2):
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
        if max_chars >= 120:
            raise RuntimeError("Caption cannot fit into two lines")
        return wrap_caption(text, max_chars + 12, max_lines)

    return r"\N".join(lines)


def srt_time(seconds):
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def caption_signature(source_hash, text, font, size, video_duration, cue_duration):
    payload = {
        "version": VERSION,
        "source_sha256": source_hash,
        "text": text,
        "font": font,
        "font_size": size,
        "video_duration": round(video_duration, 3),
        "cue_duration": round(cue_duration, 3),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def escape_filter_path(path):
    value = str(path.resolve()).replace("\\", "\\\\")
    for char in (":", "'", ",", "[", "]"):
        value = value.replace(char, "\\" + char)
    return value


def validate_captioned(path, resolution, expected_duration):
    info = probe(path, require_audio=True)

    if (info["width"], info["height"]) != resolution:
        raise RuntimeError(f"Captioned resolution mismatch: {path.name}")
    if abs(info["duration"] - expected_duration) > DURATION_TOLERANCE:
        raise RuntimeError(
            f"Captioned duration mismatch: {path.name}; "
            f"expected {expected_duration:.2f}s, got {info['duration']:.2f}s"
        )
    return info


def burn_one_scene(
    source,
    destination,
    caption_text,
    video_duration,
    cue_duration,
    font_name,
    font_size,
    signature,
    resolution,
):
    sidecar = destination.with_suffix(".caption.json")

    if destination.is_file() and sidecar.is_file():
        try:
            cache = read_json(sidecar)
            if cache.get("signature") == signature:
                info = validate_captioned(
                    destination, resolution, video_duration
                )
                log("RESUME: cached captioned scene", destination.name)
                return info
        except Exception as exc:
            log("CAPTION CACHE INVALID:", destination.name, str(exc))

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    local_srt = destination.with_suffix(".srt")
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    # The caption cue ends with narration, not necessarily with the video.
    local_srt.write_text(
        "1\n"
        f"{srt_time(0)} --> {srt_time(cue_duration)}\n"
        f"{wrap_caption(caption_text)}\n",
        encoding="utf-8",
    )

    subtitle_path = escape_filter_path(local_srt)
    safe_font = re.sub(r"[':\r\n]", "", str(font_name))

    force_style = (
        f"FontName='{safe_font}',"
        f"FontSize={font_size},"
        "Bold=0,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00101010,"
        "BackColour=&H60000000,"
        "BorderStyle=1,"
        "Outline=2,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginL=36,"
        "MarginR=36,"
        "MarginV=64,"
        "WrapStyle=2"
    )

    video_filter = (
        f"subtitles=filename='{subtitle_path}':"
        f"force_style='{force_style}'"
    )

    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", video_filter,
                "-map", "0:v:0",
                "-map", "0:a:0",
                "-t", f"{video_duration:.3f}",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "19",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                "-max_muxing_queue_size", "2048",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )

        if result.returncode:
            raise RuntimeError(
                f"Caption burn failed for {source.name}: "
                f"{result.stderr[-1800:]}"
            )

        if not temporary.is_file() or temporary.stat().st_size < MIN_BYTES:
            raise RuntimeError(f"Captioned output is empty: {temporary.name}")

        info = validate_captioned(temporary, resolution, video_duration)
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
                "font_size": font_size,
                "video_duration": info["duration"],
                "cue_duration": cue_duration,
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
    mapping = {}

    for row in get_rows(data, "jobs", "audio_jobs"):
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).lower() != "completed":
            continue

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        if key in mapping:
            raise RuntimeError(f"Duplicate audio mapping: {key}")

        value = first_value(
            row, ("output", "audio_path", "path", "file", "audio")
        )
        path = safe_path(value, f"Narration {key}", 100)
        mapping[key] = {
            "path": path,
            "duration": audio_duration(path),
        }

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
        if key in mapping:
            raise RuntimeError(f"Duplicate caption mapping: {key}")
        mapping[key] = clean_caption(row.get("text", ""))

    return mapping


def concat_videos(videos, destination):
    if not videos:
        raise RuntimeError(f"No videos to merge for {destination.name}")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    destination.parent.mkdir(parents=True, exist_ok=True)
    concat_file = destination.with_suffix(".concat.txt")
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    lines = []
    for video in videos:
        resolved = str(video.resolve())
        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Newline in video path cannot be concatenated")
        lines.append("file '" + resolved.replace("'", "'\\''") + "'")

    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
            timeout=2400,
            check=False,
        )

        if result.returncode:
            raise RuntimeError(
                f"Video merge failed for {destination.name}: "
                f"{result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_BYTES:
            raise RuntimeError(f"Merged video is empty: {destination.name}")

        probe(temporary, require_audio=True)
        os.replace(temporary, destination)
        return probe(destination, require_audio=True)

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
    if not 16 <= font_size <= 64:
        raise RuntimeError("CAPTION_FONT_SIZE must resolve to 16–64")
    validate_font(font_name)

    render_data = read_json(RENDER_MANIFEST)
    if not isinstance(render_data, dict):
        raise RuntimeError("Render manifest must be a JSON object")
    if render_data.get("status") != "completed":
        raise RuntimeError("Scene rendering must finish before caption burning")
    if render_data.get("format") != mode:
        raise RuntimeError("Input format differs from rendered format")

    scene_rows = render_data.get("completed_scenes")
    if not isinstance(scene_rows, list) or len(scene_rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; "
            f"found {len(scene_rows) if isinstance(scene_rows, list) else 0}"
        )

    captions = load_captions()
    audio = load_audio_jobs()
    expected_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    if set(captions) != expected_keys:
        raise RuntimeError("Caption scene mapping does not match PARTS × SCENES")
    if set(audio) != expected_keys:
        raise RuntimeError("Audio scene mapping does not match PARTS × SCENES")

    resolution = (1280, 720) if mode == "full" else (720, 1280)
    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    CAPTION_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    sorted_rows = sorted(
        scene_rows,
        key=lambda row: int(row.get("global_scene", row.get("scene", 0))),
    )

    normalized = []
    seen = set()

    for index, row in enumerate(sorted_rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid rendered scene row {index}")

        try:
            part = int(row["part"])
            local_scene = int(row["local_scene"])
            global_scene = int(row.get("global_scene", row.get("scene", index)))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

        expected_part = ((index - 1) // scenes_per_part) + 1
        expected_local = ((index - 1) % scenes_per_part) + 1
        key = (part, local_scene)

        if key in seen:
            raise RuntimeError(f"Duplicate rendered scene mapping: {key}")
        seen.add(key)

        if (part, local_scene, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(f"Non-contiguous rendered scene mapping: {key}")

        source_value = first_value(
            row, ("source_rendered_video", "video", "output")
        )
        source = safe_path(source_value, f"Rendered scene {index}", MIN_BYTES)
        source_info = probe(source, require_audio=True)

        if (source_info["width"], source_info["height"]) != resolution:
            raise RuntimeError(f"Scene {index} resolution mismatch")

        narration_duration = audio[key]["duration"]
        cue_duration = min(source_info["duration"], narration_duration)
        if cue_duration <= 0:
            raise RuntimeError(f"Invalid caption cue duration for {key}")

        normalized.append({
            "row": row,
            "part": part,
            "local_scene": local_scene,
            "global_scene": global_scene,
            "key": key,
            "source": source,
            "source_info": source_info,
            "cue_duration": cue_duration,
        })

    progress = {
        "status": "in_progress",
        "version": VERSION,
        "format": mode,
        "expected_scenes": expected_count,
        "completed_scenes": [],
        "completed_count": 0,
    }
    write_json(PROGRESS_MANIFEST, progress)

    captioned_rows = []
    srt_entries = []
    global_offset = 0.0

    for item in normalized:
        row = item["row"]
        part = item["part"]
        local_scene = item["local_scene"]
        global_scene = item["global_scene"]
        key = item["key"]
        source = item["source"]
        source_duration = item["source_info"]["duration"]
        cue_duration = item["cue_duration"]
        text = captions[key]

        destination = CAPTIONED_DIR / f"scene_{global_scene:04d}.mp4"
        signature = caption_signature(
            sha256_file(source),
            text,
            font_name,
            font_size,
            source_duration,
            cue_duration,
        )

        info = burn_one_scene(
            source=source,
            destination=destination,
            caption_text=text,
            video_duration=source_duration,
            cue_duration=cue_duration,
            font_name=font_name,
            font_size=font_size,
            signature=signature,
            resolution=resolution,
        )

        updated = dict(row)
        updated.update({
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
            "duration": info["duration"],
        })
        captioned_rows.append(updated)

        # Avoid backslashes inside f-string expressions on Python 3.11.
        srt_text = wrap_caption(text).replace("\\N", "\n")
        srt_entries.append(
            f"{len(srt_entries) + 1}\n"
            f"{srt_time(global_offset)} --> "
            f"{srt_time(global_offset + cue_duration)}\n"
            f"{srt_text}\n"
        )

        # The next scene begins after the entire preceding scene video.
        global_offset += info["duration"]

        progress["completed_scenes"] = [
            {
                "global_scene": value["global_scene"],
                "part": value["part"],
                "local_scene": value["local_scene"],
                "video": value["video"],
                "signature": value["caption_signature"],
            }
            for value in captioned_rows
        ]
        progress["completed_count"] = len(captioned_rows)
        progress["last_global_scene"] = global_scene
        write_json(PROGRESS_MANIFEST, progress)
        log(f"CAPTIONED {global_scene}/{expected_count}")

    if len(captioned_rows) != expected_count:
        raise RuntimeError("Not all scenes were captioned")

    part_paths = []
    for part in range(1, parts + 1):
        part_rows = sorted(
            [row for row in captioned_rows if row["part"] == part],
            key=lambda row: row["local_scene"],
        )
        if len(part_rows) != scenes_per_part:
            raise RuntimeError(f"Part {part} scene count mismatch")

        videos = [
            safe_path(row["video"], f"Part {part} captioned scene", MIN_BYTES)
            for row in part_rows
        ]
        part_path = PART_DIR / f"part_{part:02d}.mp4"
        part_info = concat_videos(videos, part_path)

        if (part_info["width"], part_info["height"]) != resolution:
            raise RuntimeError(f"Part {part} resolution mismatch")

        part_paths.append(relative(part_path))
        log(f"MERGED PART {part}/{parts}")

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(
        [
            safe_path(row["video"], "Final captioned scene", MIN_BYTES)
            for row in captioned_rows
        ],
        final_path,
    )

    if (final_info["width"], final_info["height"]) != resolution:
        raise RuntimeError("Final video resolution mismatch")

    alias_path = VIDEO_DIR / (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
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
    final_data.update({
        "status": "completed",
        "renderer": VERSION,
        "format": mode,
        "captions_burned": True,
        "expected_scenes": expected_count,
        "completed_count": expected_count,
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
    })
    write_json(RENDER_MANIFEST, final_data)

    progress.update({
        "status": "completed",
        "completed_count": expected_count,
        "final_video": relative(final_path),
        "parts": part_paths,
    })
    write_json(PROGRESS_MANIFEST, progress)

    log("=" * 58)
    log("CAPTIONS BURNED AND FINAL VIDEO REBUILT")
    log("FORMAT:", mode)
    log("SCENES:", expected_count)
    log("PARTS:", parts)
    log("FINAL:", relative(final_path))
    log("ALIAS:", relative(alias_path))
    log("SRT:", relative(FINAL_SRT))
    log("FONT:", font_name, "/", font_size)
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION PIPELINE FAILED:", str(exc))
        sys.exit(1)
