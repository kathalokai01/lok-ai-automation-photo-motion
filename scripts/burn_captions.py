
#!/usr/bin/env python3
"""Validate scene mapping and burn selected-language captions into the final MP4."""

import json
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
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def run(command):
    print("\n$", " ".join(map(str, command)), flush=True)

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    print(result.stdout, flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}"
        )


def seconds_to_srt(value):
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)

    return (
        f"{hours:02}:{minutes:02}:"
        f"{seconds:02},{millis:03}"
    )


def clean_caption(value):
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ")
    return " ".join(text.split()).strip()


def extract_jobs(data):
    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = (
            data.get("jobs")
            or data.get("visual_jobs")
            or data.get("scenes")
            or []
        )
    else:
        jobs = []

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("visual_jobs.json contains no scene jobs.")

    return jobs


def extract_caption_rows(data):
    if not isinstance(data, dict):
        raise RuntimeError("captions.json must contain a JSON object.")

    if data.get("status") != "completed":
        raise RuntimeError("Captions are not marked completed.")

    rows = data.get("captions")

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("captions.json contains no captions.")

    return rows


def extract_motion_rows(data):
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            "Photo Motion manifest must be a non-empty JSON list."
        )

    result = {}

    for row in data:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid entry in Photo Motion manifest.")

        try:
            number = int(row.get("global_scene", row.get("scene")))
            duration = float(row["duration"])
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError(
                f"Invalid scene number or duration in manifest: {row}"
            ) from exc

        if number < 1 or duration <= 0:
            raise RuntimeError(
                f"Invalid scene number/duration in manifest: {row}"
            )

        if number in result:
            raise RuntimeError(
                f"Duplicate global scene {number} in motion manifest."
            )

        result[number] = duration

    return result


def build_caption_map(rows):
    result = {}

    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption entry.")

        try:
            part = int(row["part"])
            local_scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Caption is missing numeric part/scene fields: {row}"
            ) from exc

        if part < 1 or local_scene < 1:
            raise RuntimeError(
                f"Invalid caption scene identifiers: {row}"
            )

        text = clean_caption(row.get("text", ""))

        if not text:
            raise RuntimeError(
                f"Empty caption for Part {part}, Scene {local_scene}."
            )

        key = (part, local_scene)

        if key in result:
            raise RuntimeError(
                f"Duplicate caption for Part {part}, Scene {local_scene}."
            )

        result[key] = text

    return result


def build_scene_timeline(jobs, caption_map, duration_map):
    scenes = []
    seen_global = set()
    seen_local = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(
                f"Invalid visual job at position {index}."
            )

        try:
            global_scene = int(
                job.get("global_scene", job.get("scene", index))
            )
            part = int(job["part"])
            local_scene = int(job.get("local_scene", job.get("scene", index)))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene identifiers in visual job {index}: {job}"
            ) from exc

        if global_scene < 1 or part < 1 or local_scene < 1:
            raise RuntimeError(
                f"Scene identifiers must be positive: {job}"
            )

        if global_scene in seen_global:
            raise RuntimeError(
                f"Duplicate global scene number {global_scene}."
            )

        key = (part, local_scene)

        if key in seen_local:
            raise RuntimeError(
                f"Duplicate Part {part}, Scene {local_scene}."
            )

        seen_global.add(global_scene)
        seen_local.add(key)

        if key not in caption_map:
            raise RuntimeError(
                f"Caption missing for Part {part}, Scene {local_scene}."
            )

        duration = duration_map.get(global_scene)

        if duration is None:
            try:
                duration = float(job.get("duration", 0))
            except (TypeError, ValueError):
                duration = 0

        if duration <= 0:
            raise RuntimeError(
                f"Duration missing for global scene {global_scene}."
            )

        scenes.append({
            "global_scene": global_scene,
            "part": part,
            "local_scene": local_scene,
            "duration": duration,
            "text": caption_map[key],
        })

    scenes.sort(key=lambda item: item["global_scene"])

    expected = list(range(1, len(scenes) + 1))
    actual = [item["global_scene"] for item in scenes]

    if actual != expected:
        raise RuntimeError(
            "Global scene numbers must be continuous from 1. "
            f"Found: {actual}"
        )

    if len(caption_map) != len(scenes):
        missing = sorted(set(caption_map) - seen_local)
        extra = sorted(seen_local - set(caption_map))

        raise RuntimeError(
            "Caption/job count mismatch. "
            f"Caption count={len(caption_map)}, job count={len(scenes)}. "
            f"Caption-only keys={missing}; job-only keys={extra}"
        )

    return scenes


