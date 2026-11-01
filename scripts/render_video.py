#!/usr/bin/env python3
"""Render Photo Motion scenes with narration, music, ambience and SFX."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
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
SOUND_MANIFEST = OUT / "sound_design" / "sound_design_manifest.json"
SFX_VALIDATION = OUT / "sound_design" / "sfx_timing_validation.json"

RENDER_DIR = OUT / "scene_renders"
PART_DIR = OUT / "parts"
VIDEO_DIR = OUT / "videos"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"

VERSION = "render-video-v37-verified-audio-sfx-resume"
AUDIO_RATE = 48000
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
FPS_TOLERANCE = 0.03
DURATION_TOLERANCE = 1.25

NARRATION_GAIN = 1.0
MUSIC_GAIN = 0.30
AMBIENT_GAIN = 0.22
SFX_GAIN_MULTIPLIER = 1.65
MASTER_LIMIT = 0.95
SFX_END_MARGIN = 0.08


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
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
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
        value = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
    except Exception as exc:
        raise RuntimeError(f"Invalid {label}: {exc}") from exc

    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")

    return value


def safe_path(value, label, must_exist=True):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path is missing")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory")

    if must_exist and not path.is_file():
        raise RuntimeError(f"{label}: file does not exist: {path}")

    return path


def require_file(path, label, minimum=MIN_VIDEO_BYTES):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label} missing or too small: {path}")
    return path


def run(command, label, timeout=1800):
    result = subprocess.run(
        [str(item) for item in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.stdout:
        log(result.stdout[-5000:])

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
            "ffprobe", "-v", "error",
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

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path}: {result.stderr[-1500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except Exception as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc

    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    return {
        "duration": duration,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "fps": (
            parse_fps(video.get("avg_frame_rate"))
            if video else None
        ),
    }


def validate_fps(info, expected, label):
    actual = info.get("fps")
    if actual is None or abs(actual - float(expected)) > FPS_TOLERANCE:
        raise RuntimeError(
            f"FPS mismatch at {label}: {actual}, expected {expected}"
        )


def positive_int(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc
    if number < 1:
        raise RuntimeError(f"{label} must be positive")
    return number


def config_bool(config, key, default=True):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()
    if value in {"true", "yes", "1", "on", "enabled"}:
        return True
    if value in {"false", "no", "0", "off", "disabled", ""}:
        return False

    raise RuntimeError(f"{key} must be true or false")


def validate_completed(data, label):
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
    data = read_json(PHOTO_MANIFEST, "Photo Motion manifest")
    validate_completed(data, "Photo Motion manifest")

    if str(data.get("format", "")).lower() != mode:
        raise RuntimeError("Photo Motion FORMAT mismatch")

    if (
        int(data.get("width", 0)) != width
        or int(data.get("height", 0)) != height
        or int(data.get("fps", 0)) != fps
    ):
        raise RuntimeError("Photo Motion dimensions/FPS mismatch")

    rows = data.get("scenes")
    expected_count = parts * scenes

    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError(
            f"Photo Motion scene count mismatch: "
            f"expected {expected_count}, found "
            f"{len(rows) if isinstance(rows, list) else 0}"
        )

    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Photo Motion completed_count mismatch")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Photo Motion scene {index} is incomplete")

        expected = expected_mapping(index, scenes)

        try:
            part = int(row["part"])
            scene = int(row["local_scene"])
            global_scene = int(row["global_scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid Photo Motion mapping at scene {index}"
            ) from exc

        if (part, scene, global_scene) != expected:
            raise RuntimeError(
                f"Photo Motion scene order mismatch at row {index}"
            )

        video = safe_path(row.get("video"), f"Photo Motion scene {index}")
        require_file(video, f"Photo Motion scene {index}")

        info = probe(video)
        if not info["has_video"]:
            raise RuntimeError(f"Photo Motion scene {index} has no video")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"Photo Motion scene {index} dimensions mismatch"
            )

        validate_fps(info, fps, f"Photo Motion scene {index}")

        actual_hash = sha256_file(video)
        if row.get("video_sha256") != actual_hash:
            raise RuntimeError(
                f"Photo Motion scene {index} checksum mismatch"
            )

        mapping[(part, scene)] = {
            "part": part,
            "scene": scene,
            "global_scene": global_scene,
            "video": video,
            "video_sha256": actual_hash,
            "duration": info["duration"],
        }

    return mapping, data


def load_audio_map(parts, scenes):
    data = read_json(AUDIO_MANIFEST, "Narration audio manifest")
    validate_completed(data, "Narration audio manifest")

    rows = data.get("jobs")
    expected_count = parts * scenes

    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError("Narration audio job count mismatch")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration job {index}")

        if row.get("status") not in {"completed", "reused"}:
            raise RuntimeError(f"Narration job {index} is incomplete")

        expected = expected_mapping(index, scenes)

        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration mapping at job {index}"
            ) from exc

        if (part, scene) != expected[:2]:
            raise RuntimeError(
                f"Narration job order mismatch at {index}"
            )

        audio = safe_path(
            row.get("output")
            or row.get("audio_path")
            or row.get("path")
            or row.get("file"),
            f"Narration audio {index}",
        )
        require_file(audio, f"Narration audio {index}", MIN_AUDIO_BYTES)

        info = probe(audio)
        if not info["has_audio"]:
            raise RuntimeError(
                f"Narration audio {index} has no audio stream"
            )

        actual_hash = sha256_file(audio)
        declared_hash = row.get("sha256") or row.get("audio_sha256")

        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(
                f"Narration audio checksum mismatch at {index}"
            )

        mapping[(part, scene)] = {
            "part": part,
            "scene": scene,
            "audio": audio,
            "audio_sha256": actual_hash,
            "duration": info["duration"],
        }

    return mapping, data


def load_sound_assets(parts, narration):
    manifest = read_json(SOUND_MANIFEST, "Sound design manifest")
    validate_completed(manifest, "Sound design manifest")

    validation = read_json(SFX_VALIDATION, "SFX validation report")
    validate_completed(validation, "SFX validation report")

    mode = str(manifest.get("format", "")).lower()
    if mode not in {"short", "full"}:
        raise RuntimeError("Sound design FORMAT is invalid")

    if str(validation.get("format", "")).lower() != mode:
        raise RuntimeError("SFX validation FORMAT mismatch")

    if int(manifest.get("parts", -1)) != parts:
        raise RuntimeError("Sound design part count mismatch")

    tracks = manifest.get("tracks", {})
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound design tracks must be an object")

    assets = {
        "music": None,
        "ambient": None,
        "sfx": {},
        "warnings": [],
    }
    track_hashes = {}

    for category in ("music", "ambient"):
        record = tracks.get(category)

        if record is None:
            continue

        if not isinstance(record, dict) or record.get("status") != "completed":
            raise RuntimeError(f"{category} track is not completed")

        path = safe_path(record.get("path"), f"{category} track")
        require_file(path, f"{category} track", MIN_AUDIO_BYTES)

        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(f"{category} track has no audio stream")

        checksum = sha256_file(path)
        if record.get("sha256") and record["sha256"] != checksum:
            raise RuntimeError(f"{category} track checksum mismatch")

        assets[category] = {
            "path": path,
            "sha256": checksum,
            "duration": info["duration"],
        }
        track_hashes[category] = checksum

    report = validation.get("sfx", {})
    cues = report.get("cues", []) if isinstance(report, dict) else []

    if not isinstance(cues, list):
        raise RuntimeError("SFX validation cues must be a list")

    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        try:
            key = (int(cue["part"]), int(cue["scene"]))
            start = float(
                cue.get("start_seconds", cue.get("time"))
            )
            gain = float(cue.get("gain", 0.5))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX cue values at {index}") from exc

        if key not in narration:
            raise RuntimeError(f"SFX references unknown scene: {key}")

        scene_duration = narration[key]["duration"]
        if not math.isfinite(start) or start < 0 or start >= scene_duration:
            raise RuntimeError(f"SFX time outside narration scene: {key}")

        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"SFX gain outside 0..1 at cue {index}")

        path = safe_path(cue.get("file") or cue.get("path"), f"SFX cue {index}")
        require_file(path, f"SFX cue {index}", MIN_AUDIO_BYTES)

        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(f"SFX cue {index} has no audio stream")

        checksum = sha256_file(path)
        if cue.get("sha256") and cue["sha256"] != checksum:
            raise RuntimeError(f"SFX checksum mismatch at cue {index}")

        # Keep the complete effect whenever it fits; shift earlier if needed.
        latest_start = max(
            0.0,
            scene_duration - info["duration"] - SFX_END_MARGIN,
        )
        actual_start = min(start, latest_start)
        usable_duration = min(
            info["duration"],
            max(0.0, scene_duration - actual_start),
        )

        if usable_duration <= 0:
            assets["warnings"].append({
                "part": key[0],
                "scene": key[1],
                "warning": "SFX does not fit in scene",
            })
            continue

        if actual_start < start:
            assets["warnings"].append({
                "part": key[0],
                "scene": key[1],
                "declared_start": round(start, 3),
                "actual_start": round(actual_start, 3),
                "warning": "SFX shifted earlier to preserve its tail",
            })

        assets["sfx"].setdefault(key, []).append({
            "path": path,
            "sha256": checksum,
            "start": actual_start,
            "duration": usable_duration,
            "gain": min(1.0, gain * SFX_GAIN_MULTIPLIER),
            "kind": str(cue.get("kind", "effect")),
        })
        track_hashes[f"sfx_{index}"] = checksum

    for key in assets["sfx"]:
        assets["sfx"][key].sort(key=lambda cue: cue["start"])

    assets["fingerprint"] = digest_data({
        "sound_manifest": sha256_file(SOUND_MANIFEST),
        "sfx_validation": sha256_file(SFX_VALIDATION),
        "track_hashes": track_hashes,
        "gains": {
            "narration": NARRATION_GAIN,
            "music": MUSIC_GAIN,
            "ambient": AMBIENT_GAIN,
            "sfx_multiplier": SFX_GAIN_MULTIPLIER,
            "limiter": MASTER_LIMIT,
        },
    })

    return assets, manifest

def scene_fingerprint(photo, audio, sound_assets, mode, width, height, fps):
    return digest_data({
        "version": VERSION,
        "format": mode,
        "width": width,
        "height": height,
        "fps": fps,
        "photo_sha256": photo["video_sha256"],
        "audio_sha256": audio["audio_sha256"],
        "audio_duration": round(audio["duration"], 5),
        "sound_fingerprint": sound_assets["fingerprint"],
    })


def previous_manifest(mode):
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

    if str(data.get("format", "")).lower() != mode:
        return {}

    rows = data.get("completed_scenes", data.get("scenes", []))
    result = {}

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                key = (
                    int(row["part"]),
                    int(row.get("local_scene", row.get("scene"))),
                )
                result[key] = row
            except (KeyError, TypeError, ValueError):
                continue

    return result


def reusable_scene(
    output,
    previous,
    fingerprint,
    width,
    height,
    fps,
    duration,
):
    if not isinstance(previous, dict):
        return None

    if previous.get("status") != "completed":
        return None

    if previous.get("fingerprint") != fingerprint:
        return None

    if not output.is_file() or output.stat().st_size < MIN_VIDEO_BYTES:
        return None

    try:
        checksum = sha256_file(output)

        if previous.get("sha256") != checksum:
            return None

        info = probe(output)

        if not info["has_video"] or not info["has_audio"]:
            return None

        if (info["width"], info["height"]) != (width, height):
            return None

        validate_fps(info, fps, output.name)

        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            return None

        return info, checksum

    except Exception:
        return None


def render_scene(
    photo,
    audio,
    sound_assets,
    output,
    fingerprint,
    old_row,
    width,
    height,
    fps,
    timeline_offset,
    resume,
    skip_completed,
):
    output.parent.mkdir(parents=True, exist_ok=True)

    if resume and skip_completed:
        cached = reusable_scene(
            output,
            old_row,
            fingerprint,
            width,
            height,
            fps,
            audio["duration"],
        )
        if cached:
            log(
                f"REUSED Part {photo['part']} "
                f"Scene {photo['scene']}"
            )
            return cached[0], cached[1], True

    temporary = output.with_name(output.stem + ".rendering.mp4")
    temporary.unlink(missing_ok=True)

    video_filter = (
        f"scale={width}:{height}:"
        "force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,"
        f"fps={fps},format=yuv420p"
    )

    command = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1",
        "-i", str(photo["video"]),
        "-i", str(audio["audio"]),
    ]

    filters = [
        f"[0:v]{video_filter}[v]",
        f"[1:a]aresample={AUDIO_RATE},"
        f"volume={NARRATION_GAIN:.3f},"
        "asetpts=PTS-STARTPTS[narr]",
    ]
    mix_labels = ["[narr]"]
    input_index = 2

    for category, gain in (
        ("music", MUSIC_GAIN),
        ("ambient", AMBIENT_GAIN),
    ):
        track = sound_assets.get(category)
        if not track:
            continue

        if timeline_offset >= track["duration"]:
            raise RuntimeError(
                f"{category} track ends before scene "
                f"{photo['part']}/{photo['scene']}"
            )

        command.extend([
            "-ss", f"{timeline_offset:.4f}",
            "-i", str(track["path"]),
        ])

        label = category
        filters.append(
            f"[{input_index}:a]"
            f"aresample={AUDIO_RATE},"
            f"volume={gain:.3f},"
            f"atrim=duration={audio['duration']:.4f},"
            "asetpts=PTS-STARTPTS"
            f"[{label}]"
        )
        mix_labels.append(f"[{label}]")
        input_index += 1

    cues = sound_assets.get("sfx", {}).get(
        (photo["part"], photo["scene"]),
        [],
    )

    for cue_index, cue in enumerate(cues):
        available = max(
            0.0,
            audio["duration"] - cue["start"],
        )
        duration = min(cue["duration"], available)

        if duration <= 0:
            continue

        command.extend(["-i", str(cue["path"])])

        label = f"sfx{cue_index}"
        delay_ms = max(0, int(round(cue["start"] * 1000)))
        fade_duration = min(0.4, max(0.05, duration * 0.20))
        fade_start = max(0.0, duration - fade_duration)

        filters.append(
            f"[{input_index}:a]"
            f"aresample={AUDIO_RATE},"
            f"atrim=duration={duration:.4f},"
            "asetpts=PTS-STARTPTS,"
            f"volume={cue['gain']:.3f},"
            f"afade=t=out:st={fade_start:.4f}:"
            f"d={fade_duration:.4f},"
            f"adelay={delay_ms}|{delay_ms}"
            f"[{label}]"
        )

        mix_labels.append(f"[{label}]")
        input_index += 1

    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:"
          "duration=first:dropout_transition=0:normalize=0,"
        + f"alimiter=limit={MASTER_LIMIT:.3f}:level=disabled,"
        + f"aresample={AUDIO_RATE}[aout]"
    )

    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
        "-map", "[aout]",
        "-t", f"{audio['duration']:.4f}",
        "-r", str(fps),
        "-fps_mode", "cfr",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart",
        str(temporary),
    ])

    try:
        run(
            command,
            f"Render Part {photo['part']} Scene {photo['scene']}",
        )

        require_file(temporary, "Rendered scene")
        info = probe(temporary)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Rendered scene has no audio or video")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Rendered scene dimensions mismatch")

        validate_fps(info, fps, "new rendered scene")

        if abs(info["duration"] - audio["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError("Rendered scene duration mismatch")

        os.replace(temporary, output)

    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return probe(output), sha256_file(output), False


def concat_videos(paths, destination, width, height, fps):
    if not paths:
        raise RuntimeError(f"No input clips for {destination.name}")

    for path in paths:
        require_file(path, f"Concat input {path.name}")
        info = probe(path)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Concat input has no audio/video: {path}")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Concat dimensions mismatch: {path}")

        validate_fps(info, fps, path.name)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.stem + ".joining.mp4"
    )
    temporary.unlink(missing_ok=True)
    list_file = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="photo_motion_concat_",
            encoding="utf-8",
            delete=False,
        ) as stream:
            list_file = Path(stream.name)

            for path in paths:
                resolved = str(Path(path).resolve())
                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in concat path")

                stream.write(
                    "file '" + resolved.replace("'", "'\\''") + "'\n"
                )

        run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-r", str(fps),
                "-fps_mode", "cfr",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "19",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", str(AUDIO_RATE),
                "-movflags", "+faststart",
                str(temporary),
            ],
            f"Join {destination.name}",
        )

        require_file(temporary, "Joined video", 10000)
        info = probe(temporary)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError("Joined video has no audio/video")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError("Joined video dimensions mismatch")

        validate_fps(info, fps, destination.name)
        os.replace(temporary, destination)
        return probe(destination)

    finally:
        if list_file is not None:
            list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def build_manifest(
    mode,
    fps,
    width,
    height,
    parts,
    scenes,
    completed,
    part_rows,
    photo_hash,
    audio_hash,
    sound_fingerprint,
    status,
):
    ordered = [
        completed[key]
        for key in sorted(completed)
    ]

    return {
        "status": status,
        "version": VERSION,
        "format": mode,
        "fps": fps,
        "width": width,
        "height": height,
        "total_parts": parts,
        "parts": part_rows,
        "scenes_per_part": scenes,
        "expected_scene_count": parts * scenes,
        "completed_count": len(ordered),
        "captions_burned": False,
        "source_photo_manifest": (
            PHOTO_MANIFEST.relative_to(ROOT).as_posix()
        ),
        "source_audio_manifest": (
            AUDIO_MANIFEST.relative_to(ROOT).as_posix()
        ),
        "source_sound_manifest": (
            SOUND_MANIFEST.relative_to(ROOT).as_posix()
        ),
        "source_sfx_validation": (
            SFX_VALIDATION.relative_to(ROOT).as_posix()
        ),
        "source_photo_manifest_sha256": photo_hash,
        "source_audio_manifest_sha256": audio_hash,
        "source_sound_fingerprint": sound_fingerprint,
        "completed_scenes": ordered,
        "scenes": ordered,
        "audio_mix": {
            "narration_gain": NARRATION_GAIN,
            "music_gain": MUSIC_GAIN,
            "ambient_gain": AMBIENT_GAIN,
            "sfx_gain_multiplier": SFX_GAIN_MULTIPLIER,
            "master_limiter": MASTER_LIMIT,
            "sample_rate": AUDIO_RATE,
            "music_ambient_global_timeline": True,
            "sfx_tail_protection": True,
        },
    }


def main():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    parts = positive_int(get_parts(config), "PARTS")
    scenes = positive_int(get_scenes(config), "SCENES")
    fps = positive_int(get_fps(config), "FPS")

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if fps > 60:
        raise RuntimeError("FPS cannot exceed 60")

    width, height = (
        (720, 1280) if mode == "short"
        else (1280, 720)
    )

    expected_count = parts * scenes

    photo_map, photo_data = load_photo_map(
        parts, scenes, mode, width, height, fps
    )
    audio_map, audio_data = load_audio_map(parts, scenes)

    if set(photo_map) != set(audio_map):
        raise RuntimeError(
            "Photo Motion and narration scene mappings differ"
        )

    sound_assets, sound_data = load_sound_assets(
        parts, audio_map
    )

    photo_hash = sha256_file(PHOTO_MANIFEST)
    audio_hash = sha256_file(AUDIO_MANIFEST)

    resume = config_bool(config, "RESUME_ENABLED", True)
    skip_completed = config_bool(
        config, "SKIP_COMPLETED_SCENES", True
    )

    previous = previous_manifest(mode)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    timeline_offsets = {}
    total_duration = 0.0

    for part in range(1, parts + 1):
        for scene in range(1, scenes + 1):
            key = (part, scene)
            timeline_offsets[key] = total_duration
            total_duration += audio_map[key]["duration"]

    for category in ("music", "ambient"):
        track = sound_assets.get(category)
        if track and track["duration"] + 0.5 < total_duration:
            raise RuntimeError(
                f"{category} track too short: "
                f"{track['duration']:.2f}s; narration "
                f"{total_duration:.2f}s"
            )

    log("=" * 65)
    log("PHOTO MOTION VIDEO RENDERER:", VERSION)
    log("FORMAT:", mode)
    log("RESOLUTION:", f"{width}x{height}")
    log("FPS:", fps)
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("TOTAL SCENES:", expected_count)
    log("NARRATION DURATION:", round(total_duration, 2))
    log("MUSIC:", bool(sound_assets["music"]))
    log("AMBIENT:", bool(sound_assets["ambient"]))
    log(
        "SFX CUES:",
        sum(len(value) for value in sound_assets["sfx"].values()),
    )

    for warning in sound_assets["warnings"][:30]:
        log("SFX WARNING:", warning)

    completed = {}
    part_rows = []
    new_renders = 0

    def save_checkpoint(status="in_progress"):
        write_json(
            RENDER_MANIFEST,
            build_manifest(
                mode,
                fps,
                width,
                height,
                parts,
                scenes,
                completed,
                part_rows,
                photo_hash,
                audio_hash,
                sound_assets["fingerprint"],
                status,
            ),
        )

    save_checkpoint()

    try:
        for part in range(1, parts + 1):
            log("")
            log("#" * 55)
            log("RENDERING PART", part)
            log("#" * 55)

            scene_paths = []

            for scene in range(1, scenes + 1):
                key = (part, scene)
                photo = photo_map[key]
                audio = audio_map[key]

                fingerprint = scene_fingerprint(
                    photo,
                    audio,
                    sound_assets,
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

                info, checksum, reused = render_scene(
                    photo,
                    audio,
                    sound_assets,
                    output,
                    fingerprint,
                    previous.get(key),
                    width,
                    height,
                    fps,
                    timeline_offsets[key],
                    resume,
                    skip_completed,
                )

                new_renders += int(not reused)

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
                    "source_rendered_video": (
                        output.relative_to(ROOT).as_posix()
                    ),
                    "video": output.relative_to(ROOT).as_posix(),
                    "output": output.relative_to(ROOT).as_posix(),
                    "duration": round(info["duration"], 4),
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "fingerprint": fingerprint,
                    "sha256": checksum,
                    "captions_burned": False,
                    "cache_reused": reused,
                    "audio_mix": {
                        "narration": True,
                        "music": bool(sound_assets["music"]),
                        "ambient": bool(sound_assets["ambient"]),
                        "sfx_count": len(
                            sound_assets["sfx"].get(key, [])
                        ),
                        "global_timeline_offset": round(
                            timeline_offsets[key], 4
                        ),
                    },
                }

                completed[key] = row
                scene_paths.append(output)
                save_checkpoint()

                log(
                    f"CHECKPOINT {len(completed)}/{expected_count}: "
                    f"Part {part} Scene {scene}"
                )

            part_output = PART_DIR / f"part_{part:02d}.mp4"

            part_info = concat_videos(
                scene_paths,
                part_output,
                width,
                height,
                fps,
            )

            part_rows = [
                row for row in part_rows
                if int(row.get("part", 0)) != part
            ]
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
            part_rows.sort(key=lambda row: int(row["part"]))
            save_checkpoint()

            log("PART COMPLETE:", part_output.relative_to(ROOT))

        if len(completed) != expected_count:
            raise RuntimeError(
                f"Rendered {len(completed)}/{expected_count} scenes"
            )

        ordered = [
            completed[(part, scene)]
            for part in range(1, parts + 1)
            for scene in range(1, scenes + 1)
        ]

        for index, row in enumerate(ordered, 1):
            if int(row["global_scene"]) != index:
                raise RuntimeError(
                    f"Global scene order mismatch at {index}"
                )

            path = safe_path(row["video"], f"rendered scene {index}")
            if sha256_file(path) != row["sha256"]:
                raise RuntimeError(
                    f"Rendered scene checksum mismatch: {index}"
                )

            info = probe(path)
            if not info["has_video"] or not info["has_audio"]:
                raise RuntimeError(
                    f"Rendered scene {index} lacks audio/video"
                )

            if (info["width"], info["height"]) != (width, height):
                raise RuntimeError(
                    f"Rendered scene {index} dimensions mismatch"
                )

            validate_fps(info, fps, f"final scene {index}")

        pre_caption = VIDEO_DIR / "pre_caption_render.mp4"

        concat_videos(
            [
                PART_DIR / f"part_{part:02d}.mp4"
                for part in range(1, parts + 1)
            ],
            pre_caption,
            width,
            height,
            fps,
        )

        pre_info = probe(pre_caption)

        if not pre_info["has_audio"]:
            raise RuntimeError("Pre-caption video has no audio stream")

        if (pre_info["width"], pre_info["height"]) != (width, height):
            raise RuntimeError("Pre-caption video dimensions mismatch")

        validate_fps(pre_info, fps, "pre-caption video")

        final_manifest = build_manifest(
            mode,
            fps,
            width,
            height,
            parts,
            scenes,
            completed,
            part_rows,
            photo_hash,
            audio_hash,
            sound_assets["fingerprint"],
            "completed",
        )

        final_manifest.update({
            "pre_caption_video": (
                pre_caption.relative_to(ROOT).as_posix()
            ),
            "pre_caption_sha256": sha256_file(pre_caption),
            "pre_caption_duration": round(pre_info["duration"], 4),
            "captions_burned": False,
            "rendered_new": new_renders,
            "rendered_reused": sum(
                1 for row in completed.values()
                if row.get("cache_reused") is True
            ),
            "audio_mix_summary": {
                "narration_seconds": round(total_duration, 3),
                "music_included": bool(sound_assets["music"]),
                "ambient_included": bool(sound_assets["ambient"]),
                "sfx_cues_included": sum(
                    len(value)
                    for value in sound_assets["sfx"].values()
                ),
                "sfx_adjustments": len(sound_assets["warnings"]),
                "narration_gain": NARRATION_GAIN,
                "music_gain": MUSIC_GAIN,
                "ambient_gain": AMBIENT_GAIN,
                "sfx_gain_multiplier": SFX_GAIN_MULTIPLIER,
                "master_limiter": MASTER_LIMIT,
            },
        })

        write_json(RENDER_MANIFEST, final_manifest)

    except Exception as exc:
        save_checkpoint("failed")
        log("RENDER FAILED:", str(exc))
        raise

    log("=" * 65)
    log("RENDER AND AUDIO MIX COMPLETED")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES:", expected_count)
    log("NEW RENDERS:", new_renders)
    log(
        "REUSED RENDERS:",
        sum(
            1 for row in completed.values()
            if row.get("cache_reused") is True
        ),
    )
    log("PRE-CAPTION VIDEO:", "output/videos/pre_caption_render.mp4")
    log("MANIFEST:", RENDER_MANIFEST.relative_to(ROOT))
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