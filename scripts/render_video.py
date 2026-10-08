#!/usr/bin/env python3

import json
import os
import re
import shutil
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


# ============================================================
# CONFIG
# ============================================================

def clean_config_value(value: str) -> str:
    value = value.strip()

    if "#" in value:
        value = value.split("#", 1)[0].strip()

    value = value.strip().strip('"').strip("'")

    return value.strip()


def load_config():
    config = {}

    if not CONFIG_FILE.exists():
        raise RuntimeError("Input/topic.txt not found.")

    for raw in CONFIG_FILE.read_text(
        encoding="utf-8"
    ).splitlines():

        line = raw.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)

        key = key.strip().upper()
        value = clean_config_value(value)

        config[key] = value

    return config


CONFIG = load_config()

FORMAT = CONFIG.get("FORMAT", "full").lower()

if FORMAT not in {"short", "full"}:
    raise RuntimeError(
        f"Invalid FORMAT: {FORMAT}. "
        f"Expected short or full."
    )


# ============================================================
# FORMAT
# ============================================================

def get_output_geometry():

    if FORMAT == "short":
        return 720, 1280

    return 1920, 1080


WIDTH, HEIGHT = get_output_geometry()


# ============================================================
# HELPERS
# ============================================================

def run(cmd, label):

    print()
    print("=" * 70)
    print(label)
    print("=" * 70)

    print(" ".join(str(x) for x in cmd))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code "
            f"{result.returncode}"
        )


def probe_duration(path: Path) -> float:

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Unable to read duration: {path}"
        )

    try:
        return float(result.stdout.strip())
    except Exception:
        raise RuntimeError(
            f"Invalid duration returned for: {path}"
        )


def find_i2v_clip(part: int, scene: int):

    directory = I2V_DIR / f"part_{part:02d}"

    candidates = [
        directory / f"scene_{scene:02d}.mp4",
        directory / f"scene_{scene}.mp4",
    ]

    for path in candidates:
        if path.exists() and path.stat().st_size > 1000:
            return path

    return None


def find_audio(part: int, scene: int):

    directory = AUDIO_DIR / f"part_{part:02d}"

    candidates = [
        directory / f"scene_{scene:02d}.mp3",
        directory / f"scene_{scene}.mp3",
    ]

    for path in candidates:
        if path.exists() and path.stat().st_size > 1000:
            return path

    return None


def natural_part_sort(path: Path):

    match = re.search(
        r"(\d+)",
        path.stem
    )

    return int(match.group(1)) if match else 999999


# ============================================================
# VALIDATE SCENES
# ============================================================

def load_scenes():

    if not SCENES_FILE.exists():
        raise RuntimeError(
            "output/scenes/scenes.json not found."
        )

    data = json.loads(
        SCENES_FILE.read_text(
            encoding="utf-8"
        )
    )

    if data.get("status") != "completed":
        raise RuntimeError(
            "scenes.json is not marked completed."
        )

    scenes = data.get("scenes", [])

    if not scenes:
        raise RuntimeError(
            "No scenes found."
        )

    return scenes


# ============================================================
# RENDER ONE SCENE
# ============================================================

def render_scene(
    part: int,
    scene: int,
    i2v_path: Path,
    audio_path: Path,
    output_path: Path,
):

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    audio_duration = probe_duration(
        audio_path
    )

    video_duration = probe_duration(
        i2v_path
    )

    print()
    print(
        f"Part {part} Scene {scene}"
    )
    print(
        f"I2V duration   : {video_duration:.2f}s"
    )
    print(
        f"Audio duration : {audio_duration:.2f}s"
    )

    # --------------------------------------------------------
    # If AI video is shorter than narration:
    # loop the I2V clip naturally until narration finishes.
    #
    # If AI video is longer:
    # trim it to narration length.
    # --------------------------------------------------------

    filter_complex = (
        f"[0:v]"
        f"scale={WIDTH}:{HEIGHT}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        f"setsar=1,"
        f"format=yuv420p"
        f"[v]"
    )

    cmd = [
        "ffmpeg",
        "-y",

        # Loop I2V only when needed.
        "-stream_loop",
        "-1",

        "-i",
        str(i2v_path),

        "-i",
        str(audio_path),

        "-filter_complex",
        filter_complex,

        "-map",
        "[v]",

        "-map",
        "1:a:0",

        "-t",
        f"{audio_duration:.3f}",

        "-r",
        "24",

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

        str(output_path),
    ]

    run(
        cmd,
        f"Rendering Part {part} Scene {scene}"
    )

    if not output_path.exists():
        raise RuntimeError(
            f"Scene output missing: {output_path}"
        )

    if output_path.stat().st_size < 1000:
        raise RuntimeError(
            f"Scene output is too small: "
            f"{output_path}"
        )


# ============================================================
# CONCAT PART
# ============================================================

