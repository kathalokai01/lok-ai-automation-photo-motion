
#!/usr/bin/env python3
"""Resumable photo-motion rendering with narration and validated outputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes, get_fps

OUTPUT = ROOT / "output"
MOTION_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_JOBS = OUTPUT / "narration" / "audio_jobs.json"
RENDER_DIR = OUTPUT / "scene_renders"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"

CACHE_VERSION = 5
MIN_BYTES = 1000
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

AUDIO_DIRS = {
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


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_json(path, data):
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
        raise RuntimeError(f"Missing path for {label}.")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} must be inside the repository.")
    return path


def run(command, timeout=900):
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
            f"Command failed with exit code {result.returncode}: {command[0]}"
        )


def probe(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError(f"Media file missing or too small: {path}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required.")
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
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
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not math.isfinite(duration) or duration <= 0 or video is None:
        raise RuntimeError(f"Invalid video or duration: {path}")
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": audio is not None,
        "has_video": True,
    }


def audio_duration(path):
    info = probe(path)
    if not info["has_audio"]:
        raise RuntimeError(f"Audio stream missing: {path}")
    return info["duration"]


def read_motion(mode):
    if not MOTION_MANIFEST.is_file():
        raise RuntimeError(f"Missing photo-motion manifest: {MOTION_MANIFEST}")
    rows = read_json(MOTION_MANIFEST)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Photo-motion manifest must be a non-empty list.")

    resolution = (1280, 720) if mode == "full" else (720, 1280)
    clips, seen = [], set()

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid motion row {index}.")
        try:
            global_scene = int(row.get("global_scene", row.get("scene", 0)))
            part = int(row["part"])
            local_scene = int(row["local_scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}.") from exc

        key = (part, local_scene)
        if global_scene != index or part < 1 or local_scene < 1 or key in seen:
            raise RuntimeError(f"Invalid or duplicate scene mapping: {key}")
        seen.add(key)

        path = safe_path(row.get("video"), f"scene {global_scene} motion video")
        info = probe(path)
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(
                f"Scene {global_scene} resolution is "
                f"{info['width']}x{info['height']}; expected {resolution}."
            )
        clips.append({
            "scene": global_scene,
            "global_scene": global_scene,
            "part": part,
            "local_scene": local_scene,
            "path": path,
            "duration": info["duration"],
            "manifest_row": row,
        })
    return clips, resolution


def read_narration(clips):
    if not AUDIO_JOBS.is_file():
        raise RuntimeError(f"Missing TTS manifest: {AUDIO_JOBS}")
    data = read_json(AUDIO_JOBS)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("TTS manifest is not completed.")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("TTS manifest must contain a jobs list.")

    identity = {
        (clip["part"], clip["local_scene"]): clip["scene"]
        for clip in clips
    }
    audio = {}
    for job in jobs:
        if not isinstance(job, dict) or job.get("status") != "completed":
            continue
        try:
            key = (int(job["part"]), int(job["scene"]))
        except (KeyError, TypeError, ValueError):
            continue
        number = identity.get(key)
        if number is None:
            continue
        if number in audio:
            raise RuntimeError(f"Duplicate narration job for scene {number}.")
        path = safe_path(job.get("output"), f"narration scene {number}")
        audio_duration(path)
        audio[number] = path

    missing = [c["scene"] for c in clips if c["scene"] not in audio]
    if missing:
        raise RuntimeError(f"Narration missing for scenes: {missing[:30]}")
    return audio


def discover_backgrounds():
    result = {}
    for kind, directories in AUDIO_DIRS.items():
        files = set()
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
                    files.add(path.resolve())
        result[kind] = sorted(files, key=lambda p: p.as_posix().lower())[:1]
    return result


def cache_key(clip, narration, backgrounds, fps, duration):
    return fingerprint({
        "version": CACHE_VERSION,
        "renderer_sha256": digest_file(Path(__file__).resolve()),
        "motion_sha256": digest_file(clip["path"]),
        "narration_sha256": digest_file(narration),
        "background_sha256": {
            kind: digest_file(path)
            for kind, paths in backgrounds.items()
            for path in paths
        },
        "fps": fps,
        "duration": round(duration, 4),
        "mix_volumes": {"music": 0.15, "ambient": 0.10, "sfx": 0.25},
    })


def render_scene(clip, narration, backgrounds, fps):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required.")

    source = clip["path"]
    source_info = probe(source)
    voice_duration = audio_duration(narration)
    target_duration = math.ceil(
        max(source_info["duration"], voice_duration) * fps
    ) / fps
    destination = RENDER_DIR / f"scene_{clip['scene']:04d}.mp4"
    cache_file = RENDER_DIR / f"scene_{clip['scene']:04d}.render.json"
    RENDER_DIR.mkdir(parents=True, exist_ok=True)

    key = cache_key(clip, narration, backgrounds, fps, target_duration)
    if destination.is_file() and cache_file.is_file():
        try:
            saved = read_json(cache_file)
            info = probe(destination)
            if (
                saved.get("version") == CACHE_VERSION
                and saved.get("cache_key") == key
                and info["has_audio"]
                and (info["width"], info["height"])
                == (source_info["width"], source_info["height"])
                and info["duration"] + 0.15 >= voice_duration
            ):
                log(f"REUSE validated rendered scene {clip['scene']}.")
                return destination, info["duration"], key
        except Exception:
            pass

    tracks = [
        (kind, paths[0])
        for kind, paths in backgrounds.items()
        if paths
    ]
    command = [ffmpeg, "-hide_banner", "-y", "-i", str(source), "-i", str(narration)]
    for kind, path in tracks:
        if kind in {"music", "ambient"}:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(path)])

    padding = max(0.0, target_duration - source_info["duration"])
    filters = [
        f"[0:v]tpad=stop_mode=clone:stop_duration={padding:.6f},"
        f"trim=duration={target_duration:.6f},setpts=PTS-STARTPTS[vout]",
        "[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"apad,atrim=duration={target_duration:.6f},asetpts=PTS-STARTPTS[voice]",
    ]
    labels = ["[voice]"]
    volumes = {"music": 0.15, "ambient": 0.10, "sfx": 0.25}

    for input_index, (kind, _) in enumerate(tracks, start=2):
        label = f"bg{input_index}"
        filters.append(
            f"[{input_index}:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={volumes[kind]},apad,atrim=duration={target_duration:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")

    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=1,"
        + f"atrim=duration={target_duration:.6f},alimiter=limit=0.95,"
        + "aresample=44100[aout]"
    )

    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)
    try:
        command.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{target_duration:.6f}", "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
            str(temp),
        ])
        run(command)
        info = probe(temp)
        if not info["has_audio"]:
            raise RuntimeError(f"Rendered scene {clip['scene']} has no audio.")
        if (info["width"], info["height"]) != (
            source_info["width"], source_info["height"]
        ):
            raise RuntimeError(f"Rendered scene {clip['scene']} resolution changed.")
        if info["duration"] + 0.15 < voice_duration:
            raise RuntimeError(f"Narration was truncated in scene {clip['scene']}.")

        temp.replace(destination)
        write_json(cache_file, {
            "version": CACHE_VERSION,
            "cache_key": key,
            "scene": clip["scene"],
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "video": destination.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 6),
        })
        return destination, info["duration"], key
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def concat_videos(paths, destination, list_file, resolution):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required.")

    list_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in paths:
        value = str(Path(path).resolve())
        if "\n" in value or "\r" in value:
            raise RuntimeError("Invalid newline in media path.")
        lines.append("file '" + value.replace("'", "'\\''") + "'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)
    try:
        run([
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
            str(temp),
        ])
        info = probe(temp)
        if not info["has_audio"]:
            raise RuntimeError(f"Output has no audio: {destination}")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Output resolution is wrong: {destination}")
        temp.replace(destination)
        return probe(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        list_file.unlink(missing_ok=True)


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

    for path in (RENDER_DIR, PARTS_DIR, VIDEOS_DIR):
        path.mkdir(parents=True, exist_ok=True)

    clips, resolution = read_motion(mode)
    expected = {
        (part, local)
        for part in range(1, requested_parts + 1)
        for local in range(1, scenes_per_part + 1)
    }
    actual = {(c["part"], c["local_scene"]) for c in clips}
    if actual != expected:
        raise RuntimeError(
            "Input scene mapping differs from motion manifest. "
            f"Missing={sorted(expected - actual)[:20]}; "
            f"extra={sorted(actual - expected)[:20]}"
        )

    narration = read_narration(clips)
    backgrounds = discover_backgrounds()
    log(
        f"FORMAT={mode} | PARTS={requested_parts} "
        f"| SCENES={len(clips)} | FPS={fps}"
    )
    log("Optional audio:", {
        kind: [p.name for p in paths] for kind, paths in backgrounds.items()
    })

    rows = []
    rendered = {}
    for index, clip in enumerate(clips, 1):
        path, duration, key = render_scene(
            clip, narration[clip["scene"]], backgrounds, fps
        )
        relative = path.relative_to(ROOT).as_posix()
        source_relative = clip["path"].relative_to(ROOT).as_posix()
        row = {
            **clip["manifest_row"],
            "scene": clip["scene"],
            "global_scene": clip["global_scene"],
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "source_photo_motion_video": source_relative,
            "source_rendered_video": relative,
            "video": relative,
            "duration": round(duration, 6),
            "fps": fps,
            "render_cache_key": key,
            "narration": narration[clip["scene"]].relative_to(ROOT).as_posix(),
            "rendered_with_narration": True,
            "captions_burned": False,
            "status": "completed",
        }
        rows.append(row)
        rendered[clip["scene"]] = path
        write_json(RENDER_MANIFEST, {
            "version": CACHE_VERSION,
            "format": mode,
            "status": "in_progress",
            "total_scenes": len(clips),
            "completed_count": len(rows),
            "completed_scenes": rows,
            "updated_at": datetime.now().astimezone().isoformat(),
        })
        log(f"CHECKPOINT scene {index}/{len(clips)} saved.")

    part_paths = []
    for part in range(1, requested_parts + 1):
        group = sorted(
            [c for c in clips if c["part"] == part],
            key=lambda c: c["local_scene"],
        )
        if [c["local_scene"] for c in group] != list(range(1, len(group) + 1)):
            raise RuntimeError(f"Part {part} has a gap in scene numbering.")
        destination = PARTS_DIR / f"part_{part:02d}.mp4"
        concat_videos(
            [rendered[c["scene"]] for c in group],
            destination,
            OUTPUT / "photo_motion" / f"concat_part_{part:02d}.txt",
            resolution,
        )
        part_paths.append(destination)

    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(
        [rendered[c["scene"]] for c in clips],
        final_video,
        OUTPUT / "photo_motion" / "concat_final.txt",
        resolution,
    )

    alias = VIDEOS_DIR / (
        "katha_lok_ai_full.mp4" if mode == "full"
        else "katha_lok_ai_short.mp4"
    )
    temp_alias = alias.with_name(alias.stem + ".tmp.mp4")
    try:
        shutil.copy2(final_video, temp_alias)
        probe(temp_alias)
        temp_alias.replace(alias)
    finally:
        temp_alias.unlink(missing_ok=True)

    opposite = VIDEOS_DIR / (
        "katha_lok_ai_short.mp4" if mode == "full"
        else "katha_lok_ai_full.mp4"
    )
    opposite.unlink(missing_ok=True)

    write_json(RENDER_MANIFEST, {
        "version": CACHE_VERSION,
        "format": mode,
        "status": "completed",
        "total_scenes": len(clips),
        "completed_count": len(rows),
        "completed_scenes": rows,
        "parts": [p.relative_to(ROOT).as_posix() for p in part_paths],
        "final_video": final_video.relative_to(ROOT).as_posix(),
        "alias": alias.relative_to(ROOT).as_posix(),
        "duration": round(final_info["duration"], 6),
        "width": final_info["width"],
        "height": final_info["height"],
        "captions_burned": False,
        "updated_at": datetime.now().astimezone().isoformat(),
    })

    log("=" * 48)
    log("SCENE VIDEO/AUDIO RENDER COMPLETE")
    log(f"Format: {mode} | Scenes: {len(clips)} | Parts: {len(part_paths)}")
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
