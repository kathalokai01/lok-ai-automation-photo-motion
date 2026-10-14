
#!/usr/bin/env python3
"""Resume-safe caption burning, scene merging and final SRT generation."""

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

VERSION = "burn-captions-v19-no-text-truncation"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.0


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_data(data):
    return hashlib.sha256(
        json.dumps(
            data, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path missing")

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


def run(command, label, timeout=300):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{label} failed: {result.stderr[-2000:]}"
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
        f"Inspect {path.name}",
    )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid media metadata: {path.name}"
        ) from exc

    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"),
        None,
    )

    if duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")

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


def resolve_font(family):
    if not shutil.which("fc-match"):
        raise RuntimeError("fontconfig fc-match is required")

    result = run(
        ["fc-match", "-f", "%{file}\n", family],
        f"Resolve caption font {family}",
    )
    candidates = result.stdout.strip().splitlines()
    if not candidates:
        raise RuntimeError(f"No font found for {family!r}")

    path = Path(candidates[0]).resolve()
    if not path.is_file():
        raise RuntimeError(f"Font file not found: {path}")
    return path


def load_caption_mapping(parts, scenes):
    data = read_json(CAPTION_MANIFEST)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest missing or incomplete")

    mapping = {}
    for row in get_rows(data, "captions"):
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        text = str(row.get("text", "")).strip()
        if not text:
            raise RuntimeError(f"Empty caption: {key}")
        if key in mapping:
            raise RuntimeError(f"Duplicate caption: {key}")
        mapping[key] = text

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


