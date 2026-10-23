#!/usr/bin/env python3
"""Resume-safe photo-motion renderer with configurable narration-first audio mix."""

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
PHOTO_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO_MANIFEST = OUTPUT / "narration" / "audio_jobs.json"
SOUND_MANIFEST = OUTPUT / "sound_design" / "sound_design_manifest.json"
SFX_MANIFEST = OUTPUT / "sound_design" / "sfx_cues.json"
RENDER_DIR = OUTPUT / "scene_renders"
PART_DIR = OUTPUT / "parts"
VIDEO_DIR = OUTPUT / "videos"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"

VERSION = "render-video-v21-configurable-narration-first-mix"
RESOLUTION = (1280, 720)
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.0

AUDIO_PATH_KEYS = ("output", "audio_path", "path", "file", "audio")
SFX_TIME_KEYS = ("time", "time_seconds", "timestamp", "start_seconds", "start")


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path}")
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def safe_path(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path is outside the repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file is too small: {path}")
    return path


def rows_from(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def first_path(row, keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def probe(path, audio_only=False):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    entries = (
        "format=duration:stream=codec_type"
        if audio_only
        else "format=duration:stream=codec_type,width,height"
    )
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries", entries,
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr[-800:]}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path}")

    streams = data.get("streams", [])
    has_audio = any(
        isinstance(row, dict) and row.get("codec_type") == "audio"
        for row in streams
    )
    if audio_only:
        if not has_audio:
            raise RuntimeError(f"No audio stream: {path}")
        return {"duration": duration}

    video = next(
        (row for row in streams
         if isinstance(row, dict) and row.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise RuntimeError(f"No video stream: {path}")

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid video dimensions: {path}")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": has_audio,
    }


def enabled(config, key, default=True):
    aliases = {
        "music": ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC"),
        "ambient": (
            "AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
            "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED",
        ),
        "sfx": ("SFX_ENABLED", "SFX", "SOUND_EFFECTS", "SOUND_EFFECTS_ENABLED"),
    }
    for name in aliases[key]:
        if name not in config:
            continue
        value = str(config[name]).strip().lower()
        if value in {"1", "true", "yes", "on", "enabled"}:
            return True
        if value in {"0", "false", "no", "off", "disabled"}:
            return False
        raise RuntimeError(f"{name} must be true or false")
    return default


def mix_gain(config, key, default):
    """Read a linear volume gain; never silently accept invalid or unsafe values."""
    aliases = {
        "music": ("MUSIC_GAIN", "BACKGROUND_MUSIC_GAIN"),
        "ambient": ("AMBIENT_GAIN", "AMBIENT_SOUND_GAIN"),
        "sfx": ("SFX_GAIN", "SOUND_EFFECTS_GAIN"),
    }
    raw = next((config[k] for k in aliases[key] if k in config), default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{aliases[key][0]} must be between 0 and 1") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise RuntimeError(f"{aliases[key][0]} must be between 0 and 1")
    return value


def expected_keys(parts, scenes_per_part):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }


def scene_number(row, primary, fallback=None):
    value = row.get(primary)
    if value is None and fallback:
        value = row.get(fallback)
    if value is None:
        raise ValueError(f"Missing {primary}")
    return int(value)


def validate_photo_scenes(data, parts, scenes_per_part):
    expected = expected_keys(parts, scenes_per_part)
    rows = rows_from(data, "scenes", "videos", "completed_scenes", "items")
    if len(rows) != len(expected):
        raise RuntimeError(
            f"Expected {len(expected)} photo-motion scenes; found {len(rows)}"
        )

    mapping = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid photo-motion row {index}")
        try:
            part = scene_number(row, "part")
            scene = scene_number(row, "local_scene", "scene")
            global_scene = int(row.get("global_scene", index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid photo-motion mapping row {index}: {exc}") from exc

        expected_part = (index - 1) // scenes_per_part + 1
        expected_scene = (index - 1) % scenes_per_part + 1
        if (part, scene, global_scene) != (expected_part, expected_scene, index):
            raise RuntimeError(f"Photo-motion scene order mismatch at row {index}")

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"Duplicate photo-motion scene: {key}")

        source = safe_path(
            first_path(row, (
                "source_video", "video", "output", "rendered_video", "path", "file"
            )),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )
        info = probe(source)
        mapping[key] = {
            "part": part,
            "scene": scene,
            "global_scene": global_scene,
            "path": source,
            "duration": info["duration"],
            "sha256": digest_file(source),
        }

    if set(mapping) != expected:
        raise RuntimeError("Photo-motion manifest scene mapping is incomplete")
    return mapping


def load_audio_map(parts, scenes_per_part):
    if not AUDIO_MANIFEST.is_file():
        raise RuntimeError(f"Narration manifest missing: {AUDIO_MANIFEST}")

    data = read_json(AUDIO_MANIFEST)
    jobs = rows_from(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        raise RuntimeError("No narration jobs found")

    mapping = {}
    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}")
        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene")))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid narration mapping row {index}") from exc

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration is not completed for {key}")

        audio = safe_path(
            first_path(row, AUDIO_PATH_KEYS),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )
        info = probe(audio, audio_only=True)
        mapping[key] = {
            "part": part,
            "scene": scene,
            "path": audio,
            "duration": info["duration"],
            "sha256": digest_file(audio),
        }

    if set(mapping) != expected_keys(parts, scenes_per_part):
        raise RuntimeError("Narration manifest scene mapping does not match input")
    return mapping


def load_backgrounds(config):
    data = read_json(SOUND_MANIFEST) if SOUND_MANIFEST.is_file() else {}
    tracks = data.get("tracks", {}) if isinstance(data, dict) else {}
    selected = {}

    for category in ("music", "ambient"):
        if not enabled(config, category):
            log("DISABLED:", category)
            continue

        record = tracks.get(category) if isinstance(tracks, dict) else None
        path_value = (
            record.get("path") or record.get("file") or record.get("output")
            if isinstance(record, dict)
            and record.get("status") == "completed"
            else None
        )

        if not path_value:
            log("NOT FOUND:", category, "track; rendering narration without it")
            continue

        path = safe_path(path_value, f"{category} track", MIN_AUDIO_BYTES)
        probe(path, audio_only=True)
        selected[category] = path

    return selected


def load_sfx(part, scene, duration, config, sfx_gain):
    if not enabled(config, "sfx") or not SFX_MANIFEST.is_file():
        return []

    data = read_json(SFX_MANIFEST)
    if not isinstance(data, dict) or data.get("status") not in (None, "completed"):
        raise RuntimeError("SFX cue manifest is invalid or incomplete")

    result = []
    for index, row in enumerate(rows_from(data, "cues", "items"), 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid SFX cue row {index}")
        try:
            cue_part, cue_scene = int(row["part"]), int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX mapping row {index}") from exc
        if (cue_part, cue_scene) != (part, scene):
            continue

        try:
            cue_time = float(next(row[k] for k in SFX_TIME_KEYS if k in row))
            cue_gain = float(row.get("gain", 0.12))
        except (StopIteration, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX time/gain in cue {index}") from exc

        if not math.isfinite(cue_time) or cue_time < 0 or cue_time >= duration:
            raise RuntimeError(f"SFX cue {index} is outside narration duration")
        if not math.isfinite(cue_gain) or not 0 <= cue_gain <= 1:
            raise RuntimeError(f"SFX cue {index} has invalid gain")

        path = safe_path(
            first_path(row, ("file", "path", "audio", "output")),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )
        info = probe(path, audio_only=True)
        result.append({
            "path": path,
            "time": cue_time,
            "gain": min(cue_gain * sfx_gain, 0.50),
            "source_gain": cue_gain,
            "kind": str(row.get("kind", row.get("type", "unspecified"))),
            "duration": info["duration"],
            "sha256": digest_file(path),
        })

    result.sort(key=lambda item: (item["time"], item["kind"]))
    return result


def render_scene(source, narration, backgrounds, cues, destination,
                 fps, duration, gains):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    signature_payload = {
        "version": VERSION,
        "source_sha256": source["sha256"],
        "narration_sha256": narration["sha256"],
        "backgrounds": [
            {
                "category": category,
                "path": relative(path),
                "sha256": digest_file(path),
                "gain": gains[category],
            }
            for category, path in sorted(backgrounds.items())
        ],
        "sfx": [
            {
                "path": relative(cue["path"]),
                "sha256": cue["sha256"],
                "time": cue["time"],
                "gain": cue["gain"],
                "kind": cue["kind"],
            }
            for cue in cues
        ],
        "fps": fps,
        "resolution": list(RESOLUTION),
        "duration": round(duration, 3),
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()

    sidecar = destination.with_suffix(".render.json")
    if destination.is_file() and sidecar.is_file():
        try:
            cached = read_json(sidecar)
            info = probe(destination)
            if (
                isinstance(cached, dict)
                and cached.get("signature") == signature
                and info["has_audio"]
                and (info["width"], info["height"]) == RESOLUTION
                and abs(info["duration"] - duration) <= DURATION_TOLERANCE
            ):
                log("RESUME: reused", destination.name)
                return info, signature, cues
        except Exception as exc:
            log("CACHE INVALID:", destination.name, str(exc))

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    inputs = ["-i", str(source["path"]), "-i", str(narration["path"])]
    ordered_backgrounds = sorted(backgrounds.items())

    for _, path in ordered_backgrounds:
        inputs.extend(["-stream_loop", "-1", "-i", str(path)])
    for cue in cues:
        inputs.extend(["-i", str(cue["path"])])

    width, height = RESOLUTION
    filters = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[v]",
        "[1:a]aresample=44100,"
        f"apad,atrim=duration={duration:.3f},"
        "asetpts=PTS-STARTPTS,volume=1.0[narr]",
    ]

    bg_labels = []
    input_index = 2
    for category, _ in ordered_backgrounds:
        label = f"bg{input_index}"
        filters.append(
            f"[{input_index}:a]aresample=44100,"
            f"volume={gains[category]:.4f},"
            f"apad,atrim=duration={duration:.3f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        bg_labels.append(f"[{label}]")
        input_index += 1

    mix_labels = ["[narr]"]
    if bg_labels:
        filters.append(
            "".join(bg_labels)
            + f"amix=inputs={len(bg_labels)}:duration=longest:"
            "dropout_transition=1:normalize=0[bgmix]"
        )
        # Duck music/ambient under narration; voice remains the priority.
        filters.append(
            "[bgmix][narr]sidechaincompress="
            "threshold=0.025:ratio=8:attack=15:release=350:"
            "makeup=1[duckedbg]"
        )
        mix_labels.append("[duckedbg]")

    cue_details = []
    for cue in cues:
        label = f"sfx{input_index}"
        delay = int(round(cue["time"] * 1000))
        filters.append(
            f"[{input_index}:a]aresample=44100,"
            f"volume={cue['gain']:.4f},"
            f"adelay=delays={delay}:all=1,"
            f"atrim=duration={duration:.3f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        mix_labels.append(f"[{label}]")
        cue_details.append({
            "path": relative(cue["path"]),
            "time": cue["time"],
            "gain": cue["gain"],
            "kind": cue["kind"],
            "sha256": cue["sha256"],
        })
        input_index += 1

    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:duration=longest:"
        "dropout_transition=1:normalize=0,"
        "alimiter=limit=0.90:level=false,"
        f"atrim=duration={duration:.3f},"
        "asetpts=PTS-STARTPTS[a]"
    )

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *inputs, "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(temporary),
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=1200, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Render failed for {destination.name}: {result.stderr[-1600:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_VIDEO_BYTES:
            raise RuntimeError(f"Rendered file is empty: {destination.name}")

        info = probe(temporary)
        if not info["has_audio"]:
            raise RuntimeError("Rendered scene has no audio")
        if (info["width"], info["height"]) != RESOLUTION:
            raise RuntimeError("Rendered scene resolution is not 1280x720")
        if abs(info["duration"] - duration) > DURATION_TOLERANCE:
            raise RuntimeError("Rendered scene duration differs from narration")

        os.replace(temporary, destination)
        write_json(sidecar, {
            "status": "completed",
            "signature": signature,
            "source": relative(source["path"]),
            "narration": relative(narration["path"]),
            "background_tracks": [
                {
                    "category": category,
                    "path": relative(path),
                    "gain": gains[category],
                }
                for category, path in ordered_backgrounds
            ],
            "sfx_cues": cue_details,
            "video": relative(destination),
            "duration": info["duration"],
            "fps": fps,
            "resolution": list(RESOLUTION),
            "renderer": VERSION,
        })
        return info, signature, cues
    finally:
        temporary.unlink(missing_ok=True)


def concat_videos(videos, destination):
    if not videos:
        raise RuntimeError(f"No videos to merge for {destination.name}")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    destination.parent.mkdir(parents=True, exist_ok=True)
    concat_file = destination.with_suffix(".concat.txt")
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    lines = []
    for video in videos:
        resolved = str(video.resolve())
        if "\n" in resolved or "\r" in resolved:
            raise RuntimeError("Unexpected newline in video path")
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
            capture_output=True, text=True, timeout=2400, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Merge failed for {destination.name}: {result.stderr[-1600:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_VIDEO_BYTES:
            raise RuntimeError(f"Merged file is empty: {destination.name}")

        info = probe(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Merged video has no audio: {destination.name}")
        if (info["width"], info["height"]) != RESOLUTION:
            raise RuntimeError(f"Merged video is not 1280x720: {destination.name}")

        os.replace(temporary, destination)
        return probe(destination)
    finally:
        concat_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def save_manifest(status, mode, fps, parts, scenes_per_part,
                  completed_scenes, part_records, final_video=None,
                  alias=None, duration=None, errors=None, audio_mix=None):
    write_json(RENDER_MANIFEST, {
        "status": status,
        "renderer": VERSION,
        "format": mode,
        "fps": fps,
        "resolution": list(RESOLUTION),
        "parts_expected": parts,
        "scenes_per_part": scenes_per_part,
        "expected_scenes": parts * scenes_per_part,
        "completed_count": len(completed_scenes),
        "completed_scenes": completed_scenes,
        "parts": part_records,
        "final_video": final_video,
        "alias": alias,
        "duration": duration,
        "captions_burned": False,
        "audio_mix": audio_mix or {},
        "errors": errors or [],
    })


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Both ffmpeg and ffprobe are required")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    fps = int(get_fps(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")
    if min(parts, scenes_per_part, fps) < 1:
        raise RuntimeError("PARTS, SCENES and FPS must be positive")

    gains = {
        "music": mix_gain(config, "music", 0.24),
        "ambient": mix_gain(config, "ambient", 0.09),
        "sfx": mix_gain(config, "sfx", 1.0),
    }

    if not PHOTO_MANIFEST.is_file():
        raise RuntimeError(f"Photo-motion manifest missing: {PHOTO_MANIFEST}")

    photo_data = read_json(PHOTO_MANIFEST)
    if isinstance(photo_data, dict) and photo_data.get("status") not in (None, "completed"):
        raise RuntimeError("Photo-motion manifest is not completed")

    photo_map = validate_photo_scenes(photo_data, parts, scenes_per_part)
    audio_map = load_audio_map(parts, scenes_per_part)
    backgrounds = load_backgrounds(config)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    completed_scenes = []
    part_records = []
    errors = []

    for global_scene in range(1, parts * scenes_per_part + 1):
        part = (global_scene - 1) // scenes_per_part + 1
        scene = (global_scene - 1) % scenes_per_part + 1
        key = (part, scene)
        source, narration = photo_map[key], audio_map[key]
        duration = narration["duration"]
        cues = load_sfx(part, scene, duration, config, gains["sfx"])
        destination = RENDER_DIR / f"part_{part:02d}_scene_{scene:02d}.mp4"

        try:
            info, signature, used_cues = render_scene(
                source, narration, backgrounds, cues,
                destination, fps, duration, gains,
            )
        except Exception as exc:
            errors.append({
                "part": part, "scene": scene,
                "global_scene": global_scene, "error": str(exc),
            })
            save_manifest(
                "failed", mode, fps, parts, scenes_per_part,
                completed_scenes, part_records, errors=errors,
            )
            raise RuntimeError(
                f"Scene render failed at part {part}, scene {scene}: {exc}"
            ) from exc

        completed_scenes.append({
            "part": part,
            "scene": scene,
            "global_scene": global_scene,
            "status": "completed",
            "source_video": relative(source["path"]),
            "narration": relative(narration["path"]),
            "source_rendered_video": relative(destination),
            "video": relative(destination),
            "duration": info["duration"],
            "signature": signature,
            "captions_burned": False,
            "background_tracks": [
                {"category": category, "path": relative(path)}
                for category, path in sorted(backgrounds.items())
            ],
            "sfx_count": len(used_cues),
        })
        save_manifest(
            "in_progress", mode, fps, parts, scenes_per_part,
            completed_scenes, part_records,
        )
        log(
            f"SCENE {global_scene}/{parts * scenes_per_part} complete "
            f"(part {part}, scene {scene}, {info['duration']:.2f}s)"
        )

    part_videos = []
    for part in range(1, parts + 1):
        scene_files = [
            RENDER_DIR / f"part_{part:02d}_scene_{scene:02d}.mp4"
            for scene in range(1, scenes_per_part + 1)
        ]
        for path in scene_files:
            if not path.is_file():
                raise RuntimeError(f"Expected scene render missing: {path}")

        part_path = PART_DIR / f"part_{part:02d}.mp4"
        part_info = concat_videos(scene_files, part_path)
        part_videos.append(part_path)
        part_records.append({
            "part": part,
            "status": "completed",
            "path": relative(part_path),
            "duration": part_info["duration"],
            "scenes": scenes_per_part,
        })
        save_manifest(
            "in_progress", mode, fps, parts, scenes_per_part,
            completed_scenes, part_records,
        )
        log(f"PART {part}/{parts} merged")

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(part_videos, final_path)

    alias_name = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    alias_path = VIDEO_DIR / alias_name
    shutil.copy2(final_path, alias_path)

    opposite = VIDEO_DIR / (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite.unlink(missing_ok=True)

    audio_mix = {
        "narration": True,
        "music": "music" in backgrounds,
        "ambient": "ambient" in backgrounds,
        "sfx_enabled": enabled(config, "sfx"),
        "mix_profile": "narration-first-ducked-background",
        "background_gains": {
            category: gains[category] for category in backgrounds
        },
        "sfx_gain_multiplier": gains["sfx"],
        "external_api_calls": 0,
        "paid_services_used": False,
    }

    save_manifest(
        "completed", mode, fps, parts, scenes_per_part,
        completed_scenes, part_records,
        final_video=relative(final_path),
        alias=relative(alias_path),
        duration=final_info["duration"],
        errors=errors,
        audio_mix=audio_mix,
    )

    log("RENDER COMPLETE")
    log("Mode:", mode)
    log("Final video:", relative(final_path))
    log("Resolution:", f"{RESOLUTION[0]}x{RESOLUTION[1]}")
    log("Scenes:", len(completed_scenes))
    log("Parts:", len(part_records))
    log("Music gain:", gains["music"])
    log("Ambient gain:", gains["ambient"])
    log("SFX multiplier:", gains["sfx"])
    log("Captions are not burned yet; caption stage must run afterward.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("RENDER FAILED:", str(exc))
        sys.exit(1)