def concat_part(
    part: int,
    scene_files,
    output_path: Path,
):

    concat_file = Path(
        f"/tmp/lok_ai_part_{part:02d}.txt"
    )

    with concat_file.open(
        "w",
        encoding="utf-8"
    ) as f:

        for scene_file in scene_files:

            f.write(
                f"file '{scene_file.resolve()}'\n"
            )

    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        f"Creating Part {part} video"
    )

    if not output_path.exists():
        raise RuntimeError(
            f"Part video missing: {output_path}"
        )

    if output_path.stat().st_size < 10000:
        raise RuntimeError(
            f"Part video is too small: {output_path}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 70)
    print("        REAL AI IMAGE-TO-VIDEO RENDER")
    print("=" * 70)

    print()
    print(f"FORMAT : {FORMAT}")
    print(
        f"OUTPUT : {WIDTH}x{HEIGHT}"
    )

    print()
    print(
        "IMPORTANT: Rendering from I2V clips."
    )
    print(
        "Still images are NOT used as video sources."
    )

    scenes = load_scenes()

    print()
    print(
        f"Total scenes: {len(scenes)}"
    )

    # --------------------------------------------------------
    # Prepare directories
    # --------------------------------------------------------

    SCENES_OUT.mkdir(
        parents=True,
        exist_ok=True
    )

    PARTS_OUT.mkdir(
        parents=True,
        exist_ok=True
    )

    # Remove stale rendered scene videos.
    if SCENES_OUT.exists():

        for old in SCENES_OUT.glob(
            "part_*/scene_*.mp4"
        ):

            try:
                old.unlink()
            except Exception:
                pass

    # Remove stale part videos.
    for old in PARTS_OUT.glob(
        "part_*.mp4"
    ):

        try:
            old.unlink()
        except Exception:
            pass

    # --------------------------------------------------------
    # Group scenes by part
    # --------------------------------------------------------

    parts = {}

    for item in scenes:

        part = int(
            item.get("part", 0)
        )

        scene = int(
            item.get("scene", 0)
        )

        if part <= 0 or scene <= 0:
            raise RuntimeError(
                f"Invalid scene reference: {item}"
            )

        parts.setdefault(
            part,
            []
        ).append(
            scene
        )

    manifest = {
        "status": "rendering",
        "format": FORMAT,
        "width": WIDTH,
        "height": HEIGHT,
        "source": "image_to_video",
        "parts": [],
    }

    # --------------------------------------------------------
    # Render every scene
    # --------------------------------------------------------

    for part in sorted(parts):

        print()
        print(
            "#" * 70
        )
        print(
            f"# PART {part}"
        )
        print(
            "#" * 70
        )

        scene_outputs = []

        for scene in sorted(
            parts[part]
        ):

            i2v = find_i2v_clip(
                part,
                scene
            )

            if i2v is None:
                raise RuntimeError(
                    f"Missing I2V video for "
                    f"Part {part} Scene {scene}"
                )

            audio = find_audio(
                part,
                scene
            )

            if audio is None:
                raise RuntimeError(
                    f"Missing narration audio for "
                    f"Part {part} Scene {scene}"
                )

            scene_output_dir = (
                SCENES_OUT /
                f"part_{part:02d}"
            )

            scene_output = (
                scene_output_dir /
                f"scene_{scene:02d}.mp4"
            )

            render_scene(
                part=part,
                scene=scene,
                i2v_path=i2v,
                audio_path=audio,
                output_path=scene_output,
            )

            scene_outputs.append(
                scene_output
            )

        # ----------------------------------------------------
        # Validate scene count
        # ----------------------------------------------------

        if len(scene_outputs) != len(
            parts[part]
        ):

            raise RuntimeError(
                f"Part {part} scene count mismatch."
            )

        # ----------------------------------------------------
        # Create part video
        # ----------------------------------------------------

        part_output = (
            PARTS_OUT /
            f"part_{part:02d}.mp4"
        )

        concat_part(
            part=part,
            scene_files=scene_outputs,
            output_path=part_output,
        )

        manifest["parts"].append(
            {
                "part": part,
                "scenes": len(scene_outputs),
                "output": str(
                    part_output
                ),
                "duration": probe_duration(
                    part_output
                ),
            }
        )

    # --------------------------------------------------------
    # Final validation
    # --------------------------------------------------------

    expected_parts = len(parts)

    actual_parts = len(
        list(
            PARTS_OUT.glob("part_*.mp4")
        )
    )

    if actual_parts != expected_parts:

        raise RuntimeError(
            f"Part video count mismatch: "
            f"{actual_parts}/{expected_parts}"
        )

    manifest["status"] = "completed"

    MANIFEST_OUT.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("              RENDER COMPLETE")
    print("=" * 70)

    print()
    print(
        f"Format          : {FORMAT}"
    )

    print(
        f"Resolution      : {WIDTH}x{HEIGHT}"
    )

    print(
        f"Parts           : {actual_parts}"
    )

    print(
        f"Scenes          : {len(scenes)}"
    )

    print()
    print(
        "Source: REAL AI IMAGE-TO-VIDEO clips"
    )

    print(
        "Audio: Hindi scene narration"
    )

    print()
    print("Generated parts:")

    for part_file in sorted(
        PARTS_OUT.glob("part_*.mp4"),
        key=natural_part_sort
    ):

        duration = probe_duration(
            part_file
        )

        size_mb = (
            part_file.stat().st_size /
            (1024 * 1024)
        )

        print(
            f"  {part_file} | "
            f"{duration:.2f}s | "
            f"{size_mb:.2f} MB"
        )

    print()
    print(
        f"Manifest: {MANIFEST_OUT}"
    )

    print()
    print("=" * 70)


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "Interrupted."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print(
            f"ERROR: {exc}"
        )

        sys.exit(1)
