
#!/usr/bin/env python3
"""Katha Lok AI final video renderer with optional audio mixing."""

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
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

MUSIC_DIRS = [
    OUTPUT / "audio" / "music",
    OUTPUT / "music",
    ROOT / "assets" / "audio" / "music",
    ROOT / "Input" / "audio" / "music",
]

AMBIENT_DIRS = [
    OUTPUT / "audio" / "ambient",
    ROOT / "assets" / "audio" / "ambient",
]

SFX_DIRS = [
    OUTPUT / "audio" / "sfx",
    OUTPUT / "sfx",
    ROOT / "assets" / "audio" / "sfx",
]


def run(cmd):
    print("\n$", " ".join(map(str, cmd)), flush=True)
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout, flush=True)

    if result.returncode:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}"
        )


def scene_number(path):
    match = re.search(r"scene_(\d+)", path.stem, re.IGNORECASE)
    return int(match.group(1)) if match else 999999


def concat_file(paths, destination):
    lines = []

    for path in paths:
        resolved = str(path.resolve())
        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Newline in media file path is not supported.")
        safe_path = resolved.replace("'", "'\\''")
        lines.append(f"file '{safe_path}'")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_videos(ffmpeg, paths, destination, list_file):
    if not paths:
        raise RuntimeError("No scene videos supplied for concatenation.")

    concat_file(paths, list_file)
    destination.parent.mkdir(parents=True, exist_ok=True)

    run([
        ffmpeg, "-hide_banner", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(destination),
    ])

    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"Concatenated video missing: {destination}")


def probe_media(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found.")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration,size",
            "-show_entries", "stream=codec_type,codec_name,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
        raise RuntimeError(
            f"Cannot inspect media file {path}: {result.stderr}"
        )

    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration") or 0)

    if duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    return data, duration


def find_narration():
    if not NARRATION.exists():
        return []

    files = [
        path for path in NARRATION.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and path.name.lower() != "combined_narration.m4a"
        and path.name.lower() != "audio_concat.txt"
    ]

    preferred_names = {
        "final", "narration", "full_narration", "voiceover"
    }

    for path in files:
        if path.stem.lower() in preferred_names:
            return [path]

    return sorted(files, key=lambda path: (scene_number(path), path.name))


def find_optional_audio(directories):
    found = []

    for directory in directories:
        if not directory.exists():
            continue

        for path in directory.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in AUDIO_EXTENSIONS
                and path not in found
            ):
                found.append(path)

    return sorted(found, key=lambda path: path.name.lower())


def concatenate_narration(ffmpeg, files):
    if not files:
        return None

    for path in files:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Narration file is missing or empty: {path}")

    if len(files) == 1:
        return files[0]

    audio_list = NARRATION / "audio_concat.txt"
    combined = NARRATION / "combined_narration.m4a"

    concat_file(files, audio_list)

    run([
        ffmpeg, "-hide_banner", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(audio_list),
        "-vn",
        "-c:a", "aac",
        "-b:a", "192k",
        str(combined),
    ])

    if not combined.is_file() or combined.stat().st_size == 0:
        raise RuntimeError("Combined narration was not created.")

    return combined


