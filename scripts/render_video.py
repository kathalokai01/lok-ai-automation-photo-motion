#!/usr/bin/env python3
"""Validated, resumable, role-aware scene rendering and audio mixing."""

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
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"
CHECKPOINT_FILE = OUT / "checkpoints" / "render_video_progress.json"

VERSION = "render-video-v51-role-safe-measured-audio-part-only"
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

ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}


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
        separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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


def read_json(path, label, required=True, default=None):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"{label} missing: {path}")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid {label}: {exc}") from exc


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
        [str(x) for x in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.stdout:
        log(result.stdout[-4000:])
    if result.returncode:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


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
            "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr[-1500:]}")
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except Exception as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")
    fps_value = (
        (video or {}).get("avg_frame_rate")
        or (video or {}).get("r_frame_rate")
    )
    return {
        "duration": duration,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "fps": parse_fps(fps_value),
    }


def validate_fps(info, expected, label):
    actual = info.get("fps")
    if actual is None or abs(actual - float(expected)) > FPS_TOLERANCE:
        raise RuntimeError(f"FPS mismatch at {label}: {actual}, expected {expected}")


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
    if not isinstance(data, dict) or data.get("status") != "completed":
        status = data.get("status") if isinstance(data, dict) else None
        raise RuntimeError(f"{label} is not completed: {status}")


