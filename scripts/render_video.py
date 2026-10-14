
#!/usr/bin/env python3
"""Resumable scene renderer with audio mixing and validated video assembly."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
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

VERSION = 10
MIN_BYTES = 1000
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
VOLUMES = {"music": 0.12, "ambient": 0.08, "sfx": 0.20}

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


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing path for {label}")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} points outside repository")
    return path


def config_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled", ""}:
        return False
    log(f"WARNING: Invalid {key}={value!r}; using {default}")
    return default


def run(command, timeout=1800):
    command = [str(item) for item in command]
    log("$", " ".join(command))
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
        raise RuntimeError(f"Media missing or too small: {path}")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr[-400:]}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc

    video = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    width = int(video.get("width") or 0) if video else 0
    height = int(video.get("height") or 0) if video else 0
    if video and (width < 1 or height < 1):
        raise RuntimeError(f"Invalid video dimensions: {path}")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def audio_duration(path):
    info = probe(path)
    if not info["has_audio"]:
        raise RuntimeError(f"Audio stream missing: {path}")
    return info["duration"]


def read_motion(mode, total):
    data = read_json(MOTION_MANIFEST)
    if not isinstance(data, list) or len(data) != total:
        count = len(data) if isinstance(data, list) else "invalid"
        raise RuntimeError(
            f"Motion manifest requires {total} scenes; found {count}"
        )

    resolution = (1280, 720) if mode == "full" else (720, 1280)
    clips = []
    seen_global = set()
    seen_mapping = set()

    for index, row in enumerate(data, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid motion row {index}")
        try:
            global_scene = int(row.get("global_scene", row.get("scene", 0)))
            part = int(row["part"])
            local = int(row["local_scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

        mapping = (part, local)
        if (
            global_scene != index
            or part < 1
            or local < 1
            or global_scene in seen_global
            or mapping in seen_mapping
        ):
            raise RuntimeError(f"Duplicate or invalid scene mapping: {mapping}")

        path = safe_path(row.get("video"), f"motion scene {global_scene}")
        info = probe(path)
        if not info["has_video"]:
            raise RuntimeError(f"Motion scene {global_scene} has no video")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(
                f"Scene {global_scene} resolution is "
                f"{info['width']}x{info['height']}; expected {resolution}"
            )

        seen_global.add(global_scene)
        seen_mapping.add(mapping)
        clips.append({
            "scene": global_scene,
            "global_scene": global_scene,
            "part": part,
            "local_scene": local,
            "path": path,
            "duration": info["duration"],
            "manifest_row": row,
        })

    return clips, resolution


def read_narration(clips):
    data = read_json(AUDIO_JOBS)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("TTS audio manifest is not completed")

    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("TTS manifest must contain a jobs list")

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

        scene_number = identity.get(key)
        if scene_number is None:
            continue
        if scene_number in audio:
            raise RuntimeError(f"Duplicate narration for scene {scene_number}")

        path = safe_path(job.get("output"), f"narration scene {scene_number}")
        audio_duration(path)
        audio[scene_number] = path

    missing = [c["scene"] for c in clips if c["scene"] not in audio]
    if missing:
        raise RuntimeError(f"Narration missing for scenes: {missing[:30]}")
    return audio


def discover_backgrounds(config):
    enabled = {
        "music": config_bool(config, "MUSIC", True),
        "ambient": config_bool(config, "AMBIENT_SOUND", True),
        "sfx": config_bool(config, "SFX", True),
    }
    result = {}

    for kind, directories in AUDIO_DIRS.items():
        candidates = set()
        if enabled[kind]:
            for directory in directories:
                if not directory.is_dir():
                    continue
                for path in directory.rglob("*"):
                    try:
                        if (
                            path.is_file()
                            and path.suffix.lower() in AUDIO_EXTENSIONS
                            and path.stat().st_size > 0
                            and path.resolve().is_relative_to(ROOT.resolve())
                        ):
                            candidates.add(path.resolve())
                    except OSError:
                        continue

        result[kind] = sorted(
            candidates, key=lambda p: p.as_posix().lower()
        )[:1]
        if enabled[kind] and not result[kind]:
            log(f"INFO: {kind} enabled, but no audio track was found")
    return result


def cache_key(clip, narration, backgrounds, fps, duration):
    background_hashes = {
        kind: sha256_file(paths[0])
        for kind, paths in backgrounds.items() if paths
    }
    return fingerprint({
        "version": VERSION,
        "renderer_sha256": sha256_file(Path(__file__).resolve()),
        "motion_sha256": sha256_file(clip["path"]),
        "narration_sha256": sha256_file(narration),
        "background_hashes": background_hashes,
        "fps": fps,
        "duration": round(duration, 4),
        "volumes": VOLUMES,
    })


def render_scene(clip, narration, backgrounds, fps):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required")

    source = clip["path"]
    source_info = probe(source)
    voice_duration = audio_duration(narration)
    target_duration = (
        math.ceil(max(source_info["duration"], voice_duration) * fps) / fps
    )

    number = clip["scene"]
    destination = RENDER_DIR / f"scene_{number:04d}.mp4"
    cache_file = RENDER_DIR / f"scene_{number:04d}.render.json"
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    key = cache_key(clip, narration, backgrounds, fps, target_duration)

    if destination.is_file() and cache_file.is_file():
        try:
            saved = read_json(cache_file, {})
            info = probe(destination)
            if (
                saved.get("version") == VERSION
                and saved.get("cache_key") == key
                and info["has_video"] and info["has_audio"]
                and (info["width"], info["height"])
                == (source_info["width"], source_info["height"])
                and info["duration"] + 0.15 >= voice_duration
                and abs(info["duration"] - target_duration) <= 0.75
            ):
                log(f"REUSE rendered scene {number}")
                return destination, info["duration"], key
        except Exception as exc:
            log(f"Scene {number} cache rejected: {exc}")

    tracks = [
        (kind, paths[0])
        for kind, paths in backgrounds.items() if paths
    ]
    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(source), "-i", str(narration),
    ]
    for kind, path in tracks:
        if kind in {"music", "ambient"}:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(path)])

    padding = max(0.0, target_duration - source_info["duration"])
    filters = [
        f"[0:v]tpad=stop_mode=clone:stop_duration={padding:.6f},"
        f"trim=duration={target_duration:.6f},setpts=PTS-STARTPTS[vout]",
        "[1:a]aresample=44100,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"apad,atrim=duration={target_duration:.6f},"
        "asetpts=PTS-STARTPTS[voice]",
    ]
    audio_labels = ["[voice]"]

    for input_index, (kind, _) in enumerate(tracks, start=2):
        label = f"bg{input_index}"
        filters.append(
            f"[{input_index}:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={VOLUMES[kind]},"
            f"apad,atrim=duration={target_duration:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        audio_labels.append(f"[{label}]")

    filters.append(
        "".join(audio_labels)
        + f"amix=inputs={len(audio_labels)}:duration=longest:"
        "dropout_transition=1,"
        f"atrim=duration={target_duration:.6f},"
        "alimiter=limit=0.95,aresample=44100[aout]"
    )

    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)
    try:
        command.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{target_duration:.6f}",
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", str(temp),
        ])
        run(command)
        info = probe(temp)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Rendered scene {number} has missing streams")
        if (info["width"], info["height"]) != (
            source_info["width"], source_info["height"]
        ):
            raise RuntimeError(f"Scene {number} resolution changed")
        if info["duration"] + 0.15 < voice_duration:
            raise RuntimeError(f"Narration truncated in scene {number}")

        os.replace(temp, destination)
        write_json(cache_file, {
            "version": VERSION,
            "cache_key": key,
            "scene": number,
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "video": destination.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 6),
            "size_bytes": destination.stat().st_size,
            "status": "completed",
        })
        return destination, info["duration"], key
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def concat_videos(paths, destination, list_file, resolution):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required")

    for path in paths:
        info = probe(path)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Missing video/audio stream: {path}")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Unexpected concat resolution: {path}")

    list_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in paths:
        value = str(Path(path).resolve())
        if "\n" in value or "\r" in value:
            raise RuntimeError("Invalid newline in media path")
        lines.append("file '" + value.replace("'", "'\\''") + "'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)
    try:
        run([
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", str(temp),
        ])
        info = probe(temp)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Output missing streams: {destination}")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Output resolution is wrong: {destination}")
        os.replace(temp, destination)
        return probe(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        list_file.unlink(missing_ok=True)


def save_checkpoint(
    mode, rows, total, parts=None, final_video=None,
    alias=None, final_info=None, status="in_progress",
):
    data = {
        "version": VERSION,
        "format": mode,
        "status": status,
        "total_scenes": total,
        "completed_count": len(rows),
        "completed_scenes": rows,
        "captions_burned": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if parts is not None:
        data["parts"] = [p.relative_to(ROOT).as_posix() for p in parts]
    if final_video is not None:
        data["final_video"] = final_video.relative_to(ROOT).as_posix()
    if alias is not None:
        data["alias"] = alias.relative_to(ROOT).as_posix()
    if final_info is not None:
        data.update({
            "duration": round(final_info["duration"], 6),
            "width": final_info["width"],
            "height": final_info["height"],
        })
    write_json(RENDER_MANIFEST, data)


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts_count = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    fps = int(get_fps(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not 1 <= fps <= 60:
        raise RuntimeError("FPS must be between 1 and 60")

    for directory in (RENDER_DIR, PARTS_DIR, VIDEOS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    total = parts_count * scenes_per_part
    clips, resolution = read_motion(mode, total)

    expected = {
        (part, local)
        for part in range(1, parts_count + 1)
        for local in range(1, scenes_per_part + 1)
    }
    actual = {(c["part"], c["local_scene"]) for c in clips}
    if actual != expected:
        raise RuntimeError(
            "Scene mapping differs from Input/topic.txt. "
            f"Missing={sorted(expected - actual)[:20]}; "
            f"extra={sorted(actual - expected)[:20]}"
        )

    narration = read_narration(clips)
    backgrounds = discover_backgrounds(config)
    log(f"FORMAT={mode} | PARTS={parts_count} | SCENES={total} | FPS={fps}")
    log("Optional tracks:", {
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
        save_checkpoint(mode, rows, total)
        log(f"CHECKPOINT saved: scene {index}/{total}")

    part_paths = []
    for part in range(1, parts_count + 1):
        group = sorted(
            [c for c in clips if c["part"] == part],
            key=lambda c: c["local_scene"],
        )
        local_ids = [c["local_scene"] for c in group]
        if local_ids != list(range(1, scenes_per_part + 1)):
            raise RuntimeError(f"Part {part} has missing or unordered scenes")

        destination = PARTS_DIR / f"part_{part:02d}.mp4"
        concat_videos(
            [rendered[c["scene"]] for c in group],
            destination,
            OUTPUT / "photo_motion" / f"concat_part_{part:02d}.txt",
            resolution,
        )
        part_paths.append(destination)
        log(f"PART {part}/{parts_count} merged")

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
        alias_info = probe(temp_alias)
        if (
            not alias_info["has_video"] or not alias_info["has_audio"]
            or (alias_info["width"], alias_info["height"]) != resolution
            or abs(alias_info["duration"] - final_info["duration"]) > 0.5
        ):
            raise RuntimeError("Final format alias failed validation")
        os.replace(temp_alias, alias)
    finally:
        temp_alias.unlink(missing_ok=True)

    opposite = VIDEOS_DIR / (
        "katha_lok_ai_short.mp4" if mode == "full"
        else "katha_lok_ai_full.mp4"
    )
    opposite.unlink(missing_ok=True)

    save_checkpoint(
        mode, rows, total, parts=part_paths,
        final_video=final_video, alias=alias,
        final_info=final_info, status="completed",
    )
    log("=" * 48)
    log("SCENE VIDEO/AUDIO RENDER COMPLETE")
    log(f"Format: {mode} | Scenes: {total} | Parts: {len(part_paths)}")
    log("Final:", final_video.relative_to(ROOT))
    log("Alias:", alias.relative_to(ROOT))
    log("Captions are not burned yet; burn_captions.py handles that stage.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("RENDER CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"RENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
