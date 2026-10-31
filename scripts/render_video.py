#!/usr/bin/env python3
"""Render ordered photo-motion scenes with validated resumable checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes, get_fps

OUT = ROOT / "output"
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"
RENDER_DIR = OUT / "scene_renders"
PART_DIR = OUT / "parts"
VIDEO_DIR = OUT / "videos"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"

VERSION = "render-video-v34-dynamic-short-parts"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.25
FPS_TOLERANCE = 0.02


def log(*values):
    print(*values, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_data(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, label):
    if not Path(path).is_file():
        raise RuntimeError(f"{label} missing: {path}")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return data


def safe_project_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing path: {label}")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"Path outside project: {label}")
    if not path.is_file():
        raise RuntimeError(f"File not found: {path}")
    return path


def require_file(path, label, minimum=MIN_VIDEO_BYTES):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label} missing or too small: {path}")
    return path


def run(command, label, timeout=1800):
    result = subprocess.run(
        [str(item) for item in command],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout, check=False,
    )
    if result.stdout:
        print(result.stdout[-5000:], flush=True)
    if result.returncode:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}"
        )


def parse_fps(value):
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        rate = Fraction(str(value))
        return float(rate) if rate > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-1500:]}")
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except Exception as exc:
        raise RuntimeError(f"Invalid media information: {path}") from exc
    if duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")
    streams = data.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    return {
        "duration": duration,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "fps": parse_fps(video.get("avg_frame_rate")) if video else None,
    }


def validate_fps(info, expected, label):
    actual = info.get("fps")
    if actual is None or abs(actual - float(expected)) > FPS_TOLERANCE:
        raise RuntimeError(
            f"FPS mismatch at {label}: {actual} vs expected {expected}"
        )


def validate_manifest_status(data, label):
    if data.get("status") != "completed":
        raise RuntimeError(f"{label} is not completed: {data.get('status')}")


def expected_mapping(index, scenes):
    return ((index - 1) // scenes + 1, (index - 1) % scenes + 1, index)


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}") from exc
    if result < 1:
        raise RuntimeError(f"{label} must be positive")
    return result


def resolve_actual_parts(mode, configured_parts, scenes):
    """Resolve SHORT count from completed source manifests, not Input PARTS."""
    photo = read_json(PHOTO_MANIFEST, "photo-motion manifest")
    audio = read_json(AUDIO_MANIFEST, "narration audio manifest")
    validate_manifest_status(photo, "Photo-motion manifest")
    validate_manifest_status(audio, "Narration audio manifest")

    photo_rows = photo.get("scenes")
    audio_rows = audio.get("jobs")
    if not isinstance(photo_rows, list) or not photo_rows:
        raise RuntimeError("Photo-motion manifest has no scene rows")
    if not isinstance(audio_rows, list) or not audio_rows:
        raise RuntimeError("Narration manifest has no audio jobs")

    photo_parts = {
        positive_int(row.get("part"), "photo-motion part")
        for row in photo_rows if isinstance(row, dict)
    }
    audio_parts = {
        positive_int(row.get("part"), "narration part")
        for row in audio_rows if isinstance(row, dict)
    }
    if not photo_parts or not audio_parts:
        raise RuntimeError("Cannot determine actual part count")
    if photo_parts != audio_parts:
        raise RuntimeError("Photo-motion and narration part counts differ")

    actual_parts = max(photo_parts)
    expected_parts = set(range(1, actual_parts + 1))
    if photo_parts != expected_parts:
        raise RuntimeError("Part numbering is not continuous")

    if mode == "full" and actual_parts != configured_parts:
        raise RuntimeError(
            f"FULL part count mismatch: configured {configured_parts}, "
            f"manifest {actual_parts}"
        )

    expected_scenes = actual_parts * scenes
    if len(photo_rows) != expected_scenes or len(audio_rows) != expected_scenes:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected_scenes}, "
            f"photo={len(photo_rows)}, audio={len(audio_rows)}"
        )

    declared_photo = photo.get("completed_count")
    declared_audio = audio.get("completed_count")
    if declared_photo is not None and int(declared_photo) != expected_scenes:
        raise RuntimeError("Photo-motion completed_count mismatch")
    if declared_audio is not None and int(declared_audio) != expected_scenes:
        raise RuntimeError("Narration completed_count mismatch")

    return actual_parts, photo, audio


def load_photo_map(parts, scenes, mode, width, height, fps, data):
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Photo-motion FORMAT mismatch")
    if (int(data.get("width", 0)), int(data.get("height", 0))) != (width, height):
        raise RuntimeError("Photo-motion dimensions mismatch")
    if int(data.get("fps", 0)) != fps:
        raise RuntimeError("Photo-motion FPS mismatch")

    rows = data.get("scenes")
    expected_count = parts * scenes
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError("Photo-motion scene count mismatch")
    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Photo-motion completed_count mismatch")

    mapping = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene incomplete: {index}")
        try:
            part = int(row["part"])
            scene = int(row["local_scene"])
            global_scene = int(row["global_scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid photo-motion mapping: {index}") from exc
        if (part, scene, global_scene) != expected_mapping(index, scenes):
            raise RuntimeError(f"Photo-motion order mismatch: {index}")

        video = safe_project_path(row.get("video"), f"photo-motion scene {index}")
        require_file(video, f"Photo-motion scene {index}")
        info = probe(video)
        if not info["has_video"] or (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Photo-motion video invalid: scene {index}")
        validate_fps(info, fps, f"photo-motion scene {index}")
        actual_hash = sha256_file(video)
        declared_hash = row.get("video_sha256")
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Photo-motion checksum mismatch: {index}")
        mapping[(part, scene)] = {
            "part": part, "scene": scene, "global_scene": global_scene,
            "video": video, "video_sha256": actual_hash,
            "duration": info["duration"],
        }
    return mapping


def load_audio_map(parts, scenes, data):
    validate_manifest_status(data, "Narration audio manifest")
    rows = data.get("jobs")
    expected_count = parts * scenes
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError("Narration scene count mismatch")

    mapping = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Narration scene incomplete: {index}")
        try:
            part, scene = int(row["part"]), int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid narration mapping: {index}") from exc
        if (part, scene) != expected_mapping(index, scenes)[:2]:
            raise RuntimeError(f"Narration scene order mismatch: {index}")
        audio = safe_project_path(row.get("output"), f"narration scene {index}")
        require_file(audio, "Narration audio", MIN_AUDIO_BYTES)
        info = probe(audio)
        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {audio}")
        mapping[(part, scene)] = {
            "part": part, "scene": scene, "audio": audio,
            "audio_sha256": sha256_file(audio), "duration": info["duration"],
        }
    return mapping


def scene_fingerprint(photo, audio, mode, width, height, fps):
    return digest_data({
        "version": VERSION, "format": mode,
        "width": width, "height": height, "fps": fps,
        "photo_sha256": photo["video_sha256"],
        "audio_sha256": audio["audio_sha256"],
        "audio_duration": round(audio["duration"], 5),
    })


def load_previous_manifest(mode):
    if not RENDER_MANIFEST.is_file():
        return {}
    try:
        data = json.loads(RENDER_MANIFEST.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict) or str(data.get("format", "")).lower() != mode:
        return {}
    rows = data.get("completed_scenes", data.get("scenes", []))
    result = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                key = (int(row["part"]), int(row.get("local_scene", row["scene"])))
            except (KeyError, TypeError, ValueError):
                continue
            result[key] = row
    return result


def reusable_scene(output, previous, fingerprint, width, height, fps, audio_duration):
    if not isinstance(previous, dict) or previous.get("status") != "completed":
        return None
    if previous.get("fingerprint") != fingerprint:
        return None
    if not output.is_file() or output.stat().st_size < MIN_VIDEO_BYTES:
        return None
    try:
        actual_hash = sha256_file(output)
        if previous.get("sha256") != actual_hash:
            return None
        info = probe(output)
        if not info["has_video"] or not info["has_audio"]:
            return None
        if (info["width"], info["height"]) != (width, height):
            return None
        validate_fps(info, fps, str(output))
        if abs(info["duration"] - audio_duration) > DURATION_TOLERANCE:
            return None
        return info, actual_hash
    except Exception:
        return None


def render_scene(photo, audio, output, fingerprint, previous,
                 width, height, fps, resume, skip_completed):
    output.parent.mkdir(parents=True, exist_ok=True)
    if resume and skip_completed:
        cached = reusable_scene(
            output, previous, fingerprint, width, height, fps, audio["duration"]
        )
        if cached:
            log(f"REUSED Part {photo['part']} Scene {photo['scene']}")
            return cached[0], cached[1], True

    temporary = output.with_name(output.stem + ".rendering.mp4")
    temporary.unlink(missing_ok=True)
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
    )
    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(photo["video"]),
            "-i", str(audio["audio"]),
            "-filter_complex", f"[0:v]{video_filter}[v]",
            "-map", "[v]", "-map", "1:a:0",
            "-t", f"{audio['duration']:.4f}",
            "-r", str(fps), "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "medium",
            "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(temporary),
        ], f"Render Part {photo['part']} Scene {photo['scene']}")
        require_file(temporary, "Rendered scene")
        info = probe(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Rendered scene missing audio/video")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Rendered scene dimensions mismatch")
        validate_fps(info, fps, "newly rendered scene")
        if abs(info["duration"] - audio["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError("Rendered duration differs from narration")
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return probe(output), sha256_file(output), False

def concat_videos(paths, destination, width, height, fps):
    if not paths:
        raise RuntimeError(f"No videos supplied for {destination.name}")
    for path in paths:
        require_file(path, f"Concat input {path.name}")
        info = probe(path)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Concat input lacks audio/video: {path}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Concat dimensions mismatch: {path}")
        validate_fps(info, fps, f"concat input {path.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".joining.mp4")
    temporary.unlink(missing_ok=True)
    list_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="katha_concat_",
            encoding="utf-8", delete=False,
        ) as stream:
            list_file = Path(stream.name)
            for path in paths:
                resolved = str(Path(path).resolve())
                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in concat path")
                stream.write("file '" + resolved.replace("'", "'\\''") + "'\n")

        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-r", str(fps), "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "19", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", str(temporary),
        ], f"Join {destination.name}")

        require_file(temporary, "Joined video", 10000)
        info = probe(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Joined video missing audio/video")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Joined video dimensions mismatch")
        validate_fps(info, fps, f"joined video {destination.name}")
        os.replace(temporary, destination)
        return probe(destination)
    finally:
        if list_file is not None:
            list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def build_manifest(mode, fps, width, height, expected_count,
                   completed, part_rows, parts, scenes,
                   photo_hash, audio_hash):
    ordered = [completed[key] for key in sorted(completed)]
    return {
        "status": "completed" if len(ordered) == expected_count else "in_progress",
        "version": VERSION,
        "format": mode,
        "fps": fps,
        "width": width,
        "height": height,
        "total_parts": parts,
        "scenes_per_part": scenes,
        "expected_scene_count": expected_count,
        "completed_count": len(ordered),
        "captions_burned": False,
        "source_photo_manifest": PHOTO_MANIFEST.relative_to(ROOT).as_posix(),
        "source_audio_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "source_photo_manifest_sha256": photo_hash,
        "source_audio_manifest_sha256": audio_hash,
        "completed_scenes": ordered,
        "scenes": ordered,
        "parts": list(part_rows),
    }


def main():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    configured_parts = positive_int(get_parts(config), "PARTS")
    scenes = positive_int(get_scenes(config), "SCENES")
    fps = positive_int(get_fps(config), "FPS")

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if fps > 60:
        raise RuntimeError(f"Invalid FPS: {fps}")

    width, height = (720, 1280) if mode == "short" else (1280, 720)

    # SHORT ignores Input PARTS. The manifests determine its real part count.
    parts, photo_data, audio_data = resolve_actual_parts(
        mode, configured_parts, scenes
    )
    expected_count = parts * scenes

    log("=" * 65)
    log("KATHA LOK AI — SCENE RENDER")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("SIZE:", f"{width}x{height}")
    log("FPS:", fps)
    log("CONFIGURED PARTS:", configured_parts)
    log("ACTUAL PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED SCENES:", expected_count)

    photo_map = load_photo_map(
        parts, scenes, mode, width, height, fps, photo_data
    )
    audio_map = load_audio_map(parts, scenes, audio_data)
    if set(photo_map) != set(audio_map):
        raise RuntimeError("Photo-motion and narration scene mappings differ")

    photo_hash = sha256_file(PHOTO_MANIFEST)
    audio_hash = sha256_file(AUDIO_MANIFEST)
    resume = bool(config.get("RESUME_ENABLED", True))
    skip_completed = bool(config.get("SKIP_COMPLETED_SCENES", True))
    previous = load_previous_manifest(mode)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    completed = {}
    part_rows = []
    reused_count = 0
    rendered_count = 0

    # Reuse only checkpoints whose source hashes and media properties match.
    if resume and skip_completed:
        for part in range(1, parts + 1):
            for scene in range(1, scenes + 1):
                key = (part, scene)
                old_row = previous.get(key)
                if not isinstance(old_row, dict):
                    continue

                photo = photo_map[key]
                audio = audio_map[key]
                fingerprint = scene_fingerprint(
                    photo, audio, mode, width, height, fps
                )
                output = (
                    RENDER_DIR / f"part_{part:02d}" / f"scene_{scene:03d}.mp4"
                )
                cached = reusable_scene(
                    output, old_row, fingerprint, width, height,
                    fps, audio["duration"],
                )
                if not cached:
                    continue

                info, actual_hash = cached
                completed[key] = {
                    **old_row,
                    "part": part,
                    "scene": scene,
                    "local_scene": scene,
                    "global_scene": photo["global_scene"],
                    "status": "completed",
                    "source_photo_motion_video": photo["video"].relative_to(ROOT).as_posix(),
                    "source_audio": audio["audio"].relative_to(ROOT).as_posix(),
                    "source_photo_sha256": photo["video_sha256"],
                    "source_audio_sha256": audio["audio_sha256"],
                    "source_rendered_video": output.relative_to(ROOT).as_posix(),
                    "video": output.relative_to(ROOT).as_posix(),
                    "output": output.relative_to(ROOT).as_posix(),
                    "duration": round(info["duration"], 4),
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "fingerprint": fingerprint,
                    "sha256": actual_hash,
                    "captions_burned": False,
                    "cache_reused": True,
                }

    write_json(
        RENDER_MANIFEST,
        build_manifest(
            mode, fps, width, height, expected_count,
            completed, part_rows, parts, scenes, photo_hash, audio_hash,
        ),
    )

    for part in range(1, parts + 1):
        scene_paths = []
        log("")
        log("#" * 60)
        log("PART", part)
        log("#" * 60)

        for scene in range(1, scenes + 1):
            key = (part, scene)
            photo = photo_map[key]
            audio = audio_map[key]
            fingerprint = scene_fingerprint(
                photo, audio, mode, width, height, fps
            )
            output = (
                RENDER_DIR / f"part_{part:02d}" / f"scene_{scene:03d}.mp4"
            )
            old_row = completed.get(key)
            was_cached = bool(old_row)

            if was_cached:
                info = probe(output)
                actual_hash = sha256_file(output)
                if abs(info["duration"] - audio["duration"]) > DURATION_TOLERANCE:
                    raise RuntimeError(f"Cached duration changed: Part {part} Scene {scene}")
                validate_fps(info, fps, f"cached Part {part} Scene {scene}")
                reused = True
            else:
                info, actual_hash, reused = render_scene(
                    photo, audio, output, fingerprint, previous.get(key),
                    width, height, fps, resume, skip_completed,
                )

            reused_count += int(reused and not was_cached)
            rendered_count += int(not reused)

            row = {
                "part": part,
                "scene": scene,
                "local_scene": scene,
                "global_scene": photo["global_scene"],
                "status": "completed",
                "source_photo_motion_video": photo["video"].relative_to(ROOT).as_posix(),
                "source_audio": audio["audio"].relative_to(ROOT).as_posix(),
                "source_photo_sha256": photo["video_sha256"],
                "source_audio_sha256": audio["audio_sha256"],
                "source_rendered_video": output.relative_to(ROOT).as_posix(),
                "video": output.relative_to(ROOT).as_posix(),
                "output": output.relative_to(ROOT).as_posix(),
                "duration": round(info["duration"], 4),
                "width": width,
                "height": height,
                "fps": fps,
                "fingerprint": fingerprint,
                "sha256": actual_hash,
                "captions_burned": False,
                "cache_reused": reused,
            }
            completed[key] = row
            scene_paths.append(output)

            # Durable checkpoint after each successfully rendered or reused scene.
            write_json(
                RENDER_MANIFEST,
                build_manifest(
                    mode, fps, width, height, expected_count,
                    completed, part_rows, parts, scenes, photo_hash, audio_hash,
                ),
            )
            log(f"CHECKPOINT {len(completed)}/{expected_count}: Part {part} Scene {scene}")

        part_output = PART_DIR / f"part_{part:02d}.mp4"
        part_info = concat_videos(scene_paths, part_output, width, height, fps)
        part_rows.append({
            "part": part,
            "status": "completed",
            "scene_count": scenes,
            "path": part_output.relative_to(ROOT).as_posix(),
            "output": part_output.relative_to(ROOT).as_posix(),
            "duration": round(part_info["duration"], 4),
            "sha256": sha256_file(part_output),
            "fps": fps,
        })
        write_json(
            RENDER_MANIFEST,
            build_manifest(
                mode, fps, width, height, expected_count,
                completed, part_rows, parts, scenes, photo_hash, audio_hash,
            ),
        )
        log(f"PART CHECKPOINT {part}/{parts}: {part_output}")

    if len(completed) != expected_count:
        raise RuntimeError(
            f"Rendered scene count mismatch: {len(completed)}/{expected_count}"
        )

    ordered_rows = [
        completed[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]

    for index, row in enumerate(ordered_rows, 1):
        if int(row["global_scene"]) != index:
            raise RuntimeError(f"Global scene order mismatch: {index}")
        path = safe_project_path(row["video"], f"rendered scene {index}")
        if sha256_file(path) != row["sha256"]:
            raise RuntimeError(f"Rendered scene checksum mismatch: {index}")
        info = probe(path)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Rendered scene missing audio/video: {index}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Rendered scene dimensions mismatch: {index}")
        validate_fps(info, fps, f"final scene {index}")
        expected_duration = audio_map[
            (int(row["part"]), int(row["scene"]))
        ]["duration"]
        if abs(info["duration"] - expected_duration) > DURATION_TOLERANCE:
            raise RuntimeError(f"Final scene duration mismatch: {index}")

    pre_caption = VIDEO_DIR / "pre_caption_render.mp4"
    concat_videos(
        [PART_DIR / f"part_{part:02d}.mp4" for part in range(1, parts + 1)],
        pre_caption, width, height, fps,
    )
    pre_info = probe(pre_caption)
    if not pre_info["has_audio"]:
        raise RuntimeError("Pre-caption video has no audio stream")
    if (pre_info["width"], pre_info["height"]) != (width, height):
        raise RuntimeError("Pre-caption video dimensions mismatch")
    validate_fps(pre_info, fps, "pre-caption video")

    manifest = build_manifest(
        mode, fps, width, height, expected_count,
        completed, part_rows, parts, scenes, photo_hash, audio_hash,
    )
    manifest.update({
        "status": "completed",
        "pre_caption_video": pre_caption.relative_to(ROOT).as_posix(),
        "pre_caption_sha256": sha256_file(pre_caption),
        "pre_caption_duration": round(pre_info["duration"], 4),
        "captions_burned": False,
        "rendered_new": rendered_count,
        "rendered_reused": reused_count + sum(
            1 for row in completed.values()
            if row.get("cache_reused") is True
        ),
    })
    write_json(RENDER_MANIFEST, manifest)

    log("=" * 65)
    log("RENDER STAGE COMPLETED")
    log("FORMAT:", mode)
    log("ACTUAL PARTS:", parts)
    log("SCENES:", expected_count)
    log("NEW RENDERS:", rendered_count)
    log("REUSED RENDERS:", manifest["rendered_reused"])
    log("MANIFEST:", RENDER_MANIFEST.relative_to(ROOT))
    log("PRE-CAPTION VIDEO:", pre_caption.relative_to(ROOT))
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("RENDER INTERRUPTED")
        sys.exit(130)
    except Exception as exc:
        log("RENDER FAILED:", str(exc))
        sys.exit(1)