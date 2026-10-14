
#!/usr/bin/env python3
"""Resumable scene rendering with narration, optional background audio and manifests."""

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
PHOTO_DIR = OUTPUT / "photo_motion"
AUDIO_DIR = OUTPUT / "narration"
RENDER_DIR = OUTPUT / "scene_renders"
PART_DIR = OUTPUT / "parts"
VIDEO_DIR = OUTPUT / "videos"

PHOTO_MANIFEST = PHOTO_DIR / "photo_motion_manifest.json"
AUDIO_MANIFEST = AUDIO_DIR / "audio_jobs.json"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"

MIN_MEDIA_BYTES = 1000


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file too small: {path}")

    return path


def ffprobe(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr[-800:]}")

    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    streams = data.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)

    if not video or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video metadata: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": audio is not None,
    }


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def get_rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def audio_job_map():
    data = read_json(AUDIO_MANIFEST)
    rows = get_rows(data, "jobs", "audio_jobs")
    mapping = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).lower() != "completed":
            continue
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")

        value = next(
            (
                row.get(field)
                for field in ("output", "audio_path", "path", "file", "audio")
                if isinstance(row.get(field), str) and row[field].strip()
            ),
            None,
        )
        if not value:
            raise RuntimeError(f"Narration job {key} has no output audio path")

        audio = safe_path(value, f"Narration {key}", 100)
        info = probe_audio(audio)
        mapping[key] = {
            "part": key[0],
            "scene": key[1],
            "path": audio,
            "duration": info["duration"],
            "sha256": sha256_file(audio),
        }

    return mapping


def probe_audio(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Audio probe failed: {path.name}")

    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid audio duration: {path.name}")

    if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise RuntimeError(f"No audio stream: {path.name}")

    return {"duration": duration}


def optional_background_files():
    """Discover optional tracks without requiring any paid/provider request."""
    result = {"music": [], "ambient": [], "sfx": []}
    roots = [
        OUTPUT / "audio",
        OUTPUT / "sound",
        OUTPUT / "music",
        OUTPUT / "sfx",
    ]
    allowed = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}

    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed:
                continue
            lower = str(path).lower()
            if any(token in lower for token in ("ambient", "environment")):
                category = "ambient"
            elif any(token in lower for token in ("sfx", "effect", "foley")):
                category = "sfx"
            elif any(token in lower for token in ("music", "bgm", "background")):
                category = "music"
            else:
                continue
            result[category].append(path.resolve())

    # One track per category avoids accidentally layering duplicate files.
    return {
        key: paths[:1]
        for key, paths in result.items()
    }


