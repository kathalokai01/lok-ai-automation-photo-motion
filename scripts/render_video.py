#!/usr/bin/env python3
"""
KATHA LOK AI — PHOTO MOTION FINAL VIDEO RENDERER

Pipeline:

output/photo_motion/scene_01.mp4
output/photo_motion/scene_02.mp4
...
        ↓
ordered scene videos
        ↓
part videos
        ↓
narration audio
        ↓
final MP4

NO Wan2GP
NO Colab
NO paid video API

FFmpeg only.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ============================================================
# PATHS
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

OUTPUT = ROOT / "output"

PHOTO_MOTION_DIR = OUTPUT / "photo_motion"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"
NARRATION_DIR = OUTPUT / "narration"
SCENES_DIR = OUTPUT / "scenes"

MANIFEST = (
    PHOTO_MOTION_DIR /
    "photo_motion_manifest.json"
)

FINAL_VIDEO = (
    VIDEOS_DIR /
    "katha_lok_ai_final.mp4"
)

CONCAT_FILE = (
    PHOTO_MOTION_DIR /
    "concat.txt"
)

FINAL_CONCAT_FILE = (
    PHOTO_MOTION_DIR /
    "final_concat.txt"
)


# ============================================================
# CONFIG
# ============================================================

FPS = 24
CRF = 20
PRESET = "medium"

DEFAULT_PART_SIZE = 4


# ============================================================
# HELPERS
# ============================================================

def run(cmd):

    print()
    print("COMMAND:")
    print(" ".join(str(x) for x in cmd))
    print()

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    print(result.stdout)

    if result.returncode != 0:

        raise RuntimeError(
            "FFmpeg command failed "
            f"with exit code {result.returncode}"
        )


def find_ffmpeg():

    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:

        raise RuntimeError(
            "FFmpeg not found."
        )

    return ffmpeg


def natural_number(path):

    digits = re.findall(
        r"\d+",
        Path(path).stem
    )

    if digits:

        return int(digits[-1])

    return 999999


def discover_scene_videos():

    if not PHOTO_MOTION_DIR.exists():

        return []

    files = []

    for path in PHOTO_MOTION_DIR.glob(
        "scene_*.mp4"
    ):

        if path.is_file():

            files.append(path)

    files.sort(
        key=natural_number
    )

    return files


def load_manifest():

    if not MANIFEST.exists():

        return []

    try:

        data = json.loads(
            MANIFEST.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(data, list):

            return data

        if isinstance(data, dict):

            if isinstance(
                data.get("scenes"),
                list
            ):

                return data["scenes"]

            if isinstance(
                data.get("manifest"),
                list
            ):

                return data["manifest"]

        return []

    except Exception as exc:

        print(
            "WARNING: manifest read failed:",
            exc
        )

        return []


def safe_duration(ffprobe, path):

    probe = shutil.which(
        "ffprobe"
    )

    if not probe:

        return 0.0

    cmd = [
        probe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    try:

        return float(
            result.stdout.strip()
        )

    except Exception:

        return 0.0


# ============================================================
# CONCAT FILE
# ============================================================

def create_concat_file(
    paths,
    output_file
):

    lines = []

    for path in paths:

        # FFmpeg concat format.
        # Absolute paths are converted to safe POSIX strings.

        escaped = (
            str(path.resolve())
            .replace("'", "'\\''")
        )

        lines.append(
            f"file '{escaped}'"
        )

    output_file.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8"
    )


# ============================================================
# CONCAT VIDEOS
# ============================================================

def concat_videos(
    ffmpeg,
    paths,
    output
):

    if not paths:

        raise RuntimeError(
            "No videos supplied for concatenation."
        )

    create_concat_file(
        paths,
        CONCAT_FILE
    )

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    cmd = [

        ffmpeg,

        "-y",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        str(CONCAT_FILE),

        "-c",
        "copy",

        "-movflags",
        "+faststart",

        str(output)
    ]

    run(cmd)


# ============================================================
# PART CREATION
# ============================================================

def make_parts(
    ffmpeg,
    scene_videos
):

    PARTS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # Remove old parts
    for old in PARTS_DIR.glob(
        "part_*.mp4"
    ):

        try:
            old.unlink()
        except Exception:
            pass

    parts = []

    for start in range(
        0,
        len(scene_videos),
        DEFAULT_PART_SIZE
    ):

        group = scene_videos[
            start:start + DEFAULT_PART_SIZE
        ]

        part_number = (
            start // DEFAULT_PART_SIZE
        ) + 1

        part_path = (
            PARTS_DIR /
            f"part_{part_number:02d}.mp4"
        )

        print()
        print(
            "=" * 60
        )
        print(
            f"PART {part_number}"
        )
        print(
            "=" * 60
        )

        for item in group:

            print(
                "  ",
                item.name
            )

        concat_videos(
            ffmpeg,
            group,
            part_path
        )

        parts.append(
            part_path
        )

    return parts


# ============================================================
# AUDIO DISCOVERY
# ============================================================

def discover_audio():

    if not NARRATION_DIR.exists():

        return []

    candidates = []

    for extension in [
        "*.mp3",
        "*.wav",
        "*.m4a",
        "*.aac",
        "*.ogg"
    ]:

        candidates.extend(
            NARRATION_DIR.rglob(
                extension
            )
        )

    candidates = [
        p
        for p in candidates
        if p.is_file()
    ]

    candidates.sort(
        key=natural_number
    )

    return candidates


def discover_single_final_audio():

    names = [
        "final.mp3",
        "final.wav",
        "narration.mp3",
        "narration.wav",
        "full_narration.mp3",
        "full_narration.wav",
        "voiceover.mp3",
        "voiceover.wav"
    ]

    for name in names:

        path = NARRATION_DIR / name

        if path.exists():

            return path

    return None


# ============================================================
# CREATE AUDIO CONCAT
# ============================================================

def concat_audio(
    ffmpeg,
    audio_files
):

    if not audio_files:

        return None

    audio_concat = (
        NARRATION_DIR /
        "audio_concat.txt"
    )

    lines = []

    for path in audio_files:

        escaped = (
            str(path.resolve())
            .replace("'", "'\\''")
        )

        lines.append(
            f"file '{escaped}'"
        )

    audio_concat.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8"
    )

    audio_output = (
        NARRATION_DIR /
        "combined_narration.m4a"
    )

    cmd = [

        ffmpeg,

        "-y",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        str(audio_concat),

        "-vn",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        str(audio_output)
    ]

    run(cmd)

    return audio_output


# ============================================================
# FINAL VIDEO + AUDIO
# ============================================================

def attach_audio(
    ffmpeg,
    video,
    audio,
    output
):

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    cmd = [

        ffmpeg,

        "-y",

        "-i",
        str(video),

        "-i",
        str(audio),

        "-map",
        "0:v:0",

        "-map",
        "1:a:0",

        "-c:v",
        "copy",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-shortest",

        "-movflags",
        "+faststart",

        str(output)
    ]

    run(cmd)


# ============================================================
# FINAL VIDEO WITHOUT AUDIO
# ============================================================

def copy_final_video(
    ffmpeg,
    video,
    output
):

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    cmd = [

        ffmpeg,

        "-y",

        "-i",
        str(video),

        "-c:v",
        "copy",

        "-an",

        "-movflags",
        "+faststart",

        str(output)
    ]

    run(cmd)


# ============================================================
# CLEAN OLD FINAL FILES
# ============================================================

def clean():

    VIDEOS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    for path in [
        FINAL_VIDEO,
        CONCAT_FILE,
        FINAL_CONCAT_FILE
    ]:

        if path.exists():

            try:
                path.unlink()
            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 68)
    print("KATHA LOK AI — PHOTO MOTION FINAL RENDER")
    print("=" * 68)

    ffmpeg = find_ffmpeg()

    print()
    print("FFmpeg:", ffmpeg)

    clean()

    # --------------------------------------------------------
    # Discover scene videos
    # --------------------------------------------------------

    scene_videos = (
        discover_scene_videos()
    )

    print()
    print(
        "Scene videos found:",
        len(scene_videos)
    )

    if not scene_videos:

        raise RuntimeError(
            "No Photo Motion scene videos found in "
            f"{PHOTO_MOTION_DIR}"
        )

    print()

    for path in scene_videos:

        print(
            "  ",
            path.name
        )

    # --------------------------------------------------------
    # Manifest
    # --------------------------------------------------------

    manifest = load_manifest()

    if manifest:

        print()
        print(
            "Photo Motion manifest loaded:",
            len(manifest)
        )

    # --------------------------------------------------------
    # Scene durations
    # --------------------------------------------------------

    total_video_duration = 0.0

    for path in scene_videos:

        duration = safe_duration(
            ffmpeg,
            path
        )

        total_video_duration += duration

    print()
    print(
        "Total scene-video duration:",
        f"{total_video_duration:.2f}s"
    )

    # --------------------------------------------------------
    # Create parts
    # --------------------------------------------------------

    parts = make_parts(
        ffmpeg,
        scene_videos
    )

    print()
    print(
        "Parts created:",
        len(parts)
    )

    # --------------------------------------------------------
    # Combine all scene videos
    # --------------------------------------------------------

    combined_video = (
        VIDEOS_DIR /
        "video_without_audio.mp4"
    )

    concat_videos(
        ffmpeg,
        scene_videos,
        combined_video
    )

    # --------------------------------------------------------
    # Find narration
    # --------------------------------------------------------

    final_audio = (
        discover_single_final_audio()
    )

    if final_audio:

        print()
        print(
            "Final narration found:",
            final_audio
        )

    else:

        audio_files = discover_audio()

        # Do not accidentally use generated combined file
        audio_files = [
            p
            for p in audio_files
            if p.name !=
            "combined_narration.m4a"
        ]

        print()
        print(
            "Narration files found:",
            len(audio_files)
        )

        if audio_files:

            final_audio = concat_audio(
                ffmpeg,
                audio_files
            )

    # --------------------------------------------------------
    # Attach narration
    # --------------------------------------------------------

    if final_audio and final_audio.exists():

        print()
        print(
            "=" * 68
        )
        print(
            "ATTACHING NARRATION"
        )
        print(
            "=" * 68
        )

        attach_audio(
            ffmpeg,
            combined_video,
            final_audio,
            FINAL_VIDEO
        )

    else:

        print()
        print(
            "WARNING: No narration audio found."
        )

        print(
            "Creating video without audio."
        )

        copy_final_video(
            ffmpeg,
            combined_video,
            FINAL_VIDEO
        )

    # --------------------------------------------------------
    # Verify final file
    # --------------------------------------------------------

    if not FINAL_VIDEO.exists():

        raise RuntimeError(
            "Final video was not created."
        )

    size = (
        FINAL_VIDEO.stat().st_size
    )

    if size <= 0:

        raise RuntimeError(
            "Final video is empty."
        )

    # --------------------------------------------------------
    # Optional aliases
    # --------------------------------------------------------

    format_value = os.environ.get(
        "FORMAT",
        "short"
    ).lower()

    if format_value == "full":

        final_alias = (
            VIDEOS_DIR /
            "katha_lok_ai_full.mp4"
        )

    else:

        final_alias = (
            VIDEOS_DIR /
            "katha_lok_ai_short.mp4"
        )

    if final_alias != FINAL_VIDEO:

        try:

            if final_alias.exists():
                final_alias.unlink()

            shutil.copy2(
                FINAL_VIDEO,
                final_alias
            )

        except Exception as exc:

            print(
                "WARNING: alias creation failed:",
                exc
            )

    # --------------------------------------------------------
    # Final report
    # --------------------------------------------------------

    print()
    print("=" * 68)
    print("PHOTO MOTION FINAL VIDEO READY")
    print("=" * 68)

    print()
    print(
        "Scenes:",
        len(scene_videos)
    )

    print(
        "Parts:",
        len(parts)
    )

    print(
        "Duration:",
        f"{total_video_duration:.2f}s"
    )

    print(
        "Format:",
        format_value
    )

    print(
        "Final video:",
        FINAL_VIDEO
    )

    print(
        "Size:",
        f"{size / (1024 * 1024):.2f} MB"
    )

    print()
    print(
        "STATUS: SUCCESS"
    )

    print("=" * 68)


if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print("=" * 68)
        print("PHOTO MOTION RENDER FAILED")
        print("=" * 68)

        print(
            str(exc)
        )

        sys.exit(1)
