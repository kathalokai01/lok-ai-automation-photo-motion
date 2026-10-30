#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG_FILE = Path("Input/topic.txt")
SCENES_FILE = Path("output/scenes/scenes.json")

I2V_DIR = Path("output/i2v")
AUDIO_DIR = Path("output/narration/audio")
SCENES_OUT = Path("output/scenes")
PARTS_OUT = Path("output/parts")

MANIFEST_OUT = Path("output/video_render_manifest.json")

FPS = 24
VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"
CRF = "18"
PRESET = "medium"


def load_config():
    if not CONFIG_FILE.is_file():
        raise RuntimeError("Input/topic.txt missing.")

    config = {}

    for raw in CONFIG_FILE.read_text(
        encoding="utf-8"
    ).splitlines():

        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)

        value = value.split("#", 1)[0].strip()
        value = value.strip("\"'")

        config[key.strip().upper()] = value

    return config


CONFIG = load_config()

FORMAT = CONFIG.get("FORMAT", "full").lower()

if FORMAT not in {"short", "full"}:
    raise RuntimeError(
        f"Unsupported FORMAT: {FORMAT}"
    )

if FORMAT == "short":
    WIDTH, HEIGHT = 720, 1280
else:
    WIDTH, HEIGHT = 1920, 1080

FPS = int(CONFIG.get("FPS", 24))


def run(command, label):
    print(f"\n{'=' * 60}")
    print(label)
    print(f"{'=' * 60}")

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed: {result.returncode}"
        )


def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "format=duration,size",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FFprobe failed: {path}"
        )

    data = json.loads(result.stdout)

    duration = float(
        data.get("format", {}).get("duration", 0)
    )

    streams = data.get("streams", [])

    video = any(
        s.get("codec_type") == "video"
        for s in streams
    )

    audio = any(
        s.get("codec_type") == "audio"
        for s in streams
    )

    if duration <= 0 or not video:
        raise RuntimeError(
            f"Invalid video: {path}"
        )

    return {
        "duration": duration,
        "video": video,
        "audio": audio,
        "streams": streams,
    }


def valid_file(path, minimum_size=1000):
    return (
        path.is_file()
        and path.stat().st_size >= minimum_size
    )


def atomic_replace(temp, destination):
    if not valid_file(temp):
        raise RuntimeError(
            f"Temporary output invalid: {temp}"
        )

    probe(temp)

    os.replace(temp, destination)


def find_i2v_clip(part, scene):
    directory = I2V_DIR / f"part_{part:02d}"

    candidates = [
        directory / f"scene_{scene:02d}.mp4",
        directory / f"scene_{scene}.mp4",
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
    ]

    for path in candidates:
        if valid_file(path):
            return path

    return None


def natural_sort(path):
    numbers = re.findall(r"\d+", path.stem)

    return tuple(
        int(number)
        for number in numbers
    )


def load_scenes():
    if not SCENES_FILE.is_file():
        raise RuntimeError(
            "output/scenes/scenes.json missing."
        )

    data = json.loads(
        SCENES_FILE.read_text(
            encoding="utf-8"
        )
    )

    if data.get("status") != "completed":
        raise RuntimeError(
            "Scene generation is not completed."
        )

    scenes = data.get("scenes")

    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError(
            "No valid scenes found."
        )

    seen = set()

    for item in scenes:
        part = int(item.get("part", 0))
        scene = int(item.get("scene", 0))

        if part < 1 or scene < 1:
            raise RuntimeError(
                f"Invalid scene reference: {item}"
            )

        key = (part, scene)

        if key in seen:
            raise RuntimeError(
                f"Duplicate scene: {key}"
            )

        seen.add(key)

    return scenes


def valid_rendered_scene(path, audio_path):
    if not valid_file(path, 10000):
        return False

    try:
        rendered = probe(path)
        audio = probe(audio_path)

        if not rendered["audio"]:
            return False

        difference = abs(
            rendered["duration"]
            - audio["duration"]
        )

        return difference <= 1.5

    except Exception:
        return False


def render_scene(
    part,
    scene,
    video_path,
    audio_path,
    output_path,
):
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if valid_rendered_scene(
        output_path,
        audio_path,
    ):
        print(
            f"REUSING: Part {part}, Scene {scene}"
        )
        return

    audio_info = probe(audio_path)

    audio_duration = audio_info["duration"]

    temp = output_path.with_name(
        output_path.stem + ".tmp.mp4"
    )

    if temp.exists():
        temp.unlink()

    video_filter = (
        f"scale={WIDTH}:{HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        "setsar=1,"
        "fps=24,"
        "format=yuv420p"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-stream_loop", "-1",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", video_filter,
        "-t", f"{audio_duration:.3f}",
        "-r", str(FPS),
        "-c:v", VIDEO_CODEC,
        "-preset", PRESET,
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-c:a", AUDIO_CODEC,
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        str(temp),
    ]

    try:
        run(
            command,
            f"Render Part {part} Scene {scene}",
        )

        atomic_replace(
            temp,
            output_path,
        )

        print(
            f"SUCCESS: Part {part} Scene {scene}"
        )

    finally:
        if temp.exists():
            temp.unlink()