def normalize_role(row):
    value = str(
        row.get("role") or row.get("scene_type") or row.get("type") or "main"
    ).strip().lower()
    aliases = {
        "opening": "hook", "intro": "hook", "part_hook": "hook",
        "story": "main", "main_scene": "main",
        "cliffhanger": "suspense", "ending": "resolution", "final": "resolution",
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


def configured_part_numbers(config, manifest, count):
    """Resolve the actual selected range; never silently substitute its minimum."""
    candidates = (
        manifest.get("part_numbers"),
        config.get("SELECTED_PART_NUMBERS"),
        config.get("PART_NUMBERS"),
    )
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            parts = [positive_int(x, "part number") for x in candidate]
            if len(parts) != count or parts != sorted(set(parts)):
                raise RuntimeError("Selected part number list is inconsistent")
            if parts != list(range(parts[0], parts[0] + count)):
                raise RuntimeError("Selected part range must be continuous")
            return parts

    start = manifest.get("first_part", manifest.get("start_part"))
    if start is None:
        start = config.get("PARTS_START", config.get("PART_START", 1))
    start = positive_int(start, "first selected part")
    return list(range(start, start + count))


def expected_roles(parts):
    result = {}
    for index, part in enumerate(parts):
        roles = ["hook", "main"]
        roles.append("suspense" if index < len(parts)-1 else "resolution")
        result[part] = roles
    return result


def expected_main_keys(parts, scenes):
    return {(part, scene) for part in parts for scene in range(1, scenes+1)}


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

    by_id, main_map, role_counts = {}, {}, {role: {} for role in ROLE_ORDER}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("status") != "completed":
            raise RuntimeError(f"Photo Motion scene {index} is incomplete")
        sid = row_scene_id(row, f"Photo Motion row {index}")
        if sid in by_id:
            raise RuntimeError(f"Duplicate Photo Motion scene_id: {sid}")
        part = positive_int(row.get("part"), "Photo Motion part")
        if part not in parts:
            raise RuntimeError(f"{sid}: part outside selected range")
        role = normalize_role(row)
        global_scene = positive_int(
            row.get("global_scene", row.get("scene")),
            f"{sid} global_scene",
        )
        local_scene = (
            positive_int(row.get("local_scene", row.get("scene")), f"{sid} local_scene")
            if role == "main" else 0
        )
        video = media_path_from_row(row, ("video", "output", "path"), f"Photo Motion {sid}")
        require_file(video, f"Photo Motion {sid}")
        checksum = sha256_file(video)
        if row.get("video_sha256") != checksum:
            raise RuntimeError(f"Photo Motion checksum mismatch: {sid}")
        info = probe(video)
        if not info["has_video"]:
            raise RuntimeError(f"Photo Motion {sid} has no video")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Photo Motion {sid} dimensions mismatch")
        validate_fps(info, fps, sid)

        record = {
            "scene_id": sid, "part": part, "role": role,
            "local_scene": local_scene, "global_scene": global_scene,
            "video": video, "video_sha256": checksum,
            "duration": info["duration"], "width": width,
            "height": height, "fps": fps,
        }
        by_id[sid] = record
        role_counts[role][part] = role_counts[role].get(part, 0) + 1

        if role == "main":
            key = (part, local_scene)
            if key in main_map:
                raise RuntimeError(f"Duplicate Photo Motion main scene: {key}")
            main_map[key] = record

    expected = expected_main_keys(parts, scenes)
    if set(main_map) != expected:
        raise RuntimeError(
            "Photo Motion main scene mapping mismatch; "
            f"missing={sorted(expected-set(main_map))}; "
            f"extra={sorted(set(main_map)-expected)}"
        )

    role_plan = expected_roles(parts)
    for part, required in role_plan.items():
        for role in required:
            if role_counts[role].get(part, 0) != 1:
                raise RuntimeError(f"Part {part} requires exactly one {role}")
        for role in ROLE_ORDER:
            if role not in required and role_counts[role].get(part, 0):
                raise RuntimeError(f"Part {part} has forbidden {role} clip")

    ordered = sorted(
        by_id.values(),
        key=lambda x: (
            x["part"], ROLE_ORDER[x["role"]],
            x["local_scene"] if x["role"] == "main" else x["global_scene"],
            x["global_scene"],
        ),
    )
    global_numbers = [x["global_scene"] for x in ordered]
    if global_numbers != list(range(1, len(ordered)+1)):
        raise RuntimeError("Photo Motion global scene sequence is not continuous")
    return by_id, main_map, ordered, data


def load_audio_map(parts):
    data = read_json(AUDIO_MANIFEST, "Narration audio manifest")
    validate_completed(data, "Narration audio manifest")
    rows = data.get("jobs") or data.get("audio_jobs") or data.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Narration audio manifest has no jobs list")

    mapping = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration job {index}")
        sid = row_scene_id(row, f"Narration job {index}")
        if sid in mapping:
            raise RuntimeError(f"Duplicate narration scene_id: {sid}")
        if str(row.get("status", "")).lower() not in {"completed", "reused"}:
            raise RuntimeError(f"Narration job {sid} is incomplete")
        part = positive_int(row.get("part"), "Narration part")
        if part not in parts:
            raise RuntimeError(f"Narration {sid}: part outside selected range")
        role = normalize_role(row)
        audio = media_path_from_row(
            row, ("output", "audio_path", "audio_file", "path", "file", "audio"),
            f"Narration {sid}",
        )
        require_file(audio, f"Narration {sid}", MIN_AUDIO_BYTES)
        info = probe(audio)
        if not info["has_audio"]:
            raise RuntimeError(f"Narration {sid} has no audio stream")
        checksum = sha256_file(audio)
        declared = row.get("sha256") or row.get("audio_sha256")
        if declared and declared != checksum:
            raise RuntimeError(f"Narration checksum mismatch: {sid}")
        mapping[sid] = {
            "scene_id": sid, "part": part, "role": role,
            "audio": audio, "audio_sha256": checksum,
            "duration": info["duration"],
            "provider": row.get("provider"), "model": row.get("model"),
        }

    return mapping, data


def validate_photo_audio_pairing(photo_by_id, audio_by_id):
    photo_ids, audio_ids = set(photo_by_id), set(audio_by_id)
    if photo_ids != audio_ids:
        raise RuntimeError(
            "Photo Motion and narration scene IDs differ; "
            f"missing_audio={sorted(photo_ids-audio_ids)[:20]}; "
            f"missing_video={sorted(audio_ids-photo_ids)[:20]}"
        )
    for sid, photo in photo_by_id.items():
        audio = audio_by_id[sid]
        if photo["part"] != audio["part"] or photo["role"] != audio["role"]:
            raise RuntimeError(f"{sid}: narration/render role or part mismatch")


def load_sound_assets(parts, narration):
    manifest = read_json(SOUND_MANIFEST, "Sound design manifest")
    validate_completed(manifest, "Sound design manifest")
    validation = read_json(SFX_VALIDATION, "SFX validation report")
    validate_completed(validation, "SFX validation report")

    mode = str(manifest.get("format", "")).lower()
    if mode not in {"short", "full"} or str(validation.get("format", "")).lower() != mode:
        raise RuntimeError("Sound design/SFX validation FORMAT mismatch")
    manifest_parts = manifest.get("part_numbers")
    if isinstance(manifest_parts, list):
        if [int(x) for x in manifest_parts] != parts:
            raise RuntimeError("Sound design selected part range mismatch")
    elif int(manifest.get("parts", manifest.get("total_parts", -1))) != len(parts):
        raise RuntimeError("Sound design part count mismatch")

    tracks = manifest.get("tracks", {})
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound design tracks must be an object")

    assets = {"music": None, "ambient": None, "sfx": {}, "warnings": []}
    hashes = {}
    for category in ("music", "ambient"):
        record = tracks.get(category)
        if record is None:
            continue
        if not isinstance(record, dict) or record.get("status") != "completed":
            raise RuntimeError(f"{category} track is not completed")
        path = safe_path(record.get("path") or record.get("audio_path"), f"{category} track")
        require_file(path, f"{category} track", MIN_AUDIO_BYTES)
        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(f"{category} track has no audio stream")
        checksum = sha256_file(path)
        if record.get("sha256") and record["sha256"] != checksum:
            raise RuntimeError(f"{category} track checksum mismatch")
        assets[category] = {"path": path, "sha256": checksum, "duration": info["duration"]}
        hashes[category] = checksum

    sfx_data = validation.get("sfx", {})
    cues = sfx_data.get("cues", []) if isinstance(sfx_data, dict) else []
    if not isinstance(cues, list):
        raise RuntimeError("SFX validation cues must be a list")

    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")
        sid = str(cue.get("scene_id") or "").strip()
        if not sid:
            part = cue.get("part")
            scene = cue.get("scene")
            try:
                part, scene = int(part), int(scene)
            except (TypeError, ValueError):
                raise RuntimeError(f"SFX cue {index} requires scene_id for extra roles")
            candidates = [
                key for key, row in narration.items()
                if row["part"] == part and row.get("local_scene") == scene
            ]
            if len(candidates) != 1:
                raise RuntimeError(f"SFX cue {index} scene mapping is ambiguous")
            sid = candidates[0]
        if sid not in narration:
            raise RuntimeError(f"SFX cue references unknown scene: {sid}")

        start = finite_number(
            cue.get("start_seconds", cue.get("time", cue.get("start"))),
            f"SFX cue {index} start", minimum=0,
        )
        gain = finite_number(cue.get("gain", 0.5), f"SFX cue {index} gain", 0, 1)
        scene_duration = narration[sid]["duration"]
        if start >= scene_duration:
            raise RuntimeError(f"SFX cue outside narration duration: {sid}")

        path = media_path_from_row(cue, ("file", "path", "audio_path"), f"SFX cue {index}")
        require_file(path, f"SFX cue {index}", MIN_AUDIO_BYTES)
        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(f"SFX cue {index} has no audio")
        checksum = sha256_file(path)
        if cue.get("sha256") and cue["sha256"] != checksum:
            raise RuntimeError(f"SFX cue checksum mismatch: {index}")

        latest_start = max(0.0, scene_duration-info["duration"]-SFX_END_MARGIN)
        actual_start = min(start, latest_start)
        usable = min(info["duration"], scene_duration-actual_start)
        if usable <= 0:
            assets["warnings"].append({"scene_id": sid, "warning": "SFX does not fit"})
            continue
        if actual_start < start:
            assets["warnings"].append({
                "scene_id": sid, "declared_start": start,
                "actual_start": actual_start,
                "warning": "SFX shifted earlier to preserve its tail",
            })
        assets["sfx"].setdefault(sid, []).append({
            "path": path, "sha256": checksum, "start": actual_start,
            "duration": usable,
            "gain": min(1.0, gain*SFX_GAIN_MULTIPLIER),
            "kind": str(cue.get("kind", "effect")),
        })
        hashes[f"sfx_{index}"] = checksum

    for sid in assets["sfx"]:
        assets["sfx"][sid].sort(key=lambda x: x["start"])

    assets["fingerprint"] = digest_data({
        "sound_manifest": sha256_file(SOUND_MANIFEST),
        "sfx_validation": sha256_file(SFX_VALIDATION),
        "track_hashes": hashes,
        "gains": {
            "narration": NARRATION_GAIN, "music": MUSIC_GAIN,
            "ambient": AMBIENT_GAIN, "sfx": SFX_GAIN_MULTIPLIER,
            "limiter": MASTER_LIMIT,
        },
    })
    return assets, manifest


def scene_fingerprint(photo, audio, sound_assets, mode, width, height, fps):
    return digest_data({
        "version": VERSION, "format": mode, "width": width, "height": height,
        "fps": fps, "scene_id": photo["scene_id"], "role": photo["role"],
        "part": photo["part"], "photo_sha256": photo["video_sha256"],
        "audio_sha256": audio["audio_sha256"],
        "audio_duration": round(audio["duration"], 5),
        "sound_fingerprint": sound_assets["fingerprint"],
    })


def previous_manifest(mode):
    data = read_json(RENDER_MANIFEST, "previous render manifest", False, {})
    if not isinstance(data, dict) or str(data.get("format", "")).lower() != mode:
        return {}
    rows = data.get("all_completed_scenes") or data.get("completed_scenes") or data.get("scenes") or []
    result = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("scene_id"):
                result[row["scene_id"]] = row
    return result


def reusable_scene(output, previous, signature, width, height, fps, duration):
    if not isinstance(previous, dict) or previous.get("status") != "completed":
        return None
    if previous.get("fingerprint") != signature:
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
        if abs(info["duration"]-duration) > DURATION_TOLERANCE:
            return None
        return info, checksum
    except Exception:
        return None


def build_audio_filter(photo, audio, sound_assets, timeline_offset):
    command_inputs = [
        "-stream_loop", "-1", "-i", str(photo["video"]),
        "-i", str(audio["audio"]),
    ]
    filters = [
        f"[0:v]scale={CURRENT_WIDTH}:{CURRENT_HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={CURRENT_WIDTH}:{CURRENT_HEIGHT},setsar=1,"
        f"fps={CURRENT_FPS},format=yuv420p[v]",
        f"[1:a]aresample={AUDIO_RATE},volume={NARRATION_GAIN:.3f},"
        "asetpts=PTS-STARTPTS[narr]",
    ]
    mix_labels = ["[narr]"]
    input_index = 2

    for category, gain in (("music", MUSIC_GAIN), ("ambient", AMBIENT_GAIN)):
        track = sound_assets.get(category)
        if not track:
            continue
        offset = timeline_offset % track["duration"]
        command_inputs.extend([
            "-stream_loop", "-1", "-ss", f"{offset:.4f}", "-i", str(track["path"])
        ])
        label = category
        filters.append(
            f"[{input_index}:a]aresample={AUDIO_RATE},"
            f"volume={gain:.3f},atrim=duration={audio['duration']:.4f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        mix_labels.append(f"[{label}]")
        input_index += 1

    for cue_index, cue in enumerate(sound_assets["sfx"].get(photo["scene_id"], [])):
        duration = min(cue["duration"], audio["duration"]-cue["start"])
        if duration <= 0:
            continue
        command_inputs.extend(["-i", str(cue["path"])])
        label = f"sfx{cue_index}"
        delay = max(0, int(round(cue["start"]*1000)))
        fade = min(0.4, max(0.05, duration*0.2))
        filters.append(
            f"[{input_index}:a]aresample={AUDIO_RATE},"
            f"atrim=duration={duration:.4f},asetpts=PTS-STARTPTS,"
            f"volume={cue['gain']:.3f},"
            f"afade=t=out:st={max(0,duration-fade):.4f}:d={fade:.4f},"
            f"adelay={delay}|{delay}[{label}]"
        )
        mix_labels.append(f"[{label}]")
        input_index += 1

    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:duration=first:"
          "dropout_transition=0:normalize=0,"
        + f"alimiter=limit={MASTER_LIMIT:.3f}:level=disabled,"
        + f"aresample={AUDIO_RATE}[aout]"
    )
    return command_inputs, filters


def render_scene(photo, audio, sound_assets, output, signature, old_row,
                 width, height, fps, timeline_offset, resume, skip_completed):
    output.parent.mkdir(parents=True, exist_ok=True)
    if resume and skip_completed:
        cached = reusable_scene(
            output, old_row, signature, width, height, fps, audio["duration"]
        )
        if cached:
            log("REUSED:", photo["scene_id"])
            return cached[0], cached[1], True

    temporary = output.with_name(output.stem+".rendering.mp4")
    temporary.unlink(missing_ok=True)
    inputs, filters = build_audio_filter(photo, audio, sound_assets, timeline_offset)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs, "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[aout]",
        "-t", f"{audio['duration']:.4f}",
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart", str(temporary),
    ]
    try:
        run(command, f"Render {photo['scene_id']}")
        require_file(temporary, "Rendered scene")
        info = probe(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"{photo['scene_id']}: output lacks audio/video")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"{photo['scene_id']}: output dimensions mismatch")
        validate_fps(info, fps, photo["scene_id"])
        if abs(info["duration"]-audio["duration"]) > DURATION_TOLERANCE:
            raise RuntimeError(f"{photo['scene_id']}: measured duration mismatch")
        os.replace(temporary, output)
        return probe(output), sha256_file(output), False
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


