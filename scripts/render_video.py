#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from input_config import load_input_config, normalize_format


CONFIG_FILE = Path("Input/topic.txt")
SCENES_FILE = Path("output/scenes/scenes.json")

I2V_DIR = Path("output/i2v")
AUDIO_DIR = Path("output/narration/audio")
SCENES_OUT = Path("output/scenes/rendered")
PARTS_OUT = Path("output/parts")
VIDEOS_OUT = Path("output/videos")

MANIFEST_OUT = Path("output/video_render_manifest.json")


def run(command, label):
    print(f"\n{'=' * 65}\n{label}\n{'=' * 65}")

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}"
        )


def probe_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot read media duration: {path}"
        )

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid media duration: {path}"
        ) from exc

    if duration <= 0:
        raise RuntimeError(f"Invalid duration: {path}")

    return duration


def valid_media(path, minimum_size=1000):
    if not path.is_file():
        return False

    if path.stat().st_size < minimum_size:
        return False

    try:
        probe_duration(path)
        return True
    except Exception:
        return False


def natural_sort(path):
    match = re.search(r"(\d+)", path.stem)
    return int(match.group(1)) if match else 999999


def find_existing(directory, names):
    for name in names:
        path = directory / name
        if valid_media(path):
            return path
    return None


def find_i2v_clip(part, scene):
    directory = I2V_DIR / f"part_{part:02d}"

    return find_existing(
        directory,
        [
            f"scene_{scene:02d}.mp4",
            f"scene_{scene}.mp4",
        ],
    )


def find_audio(part, scene):
    directory = AUDIO_DIR / f"part_{part:02d}"

    return find_existing(
        directory,
        [
            f"scene_{scene:02d}.mp3",
            f"scene_{scene}.mp3",
            f"scene_{scene:02d}.wav",
            f"scene_{scene}.wav",
        ],
    )


