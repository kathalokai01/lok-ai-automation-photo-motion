
#!/usr/bin/env python3
"""Burn captions per scene, then assemble captioned scenes into parts and final video."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUTPUT = ROOT / "output"
MOTION_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
VISUAL_JOBS = OUTPUT / "visuals" / "visual_jobs.json"
CAPTIONS = OUTPUT / "captions" / "captions.json"

SCENE_DIR = OUTPUT / "scene_renders"
CAPTIONED_DIR = OUTPUT / "scene_renders_captioned"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"
CAPTION_DIR = OUTPUT / "captions"

FONT_NAME = "Noto Sans Devanagari"
FONT_SIZE = 14
OUTLINE_SIZE = 2
BOTTOM_MARGIN = 80
MAX_LINE_CHARS = 30
MAX_LINES = 2

PALETTE = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "yellow": (255, 225, 0),
    "cyan": (0, 240, 255),
    "lime": (130, 255, 0),
    "orange": (255, 145, 0),
    "magenta": (255, 50, 210),
    "deep_blue": (35, 95, 255),
    "violet": (190, 110, 255),
    "pink": (255, 115, 160),
    "green": (0, 220, 120),
}
FALLBACK_RGB = (45, 45, 45)


def log(*args):
    print(*args, flush=True)


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


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing {label}.")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} escapes the repository.")
    return path


def media_info(path):
    if not path.is_file() or path.stat().st_size < 1000:
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
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    if not video or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video or duration: {path}")

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(
            s.get("codec_type") == "audio" for s in streams
        ),
    }


def extract_rows(data, label, keys):
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


def scene_key(row, index):
    part = positive_int(row.get("part", 1), "part")
    local = positive_int(
        row.get("local_scene", row.get("scene", index)),
        "local scene",
    )
    global_scene = positive_int(
        row.get("global_scene", row.get("scene", index)),
        "global scene",
    )
    return part, local, global_scene


def clean_text(value):
    return " ".join(
        str(value or "").replace("\r", " ").replace("\n", " ").split()
    ).strip()


def extract_captions(data):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Captions are not marked completed.")

    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("captions.json contains no captions.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption entry.")
        part = positive_int(row.get("part"), "caption part")
        local = positive_int(row.get("scene"), "caption scene")
        text = clean_text(row.get("text"))
        if not text:
            raise RuntimeError(
                f"Empty caption in Part {part}, Scene {local}."
            )
        key = (part, local)
        if key in result:
            raise RuntimeError(
                f"Duplicate caption for Part {part}, Scene {local}."
            )
        result[key] = text
    return result


def read_scenes():
    motion_rows = extract_rows(
        read_json(MOTION_MANIFEST),
        "Motion manifest",
        ("scenes", "clips", "jobs", "items"),
    )
    visual_rows = extract_rows(
        read_json(VISUAL_JOBS),
        "Visual jobs",
        ("jobs", "visual_jobs", "scenes"),
    )
    captions = extract_captions(read_json(CAPTIONS))

    visual_by_key = {}
    for index, row in enumerate(visual_rows, 1):
        part, local, global_scene = scene_key(row, index)
        key = (part, local)
        if key in visual_by_key:
            raise RuntimeError(f"Duplicate visual scene: {key}")
        visual_by_key[key] = global_scene

    motion_by_key = {}
    for index, row in enumerate(motion_rows, 1):
        part, local, global_scene = scene_key(row, index)
        key = (part, local)
        if key in motion_by_key:
            raise RuntimeError(f"Duplicate motion scene: {key}")

        video_value = row.get("video")
        video_path = safe_path(video_value, f"video for scene {global_scene}")
        info = media_info(video_path)

        try:
            duration = float(row.get("duration", info["duration"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid duration for scene {global_scene}."
            ) from exc

        # Use the actual rendered duration as the authoritative duration.
        if abs(duration - info["duration"]) > max(1.0, 2.0 / 24):
            log(
                f"WARNING: Scene {global_scene} manifest duration differs "
                "from the actual clip; using actual duration."
            )
        duration = info["duration"]

        motion_by_key[key] = {
            "part": part,
            "local_scene": local,
            "global_scene": global_scene,
            "path": video_path,
            "duration": duration,
            "width": info["width"],
            "height": info["height"],
            "has_audio": info["has_audio"],
            "row": dict(row),
        }

    if set(visual_by_key) != set(motion_by_key):
        raise RuntimeError("Visual and motion scene lists do not match.")
    if set(captions) != set(visual_by_key):
        raise RuntimeError(
            "Caption and visual scene lists do not match. "
            f"Missing captions={sorted(set(visual_by_key)-set(captions))}; "
            f"extra captions={sorted(set(captions)-set(visual_by_key))}."
        )

    scenes = []
    for key, global_scene in visual_by_key.items():
        motion = motion_by_key[key]
        if motion["global_scene"] != global_scene:
            raise RuntimeError(
                f"Global scene number mismatch for Part {key[0]}, "
                f"Scene {key[1]}."
            )
        motion["text"] = captions[key]
        scenes.append(motion)

    scenes.sort(key=lambda item: item["global_scene"])
    expected = list(range(1, len(scenes) + 1))
    actual = [item["global_scene"] for item in scenes]
    if actual != expected:
        raise RuntimeError(f"Scene numbering is not continuous: {actual}")

    return scenes, motion_rows


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

    cues = []
    for index in range(0, len(lines), MAX_LINES):
        cue = r"\N".join(lines[index:index + MAX_LINES]).strip()
        if cue:
            cues.append(cue)
    return cues


def ass_time(seconds):
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centiseconds:02}"


def linear_channel(value):
    value = value / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (linear_channel(v) for v in rgb)
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
        "crop=iw:ih*0.30:0:ih*0.65,scale=1:1:flags=area,format=rgb24",
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
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def ass_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def build_scene_ass(ffmpeg, scene, destination):
    duration = scene["duration"]
    cues = wrap_words(scene["text"])
    if not cues:
        raise RuntimeError(
            f"No caption text for scene {scene['global_scene']}."
        )

    # Distribute caption cues through this scene's own duration.
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
    destination.write_text(
        header + "".join(dialogue),
        encoding="utf-8",
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


def burn_one_scene(ffmpeg, scene, index):
    source = scene["path"]
    destination = CAPTIONED_DIR / f"scene_{index:04d}.mp4"
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    ass_path = CAPTION_DIR / "scene_ass" / f"scene_{index:04d}.ass"

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)
    build_scene_ass(ffmpeg, scene, ass_path)

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
        str(temp),
    ])

    info = media_info(temp)
    if not info["has_audio"]:
        raise RuntimeError(
            f"Captioned scene {index} has no audio stream."
        )
    if abs(info["duration"] - scene["duration"]) > 1.0:
        raise RuntimeError(
            f"Caption burn changed scene {index} duration unexpectedly."
        )

    temp.replace(destination)
    log(
        f"CAPTIONED SCENE {index}/{len_scenes_hint}: "
        f"{destination.relative_to(ROOT)}"
    )
    return destination, info


len_scenes_hint = 0


def write_concat_file(paths, destination):
    lines = []
    for path in paths:
        value = str(path.resolve())
        if "\n" in value or "\r" in value:
            raise RuntimeError("Invalid newline in media path.")
        lines.append("file '" + value.replace("'", "'\\''") + "'")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concatenate(ffmpeg, paths, destination, list_file):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty scene list.")
    write_concat_file(paths, list_file)
    temp = destination.with_name(destination.stem + ".tmp.mp4")
    temp.unlink(missing_ok=True)
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
            str(temp),
        ])
        media_info(temp)
        temp.replace(destination)
        return media_info(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def split_groups(scenes, requested_parts):
    if requested_parts < 1 or requested_parts > len(scenes):
        raise RuntimeError(
            f"PARTS={requested_parts} is invalid for {len(scenes)} scenes."
        )
    base, extra = divmod(len(scenes), requested_parts)
    groups = []
    cursor = 0
    for index in range(requested_parts):
        count = base + (1 if index < extra else 0)
        groups.append(scenes[cursor:cursor + count])
        cursor += count
    return groups


def main():
    global len_scenes_hint

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required.")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    requested_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    scenes, motion_rows = read_scenes()
    len_scenes_hint = len(scenes)

    expected_resolution = (
        (1280, 720) if mode == "full" else (720, 1280)
    )
    for scene in scenes:
        if (scene["width"], scene["height"]) != expected_resolution:
            raise RuntimeError(
                f"Scene {scene['global_scene']} has wrong resolution for "
                f"FORMAT={mode}: {scene['width']}x{scene['height']}."
            )
        if not scene["has_audio"]:
            raise RuntimeError(
                f"Scene {scene['global_scene']} has no narration/audio. "
                "Run the scene audio-video renderer first."
            )

    log("=" * 55)
    log("SCENE-BY-SCENE CAPTION RENDER")
    log("Format:", mode)
    log("Scenes:", len(scenes))
    log("Parts:", requested_parts)

    captioned = []
    updated_rows_by_global = {}

    for index, scene in enumerate(scenes, start=1):
        destination, info = burn_one_scene(ffmpeg, scene, index)
        captioned.append(destination)

        updated = dict(scene["row"])
        updated["video"] = destination.relative_to(ROOT).as_posix()
        updated["duration"] = round(info["duration"], 6)
        updated["captions_burned"] = True
        updated_rows_by_global[scene["global_scene"]] = updated

    # Keep the motion manifest pointing to the final captioned scene clips.
    ordered_rows = [
        updated_rows_by_global[index]
        for index in sorted(updated_rows_by_global)
    ]
    temp_manifest = MOTION_MANIFEST.with_name(
        MOTION_MANIFEST.name + ".tmp"
    )
    temp_manifest.write_text(
        json.dumps(ordered_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_manifest.replace(MOTION_MANIFEST)

    for stale in PARTS_DIR.glob("part_*.mp4"):
        stale.unlink(missing_ok=True)
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    # Merge the captioned scenes only after each scene is finished.
    groups = split_groups(scenes, requested_parts)
    for part_number, group in enumerate(groups, start=1):
        start = group[0]["global_scene"]
        end = group[-1]["global_scene"]
        paths = captioned[start - 1:end]
        part_path = PARTS_DIR / f"part_{part_number:02d}.mp4"
        concatenate(
            ffmpeg,
            paths,
            part_path,
            OUTPUT / "captions" / f"concat_part_{part_number:02d}.txt",
        )
        log(f"PART {part_number}/{requested_parts} READY")

    final_video = VIDEOS_DIR / "katha_lok_ai_final.mp4"
    final_info = concatenate(
        ffmpeg,
        captioned,
        final_video,
        OUTPUT / "captions" / "concat_captioned_final.txt",
    )

    if not final_info["has_audio"]:
        raise RuntimeError("Final captioned video has no audio.")
    if (
        final_info["width"], final_info["height"]
    ) != expected_resolution:
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

    log("=" * 55)
    log("PER-SCENE CAPTION RENDER SUCCESS")
    log("Scenes captioned:", len(captioned))
    log("Parts created:", requested_parts)
    log("Format:", mode)
    log("Duration:", f"{final_info['duration']:.2f}s")
    log("Final video:", final_video.relative_to(ROOT))
    log("Format alias:", alias.relative_to(ROOT))
    log("Captioned scene clips:", CAPTIONED_DIR.relative_to(ROOT))
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
