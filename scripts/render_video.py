#!/usr/bin/env python3

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


CONFIG_FILE = Path("Input/topic.txt")
SCENES_FILE = Path("output/scenes/scenes.json")

I2V_DIR = Path("output/i2v")
AUDIO_DIR = Path("output/narration/audio")

SCENES_OUT = Path("output/scenes/rendered")
PARTS_OUT = Path("output/parts")

MANIFEST_OUT = Path("output/video_render_manifest.json")


# ============================================================
# CONFIG
# ============================================================

def load_config():
    if not CONFIG_FILE.is_file():
        raise RuntimeError(
            "Input/topic.txt not found."
        )

    config = {}

    for raw in CONFIG_FILE.read_text(
        encoding="utf-8-sig"
    ).splitlines():

        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)

        config[key.strip().upper()] = (
            value.strip().strip('"').strip("'")
        )

    return config


CONFIG = load_config()

FORMAT = CONFIG.get(
    "FORMAT",
    "full"
).strip().lower()

if FORMAT in {
    "short",
    "shorts",
    "vertical",
    "reels",
    "reel",
    "youtube_short",
    "youtube-shorts",
}:
    FORMAT = "short"

elif FORMAT in {
    "full",
    "long",
    "landscape",
    "youtube",
    "youtube_full",
    "youtube-long",
}:
    FORMAT = "full"

else:
    raise RuntimeError(
        f"Unsupported FORMAT: {FORMAT}"
    )


def config_bool(key, default):
    value = CONFIG.get(key)

    if value is None:
        return default

    return str(value).strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
    }


def config_int(key, default, minimum=1, maximum=120):
    try:
        value = int(CONFIG.get(key, default))
    except (TypeError, ValueError):
        value = default

    return max(
        minimum,
        min(maximum, value)
    )


FPS = config_int(
    "FPS",
    24,
    1,
    60
)

RESUME_ENABLED = config_bool(
    "RESUME_ENABLED",
    True
)

SKIP_COMPLETED = config_bool(
    "SKIP_COMPLETED_SCENES",
    True
)

SAVE_CHECKPOINT = config_bool(
    "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
    True
)

WIDTH, HEIGHT = (
    (720, 1280)
    if FORMAT == "short"
    else (1920, 1080)
)


# ============================================================
# HELPERS
# ============================================================

def log(message):
    print(message, flush=True)


def atomic_json(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    temp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    os.replace(
        temp,
        path
    )


def load_json(path):
    if not path.is_file():
        raise RuntimeError(
            f"Required JSON not found: {path}"
        )

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8-sig"
            )
        )

    except Exception as exc:
        raise RuntimeError(
            f"Invalid JSON {path}: {exc}"
        )


def load_optional_json(path):
    if not path.is_file():
        return {}

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8-sig"
            )
        )

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def valid_file(path, minimum_size=1000):
    return (
        path.is_file()
        and path.stat().st_size >= minimum_size
    )


def run_command(command, label):
    log("")
    log("=" * 65)
    log(label)
    log("=" * 65)

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False
    )

    if result.stdout:
        print(result.stdout, flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code "
            f"{result.returncode}"
        )


def probe_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path)
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path}: "
            f"{result.stderr.strip()}"
        )

    try:
        duration = float(result.stdout.strip())
    except Exception:
        raise RuntimeError(
            f"Invalid duration for {path}"
        )

    if duration <= 0:
        raise RuntimeError(
            f"Zero or negative duration: {path}"
        )

    return duration


def file_fingerprint(*paths):
    digest = hashlib.sha256()

    digest.update(
        f"{FORMAT}|{WIDTH}|{HEIGHT}|{FPS}".encode()
    )

    for path in paths:
        digest.update(
            str(path).encode("utf-8")
        )

        if not path.is_file():
            digest.update(b"MISSING")
            continue

        stat = path.stat()

        digest.update(
            f"{stat.st_size}:{stat.st_mtime_ns}".encode()
        )

    return digest.hexdigest()


# ============================================================
# INPUT SCENES
# ============================================================