def cache_signature(source, narration, background, fps, resolution, duration):
    payload = {
        "renderer": "render-video-v11",
        "source_sha256": sha256_file(source),
        "narration_sha256": narration["sha256"],
        "background": [
            {"path": relative(path), "sha256": sha256_file(path)}
            for path in background
        ],
        "fps": fps,
        "resolution": list(resolution),
        "duration": round(duration, 3),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def render_scene(source, narration, backgrounds, destination, fps, resolution, duration):
    sidecar = destination.with_suffix(".render.json")
    signature = cache_signature(
        source, narration, backgrounds, fps, resolution, duration
    )

    if destination.is_file() and sidecar.is_file():
        try:
            cached = read_json(sidecar)
            if cached.get("signature") == signature:
                info = ffprobe(destination)
                if (
                    info["has_audio"]
                    and (info["width"], info["height"]) == resolution
                    and info["duration"] > 0
                ):
                    log("RESUME: cached scene", destination.name)
                    return info, signature
        except Exception:
            pass

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    inputs = ["-i", str(source), "-i", str(narration["path"])]
    for background in backgrounds:
        inputs.extend(["-stream_loop", "-1", "-i", str(background)])

    filters = [
        f"[0:v]scale={resolution[0]}:{resolution[1]}:force_original_aspect_ratio=increase,"
        f"crop={resolution[0]}:{resolution[1]},fps={fps},setsar=1[v]"
    ]

    audio_labels = ["[1:a]"]
    next_input = 2

    for background in backgrounds:
        label = f"bg{next_input}"
        filters.append(
            f"[{next_input}:a]aresample=44100,"
            f"volume=0.12,atrim=duration={duration:.3f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        audio_labels.append(f"[{label}]")
        next_input += 1

    filters.append(
        "".join(audio_labels)
        + f"amix=inputs={len(audio_labels)}:duration=first:dropout_transition=2,"
        + "alimiter=limit=0.95[a]"
    )

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(temporary),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Render failed for {destination.name}: {result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_MEDIA_BYTES:
            raise RuntimeError(f"Rendered file is empty: {destination.name}")

        info = ffprobe(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Rendered scene has no audio: {destination.name}")
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Rendered scene resolution mismatch: {destination.name}")

        os.replace(temporary, destination)
        write_json(
            sidecar,
            {
                "status": "completed",
                "signature": signature,
                "source": relative(source),
                "narration": relative(narration["path"]),
                "background_tracks": [relative(p) for p in backgrounds],
                "video": relative(destination),
                "duration": info["duration"],
                "fps": fps,
                "resolution": list(resolution),
            },
        )
        return info, signature
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def concat_videos(videos, destination):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")
    if not videos:
        raise RuntimeError(f"No scene videos for {destination.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    concat_file = destination.with_suffix(".concat.txt")
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    lines = []
    for path in videos:
        resolved = str(path.resolve())
        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Newline in video path")
        lines.append("file '" + resolved.replace("'", "'\\''") + "'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Merge failed for {destination.name}: {result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_MEDIA_BYTES:
            raise RuntimeError(f"Merged file is empty: {destination.name}")

        info = ffprobe(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Merged video has no audio: {destination.name}")
        os.replace(temporary, destination)
        return info
    finally:
        for path in (concat_file, temporary):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    expected_count = parts * scenes_per_part
    fps = int(get_fps(config))
    resolution = (1280, 720) if mode == "full" else (720, 1280)

    for required in (PHOTO_MANIFEST, AUDIO_MANIFEST):
        if not required.is_file():
            raise RuntimeError(f"Required manifest missing: {required}")

    photo_data = read_json(PHOTO_MANIFEST)
    photo_rows = get_rows(
        photo_data, "scenes", "videos", "completed_scenes", "items"
    )
    if len(photo_rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} photo-motion scenes; found {len(photo_rows)}"
        )

    photo_map = {}
    for index, row in enumerate(photo_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid photo-motion row {index}")
        part = int(row["part"])
        local_scene = int(row["local_scene"])
        global_scene = int(row.get("global_scene", row.get("scene", index)))
        key = (part, local_scene)

        if key in photo_map:
            raise RuntimeError(f"Duplicate photo-motion mapping: {key}")
        if global_scene != index:
            raise RuntimeError("Photo-motion global scene numbers are not continuous")

        value = next(
            (
                row.get(field)
                for field in ("video", "output", "video_path")
                if isinstance(row.get(field), str) and row[field].strip()
            ),
            None,
        )
        source = safe_path(value, f"Photo-motion scene {key}", MIN_MEDIA_BYTES)
        info = ffprobe(source)

        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(
                f"Photo-motion resolution mismatch for {key}: "
                f"{info['width']}x{info['height']}"
            )

        photo_map[key] = {
            "part": part,
            "scene": local_scene,
            "global_scene": global_scene,
            "path": source,
        }

    narration_map = audio_job_map()
    if len(narration_map) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} narration jobs; found {len(narration_map)}"
        )

    missing = sorted(set(photo_map) - set(narration_map))
    extra = sorted(set(narration_map) - set(photo_map))
    if missing or extra:
        raise RuntimeError(f"Scene mapping mismatch; missing={missing}, extra={extra}")

    backgrounds = optional_background_files()
    background_files = [
        paths[0]
        for category in ("music", "ambient", "sfx")
        for paths in [backgrounds[category]]
        if paths
    ]
    log("BACKGROUND TRACKS:", [relative(path) for path in background_files])

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    scene_rows = []

    for global_scene in range(1, expected_count + 1):
        row = next(
            item for item in photo_map.values()
            if item["global_scene"] == global_scene
        )
        key = (row["part"], row["scene"])
        source = row["path"]
        narration = narration_map[key]

        # Do not shorten the video below narration duration. If the source
        # visual clip is shorter, FFmpeg's -t uses the narration duration
        # while the video frame stream naturally ends; clone the last frame
        # so the entire narration remains visible.
        duration = max(
            ffprobe(source)["duration"],
            narration["duration"],
        )

        destination = RENDER_DIR / f"scene_{global_scene:04d}.mp4"
        info, signature = render_scene(
            source=source,
            narration=narration,
            backgrounds=background_files,
            destination=destination,
            fps=fps,
            resolution=resolution,
            duration=duration,
        )

        scene_rows.append(
            {
                "scene": global_scene,
                "global_scene": global_scene,
                "part": row["part"],
                "local_scene": row["scene"],
                "source_photo_motion_video": relative(source),
                "source_rendered_video": relative(destination),
                "video": relative(destination),
                "output": relative(destination),
                "narration": relative(narration["path"]),
                "duration": info["duration"],
                "fps": fps,
                "resolution": list(resolution),
                "render_cache_key": signature,
                "rendered_with_narration": True,
                "captions_burned": False,
                "status": "completed",
            }
        )

        # Persist after every successfully rendered scene.
        write_json(
            RENDER_MANIFEST,
            {
                "status": "in_progress",
                "format": mode,
                "captions_burned": False,
                "expected_scenes": expected_count,
                "completed_scenes": scene_rows,
                "parts": [],
                "background_tracks": [relative(p) for p in background_files],
            },
        )
        log(f"RENDERED {global_scene}/{expected_count}")

    part_paths = []
    for part in range(1, parts + 1):
        part_rows = sorted(
            [row for row in scene_rows if row["part"] == part],
            key=lambda row: row["local_scene"],
        )
        if len(part_rows) != scenes_per_part:
            raise RuntimeError(f"Part {part} scene count mismatch")

        videos = [
            safe_path(row["video"], f"Part {part} scene")
            for row in part_rows
        ]
        part_path = PART_DIR / f"part_{part:02d}.mp4"
        info = concat_videos(videos, part_path)
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Part {part} resolution mismatch")
        part_paths.append(relative(part_path))

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(
        [
            safe_path(row["video"], "Final scene")
            for row in scene_rows
        ],
        final_path,
    )

    alias = VIDEO_DIR / (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    shutil.copy2(final_path, alias)

    opposite = VIDEO_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if opposite.exists():
        opposite.unlink()

    write_json(
        RENDER_MANIFEST,
        {
            "status": "completed",
            "format": mode,
            "captions_burned": False,
            "expected_scenes": expected_count,
            "completed_scenes": scene_rows,
            "parts": part_paths,
            "final_video": relative(final_path),
            "alias": relative(alias),
            "duration": final_info["duration"],
            "resolution": {
                "width": final_info["width"],
                "height": final_info["height"],
            },
            "fps": fps,
            "background_tracks": [relative(p) for p in background_files],
        },
    )

    log("=" * 58)
    log("SCENE RENDERING COMPLETED")
    log(f"FORMAT: {mode}")
    log(f"SCENES: {expected_count}")
    log(f"PARTS: {parts}")
    log(f"FINAL: {relative(final_path)}")
    log("Captions will be burned in the next pipeline stage.")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("RENDER PIPELINE FAILED:", str(exc))
        sys.exit(1)
