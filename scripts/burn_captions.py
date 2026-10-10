#!/usr/bin/env python3
"""Validate scene timing and burn Hindi, English, or Hinglish captions."""

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"

VIDEO = OUTPUT / "videos" / "katha_lok_ai_final.mp4"
CAPTIONS = OUTPUT / "captions" / "captions.json"
VISUAL_JOBS = OUTPUT / "visuals" / "visual_jobs.json"
MOTION_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
SRT = OUTPUT / "captions" / "final_captions.srt"
TEMP_VIDEO = OUTPUT / "videos" / "katha_lok_ai_captioned.tmp.mp4"


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def run(command):
    print("$", " ".join(map(str, command)), flush=True)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.stdout:
        print(result.stdout, flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}")


def extract_list(data, label, keys):
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next(
            (data.get(key) for key in keys
             if isinstance(data.get(key), list)),
            None,
        )
    else:
        rows = None

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{label} contains no usable entries.")
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{label} contains a non-object entry.")
    return rows


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc
    if result < 1:
        raise RuntimeError(f"{label} must be positive.")
    return result


def scene_key(row, index, *, caption=False):
    """Return (part, local scene, global scene)."""
    try:
        part = positive_int(row.get("part", 1), "part")
        local = positive_int(
            row.get("local_scene", row.get("scene", index)),
            "local scene",
        )
        global_scene = positive_int(
            row.get("global_scene", row.get("scene", index)),
            "global scene",
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid scene identifiers: {row}") from exc

    return part, local, global_scene


def clean_text(value):
    return " ".join(str(value or "").replace("\r", " ").split()).strip()


def extract_captions(data):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Captions are not marked completed.")

    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("captions.json has no captions.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption entry.")
        part = positive_int(row.get("part"), "caption part")
        local = positive_int(row.get("scene"), "caption scene")
        text = clean_text(row.get("text"))
        if not text:
            raise RuntimeError(f"Empty caption at Part {part}, Scene {local}.")
        key = (part, local)
        if key in result:
            raise RuntimeError(f"Duplicate caption for Part {part}, Scene {local}.")
        result[key] = text

    return result


def extract_motion(data):
    rows = extract_list(
        data, "Photo Motion manifest",
        ("scenes", "clips", "jobs", "items"),
    )
    result = {}

    for index, row in enumerate(rows, 1):
        part, local, global_scene = scene_key(row, index)
        try:
            duration = float(row["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid duration in motion row: {row}") from exc

        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(f"Invalid duration for scene {global_scene}.")

        key = (part, local)
        if key in result:
            raise RuntimeError(f"Duplicate motion scene: Part {part}, Scene {local}.")
        result[key] = {"global_scene": global_scene, "duration": duration}

    return result


def build_timeline(jobs_data, motion_data, captions):
    jobs = extract_list(
        jobs_data, "Visual jobs",
        ("jobs", "visual_jobs", "scenes"),
    )

    scenes = []
    seen_keys = set()
    seen_global = set()

    for index, job in enumerate(jobs, 1):
        part, local, global_scene = scene_key(job, index)
        key = (part, local)

        if key in seen_keys or global_scene in seen_global:
            raise RuntimeError(f"Duplicate visual scene: {job}")
        seen_keys.add(key)
        seen_global.add(global_scene)

        if key not in captions:
            raise RuntimeError(f"Caption missing for Part {part}, Scene {local}.")
        if key not in motion_data:
            raise RuntimeError(f"Motion clip missing for Part {part}, Scene {local}.")

        motion = motion_data[key]
        if motion["global_scene"] != global_scene:
            raise RuntimeError(
                f"Scene numbering mismatch for Part {part}, Scene {local}: "
                f"visual={global_scene}, motion={motion['global_scene']}."
            )

        scenes.append({
            "part": part,
            "local_scene": local,
            "global_scene": global_scene,
            "duration": motion["duration"],
            "text": captions[key],
        })

    scenes.sort(key=lambda row: row["global_scene"])
    actual = [row["global_scene"] for row in scenes]
    expected = list(range(1, len(scenes) + 1))

    if actual != expected:
        raise RuntimeError(f"Global scene numbering is not continuous: {actual}")

    if seen_keys != set(captions):
        raise RuntimeError(
            "Caption and visual scene sets differ. "
            f"Caption-only={sorted(set(captions) - seen_keys)}; "
            f"Visual-only={sorted(seen_keys - set(captions))}"
        )

    if seen_keys != set(motion_data):
        raise RuntimeError(
            "Visual and motion scene sets differ. "
            f"Motion-only={sorted(set(motion_data) - seen_keys)}"
        )

    return scenes


def probe(ffprobe, path):
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Cannot inspect {path.name}: {exc}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration in {path.name}.")
    if not any(row.get("codec_type") == "video" for row in streams):
        raise RuntimeError(f"No video stream in {path.name}.")
    return duration


def srt_time(seconds):
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def make_srt(scenes, video_duration):
    timeline = sum(row["duration"] for row in scenes)
    tolerance = max(2.0, video_duration * 0.03)

    if abs(timeline - video_duration) > tolerance:
        raise RuntimeError(
            "Scene timeline does not match final video duration. "
            f"Scenes={timeline:.2f}s; video={video_duration:.2f}s. "
            "Check the video assembly and scene durations before burning captions."
        )

    entries = []
    cursor = 0.0

    for index, scene in enumerate(scenes, 1):
        start = cursor
        end = min(cursor + scene["duration"], video_duration)
        cursor += scene["duration"]

        if end <= start:
            continue

        entries.append(
            f"{index}\n"
            f"{srt_time(start)} --> {srt_time(end)}\n"
            f"{scene['text']}\n"
        )

    if not entries:
        raise RuntimeError("No valid subtitle intervals were generated.")
    return "\n".join(entries) + "\n"


def escape_filter_path(path):
    value = str(path.resolve())
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


def main():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe must be installed.")

    for path in (VIDEO, CAPTIONS, VISUAL_JOBS, MOTION_MANIFEST):
        if not path.is_file():
            raise RuntimeError(f"Required input missing: {path}")
    if VIDEO.stat().st_size < 1000:
        raise RuntimeError("Final video is missing or too small.")

    caption_data = read_json(CAPTIONS)
    scenes = build_timeline(
        read_json(VISUAL_JOBS),
        extract_motion(read_json(MOTION_MANIFEST)),
        extract_captions(caption_data),
    )

    video_duration = probe(ffprobe, VIDEO)
    SRT.parent.mkdir(parents=True, exist_ok=True)
    SRT.write_text(make_srt(scenes, video_duration), encoding="utf-8")

    subtitle_filter = (
        f"subtitles=filename='{escape_filter_path(SRT)}':"
        "force_style='FontName=Noto Sans Devanagari,"
        "FontSize=22,Outline=2,Shadow=0,Alignment=2,MarginV=48'"
    )

    TEMP_VIDEO.unlink(missing_ok=True)
    run([
        ffmpeg, "-hide_banner", "-y",
        "-i", str(VIDEO),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", subtitle_filter,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(TEMP_VIDEO),
    ])

    if not TEMP_VIDEO.is_file() or TEMP_VIDEO.stat().st_size < 1000:
        raise RuntimeError("Captioned MP4 was not created correctly.")

    probe(ffprobe, TEMP_VIDEO)
    TEMP_VIDEO.replace(VIDEO)

    # Keep the format-specific alias in sync with the captioned final video.
    alias = (
        OUTPUT / "videos" / "katha_lok_ai_full.mp4"
        if str(caption_data.get("format", "")).lower() == "full"
        else OUTPUT / "videos" / "katha_lok_ai_short.mp4"
    )
    # Determine alias from the input configuration if captions.json lacks format.
    config_path = ROOT / "Input" / "topic.txt"
    if config_path.is_file():
        for line in config_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and line.split("=", 1)[0].strip().upper() == "FORMAT":
                value = line.split("=", 1)[1].split("#", 1)[0].strip().strip("'\"").lower()
                alias = (
                    OUTPUT / "videos" / "katha_lok_ai_full.mp4"
                    if value in {"full", "long", "landscape", "youtube", "youtube_full", "youtube-long"}
                    else OUTPUT / "videos" / "katha_lok_ai_short.mp4"
                )
                break

    if alias != VIDEO and alias.exists():
        shutil.copy2(VIDEO, alias)
        if alias.stat().st_size != VIDEO.stat().st_size:
            raise RuntimeError("Captioned format alias failed verification.")

    print("=" * 55)
    print("CAPTION BURN-IN: SUCCESS")
    print("Caption mode:", caption_data.get("mode", "unknown"))
    print("Scenes:", len(scenes))
    print("Captions:", len(scenes))
    print("Duration:", f"{video_duration:.2f}s")
    print("Final MP4:", VIDEO.relative_to(ROOT))
    print("Subtitle file:", SRT.relative_to(ROOT))
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CAPTION BURN-IN FAILED: {exc}", file=sys.stderr)
        sys.exit(1)