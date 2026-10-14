
#!/usr/bin/env python3
"""Resume-safe photo-motion renderer with narration, music, ambient audio and SFX."""

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

VERSION = "render-video-v16-safe-scene-mapping"
MIN_BYTES = 1000
DURATION_TOLERANCE = 1.0


def log(*values):
    print(*values, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


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
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    entries = "format=duration:stream=codec_type"
    if not audio_only:
        entries += ",stream=width,height"

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
            f"ffprobe failed for {Path(path).name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    streams = data.get("streams", [])
    has_audio = any(
        stream.get("codec_type") == "audio" for stream in streams
    )

    if audio_only:
        if not has_audio:
            raise RuntimeError(f"No audio stream in {path}")
        return {"duration": duration}

    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise RuntimeError(f"No video stream in {path}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": has_audio,
    }


def enabled(config, key, default=True):
    aliases = {
        "music": ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC"),
        "ambient": (
            "AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND",
            "ENVIRONMENT_SOUND", "AMBIENCE_ENABLED",
        ),
        "sfx": ("SFX_ENABLED", "SFX", "SOUND_EFFECTS"),
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


def get_scene_number(row, primary, fallback=None):
    """Read scene identifiers without eagerly evaluating a missing fallback."""
    value = row.get(primary)
    if value is None and fallback is not None:
        value = row.get(fallback)
    if value is None:
        raise ValueError(f"missing {primary}")
    return int(value)


def validate_scene_rows(data, expected_count, scenes_per_part):
    photo_rows = rows_from(
        data, "scenes", "videos", "completed_scenes", "items"
    )

    if len(photo_rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} photo-motion scenes; "
            f"found {len(photo_rows)}"
        )

    mapping = {}
    for index, row in enumerate(photo_rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid photo-motion row {index}")

        try:
            part = get_scene_number(row, "part")
            local_scene = get_scene_number(row, "local_scene", "scene")
            global_scene = get_scene_number(row, "global_scene")
        except (TypeError, ValueError) as exc:
            # Older manifests may omit global_scene; row order is the fallback.
            if isinstance(row, dict) and row.get("global_scene") is None:
                try:
                    part = get_scene_number(row, "part")
                    local_scene = get_scene_number(row, "local_scene", "scene")
                    global_scene = index
                except (TypeError, ValueError) as inner:
                    raise RuntimeError(
                        f"Invalid scene mapping at row {index}: {inner}"
                    ) from inner
            else:
                raise RuntimeError(
                    f"Invalid scene mapping at row {index}: {exc}"
                ) from exc

        expected_part = ((index - 1) // scenes_per_part) + 1
        expected_local = ((index - 1) % scenes_per_part) + 1

        if (part, local_scene, global_scene) != (
            expected_part, expected_local, index
        ):
            raise RuntimeError(
                f"Scene order/mapping error at row {index}: "
                f"part={part}, scene={local_scene}, global={global_scene}; "
                f"expected part={expected_part}, scene={expected_local}, "
                f"global={index}"
            )

        source_value = next(
            (
                row.get(key) for key in (
                    "source_video", "video", "output",
                    "rendered_video", "path", "file",
                )
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        source = safe_path(source_value, f"Photo-motion scene {index}", MIN_BYTES)
        info = probe(source)

        if info["width"] <= 0 or info["height"] <= 0:
            raise RuntimeError(f"Invalid video dimensions: {source}")

        mapping[(part, local_scene)] = {
            "part": part,
            "scene": local_scene,
            "global_scene": global_scene,
            "path": source,
            "duration": info["duration"],
            "sha256": sha256_file(source),
            "width": info["width"],
            "height": info["height"],
        }

    if len(mapping) != expected_count:
        raise RuntimeError("Duplicate or missing photo-motion scene mapping")

    return mapping


def load_audio_map(expected_count):
    if not AUDIO_MANIFEST.is_file():
        raise RuntimeError(f"Narration manifest missing: {AUDIO_MANIFEST}")

    data = read_json(AUDIO_MANIFEST)
    jobs = rows_from(data, "jobs", "audio_jobs", "scenes")
    mapping = {}

    for index, row in enumerate(jobs, start=1):
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).strip().lower() != "completed":
            continue

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError):
            continue

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene mapping: {key}")

        audio_value = next(
            (
                row.get(name) for name in (
                    "output", "audio_path", "path", "file", "audio",
                )
                if isinstance(row.get(name), str) and row[name].strip()
            ),
            None,
        )
        audio = safe_path(audio_value, f"Narration {key}", 100)
        info = probe(audio, audio_only=True)

        mapping[key] = {
            "part": part,
            "scene": scene,
            "path": audio,
            "duration": info["duration"],
            "sha256": sha256_file(audio),
        }

    if len(mapping) != expected_count:
        missing = []
        for index in range(1, expected_count + 1):
            key = (
                ((index - 1) // max(1, expected_count)) + 1,
                ((index - 1) % max(1, expected_count)) + 1,
            )
            if key not in mapping:
                missing.append(key)

        raise RuntimeError(
            f"Narration manifest has {len(mapping)} completed rows; "
            f"expected {expected_count}. Check part/scene mapping. "
            f"Sample available keys: {list(mapping)[:8]}"
        )

    return mapping


def background_gain(category):
    # Keep the voice prominent over the background layers.
    return {"music": 0.12, "ambient": 0.045}[category]


def load_backgrounds(config):
    manifest_tracks = {}
    if SOUND_MANIFEST.is_file():
        data = read_json(SOUND_MANIFEST)
        if data.get("status") == "completed":
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
                record.get("path"), f"{category} sound-design track", 100
            )
            probe(track, audio_only=True)
            selected[category] = track
            continue

        # Backward compatibility with earlier output layouts.
        tokens = (
            ("ambient", "environment", "ambience")
            if category == "ambient"
            else ("music", "bgm", "background")
        )
        found = None

        for root in (OUTPUT / "audio", OUTPUT / "sound", OUTPUT / "music"):
            if not root.exists():
                continue
            for candidate in sorted(root.rglob("*")):
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in {
                    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac",
                }:
                    continue
                if any(token in str(candidate).lower() for token in tokens):
                    found = candidate.resolve()
                    break
            if found:
                break

        if found:
            if not found.is_relative_to(ROOT.resolve()):
                raise RuntimeError("Background audio path escapes repository")
            probe(found, audio_only=True)
            selected[category] = found

    return selected


def load_sfx(part, scene, duration, config):
    if not enabled(config, "sfx") or not SFX_MANIFEST.is_file():
        return []

    data = read_json(SFX_MANIFEST)
    if data.get("status") not in (None, "completed"):
        raise RuntimeError("SFX cue manifest is not completed")

    selected = []
    for index, row in enumerate(rows_from(data, "cues", "items"), start=1):
        if not isinstance(row, dict):
            continue

        try:
            cue_part = int(row["part"])
            cue_scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX scene mapping at cue {index}") from exc

        if (cue_part, cue_scene) != (part, scene):
            continue

        try:
            cue_time = float(row.get("time", 0))
            gain = float(row.get("gain", 0.075))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX time/gain at cue {index}") from exc

        if not math.isfinite(cue_time) or cue_time < 0 or cue_time >= duration:
            raise RuntimeError(f"SFX cue {index} is outside scene duration")
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"SFX cue {index} gain must be from 0 to 1")

        sound = safe_path(row.get("file"), f"SFX cue {index}", 100)
        probe(sound, audio_only=True)

        selected.append({
            "path": sound,
            "time": cue_time,
            "gain": gain,
            "sha256": sha256_file(sound),
        })

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


def render_scene(source, narration, backgrounds, cues, destination,
                 fps, resolution, duration):
    sidecar = destination.with_suffix(".render.json")
    signature = signature_for(
        source, narration, backgrounds, cues, fps, resolution, duration
    )

    if destination.is_file() and sidecar.is_file():
        try:
            cached = read_json(sidecar)
            if cached.get("signature") == signature:
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
            "gain": cue["gain"],
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
        "-movflags", "+faststart", str(temporary),
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
        if not temporary.is_file() or temporary.stat().st_size < MIN_BYTES:
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
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
            timeout=2400,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Merge failed for {destination.name}: "
                f"{result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_BYTES:
            raise RuntimeError(f"Merged output is empty: {destination.name}")

        info = probe(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Merged output has no audio: {destination.name}")

        os.replace(temporary, destination)
        return probe(destination)
    finally:
        for path in (concat_file, temporary):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Both ffmpeg and ffprobe are required")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    fps = int(get_fps(config))
    expected_count = parts * scenes_per_part
    resolution = (1280, 720) if mode == "full" else (720, 1280)

    if not PHOTO_MANIFEST.is_file():
        raise RuntimeError(f"Photo-motion manifest missing: {PHOTO_MANIFEST}")
    if not AUDIO_MANIFEST.is_file():
        raise RuntimeError(f"Narration manifest missing: {AUDIO_MANIFEST}")

    photo_data = read_json(PHOTO_MANIFEST)
    photo_map = validate_scene_rows(
        photo_data, expected_count, scenes_per_part
    )
    audio_map = load_audio_map(expected_count)
    backgrounds = load_backgrounds(config)

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    PART_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    completed = []
    completed_scenes = []
    part_records = []
    errors = []

    for global_scene in range(1, expected_count + 1):
        part = ((global_scene - 1) // scenes_per_part) + 1
        scene = ((global_scene - 1) % scenes_per_part) + 1
        key = (part, scene)

        source = photo_map[key]
        narration = audio_map[key]

        # Keep video and narration aligned to narration length.
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
            write_json(RENDER_MANIFEST, {
                "status": "failed",
                "renderer": VERSION,
                "format": mode,
                "expected_scenes": expected_count,
                "completed_count": len(completed_scenes),
                "completed_scenes": completed_scenes,
                "parts": part_records,
                "errors": errors,
                "captions_burned": False,
            })
            raise RuntimeError(
                f"Scene render failed at part {part}, scene {scene}: {exc}"
            ) from exc

        completed.append(destination)
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
                {
                    "category": category,
                    "path": relative(path),
                }
                for category, path in sorted(backgrounds.items())
            ],
            "sfx_count": len(used_cues),
        })

        write_json(RENDER_MANIFEST, {
            "status": "in_progress",
            "renderer": VERSION,
            "format": mode,
            "fps": fps,
            "resolution": list(resolution),
            "parts_expected": parts,
            "scenes_per_part": scenes_per_part,
            "expected_scenes": expected_count,
            "completed_count": len(completed_scenes),
            "completed_scenes": completed_scenes,
            "parts": part_records,
            "final_video": None,
            "alias": None,
            "captions_burned": False,
            "errors": errors,
        })
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

        write_json(RENDER_MANIFEST, {
            "status": "in_progress",
            "renderer": VERSION,
            "format": mode,
            "fps": fps,
            "resolution": list(resolution),
            "parts_expected": parts,
            "scenes_per_part": scenes_per_part,
            "expected_scenes": expected_count,
            "completed_count": len(completed_scenes),
            "completed_scenes": completed_scenes,
            "parts": part_records,
            "final_video": None,
            "alias": None,
            "captions_burned": False,
            "errors": errors,
        })
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
    try:
        opposite_alias.unlink(missing_ok=True)
    except OSError:
        pass

    manifest = {
        "status": "completed",
        "renderer": VERSION,
        "format": mode,
        "fps": fps,
        "resolution": list(resolution),
        "parts_expected": parts,
        "scenes_per_part": scenes_per_part,
        "expected_scenes": expected_count,
        "completed_count": len(completed_scenes),
        "completed_scenes": completed_scenes,
        "parts": part_records,
        "final_video": relative(final_path),
        "alias": relative(alias_path),
        "duration": final_info["duration"],
        "width": final_info["width"],
        "height": final_info["height"],
        "captions_burned": False,
        "audio_mix": {
            "narration": True,
            "music": "music" in backgrounds,
            "ambient": "ambient" in backgrounds,
            "sfx_enabled": enabled(config, "sfx"),
            "external_api_calls": 0,
            "paid_services_used": False,
        },
        "errors": errors,
    }
    write_json(RENDER_MANIFEST, manifest)

    log("RENDER COMPLETE")
    log("Final video:", relative(final_path))
    log("Mode:", mode)
    log("Scenes:", len(completed_scenes))
    log("Parts:", len(part_records))
    log("Duration:", round(final_info["duration"], 2), "seconds")
    log("Captions are not burned yet; caption stage must run afterward.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FATAL:", exc)
        raise
