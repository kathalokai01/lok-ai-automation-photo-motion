#!/usr/bin/env python3
"""Burn captions scene-by-scene, write SRT, then assemble parts and final video."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts

OUTPUT = ROOT / "output"
MOTION_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
VISUAL_JOBS = OUTPUT / "visuals" / "visual_jobs.json"
CAPTIONS_JSON = OUTPUT / "captions" / "captions.json"
CAPTION_DIR = OUTPUT / "captions"
CAPTIONED_DIR = OUTPUT / "scene_renders_captioned"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"

FONT_NAME = "Noto Sans Devanagari"
FONT_SIZE = 14
OUTLINE_SIZE = 2
BOTTOM_MARGIN = 80
MAX_LINE_CHARS = 30
MAX_LINES = 2
MIN_VIDEO_BYTES = 1000

PALETTE = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "yellow": (255, 225, 0),
    "cyan": (0, 240, 255),
    "lime": (130, 255, 0),
    "orange": (255, 145, 0),
    "magenta": (255, 50, 210),
    "blue": (35, 95, 255),
    "violet": (190, 110, 255),
    "pink": (255, 115, 160),
    "green": (0, 220, 120),
}
FALLBACK_RGB = (45, 45, 45)


def log(*args):
    print(*args, flush=True)


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} is outside the repository.")
    return path


def run(command):
    log("$", " ".join(map(str, command)))
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.stdout:
        log(result.stdout.rstrip())
    if result.returncode:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}."
        )


def media_info(path):
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        raise RuntimeError(f"Missing or too-small video: {path}")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Cannot inspect {path.name}.") from exc

    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if not video or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video or duration: {path}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(
            stream.get("codec_type") == "audio" for stream in streams
        ),
    }


def rows_from_json(data, label, keys):
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next(
            (
                data.get(key)
                for key in keys
                if isinstance(data.get(key), list)
            ),
            None,
        )
    else:
        rows = None

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{label} contains no entries.")
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{label} contains an invalid entry.")
    return rows


def positive_int(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc
    if number < 1:
        raise RuntimeError(f"{label} must be positive.")
    return number


def scene_identity(row, index, label):
    part = positive_int(row.get("part", 1), f"{label} part")
    local = positive_int(
        row.get("local_scene", row.get("scene", index)),
        f"{label} local scene",
    )
    global_scene = positive_int(
        row.get("global_scene", row.get("scene", index)),
        f"{label} global scene",
    )
    return part, local, global_scene


def clean_text(value):
    return " ".join(
        str(value or "").replace("\r", " ").replace("\n", " ").split()
    ).strip()


def load_caption_map():
    data = read_json(CAPTIONS_JSON)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Captions are not marked completed.")

    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("captions.json contains no caption records.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption record.")

        part = positive_int(row.get("part"), "caption part")
        local = positive_int(row.get("scene"), "caption scene")
        caption = clean_text(row.get("text"))

        if not caption:
            raise RuntimeError(
                f"Empty caption at Part {part}, Scene {local}."
            )

        key = (part, local)
        if key in result:
            raise RuntimeError(
                f"Duplicate caption at Part {part}, Scene {local}."
            )
        result[key] = caption

    return result


def wrap_words(text):
    lines = []
    current = ""

    for word in clean_text(text).split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > MAX_LINE_CHARS:
            lines.append(current)
            current = word
        else:
            current = candidate

    if current:
        lines.append(current)

    return [
        r"\N".join(lines[index:index + MAX_LINES])
        for index in range(0, len(lines), MAX_LINES)
        if lines[index:index + MAX_LINES]
    ]


def ass_time(seconds):
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centiseconds:02}"


def srt_time(seconds):
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return (
        f"{hours:02}:{minutes:02}:{secs:02},"
        f"{milliseconds:03}"
    )


def linear_channel(value):
    value = value / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (linear_channel(value) for value in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(first, second):
    high = max(luminance(first), luminance(second))
    low = min(luminance(first), luminance(second))
    return (high + 0.05) / (low + 0.05)


def sample_background_rgb(ffmpeg, video, seconds):
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, seconds):.3f}",
        "-i", str(video),
        "-vf",
        "crop=iw:ih*0.30:0:ih*0.65,"
        "scale=1:1:flags=area,format=rgb24",
        "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode == 0 and len(result.stdout) >= 3:
            return tuple(result.stdout[:3])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return FALLBACK_RGB


def choose_colour(background):
    name, rgb = max(
        PALETTE.items(),
        key=lambda item: contrast_ratio(item[1], background),
    )
    black = (0, 0, 0)
    white = (255, 255, 255)
    outline = (
        black
        if contrast_ratio(rgb, black) >= contrast_ratio(rgb, white)
        else white
    )
    return name, rgb, outline, contrast_ratio(rgb, background)


def ass_colour(rgb):
    red, green, blue = rgb
    return f"&H00{blue:02X}{green:02X}{red:02X}&"


def ass_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def escape_filter_path(path):
    value = str(path.resolve())
    for old, new in (
        ("\\", "\\\\"),
        (":", "\\:"),
        ("'", "\\'"),
        (",", "\\,"),
        ("[", "\\["),
        ("]", "\\]"),
    ):
        value = value.replace(old, new)
    return value


def make_scene_ass(ffmpeg, scene, destination):
    duration = scene["duration"]
    cues = wrap_words(scene["text"])
    if not cues:
        raise RuntimeError(
            f"No caption text for scene {scene['global_scene']}."
        )

    weights = [
        max(1, len(cue.replace(r"\N", " ").split()))
        for cue in cues
    ]
    total_weight = sum(weights)
    cursor = 0.0
    dialogue = []

    for index, (cue, weight) in enumerate(zip(cues, weights)):
        end = (
            duration
            if index == len(cues) - 1
            else cursor + duration * weight / total_weight
        )
        midpoint = (cursor + end) / 2
        background = sample_background_rgb(
            ffmpeg, scene["path"], midpoint
        )
        name, rgb, outline, ratio = choose_colour(background)

        override = (
            f"{{\\1c{ass_colour(rgb)}"
            f"\\3c{ass_colour(outline)}"
            f"\\bord{OUTLINE_SIZE}}}"
        )
        dialogue.append(
            f"Dialogue: 0,{ass_time(cursor)},{ass_time(end)},Default,"
            f",0,0,0,,{override}{ass_escape(cue)}\n"
        )
        log(
            f"Scene {scene['global_scene']} caption colour: "
            f"{name}, contrast={ratio:.2f}:1"
        )
        cursor = end

    header = f"""[Script Info]