def load_scenes():
    data = load_json(
        SCENES_FILE
    )

    if isinstance(data, list):
        scenes = data

    elif isinstance(data, dict):
        scenes = data.get(
            "scenes",
            []
        )

        if not isinstance(scenes, list):
            raise RuntimeError(
                "scenes.json 'scenes' must be a list."
            )

        if data.get("status") not in {
            None,
            "completed",
        }:
            raise RuntimeError(
                "scenes.json is not completed. "
                f"Current status: {data.get('status')}"
            )

    else:
        raise RuntimeError(
            "Unsupported scenes.json structure."
        )

    if not scenes:
        raise RuntimeError(
            "No scenes found in scenes.json."
        )

    normalized = []
    seen = set()

    for item in scenes:
        if not isinstance(item, dict):
            raise RuntimeError(
                "Invalid scene entry: expected object."
            )

        try:
            part = int(item["part"])
            scene = int(item["scene"])

        except Exception:
            raise RuntimeError(
                f"Scene has invalid part/scene fields: {item}"
            )

        if part <= 0 or scene <= 0:
            raise RuntimeError(
                f"Invalid scene number: {part}/{scene}"
            )

        key = (part, scene)

        if key in seen:
            raise RuntimeError(
                f"Duplicate scene detected: {key}"
            )

        seen.add(key)

        normalized.append({
            "part": part,
            "scene": scene,
            "source": item
        })

    return sorted(
        normalized,
        key=lambda x: (
            x["part"],
            x["scene"]
        )
    )


# ============================================================
# SOURCE FILE DISCOVERY
# ============================================================

def find_video(part, scene):
    directory = I2V_DIR / f"part_{part:02d}"

    candidates = [
        directory / f"scene_{scene:02d}.mp4",
        directory / f"scene_{scene}.mp4",
        I2V_DIR / f"part_{part:02d}_scene_{scene:02d}.mp4",
        I2V_DIR / f"part_{part}_scene_{scene}.mp4",
    ]

    for path in candidates:
        if valid_file(path):
            return path

    return None


def find_audio(part, scene):
    directory = AUDIO_DIR / f"part_{part:02d}"

    candidates = [
        directory / f"scene_{scene:02d}.mp3",
        directory / f"scene_{scene}.mp3",
        directory / f"scene_{scene:02d}.wav",
        directory / f"scene_{scene}.wav",
        directory / f"scene_{scene:02d}.m4a",
        directory / f"scene_{scene}.m4a",
    ]

    for path in candidates:
        if valid_file(path, 500):
            return path

    return None


# ============================================================
# RENDER MANIFEST
# ============================================================

def load_manifest():
    manifest = load_optional_json(
        MANIFEST_OUT
    )

    if manifest.get("format") != FORMAT:
        manifest = {}

    if not isinstance(
        manifest.get("scenes"),
        dict
    ):
        manifest["scenes"] = {}

    if not isinstance(
        manifest.get("parts"),
        list
    ):
        manifest["parts"] = []

    manifest.update({
        "version": 2,
        "format": FORMAT,
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "status": "in_progress",
        "source": "image_to_video",
    })

    return manifest


def scene_manifest_key(part, scene):
    return (
        f"part_{part:02d}/"
        f"scene_{scene:02d}"
    )


def save_scene_checkpoint(
    manifest,
    part,
    scene,
    output,
    fingerprint,
    status
):
    key = scene_manifest_key(
        part,
        scene
    )

    manifest["scenes"][key] = {
        "part": part,
        "scene": scene,
        "status": status,
        "output": str(output),
        "fingerprint": fingerprint,
        "size": (
            output.stat().st_size
            if output.exists()
            else 0
        ),
    }

    if SAVE_CHECKPOINT:
        atomic_json(
            MANIFEST_OUT,
            manifest
        )


# ============================================================
# RENDER SCENE
# ============================================================

