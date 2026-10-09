
#!/usr/bin/env python3
"""Validate Photo Motion clips and render the final Katha Lok AI video."""

import json
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
        log(result.stdout)

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

    if duration <= 0:
        raise RuntimeError(f"Invalid duration: {path}")

    return data, duration


def write_concat_list(paths, destination):
    lines = []

    for path in paths:
        resolved = str(path.resolve())

        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Media paths cannot contain newlines.")

        # Escape single quotes for FFmpeg concat demuxer.
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

    write_concat_list(paths, list_file)
    destination.parent.mkdir(parents=True, exist_ok=True)

    run_command([
        ffmpeg,
        "-hide_banner",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-map", "0:v:0",
        "-an",
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(destination),
    ])

    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(
            f"Video concatenation produced no output: {destination}"
        )

    probe_media(destination)


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

    return sorted(found, key=lambda p: p.as_posix().lower())


def concatenate_audio(ffmpeg, files):
    if not files:
        return None

    if len(files) == 1:
        return files[0]

    for path in files:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Narration file is missing or empty: {path}")

    list_file = NARRATION_DIR / "audio_concat.txt"
    combined = NARRATION_DIR / "combined_narration.m4a"

    write_concat_list(files, list_file)

    run_command([
        ffmpeg,
        "-hide_banner",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-vn",
        "-c:a", "aac",
        "-b:a", "192k",
        str(combined),
    ])

    if not combined.is_file() or combined.stat().st_size == 0:
        raise RuntimeError("Could not combine narration audio.")

    probe_media(combined)
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
        log("WARNING: No audio files found; rendering silent video.")
        shutil.copy2(silent_video, final_video)
        return

    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
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
            # Keep the video running if narration is shorter.
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
        str(final_video),
    ])

    run_command(command)

    if not final_video.is_file() or final_video.stat().st_size == 0:
        raise RuntimeError("Final video with audio was not created.")


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
        raise RuntimeError(f"Cannot read motion manifest: {exc}") from exc

    if not isinstance(manifest, list) or not manifest:
        raise RuntimeError("Photo Motion manifest is empty or invalid.")

    clips = []
    scene_numbers = []

    for index, item in enumerate(manifest, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Manifest item {index} is invalid.")

        number = int(item.get("global_scene", item.get("scene", 0)))
        if number < 1:
            raise RuntimeError(f"Invalid scene number: {number}")

        video_value = item.get("video")
        if not video_value:
            raise RuntimeError(f"Scene {number} has no video path.")

        video_path = (ROOT / str(video_value)).resolve()

        if not video_path.is_relative_to(ROOT):
            raise RuntimeError(
                f"Scene {number} path escapes repository: {video_value}"
            )

        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise RuntimeError(
                f"Scene {number} video is missing or empty: {video_path}"
            )

        probe_media(video_path)
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
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed.")

    for directory in (MOTION_DIR, PARTS_DIR, VIDEOS_DIR, NARRATION_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    clips = load_scene_clips()

    # Remove old part files so a shorter new run cannot leave stale parts.
    for old in PARTS_DIR.glob("part_*.mp4"):
        old.unlink()

    silent_video = VIDEOS_DIR / "video_without_audio.mp4"
    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    short_alias = VIDEOS_DIR / "katha_lok_ai_short.mp4"
    full_alias = VIDEOS_DIR / "katha_lok_ai_full.mp4"

    for old in (silent_video, final_video, short_alias, full_alias):
        if old.exists():
            old.unlink()

    # Create part videos of up to four consecutive scenes each.
    for start in range(0, len(clips), PART_SIZE):
        group = clips[start:start + PART_SIZE]
        part_number = start // PART_SIZE + 1

        concatenate_videos(
            ffmpeg,
            group,
            PARTS_DIR / f"part_{part_number:02d}.mp4",
            MOTION_DIR / f"concat_part_{part_number:02d}.txt",
        )

    # Assemble the complete silent visual timeline.
    concatenate_videos(
        ffmpeg,
        clips,
        silent_video,
        MOTION_DIR / "concat_final.txt",
    )

    _, video_duration = probe_media(silent_video)

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

    final_data, final_duration = probe_media(final_video)
    streams = final_data.get("streams", [])
    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]

    if not video_streams:
        raise RuntimeError("Final output has no video stream.")

    if abs(final_duration - video_duration) > 1.0:
        raise RuntimeError(
            "Final duration differs too much from the scene timeline: "
            f"video={video_duration:.2f}s, final={final_duration:.2f}s"
        )

    alias = full_alias if mode == "full" else short_alias
    shutil.copy2(final_video, alias)

    if not alias.is_file() or alias.stat().st_size == 0:
        raise RuntimeError("Format-specific video alias was not created.")

    log("\n" + "=" * 55)
    log("RENDER SUCCESS")
    log("Format:", mode)
    log("Scenes:", len(clips))
    log("Parts:", len(list(PARTS_DIR.glob("part_*.mp4"))))
    log("Final video:", final_video.relative_to(ROOT))
    log("Format output:", alias.relative_to(ROOT))
    log("Duration:", f"{final_duration:.2f} seconds")
    log("Size:", f"{final_video.stat().st_size / 1048576:.2f} MB")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"FINAL RENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
