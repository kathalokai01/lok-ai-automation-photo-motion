#!/usr/bin/env python3
"""Validate scene clips and assemble the final Photo Motion video."""

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format

OUTPUT = ROOT / "output"
MOTION_DIR = OUTPUT / "photo_motion"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"
NARRATION_DIR = OUTPUT / "narration"

MANIFEST_FILE = MOTION_DIR / "photo_motion_manifest.json"
PART_SIZE = 4

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"
}

AUDIO_DIRECTORIES = {
    "music": [
        OUTPUT / "audio" / "music",
        OUTPUT / "music",
        ROOT / "assets" / "audio" / "music",
        ROOT / "Input" / "audio" / "music",
    ],
    "ambient": [
        OUTPUT / "audio" / "ambient",
        ROOT / "assets" / "audio" / "ambient",
    ],
    "sfx": [
        OUTPUT / "audio" / "sfx",
        OUTPUT / "sfx",
        ROOT / "assets" / "audio" / "sfx",
    ],
}


def log(*args):
    print(*args, flush=True)


def run_command(command):
    log("\n$", " ".join(map(str, command)))
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.stdout:
        log(result.stdout.rstrip())

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}."
        )


def probe_media(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "format=duration,size:"
            "stream=codec_type,codec_name,width,height",
            "-of", "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot inspect {path}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration") or 0)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid media information for {path}: {exc}"
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    return data, duration


def validate_video_file(path):
    if not path.is_file() or path.stat().st_size < 1000:
        raise RuntimeError(f"Video is missing or too small: {path}")

    data, duration = probe_media(path)
    streams = [
        item for item in data.get("streams", [])
        if item.get("codec_type") == "video"
    ]

    if not streams:
        raise RuntimeError(f"No video stream found: {path}")

    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)

    if width < 64 or height < 64:
        raise RuntimeError(
            f"Invalid video resolution for {path.name}: {width}x{height}"
        )

    return data, duration, width, height


def write_concat_list(paths, destination):
    lines = []

    for path in paths:
        resolved = str(path.resolve())

        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Media paths cannot contain newlines.")

        safe_path = resolved.replace("'", "'\\''")
        lines.append(f"file '{safe_path}'")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def concatenate_videos(ffmpeg, paths, destination, list_file):
    if not paths:
        raise RuntimeError("No video clips were supplied.")

    reference = None
    for path in paths:
        _, _, width, height = validate_video_file(path)
        if reference is None:
            reference = (width, height)
        elif (width, height) != reference:
            raise RuntimeError(
                f"Cannot concatenate mixed resolutions: {path.name} "
                f"is {width}x{height}, expected {reference[0]}x{reference[1]}."
            )

    write_concat_list(paths, list_file)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    temporary = destination.with_name(
        destination.stem + ".temporary.mp4"
    )
    temporary.unlink(missing_ok=True)

    run_command([
        ffmpeg,
        "-hide_banner", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-map", "0:v:0",
        "-an",
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(temporary),
    ])

    validate_video_file(temporary)
    temporary.replace(destination)
    validate_video_file(destination)


def discover_narration():
    if not NARRATION_DIR.exists():
        return []

    files = sorted(
        (
            path for path in NARRATION_DIR.rglob("*")
            if path.is_file()
            and path.suffix.lower() in AUDIO_EXTENSIONS
            and path.name.lower() not in {
                "combined_narration.m4a",
                "audio_concat.txt",
            }
            and path.stat().st_size > 0
        ),
        key=lambda path: path.as_posix().lower(),
    )

    preferred = {
        "final",
        "narration",
        "full_narration",
        "voiceover",
    }

    for path in files:
        if path.stem.lower() in preferred:
            return [path]

    return files


def discover_optional_audio(kind):
    found = []

    for directory in AUDIO_DIRECTORIES[kind]:
        if not directory.exists():
            continue

        for path in directory.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in AUDIO_EXTENSIONS
                and path.stat().st_size > 0
                and path not in found
            ):
                found.append(path)

    return sorted(found, key=lambda path: path.as_posix().lower())