def render_scene(
    part,
    scene,
    video,
    audio,
    output,
    manifest
):
    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    fingerprint = file_fingerprint(
        video,
        audio
    )

    key = scene_manifest_key(
        part,
        scene
    )

    previous = manifest["scenes"].get(
        key,
        {}
    )

    # Reuse only when the old output and both source
    # fingerprints are still valid.
    if (
        RESUME_ENABLED
        and SKIP_COMPLETED
        and valid_file(output)
        and previous.get("status") == "completed"
        and previous.get("fingerprint") == fingerprint
    ):
        log(
            f"REUSE: Part {part} Scene {scene}"
        )

        return output

    audio_duration = probe_duration(
        audio
    )

    video_duration = probe_duration(
        video
    )

    log(
        f"Part {part} Scene {scene} | "
        f"Video {video_duration:.2f}s | "
        f"Audio {audio_duration:.2f}s"
    )

    # The I2V clip is looped only as needed and trimmed
    # to the narration duration.
    video_filter = (
        f"scale={WIDTH}:{HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        "setsar=1,"
        f"fps={FPS},"
        "format=yuv420p"
    )

    temp_output = output.with_name(
        output.stem + ".rendering.mp4"
    )

    if temp_output.exists():
        temp_output.unlink()

    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",

        "-stream_loop",
        "-1",

        "-i",
        str(video),

        "-i",
        str(audio),

        "-filter_complex",
        f"[0:v]{video_filter}[v]",

        "-map",
        "[v]",

        "-map",
        "1:a:0",

        "-t",
        f"{audio_duration:.3f}",

        "-r",
        str(FPS),

        "-c:v",
        "libx264",

        "-preset",
        "medium",

        "-crf",
        "18",

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-ar",
        "48000",

        "-movflags",
        "+faststart",

        str(temp_output)
    ]

    try:
        run_command(
            command,
            f"Rendering Part {part} Scene {scene}"
        )

        if not valid_file(temp_output):
            raise RuntimeError(
                f"Rendered scene is missing or too small: "
                f"{temp_output}"
            )

        probe_duration(
            temp_output
        )

        os.replace(
            temp_output,
            output
        )

    except Exception:
        if temp_output.exists():
            temp_output.unlink()
        raise

    save_scene_checkpoint(
        manifest,
        part,
        scene,
        output,
        fingerprint,
        "completed"
    )

    return output


# ============================================================
# CONCATENATION
# ============================================================

def concat_files(input_files, output):
    if not input_files:
        raise RuntimeError(
            "No input videos provided for concatenation."
        )

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # Use a temporary concat list inside /tmp.
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="lok_ai_concat_",
        encoding="utf-8",
        delete=False
    ) as handle:

        concat_list = Path(
            handle.name
        )

        for path in input_files:
            escaped = str(
                path.resolve()
            ).replace("'", "'\\''")

            handle.write(
                f"file '{escaped}'\n"
            )

    temp_output = output.with_name(
        output.stem + ".joining.mp4"
    )

    if temp_output.exists():
        temp_output.unlink()

    try:
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temp_output)
            ],
            f"Joining {output.name}"
        )

        if not valid_file(temp_output, 10000):
            raise RuntimeError(
                f"Concatenated video invalid: {temp_output}"
            )

        probe_duration(
            temp_output
        )

        os.replace(
            temp_output,
            output
        )

    finally:
        if concat_list.exists():
            concat_list.unlink()

        if temp_output.exists():
            temp_output.unlink()


# ============================================================
# MAIN
# ============================================================

