#!/usr/bin/env python3
"""Render ordered Photo Motion scenes with narration, music, ambience and SFX."""

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
CHECKPOINT_FILE = OUT / "checkpoints" / "render_video_progress.json"

VERSION = "render-video-v38-role-aware-resumable"
AUDIO_RATE = 48000
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
FPS_TOLERANCE = 0.03
DURATION_TOLERANCE = 1.25
SFX_END_MARGIN = 0.08

NARRATION_GAIN = 1.0
MUSIC_GAIN = 0.30
AMBIENT_GAIN = 0.22
SFX_GAIN_MULTIPLIER = 1.65
MASTER_LIMIT = 0.95

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}


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


def read_json(path, label, required=True, default=None):
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"{label} missing: {path}")
        return default

    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig")
        )
    except Exception as exc:
        raise RuntimeError(f"Invalid {label}: {exc}") from exc

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
    command = [str(item) for item in command]

    result = subprocess.run(
        command,
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
    if not value or value in {"0/0", "N/A"}:
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
            "format=duration:"
            "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
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

    fps_value = None

    if video:
        fps_value = (
            video.get("avg_frame_rate")
            or video.get("r_frame_rate")
        )

    return {
        "duration": duration,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "fps": parse_fps(fps_value),
    }


def validate_fps(info, expected, label):
    actual = info.get("fps")

    if actual is None or abs(actual - float(expected)) > FPS_TOLERANCE:
        raise RuntimeError(
            f"FPS mismatch at {label}: {actual}, expected {expected}"
        )


def positive_int(value, label):
    if isinstance(value, bool):
        raise RuntimeError(f"Invalid {label}: {value!r}")

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if number < 1:
        raise RuntimeError(f"{label} must be positive")

    return number


def finite_number(value, label, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if not math.isfinite(number):
        raise RuntimeError(f"{label} must be finite")

    if minimum is not None and number < minimum:
        raise RuntimeError(f"{label} must be at least {minimum}")

    if maximum is not None and number > maximum:
        raise RuntimeError(f"{label} cannot exceed {maximum}")

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
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} must be a JSON object")

    if data.get("status") != "completed":
        raise RuntimeError(
            f"{label} is not completed: {data.get('status')}"
        )


def normalize_role(row):
    value = str(
        row.get("role")
        or row.get("scene_type")
        or row.get("type")
        or "main"
    ).strip().lower()

    aliases = {
        "opening": "hook",
        "intro": "hook",
        "part_hook": "hook",
        "story": "main",
        "main_scene": "main",
        "cliffhanger": "suspense",
        "ending": "resolution",
        "final": "resolution",
    }

    value = aliases.get(value, value)

    if value not in ROLE_ORDER:
        raise RuntimeError(f"Unsupported scene role: {value!r}")

    return value


def row_scene_id(row, label):
    value = str(row.get("scene_id") or row.get("id") or "").strip()

    if not value:
        raise RuntimeError(f"{label}: scene_id is missing")

    return value


def media_path_from_row(row, keys, label):
    for key in keys:
        value = row.get(key)

        if isinstance(value, str) and value.strip():
            return safe_path(value, label)

    raise RuntimeError(f"{label}: no path field found")


def expected_main_keys(parts, scenes):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }


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

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Photo Motion manifest has no scenes")

    if int(data.get("completed_count", -1)) != len(rows):
        raise RuntimeError("Photo Motion completed_count mismatch")

    by_id = {}
    main_map = {}
    role_counts = {
        role: {} for role in ROLE_ORDER
    }

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Photo Motion scene {index} is incomplete")

        scene_id = row_scene_id(row, f"Photo Motion row {index}")

        if scene_id in by_id:
            raise RuntimeError(f"Duplicate Photo Motion scene_id: {scene_id}")

        part = positive_int(row.get("part"), "Photo Motion part")
        if part > parts:
            raise RuntimeError(f"{scene_id}: part exceeds configured PARTS")

        role = normalize_role(row)

        global_scene = positive_int(
            row.get("global_scene", row.get("scene")),
            f"{scene_id} global_scene",
        )

        local_scene = positive_int(
            row.get("local_scene", row.get("scene", index)),
            f"{scene_id} local_scene",
        )

        video = media_path_from_row(
            row,
            ("video", "output", "path"),
            f"Photo Motion {scene_id}",
        )

        require_file(video, f"Photo Motion {scene_id}")
        actual_hash = sha256_file(video)

        if row.get("video_sha256") != actual_hash:
            raise RuntimeError(
                f"Photo Motion checksum mismatch: {scene_id}"
            )

        info = probe(video)

        if not info["has_video"]:
            raise RuntimeError(f"Photo Motion {scene_id} has no video")

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"Photo Motion {scene_id} dimensions mismatch"
            )

        validate_fps(info, fps, scene_id)

        record = {
            "scene_id": scene_id,
            "part": part,
            "role": role,
            "local_scene": local_scene,
            "global_scene": global_scene,
            "video": video,
            "video_sha256": actual_hash,
            "duration": info["duration"],
            "width": width,
            "height": height,
            "fps": fps,
        }

        by_id[scene_id] = record
        role_counts[role][part] = (
            role_counts[role].get(part, 0) + 1
        )

        if role == "main":
            source_scene = positive_int(
                row.get(
                    "source_scene_number",
                    row.get("local_scene", row.get("scene")),
                ),
                f"{scene_id} main scene number",
            )

            key = (part, source_scene)

            if key in main_map:
                raise RuntimeError(
                    f"Duplicate Photo Motion main scene: {key}"
                )

            main_map[key] = record

    expected = expected_main_keys(parts, scenes)

    if set(main_map) != expected:
        raise RuntimeError(
            "Photo Motion main scene mapping mismatch; "
            f"missing={sorted(expected - set(main_map))}; "
            f"extra={sorted(set(main_map) - expected)}"
        )

    for part in range(1, parts + 1):
        if role_counts["hook"].get(part, 0) != 1:
            raise RuntimeError(
                f"Part {part} must have exactly one Hook clip"
            )

        if part < parts:
            if role_counts["suspense"].get(part, 0) != 1:
                raise RuntimeError(
                    f"Part {part} must have exactly one Suspense clip"
                )

            if role_counts["resolution"].get(part, 0):
                raise RuntimeError(
                    f"Non-final part {part} cannot have Resolution"
                )
        else:
            if role_counts["resolution"].get(part, 0) != 1:
                raise RuntimeError(
                    "Final part must have exactly one Resolution clip"
                )

            if role_counts["suspense"].get(part, 0):
                raise RuntimeError(
                    "Final part cannot have a Suspense clip"
                )

    ordered = sorted(
        by_id.values(),
        key=lambda item: (
            item["part"],
            ROLE_ORDER[item["role"]],
            item["local_scene"] if item["role"] == "main"
            else item["global_scene"],
            item["global_scene"],
        ),
    )

    if [item["global_scene"] for item in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        raise RuntimeError(
            "Photo Motion global scene sequence is not continuous"
        )

    return by_id, main_map, ordered, data


def load_audio_map(parts, scenes):
    data = read_json(AUDIO_MANIFEST, "Narration audio manifest")
    validate_completed(data, "Narration audio manifest")

    rows = (
        data.get("jobs")
        or data.get("audio_jobs")
        or data.get("scenes")
    )

    if not isinstance(rows, list):
        raise RuntimeError("Narration audio manifest has no jobs list")

    mapping = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration job {index}")

        scene_id = row_scene_id(row, f"Narration job {index}")

        if scene_id in mapping:
            raise RuntimeError(f"Duplicate narration scene_id: {scene_id}")

        if str(row.get("status", "")).lower() not in {
            "completed", "reused",
        }:
            raise RuntimeError(
                f"Narration job {scene_id} is incomplete"
            )

        part = positive_int(row.get("part"), "Narration part")
        role = normalize_role(row)

        audio = media_path_from_row(
            row,
            ("output", "audio_path", "path", "file", "audio"),
            f"Narration {scene_id}",
        )

        require_file(audio, f"Narration {scene_id}", MIN_AUDIO_BYTES)

        info = probe(audio)

        if not info["has_audio"]:
            raise RuntimeError(
                f"Narration {scene_id} has no audio stream"
            )

        checksum = sha256_file(audio)
        declared = row.get("sha256") or row.get("audio_sha256")

        if declared and declared != checksum:
            raise RuntimeError(
                f"Narration checksum mismatch: {scene_id}"
            )

        mapping[scene_id] = {
            "scene_id": scene_id,
            "part": part,
            "role": role,
            "audio": audio,
            "audio_sha256": checksum,
            "duration": info["duration"],
            "provider": row.get("provider"),
            "model": row.get("model"),
        }

    expected_count = parts * scenes + sum(
        1 for part in range(1, parts + 1)
    ) + max(0, parts - 1) + 1

    if len(mapping) != expected_count:
        raise RuntimeError(
            "Narration job count mismatch: "
            f"expected {expected_count}, found {len(mapping)}"
        )

    return mapping, data


def validate_photo_audio_pairing(photo_by_id, audio_by_id):
    photo_ids = set(photo_by_id)
    audio_ids = set(audio_by_id)

    if photo_ids != audio_ids:
        raise RuntimeError(
            "Photo Motion and narration scene IDs differ; "
            f"missing_audio={sorted(photo_ids - audio_ids)[:20]}; "
            f"missing_video={sorted(audio_ids - photo_ids)[:20]}"
        )

    for scene_id, photo in photo_by_id.items():
        audio = audio_by_id[scene_id]

        if photo["part"] != audio["part"]:
            raise RuntimeError(
                f"{scene_id}: Photo Motion and narration part mismatch"
            )

        if photo["role"] != audio["role"]:
            raise RuntimeError(
                f"{scene_id}: Photo Motion and narration role mismatch"
            )


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

        if (
            not isinstance(record, dict)
            or record.get("status") != "completed"
        ):
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

        scene_id = str(cue.get("scene_id") or "").strip()

        try:
            part = positive_int(cue.get("part"), "SFX part")
        except RuntimeError:
            part = None

        if scene_id:
            if scene_id not in narration:
                raise RuntimeError(
                    f"SFX references unknown scene_id: {scene_id}"
                )
            key = scene_id
        else:
            try:
                scene = positive_int(cue.get("scene"), "SFX scene")
            except RuntimeError as exc:
                raise RuntimeError(
                    f"SFX cue {index} has no scene mapping"
                ) from exc

            candidates = [
                sid for sid, row in narration.items()
                if row["part"] == part
                and row.get("scene") == scene
            ]

            if len(candidates) != 1:
                raise RuntimeError(
                    f"SFX cue {index} mapping is ambiguous; "
                    "scene_id is required for extra scenes"
                )

            key = candidates[0]

        try:
            start = finite_number(
                cue.get("start_seconds", cue.get("time")),
                f"SFX cue {index} start",
                minimum=0,
            )
            gain = finite_number(
                cue.get("gain", 0.5),
                f"SFX cue {index} gain",
                minimum=0,
                maximum=1,
            )
        except RuntimeError:
            raise

        scene_duration = narration[key]["duration"]

        if start >= scene_duration:
            raise RuntimeError(
                f"SFX time outside narration scene: {key}"
            )

        path = media_path_from_row(
            cue,
            ("file", "path"),
            f"SFX cue {index}",
        )

        require_file(path, f"SFX cue {index}", MIN_AUDIO_BYTES)

        info = probe(path)

        if not info["has_audio"]:
            raise RuntimeError(
                f"SFX cue {index} has no audio stream"
            )

        checksum = sha256_file(path)

        if cue.get("sha256") and cue["sha256"] != checksum:
            raise RuntimeError(
                f"SFX checksum mismatch at cue {index}"
            )

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
                "scene_id": key,
                "warning": "SFX does not fit in scene",
            })
            continue

        if actual_start < start:
            assets["warnings"].append({
                "scene_id": key,
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
        assets["sfx"][key].sort(key=lambda item: item["start"])

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
        "scene_id": photo["scene_id"],
        "role": photo["role"],
        "photo_sha256": photo["video_sha256"],
        "audio_sha256": audio["audio_sha256"],
        "audio_duration": round(audio["duration"], 5),
        "sound_fingerprint": sound_assets["fingerprint"],
    })