Title: Scene {scene['global_scene']} Captions
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {scene['width']}
PlayResY: {scene['height']}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{FONT_SIZE},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{OUTLINE_SIZE},0,2,45,45,{BOTTOM_MARGIN},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header + "".join(dialogue), encoding="utf-8")


def render_captioned_scene(ffmpeg, scene, index, total):
    source = scene["path"]
    destination = CAPTIONED_DIR / f"scene_{index:04d}.mp4"
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    ass_path = CAPTION_DIR / "scene_ass" / f"scene_{index:04d}.ass"

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    make_scene_ass(ffmpeg, scene, ass_path)

    run([
        ffmpeg, "-hide_banner", "-y",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", f"ass=filename='{escape_filter_path(ass_path)}'",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(temporary),
    ])

    info = media_info(temporary)
    if not info["has_audio"]:
        raise RuntimeError(
            f"Captioned scene {index} has no audio. "
            "Check render_video.py first."
        )
    if abs(info["duration"] - scene["duration"]) > 1.0:
        raise RuntimeError(
            f"Caption burn changed scene {index} duration unexpectedly."
        )

    temporary.replace(destination)
    log(f"CAPTIONED SCENE {index}/{total}: {destination.relative_to(ROOT)}")
    return destination, info


def write_final_srt(scenes):
    destination = CAPTION_DIR / "final_captions.srt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    global_offset = 0.0
    cue_number = 1

    for scene in scenes:
        duration = float(scene["duration"])
        cues = wrap_words(scene["text"])
        weights = [
            max(1, len(cue.replace(r"\N", " ").split()))
            for cue in cues
        ]
        total_weight = sum(weights)
        cursor = 0.0

        for index, (cue, weight) in enumerate(zip(cues, weights)):
            end = (
                duration
                if index == len(cues) - 1
                else cursor + duration * weight / total_weight
            )
            text = cue.replace(r"\N", "\n")

            entries.append(
                f"{cue_number}\n"
                f"{srt_time(global_offset + cursor)} --> "
                f"{srt_time(global_offset + end)}\n"
                f"{text}\n"
            )
            cue_number += 1
            cursor = end

        global_offset += duration

    if not entries:
        raise RuntimeError("Cannot create SRT: no captions were produced.")

    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        "\n".join(entries) + "\n",
        encoding="utf-8-sig",
    )
    temporary.replace(destination)
    log("Final SRT created:", destination.relative_to(ROOT))