CURRENT_WIDTH = 0
CURRENT_HEIGHT = 0
CURRENT_FPS = 0
CURRENT_ORDERED_PHOTOS = []
CURRENT_SOUND_ASSETS = {}
CURRENT_TIMELINE_OFFSETS = {}


def make_render_row(photo, audio, output, info, checksum, signature, reused):
    return {
        "scene_id": photo["scene_id"],
        "role": photo["role"],
        "scene_type": photo["role"],
        "part": photo["part"],
        "scene": photo["local_scene"] if photo["role"] == "main" else None,
        "local_scene": photo["local_scene"] if photo["role"] == "main" else None,
        "global_scene": photo["global_scene"],
        "photo_motion_global_scene": photo["global_scene"],
        "status": "completed",
        "source_photo_motion_video": photo["video"].relative_to(ROOT).as_posix(),
        "source_audio": audio["audio"].relative_to(ROOT).as_posix(),
        "source_photo_sha256": photo["video_sha256"],
        "source_audio_sha256": audio["audio_sha256"],
        "source_rendered_video": output.relative_to(ROOT).as_posix(),
        "video": output.relative_to(ROOT).as_posix(),
        "output": output.relative_to(ROOT).as_posix(),
        "duration": round(info["duration"], 5),
        "width": CURRENT_WIDTH, "height": CURRENT_HEIGHT, "fps": CURRENT_FPS,
        "fingerprint": signature, "sha256": checksum,
        "captions_burned": False, "cache_reused": reused,
        "audio_mix": {
            "narration": True,
            "music": bool(CURRENT_SOUND_ASSETS["music"]),
            "ambient": bool(CURRENT_SOUND_ASSETS["ambient"]),
            "sfx_count": len(CURRENT_SOUND_ASSETS["sfx"].get(photo["scene_id"], [])),
            "global_timeline_offset": round(CURRENT_TIMELINE_OFFSETS[photo["scene_id"]], 5),
        },
    }


