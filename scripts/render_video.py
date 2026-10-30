
#!/usr/bin/env python3
"""Render photo-motion clips with narration and durable scene checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_fps,
)

OUT = ROOT / "output"
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUT / "narration" / "audio_jobs.json"

RENDER_DIR = OUT / "scene_renders"
PART_DIR = OUT / "parts"
VIDEO_DIR = OUT / "videos"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"

VERSION = "render-video-v31-durable-checkpoints"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.25


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_data(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, label):
    path = Path(path)

    if not path.is_file():
        raise RuntimeError(f"Required manifest missing: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {label}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"{label} must be a JSON object")

    return data


def safe_project_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing file path: {label}")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"Unsafe path outside project: {label}")

    if not path.is_file():
        raise RuntimeError(f"File does not exist: {path}")

    return path


def require_file(path, label, minimum=MIN_VIDEO_BYTES):
    path = Path(path)

    if not path.is_file():
        raise RuntimeError(f"{label} is missing: {path}")

    size = path.stat().st_size

    if size < minimum:
        raise RuntimeError(
            f"{label} is too small: {path} ({size} bytes)"
        )

    return path


def run(command, label, timeout=1800):
    log("")
    log("=" * 65)
    log(label)
    log("=" * 65)

    result = subprocess.run(
        [str(item) for item in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.stdout:
        print(result.stdout, flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}"
        )


def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path}: {result.stderr[-1500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except Exception as exc:
        raise RuntimeError(f"Invalid media information: {path}") from exc

    streams = data.get("streams", [])

    video = next(
        (row for row in streams if row.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (row for row in streams if row.get("codec_type") == "audio"),
        None,
    )

    if not video:
        raise RuntimeError(f"No video stream: {path}")

    if duration <= 0:
        raise RuntimeError(f"Invalid duration: {path}")

    return {
        "duration": duration,
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "has_video": True,
        "has_audio": audio is not None,
        "avg_frame_rate": video.get("avg_frame_rate"),
    }


def validate_status(data, label):
    if data.get("status") != "completed":
        raise RuntimeError(
            f"{label} is not completed: {data.get('status')}"
        )


def expected_mapping(index, scenes):
    return (
        (index - 1) // scenes + 1,
        (index - 1) % scenes + 1,
        index,
    )


def load_photo_map(parts, scenes, mode, width, height, fps):
    data = read_json(PHOTO_MANIFEST, "photo-motion manifest")
    validate_status(data, "Photo-motion manifest")

    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Photo-motion FORMAT does not match Input/topic.txt")

    if int(data.get("width", 0)) != width:
        raise RuntimeError("Photo-motion manifest width mismatch")

    if int(data.get("height", 0)) != height:
        raise RuntimeError("Photo-motion manifest height mismatch")

    if int(data.get("fps", 0)) != fps:
        raise RuntimeError("Photo-motion manifest FPS mismatch")

    rows = data.get("scenes")
    expected_count = parts * scenes

    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError(
            f"Photo-motion scene count mismatch: "
            f"expected {expected_count}, found "
            f"{len(rows) if isinstance(rows, list) else 'invalid'}"
        )

    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Photo-motion completed_count mismatch")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid photo-motion row {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene {index} is incomplete")

        try:
            part = int(row["part"])
            scene = int(row["local_scene"])
            global_scene = int(row["global_scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid photo-motion mapping at row {index}"
            ) from exc

        if (part, scene, global_scene) != expected_mapping(index, scenes):
            raise RuntimeError(
                f"Photo-motion scene order mismatch at row {index}"
            )

        video = safe_project_path(
            row.get("video"),
            f"photo-motion scene {index}",
        )
        require_file(video, f"Photo-motion scene {index}")

        info = probe(video)

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"Photo-motion dimensions mismatch at scene {index}"
            )

        if not info["has_video"]:
            raise RuntimeError(f"Photo-motion video stream missing: {index}")

        actual_hash = sha256_file(video)
        declared_hash = row.get("video_sha256")

        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(
                f"Photo-motion checksum mismatch at scene {index}"
            )

        mapping[(part, scene)] = {
            "part": part,
            "scene": scene,
            "global_scene": global_scene,
            "video": video,
            "video_sha256": actual_hash,
            "duration": info["duration"],
        }

    return mapping


def load_audio_map(parts, scenes):
    data = read_json(AUDIO_MANIFEST, "narration audio manifest")
    validate_status(data, "Narration audio manifest")

    rows = data.get("jobs")
    expected_count = parts * scenes

    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError(
            f"Narration scene count mismatch: expected {expected_count}"
        )

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration job row {index}")

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration mapping at row {index}"
            ) from exc

        expected_part, expected_scene, _ = expected_mapping(index, scenes)

        if (part, scene) != (expected_part, expected_scene):
            raise RuntimeError(
                f"Narration scene order mismatch at row {index}"
            )

        if row.get("status") != "completed":
            raise RuntimeError(
                f"Narration incomplete: Part {part}, Scene {scene}"
            )

        audio = safe_project_path(
            row.get("output"),
            f"narration Part {part} Scene {scene}",
        )
        require_file(audio, "Narration audio", MIN_AUDIO_BYTES)

        info = probe(audio)

        if not info["has_audio"]:
            raise RuntimeError(f"Narration has no audio stream: {audio}")

        mapping[(part, scene)] = {
            "part": part,
            "scene": scene,
            "audio": audio,
            "audio_sha256": sha256_file(audio),
            "duration": info["duration"],
        }

    return mapping


def load_previous_manifest(mode):
    if not RENDER_MANIFEST.is_file():
        return {}

    try:
        data = json.loads(
            RENDER_MANIFEST.read_text(encoding="utf-8-sig")
        )
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    if str(data.get("format", "")).strip().lower() != mode:
        return {}

    rows = data.get("completed_scenes", [])

    if not isinstance(rows, list):
        return {}

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            key = (int(row["part"]), int(row["local_scene"]))
        except (KeyError, TypeError, ValueError):
            continue

        result[key] = row

    return result


def scene_fingerprint(photo, audio, mode, width, height, fps):
    return digest_data({
        "version": VERSION,
        "format": mode,
        "width": width,
        "height": height,
        "fps": fps,
        "photo_sha256": photo["video_sha256"],
        "audio_sha256": audio["audio_sha256"],
        "audio_duration": round(audio["duration"], 5),
    })


def reusable_scene(output, previous, fingerprint, width, height):
    if not isinstance(previous, dict):
        return None

    if previous.get("status") != "completed":
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

        return info, actual_hash
    except Exception:
        return None


def render_scene(photo, audio, output, fingerprint, previous,
                 width, height, fps, resume, skip_completed):

    output.parent.mkdir(parents=True, exist_ok=True)

    if resume and skip_completed:
        cached = reusable_scene(
            output,
            previous,
            fingerprint,
            width,
            height,
        )

        if cached:
            info, actual_hash = cached
            log(
                f"REUSED Part {photo['part']} "
                f"Scene {photo['scene']}"
            )
            return info, actual_hash, True

    audio_duration = audio["duration"]
    temporary = output.with_name(output.stem + ".rendering.mp4")
    temporary.unlink(missing_ok=True)

    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        "setsar=1,"
        f"fps={fps},"
        "format=yuv420p"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-stream_loop", "-1",
        "-i", str(photo["video"]),
        "-i", str(audio["audio"]),
        "-filter_complex", f"[0:v]{video_filter}[v]",
        "-map", "[v]",
        "-map", "1:a:0",
        "-t", f"{audio_duration:.4f}",
        "-r", str(fps),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        str(temporary),
    ]

    try:
        run(
            command,
            f"Render Part {photo['part']} Scene {photo['scene']}",
        )

        require_file(temporary, "Rendered scene")
        info = probe(temporary)

        if not info["has_audio"]:
            raise RuntimeError(
                f"Rendered scene has no narration audio: {temporary}"
            )

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"Rendered scene dimensions mismatch: {temporary}"
            )

        if abs(info["duration"] - audio_duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Rendered duration differs from narration: "
                f"{info['duration']:.2f}s vs {audio_duration:.2f}s"
            )

        os.replace(temporary, output)

    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    actual_hash = sha256_file(output)
    return probe(output), actual_hash, False


def concat_videos(paths, destination):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list")

    for path in paths:
        require_file(path, "Concatenation input")

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="katha_concat_",
        encoding="utf-8",
        delete=False,
    ) as handle:
        list_file = Path(handle.name)

        for path in paths:
            escaped = str(path.resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")

    temporary = destination.with_name(
        destination.stem + ".joining.mp4"
    )
    temporary.unlink(missing_ok=True)

    try:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                "-movflags", "+faststart",
                str(temporary),
            ],
            f"Join {destination.name}",
        )

        require_file(temporary, "Joined video", 10000)

        info = probe(temporary)

        if not info["has_audio"] or not info["has_video"]:
            raise RuntimeError(
                f"Joined video is missing audio/video: {temporary}"
            )

        os.replace(temporary, destination)
        return probe(destination)

    finally:
        list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def build_manifest(mode, fps, width, height, expected_count,
                   completed, parts, photo_manifest_hash,
                   audio_manifest_hash):
    ordered = [
        completed[key]
        for key in sorted(completed)
    ]

    return {
        "status": (
            "completed"
            if len(ordered) == expected_count
            else "in_progress"
        ),
        "version": VERSION,
        "format": mode,
        "fps": fps,
        "width": width,
        "height": height,
        "expected_scene_count": expected_count,
        "completed_count": len(ordered),
        "captions_burned": False,
        "source_photo_manifest": PHOTO_MANIFEST.relative_to(ROOT).as_posix(),
        "source_audio_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "source_photo_manifest_sha256": photo_manifest_hash,
        "source_audio_manifest_sha256": audio_manifest_hash,
        "completed_scenes": ordered,
        "scenes": ordered,
        "parts": list(parts),
    }


def main():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))
    fps = int(get_fps(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    if fps < 1 or fps > 60:
        raise RuntimeError(f"Invalid FPS: {fps}")

    width, height = (
        (720, 1280)
        if mode == "short"
        else (1280, 720)
    )

    expected_count = parts * scenes

    log("=" * 70)
    log("KATHA LOK AI — PHOTO MOTION + NARRATION RENDER")
    log("=" * 70)
    log("FORMAT:", mode)
    log("SIZE:", f"{width}x{height}")
    log("FPS:", fps)
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED SCENES:", expected_count)

    photo_map = load_photo_map(
        parts, scenes, mode, width, height, fps
    )
    audio_map = load_audio_map(parts, scenes)

    if set(photo_map) != set(audio_map):
        raise RuntimeError(
            "Photo-motion and narration scene mappings do not match"
        )

    photo_manifest_hash = sha256_file(PHOTO_MANIFEST)
    audio_manifest_hash = sha256_file(AUDIO_MANIFEST)

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

    # Save an initial checkpoint without discarding recoverable scene rows.
    initial_rows = {}

    for key, old_row in previous.items():
        if key not in photo_map:
            continue

        expected_part, expected_scene = key

        if not (
            1 <= expected_part <= parts
            and 1 <= expected_scene <= scenes
        ):
            continue

        initial_rows[key] = old_row

    manifest = build_manifest(
        mode,
        fps,
        width,
        height,
        expected_count,
        {},
        [],
        photo_manifest_hash,
        audio_manifest_hash,
    )
    write_json(RENDER_MANIFEST, manifest)

    for part in range(1, parts + 1):
        scene_paths = []

        log("")
        log("#" * 65)
        log("PART", part)
        log("#" * 65)

        for scene in range(1, scenes + 1):
            key = (part, scene)
            photo = photo_map[key]
            audio = audio_map[key]

            fingerprint = scene_fingerprint(
                photo,
                audio,
                mode,
                width,
                height,
                fps,
            )

            output = (
                RENDER_DIR
                / f"part_{part:02d}"
                / f"scene_{scene:03d}.mp4"
            )

            info, actual_hash, reused = render_scene(
                photo,
                audio,
                output,
                fingerprint,
                initial_rows.get(key),
                width,
                height,
                fps,
                resume,
                skip_completed,
            )

            reused_count += int(reused)
            rendered_count += int(not reused)

            row = {
                "part": part,
                "scene": scene,
                "local_scene": scene,
                "global_scene": photo["global_scene"],
                "status": "completed",
                "source_photo_motion_video": (
                    photo["video"].relative_to(ROOT).as_posix()
                ),
                "source_audio": (
                    audio["audio"].relative_to(ROOT).as_posix()
                ),
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

            # Always checkpoint after every successful scene.
            manifest = build_manifest(
                mode,
                fps,
                width,
                height,
                expected_count,
                completed,
                part_rows,
                photo_manifest_hash,
                audio_manifest_hash,
            )
            write_json(RENDER_MANIFEST, manifest)

            log(
                f"CHECKPOINT {len(completed)}/{expected_count}: "
                f"Part {part} Scene {scene}"
            )

        part_output = PART_DIR / f"part_{part:02d}.mp4"
        part_info = concat_videos(scene_paths, part_output)

        part_rows.append({
            "part": part,
            "status": "completed",
            "scene_count": scenes,
            "path": part_output.relative_to(ROOT).as_posix(),
            "output": part_output.relative_to(ROOT).as_posix(),
            "duration": round(part_info["duration"], 4),
            "sha256": sha256_file(part_output),
        })

        manifest = build_manifest(
            mode,
            fps,
            width,
            height,
            expected_count,
            completed,
            part_rows,
            photo_manifest_hash,
            audio_manifest_hash,
        )
        write_json(RENDER_MANIFEST, manifest)

        log(
            f"PART CHECKPOINT {part}/{parts}: "
            f"{part_output.relative_to(ROOT)}"
        )

    if len(completed) != expected_count:
        raise RuntimeError(
            f"Rendered scene count mismatch: "
            f"{len(completed)}/{expected_count}"
        )

    ordered_rows = [
        completed[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]

    for index, row in enumerate(ordered_rows, 1):
        if int(row["global_scene"]) != index:
            raise RuntimeError(
                f"Global scene order mismatch at row {index}"
            )

        path = safe_project_path(row["video"], f"rendered scene {index}")

        if sha256_file(path) != row["sha256"]:
            raise RuntimeError(
                f"Rendered scene checksum mismatch: {index}"
            )

        info = probe(path)

        if not info["has_audio"]:
            raise RuntimeError(
                f"Rendered scene has no narration audio: {index}"
            )

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"Rendered scene dimensions mismatch: {index}"
            )

    pre_caption = VIDEO_DIR / "pre_caption_render.mp4"

    concat_videos(
        [
            PART_DIR / f"part_{part:02d}.mp4"
            for part in range(1, parts + 1)
        ],
        pre_caption,
    )

    pre_caption_info = probe(pre_caption)

    if not pre_caption_info["has_audio"]:
        raise RuntimeError("Pre-caption video has no audio stream")

    if (pre_caption_info["width"], pre_caption_info["height"]) != (
        width,
        height,
    ):
        raise RuntimeError("Pre-caption video dimensions mismatch")

    manifest = build_manifest(
        mode,
        fps,
        width,
        height,
        expected_count,
        completed,
        part_rows,
        photo_manifest_hash,
        audio_manifest_hash,
    )

    manifest.update({
        "status": "completed",
        "pre_caption_video": pre_caption.relative_to(ROOT).as_posix(),
        "pre_caption_sha256": sha256_file(pre_caption),
        "pre_caption_duration": round(pre_caption_info["duration"], 4),
        "captions_burned": False,
        "rendered_new": rendered_count,
        "rendered_reused": reused_count,
    })

    write_json(RENDER_MANIFEST, manifest)

    log("")
    log("=" * 70)
    log("RENDER STAGE COMPLETED")
    log("FORMAT:", mode)
    log("SCENES:", expected_count)
    log("NEW RENDERS:", rendered_count)
    log("REUSED RENDERS:", reused_count)
    log("MANIFEST:", RENDER_MANIFEST.relative_to(ROOT))
    log("PRE-CAPTION VIDEO:", pre_caption.relative_to(ROOT))
    log("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("RENDER INTERRUPTED")
        sys.exit(130)
    except Exception as exc:
        log("RENDER FAILED:", str(exc))
        sys.exit(1)
