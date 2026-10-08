#!/usr/bin/env python3
"""Katha Lok AI Photo Motion final renderer."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format

OUTPUT = ROOT / "output"
PHOTO_MOTION = OUTPUT / "photo_motion"
PARTS = OUTPUT / "parts"
VIDEOS = OUTPUT / "videos"
NARRATION = OUTPUT / "narration"

PART_SIZE = 4


def run(cmd):
    print("\n$", " ".join(map(str, cmd)))
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True
    )
    print(result.stdout)
    if result.returncode:
        raise RuntimeError(f"Command failed: {result.returncode}")


def number(path):
    matches = re.findall(r"\d+", path.stem)
    return int(matches[-1]) if matches else 999999


def concat_file(paths, destination):
    lines = []
    for path in paths:
        safe_path = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe_path}'")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_videos(ffmpeg, paths, destination, list_file):
    if not paths:
        raise RuntimeError("No videos supplied for concatenation.")

    concat_file(paths, list_file)
    destination.parent.mkdir(parents=True, exist_ok=True)

    run([
        ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(destination),
    ])


def find_audio():
    if not NARRATION.exists():
        return []

    extensions = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
    files = [
        p for p in NARRATION.rglob("*")
        if p.is_file()
        and p.suffix.lower() in extensions
        and p.name != "combined_narration.m4a"
    ]

    for preferred in ("final", "narration", "full_narration", "voiceover"):
        for path in files:
            if path.stem.lower() == preferred:
                return [path]

    return sorted(files, key=number)


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found.")

    for directory in (PHOTO_MOTION, PARTS, VIDEOS):
        directory.mkdir(parents=True, exist_ok=True)

    clips = sorted(
        [p for p in PHOTO_MOTION.glob("scene_*.mp4") if p.is_file()],
        key=number,
    )
    if not clips:
        raise RuntimeError(f"No scene videos found in {PHOTO_MOTION}")

    # Clear old parts and aliases to avoid stale files.
    for old in PARTS.glob("part_*.mp4"):
        old.unlink()

    short_alias = VIDEOS / "katha_lok_ai_short.mp4"
    full_alias = VIDEOS / "katha_lok_ai_full.mp4"
    for old in (short_alias, full_alias):
        if old.exists():
            old.unlink()

    # Create intermediate part videos.
    for start in range(0, len(clips), PART_SIZE):
        group = clips[start:start + PART_SIZE]
        part_number = start // PART_SIZE + 1
        concat_videos(
            ffmpeg,
            group,
            PARTS / f"part_{part_number:02d}.mp4",
            PHOTO_MOTION / "concat_part.txt",
        )

    silent_video = VIDEOS / "video_without_audio.mp4"
    concat_videos(
        ffmpeg,
        clips,
        silent_video,
        PHOTO_MOTION / "concat_final.txt",
    )

    audio_files = find_audio()
    final_audio = None

    if len(audio_files) == 1:
        final_audio = audio_files[0]
    elif len(audio_files) > 1:
        audio_list = NARRATION / "audio_concat.txt"
        concat_file(audio_files, audio_list)
        final_audio = NARRATION / "combined_narration.m4a"

        run([
            ffmpeg, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(audio_list),
            "-vn",
            "-c:a", "aac",
            "-b:a", "192k",
            str(final_audio),
        ])

    final_video = VIDEOS / "katha_lok_ai_final.mp4"
    if final_video.exists():
        final_video.unlink()

    if final_audio and final_audio.exists():
        run([
            ffmpeg, "-y",
            "-i", str(silent_video),
            "-i", str(final_audio),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(final_video),
        ])
    else:
        print("WARNING: narration not found; output will be silent.")
        shutil.copy2(silent_video, final_video)

    if not final_video.is_file() or final_video.stat().st_size <= 0:
        raise RuntimeError("Final video missing or empty.")

    # Create only the alias matching the requested format.
    alias = full_alias if mode == "full" else short_alias
    shutil.copy2(final_video, alias)

    manifest_path = PHOTO_MOTION / "photo_motion_manifest.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            print("Manifest entries:", len(data) if isinstance(data, list) else "unknown")
        except Exception:
            print("WARNING: manifest could not be read.")

    print("=" * 60)
    print("STATUS: SUCCESS")
    print("FORMAT:", mode)
    print("SCENES:", len(clips))
    print("PARTS:", len(list(PARTS.glob("part_*.mp4"))))
    print("FINAL VIDEO:", final_video)
    print("FORMAT OUTPUT:", alias)
    print("SIZE MB:", round(final_video.stat().st_size / (1024 * 1024), 2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print("PHOTO MOTION RENDER FAILED:", exc)
        sys.exit(1)