def previous_manifest(mode):
    data = read_json(
        RENDER_MANIFEST,
        "previous render manifest",
        required=False,
        default={},
    )

    if not isinstance(data, dict):
        return {}

    if str(data.get("format", "")).lower() != mode:
        return {}

    rows = (
        data.get("all_completed_scenes")
        or data.get("completed_scenes")
        or data.get("scenes")
        or []
    )

    result = {}

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            scene_id = str(row.get("scene_id") or "").strip()

            if scene_id:
                result[scene_id] = row

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

    if (
        not output.is_file()
        or output.stat().st_size < MIN_VIDEO_BYTES
    ):
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
            log(f"REUSED {photo['scene_id']} ({photo['role']})")
            return cached[0], cached[1], True

    temporary = output.with_name(
        output.stem + ".rendering.mp4"
    )
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
                f"{category} track ends before {photo['scene_id']}"
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
        photo["scene_id"], []
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
            f"Render {photo['scene_id']}",
        )

        require_file(temporary, "Rendered scene")
        info = probe(temporary)

        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(
                f"{photo['scene_id']}: output lacks audio or video"
            )

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"{photo['scene_id']}: output dimensions mismatch"
            )

        validate_fps(info, fps, photo["scene_id"])

        if abs(info["duration"] - audio["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"{photo['scene_id']}: duration mismatch"
            )

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
            raise RuntimeError(
                f"Concat input lacks audio/video: {path}"
            )

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"Concat dimensions mismatch: {path}"
            )

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
                    raise RuntimeError(
                        "Invalid newline in concat path"
                    )

                escaped = resolved.replace("'", "'\\''")
                stream.write(f"file '{escaped}'\n")

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
            raise RuntimeError(
                f"Joined video lacks audio/video: {destination.name}"
            )

        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(
                f"Joined video dimensions mismatch: {destination.name}"
            )

        validate_fps(info, fps, destination.name)

        os.replace(temporary, destination)

        return probe(destination)

    finally:
        if list_file is not None:
            list_file.unlink(missing_ok=True)

        temporary.unlink(missing_ok=True)