def validate_audio_file(path):
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Audio file is missing or empty: {path}")

    data, duration = probe_media(path)
    if not any(
        stream.get("codec_type") == "audio"
        for stream in data.get("streams", [])
    ):
        raise RuntimeError(f"No audio stream found: {path}")

    return duration


def concatenate_audio(ffmpeg, files):
    if not files:
        return None

    for path in files:
        validate_audio_file(path)

    if len(files) == 1:
        return files[0]

    list_file = NARRATION_DIR / "audio_concat.txt"
    combined = NARRATION_DIR / "combined_narration.m4a"
    temporary = NARRATION_DIR / "combined_narration.temporary.m4a"

    write_concat_list(files, list_file)
    temporary.unlink(missing_ok=True)

    run_command([
        ffmpeg,
        "-hide_banner", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-vn",
        "-c:a", "aac",
        "-b:a", "192k",
        str(temporary),
    ])

    validate_audio_file(temporary)
    temporary.replace(combined)
    validate_audio_file(combined)
    return combined


def mix_audio(ffmpeg, silent_video, final_video, duration,
              voice, music, ambient, sfx):
    tracks = []

    if voice:
        tracks.append(("voice", voice))

    if music:
        tracks.append(("music", music[0]))

    if ambient:
        tracks.append(("ambient", ambient[0]))

    if sfx:
        tracks.append(("sfx", sfx[0]))

    if not tracks:
        log("WARNING: No audio files found; final video will be silent.")
        shutil.copy2(silent_video, final_video)
        validate_video_file(final_video)
        return

    for kind, path in tracks:
        validate_audio_file(path)

    command = [
        ffmpeg,
        "-hide_banner", "-y",
        "-i", str(silent_video),
    ]

    for kind, path in tracks:
        if kind in {"music", "ambient"}:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(path)])

    filters = []
    labels = []

    for index, (kind, _path) in enumerate(tracks, start=1):
        label = f"audio{index}"
        source = f"[{index}:a]"

        if kind == "voice":
            filters.append(
                f"{source}aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"apad,atrim=duration={duration:.6f},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
        else:
            volume = {
                "music": 0.15,
                "ambient": 0.10,
                "sfx": 0.25,
            }[kind]

            filters.append(
                f"{source}aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={volume},"
                f"atrim=duration={duration:.6f},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )

        labels.append(f"[{label}]")

    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:"
        "dropout_transition=2,"
        f"atrim=duration={duration:.6f},"
        "alimiter=limit=0.95,"
        "aresample=44100[aout]"
    )

    temporary = final_video.with_name(
        final_video.stem + ".temporary.mp4"
    )
    temporary.unlink(missing_ok=True)

    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0",
        "-map", "[aout]",
        "-t", f"{duration:.6f}",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        str(temporary),
    ])

    run_command(command)
    validate_video_file(temporary)
    temporary.replace(final_video)
    validate_video_file(final_video)