def mix_audio(ffmpeg, video, final_video, voice, music, ambient, sfx):
    """Mix available tracks; missing optional tracks are allowed."""
    _, video_duration = probe_media(video)

    tracks = []

    if voice and voice.is_file():
        tracks.append(("voice", voice))

    if music:
        tracks.append(("music", music[0]))

    if ambient:
        tracks.append(("ambient", ambient[0]))

    if sfx:
        # A single complete SFX track is supported here.
        tracks.append(("sfx", sfx[0]))

    if not tracks:
        print("WARNING: No audio tracks found; final video will be silent.")
        shutil.copy2(video, final_video)
        return

    cmd = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(video),
    ]

    # Audio input indexes begin at 1 because index 0 is the video.
    for kind, path in tracks:
        if kind in ("music", "ambient"):
            cmd.extend(["-stream_loop", "-1"])
        cmd.extend(["-i", str(path)])

    filters = []
    labels = []
    audio_index = 1

    for kind, _path in tracks:
        label = f"a{audio_index}"

        if kind == "voice":
            filters.append(
                f"[{audio_index}:a]"
                "aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume=1.0[{label}]"
            )
        elif kind == "music":
            filters.append(
                f"[{audio_index}:a]"
                "aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "volume=0.15,"
                f"atrim=duration={video_duration:.6f},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
        elif kind == "ambient":
            filters.append(
                f"[{audio_index}:a]"
                "aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "volume=0.10,"
                f"atrim=duration={video_duration:.6f},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
        else:
            filters.append(
                f"[{audio_index}:a]"
                "aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "volume=0.25,"
                f"atrim=duration={video_duration:.6f},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )

        labels.append(f"[{label}]")
        audio_index += 1

    # Pad narration so a short voice track does not cut the video.
    if tracks and tracks[0][0] == "voice":
        filters[0] = (
            "[1:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={video_duration:.6f},"
            "asetpts=PTS-STARTPTS[a1]"
        )

    mix_inputs = "".join(labels)
    filters.append(
        f"{mix_inputs}amix=inputs={len(labels)}:"
        "duration=longest:dropout_transition=2,"
        f"atrim=duration={video_duration:.6f},"
        "alimiter=limit=0.95,"
        "aresample=44100[aout]"
    )

    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0",
        "-map", "[aout]",
        "-t", f"{video_duration:.6f}",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        str(final_video),
    ])

    run(cmd)

    if not final_video.is_file() or final_video.stat().st_size == 0:
        raise RuntimeError("Final mixed video is missing or empty.")


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found.")

    for directory in (PHOTO_MOTION, PARTS, VIDEOS, NARRATION):
        directory.mkdir(parents=True, exist_ok=True)

    manifest_path = PHOTO_MOTION / "photo_motion_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("Photo Motion manifest is missing.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not manifest:
        raise RuntimeError("Photo Motion manifest contains no scenes.")

    manifest_numbers = [
        int(item.get("global_scene", item.get("scene", 0)))
        for item in manifest
    ]

    if manifest_numbers != list(range(1, len(manifest) + 1)):
        raise RuntimeError(
            "Manifest scenes must be numbered continuously from 1."
        )

    clips = []

    for item in manifest:
        relative_path = item.get("video")
        if not relative_path:
            raise RuntimeError(
                f"Manifest scene {item.get('scene')} has no video path."
            )

        path = (ROOT / relative_path).resolve()
        if not path.is_relative_to(ROOT):
            raise RuntimeError("Manifest video path escapes repository.")

        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Scene video missing or empty: {path}")

        clips.append(path)

    for path in clips:
        probe_media(path)

    # Remove stale part outputs before creating the new run.
    for old in PARTS.glob("part_*.mp4"):
        old.unlink()

    for old in (
        VIDEOS / "katha_lok_ai_short.mp4",
        VIDEOS / "katha_lok_ai_full.mp4",
        VIDEOS / "katha_lok_ai_final.mp4",
        VIDEOS / "video_without_audio.mp4",
    ):
        if old.exists():
            old.unlink()

    for start in range(0, len(clips), PART_SIZE):
        group = clips[start:start + PART_SIZE]
        part_number = start // PART_SIZE + 1

        concat_videos(
            ffmpeg,
            group,
            PARTS / f"part_{part_number:02d}.mp4",
            PHOTO_MOTION / f"concat_part_{part_number:02d}.txt",
        )

    silent_video = VIDEOS / "video_without_audio.mp4"

    concat_videos(
        ffmpeg,
        clips,
        silent_video,
        PHOTO_MOTION / "concat_final.txt",
    )

    voice_files = find_narration()
    voice = concatenate_narration(ffmpeg, voice_files)

    music = find_optional_audio(MUSIC_DIRS)
    ambient = find_optional_audio(AMBIENT_DIRS)
    sfx = find_optional_audio(SFX_DIRS)

    print("\nAudio inventory:")
    print("Narration:", voice or "not found")
    print("Music:", [str(p.relative_to(ROOT)) for p in music] or "not found")
    print("Ambient:", [str(p.relative_to(ROOT)) for p in ambient] or "not found")
    print("SFX:", [str(p.relative_to(ROOT)) for p in sfx] or "not found")

    final_video = VIDEOS / "katha_lok_ai_final.mp4"

    mix_audio(
        ffmpeg=ffmpeg,
        video=silent_video,
        final_video=final_video,
        voice=voice,
        music=music,
        ambient=ambient,
        sfx=sfx,
    )

    final_data, final_duration = probe_media(final_video)
    streams = final_data.get("streams", [])

    if not any(s.get("codec_type") == "video" for s in streams):
        raise RuntimeError("Final video has no video stream.")

    if final_duration <= 0:
        raise RuntimeError("Final video duration is invalid.")

    alias = (
        VIDEOS / "katha_lok_ai_full.mp4"
        if mode == "full"
        else VIDEOS / "katha_lok_ai_short.mp4"
    )
    shutil.copy2(final_video, alias)

    print("\n" + "=" * 60)
    print("RENDER SUCCESS")
    print("Format:", mode)
    print("Scenes:", len(clips))
    print("Parts:", len(list(PARTS.glob("part_*.mp4"))))
    print("Final video:", final_video)
    print("Format output:", alias)
    print("Duration:", f"{final_duration:.2f}s")
    print("Size MB:", round(final_video.stat().st_size / (1024 * 1024), 2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print("FINAL RENDER FAILED:", exc, file=sys.stderr)
        sys.exit(1)
