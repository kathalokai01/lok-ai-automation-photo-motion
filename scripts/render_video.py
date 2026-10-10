
#!/usr/bin/env python3
"""Validate, assemble and render the final Photo Motion video."""

from __future__ import annotations

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
MOTION_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
NARRATION_DIR = OUTPUT / "narration"
NARRATION_AUDIO = NARRATION_DIR / "audio"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

OPTIONAL_AUDIO_DIRS = {
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


def run(command):
    log("$", " ".join(map(str, command)))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.stdout:
        log(result.stdout.rstrip())

    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command[0]}"
        )


def media_info(path):
    """Inspect any media file, including audio-only files."""
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"Missing or too-small media file: {path}")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    video = next(
        (stream for stream in streams
         if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams
         if stream.get("codec_type") == "audio"),
        None,
    )

    return {
        "duration": duration,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def probe_video(path):
    info = media_info(path)

    if not info["has_video"]:
        raise RuntimeError(f"No video stream in {path}")

    return info


def validate_audio(path):
    """Audio-only files are valid; do not require a video stream."""
    info = media_info(path)

    if not info["has_audio"]:
        raise RuntimeError(f"No usable audio stream in {path}")

    return info["duration"]


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} escapes the repository.")

    return path


def read_motion_clips():
    if not MOTION_MANIFEST.is_file():
        raise RuntimeError(f"Missing motion manifest: {MOTION_MANIFEST}")

    try:
        rows = json.loads(MOTION_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read motion manifest: {exc}") from exc

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Motion manifest must be a non-empty JSON list.")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    expected = (1280, 720) if mode == "full" else (720, 1280)

    clips = []

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid motion manifest row {index}.")

        try:
            scene = int(row.get("global_scene", row.get("scene", 0)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene number in row {index}."
            ) from exc

        if scene != index:
            raise RuntimeError(
                f"Scene numbering must be sequential; "
                f"row {index} has scene {scene}."
            )

        path = safe_path(row.get("video"), f"video for scene {scene}")
        info = probe_video(path)

        if (info["width"], info["height"]) != expected:
            raise RuntimeError(
                f"Scene {scene} resolution is "
                f"{info['width']}x{info['height']}; "
                f"expected {expected[0]}x{expected[1]}."
            )

        clips.append((scene, path, info["duration"]))

    return clips, expected, mode


def write_concat_file(paths, destination):
    lines = []

    for path in paths:
        value = str(path.resolve())

        if "\n" in value or "\r" in value:
            raise RuntimeError("Newlines are not allowed in media paths.")

        lines.append("file '" + value.replace("'", "'\\''") + "'")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concatenate_videos(ffmpeg, paths, destination, list_file):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list.")

    write_concat_file(paths, list_file)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)

    try:
        run([
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-map", "0:v:0",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(temp),
        ])

        probe_video(temp)
        temp.replace(destination)
        probe_video(destination)

    except Exception:
        temp.unlink(missing_ok=True)
        raise


def discover_narration():
    """Return completed narration files in part/scene order."""
    if not NARRATION_AUDIO.is_dir():
        raise RuntimeError(
            f"Narration audio directory is missing: {NARRATION_AUDIO}"
        )

    manifest_path = NARRATION_DIR / "audio_jobs.json"
    jobs = []

    if manifest_path.is_file():
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid TTS audio manifest: {exc}"
            ) from exc

        if isinstance(manifest, dict):
            jobs = manifest.get("jobs", [])

        if not isinstance(jobs, list):
            raise RuntimeError("TTS manifest 'jobs' must be a list.")

    ordered = []

    for job in jobs:
        if not isinstance(job, dict):
            continue

        if str(job.get("status", "")).lower() != "completed":
            continue

        value = job.get("output")
        if not value:
            continue

        path = safe_path(value, "narration output")

        if path.is_file() and path.stat().st_size > 0:
            try:
                part = int(job.get("part", 1))
                scene = int(job.get("scene", 1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Invalid part/scene number in TTS manifest."
                ) from exc

            ordered.append((part, scene, path))

    if ordered:
        ordered.sort(key=lambda item: (item[0], item[1]))
        keys = [(part, scene) for part, scene, _ in ordered]

        if len(keys) != len(set(keys)):
            raise RuntimeError(
                "Duplicate part/scene entries in audio_jobs.json."
            )

        files = [path for _, _, path in ordered]
    else:
        # Fallback only when the TTS manifest has no completed outputs.
        files = sorted(
            (
                path for path in NARRATION_AUDIO.glob("part_*/scene_*.mp3")
                if path.is_file() and path.stat().st_size > 0
            ),
            key=lambda path: path.as_posix().lower(),
        )

    if not files:
        raise RuntimeError(
            "No completed narration audio was found. "
            "The pipeline will not silently create a silent final video."
        )

    for path in files:
        validate_audio(path)

    return files


def discover_optional_audio(kind):
    found = []

    for directory in OPTIONAL_AUDIO_DIRS[kind]:
        if not directory.is_dir():
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


def concatenate_audio(ffmpeg, files):
    if not files:
        raise RuntimeError("No narration files to concatenate.")

    for path in files:
        validate_audio(path)

    if len(files) == 1:
        return files[0]

    concat_file = NARRATION_DIR / "audio_concat.txt"
    combined = NARRATION_DIR / "combined_narration.m4a"
    temp = NARRATION_DIR / "combined_narration.tmp.m4a"

    write_concat_file(files, concat_file)
    temp.unlink(missing_ok=True)

    try:
        run([
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-vn",
            "-c:a", "aac",
            "-b:a", "192k",
            str(temp),
        ])

        validate_audio(temp)
        temp.replace(combined)
        validate_audio(combined)

    except Exception:
        temp.unlink(missing_ok=True)
        raise

    return combined


def mix_audio(ffmpeg, silent_video, final_video, duration,
              voice, music, ambient, sfx):
    tracks = [("voice", voice)]

    if music:
        tracks.append(("music", music[0]))
    if ambient:
        tracks.append(("ambient", ambient[0]))
    if sfx:
        tracks.append(("sfx", sfx[0]))

    for _, path in tracks:
        validate_audio(path)

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(silent_video),
    ]

    for kind, path in tracks:
        if kind in {"music", "ambient"}:
            command.extend(["-stream_loop", "-1"])

        command.extend(["-i", str(path)])

    filters = []
    labels = []

    for index, (kind, _) in enumerate(tracks, start=1):
        label = f"a{index}"
        source = f"[{index}:a]"

        common = (
            f"{source}aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        )

        if kind == "voice":
            chain = (
                common
                + f"apad,atrim=duration={duration:.6f},"
                + f"asetpts=PTS-STARTPTS[{label}]"
            )
        else:
            volume = {
                "music": 0.15,
                "ambient": 0.10,
                "sfx": 0.25,
            }[kind]

            chain = (
                common
                + f"volume={volume},"
                + f"atrim=duration={duration:.6f},"
                + f"asetpts=PTS-STARTPTS[{label}]"
            )

        filters.append(chain)
        labels.append(f"[{label}]")

    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:"
        "dropout_transition=2,"
        + f"atrim=duration={duration:.6f},"
        + "alimiter=limit=0.95,"
        + "aresample=44100[aout]"
    )

    temp = final_video.with_name(final_video.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)

    try:
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
            str(temp),
        ])

        run(command)

        info = probe_video(temp)

        if not info["has_audio"]:
            raise RuntimeError("Final output has no audio stream.")

        if abs(info["duration"] - duration) > 1.0:
            raise RuntimeError(
                "Final duration does not match the scene timeline."
            )

        temp.replace(final_video)
        probe_video(final_video)

    except Exception:
        temp.unlink(missing_ok=True)
        raise


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    expected = (1280, 720) if mode == "full" else (720, 1280)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required.")

    for directory in (PARTS_DIR, VIDEOS_DIR, NARRATION_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    clips, resolution, manifest_mode = read_motion_clips()

    if resolution != expected or manifest_mode != mode:
        raise RuntimeError(
            "Input FORMAT and scene resolution do not match."
        )

    # Create part outputs in groups of four scenes.
    part_paths = []

    for start in range(0, len(clips), 4):
        group = clips[start:start + 4]
        part_number = start // 4 + 1
        part_path = PARTS_DIR / f"part_{part_number:02d}.mp4"

        concatenate_videos(
            ffmpeg,
            [item[1] for item in group],
            part_path,
            OUTPUT / "photo_motion" / f"concat_part_{part_number:02d}.txt",
        )

        part_paths.append(part_path)

    silent_video = VIDEOS_DIR / "video_without_audio.mp4"
    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"

    concatenate_videos(
        ffmpeg,
        [item[1] for item in clips],
        silent_video,
        OUTPUT / "photo_motion" / "concat_final.txt",
    )

    video_info = probe_video(silent_video)

    narration_files = discover_narration()
    voice = concatenate_audio(ffmpeg, narration_files)
    music = discover_optional_audio("music")
    ambient = discover_optional_audio("ambient")
    sfx = discover_optional_audio("sfx")

    log("Narration files:", len(narration_files))
    log("Narration source:", voice)
    log("Music:", music[0] if music else "not found (optional)")
    log("Ambient:", ambient[0] if ambient else "not found (optional)")
    log("SFX:", sfx[0] if sfx else "not found (optional)")

    mix_audio(
        ffmpeg,
        silent_video,
        final_video,
        video_info["duration"],
        voice,
        music,
        ambient,
        sfx,
    )

    final_info = probe_video(final_video)

    if (final_info["width"], final_info["height"]) != expected:
        raise RuntimeError("Final video resolution does not match FORMAT.")

    if not final_info["has_audio"]:
        raise RuntimeError("Final video is missing its audio stream.")

    if abs(final_info["duration"] - video_info["duration"]) > 1.0:
        raise RuntimeError(
            "Final video duration differs from the scene timeline."
        )

    alias = VIDEOS_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "full"
        else "katha_lok_ai_short.mp4"
    )

    # Copy to a temporary file first to avoid leaving a partial alias.
    alias_temp = alias.with_name(alias.stem + ".tmp.mp4")

    try:
        shutil.copy2(final_video, alias_temp)
        probe_video(alias_temp)
        alias_temp.replace(alias)
    except Exception:
        alias_temp.unlink(missing_ok=True)
        raise

    log("=" * 52)
    log("FINAL RENDER SUCCESS")
    log("Format:", mode)
    log("Scenes:", len(clips))
    log("Parts:", len(part_paths))
    log("Resolution:", f"{final_info['width']}x{final_info['height']}")
    log("Duration:", f"{final_info['duration']:.2f}s")
    log("Final video:", final_video.relative_to(ROOT))
    log("Format alias:", alias.relative_to(ROOT))
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