def load_audio_mapping(parts, scenes):
    data = read_json(AUDIO_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("Narration audio manifest missing")

    mapping = {}
    for row in get_rows(data, "jobs", "audio_jobs"):
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration incomplete: {key}")
        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")

        path = safe_file(
            find_path(
                row, ("output", "audio_path", "path", "file", "audio")
            ),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio: {key}")

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


def wrap_caption(text, max_chars=38):
    """Wrap text for readability without deleting any words."""
    clean = re.sub(r"\s+", " ", str(text)).strip()
    if not clean:
        return ""

    words = clean.split()
    lines = []
    current = ""

    for word in words:
        # Preserve long words intact rather than slicing them.
        candidate = word if not current else current + " " + word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    # No truncation: every input word is retained.
    return r"\N".join(lines)


def srt_time(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def escape_filter_path(path):
    value = str(path.resolve()).replace("\\", "/")
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


def estimate_frame_luma(video, timestamp):
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
    if result.returncode != 0 or not result.stdout:
        log("WARNING: brightness sampling failed; using dark style")
        return 64
    return int(result.stdout[0])


def choose_contrast(video, duration):
    luma = estimate_frame_luma(video, duration / 2.0)
    if luma >= 145:
        return {
            "average_luma": luma,
            "mode": "light-background",
            "primary": "&H00000000",
            "outline": "&H00FFFFFF",
            "back": "&H70FFFFFF",
        }
    return {
        "average_luma": luma,
        "mode": "dark-background",
        "primary": "&H00FFFFFF",
        "outline": "&H00000000",
        "back": "&H90000000",
    }


def caption_filter(srt_path, family, font_path, size, height, contrast):
    font_size = max(12, min(int(size), int(height * 0.045)))
    margin_v = max(32, int(height * 0.055))
    subtitle_file = escape_filter_path(srt_path)
    font_directory = escape_filter_path(font_path.parent)

    style = (
        f"FontName={family},"
        f"FontSize={font_size},"
        f"PrimaryColour={contrast['primary']},"
        f"OutlineColour={contrast['outline']},"
        f"BackColour={contrast['back']},"
        "BorderStyle=3,Outline=2,Shadow=0,Alignment=2,"
        "MarginL=36,MarginR=36,"
        f"MarginV={margin_v}"
    )
    return (
        f"subtitles=filename='{subtitle_file}':"
        f"fontsdir='{font_directory}':"
        f"force_style='{style}'"
    )


def burn_scene(source, output, text, family, font_path, size,
               duration, resolution, contrast):
    width, height = resolution
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary_srt = output.with_suffix(".srt")
    temporary_video = output.with_name(output.stem + ".tmp.mp4")
    temporary_video.unlink(missing_ok=True)

    # Keep all caption text. The subtitle may wrap to more than two lines.
    caption_text = wrap_caption(text).replace(r"\N", "\n")
    temporary_srt.write_text(
        "1\n"
        f"{srt_time(0)} --> {srt_time(duration)}\n"
        f"{caption_text}\n",
        encoding="utf-8",
    )

    filter_value = caption_filter(
        temporary_srt, family, font_path, size, height, contrast
    )

    try:
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", filter_value,
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
            f"Burn captions into {output.name}",
            timeout=900,
        )

        info = media_info(temporary_video)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Captioned scene missing audio/video: {output.name}")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Captioned resolution mismatch: {output.name}")
        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Captioned duration mismatch: {output.name}")

        temporary_video.replace(output)
    finally:
        temporary_srt.unlink(missing_ok=True)
        temporary_video.unlink(missing_ok=True)


def concat_videos(paths, output):
    if not paths:
        raise RuntimeError(f"No videos supplied for {output.name}")

    output.parent.mkdir(parents=True, exist_ok=True)
    list_file = output.with_suffix(".concat.txt")
    temporary = output.with_name(output.stem + ".tmp.mp4")

    lines = []
    for path in paths:
        resolved = str(path.resolve())
        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Unexpected newline in video path")
        lines.append("file '" + resolved.replace("'", "'\\''") + "'")

    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.unlink(missing_ok=True)

    try:
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(temporary),
            ],
            f"Merge {output.name}",
            timeout=1800,
        )
        info = media_info(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Merged video missing audio/video: {output.name}")
        temporary.replace(output)
    finally:
        list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def build_final_srt(captions, audio, rendered, parts, scenes):
    lines = []
    cue_number = 1
    cursor = 0.0

    for part in range(1, parts + 1):
        for scene in range(1, scenes + 1):
            key = (part, scene)
            duration = min(
                rendered[key]["duration"],
                audio[key]["duration"],
            )
            if duration <= 0:
                raise RuntimeError(f"Invalid caption duration: {key}")

            lines.extend([
                str(cue_number),
                f"{srt_time(cursor)} --> {srt_time(cursor + duration)}",
                wrap_caption(captions[key]).replace(r"\N", "\n"),
                "",
            ])
            cue_number += 1
            cursor += rendered[key]["duration"]

    FINAL_SRT.parent.mkdir(parents=True, exist_ok=True)
    temporary = FINAL_SRT.with_suffix(".srt.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(FINAL_SRT)


def main():
    for command in ("ffmpeg", "ffprobe", "fc-match"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")

    resolution = (1280, 720) if mode == "full" else (720, 1280)
    family = get_caption_font(config)
    configured_size = get_caption_font_size(config)

    if str(configured_size).strip().lower() == "auto":
        font_size = 32 if mode == "full" else 46
    else:
        font_size = int(configured_size)

    if not 8 <= font_size <= 160:
        raise RuntimeError("Caption font size outside safe limits")

    font_path = resolve_font(family)
    font_hash = digest_file(font_path)
    captions = load_caption_mapping(parts, scenes)
    audio = load_audio_mapping(parts, scenes)

    manifest = read_json(RENDER_MANIFEST)
    if not isinstance(manifest, dict):
        raise RuntimeError("Render manifest missing")

    rows = get_rows(manifest, "completed_scenes")
    expected_count = parts * scenes
    if len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; found {len(rows)}"
        )

    render_rows = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid rendered scene row {index}")

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
            raise RuntimeError(f"Rendered scene order mismatch at row {index}")
        render_rows[(part, scene)] = row

    progress = read_json(PROGRESS_FILE, {})
    cached = progress.get("scenes", {}) if isinstance(progress, dict) else {}
    if not isinstance(cached, dict):
        cached = {}

    captioned_dir = OUT / "scene_renders" / "captioned"
    captioned_dir.mkdir(parents=True, exist_ok=True)
    completed = {}

    log("CAPTION FONT:", family)
    log("FONT SIZE:", font_size)
    log("ADAPTIVE CONTRAST: enabled")
    log("CAPTION TRUNCATION: disabled")

    for index in range(1, expected_count + 1):
        part = (index - 1) // scenes + 1
        scene = (index - 1) % scenes + 1
        key = (part, scene)
        row = render_rows[key]

        source = safe_file(
            find_path(
                row,
                ("source_rendered_video", "source_video", "video", "output"),
            ),
            f"Source scene {key}",
            MIN_VIDEO_BYTES,
        )
        source_info = media_info(source)

        if not source_info["has_video"] or not source_info["has_audio"]:
            raise RuntimeError(f"Source scene missing audio/video: {key}")
        if (source_info["width"], source_info["height"]) != resolution:
            raise RuntimeError(f"Source resolution mismatch: {key}")

        duration = min(source_info["duration"], audio[key]["duration"])
        if duration <= 0:
            raise RuntimeError(f"Invalid source duration: {key}")

        contrast = choose_contrast(source, duration)
        target = captioned_dir / f"part_{part:02d}_scene_{scene:03d}.mp4"

        signature = digest_data({
            "version": VERSION,
            "source_sha256": digest_file(source),
            "audio_sha256": audio[key]["sha256"],
            "caption": captions[key],
            "font_family": family,
            "font_path": str(font_path),
            "font_sha256": font_hash,
            "font_size": font_size,
            "duration": round(duration, 4),
            "resolution": resolution,
            "contrast": contrast,
        })

        old = cached.get(f"{part}:{scene}", {})
        reused = False

        if target.is_file() and isinstance(old, dict):
            try:
                info = media_info(target)
                reused = (
                    old.get("signature") == signature
                    and old.get("sha256") == digest_file(target)
                    and info["has_video"]
                    and info["has_audio"]
                    and (info["width"], info["height"]) == resolution
                    and abs(info["duration"] - duration) <= DURATION_TOLERANCE
                )
            except Exception:
                reused = False

        if not reused:
            burn_scene(
                source, target, captions[key], family, font_path,
                font_size, duration, resolution, contrast,
            )

        target_hash = digest_file(target)
        target_info = media_info(target)

        completed[key] = {
            "part": part,
            "scene": scene,
            "global_scene": index,
            "status": "completed",
            "source_rendered_video": relative(source),
            "video": relative(target),
            "duration": round(target_info["duration"], 4),
            "captions_burned": True,
            "signature": signature,
            "sha256": target_hash,
            "cache_reused": reused,
            "caption_contrast": contrast,
        }

        cached[f"{part}:{scene}"] = {
            "signature": signature,
            "sha256": target_hash,
            "path": relative(target),
            "caption_contrast": contrast,
        }

        write_json(PROGRESS_FILE, {
            "status": "in_progress",
            "version": VERSION,
            "completed_count": len(completed),
            "total_scenes": expected_count,
            "scenes": cached,
        })

        log(
            f"{'REUSED' if reused else 'BURNED'} "
            f"{index}/{expected_count}: part={part} scene={scene}; "
            f"contrast={contrast['mode']}"
        )

    parts_dir = OUT / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_paths = []

    for part in range(1, parts + 1):
        scene_paths = [
            ROOT / completed[(part, scene)]["video"]
            for scene in range(1, scenes + 1)
        ]
        part_path = parts_dir / f"part_{part:02d}.mp4"
        concat_videos(scene_paths, part_path)
        part_paths.append(part_path)
        log("PART READY:", relative(part_path))

    videos_dir = OUT / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    final_path = videos_dir / "katha_lok_ai_final.mp4"
    concat_videos(part_paths, final_path)

    alias_name = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    alias_path = videos_dir / alias_name
    shutil.copy2(final_path, alias_path)

    opposite = videos_dir / (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite.unlink(missing_ok=True)

    build_final_srt(captions, audio, completed, parts, scenes)
    final_info = media_info(final_path)

    final_manifest = {
        **manifest,
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "captions_burned": True,
        "caption_style": "adaptive-contrast;no-text-truncation;bottom-centered",
        "caption_font": family,
        "caption_font_file": str(font_path),
        "caption_font_size": font_size,
        "completed_count": expected_count,
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
    log("SRT:", relative(FINAL_SRT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION BURN FAILED:", str(exc))
        sys.exit(1)