def load_scene_clips():
    if not MANIFEST_FILE.is_file():
        raise RuntimeError(
            "Photo Motion manifest is missing. "
            "Run scripts/photo_motion.py first."
        )

    try:
        manifest = json.loads(
            MANIFEST_FILE.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read motion manifest: {exc}"
        ) from exc

    if not isinstance(manifest, list) or not manifest:
        raise RuntimeError("Photo Motion manifest is empty or invalid.")

    clips = []
    scene_numbers = []

    for index, item in enumerate(manifest, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Manifest item {index} is invalid.")

        try:
            number = int(item.get("global_scene", item.get("scene", 0)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene number in manifest item {index}."
            ) from exc

        if number < 1:
            raise RuntimeError(f"Invalid scene number: {number}")

        video_value = item.get("video")
        if not video_value:
            raise RuntimeError(f"Scene {number} has no video path.")

        video_path = Path(str(video_value))
        if not video_path.is_absolute():
            video_path = ROOT / video_path
        video_path = video_path.resolve()

        if not video_path.is_relative_to(ROOT):
            raise RuntimeError(
                f"Scene {number} path escapes repository: {video_value}"
            )

        validate_video_file(video_path)
        clips.append(video_path)
        scene_numbers.append(number)

    expected = list(range(1, len(clips) + 1))
    if scene_numbers != expected:
        raise RuntimeError(
            "Manifest scene numbers must be sequential from 1. "
            f"Found: {scene_numbers}"
        )

    return clips


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe must both be installed.")

    for directory in (MOTION_DIR, PARTS_DIR, VIDEOS_DIR, NARRATION_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    clips = load_scene_clips()

    reference_resolution = None
    for clip in clips:
        _, _, width, height = validate_video_file(clip)
        resolution = (width, height)
        if reference_resolution is None:
            reference_resolution = resolution
        elif resolution != reference_resolution:
            raise RuntimeError(
                "Scene clips have inconsistent resolutions."
            )

    expected_resolution = (1280, 720) if mode == "full" else (720, 1280)
    if reference_resolution != expected_resolution:
        raise RuntimeError(
            f"FORMAT={mode} expects {expected_resolution[0]}x"
            f"{expected_resolution[1]}, but scene clips are "
            f"{reference_resolution[0]}x{reference_resolution[1]}."
        )

    for old in PARTS_DIR.glob("part_*.mp4"):
        old.unlink()

    silent_video = VIDEOS_DIR / "video_without_audio.mp4"
    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    short_alias = VIDEOS_DIR / "katha_lok_ai_short.mp4"
    full_alias = VIDEOS_DIR / "katha_lok_ai_full.mp4"

    for old in (silent_video, final_video, short_alias, full_alias):
        old.unlink(missing_ok=True)

    for start in range(0, len(clips), PART_SIZE):
        group = clips[start:start + PART_SIZE]
        part_number = start // PART_SIZE + 1

        concatenate_videos(
            ffmpeg,
            group,
            PARTS_DIR / f"part_{part_number:02d}.mp4",
            MOTION_DIR / f"concat_part_{part_number:02d}.txt",
        )

    concatenate_videos(
        ffmpeg,
        clips,
        silent_video,
        MOTION_DIR / "concat_final.txt",
    )

    _, video_duration, _, _ = validate_video_file(silent_video)

    narration_files = discover_narration()
    voice = concatenate_audio(ffmpeg, narration_files)

    music = discover_optional_audio("music")
    ambient = discover_optional_audio("ambient")
    sfx = discover_optional_audio("sfx")

    log("\nAudio inventory")
    log("Narration:", voice or "not found")
    log("Music:", music[0] if music else "not found")
    log("Ambient:", ambient[0] if ambient else "not found")
    log("SFX:", sfx[0] if sfx else "not found")

    mix_audio(
        ffmpeg=ffmpeg,
        silent_video=silent_video,
        final_video=final_video,
        duration=video_duration,
        voice=voice,
        music=music,
        ambient=ambient,
        sfx=sfx,
    )

    final_data, final_duration, final_width, final_height = (
        validate_video_file(final_video)
    )

    if (final_width, final_height) != expected_resolution:
        raise RuntimeError("Final video resolution does not match FORMAT.")

    if abs(final_duration - video_duration) > 1.0:
        raise RuntimeError(
            "Final duration differs from scene timeline: "
            f"video={video_duration:.2f}s, final={final_duration:.2f}s"
        )

    if voice and not any(
        stream.get("codec_type") == "audio"
        for stream in final_data.get("streams", [])
    ):
        raise RuntimeError("Narration was expected but final audio is missing.")

    alias = full_alias if mode == "full" else short_alias
    shutil.copy2(final_video, alias)
    validate_video_file(alias)

    log("\n" + "=" * 55)
    log("RENDER SUCCESS")
    log("Format:", mode)
    log("Scenes:", len(clips))
    log("Parts:", len(list(PARTS_DIR.glob("part_*.mp4"))))
    log("Final video:", final_video.relative_to(ROOT))
    log("Format output:", alias.relative_to(ROOT))
    log("Resolution:", f"{final_width}x{final_height}")
    log("Duration:", f"{final_duration:.2f} seconds")
    log("Size:", f"{final_video.stat().st_size / 1048576:.2f} MB")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("FINAL RENDER CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"FINAL RENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)