def concat_videos(paths, destination, width, height, fps):
    if not paths:
        raise RuntimeError(f"No input clips for {destination.name}")
    for path in paths:
        require_file(path, f"Concat input {path.name}")
        info = probe(path)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Concat input lacks audio/video: {path}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Concat dimensions mismatch: {path}")
        validate_fps(info, fps, path.name)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem+".joining.mp4")
    list_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="render_concat_",
            encoding="utf-8", delete=False
        ) as stream:
            list_file = Path(stream.name)
            for path in paths:
                resolved = str(Path(path).resolve())
                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in concat path")
                stream.write("file '"+resolved.replace("'", "'\\''")+"'\n")
        temporary.unlink(missing_ok=True)
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-r", str(fps), "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-ar", str(AUDIO_RATE), "-movflags", "+faststart",
            str(temporary),
        ], f"Join {destination.name}", timeout=1800)
        require_file(temporary, "Joined part video", 10000)
        info = probe(temporary)
        if not info["has_video"] or not info["has_audio"]:
            raise RuntimeError(f"Joined video lacks audio/video: {destination.name}")
        if (info["width"], info["height"]) != (width, height):
            raise RuntimeError(f"Joined video dimensions mismatch: {destination.name}")
        validate_fps(info, fps, destination.name)
        os.replace(temporary, destination)
        return probe(destination)
    finally:
        if list_file:
            list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def verify_rendered_scene(row, width, height, fps):
    path = safe_path(row.get("video"), f"Rendered scene {row.get('scene_id')}")
    require_file(path, "Rendered scene")
    checksum = sha256_file(path)
    if checksum != row.get("sha256"):
        raise RuntimeError(f"Rendered scene checksum mismatch: {row.get('scene_id')}")
    info = probe(path)
    if not info["has_video"] or not info["has_audio"]:
        raise RuntimeError(f"Rendered scene lacks audio/video: {row.get('scene_id')}")
    if (info["width"], info["height"]) != (width, height):
        raise RuntimeError(f"Rendered scene dimensions mismatch: {row.get('scene_id')}")
    validate_fps(info, fps, str(row.get("scene_id")))
    if abs(info["duration"]-float(row.get("duration", 0))) > DURATION_TOLERANCE:
        raise RuntimeError(f"Rendered scene duration mismatch: {row.get('scene_id')}")
    return info