def main():
    log("=" * 70)
    log("KATHA LOK AI — RESUMABLE VIDEO RENDER")
    log("=" * 70)

    log(f"FORMAT          : {FORMAT}")
    log(f"OUTPUT SIZE     : {WIDTH}x{HEIGHT}")
    log(f"FPS             : {FPS}")
    log(f"RESUME          : {RESUME_ENABLED}")
    log(f"SKIP COMPLETED  : {SKIP_COMPLETED}")

    scenes = load_scenes()

    manifest = load_manifest()

    grouped = {}

    for item in scenes:
        grouped.setdefault(
            item["part"],
            []
        ).append(
            item["scene"]
        )

    total_scenes = len(scenes)
    completed_scenes = 0

    log(
        f"Total scenes: {total_scenes}"
    )

    # Do not delete old rendered scenes or part videos.
    # Each valid scene is independently reused.

    for part in sorted(grouped):
        log("")
        log("#" * 65)
        log(f"PART {part}")
        log("#" * 65)

        rendered_scenes = []

        for scene in sorted(grouped[part]):
            video = find_video(
                part,
                scene
            )

            if video is None:
                raise RuntimeError(
                    f"Missing I2V video: "
                    f"Part {part} Scene {scene}. "
                    "Expected output/i2v/part_XX/scene_XX.mp4"
                )

            audio = find_audio(
                part,
                scene
            )

            if audio is None:
                raise RuntimeError(
                    f"Missing narration audio: "
                    f"Part {part} Scene {scene}. "
                    "Expected output/narration/audio/part_XX/"
                    "scene_XX.mp3 or supported audio extension."
                )

            output = (
                SCENES_OUT
                / f"part_{part:02d}"
                / f"scene_{scene:02d}.mp4"
            )

            rendered = render_scene(
                part,
                scene,
                video,
                audio,
                output,
                manifest
            )

            rendered_scenes.append(
                rendered
            )

            completed_scenes += 1

            log(
                f"Progress: {completed_scenes}/{total_scenes}"
            )

        if len(rendered_scenes) != len(grouped[part]):
            raise RuntimeError(
                f"Part {part} scene count mismatch."
            )

        part_output = (
            PARTS_OUT
            / f"part_{part:02d}.mp4"
        )

        concat_files(
            rendered_scenes,
            part_output
        )

        manifest["parts"] = [
            item
            for item in manifest["parts"]
            if not (
                isinstance(item, dict)
                and item.get("part") == part
            )
        ]

        manifest["parts"].append({
            "part": part,
            "scene_count": len(rendered_scenes),
            "output": str(part_output),
            "duration": probe_duration(part_output),
            "status": "completed",
        })

        manifest["parts"].sort(
            key=lambda x: int(x.get("part", 0))
        )

        atomic_json(
            MANIFEST_OUT,
            manifest
        )

    # --------------------------------------------------------
    # FINAL VALIDATION
    # --------------------------------------------------------

    expected_parts = len(grouped)

    actual_part_files = []

    for part in sorted(grouped):
        path = (
            PARTS_OUT
            / f"part_{part:02d}.mp4"
        )

        if not valid_file(path, 10000):
            raise RuntimeError(
                f"Part output missing or invalid: {path}"
            )

        actual_part_files.append(path)

    if len(actual_part_files) != expected_parts:
        raise RuntimeError(
            f"Part count mismatch: "
            f"{len(actual_part_files)}/{expected_parts}"
        )

    # --------------------------------------------------------
    # FORMAT-SPECIFIC FINAL OUTPUT
    # --------------------------------------------------------

    videos_dir = Path(
        "output/videos"
    )

    videos_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    if FORMAT == "short":
        final_output = (
            videos_dir
            / "final_short.mp4"
        )
    else:
        final_output = (
            videos_dir
            / "final_full.mp4"
        )

    concat_files(
        actual_part_files,
        final_output
    )

    if not valid_file(final_output, 10000):
        raise RuntimeError(
            "Final MP4 validation failed."
        )

    final_duration = probe_duration(
        final_output
    )

    manifest.update({
        "status": "completed",
        "format": FORMAT,
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "total_scenes": total_scenes,
        "total_parts": expected_parts,
        "final_output": str(final_output),
        "final_duration": final_duration,
    })

    atomic_json(
        MANIFEST_OUT,
        manifest
    )

    log("")
    log("=" * 70)
    log("VIDEO RENDER COMPLETED")
    log("=" * 70)

    log(f"Format       : {FORMAT}")
    log(f"Scenes       : {total_scenes}")
    log(f"Parts        : {expected_parts}")
    log(f"FPS          : {FPS}")
    log(f"Duration     : {final_duration:.2f}s")
    log(f"Final video  : {final_output}")
    log(f"Manifest     : {MANIFEST_OUT}")

    log("=" * 70)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)

    except Exception as exc:
        log("")
        log(f"RENDER ERROR: {exc}")
        sys.exit(1)