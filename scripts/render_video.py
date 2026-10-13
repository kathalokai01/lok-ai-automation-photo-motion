
#!/usr/bin/env python3
"""Render narration with photo-motion clips and preserve resumable manifests."""

from __future__ import annotations

import hashlib
import json
import math
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

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
CACHE_VERSION = 3

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


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


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
        raise RuntimeError(f"No video stream in {path}")
    return info


def validate_audio(path):
    info = media_info(path)
    if not info["has_audio"]:
        raise RuntimeError(f"No usable audio stream in {path}")
    return info["duration"]


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
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
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
    seen_part_scene = set()

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid motion manifest row {index}.")

        try:
            global_scene = int(row.get("global_scene", row.get("scene", 0)))
            part = int(row.get("part", 0))
            local_scene = int(row.get("local_scene", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene mapping in manifest row {index}."
            ) from exc

        if global_scene != index:
            raise RuntimeError(
                f"Expected global scene {index}; found {global_scene}."
            )
        if part < 1 or local_scene < 1:
            raise RuntimeError(
                f"Scene {global_scene} has no valid part/local_scene mapping."
            )

        key = (part, local_scene)
        if key in seen_part_scene:
            raise RuntimeError(f"Duplicate part/scene mapping: {key}.")
        seen_part_scene.add(key)

        path = safe_path(row.get("video"), f"video for scene {global_scene}")
        info = probe_video(path)

        if (info["width"], info["height"]) != expected:
            raise RuntimeError(
                f"Scene {global_scene} is {info['width']}x{info['height']}; "
                f"expected {expected[0]}x{expected[1]}."
            )

        clips.append({
            "scene": global_scene,
            "part": part,
            "local_scene": local_scene,
            "path": path,
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "manifest_row": row,
        })

    return rows, clips, expected


def discover_narration(clips, total_scenes):
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

    mapping = {
        (clip["part"], clip["local_scene"]): clip["scene"]
        for clip in clips
    }
    audio_by_scene = {}

    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("status", "")).lower() != "completed":
            continue

        try:
            key = (int(job["part"]), int(job["scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        global_scene = mapping.get(key)
        if global_scene is None or not job.get("output"):
            continue

        path = safe_path(job["output"], f"audio for scene {global_scene}")
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        if global_scene in audio_by_scene:
            raise RuntimeError(
                f"Duplicate narration audio for scene {global_scene}."
            )
        audio_by_scene[global_scene] = path

    for clip in clips:
        number = clip["scene"]
        if number in audio_by_scene:
            continue

        candidate = (
            AUDIO_DIR
            / f"part_{clip['part']:02d}"
            / f"scene_{clip['local_scene']:02d}.mp3"
        )
        if candidate.is_file() and candidate.stat().st_size > 0:
            audio_by_scene[number] = candidate

    missing = [
        number for number in range(1, total_scenes + 1)
        if number not in audio_by_scene
    ]
    if missing:
        raise RuntimeError(
            "Narration missing for scene(s): "
            + ", ".join(map(str, missing))
        )

    for number, path in audio_by_scene.items():
        validate_audio(path)
        log(f"Narration scene {number}: {path.relative_to(ROOT)}")

    return audio_by_scene


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
            ):
                resolved = path.resolve()
                if resolved.is_relative_to(ROOT.resolve()) and resolved not in found:
                    found.append(resolved)

    return sorted(found, key=lambda path: path.as_posix().lower())


def make_render_key(source_video, narration, backgrounds, fps, target_duration):
    payload = {
        "version": CACHE_VERSION,
        "renderer_sha256": sha256_file(Path(__file__).resolve()),
        "source_sha256": sha256_file(source_video),
        "narration_sha256": sha256_file(narration),
        "backgrounds": {
            kind: sha256_file(path) for kind, path in backgrounds.items()
        },
        "fps": fps,
        "target_duration": round(target_duration, 4),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def cached_scene(destination, cache_file, key, expected_resolution, voice_duration):
    if not destination.is_file() or not cache_file.is_file():
        return None

    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        if cache.get("cache_key") != key or cache.get("version") != CACHE_VERSION:
            return None

        info = probe_video(destination)
        if not info["has_audio"]:
            return None
        if (info["width"], info["height"]) != expected_resolution:
            return None
        if info["duration"] + 0.15 < voice_duration:
            return None

        return info
    except Exception:
        return None


def render_one_scene(ffmpeg, clip, narration, backgrounds, fps):
    source = clip["path"]
    number = clip["scene"]
    video_info = probe_video(source)
    voice_duration = validate_audio(narration)

    target_duration = (
        math.ceil(max(video_info["duration"], voice_duration) * fps) / fps
    )

    destination = SYNCED_DIR / f"scene_{number:04d}.mp4"
    cache_file = SYNCED_DIR / f"scene_{number:04d}.render.json"
    SYNCED_DIR.mkdir(parents=True, exist_ok=True)

    background_tracks = {
        kind: files[0] for kind, files in backgrounds.items() if files
    }
    key = make_render_key(
        source, narration, background_tracks, fps, target_duration
    )
    expected_resolution = (video_info["width"], video_info["height"])

    cached = cached_scene(
        destination, cache_file, key, expected_resolution, voice_duration
    )
    if cached:
        log(f"SCENE {number} REUSED FROM CACHE | {cached['duration']:.2f}s")
        return destination, cached["duration"], key

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

    filters = [
        (
            f"[0:v]tpad=stop_mode=clone:"
            f"stop_duration={max(0, target_duration-video_info['duration']):.6f},"
            f"trim=duration={target_duration:.6f},"
            f"setpts=PTS-STARTPTS[vout]"
        ),
        (
            f"[1:a]aresample=44100,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={target_duration:.6f},"
            f"asetpts=PTS-STARTPTS[voice]"
        ),
    ]

    labels = ["[voice]"]
    for input_index, (kind, _) in enumerate(tracks, start=2):
        label = f"bg{input_index}"
        volume = {"music": 0.15, "ambient": 0.10, "sfx": 0.25}[kind]
        filters.append(
            f"[{input_index}:a]aresample=44100,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
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
            raise RuntimeError(f"Scene {number} has no audio stream.")
        if (info["width"], info["height"]) != expected_resolution:
            raise RuntimeError(f"Scene {number} has the wrong resolution.")
        if info["duration"] + 0.15 < voice_duration:
            raise RuntimeError(f"Scene {number} narration was truncated.")

        temp.replace(destination)

        atomic_json(cache_file, {
            "version": CACHE_VERSION,
            "cache_key": key,
            "scene": number,
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "video": destination.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 6),
            "narration": narration.relative_to(ROOT).as_posix(),
        })

        log(
            f"SCENE {number} RENDERED | voice={voice_duration:.2f}s "
            f"| output={info['duration']:.2f}s"
        )
        return destination, info["duration"], key

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
        raise RuntimeError(f"FPS must be from 1 to 60; got {fps}.")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required.")

    for directory in (SYNCED_DIR, PARTS_DIR, VIDEOS_DIR, MOTION_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    _, clips, expected_resolution = read_motion_manifest(mode)

    if requested_parts > len(clips):
        raise RuntimeError(
            f"PARTS={requested_parts}, but only {len(clips)} scenes exist."
        )

    expected_keys = {
        (part, local_scene)
        for part in range(1, requested_parts + 1)
        for local_scene in range(1, scenes_per_part + 1)
    }
    actual_keys = {(clip["part"], clip["local_scene"]) for clip in clips}

    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise RuntimeError(
            "Input scene mapping does not match photo-motion manifest. "
            f"Missing={missing}; extra={extra}"
        )

    narration = discover_narration(clips, len(clips))
    backgrounds = {
        kind: discover_optional_audio(kind)
        for kind in ("music", "ambient", "sfx")
    }

    log(
        "FORMAT:", mode,
        "| SCENES:", len(clips),
        "| PARTS:", requested_parts,
        "| FPS:", fps,
    )

    rendered = []
    render_rows = []

    for clip in clips:
        number = clip["scene"]
        audio = narration[number]
        path, duration, render_key = render_one_scene(
            ffmpeg, clip, audio, backgrounds, fps
        )

        rendered_item = {
            "scene": number,
            "global_scene": number,
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "path": path,
            "duration": duration,
        }
        rendered.append(rendered_item)

        render_rows.append({
            **clip["manifest_row"],
            "scene": number,
            "global_scene": number,
            "part": clip["part"],
            "local_scene": clip["local_scene"],
            "source_photo_motion_video": clip["path"].relative_to(ROOT).as_posix(),
            "video": path.relative_to(ROOT).as_posix(),
            "duration": round(duration, 6),
            "fps": fps,
            "rendered_with_narration": True,
            "narration": audio.relative_to(ROOT).as_posix(),
            "render_cache_key": render_key,
            "status": "completed",
        })

        # Save after every scene. Do not overwrite photo_motion_manifest.json.
        atomic_json(RENDER_MANIFEST, {
            "version": CACHE_VERSION,
            "format": mode,
            "status": "in_progress",
            "total_scenes": len(clips),
            "completed_scenes": render_rows,
        })

    # Parts are grouped by the actual mapping in the scene manifest.
    for stale in PARTS_DIR.glob("part_*.mp4"):
        stale.unlink(missing_ok=True)

    part_paths = []
    for part_number in range(1, requested_parts + 1):
        group = [
            item for item in rendered
            if item["part"] == part_number
        ]
        group.sort(key=lambda item: item["local_scene"])

        if not group:
            raise RuntimeError(f"Part {part_number} has no rendered scenes.")

        expected_local = list(range(1, len(group) + 1))
        actual_local = [item["local_scene"] for item in group]
        if actual_local != expected_local:
            raise RuntimeError(
                f"Part {part_number} scene sequence is not continuous."
            )

        part_path = PARTS_DIR / f"part_{part_number:02d}.mp4"
        concatenate_videos(
            ffmpeg,
            [item["path"] for item in group],
            part_path,
            MOTION_DIR / f"concat_part_{part_number:02d}.txt",
        )
        part_paths.append(part_path)
        log(f"PART {part_number}/{requested_parts} READY")

    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    concatenate_videos(
        ffmpeg,
        [item["path"] for item in rendered],
        final_video,
        MOTION_DIR / "concat_final.txt",
    )

    final_info = probe_video(final_video)
    if not final_info["has_audio"]:
        raise RuntimeError("Final video is missing its audio stream.")
    if (final_info["width"], final_info["height"]) != expected_resolution:
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
    except Exception:
        temp_alias.unlink(missing_ok=True)
        raise

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
        "completed_scenes": render_rows,
        "parts": [
            path.relative_to(ROOT).as_posix() for path in part_paths
        ],
        "final_video": final_video.relative_to(ROOT).as_posix(),
        "alias": alias.relative_to(ROOT).as_posix(),
        "duration": round(final_info["duration"], 6),
        "width": final_info["width"],
        "height": final_info["height"],
    })

    log("=" * 48)
    log("SCENE AUDIO/VIDEO RENDER SUCCESS")
    log("Format:", mode, "| Scenes:", len(rendered), "| Parts:", len(part_paths))
    log("Resolution:", f"{final_info['width']}x{final_info['height']}")
    log("Duration:", f"{final_info['duration']:.2f}s")
    log("Render manifest:", RENDER_MANIFEST.relative_to(ROOT))
    log("Final:", final_video.relative_to(ROOT))
    log("Alias:", alias.relative_to(ROOT))
    log("Next: burn captions using the rendered-scene manifest.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("FINAL RENDER CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"FINAL RENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
