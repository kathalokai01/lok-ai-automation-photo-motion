
#!/usr/bin/env python3
"""Resume-safe scene renderer with manifest-driven audio mixing and SFX cues."""

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
SOUND_DIR = OUTPUT / "sound_design"

PHOTO_MANIFEST = PHOTO_DIR / "photo_motion_manifest.json"
AUDIO_MANIFEST = AUDIO_DIR / "audio_jobs.json"
SOUND_MANIFEST = SOUND_DIR / "sound_design_manifest.json"
SFX_CUE_MANIFEST = SOUND_DIR / "sfx_cues.json"
RENDER_MANIFEST = RENDER_DIR / "render_manifest.json"

MIN_MEDIA_BYTES = 1000
DURATION_TOLERANCE = 0.8
RENDERER_VERSION = "render-video-v15-manifest-audio"


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
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
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


def probe(path, audio_only=False):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is not installed")

    entries = "format=duration:stream=codec_type"
    if not audio_only:
        entries += ",stream=width,height"

    result = subprocess.run(
        [
            executable, "-v", "error", "-show_entries", entries,
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")

    streams = data.get("streams", [])
    audio = any(row.get("codec_type") == "audio" for row in streams)

    if audio_only:
        if not audio:
            raise RuntimeError(f"No audio stream: {path.name}")
        return {"duration": duration}

    video = next(
        (row for row in streams if row.get("codec_type") == "video"), None
    )
    if not video:
        raise RuntimeError(f"No video stream: {path.name}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": audio,
    }


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
        audio = safe_path(value, f"Narration {key}", 100)
        mapping[key] = {
            "part": key[0],
            "scene": key[1],
            "path": audio,
            "duration": probe(audio, audio_only=True)["duration"],
            "sha256": sha256_file(audio),
        }

    return mapping


def enabled_setting(config, category, default=True):
    aliases = {
        "music": ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC"),
        "ambient": ("AMBIENT_ENABLED", "AMBIENT", "AMBIENT_SOUND"),
        "sfx": ("SFX_ENABLED", "SFX", "SOUND_EFFECTS"),
    }
    for key in aliases[category]:
        if key not in config:
            continue
        value = str(config[key]).strip().lower()
        if value in {"1", "true", "yes", "on", "enabled"}:
            return True
        if value in {"0", "false", "no", "off", "disabled"}:
            return False
        raise RuntimeError(f"{key} must be true or false")
    return default


def track_category(path):
    lower = str(path).lower()
    if any(token in lower for token in ("ambient", "environment", "ambience")):
        return "ambient"
    if any(token in lower for token in ("music", "bgm", "background")):
        return "music"
    raise RuntimeError(f"Cannot classify background track: {path}")


def background_gain(category):
    return {"music": 0.10, "ambient": 0.045}[category]


def optional_background_files(config):
    """Use sound-design manifest first, then compatible legacy paths."""
    enabled = {
        category: enabled_setting(config, category)
        for category in ("music", "ambient")
    }
    selected = {}

    manifest = {}
    if SOUND_MANIFEST.is_file():
        data = read_json(SOUND_MANIFEST)
        if data.get("status") == "completed":
            manifest = data.get("tracks", {})

    for category in ("music", "ambient"):
        if not enabled[category]:
            continue

        record = manifest.get(category)
        if isinstance(record, dict) and record.get("status") == "completed":
            path = safe_path(record.get("path"), f"{category} track", 100)
            probe(path, audio_only=True)
            selected[category] = path
            continue

        legacy_roots = (
            OUTPUT / "audio",
            OUTPUT / "sound",
            OUTPUT / "music",
        )
        tokens = (
            ("ambient", "environment", "ambience")
            if category == "ambient"
            else ("music", "bgm", "background")
        )
        found = None

        for root in legacy_roots:
            if not root.exists():
                continue
            for candidate in sorted(root.rglob("*")):
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in {
                    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"
                }:
                    continue
                lower = str(candidate).lower()
                if any(token in lower for token in tokens):
                    found = candidate.resolve()
                    break
            if found:
                break

        if found:
            probe(found, audio_only=True)
            selected[category] = found

    return selected


def load_sfx_cues(part, scene, duration, config):
    if not enabled_setting(config, "sfx"):
        return []
    if not SFX_CUE_MANIFEST.is_file():
        return []

    data = read_json(SFX_CUE_MANIFEST)
    if data.get("status") not in (None, "completed"):
        raise RuntimeError("SFX cue manifest is not completed")

    selected = []
    for index, row in enumerate(get_rows(data, "cues"), start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid SFX cue row {index}")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"SFX cue {index}: invalid scene mapping") from exc

        if key != (part, scene):
            continue

        try:
            cue_time = float(row["time"])
            gain = float(row.get("gain", 0.075))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"SFX cue {index}: invalid time or gain") from exc

        if not math.isfinite(cue_time) or cue_time < 0 or cue_time >= duration:
            raise RuntimeError(f"SFX cue {index}: time outside scene duration")
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise RuntimeError(f"SFX cue {index}: gain must be between 0 and 1")

        sound = safe_path(row.get("file"), f"SFX cue {index}", 100)
        info = probe(sound, audio_only=True)
        if info["duration"] <= 0:
            continue

        selected.append({
            "path": sound,
            "time": cue_time,
            "gain": gain,
            "sha256": sha256_file(sound),
        })

    return selected


def cache_signature(source, narration, backgrounds, cues, fps, resolution, duration):
    payload = {
        "renderer": RENDERER_VERSION,
        "source_sha256": sha256_file(source),
        "narration_sha256": narration["sha256"],
        "background": [
            {
                "path": relative(path),
                "sha256": sha256_file(path),
                "category": category,
                "gain": background_gain(category),
            }
            for category, path in sorted(backgrounds.items())
        ],
        "sfx_cues": [
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
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def validate_rendered_file(path, resolution, expected_duration):
    info = probe(path)
    if not info["has_audio"]:
        raise RuntimeError(f"Rendered file has no audio: {path.name}")
    if (info["width"], info["height"]) != resolution:
        raise RuntimeError(f"Resolution mismatch: {path.name}")
    if abs(info["duration"] - expected_duration) > DURATION_TOLERANCE:
        raise RuntimeError(
            f"Duration mismatch for {path.name}: expected "
            f"{expected_duration:.2f}s, got {info['duration']:.2f}s"
        )
    return info


def render_scene(
    source, narration, backgrounds, cues, destination,
    fps, resolution, duration,
):
    sidecar = destination.with_suffix(".render.json")
    signature = cache_signature(
        source, narration, backgrounds, cues, fps, resolution, duration
    )

    if destination.is_file() and sidecar.is_file():
        try:
            cached = read_json(sidecar)
            if cached.get("signature") == signature:
                info = validate_rendered_file(destination, resolution, duration)
                log("RESUME: cached scene", destination.name)
                return info, signature
        except Exception as exc:
            log("CACHE INVALID:", destination.name, str(exc))

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")

    # Input 0: video; input 1: narration; then looping backgrounds; then one-shot SFX.
    inputs = ["-i", str(source), "-i", str(narration["path"])]
    background_order = list(sorted(backgrounds.items()))
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
        gain = background_gain(category)
        filters.append(
            f"[{next_input}:a]aresample=44100,"
            f"volume={gain:.4f},"
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
        "alimiter=limit=0.95:level=false,"
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
            command, capture_output=True, text=True,
            timeout=1200, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Render failed for {destination.name}: "
                f"{result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_MEDIA_BYTES:
            raise RuntimeError(f"Rendered file is empty: {destination.name}")

        info = validate_rendered_file(temporary, resolution, duration)
        os.replace(temporary, destination)

        write_json(sidecar, {
            "status": "completed",
            "signature": signature,
            "source": relative(source),
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
            "renderer": RENDERER_VERSION,
        })
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
            capture_output=True, text=True, timeout=2400, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Merge failed for {destination.name}: "
                f"{result.stderr[-1800:]}"
            )
        if not temporary.is_file() or temporary.stat().st_size < MIN_MEDIA_BYTES:
            raise RuntimeError(f"Merged file is empty: {destination.name}")
        info = probe(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Merged video has no audio: {destination.name}")
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
    expected_count = parts * scenes_per_part
    fps = int(get_fps(config))
    resolution = (1280, 720) if mode == "full" else (720, 1280)

    if parts < 1 or scenes_per_part < 1 or fps < 1:
        raise RuntimeError("Invalid parts, scenes or FPS configuration")

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
    for index, row in enumerate(photo_rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid photo-motion row {index}")
        try:
            part = int(row["part"])
            local_scene = int(row.get("local_scene", row["scene"]))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc

        expected_part = ((index - 1) // scenes_per_part) + 1
        expected_local = ((index - 1) % scenes_per_part) + 1
        if (global_scene, part, local_scene) != (
            index, expected_part, expected_local
        ):
            raise RuntimeError(f"Non-contiguous scene mapping at row {index}")

        key = (part, local_scene)
        if key in photo_map:
            raise RuntimeError(f"Duplicate photo-motion mapping: {key}")

        value = next(
            (
                row.get(field)
                for field in ("video", "output", "video_path")
                if isinstance(row.get(field), str) and row[field].strip()
            ),
            None,
        )
        source = safe_path(value, f"Photo-motion scene {key}", MIN_MEDIA_BYTES)
        info = probe(source)
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Photo-motion resolution mismatch for {key}")
        photo_map[key] = {
            "part": part,
            "scene": local_scene,
            "global_scene": global_scene,
            "path": source,
            "duration": info["duration"],
        }

    narration_map = audio_job_map()
    if len(narration_map) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} completed narration jobs; "
            f"found {len(narration_map)}"
        )

    expected_keys = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    if set(photo_map) != expected_keys or set(narration_map) != expected_keys:
        raise RuntimeError("Photo-motion/narration scene mapping mismatch")

    backgrounds = optional_background_files(config)
    log("FORMAT:", mode)
    log("EXPECTED SCENES:", expected_count)
    log("BACKGROUND TRACKS:", [
        {
            "path": relative(path),
            "category": category,
            "gain": background_gain(category),
        }
        for category, path in sorted(backgrounds.items())
    ])
    log("SFX CUE MANIFEST:", relative(SFX_CUE_MANIFEST))

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    scene_rows = []

    for global_scene in range(1, expected_count + 1):
        part = ((global_scene - 1) // scenes_per_part) + 1
        local_scene = ((global_scene - 1) % scenes_per_part) + 1
        key = (part, local_scene)
        source = photo_map[key]["path"]
        narration = narration_map[key]
        duration = max(photo_map[key]["duration"], narration["duration"])
        cues = load_sfx_cues(part, local_scene, duration, config)
        destination = RENDER_DIR / f"scene_{global_scene:04d}.mp4"

        info, signature = render_scene(
            source, narration, backgrounds, cues, destination,
            fps, resolution, duration,
        )

        scene_rows.append({
            "scene": global_scene,
            "global_scene": global_scene,
            "part": part,
            "local_scene": local_scene,
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
            "sfx_cues": [
                {
                    "file": relative(cue["path"]),
                    "time": cue["time"],
                    "gain": cue["gain"],
                }
                for cue in cues
            ],
            "captions_burned": False,
            "status": "completed",
        })

        write_json(RENDER_MANIFEST, {
            "status": "in_progress",
            "renderer": RENDERER_VERSION,
            "format": mode,
            "captions_burned": False,
            "expected_scenes": expected_count,
            "completed_count": len(scene_rows),
            "completed_scenes": scene_rows,
            "parts": [],
            "background_tracks": [
                {
                    "path": relative(path),
                    "category": category,
                    "gain": background_gain(category),
                }
                for category, path in sorted(backgrounds.items())
            ],
            "sfx_cue_manifest": (
                relative(SFX_CUE_MANIFEST)
                if SFX_CUE_MANIFEST.is_file() else None
            ),
        })
        log(f"RENDERED {global_scene}/{expected_count}; SFX cues={len(cues)}")

    part_paths = []
    for part in range(1, parts + 1):
        part_rows = [
            row for row in scene_rows if row["part"] == part
        ]
        part_rows.sort(key=lambda row: row["local_scene"])
        if len(part_rows) != scenes_per_part:
            raise RuntimeError(f"Part {part} scene count mismatch")

        videos = [
            safe_path(row["video"], f"Part {part} scene", MIN_MEDIA_BYTES)
            for row in part_rows
        ]
        part_path = PART_DIR / f"part_{part:02d}.mp4"
        info = concat_videos(videos, part_path)
        if (info["width"], info["height"]) != resolution:
            raise RuntimeError(f"Part {part} resolution mismatch")
        part_paths.append(relative(part_path))
        log(f"MERGED PART {part}/{parts}")

    final_path = VIDEO_DIR / "katha_lok_ai_final.mp4"
    final_info = concat_videos(
        [
            safe_path(row["video"], "Final scene", MIN_MEDIA_BYTES)
            for row in scene_rows
        ],
        final_path,
    )
    if (final_info["width"], final_info["height"]) != resolution:
        raise RuntimeError("Final video resolution mismatch")

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    alias = VIDEO_DIR / (
        "katha_lok_ai_short.mp4"
        if mode == "short" else "katha_lok_ai_full.mp4"
    )
    shutil.copy2(final_path, alias)

    opposite = VIDEO_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "short" else "katha_lok_ai_short.mp4"
    )
    opposite.unlink(missing_ok=True)

    write_json(RENDER_MANIFEST, {
        "status": "completed",
        "renderer": RENDERER_VERSION,
        "format": mode,
        "captions_burned": False,
        "expected_scenes": expected_count,
        "completed_count": len(scene_rows),
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
        "background_tracks": [
            {
                "path": relative(path),
                "category": category,
                "gain": background_gain(category),
            }
            for category, path in sorted(backgrounds.items())
        ],
        "sfx_cue_manifest": (
            relative(SFX_CUE_MANIFEST)
            if SFX_CUE_MANIFEST.is_file() else None
        ),
        "sfx_policy": "Only explicitly timed cue-manifest entries are mixed.",
    })

    log("=" * 58)
    log("SCENE RENDERING COMPLETED")
    log("FORMAT:", mode)
    log("SCENES:", expected_count)
    log("PARTS:", parts)
    log("FINAL:", relative(final_path))
    log("CAPTIONS: pending next pipeline stage")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("RENDER PIPELINE FAILED:", str(exc))
        sys.exit(1)