def make_render_row(photo, audio, output, info, checksum, fingerprint, reused):
    main_scene = photo["role"] == "main"

    if main_scene:
        expected_global = (
            (photo["part"] - 1) * CURRENT_SCENES
            + photo["local_scene"]
        )
    else:
        expected_global = None

    return {
        "scene_id": photo["scene_id"],
        "role": photo["role"],
        "scene_type": photo["role"],
        "part": photo["part"],
        "scene": photo["local_scene"],
        "local_scene": photo["local_scene"],
        "global_scene": expected_global,
        "photo_motion_global_scene": photo["global_scene"],
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
        "duration": round(info["duration"], 5),
        "width": CURRENT_WIDTH,
        "height": CURRENT_HEIGHT,
        "fps": CURRENT_FPS,
        "fingerprint": fingerprint,
        "sha256": checksum,
        "captions_burned": False,
        "cache_reused": reused,
        "audio_mix": {
            "narration": True,
            "music": bool(CURRENT_SOUND_ASSETS["music"]),
            "ambient": bool(CURRENT_SOUND_ASSETS["ambient"]),
            "sfx_count": len(
                CURRENT_SOUND_ASSETS["sfx"].get(
                    photo["scene_id"], []
                )
            ),
            "global_timeline_offset": round(
                CURRENT_TIMELINE_OFFSETS[photo["scene_id"]],
                5,
            ),
        },
    }