def probe_duration(ffprobe, video):
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Could not inspect final video: {result.stderr}"
        )

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Could not determine final video duration."
        ) from exc

    if duration <= 0:
        raise RuntimeError("Final video duration is invalid.")

    return duration


def make_srt(scenes, video_duration):
    entries = []
    cursor = 0.0

    for index, scene in enumerate(scenes, start=1):
        start = cursor
        end = cursor + scene["duration"]
        cursor = end

        if start >= video_duration:
            raise RuntimeError(
                f"Scene {scene['global_scene']} starts after the final "
                "video ends. Scene timing and video duration do not match."
            )

        end = min(end, video_duration)

        if end <= start:
            raise RuntimeError(
                f"Invalid subtitle interval for scene {scene['global_scene']}."
            )

        entries.append(
            f"{index}\n"
            f"{seconds_to_srt(start)} --> {seconds_to_srt(end)}\n"
            f"{scene['text']}\n"
        )

    timeline_duration = sum(item["duration"] for item in scenes)

    if abs(timeline_duration - video_duration) > max(2.0, video_duration * 0.03):
        raise RuntimeError(
            "Scene-duration timeline differs from final MP4 duration. "
            f"Scene total={timeline_duration:.3f}s; "
            f"video={video_duration:.3f}s. "
            "Refusing to burn potentially misaligned captions."
        )

    return "\n".join(entries)


def escape_filter_path(path):
    value = str(path.resolve())
    value = value.replace("\\", "\\\\")
    value = value.replace(":", "\\:")
    value = value.replace("'", "\\'")
    value = value.replace(",", "\\,")
    value = value.replace("[", "\\[")
    value = value.replace("]", "\\]")
    return value


def main():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe must be installed.")

    for path in (VIDEO, CAPTIONS, VISUAL_JOBS, MOTION_MANIFEST):
        if not path.is_file():
            raise RuntimeError(f"Required input file missing: {path}")

    if VIDEO.stat().st_size == 0:
        raise RuntimeError(f"Final video is empty: {VIDEO}")

    caption_data = read_json(CAPTIONS)
    jobs_data = read_json(VISUAL_JOBS)
    motion_data = read_json(MOTION_MANIFEST)

    caption_map = build_caption_map(
        extract_caption_rows(caption_data)
    )
    jobs = extract_jobs(jobs_data)
    duration_map = extract_motion_rows(motion_data)

    scenes = build_scene_timeline(jobs, caption_map, duration_map)
    video_duration = probe_duration(ffprobe, VIDEO)
    srt_text = make_srt(scenes, video_duration)

    SRT.parent.mkdir(parents=True, exist_ok=True)
    SRT.write_text(srt_text + "\n", encoding="utf-8")

    # Confirm subtitles contain visible text before encoding.
    if not SRT.read_text(encoding="utf-8").strip():
        raise RuntimeError("Generated subtitle file is empty.")

    subtitle_path = escape_filter_path(SRT)
    subtitle_filter = (
        f"subtitles=filename='{subtitle_path}':"
        "force_style='FontName=Noto Sans Devanagari,"
        "FontSize=22,Outline=2,Shadow=0,"
        "Alignment=2,MarginV=48'"
    )

    TEMP_VIDEO.unlink(missing_ok=True)

    run([
        ffmpeg,
        "-hide_banner",
        "-y",
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

    if not TEMP_VIDEO.is_file() or TEMP_VIDEO.stat().st_size == 0:
        raise RuntimeError("Captioned MP4 was not created.")

    # Verify temporary output before replacing the original final MP4.
    probe_duration(ffprobe, TEMP_VIDEO)

    TEMP_VIDEO.replace(VIDEO)

    print("=" * 55)
    print("CAPTION BURN-IN: SUCCESS")
    print("Caption mode:", caption_data.get("mode", "unknown"))
    print("Global scenes:", len(scenes))
    print("Captions:", len(caption_map))
    print("Duration:", f"{video_duration:.2f}s")
    print("Final MP4:", VIDEO)
    print("Subtitle file:", SRT)
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CAPTION BURN-IN FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
