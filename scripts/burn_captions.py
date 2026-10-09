#!/usr/bin/env python3
"""Burn scene captions into the final Katha Lok AI MP4."""

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
    except Exception as exc:
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

    if result.returncode:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}"
        )


def seconds_to_srt(value):
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def clean_caption(value):
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split()).strip()


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

    caption_rows = caption_data.get("captions", [])
    jobs = (
        jobs_data if isinstance(jobs_data, list)
        else jobs_data.get("jobs", jobs_data.get("visual_jobs", []))
    )

    if not isinstance(caption_rows, list) or not caption_rows:
        raise RuntimeError("captions.json contains no captions.")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("visual_jobs.json contains no scene jobs.")

    if not isinstance(motion_data, list) or not motion_data:
        raise RuntimeError("Photo Motion manifest has no scene records.")

    captions_by_scene = {}
    for row in caption_rows:
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Caption is missing numeric part/scene fields: {row}"
            ) from exc

        text = clean_caption(row.get("text", ""))
        if not text:
            raise RuntimeError(f"Empty caption for Part {key[0]}, Scene {key[1]}.")

        if key in captions_by_scene:
            raise RuntimeError(f"Duplicate caption for Part {key[0]}, Scene {key[1]}.")

        captions_by_scene[key] = text

    duration_by_global = {}
    for row in motion_data:
        try:
            global_scene = int(row["scene"])
            duration = float(row["duration"])
        except (KeyError, TypeError, ValueError):
            continue

        if duration > 0:
            duration_by_global[global_scene] = duration

    normalized_jobs = []
    seen_global = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Invalid visual job at index {index}.")

        try:
            global_scene = int(job.get("global_scene", job.get("scene", index)))
            part = int(job["part"])
            local_scene = int(job.get("local_scene", job.get("scene", index)))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Visual job {index} is missing valid scene identifiers."
            ) from exc

        if global_scene in seen_global:
            raise RuntimeError(f"Duplicate global scene number: {global_scene}")
        seen_global.add(global_scene)

        key = (part, local_scene)
        if key not in captions_by_scene:
            raise RuntimeError(
                f"No caption found for Part {part}, Scene {local_scene}."
            )

        duration = duration_by_global.get(global_scene)
        if not duration:
            try:
                duration = float(job.get("duration", 0))
            except (TypeError, ValueError):
                duration = 0

        if duration <= 0:
            raise RuntimeError(
                f"No valid duration for global scene {global_scene}."
            )

        normalized_jobs.append({
            "global_scene": global_scene,
            "key": key,
            "duration": duration,
            "text": captions_by_scene[key],
        })

    normalized_jobs.sort(key=lambda row: row["global_scene"])

    if len(normalized_jobs) != len(captions_by_scene):
        raise RuntimeError(
            "Caption count does not match visual job count: "
            f"{len(captions_by_scene)} captions, "
            f"{len(normalized_jobs)} visual jobs."
        )

    probe = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(VIDEO),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        video_duration = float(probe.stdout.strip())
    except (ValueError, TypeError):
        video_duration = 0

    if probe.returncode != 0 or video_duration <= 0:
        raise RuntimeError("Could not determine final MP4 duration.")

    SRT.parent.mkdir(parents=True, exist_ok=True)

    cursor = 0.0
    srt_entries = []
    entry_number = 1

    for row in normalized_jobs:
        start = cursor
        end = cursor + row["duration"]
        cursor = end

        # Do not create subtitle entries outside the final video.
        if start >= video_duration:
            break

        end = min(end, video_duration)
        if end <= start:
            continue

        text = row["text"]
        srt_entries.append(
            f"{entry_number}\n"
            f"{seconds_to_srt(start)} --> {seconds_to_srt(end)}\n"
            f"{text}\n"
        )
        entry_number += 1

    if not srt_entries:
        raise RuntimeError("No subtitle entries fit within the final video.")

    SRT.write_text("\n".join(srt_entries), encoding="utf-8")

    # FFmpeg subtitles filter: escape characters in the absolute path.
    subtitle_path = str(SRT.resolve())
    subtitle_path = (
        subtitle_path
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )

    subtitle_filter = (
        f"subtitles=filename='{subtitle_path}':"
        "force_style='FontName=Noto Sans Devanagari,"
        "FontSize=22,Outline=2,Shadow=0,Alignment=2,MarginV=48'"
    )

    TEMP_VIDEO.unlink(missing_ok=True)

    run([
        ffmpeg, "-y",
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

    # Replace final video only after successful encoding.
    TEMP_VIDEO.replace(VIDEO)

    print("=" * 55)
    print("CAPTION BURN-IN: SUCCESS")
    print("Caption mode:", caption_data.get("mode", "unknown"))
    print("Captions:", len(srt_entries))
    print("Video:", VIDEO)
    print("SRT:", SRT)
    print("Note: timings follow scene durations and may need audio-sync refinement.")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CAPTION BURN-IN FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

Commit message:
"Add caption burn-in for final video"