CURRENT_SCENES = 0
CURRENT_WIDTH = 0
CURRENT_HEIGHT = 0
CURRENT_FPS = 0
CURRENT_SOUND_ASSETS = {}
CURRENT_TIMELINE_OFFSETS = {}


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
    error=None,
):
    all_rows = [
        completed[item["scene_id"]]
        for item in CURRENT_ORDERED_PHOTOS
        if item["scene_id"] in completed
    ]

    main_rows = [
        row for row in all_rows
        if row.get("role") == "main"
    ]

    extra_rows = [
        row for row in all_rows
        if row.get("role") != "main"
    ]

    result = {
        "status": status,
        "version": VERSION,
        "format": mode,
        "fps": fps,
        "width": width,
        "height": height,
        "total_parts": parts,
        "parts": sorted(
            part_rows,
            key=lambda row: int(row["part"]),
        ),
        "scenes_per_part": scenes,
        "expected_scene_count": parts * scenes,
        "completed_count": len(main_rows),
        "all_completed_scene_count": len(all_rows),
        "extra_scene_count": len(extra_rows),
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

        # Main-only list preserves the existing caption-stage contract.
        "completed_scenes": main_rows,
        "scenes": main_rows,

        # Hook, suspense and resolution remain separately identifiable.
        "extra_scenes": extra_rows,
        "all_completed_scenes": all_rows,

        "audio_mix": {
            "narration_gain": NARRATION_GAIN,
            "music_gain": MUSIC_GAIN,
            "ambient_gain": AMBIENT_GAIN,
            "sfx_gain_multiplier": SFX_GAIN_MULTIPLIER,
            "master_limiter": MASTER_LIMIT,
            "sample_rate": AUDIO_RATE,
            "music_ambient_global_timeline": True,
            "music_ambient_looped_if_needed": True,
            "sfx_tail_protection": True,
        },
    }

    if error:
        result["last_error"] = str(error)[:2000]

    return result


