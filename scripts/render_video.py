
#!/usr/bin/env python3
"""Resume-safe 16:9 renderer with narration and sound mixing."""

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

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_fps,
)

OUTPUT = ROOT / "output"
PHOTO_DIR = OUTPUT / "photo_motion"
AUDIO_DIR = OUTPUT / "narration"
SOUND_DIR = OUTPUT / "sound_design"
RENDER_DIR = OUTPUT / "scene_renders"
PART_DIR = OUTPUT / "parts"
VIDEO_DIR = OUTPUT / "videos"

PHOTO_MANIFEST = PHOTO_DIR / "photo_motion_manifest.json"
AUDIO_MANIFEST = AUDIO_DIR / "audio_jobs.json"
SOUND_MANIFEST = SOUND_DIR / "sound_design_manifest.json"
SFX_MANIFEST = SOUND_DIR / "sfx_cues.json"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"

VERSION = "render-video-v19-ffprobe-metadata-fix"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100
DURATION_TOLERANCE = 1.0
OUTPUT_RESOLUTION = (1280, 720)

SFX_TIME_KEYS = (
    "time", "time_seconds", "timestamp", "start_seconds", "start"
)
AUDIO_PATH_KEYS = ("output", "audio_path", "path", "file", "audio")


def log(*values):
    print(*values, flush=True)


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, (dict, list)):
        raise RuntimeError(f"Unexpected JSON structure: {path}")
    return data


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def safe_path(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing file path")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path is outside the repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label}: file is empty or too small: {path}")

    return path