def load_project():
    if not CONFIG_FILE.is_file():
        raise RuntimeError("Input/topic.txt not found.")

    config = load_input_config()
    format_name = normalize_format(config)

    if format_name not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {format_name}"
        )

    if not SCENES_FILE.is_file():
        raise RuntimeError(
            "output/scenes/scenes.json not found."
        )

    data = json.loads(
        SCENES_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise RuntimeError("scenes.json must contain a JSON object.")

    if data.get("status") != "completed":
        raise RuntimeError(
            "Scene generation is not marked completed."
        )

    scenes = data.get("scenes")

    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("No valid scenes found.")

    normalized = []
    seen = set()

    for index, item in enumerate(scenes, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"Scene {index} is not a JSON object."
            )

        try:
            part = int(item["part"])
            scene = int(item["scene"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                f"Scene {index} has invalid part/scene numbers."
            )

        if part < 1 or scene < 1:
            raise RuntimeError(
                f"Invalid scene coordinates: part={part}, scene={scene}"
            )

        key = (part, scene)

        if key in seen:
            raise RuntimeError(
                f"Duplicate scene entry: part={part}, scene={scene}"
            )

        seen.add(key)
        normalized.append(item)

    normalized.sort(
        key=lambda item: (
            int(item["part"]),
            int(item["scene"]),
        )
    )

    return config, format_name, normalized


def output_geometry(format_name):
    if format_name == "short":
        return 720, 1280
    return 1920, 1080


def render_scene(
    part,
    scene,
    source_video,
    source_audio,
    output_path,
    width,
    height,
    fps,
):
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    audio_duration = probe_duration(source_audio)

    filter_graph = (
        f"[0:v]"
        f"scale={width}:{height}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"setsar=1,"
        f"fps={fps},"
        f"format=yuv420p"
        f"[v]"
    )

    temporary = output_path.with_name(
        output_path.stem + ".tmp.mp4"
    )

    command = [
        "ffmpeg",
        "-y",
        "-stream_loop", "-1",
        "-i", str(source_video),
        "-i", str(source_audio),
        "-filter_complex", filter_graph,
        "-map", "[v]",
        "-map", "1:a:0",
        "-t", f"{audio_duration:.3f}",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-shortest",
        "-movflags", "+faststart",
        str(temporary),
    ]

    try:
        run(
            command,
            f"Render part {part}, scene {scene}",
        )

        if not valid_media(temporary):
            raise RuntimeError(
                f"Rendered scene is invalid: {temporary}"
            )

        os.replace(temporary, output_path)

    finally:
        if temporary.exists():
            temporary.unlink()


def concat_videos(files, output_path, label):
    if not files:
        raise RuntimeError(
            f"No input videos supplied for {label}."
        )

    for path in files:
        if not valid_media(path):
            raise RuntimeError(
                f"Invalid concat input: {path}"
            )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    concat_file = output_path.with_suffix(".concat.txt")
    temporary = output_path.with_name(
        output_path.stem + ".tmp.mp4"
    )

    with concat_file.open(
        "w",
        encoding="utf-8",
    ) as handle:
        for path in files:
            escaped = str(path.resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")

    try:
        run(
            [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                "-movflags", "+faststart",
                str(temporary),
            ],
            label,
        )

        if not valid_media(temporary, minimum_size=10000):
            raise RuntimeError(
                f"Concat output is invalid: {temporary}"
            )

        os.replace(temporary, output_path)

    finally:
        if concat_file.exists():
            concat_file.unlink()

        if temporary.exists():
            temporary.unlink()


def load_previous_manifest():
    if not MANIFEST_OUT.is_file():
        return {}

    try:
        data = json.loads(
            MANIFEST_OUT.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_manifest(manifest):
    MANIFEST_OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = MANIFEST_OUT.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    os.replace(temporary, MANIFEST_OUT)


def main():
    config, format_name, scenes = load_project()

    width, height = output_geometry(format_name)

    try:
        fps = int(config.get("FPS", 24))
    except (TypeError, ValueError):
        fps = 24

    if fps < 1 or fps > 60:
        raise RuntimeError(
            f"FPS must be between 1 and 60, got {fps}."
        )

    resume = str(
        config.get("RESUME_ENABLED", True)
    ).strip().lower() not in {
        "false", "0", "no", "off"
    }

    skip_completed = str(
        config.get("SKIP_COMPLETED_SCENES", True)
    ).strip().lower() not in {
        "false", "0", "no", "off"
    }

    SCENES_OUT.mkdir(parents=True, exist_ok=True)
    PARTS_OUT.mkdir(parents=True, exist_ok=True)
    VIDEOS_OUT.mkdir(parents=True, exist_ok=True)

    previous = load_previous_manifest()

    same_project = (
        previous.get("format") == format_name
        and previous.get("width") == width
        and previous.get("height") == height
        and previous.get("fps") == fps
    )

    previous_scenes = {}

    if resume and same_project:
        for item in previous.get("scenes", []):
            if isinstance(item, dict):
                key = (
                    item.get("part"),
                    item.get("scene"),
                )
                previous_scenes[key] = item

    grouped = {}

    for item in scenes:
        part = int(item["part"])
        scene = int(item["scene"])
        grouped.setdefault(part, []).append(scene)

    manifest = {
        "status": "rendering",
        "format": format_name,
        "width": width,
        "height": height,
        "fps": fps,
        "source": "image_to_video",
        "expected_scenes": len(scenes),
        "completed_scenes": 0,
        "parts": [],
        "scenes": [],
        "final_video": None,
    }

    rendered_parts = []

    print(f"FORMAT: {format_name}")
    print(f"SIZE: {width}x{height}")
    print(f"FPS: {fps}")
    print(f"SCENES: {len(scenes)}")
    print(f"RESUME: {resume}")

    for part in sorted(grouped):
        scene_outputs = []

        print(f"\n========== PART {part} ==========")

        for scene in sorted(grouped[part]):
            source_video = find_i2v_clip(part, scene)
            source_audio = find_audio(part, scene)

            if source_video is None:
                raise RuntimeError(
                    f"Missing AI video: part {part}, scene {scene}. "
                    "Expected output/i2v/part_XX/scene_XX.mp4"
                )

            if source_audio is None:
                raise RuntimeError(
                    f"Missing narration audio: part {part}, scene {scene}. "
                    "Expected output/narration/audio/part_XX/"
                )

            output_path = (
                SCENES_OUT
                / f"part_{part:02d}"
                / f"scene_{scene:02d}.mp4"
            )

            old = previous_scenes.get((part, scene), {})

            reusable = (
                resume
                and skip_completed
                and old.get("status") == "completed"
                and old.get("source_video") == str(source_video)
                and old.get("source_audio") == str(source_audio)
                and valid_media(output_path)
            )

            if reusable:
                print(
                    f"REUSE: Part {part}, scene {scene}"
                )
            else:
                render_scene(
                    part,
                    scene,
                    source_video,
                    source_audio,
                    output_path,
                    width,
                    height,
                    fps,
                )

            duration = probe_duration(output_path)

            scene_outputs.append(output_path)

            manifest["scenes"].append({
                "part": part,
                "scene": scene,
                "status": "completed",
                "source_video": str(source_video),
                "source_audio": str(source_audio),
                "output": str(output_path),
                "duration": round(duration, 3),
                "reused": reusable,
            })

            manifest["completed_scenes"] += 1

            # Persist progress after every successful scene.
            save_manifest(manifest)

        expected_in_part = len(grouped[part])

        if len(scene_outputs) != expected_in_part:
            raise RuntimeError(
                f"Part {part} scene count mismatch: "
                f"{len(scene_outputs)}/{expected_in_part}"
            )

        part_output = PARTS_OUT / f"part_{part:02d}.mp4"

        # Rebuild part only when its current output is missing.
        # Scene outputs are already individually checkpointed.
        if not valid_media(part_output):
            concat_videos(
                scene_outputs,
                part_output,
                f"Create part {part}",
            )
        else:
            print(f"REUSE: {part_output}")

        rendered_parts.append(part_output)

        manifest["parts"].append({
            "part": part,
            "scenes": len(scene_outputs),
            "output": str(part_output),
            "duration": round(
                probe_duration(part_output),
                3,
            ),
        })

        save_manifest(manifest)

    if len(manifest["scenes"]) != len(scenes):
        raise RuntimeError(
            "Final scene count does not match scenes.json."
        )

    if format_name == "full":
        final_video = VIDEOS_OUT / "final_full.mp4"

        concat_videos(
            rendered_parts,
            final_video,
            "Create final full video",
        )

        manifest["final_video"] = str(final_video)

    else:
        # Short format intentionally remains separate per part.
        # Do not create an additional full-length video.
        manifest["final_video"] = None

    manifest["status"] = "completed"
    manifest["completed_scenes"] = len(scenes)

    save_manifest(manifest)

    print("\n========== RENDER COMPLETE ==========")
    print(f"Format: {format_name}")
    print(f"Scenes: {len(scenes)}")
    print(f"Parts: {len(rendered_parts)}")

    for path in rendered_parts:
        print(
            f"{path} | "
            f"{probe_duration(path):.2f}s | "
            f"{path.stat().st_size / (1024 * 1024):.2f} MB"
        )

    if manifest["final_video"]:
        print(f"FINAL VIDEO: {manifest['final_video']}")

    print(f"MANIFEST: {MANIFEST_OUT}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)