def write_concat_file(paths, destination):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty video list.")
    lines = []
    for path in paths:
        value = str(path.resolve())
        if "\n" in value or "\r" in value:
            raise RuntimeError("Invalid newline in video path.")
        lines.append("file '" + value.replace("'", "'\\''") + "'")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concatenate(ffmpeg, paths, destination, list_file):
    write_concat_file(paths, list_file)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    temporary.unlink(missing_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        run([
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-movflags", "+faststart",
            str(temporary),
        ])
        info = media_info(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Concatenated video has no audio: {destination}")
        temporary.replace(destination)
        return media_info(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required.")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    requested_parts = int(get_parts(config))

    motion_rows = rows_from_json(
        read_json(MOTION_MANIFEST),
        "Motion manifest",
        ("scenes", "clips", "jobs", "items"),
    )
    visual_rows = rows_from_json(
        read_json(VISUAL_JOBS),
        "Visual jobs",
        ("jobs", "visual_jobs", "scenes"),
    )
    captions = load_caption_map()

    visual_by_key = {}
    for index, row in enumerate(visual_rows, 1):
        part, local, global_scene = scene_identity(
            row, index, "visual"
        )
        key = (part, local)
        if key in visual_by_key:
            raise RuntimeError(f"Duplicate visual scene: {key}")
        visual_by_key[key] = global_scene

    motion_by_key = {}
    for index, row in enumerate(motion_rows, 1):
        part, local, global_scene = scene_identity(
            row, index, "motion"
        )
        key = (part, local)
        if key in motion_by_key:
            raise RuntimeError(f"Duplicate motion scene: {key}")

        path = safe_path(row.get("video"), f"video for scene {global_scene}")
        info = media_info(path)
        if not info["has_audio"]:
            raise RuntimeError(
                f"Scene {global_scene} has no audio. "
                "Run scripts/render_video.py before burning captions."
            )

        motion_by_key[key] = {
            "part": part,
            "local_scene": local,
            "global_scene": global_scene,
            "path": path,
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "row": dict(row),
        }

    if set(visual_by_key) != set(motion_by_key):
        raise RuntimeError("Visual and motion scene lists do not match.")
    if set(captions) != set(visual_by_key):
        missing = sorted(set(visual_by_key) - set(captions))
        extra = sorted(set(captions) - set(visual_by_key))
        raise RuntimeError(
            f"Caption/visual scene mismatch. Missing={missing}; extra={extra}"
        )

    scenes = []
    for key, global_scene in visual_by_key.items():
        scene = motion_by_key[key]
        if scene["global_scene"] != global_scene:
            raise RuntimeError(
                f"Global scene mismatch for Part {key[0]}, Scene {key[1]}."
            )
        scene["text"] = captions[key]
        scenes.append(scene)

    scenes.sort(key=lambda item: item["global_scene"])
    expected_numbers = list(range(1, len(scenes) + 1))
    actual_numbers = [scene["global_scene"] for scene in scenes]
    if actual_numbers != expected_numbers:
        raise RuntimeError(
            f"Scene numbering is not continuous: {actual_numbers}"
        )

    expected_resolution = (
        (1280, 720) if mode == "full" else (720, 1280)
    )
    for scene in scenes:
        if (scene["width"], scene["height"]) != expected_resolution:
            raise RuntimeError(
                f"Scene {scene['global_scene']} has resolution "
                f"{scene['width']}x{scene['height']}; expected "
                f"{expected_resolution[0]}x{expected_resolution[1]}."
            )

    log("=" * 55)
    log("PER-SCENE CAPTION RENDER")
    log("Format:", mode)
    log("Scenes:", len(scenes))

    captioned_paths = []
    updated_rows = {}

    # Render every scene first; do not merge until this loop succeeds.
    for index, scene in enumerate(scenes, start=1):
        destination, info = render_captioned_scene(
            ffmpeg, scene, index, len(scenes)
        )
        captioned_paths.append(destination)

        updated = dict(scene["row"])
        updated["video"] = destination.relative_to(ROOT).as_posix()
        updated["duration"] = round(info["duration"], 6)
        updated["captions_burned"] = True
        updated_rows[scene["global_scene"]] = updated

    # Save an external SRT with the same scene ordering and durations.
    write_final_srt(scenes)

    # Assemble parts using each scene's actual part number.
    grouped = defaultdict(list)
    for scene, path in zip(scenes, captioned_paths):
        grouped[scene["part"]].append((scene["global_scene"], path))

    if len(grouped) > requested_parts:
        raise RuntimeError(
            f"Found {len(grouped)} scene parts but PARTS={requested_parts}."
        )

    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    for stale in PARTS_DIR.glob("part_*.mp4"):
        stale.unlink(missing_ok=True)

    for part_number in sorted(grouped):
        ordered = sorted(grouped[part_number], key=lambda item: item[0])
        part_paths = [path for _, path in ordered]
        part_path = PARTS_DIR / f"part_{part_number:02d}.mp4"
        concatenate(
            ffmpeg,
            part_paths,
            part_path,
            CAPTION_DIR / f"concat_part_{part_number:02d}.txt",
        )
        log(f"PART {part_number} READY: {part_path.relative_to(ROOT)}")

    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    final_info = concatenate(
        ffmpeg,
        captioned_paths,
        final_video,
        CAPTION_DIR / "concat_captioned_final.txt",
    )

    if (final_info["width"], final_info["height"]) != expected_resolution:
        raise RuntimeError("Final video resolution does not match FORMAT.")

    alias = VIDEOS_DIR / (
        "katha_lok_ai_full.mp4"
        if mode == "full"
        else "katha_lok_ai_short.mp4"
    )
    alias_temp = alias.with_name(alias.stem + ".tmp.mp4")
    shutil.copy2(final_video, alias_temp)
    media_info(alias_temp)
    alias_temp.replace(alias)

    opposite_alias = VIDEOS_DIR / (
        "katha_lok_ai_short.mp4"
        if mode == "full"
        else "katha_lok_ai_full.mp4"
    )
    opposite_alias.unlink(missing_ok=True)

    # Publish the manifest last, after captioned clips and final video exist.
    ordered_rows = [
        updated_rows[number] for number in sorted(updated_rows)
    ]
    write_json(MOTION_MANIFEST, ordered_rows)

    log("=" * 55)
    log("PER-SCENE CAPTION RENDER SUCCESS")
    log("Captioned scenes:", len(captioned_paths))
    log("Parts:", len(grouped))
    log("Final duration:", f"{final_info['duration']:.2f}s")
    log("Final video:", final_video.relative_to(ROOT))
    log("SRT:", (CAPTION_DIR / "final_captions.srt").relative_to(ROOT))
    log("Format alias:", alias.relative_to(ROOT))
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("CAPTION RENDER CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"CAPTION RENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)