def first_path(row, keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def rows_from(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def probe(path, audio_only=False):
    """Read duration and stream dimensions using valid ffprobe syntax."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    # Correct syntax: format and stream entries are separate sections.
    entries = (
        "format=duration:stream=codec_type"
        if audio_only
        else "format=duration:stream=codec_type,width,height"
    )

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", entries,
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {Path(path).name}: "
            f"{result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid media metadata for {Path(path).name}: "
            f"{result.stdout[-500:]}"
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    streams = data.get("streams", [])
    has_audio = any(
        stream.get("codec_type") == "audio"
        for stream in streams
        if isinstance(stream, dict)
    )

    if audio_only:
        if not has_audio:
            raise RuntimeError(f"No audio stream in {path}")
        return {"duration": duration}

    video = next(
        (
            stream for stream in streams
            if isinstance(stream, dict)
            and stream.get("codec_type") == "video"
        ),
        None,
    )
    if video is None:
        raise RuntimeError(f"No video stream in {path}")

    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid video dimensions in {Path(path).name}: "
            f"{video!r}"
        ) from exc

    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"Invalid video dimensions in {Path(path).name}: "
            f"ffprobe returned width={width}, height={height}. "
            "The file may be corrupt or may not contain a decodable video stream."
        )

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
        "sfx": (
            "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
            "SOUND_EFFECTS_ENABLED",
        ),
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


def scene_number(row, primary, fallback=None):
    value = row.get(primary)
    if value is None and fallback:
        value = row.get(fallback)
    if value is None:
        raise ValueError(f"Missing {primary}")
    return int(value)


def expected_keys(parts, scenes_per_part):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }


def validate_photo_scenes(data, parts, scenes_per_part):
    expected = expected_keys(parts, scenes_per_part)
    rows = rows_from(
        data, "scenes", "videos", "completed_scenes", "items"
    )
    if len(rows) != len(expected):
        raise RuntimeError(
            f"Expected {len(expected)} photo-motion scenes; found {len(rows)}"
        )

    mapping = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid photo-motion row {index}")

        try:
            part = scene_number(row, "part")
            scene = scene_number(row, "local_scene", "scene")
            global_scene = (
                int(row["global_scene"])
                if row.get("global_scene") is not None
                else index
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid photo-motion mapping at row {index}: {exc}"
            ) from exc

        expected_part = (index - 1) // scenes_per_part + 1
        expected_scene = (index - 1) % scenes_per_part + 1
        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(
                f"Incorrect photo-motion order at row {index}: "
                f"got {(part, scene, global_scene)}, "
                f"expected {(expected_part, expected_scene, index)}"
            )

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"Duplicate photo-motion scene: {key}")

        source_value = first_path(
            row,
            (
                "source_video", "video", "output",
                "rendered_video", "path", "file",
            ),
        )
        source = safe_path(
            source_value, f"Photo-motion scene {key}", MIN_VIDEO_BYTES
        )

        try:
            info = probe(source)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot validate photo-motion scene {key} "
                f"({source.name}): {exc}"
            ) from exc

        mapping[key] = {
            "part": part,
            "scene": scene,
            "global_scene": global_scene,
            "path": source,
            "duration": info["duration"],
            "sha256": sha256_file(source),
            "width": info["width"],
            "height": info["height"],
        }

    if set(mapping) != expected:
        raise RuntimeError(
            "Photo-motion scene mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )
    return mapping


def load_audio_map(parts, scenes_per_part):
    if not AUDIO_MANIFEST.is_file():
        raise RuntimeError(f"Narration manifest missing: {AUDIO_MANIFEST}")

    data = read_json(AUDIO_MANIFEST)
    jobs = rows_from(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        raise RuntimeError("No narration jobs found")

    mapping = {}
    for index, row in enumerate(jobs, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}")
        try:
            part, scene = int(row["part"]), int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration part/scene at row {index}"
            ) from exc

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"Duplicate narration mapping: {key}")
        if str(row.get("status", "")).strip().lower() != "completed":
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
            "sha256": sha256_file(audio),
        }

    expected = expected_keys(parts, scenes_per_part)
    if set(mapping) != expected:
        raise RuntimeError(
            "Narration scene mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )
    return mapping


def background_gain(category):
    return {"music": 0.12, "ambient": 0.045}[category]


def load_backgrounds(config):
    manifest_tracks = {}
    if SOUND_MANIFEST.is_file():
        data = read_json(SOUND_MANIFEST)
        if isinstance(data, dict) and data.get("status") == "completed":
            tracks = data.get("tracks", {})
            if isinstance(tracks, dict):
                manifest_tracks = tracks

    selected = {}
    for category in ("music", "ambient"):
        if not enabled(config, category):
            continue

        record = manifest_tracks.get(category)
        if isinstance(record, dict) and record.get("status") == "completed":
            track = safe_path(
                record.get("path") or record.get("file") or record.get("output"),
                f"{category} sound-design track",
                MIN_AUDIO_BYTES,
            )
            probe(track, audio_only=True)
            selected[category] = track
            continue

        tokens = (
            ("ambient", "environment", "ambience")
            if category == "ambient"
            else ("music", "bgm", "background")
        )
        found = None
        for directory in (
            OUTPUT / "audio", OUTPUT / "sound", OUTPUT / "music"
        ):
            if not directory.exists():
                continue
            for candidate in sorted(directory.rglob("*")):
                if (
                    candidate.is_file()
                    and candidate.suffix.lower() in {
                        ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"
                    }
                    and any(token in str(candidate).lower() for token in tokens)
                ):
                    found = candidate.resolve()
                    break
            if found:
                break

        if found:
            found = safe_path(
                str(found), f"Discovered {category} track", MIN_AUDIO_BYTES
            )
            probe(found, audio_only=True)
            selected[category] = found
    return selected


def cue_number(row, keys, default=None, label="SFX cue"):
    value, chosen = None, None
    for key in keys:
        if key not in row or row[key] is None:
            continue
        if isinstance(row[key], str) and not row[key].strip():
            continue
        value, chosen = row[key], key
        break

    if value is None:
        if default is not None:
            return float(default), None
        raise RuntimeError(f"{label}: missing numeric value")

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{label}: invalid numeric field {chosen}: {value!r}"
        ) from exc
    if not math.isfinite(number):
        raise RuntimeError(f"{label}: non-finite numeric value in {chosen}")
    return number, chosen


def load_sfx(part, scene, duration, config):
    if not enabled(config, "sfx") or not SFX_MANIFEST.is_file():
        return []

    data = read_json(SFX_MANIFEST)
    if not isinstance(data, dict):
        raise RuntimeError("SFX cue manifest must be a JSON object")
    if data.get("status") not in (None, "completed"):
        raise RuntimeError("SFX cue manifest is not completed")

    selected = []
    for index, row in enumerate(rows_from(data, "cues", "items"), start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid SFX cue row {index}")
        try:
            cue_part, cue_scene = int(row["part"]), int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid SFX part/scene mapping at row {index}"
            ) from exc

        if cue_part < 1 or cue_scene < 1:
            raise RuntimeError(f"Invalid SFX scene mapping at row {index}")
        if (cue_part, cue_scene) != (part, scene):
            continue

        cue_time, time_field = cue_number(
            row, SFX_TIME_KEYS, label=f"SFX cue {index}"
        )
        gain, _ = cue_number(
            row, ("gain", "volume", "volume_gain"),
            default=0.075, label=f"SFX gain {index}",
        )
        if cue_time < 0 or cue_time >= duration:
            raise RuntimeError(
                f"SFX cue {index} is outside narration duration: "
                f"time={cue_time:.3f}s, duration={duration:.3f}s"
            )
        if not 0 <= gain <= 1:
            raise RuntimeError(f"SFX gain out of range at cue {index}")

        path = safe_path(
            first_path(row, ("file", "path", "audio", "output")),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )
        info = probe(path, audio_only=True)
        selected.append({
            "path": path,
            "time": cue_time,
            "time_field": time_field,
            "gain": gain,
            "sha256": sha256_file(path),
            "duration": info["duration"],
            "kind": str(row.get("kind", row.get("type", "unspecified"))),
        })

    selected.sort(key=lambda item: (item["time"], item["kind"]))
    return selected


def signature_for(source, narration, backgrounds, cues, fps, resolution, duration):
    payload = {
        "renderer": VERSION,
        "source_sha256": source["sha256"],
        "narration_sha256": narration["sha256"],
        "backgrounds": [
            {
                "path": relative(path),
                "sha256": sha256_file(path),
                "category": category,
                "gain": background_gain(category),
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
        "resolution": list(resolution),
        "duration": round(duration, 3),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_render(path, resolution, expected_duration):
    info = probe(path)
    if not info["has_audio"]:
        raise RuntimeError(f"Rendered scene has no audio: {path.name}")
    if (info["width"], info["height"]) != resolution:
        raise RuntimeError(f"Rendered resolution mismatch: {path.name}")
    if abs(info["duration"] - expected_duration) > DURATION_TOLERANCE:
        raise RuntimeError(
            f"Rendered duration mismatch for {path.name}: "
            f"expected {expected_duration:.2f}s, got {info['duration']:.2f}s"
        )
    return info


def render_scene(
    source, narration, backgrounds, cues, destination,
    fps, resolution, duration,
):
    sidecar = destination.with_suffix(".render.json")
    signature = signature_for(
        source, narration, backgrounds, cues, fps, resolution, duration
    )

    if destination.is_file() and sidecar.is_file():
        try:
            cached = read_json(sidecar)
            if isinstance(cached, dict) and cached.get("signature") == signature:
                info = validate_render(destination, resolution, duration)
                log("RESUME: reused", destination.name)
                return info, signature, cues
        except Exception as exc:
            log("CACHE INVALID:", destination.name, str(exc))

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    inputs = ["-i", str(source["path"]), "-i", str(narration["path"])]
    background_order = sorted(backgrounds.items())

    for _, path in background_order:
        inputs.extend(["-stream_loop", "-1", "-i", str(path)])
    for cue in cues:
        inputs.extend(["-i", str(cue["path"])])

    width, height = resolution
    filters = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[v]",
        "[1:a]aresample=44100,"
        f"apad,atrim=duration={duration:.3f},"
        "asetpts=PTS-STARTPTS[narr]",
    ]

    audio_labels = ["[narr]"]
    next_input = 2

    for category, _ in background_order:
        label = f"bg{next_input}"
        filters.append(
            f"[{next_input}:a]aresample=44100,"
            f"volume={background_gain(category):.4f},"
            f"apad,atrim=duration={duration:.3f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        audio_labels.append(f"[{label}]")
        next_input += 1

    cue_details = []
    for cue in cues:
        label = f"sfx{next_input}"
        delay_ms = int(round(cue["time"] * 1000))
        filters.append(
            f"[{next_input}:a]aresample=44100,"
            f"volume={cue['gain']:.4f},"
            f"adelay=delays={delay_ms}:all=1,"
            f"atrim=duration={duration:.3f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        audio_labels.append(f"[{label}]")
        cue_details.append({
            "path": relative(cue["path"]),
            "time": cue["time"],
            "time_field": cue["time_field"],
            "gain": cue["gain"],
            "kind": cue["kind"],
            "duration_seconds": cue["duration"],
            "sha256": cue["sha256"],
        })
        next_input += 1

    filters.append(
        "".join(audio_labels)
        + f"amix=inputs={len(audio_labels)}:duration=longest:"
        "dropout_transition=2:normalize=0,"
        "alimiter=limit=0.90:level=false,"
        f"atrim=duration={duration:.3f},"
        "asetpts=PTS-STARTPTS[a]"
    )

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(temporary),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Render failed for {destination.name}: "
                f"{result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_VIDEO_BYTES:
            raise RuntimeError(f"Rendered output is empty: {destination.name}")

        info = validate_render(temporary, resolution, duration)
        os.replace(temporary, destination)
        write_json(sidecar, {
            "status": "completed",
            "signature": signature,
            "source": relative(source["path"]),
            "narration": relative(narration["path"]),
            "background_tracks": [
                {
                    "path": relative(path),
                    "category": category,
                    "gain": background_gain(category),
                }
                for category, path in background_order
            ],
            "sfx_cues": cue_details,
            "video": relative(destination),
            "duration": info["duration"],
            "target_duration": duration,
            "fps": fps,
            "resolution": list(resolution),
            "renderer": VERSION,
        })
        return info, signature, cues
    finally:
        temporary.unlink(missing_ok=True)


def concat_videos(videos, destination):
    if not videos:
        raise RuntimeError(f"No scenes available for {destination.name}")

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
            raise RuntimeError("Unexpected newline in a video path")
        escaped = resolved.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")

    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=2400,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Merge failed for {destination.name}: "
                f"{result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_VIDEO_BYTES:
            raise RuntimeError(f"Merged output is empty: {destination.name}")

        info = probe(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Merged output has no audio: {destination.name}")
        if (info["width"], info["height"]) != OUTPUT_RESOLUTION:
            raise RuntimeError(
                f"Merged output is not 1280x720: {destination.name}"
            )
        os.replace(temporary, destination)
        return probe(destination)
    finally:
        concat_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def save_manifest(
    status, mode, fps, resolution, parts, scenes_per_part,
    completed_scenes, part_records, final_video=None, alias=None,
    duration=None, errors=None,
):
    write_json(RENDER_MANIFEST, {
        "status": status,
        "renderer": VERSION,
        "format": mode,
        "fps": fps,
        "resolution": list(resolution),
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
        "audio_mix": {
            "narration": True,
            "music": None,
            "ambient": None,
            "sfx_enabled": None,
            "external_api_calls": 0,
            "paid_services_used": False,
        },
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
        raise RuntimeError(f"Unsupported video format: {mode}")
    if parts < 1 or scenes_per_part < 1 or fps < 1:
        raise RuntimeError("PARTS, SCENES and FPS must be positive")

    expected_count = parts * scenes_per_part
    resolution = OUTPUT_RESOLUTION

    if not PHOTO_MANIFEST.is_file():
        raise RuntimeError(f"Photo-motion manifest missing: {PHOTO_MANIFEST}")

    photo_data = read_json(PHOTO_MANIFEST)
    if isinstance(photo_data, dict) and photo_data.get("status") not in (
        None, "completed",
    ):
        raise RuntimeError("Photo-motion manifest is not completed")

    photo_map = validate_photo_scenes(photo_data, parts, scenes_per_part)
    audio_map = load_audio_map(parts, scenes_per_part)
    backgrounds = load_backgrounds(config)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    completed_scenes, part_records, errors = [], [], []

    for global_scene in range(1, expected_count + 1):
        part = (global_scene - 1) // scenes_per_part + 1
        scene = (global_scene - 1) % scenes_per_part + 1
        key = (part, scene)
        source, narration = photo_map[key], audio_map[key]
        duration = narration["duration"]
        cues = load_sfx(part, scene, duration, config)
        destination = RENDER_DIR / f"part_{part:02d}_scene_{scene:02d}.mp4"

        try:
            info, signature, used_cues = render_scene(
                source, narration, backgrounds, cues, destination,
                fps, resolution, duration,
            )
        except Exception as exc:
            errors.append({
                "part": part,
                "scene": scene,
                "global_scene": global_scene,
                "error": str(exc),
            })
            save_manifest(
                "failed", mode, fps, resolution, parts, scenes_per_part,
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
            "in_progress", mode, fps, resolution, parts, scenes_per_part,
            completed_scenes, part_records, errors=errors,
        )
        log(
            f"SCENE {global_scene}/{expected_count} complete: "
            f"part={part}, scene={scene}, duration={info['duration']:.2f}s"
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
            "in_progress", mode, fps, resolution, parts, scenes_per_part,
            completed_scenes, part_records, errors=errors,
        )
        log(f"PART {part}/{parts} merged: {part_path.name}")

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(part_videos, final_path)
    alias_name = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    alias_path = VIDEO_DIR / alias_name
    shutil.copy2(final_path, alias_path)

    opposite_alias = VIDEO_DIR / (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite_alias.unlink(missing_ok=True)

    save_manifest(
        "completed", mode, fps, resolution, parts, scenes_per_part,
        completed_scenes, part_records,
        final_video=relative(final_path),
        alias=relative(alias_path),
        duration=final_info["duration"],
        errors=errors,
    )
    manifest = read_json(RENDER_MANIFEST)
    manifest["audio_mix"] = {
        "narration": True,
        "music": "music" in backgrounds,
        "ambient": "ambient" in backgrounds,
        "sfx_enabled": enabled(config, "sfx"),
        "external_api_calls": 0,
        "paid_services_used": False,
    }
    write_json(RENDER_MANIFEST, manifest)

    log("RENDER COMPLETE")
    log("Final video:", relative(final_path))
    log("Mode:", mode)
    log("Resolution:", f"{resolution[0]}x{resolution[1]}")
    log("Scenes:", len(completed_scenes))
    log("Parts:", len(part_records))
    log("Duration:", round(final_info["duration"], 2), "seconds")
    log("Captions are not burned yet; caption stage must run afterward.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FATAL:", exc)
        sys.exit(1)
