
#!/usr/bin/env python3
"""Render photo-motion scenes with narration, optional audio mixing and checkpoints."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes, get_fps

OUTPUT = ROOT / "output"
MOTION_DIR = OUTPUT / "photo_motion"
MOTION_MANIFEST = MOTION_DIR / "photo_motion_manifest.json"
AUDIO_DIR = OUTPUT / "narration" / "audio"
AUDIO_JOBS = OUTPUT / "narration" / "audio_jobs.json"
SYNCED_DIR = OUTPUT / "scene_renders"
RENDER_MANIFEST = SYNCED_DIR / "render_manifest.json"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"

CACHE_VERSION = 4
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


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


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


def run(command, timeout=600):
    log("$", " ".join(map(str, command)))
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.stdout:
        log(result.stdout.rstrip())
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command[0]}"
        )


def media_info(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 1000:
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
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr[-500:]}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media information: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path}")

    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
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
        raise RuntimeError(f"No video stream: {path}")
    return info


def audio_duration(path):
    info = media_info(path)
    if not info["has_audio"]:
        raise RuntimeError(f"No audio stream: {path}")
    return info["duration"]


def write_concat_file(paths, destination):
    lines = []
    for path in paths:
        value = str(Path(path).resolve())
        if "\n" in value or "\r" in value:
            raise RuntimeError("Newlines are not allowed in media paths.")
        lines.append("file '" + value.replace("'", "'\\''") + "'")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concatenate_videos(paths, destination, list_file, expected_resolution=None):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list.")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed.")

    write_concat_file(paths, list_file)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)

    try:
        run([
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            str(temp),
        ])
        info = probe_video(temp)
        if not info["has_audio"]:
            raise RuntimeError(f"Concatenated video has no audio: {temp}")
        if expected_resolution and (
            info["width"], info["height"]
        ) != expected_resolution:
            raise RuntimeError(f"Wrong resolution after concatenation: {temp}")
        temp.replace(destination)
        probe_video(destination)
        return media_info(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        Path(list_file).unlink(missing_ok=True)


def read_motion_manifest(mode):
    if not MOTION_MANIFEST.is_file():
        raise RuntimeError(f"Missing photo-motion manifest: {MOTION_MANIFEST}")

    rows = read_json(MOTION_MANIFEST)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Photo-motion manifest must be a non-empty list.")

    expected_resolution = (1280, 720) if mode == "full" else (720, 1280)
    clips = []
    seen = set()

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid motion manifest row {index}.")

        try:
            global_scene = int(row.get("global_scene", row.get("scene", 0)))
            part = int(row["part"])
            local_scene = int(row["local_scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}.") from exc

        if global_scene != index or part < 1 or local_scene < 1:
            raise RuntimeError(
                f"Invalid/non-sequential mapping at global scene {index}."
            )
        if (part, local_scene) in seen:
            raise RuntimeError(f"Duplicate part/scene mapping: {(part, local_scene)}")
        seen.add((part, local_scene))

        video = safe_path(row.get("video"), f"video for scene {global_scene}")
        info = probe_video(video)
        if (info["width"], info["height"]) != expected_resolution:
            raise RuntimeError(
                f"Scene {global_scene} is {info['width']}x{info['height']}; "
                f"expected {expected_resolution[0]}x{expected_resolution[1]}."
            )

        clips.append({
            "scene": global_scene,
            "global_scene": global_scene,
            "part": part,
            "local_scene": local_scene,
            "path": video,
            "duration": info["duration"],
            "manifest_row": row,
        })

    return clips, expected_resolution


def discover_narration(clips):
    if not AUDIO_JOBS.is_file():
        raise RuntimeError(f"Missing TTS manifest: {AUDIO_JOBS}")

    manifest = read_json(AUDIO_JOBS)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("jobs"), list):
        raise RuntimeError("audio_jobs.json must contain a jobs list.")
    if manifest.get("status") != "completed":
        raise RuntimeError("TTS manifest is not marked completed.")

    mapping = {
        (clip["part"], clip["local_scene"]): clip["scene"]
        for clip in clips
    }
    audio_by_scene = {}

    for job in manifest["jobs"]:
        if not isinstance(job, dict) or job.get("status") != "completed":
            continue
        try:
            key = (int(job["part"]), int(job["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        global_scene = mapping.get(key)
        if global_scene is None:
            continue
        if global_scene in audio_by_scene:
            raise RuntimeError(f"Duplicate narration job for scene {global_scene}.")

        path = safe_path(job.get("output"), f"audio for scene {global_scene}")
        audio_duration(path)
        audio_by_scene[global_scene] = path

    missing = [clip["scene"] for clip in clips if clip["scene"] not in audio_by_scene]
    if missing:
        raise RuntimeError(f"Narration missing for scenes: {missing[:30]}")

    return audio_by_scene


def discover_optional_audio():
    result = {}
    for kind, directories in OPTIONAL_AUDIO_DIRS.items():
        files = []
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in AUDIO_EXTENSIONS
                    and path.stat().st_size > 0
                    and path.resolve().is_relative_to(ROOT.resolve())
                ):
                    files.append(path.resolve())

        # Use one deterministic track per category, avoiding accidentally
        # mixing every file in a music folder at once.
        result[kind] = sorted(
            set(files), key=lambda item: item.as_posix().lower()
        )[:1]

    return result


def make_cache_key(clip, narration, backgrounds, fps, target_duration):
    inputs = {
        "version": CACHE_VERSION,
        "renderer": sha256_file(Path(__file__).resolve()),
        "source_video": sha256_file(clip["path"]),
        "narration": sha256_file(narration),
        "backgrounds": {
            kind: sha256_file(path)
            for kind, paths in backgrounds.items()
            for path in paths
        },
        "fps": fps,
        "duration": round(target_duration, 4),
    }
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()


def valid_cached_scene(destination, cache_path, cache_key, resolution, voice_duration):
    if not destination.is_file() or not cache_path.is_file():
        return None

    try:
        cache = read_json(cache_path)
        if cache.get("version") != CACHE_VERSION:
            return None
        if cache.get("cache_key") != cache_key:
            return None
        info = probe_video(destination)
        if not info["has_audio"]:
            return None
        if (info["width"], info["height"]) != resolution:
            return None
        if info["duration"] + 0.15 < voice_duration:
            return None
        return info
    except Exception:
        return None


def render_one_scene(clip, narration, backgrounds, fps):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed.")

    source = clip["path"]
    source_info = probe_video(source)
    voice_duration = audio_duration(narration)
    target_duration = math.ceil(
        max(source_info["duration"], voice_duration) * fps
    ) / fps

    destination = SYNCED_DIR / f"scene_{clip['scene']:04d}.mp4"
    cache_path = SYNCED_DIR / f"scene_{clip['scene']:04d}.render.json"
    SYNCED_DIR.mkdir(parents=True, exist_ok=True)

    background_tracks = {
        kind: paths[0]
        for kind, paths in backgrounds.items()
        if paths
    }
    resolution = (source_info["width"], source_info["height"])
    cache_key = make_cache_key(
        clip, narration, backgrounds, fps, target_duration
    )

    cached = valid_cached_scene(
        destination, cache_path, cache_key, resolution, voice_duration
    )
    if cached:
        log(f"REUSE scene {clip['scene']} from validated cache.")
        return destination, cached["duration"], cache_key

    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(source),
        "-i", str(narration),
    ]
    tracks = []
    for kind in ("music", "ambient", "sfx"):
        path = background_tracks.get(kind)
        if not path:
            continue
        if kind in {"music", "ambient"}:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(path)])
        tracks.append((kind, path))

    pad = max(0.0, target_duration - source_info["duration"])
    filters = [
        (
            f"[0:v]tpad=stop_mode=clone:stop_duration={pad:.6f},"
            f"trim=duration={target_duration:.6f},"
            "setpts=PTS-STARTPTS[vout]"
        ),
        (
            f"[1:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={target_duration:.6f},"
            "asetpts=PTS-STARTPTS[voice]"
        ),
    ]

    labels = ["[voice]"]
    for input_index, (kind, _) in enumerate(tracks, start=2):
        label = f"bg{input_index}"
        volume = {"music": 0.15, "ambient": 0.10, "sfx": 0.25}[kind]
        filters.append(
            f"[{input_index}:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={volume},apad,atrim=duration={target_duration:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")

    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=1,"
        + f"atrim=duration={target_duration:.6f},"
        + "alimiter=limit=0.95,aresample=44100[aout]"
    )

    try:
        command.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{target_duration:.6f}",
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            str(temp),
        ])
        run(command)

        info = probe_video(temp)
        if not info["has_audio"]:
            raise RuntimeError(f"Rendered scene {clip['scene']} has no audio.")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Rendered scene {clip['scene']} has wrong resolution.")
        if info["duration"] + 0.15 < voice_duration:
            raise RuntimeError(f"Rendered scene {clip['scene']} narration was truncated.")

        temp.replace(destination)
        atomic_json(cache_path, {
            "version": CACHE_VERSION,
            "cache_key": cache_key,
            "scene": clip["scene"],
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "video": destination.relative_to(ROOT).as_posix(),
            "narration": narration.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 6),
        })
        return destination, info["duration"], cache_key
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    requested_parts = get_parts(config)
    scenes_per_part = get_scenes(config)
    fps = get_fps(config)

    if not 1 <= fps <= 60:
        raise RuntimeError(f"FPS must be between 1 and 60; got {fps}.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required.")

    for directory in (SYNCED_DIR, PARTS_DIR, VIDEOS_DIR, MOTION_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    clips, resolution = read_motion_manifest(mode)
    expected = {
        (part, scene)
        for part in range(1, requested_parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    actual = {(clip["part"], clip["local_scene"]) for clip in clips}

    if actual != expected:
        raise RuntimeError(
            "Input mapping does not match photo-motion manifest. "
            f"Missing={sorted(expected - actual)[:30]}; "
            f"extra={sorted(actual - expected)[:30]}"
        )

    narration = discover_narration(clips)
    backgrounds = discover_optional_audio()

    log(
        f"FORMAT={mode} | PARTS={requested_parts} "
        f"| SCENES={len(clips)} | FPS={fps}"
    )
    log("Optional audio:", {
        kind: [path.name for path in paths]
        for kind, paths in backgrounds.items()
    })

    render_rows = []
    rendered_by_scene = {}

    # Keep prior completed scene rows only when their files still validate.
    # Each newly completed scene is checkpointed immediately.
    previous_rows = []
    if RENDER_MANIFEST.is_file():
        try:
            old_manifest = read_json(RENDER_MANIFEST)
            if (
                old_manifest.get("format") == mode
                and old_manifest.get("version") == CACHE_VERSION
                and isinstance(old_manifest.get("completed_scenes"), list)
            ):
                previous_rows = old_manifest["completed_scenes"]
        except Exception:
            previous_rows = []

    old_by_scene = {}
    for row in previous_rows:
        if isinstance(row, dict):
            try:
                old_by_scene[int(row.get("global_scene", row.get("scene", 0)))] = row
            except (TypeError, ValueError):
                pass

    for clip in clips:
        number = clip["scene"]
        audio = narration[number]
        video, duration, render_key = render_one_scene(
            clip, audio, backgrounds, fps
        )
        video_rel = video.relative_to(ROOT).as_posix()
        source_rel = clip["path"].relative_to(ROOT).as_posix()

        row = {
            **clip["manifest_row"],
            "scene": number,
            "global_scene": number,
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "source_photo_motion_video": source_rel,
            "source_rendered_video": video_rel,
            "video": video_rel,
            "duration": round(duration, 6),
            "fps": fps,
            "rendered_with_narration": True,
            "narration": audio.relative_to(ROOT).as_posix(),
            "render_cache_key": render_key,
            "captions_burned": False,
            "status": "completed",
        }
        render_rows.append(row)
        rendered_by_scene[number] = video

        atomic_json(RENDER_MANIFEST, {
            "version": CACHE_VERSION,
            "format": mode,
            "status": "in_progress",
            "total_scenes": len(clips),
            "completed_scenes": render_rows,
            "completed_count": len(render_rows),
            "updated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        })
        log(f"CHECKPOINT scene {number}/{len(clips)} saved.")

    # Build parts using actual part/local-scene mapping.
    part_paths = []
    for part_number in range(1, requested_parts + 1):
        group = sorted(
            [clip for clip in clips if clip["part"] == part_number],
            key=lambda item: item["local_scene"],
        )
        if not group:
            raise RuntimeError(f"Part {part_number} has no scenes.")

        local_numbers = [item["local_scene"] for item in group]
        if local_numbers != list(range(1, len(group) + 1)):
            raise RuntimeError(f"Part {part_number} scene order has a gap.")

        part_path = PARTS_DIR / f"part_{part_number:02d}.mp4"
        concatenate_videos(
            [rendered_by_scene[item["scene"]] for item in group],
            part_path,
            MOTION_DIR / f"concat_part_{part_number:02d}.txt",
            resolution,
        )
        part_paths.append(part_path)
        log(f"PART {part_number}/{requested_parts} rendered.")

    # Remove only the incompatible full/short alias; retain the single final
    # master plus requested parts.
    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    concatenate_videos(
        [rendered_by_scene[item["scene"]] for item in clips],
        final_video,
        MOTION_DIR / "concat_final.txt",
        resolution,
    )
    final_info = probe_video(final_video)
    if not final_info["has_audio"]:
        raise RuntimeError("Final video is missing audio.")
    if (final_info["width"], final_info["height"]) != resolution:
        raise RuntimeError("Final video resolution does not match FORMAT.")

    alias = VIDEOS_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "full"
        else "katha_lok_ai_short.mp4"
    )
    temp_alias = alias.with_name(alias.stem + ".tmp.mp4")
    try:
        shutil.copy2(final_video, temp_alias)
        probe_video(temp_alias)
        temp_alias.replace(alias)
    finally:
        temp_alias.unlink(missing_ok=True)

    opposite = VIDEOS_DIR / (
        "katha_lok_ai_short.mp4"
        if mode == "full"
        else "katha_lok_ai_full.mp4"
    )
    opposite.unlink(missing_ok=True)

    atomic_json(RENDER_MANIFEST, {
        "version": CACHE_VERSION,
        "format": mode,
        "status": "completed",
        "total_scenes": len(clips),
        "completed_count": len(render_rows),
        "completed_scenes": render_rows,
        "parts": [path.relative_to(ROOT).as_posix() for path in part_paths],
        "final_video": final_video.relative_to(ROOT).as_posix(),
        "alias": alias.relative_to(ROOT).as_posix(),
        "duration": round(final_info["duration"], 6),
        "width": final_info["width"],
        "height": final_info["height"],
        "captions_burned": False,
    })

    log("=" * 48)
    log("SCENE VIDEO/AUDIO RENDER COMPLETE")
    log(f"Format: {mode} | Scenes: {len(clips)} | Parts: {len(part_paths)}")
    log(f"Resolution: {final_info['width']}x{final_info['height']}")
    log(f"Duration: {final_info['duration']:.2f}s")
    log(f"Manifest: {RENDER_MANIFEST.relative_to(ROOT)}")
    log(f"Final: {final_video.relative_to(ROOT)}")
    log(f"Alias: {alias.relative_to(ROOT)}")
    log("Next workflow step: burn captions.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("RENDER CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"RENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