def build_progress(completed, ordered, status, error=None):
    rows = []

    for item in ordered:
        record = completed.get(item["scene_id"])

        rows.append({
            "scene_id": item["scene_id"],
            "part": item["part"],
            "role": item["role"],
            "global_scene": item["global_scene"],
            "status": (
                record.get("status", "pending")
                if isinstance(record, dict)
                else "pending"
            ),
            "video": (
                record.get("video")
                if isinstance(record, dict)
                else None
            ),
        })

    result = {
        "version": VERSION,
        "stage": "render_video",
        "status": status,
        "total_scenes": len(ordered),
        "completed_scenes": sum(
            row["status"] == "completed" for row in rows
        ),
        "jobs": rows,
    }

    if error:
        result["last_error"] = str(error)[:2000]

    return result


def verify_rendered_scene(row, width, height, fps):
    output = safe_path(
        row.get("video"),
        f"Rendered scene {row.get('scene_id')}",
    )

    require_file(output, "Rendered scene")

    checksum = sha256_file(output)

    if checksum != row.get("sha256"):
        raise RuntimeError(
            f"Rendered scene checksum mismatch: {row.get('scene_id')}"
        )

    info = probe(output)

    if not info["has_video"] or not info["has_audio"]:
        raise RuntimeError(
            f"Rendered scene lacks audio/video: {row.get('scene_id')}"
        )

    if (info["width"], info["height"]) != (width, height):
        raise RuntimeError(
            f"Rendered scene dimensions mismatch: "
            f"{row.get('scene_id')}"
        )

    validate_fps(info, fps, str(row.get("scene_id")))

    if abs(info["duration"] - float(row.get("duration", 0))) > (
        DURATION_TOLERANCE
    ):
        raise RuntimeError(
            f"Rendered scene duration mismatch: {row.get('scene_id')}"
        )

    return info


