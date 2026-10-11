
#!/usr/bin/env python3
"""Render each scene with its own narration, then assemble parts and final video."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUTPUT = ROOT / "output"
MOTION_DIR = OUTPUT / "photo_motion"
MOTION_MANIFEST = MOTION_DIR / "photo_motion_manifest.json"
NARRATION_DIR = OUTPUT / "narration"
AUDIO_DIR = NARRATION_DIR / "audio"
AUDIO_JOBS = NARRATION_DIR / "audio_jobs.json"
SYNCED_DIR = OUTPUT / "scene_renders"
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


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} must be inside the repository.")

    return path


def media_info(path):
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
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
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
    info = media_info(path)
    if not info["has_audio"]:
        raise RuntimeError(f"No usable audio stream in {path}")
    return info["duration"]


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


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
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-movflags", "+faststart",
            str(temp),
        ])
        probe_video(temp)
        temp.replace(destination)
        probe_video(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def read_motion_manifest(mode):
    if not MOTION_MANIFEST.is_file():
        raise RuntimeError(f"Missing motion manifest: {MOTION_MANIFEST}")

    try:
        rows = json.loads(MOTION_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read motion manifest: {exc}") from exc

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Motion manifest must be a non-empty JSON list.")

    expected = (1280, 720) if mode == "full" else (720, 1280)
    clips = []

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid motion manifest row {index}.")

        try:
            scene = int(row.get("global_scene", row.get("scene", 0)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene number in row {index}.") from exc

        if scene != index:
            raise RuntimeError(
                f"Expected scene {index}, but manifest contains {scene}."
            )

        path = safe_path(row.get("video"), f"video for scene {scene}")
        info = probe_video(path)

        if (info["width"], info["height"]) != expected:
            raise RuntimeError(
                f"Scene {scene} is {info['width']}x{info['height']}; "
                f"expected {expected[0]}x{expected[1]}."
            )

        clips.append({
            "scene": scene,
            "path": path,
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "manifest_row": row,
        })

    return rows, clips, expected


def discover_narration(scenes_per_part, total_scenes):
    """Map each global scene to the exact part/scene TTS audio."""
    if not AUDIO_DIR.is_dir():
        raise RuntimeError(f"Narration audio directory missing: {AUDIO_DIR}")

    if not AUDIO_JOBS.is_file():
        raise RuntimeError(f"Missing TTS manifest: {AUDIO_JOBS}")

    try:
        manifest = json.loads(AUDIO_JOBS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid TTS manifest: {exc}") from exc

    jobs = manifest.get("jobs", []) if isinstance(manifest, dict) else []
    if not isinstance(jobs, list):
        raise RuntimeError("TTS manifest 'jobs' must be a list.")

    audio_by_global_scene = {}

    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("status", "")).lower() != "completed":
            continue

        try:
            part = int(job["part"])
            local_scene = int(job["scene"])
        except (KeyError, TypeError, ValueError):
            continue

        global_scene = (part - 1) * scenes_per_part + local_scene
        if global_scene < 1 or global_scene > total_scenes:
            continue

        output_value = job.get("output")
        if not output_value:
            continue

        path = safe_path(output_value, f"audio for scene {global_scene}")
        if path.is_file() and path.stat().st_size > 0:
            if global_scene in audio_by_global_scene:
                raise RuntimeError(
                    f"Duplicate narration audio for scene {global_scene}."
                )
            audio_by_global_scene[global_scene] = path

    # Backward-compatible fallback for the standard TTS directory layout.
    for global_scene in range(1, total_scenes + 1):
        if global_scene in audio_by_global_scene:
            continue

        part = (global_scene - 1) // scenes_per_part + 1
        local_scene = (global_scene - 1) % scenes_per_part + 1
        candidate = (
            AUDIO_DIR / f"part_{part:02d}" / f"scene_{local_scene:02d}.mp3"
        )
        if candidate.is_file() and candidate.stat().st_size > 0:
            audio_by_global_scene[global_scene] = candidate

    missing = [
        scene for scene in range(1, total_scenes + 1)
        if scene not in audio_by_global_scene
    ]
    if missing:
        raise RuntimeError(
            "Narration audio is missing for scene(s): "
            + ", ".join(map(str, missing))
            + ". TTS must complete every scene before rendering."
        )

    for scene, path in audio_by_global_scene.items():
        validate_audio(path)
        log(f"Narration scene {scene}: {path.relative_to(ROOT)}")

    return audio_by_global_scene


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


def render_one_scene(ffmpeg, scene, narration, backgrounds, fps):
    """Mux narration into its own scene; never truncate the narration."""
    source_video = scene["path"]
    scene_number = scene["scene"]
    video_info = probe_video(source_video)
    voice_duration = validate_audio(narration)

    # Keep the entire narration. If voice exceeds the original clip,
    # freeze the last video frame until the voice has finished.
    target_duration = max(video_info["duration"], voice_duration)
    target_duration = math.ceil(target_duration * fps) / fps

    destination = SYNCED_DIR / f"scene_{scene_number:04d}.mp4"
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(source_video),
        "-i", str(narration),
    ]

    background_tracks = []
    for kind in ("music", "ambient", "sfx"):
        files = backgrounds.get(kind, [])
        if files:
            background_tracks.append((kind, files[0]))

    for kind, path in background_tracks:
        if kind in {"music", "ambient"}:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(path)])

    filters = [
        (
            f"[0:v]tpad=stop_mode=clone:"
            f"stop_duration={max(0, target_duration-video_info['duration']):.6f},"
            f"trim=duration={target_duration:.6f},"
            f"setpts=PTS-STARTPTS[vout]"
        ),
        (
            f"[1:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={target_duration:.6f},"
            "asetpts=PTS-STARTPTS[voice]"
        ),
    ]

    labels = ["[voice]"]

    for index, (kind, _) in enumerate(background_tracks, start=2):
        label = f"bg{index}"
        volume = {"music": 0.15, "ambient": 0.10, "sfx": 0.25}[kind]
        filters.append(
            f"[{index}:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={volume},apad,"
            f"atrim=duration={target_duration:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")

    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=1,"
        f"atrim=duration={target_duration:.6f},"
        "alimiter=limit=0.95,aresample=44100[aout]"
    )

    try:
        command.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[vout]",
            "-map", "[aout]",
            "-t", f"{target_duration:.6f}",
            "-r", str(fps),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
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
            raise RuntimeError(f"Scene {scene_number} has no audio.")
        if (info["width"], info["height"]) != (
            video_info["width"], video_info["height"]
        ):
            raise RuntimeError(f"Scene {scene_number} resolution changed.")

        if info["duration"] + 0.15 < voice_duration:
            raise RuntimeError(
                f"Scene {scene_number} audio was truncated: "
                f"audio={voice_duration:.2f}s, output={info['duration']:.2f}s."
            )

        temp.replace(destination)
        final_info = probe_video(destination)
        log(
            f"SCENE {scene_number} READY | "
            f"video={video_info['duration']:.2f}s | "
            f"voice={voice_duration:.2f}s | "
            f"rendered={final_info['duration']:.2f}s"
        )
        return destination, final_info["duration"]

    except Exception:
        temp.unlink(missing_ok=True)
        raise


def split_into_requested_parts(clips, requested_parts):
    if requested_parts < 1:
        raise RuntimeError("PARTS must be at least 1.")
    if requested_parts > len(clips):
        raise RuntimeError(
            f"PARTS={requested_parts}, but only {len(clips)} scenes exist."
        )

    base, extra = divmod(len(clips), requested_parts)
    groups = []
    start = 0
    for index in range(requested_parts):
        count = base + (1 if index < extra else 0)
        groups.append(clips[start:start + count])
        start += count
    return groups


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    requested_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    fps = int(config.get("FPS", 24) or 24)

    if scenes_per_part < 1 or fps < 1:
        raise RuntimeError("SCENES and FPS must be positive integers.")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required.")

    for directory in (SYNCED_DIR, PARTS_DIR, VIDEOS_DIR, NARRATION_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    original_rows, clips, expected_resolution = read_motion_manifest(mode)

    if requested_parts > len(clips):
        raise RuntimeError(
            f"PARTS={requested_parts}, but only {len(clips)} scenes exist."
        )

    narration = discover_narration(scenes_per_part, len(clips))
    backgrounds = {
        kind: discover_optional_audio(kind)
        for kind in ("music", "ambient", "sfx")
    }

    log("FORMAT:", mode)
    log("SCENES:", len(clips))
    log("PARTS:", requested_parts)
    log("Narration is being rendered per scene before assembly.")

    rendered_clips = []
    updated_rows = []

    for clip in clips:
        scene_number = clip["scene"]
        audio_path = narration[scene_number]
        rendered_path, rendered_duration = render_one_scene(
            ffmpeg, clip, audio_path, backgrounds, fps
        )

        relative_path = rendered_path.relative_to(ROOT).as_posix()
        row = dict(clip["manifest_row"])
        row["video"] = relative_path
        row["duration"] = round(rendered_duration, 6)
        row["fps"] = fps
        row["rendered_with_narration"] = True
        row["narration"] = audio_path.relative_to(ROOT).as_posix()
        updated_rows.append(row)

        rendered_clips.append({
            "scene": scene_number,
            "path": rendered_path,
            "duration": rendered_duration,
        })

    # Store the scene-level audio/video timeline for the caption step.
    atomic_json(MOTION_MANIFEST, updated_rows)

    # Remove stale parts so an older run cannot leave extra output files.
    for stale in PARTS_DIR.glob("part_*.mp4"):
        stale.unlink(missing_ok=True)

    groups = split_into_requested_parts(rendered_clips, requested_parts)
    part_paths = []

    for part_number, group in enumerate(groups, start=1):
        part_path = PARTS_DIR / f"part_{part_number:02d}.mp4"
        concatenate_videos(
            ffmpeg,
            [item["path"] for item in group],
            part_path,
            MOTION_DIR / f"concat_part_{part_number:02d}.txt",
        )
        part_paths.append(part_path)
        log(
            f"PART {part_number}/{requested_parts} READY | "
            f"scenes {group[0]['scene']}-{group[-1]['scene']}"
        )

    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    concatenate_videos(
        ffmpeg,
        [item["path"] for item in rendered_clips],
        final_video,
        MOTION_DIR / "concat_final.txt",
    )

    final_info = probe_video(final_video)
    if not final_info["has_audio"]:
        raise RuntimeError("Final video is missing its audio stream.")
    if (
        final_info["width"], final_info["height"]
    ) != expected_resolution:
        raise RuntimeError("Final video resolution does not match FORMAT.")

    alias = VIDEOS_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "full"
        else "katha_lok_ai_short.mp4"
    )
    alias_temp = alias.with_name(alias.stem + ".tmp.mp4")
    try:
        shutil.copy2(final_video, alias_temp)
        probe_video(alias_temp)
        alias_temp.replace(alias)
    except Exception:
        alias_temp.unlink(missing_ok=True)
        raise

    opposite_alias = VIDEOS_DIR / (
        "katha_lok_ai_short.mp4"
        if mode == "full"
        else "katha_lok_ai_full.mp4"
    )
    opposite_alias.unlink(missing_ok=True)

    log("=" * 52)
    log("SCENE AUDIO/VIDEO RENDER SUCCESS")
    log("Format:", mode)
    log("Scenes rendered:", len(rendered_clips))
    log("Parts created:", len(part_paths))
    log("Resolution:", f"{final_info['width']}x{final_info['height']}")
    log("Duration:", f"{final_info['duration']:.2f}s")
    log("Final video:", final_video.relative_to(ROOT))
    log("Format alias:", alias.relative_to(ROOT))
    log("Size:", f"{final_video.stat().st_size / 1048576:.2f} MB")
    log("Next workflow step: burn captions, then validate final output.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("FINAL RENDER CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"FINAL RENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