def build_manifest(mode, fps, width, height, parts, scenes, part_numbers,
                   completed, part_rows, photo_hash, audio_hash,
                   sound_fingerprint, status, error=None):
    all_rows = [
        completed[item["scene_id"]]
        for item in CURRENT_ORDERED_PHOTOS
        if item["scene_id"] in completed
    ]
    main_rows = [row for row in all_rows if row["role"] == "main"]
    extra_rows = [row for row in all_rows if row["role"] != "main"]
    result = {
        "status": status, "version": VERSION, "format": mode,
        "fps": fps, "width": width, "height": height,
        "total_parts": parts, "parts_count": parts,
        "part_numbers": part_numbers,
        "first_part": part_numbers[0],
        "last_part": part_numbers[-1],
        "parts": sorted(part_rows, key=lambda x: int(x["part"])),
        "scenes_per_part": scenes,
        "expected_scene_count": parts*scenes,
        "completed_count": len(main_rows),
        "all_completed_scene_count": len(all_rows),
        "extra_scene_count": len(extra_rows),
        "captions_burned": False,
        "source_photo_manifest": PHOTO_MANIFEST.relative_to(ROOT).as_posix(),
        "source_audio_manifest": AUDIO_MANIFEST.relative_to(ROOT).as_posix(),
        "source_sound_manifest": SOUND_MANIFEST.relative_to(ROOT).as_posix(),
        "source_sfx_validation": SFX_VALIDATION.relative_to(ROOT).as_posix(),
        "source_photo_manifest_sha256": photo_hash,
        "source_audio_manifest_sha256": audio_hash,
        "source_sound_fingerprint": sound_fingerprint,
        "completed_scenes": main_rows,
        "scenes": main_rows,
        "extra_scenes": extra_rows,
        "all_completed_scenes": all_rows,
        "part_outputs_only": True,
        "audio_mix": {
            "narration_gain": NARRATION_GAIN, "music_gain": MUSIC_GAIN,
            "ambient_gain": AMBIENT_GAIN,
            "sfx_gain_multiplier": SFX_GAIN_MULTIPLIER,
            "master_limiter": MASTER_LIMIT, "sample_rate": AUDIO_RATE,
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
            "scene_id": item["scene_id"], "part": item["part"],
            "role": item["role"], "global_scene": item["global_scene"],
            "status": record.get("status", "pending") if isinstance(record, dict) else "pending",
            "video": record.get("video") if isinstance(record, dict) else None,
        })
    result = {
        "version": VERSION, "stage": "render_video", "status": status,
        "total_scenes": len(ordered),
        "completed_scenes": sum(x["status"] == "completed" for x in rows),
        "jobs": rows,
    }
    if error:
        result["last_error"] = str(error)[:2000]
    return result