def main():
    global CURRENT_SCENES
    global CURRENT_WIDTH
    global CURRENT_HEIGHT
    global CURRENT_FPS
    global CURRENT_SOUND_ASSETS
    global CURRENT_TIMELINE_OFFSETS
    global CURRENT_ORDERED_PHOTOS

    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(
                f"Required command not installed: {command}"
            )

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

    CURRENT_SCENES = scenes
    CURRENT_WIDTH = width
    CURRENT_HEIGHT = height
    CURRENT_FPS = fps

    expected_main_count = parts * scenes

    photo_by_id, photo_main, ordered_photos, photo_data = (
        load_photo_map(
            parts,
            scenes,
            mode,
            width,
            height,
            fps,
        )
    )

    audio_by_id, audio_data = load_audio_map(parts, scenes)

    validate_photo_audio_pairing(
        photo_by_id,
        audio_by_id,
    )

    sound_assets, sound_data = load_sound_assets(
        parts,
        audio_by_id,
    )

    if str(sound_data.get("format", "")).lower() != mode:
        raise RuntimeError("Sound design FORMAT does not match Input")

    photo_hash = sha256_file(PHOTO_MANIFEST)
    audio_hash = sha256_file(AUDIO_MANIFEST)

    resume = config_bool(config, "RESUME_ENABLED", True)
    skip_completed = config_bool(
        config,
        "SKIP_COMPLETED_SCENES",
        True,
    )

    previous = previous_manifest(mode)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Music and ambience use one continuous timeline across all parts,
    # including Hook, Suspense and Resolution clips.
    timeline_offsets = {}
    total_duration = 0.0

    for photo in ordered_photos:
        audio = audio_by_id[photo["scene_id"]]
        timeline_offsets[photo["scene_id"]] = total_duration
        total_duration += audio["duration"]

    for category in ("music", "ambient"):
        track = sound_assets.get(category)

        if track and track["duration"] <= 0:
            raise RuntimeError(
                f"{category} track has invalid duration"
            )

    CURRENT_SOUND_ASSETS = sound_assets
    CURRENT_TIMELINE_OFFSETS = timeline_offsets
    CURRENT_ORDERED_PHOTOS = ordered_photos

    log("=" * 65)
    log("PHOTO MOTION VIDEO RENDERER:", VERSION)
    log("FORMAT:", mode)
    log("RESOLUTION:", f"{width}x{height}")
    log("FPS:", fps)
    log("PARTS:", parts)
    log("MAIN SCENES PER PART:", scenes)
    log("MAIN SCENES:", expected_main_count)
    log("ALL SCENES INCLUDING HOOKS/EXTRAS:", len(ordered_photos))
    log("NARRATION DURATION:", round(total_duration, 2))
    log("MUSIC:", bool(sound_assets["music"]))
    log("AMBIENT:", bool(sound_assets["ambient"]))
    log(
        "SFX CUES:",
        sum(
            len(value)
            for value in sound_assets["sfx"].values()
        ),
    )

    for warning in sound_assets["warnings"][:30]:
        log("SFX WARNING:", warning)

    completed = {}
    part_rows = []
    new_renders = 0
    reused_renders = 0

    def save_checkpoint(status="in_progress", error=None):
        manifest = build_manifest(
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
            error,
        )

        write_json(RENDER_MANIFEST, manifest)

        write_json(
            CHECKPOINT_FILE,
            build_progress(
                completed,
                ordered_photos,
                status,
                error,
            ),
        )

    save_checkpoint()

    try:
        for part in range(1, parts + 1):
            log("")
            log("#" * 55)
            log("RENDERING PART", part)
            log("#" * 55)

            part_photos = [
                item for item in ordered_photos
                if item["part"] == part
            ]

            if not part_photos:
                raise RuntimeError(
                    f"Part {part} has no ordered Photo Motion clips"
                )

            scene_paths = []

            for item in part_photos:
                scene_id = item["scene_id"]
                audio = audio_by_id[scene_id]

                fingerprint = scene_fingerprint(
                    item,
                    audio,
                    sound_assets,
                    mode,
                    width,
                    height,
                    fps,
                )

                safe_id = "".join(
                    char if char.isalnum() or char in "-_" else "_"
                    for char in scene_id
                ).strip("_")

                if not safe_id:
                    raise RuntimeError(
                        f"Unsafe empty scene filename: {scene_id}"
                    )

                output = (
                    RENDER_DIR
                    / f"part_{part:02d}"
                    / f"{safe_id}.mp4"
                )

                info, checksum, reused = render_scene(
                    item,
                    audio,
                    sound_assets,
                    output,
                    fingerprint,
                    previous.get(scene_id),
                    width,
                    height,
                    fps,
                    timeline_offsets[scene_id],
                    resume,
                    skip_completed,
                )

                new_renders += int(not reused)
                reused_renders += int(reused)

                row = make_render_row(
                    item,
                    audio,
                    output,
                    info,
                    checksum,
                    fingerprint,
                    reused,
                )

                completed[scene_id] = row
                scene_paths.append(output)

                save_checkpoint()

                log(
                    f"CHECKPOINT {len(completed)}/{len(ordered_photos)} "
                    f"part={part} role={item['role']} "
                    f"scene_id={scene_id}"
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
                "scene_count": sum(
                    item["role"] == "main" for item in part_photos
                ),
                "total_clip_count": len(part_photos),
                "roles": [
                    item["role"] for item in part_photos
                ],
                "path": part_output.relative_to(ROOT).as_posix(),
                "output": part_output.relative_to(ROOT).as_posix(),
                "duration": round(part_info["duration"], 5),
                "sha256": sha256_file(part_output),
                "fps": fps,
                "width": width,
                "height": height,
            })

            part_rows.sort(key=lambda row: int(row["part"]))
            save_checkpoint()

            log(
                "PART COMPLETE:",
                part_output.relative_to(ROOT),
            )

        if len(completed) != len(ordered_photos):
            raise RuntimeError(
                f"Rendered {len(completed)}/{len(ordered_photos)} clips"
            )

        # Verify every scene clip in the same global order.
        for item in ordered_photos:
            row = completed.get(item["scene_id"])

            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Render manifest row missing: {item['scene_id']}"
                )

            if row.get("status") != "completed":
                raise RuntimeError(
                    f"Render incomplete: {item['scene_id']}"
                )

            verify_rendered_scene(
                row,
                width,
                height,
                fps,
            )

        if len(part_rows) != parts:
            raise RuntimeError(
                f"Expected {parts} part videos, found {len(part_rows)}"
            )

        for part, row in enumerate(part_rows, 1):
            if int(row["part"]) != part:
                raise RuntimeError(
                    f"Part output ordering mismatch at part {part}"
                )

            part_path = safe_path(
                row["path"],
                f"Part {part} video",
            )

            if sha256_file(part_path) != row.get("sha256"):
                raise RuntimeError(
                    f"Part {part} video checksum mismatch"
                )

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

        if not pre_info["has_video"] or not pre_info["has_audio"]:
            raise RuntimeError(
                "Pre-caption video lacks audio/video"
            )

        if (pre_info["width"], pre_info["height"]) != (width, height):
            raise RuntimeError(
                "Pre-caption video dimensions mismatch"
            )

        validate_fps(pre_info, fps, "pre-caption video")

        main_rows = [
            completed[item["scene_id"]]
            for item in ordered_photos
            if item["role"] == "main"
        ]

        extra_rows = [
            completed[item["scene_id"]]
            for item in ordered_photos
            if item["role"] != "main"
        ]

        if len(main_rows) != expected_main_count:
            raise RuntimeError(
                "Main scene render count mismatch"
            )

        if len(extra_rows) != parts * 2:
            raise RuntimeError(
                "Hook/Suspense/Resolution render count mismatch"
            )

        # The caption renderer expects main-scene numbering to start at 1.
        # Keep Photo Motion's actual global index separately.
        for index, row in enumerate(main_rows, 1):
            expected_global = index

            if row.get("global_scene") != expected_global:
                raise RuntimeError(
                    f"Main scene sequence mismatch at {index}"
                )

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
            "pre_caption_duration": round(pre_info["duration"], 5),
            "captions_burned": False,
            "rendered_new": new_renders,
            "rendered_reused": reused_renders,
            "main_scene_count": len(main_rows),
            "extra_scene_count": len(extra_rows),
            "hook_count": sum(
                row.get("role") == "hook" for row in extra_rows
            ),
            "suspense_count": sum(
                row.get("role") == "suspense" for row in extra_rows
            ),
            "resolution_count": sum(
                row.get("role") == "resolution" for row in extra_rows
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
                "sample_rate": AUDIO_RATE,
                "music_ambient_global_timeline": True,
                "music_ambient_looped_if_needed": True,
            },
        })

        write_json(RENDER_MANIFEST, final_manifest)

        write_json(
            CHECKPOINT_FILE,
            build_progress(
                completed,
                ordered_photos,
                "completed",
            ),
        )

    except Exception as exc:
        save_checkpoint("failed", exc)
        log("RENDER FAILED:", str(exc))
        raise

    log("=" * 65)
    log("RENDER AND AUDIO MIX COMPLETED")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("MAIN SCENES:", expected_main_count)
    log("ALL SCENE CLIPS:", len(ordered_photos))
    log("NEW RENDERS:", new_renders)
    log("REUSED RENDERS:", reused_renders)
    log("PRE-CAPTION VIDEO:", "output/videos/pre_caption_render.mp4")
    log("MANIFEST:", RENDER_MANIFEST.relative_to(ROOT))
    log("CHECKPOINT:", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("RENDER INTERRUPTED; completed scene checkpoints retained.")
        sys.exit(130)
    except Exception as exc:
        log("RENDER FAILED:", str(exc))
        sys.exit(1)