def concat_part(part, scene_files, output_path):
    if not scene_files:
        raise RuntimeError(
            f"Part {part} has no rendered scenes."
        )

    concat_file = Path(
        f"/tmp/lok_ai_part_{part:02d}.txt"
    )

    temp = output_path.with_name(
        output_path.stem + ".tmp.mp4"
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with concat_file.open(
        "w",
        encoding="utf-8",
    ) as file:

        for scene_file in scene_files:
            if not valid_file(scene_file, 10000):
                raise RuntimeError(
                    f"Invalid scene video: {scene_file}"
                )

            file.write(
                "file '{}'\n".format(
                    str(scene_file.resolve()).replace(
                        "'", "'\\''"
                    )
                )
            )

    if temp.exists():
        temp.unlink()

    try:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-c:v", "libx264",
                "-preset", PRESET,
                "-crf", CRF,
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(temp),
            ],
            f"Join Part {part}",
        )

        atomic_replace(
            temp,
            output_path,
        )

    finally:
        if temp.exists():
            temp.unlink()


def save_manifest(manifest):
    MANIFEST_OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = MANIFEST_OUT.with_suffix(
        ".tmp.json"
    )

    temp.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    os.replace(
        temp,
        MANIFEST_OUT,
    )


def main():
    print("=" * 65)
    print("KATHA LOK AI VIDEO RENDERER")
    print("=" * 65)

    print(f"FORMAT: {FORMAT}")
    print(f"RESOLUTION: {WIDTH}x{HEIGHT}")
    print(f"FPS: {FPS}")

    scenes = load_scenes()

    parts = {}

    for item in scenes:
        part = int(item["part"])
        scene = int(item["scene"])

        parts.setdefault(
            part,
            [],
        ).append(scene)

    for part in parts:
        parts[part].sort()

    expected_parts = sorted(parts)

    manifest = {
        "status": "rendering",
        "format": FORMAT,
        "width": WIDTH,
        "height": HEIGHT,
        "source": "image_to_video",
        "parts": [],
    }

    save_manifest(manifest)

    SCENES_OUT.mkdir(
        parents=True,
        exist_ok=True,
    )

    PARTS_OUT.mkdir(
        parents=True,
        exist_ok=True,
    )

    total_scenes = 0

    for part in expected_parts:
        print("\n" + "#" * 60)
        print(f"PART {part}")
        print("#" * 60)

        scene_outputs = []

        for scene in parts[part]:
            i2v = find_i2v_clip(
                part,
                scene,
            )

            if i2v is None:
                raise RuntimeError(
                    f"Missing I2V clip: "
                    f"Part {part}, Scene {scene}"
                )

            audio = find_audio(
                part,
                scene,
            )

            if audio is None:
                raise RuntimeError(
                    f"Missing narration audio: "
                    f"Part {part}, Scene {scene}"
                )

            output = (
                SCENES_OUT
                / f"part_{part:02d}"
                / f"scene_{scene:02d}.mp4"
            )

            render_scene(
                part,
                scene,
                i2v,
                audio,
                output,
            )

            scene_outputs.append(output)
            total_scenes += 1

        part_output = (
            PARTS_OUT
            / f"part_{part:02d}.mp4"
        )

        # Reuse a valid part only when every
        # corresponding scene is already valid.
        reusable_part = False

        if valid_file(part_output, 10000):
            try:
                part_info = probe(part_output)

                scene_duration = sum(
                    probe(path)["duration"]
                    for path in scene_outputs
                )

                reusable_part = (
                    part_info["audio"]
                    and abs(
                        part_info["duration"]
                        - scene_duration
                    ) <= 2.0
                )

            except Exception:
                reusable_part = False

        if reusable_part:
            print(
                f"REUSING existing Part {part} video."
            )
        else:
            concat_part(
                part,
                scene_outputs,
                part_output,
            )

        info = probe(part_output)

        manifest["parts"] = [
            item
            for item in manifest["parts"]
            if item["part"] != part
        ]

        manifest["parts"].append(
            {
                "part": part,
                "scenes": len(scene_outputs),
                "output": str(part_output),
                "duration": info["duration"],
            }
        )

        manifest["parts"].sort(
            key=lambda item: item["part"]
        )

        save_manifest(manifest)

    actual_parts = len(
        list(PARTS_OUT.glob("part_*.mp4"))
    )

    if actual_parts != len(expected_parts):
        raise RuntimeError(
            f"Part count mismatch: "
            f"{actual_parts}/{len(expected_parts)}"
        )

    for item in manifest["parts"]:
        path = Path(item["output"])

        if not valid_file(path, 10000):
            raise RuntimeError(
                f"Final validation failed: {path}"
            )

        probe(path)

    manifest["status"] = "completed"

    save_manifest(manifest)

    print("\n" + "=" * 65)
    print("VIDEO RENDERING COMPLETED")
    print("=" * 65)

    print(f"Format: {FORMAT}")
    print(f"Resolution: {WIDTH}x{HEIGHT}")
    print(f"Total scenes: {total_scenes}")
    print(f"Total parts: {actual_parts}")

    for item in manifest["parts"]:
        print(
            f"Part {item['part']}: "
            f"{item['duration']:.2f}s | "
            f"{item['output']}"
        )

    print(f"Manifest: {MANIFEST_OUT}")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)

    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)