def main():
    global CURRENT_WIDTH, CURRENT_HEIGHT, CURRENT_FPS
    global CURRENT_ORDERED_PHOTOS, CURRENT_SOUND_ASSETS, CURRENT_TIMELINE_OFFSETS

    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command not installed: {command}")

    config = load_and_validate(ROOT/"Input"/"topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts_count = positive_int(get_parts(config), "PARTS")
    scenes = positive_int(get_scenes(config), "SCENES")
    fps = positive_int(get_fps(config), "FPS")
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if fps > 60:
        raise RuntimeError("FPS cannot exceed 60")

    width, height = (720, 1280) if mode == "short" else (1280, 720)
    CURRENT_WIDTH, CURRENT_HEIGHT, CURRENT_FPS = width, height, fps

    photo_data = read_json(PHOTO_MANIFEST, "Photo Motion manifest")
    part_numbers = configured_part_numbers(config, photo_data, parts_count)
    expected_main_count = parts_count*scenes

    photo_by_id, _, ordered_photos, photo_data = load_photo_map(
        part_numbers, scenes, mode, width, height, fps
    )
    audio_by_id, audio_data = load_audio_map(part_numbers)
    validate_photo_audio_pairing(photo_by_id, audio_by_id)
    sound_assets, sound_data = load_sound_assets(part_numbers, audio_by_id)
    if str(sound_data.get("format", "")).lower() != mode:
        raise RuntimeError("Sound design FORMAT does not match Input")

    photo_hash = sha256_file(PHOTO_MANIFEST)
    audio_hash = sha256_file(AUDIO_MANIFEST)
    resume = config_bool(config, "RESUME_ENABLED", True)
    skip_completed = config_bool(config, "SKIP_COMPLETED_SCENES", True)
    previous = previous_manifest(mode)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    timeline_offsets, total_duration = {}, 0.0
    for photo in ordered_photos:
        sid = photo["scene_id"]
        timeline_offsets[sid] = total_duration
        total_duration += audio_by_id[sid]["duration"]

    CURRENT_SOUND_ASSETS = sound_assets
    CURRENT_TIMELINE_OFFSETS = timeline_offsets
    CURRENT_ORDERED_PHOTOS = ordered_photos

    completed, part_rows = {}, []
    new_renders = reused_renders = 0

    def save_checkpoint(status="in_progress", error=None):
        write_json(
            RENDER_MANIFEST,
            build_manifest(
                mode, fps, width, height, parts_count, scenes, part_numbers,
                completed, part_rows, photo_hash, audio_hash,
                sound_assets["fingerprint"], status, error,
            ),
        )
        write_json(
            CHECKPOINT_FILE,
            build_progress(completed, ordered_photos, status, error),
        )

    log("RENDER VERSION:", VERSION)
    log("FORMAT:", mode, "PARTS:", part_numbers)
    log("RESOLUTION:", f"{width}x{height}", "FPS:", fps)
    log("MAIN SCENES:", expected_main_count)
    log("ALL ROLE CLIPS:", len(ordered_photos))
    log("MEASURED NARRATION SECONDS:", round(total_duration, 3))
    log("MUSIC:", bool(sound_assets["music"]), "AMBIENT:", bool(sound_assets["ambient"]))
    log("SFX CUES:", sum(len(x) for x in sound_assets["sfx"].values()))

    save_checkpoint()

    try:
        for part in part_numbers:
            part_photos = [x for x in ordered_photos if x["part"] == part]
            if not part_photos:
                raise RuntimeError(f"Part {part} has no clips")
            scene_paths = []

            for item in part_photos:
                sid = item["scene_id"]
                audio = audio_by_id[sid]
                signature = scene_fingerprint(
                    item, audio, sound_assets, mode, width, height, fps
                )
                safe_id = "".join(
                    ch if ch.isalnum() or ch in "-_" else "_"
                    for ch in sid
                ).strip("_")
                if not safe_id:
                    raise RuntimeError(f"Unsafe scene ID: {sid}")

                output = RENDER_DIR/f"part_{part:02d}"/f"{safe_id}.mp4"
                info, checksum, reused = render_scene(
                    item, audio, sound_assets, output, signature,
                    previous.get(sid), width, height, fps,
                    timeline_offsets[sid], resume, skip_completed,
                )
                new_renders += int(not reused)
                reused_renders += int(reused)
                row = make_render_row(
                    item, audio, output, info, checksum, signature, reused
                )
                completed[sid] = row
                scene_paths.append(output)
                save_checkpoint()
                log(
                    f"CHECKPOINT {len(completed)}/{len(ordered_photos)} "
                    f"part={part} role={item['role']} scene_id={sid}"
                )

            part_output = PART_DIR/f"part_{part:02d}.mp4"
            part_info = concat_videos(scene_paths, part_output, width, height, fps)
            part_rows = [x for x in part_rows if int(x["part"]) != part]
            part_rows.append({
                "part": part, "status": "completed",
                "scene_count": sum(x["role"] == "main" for x in part_photos),
                "total_clip_count": len(part_photos),
                "roles": [x["role"] for x in part_photos],
                "path": part_output.relative_to(ROOT).as_posix(),
                "output": part_output.relative_to(ROOT).as_posix(),
                "duration": round(part_info["duration"], 5),
                "sha256": sha256_file(part_output),
                "fps": fps, "width": width, "height": height,
            })
            part_rows.sort(key=lambda x: int(x["part"]))
            save_checkpoint()
            log("PART COMPLETE:", part_output.relative_to(ROOT))

        if len(completed) != len(ordered_photos):
            raise RuntimeError(
                f"Rendered {len(completed)}/{len(ordered_photos)} scene clips"
            )

        for item in ordered_photos:
            row = completed.get(item["scene_id"])
            if not isinstance(row, dict) or row.get("status") != "completed":
                raise RuntimeError(f"Render manifest row missing: {item['scene_id']}")
            verify_rendered_scene(row, width, height, fps)

        if len(part_rows) != parts_count:
            raise RuntimeError(f"Expected {parts_count} part videos, found {len(part_rows)}")

        for index, part in enumerate(part_numbers):
            row = part_rows[index]
            if int(row["part"]) != part:
                raise RuntimeError("Part output order mismatch")
            path = safe_path(row["path"], f"Part {part} video")
            if sha256_file(path) != row["sha256"]:
                raise RuntimeError(f"Part {part} checksum mismatch")
            info = probe(path)
            if (info["width"], info["height"]) != (width, height):
                raise RuntimeError(f"Part {part} resolution mismatch")
            validate_fps(info, fps, f"part {part}")
            if not info["has_video"] or not info["has_audio"]:
                raise RuntimeError(f"Part {part} lacks video/audio")

        main_rows = [completed[x["scene_id"]] for x in ordered_photos if x["role"] == "main"]
        extra_rows = [completed[x["scene_id"]] for x in ordered_photos if x["role"] != "main"]
        if len(main_rows) != expected_main_count:
            raise RuntimeError("Main scene render count mismatch")
        if len(extra_rows) != parts_count*2:
            raise RuntimeError("Hook/Suspense/Resolution render count mismatch")

        for part in part_numbers:
            roles = [x["role"] for x in ordered_photos if x["part"] == part]
            expected = expected_roles(part_numbers)[part]
            if roles.count("hook") != 1:
                raise RuntimeError(f"Part {part}: Hook count mismatch")
            if part != part_numbers[-1] and roles.count("suspense") != 1:
                raise RuntimeError(f"Part {part}: Suspense count mismatch")
            if part == part_numbers[-1] and roles.count("resolution") != 1:
                raise RuntimeError(f"Final part {part}: Resolution count mismatch")

        # This stage creates Part videos only; no combined or alias video.
        stale_combined = [
            OUT/"videos"/"pre_caption_render.mp4",
            OUT/"videos"/"katha_lok_ai_final.mp4",
            OUT/"videos"/"katha_lok_ai_short.mp4",
            OUT/"videos"/"katha_lok_ai_full.mp4",
        ]
        for path in stale_combined:
            path.unlink(missing_ok=True)

        final_manifest = build_manifest(
            mode, fps, width, height, parts_count, scenes, part_numbers,
            completed, part_rows, photo_hash, audio_hash,
            sound_assets["fingerprint"], "completed",
        )
        final_manifest.update({
            "captions_burned": False,
            "part_outputs_only": True,
            "rendered_new": new_renders,
            "rendered_reused": reused_renders,
            "main_scene_count": len(main_rows),
            "extra_scene_count": len(extra_rows),
            "hook_count": sum(x["role"] == "hook" for x in extra_rows),
            "suspense_count": sum(x["role"] == "suspense" for x in extra_rows),
            "resolution_count": sum(x["role"] == "resolution" for x in extra_rows),
            "narration_duration_seconds": round(total_duration, 5),
            "audio_mix_summary": {
                "music_included": bool(sound_assets["music"]),
                "ambient_included": bool(sound_assets["ambient"]),
                "sfx_cues_included": sum(len(x) for x in sound_assets["sfx"].values()),
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
            build_progress(completed, ordered_photos, "completed"),
        )

    except Exception as exc:
        save_checkpoint("failed", exc)
        log("RENDER FAILED:", str(exc))
        raise

    log("="*60)
    log("RENDER AND AUDIO MIX COMPLETED")
    log("FORMAT:", mode, "PARTS:", part_numbers)
    log("MAIN SCENES:", expected_main_count)
    log("ALL ROLE CLIPS:", len(ordered_photos))
    log("NEW RENDERS:", new_renders, "REUSED:", reused_renders)
    log("PART OUTPUTS:", len(part_rows))
    log("MANIFEST:", RENDER_MANIFEST.relative_to(ROOT))
    log("CHECKPOINT:", CHECKPOINT_FILE.relative_to(ROOT))
    log("="*60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("RENDER INTERRUPTED; completed scene checkpoints retained.")
        sys.exit(130)
    except Exception as exc:
        log("RENDER FAILED:", str(exc))
        sys.exit(1)