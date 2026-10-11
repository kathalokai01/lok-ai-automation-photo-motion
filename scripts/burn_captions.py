
#!/usr/bin/env python3
"""Validate scene timing and burn compact, dynamically contrasted captions."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"

VIDEO = OUTPUT / "videos" / "katha_lok_ai_final.mp4"
CAPTIONS = OUTPUT / "captions" / "captions.json"
VISUAL_JOBS = OUTPUT / "visuals" / "visual_jobs.json"
MOTION_MANIFEST = OUTPUT / "photo_motion" / "photo_motion_manifest.json"
SRT = OUTPUT / "captions" / "final_captions.srt"
ASS = OUTPUT / "captions" / "final_captions.ass"
TEMP_VIDEO = OUTPUT / "videos" / "katha_lok_ai_captioned.tmp.mp4"

# Caption readability.
MAX_LINE_CHARS = 30
MAX_LINES = 2
FONT_NAME = "Noto Sans Devanagari"
FONT_SIZE = 14
OUTLINE_SIZE = 2
BOTTOM_MARGIN = 80

# RGB colours considered for each caption cue.
# The best colour is selected by contrast against the sampled video area.
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

# A safe fallback if frame sampling fails.
FALLBACK_RGB = (45, 45, 45)


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def run(command):
    print("$", " ".join(map(str, command)), flush=True)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.stdout:
        print(result.stdout, flush=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}"
        )


def extract_list(data, label, keys):
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
        raise RuntimeError(f"{label} contains no usable entries.")
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{label} contains a non-object entry.")
    return rows


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}: {value!r}") from exc

    if result < 1:
        raise RuntimeError(f"{label} must be positive.")
    return result


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
        raise RuntimeError("captions.json has no captions.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption entry.")

        part = positive_int(row.get("part"), "caption part")
        local = positive_int(row.get("scene"), "caption scene")
        text = clean_text(row.get("text"))
        if not text:
            raise RuntimeError(
                f"Empty caption at Part {part}, Scene {local}."
            )

        key = (part, local)
        if key in result:
            raise RuntimeError(
                f"Duplicate caption for Part {part}, Scene {local}."
            )
        result[key] = text

    return result


def extract_motion(data):
    rows = extract_list(
        data,
        "Photo Motion manifest",
        ("scenes", "clips", "jobs", "items"),
    )
    result = {}

    for index, row in enumerate(rows, 1):
        part, local, global_scene = scene_key(row, index)
        try:
            duration = float(row["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid duration in motion row: {row}"
            ) from exc

        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(
                f"Invalid duration for scene {global_scene}."
            )

        key = (part, local)
        if key in result:
            raise RuntimeError(
                f"Duplicate motion scene: Part {part}, Scene {local}."
            )

        result[key] = {
            "global_scene": global_scene,
            "duration": duration,
        }

    return result


def build_timeline(jobs_data, motion_data, captions):
    jobs = extract_list(
        jobs_data,
        "Visual jobs",
        ("jobs", "visual_jobs", "scenes"),
    )
    scenes = []
    seen_keys = set()
    seen_global = set()

    for index, job in enumerate(jobs, 1):
        part, local, global_scene = scene_key(job, index)
        key = (part, local)

        if key in seen_keys or global_scene in seen_global:
            raise RuntimeError(f"Duplicate visual scene: {job}")
        seen_keys.add(key)
        seen_global.add(global_scene)

        if key not in captions:
            raise RuntimeError(
                f"Caption missing for Part {part}, Scene {local}."
            )
        if key not in motion_data:
            raise RuntimeError(
                f"Motion clip missing for Part {part}, Scene {local}."
            )

        motion = motion_data[key]
        if motion["global_scene"] != global_scene:
            raise RuntimeError(
                f"Scene numbering mismatch for Part {part}, "
                f"Scene {local}: visual={global_scene}, "
                f"motion={motion['global_scene']}."
            )

        scenes.append({
            "part": part,
            "local_scene": local,
            "global_scene": global_scene,
            "duration": motion["duration"],
            "text": captions[key],
        })

    scenes.sort(key=lambda row: row["global_scene"])
    actual = [row["global_scene"] for row in scenes]
    expected = list(range(1, len(scenes) + 1))

    if actual != expected:
        raise RuntimeError(
            f"Global scene numbering is not continuous: {actual}"
        )

    if seen_keys != set(captions):
        raise RuntimeError(
            "Caption and visual scene sets differ. "
            f"Caption-only={sorted(set(captions) - seen_keys)}; "
            f"Visual-only={sorted(seen_keys - set(captions))}"
        )

    if seen_keys != set(motion_data):
        raise RuntimeError(
            "Visual and motion scene sets differ. "
            f"Motion-only={sorted(set(motion_data) - seen_keys)}"
        )

    return scenes


def probe(ffprobe, path):
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Cannot inspect {path.name}: {exc}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration in {path.name}.")
    if not any(row.get("codec_type") == "video" for row in streams):
        raise RuntimeError(f"No video stream in {path.name}.")
    return duration


def srt_time(seconds):
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def ass_time(seconds):
    cs = max(0, round(seconds * 100))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    secs, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def wrap_words(text, max_chars=MAX_LINE_CHARS):
    words = clean_text(text).split()
    lines = []
    current = ""

    for word in words:
        if len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            # Avoid splitting ordinary Hindi words; exceptionally long
            # tokens are left intact and wrapped by the subtitle renderer.
            lines.append(word)
            continue

        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)
    return lines


def split_into_cues(text):
    lines = wrap_words(text)
    cues = []
    for start in range(0, len(lines), MAX_LINES):
        cue = "\n".join(lines[start:start + MAX_LINES]).strip()
        if cue:
            cues.append(cue)
    return cues


def make_cue_timeline(scenes, video_duration):
    timeline = sum(row["duration"] for row in scenes)
    tolerance = max(2.0, video_duration * 0.03)

    if abs(timeline - video_duration) > tolerance:
        raise RuntimeError(
            "Scene timeline does not match final video duration. "
            f"Scenes={timeline:.2f}s; video={video_duration:.2f}s. "
            "Check video assembly and scene durations before captions."
        )

    entries = []
    cursor = 0.0

    for scene in scenes:
        start = cursor
        end = min(cursor + scene["duration"], video_duration)
        cursor += scene["duration"]
        available = end - start

        if available <= 0:
            continue

        cues = split_into_cues(scene["text"])
        if not cues:
            continue

        weights = [
            max(1, len(cue.replace("\n", " ").split()))
            for cue in cues
        ]
        total_weight = sum(weights)
        cue_cursor = start

        for index, (cue, weight) in enumerate(zip(cues, weights)):
            cue_end = (
                end if index == len(cues) - 1
                else cue_cursor + available * weight / total_weight
            )
            if cue_end > cue_cursor:
                entries.append({
                    "start": cue_cursor,
                    "end": cue_end,
                    "text": cue,
                })
            cue_cursor = cue_end

    if not entries:
        raise RuntimeError("No valid subtitle intervals were generated.")
    return entries


def make_srt(entries):
    blocks = []
    for number, cue in enumerate(entries, 1):
        blocks.append(
            f"{number}\n"
            f"{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n"
            f"{cue['text']}\n"
        )
    return "\n".join(blocks) + "\n"


def linear_channel(value):
    value = value / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (linear_channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    high = max(luminance(a), luminance(b))
    low = min(luminance(a), luminance(b))
    return (high + 0.05) / (low + 0.05)


def sample_background_rgb(ffmpeg, video, seconds):
    """Sample the lower subtitle area, returning its average RGB colour."""
    timestamp = max(0.0, float(seconds))
    video_filter = (
        "crop=iw:ih*0.30:0:ih*0.65,"
        "scale=1:1:flags=area,format=rgb24"
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{timestamp:.3f}",
        "-i", str(video),
        "-vf", video_filter,
        "-frames:v", "1",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1",
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

    print(
        f"WARNING: Could not sample frame at {timestamp:.2f}s; "
        "using fallback contrast colour.",
        flush=True,
    )
    return FALLBACK_RGB


def choose_caption_colour(background_rgb):
    """Choose the palette colour with the highest measured contrast."""
    name, rgb = max(
        PALETTE.items(),
        key=lambda item: contrast_ratio(item[1], background_rgb),
    )
    ratio = contrast_ratio(rgb, background_rgb)

    # Pick the outline that separates the text colour from its surroundings.
    black = (0, 0, 0)
    white = (255, 255, 255)
    outline = (
        black
        if contrast_ratio(rgb, black) >= contrast_ratio(rgb, white)
        else white
    )
    return name, rgb, outline, ratio


def ass_colour(rgb):
    """ASS colour format is &HAABBGGRR&."""
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def ass_escape(text):
    # Keep user/story text from being interpreted as ASS formatting codes.
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", r"\N")
    )


def write_ass(ffmpeg, video, entries, path):
    header = f"""[Script Info]
Title: Dynamic Contrast Captions
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{FONT_SIZE},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{OUTLINE_SIZE},0,2,45,45,{BOTTOM_MARGIN},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    selected_colours = []

    for cue in entries:
        midpoint = (cue["start"] + cue["end"]) / 2
        background = sample_background_rgb(ffmpeg, video, midpoint)
        name, text_rgb, outline_rgb, ratio = choose_caption_colour(background)

        selected_colours.append(name)
        override = (
            f"{{\\1c{ass_colour(text_rgb)}"
            f"\\3c{ass_colour(outline_rgb)}"
            f"\\bord{OUTLINE_SIZE}}}"
        )
        text = override + ass_escape(cue["text"])

        lines.append(
            f"Dialogue: 0,{ass_time(cue['start'])},"
            f"{ass_time(cue['end'])},Default,,0,0,0,,{text}\n"
        )

        print(
            f"Caption colour: {name}; contrast={ratio:.2f}:1; "
            f"time={midpoint:.2f}s",
            flush=True,
        )

    path.write_text("".join(lines), encoding="utf-8")
    return selected_colours


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


def get_format_alias():
    config_path = ROOT / "Input" / "topic.txt"
    mode = ""

    if config_path.is_file():
        for line in config_path.read_text(
            encoding="utf-8-sig"
        ).splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().upper() == "FORMAT":
                mode = (
                    value.split("#", 1)[0]
                    .strip()
                    .strip("'\"")
                    .lower()
                )
                break

    full_modes = {
        "full", "long", "landscape", "youtube",
        "youtube_full", "youtube-long",
    }
    if mode in full_modes:
        return OUTPUT / "videos" / "katha_lok_ai_full.mp4"
    return OUTPUT / "videos" / "katha_lok_ai_short.mp4"


def main():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe must be installed.")

    for path in (VIDEO, CAPTIONS, VISUAL_JOBS, MOTION_MANIFEST):
        if not path.is_file():
            raise RuntimeError(f"Required input missing: {path}")
    if VIDEO.stat().st_size < 1000:
        raise RuntimeError("Final video is missing or too small.")

    caption_data = read_json(CAPTIONS)
    scenes = build_timeline(
        read_json(VISUAL_JOBS),
        extract_motion(read_json(MOTION_MANIFEST)),
        extract_captions(caption_data),
    )
    video_duration = probe(ffprobe, VIDEO)
    entries = make_cue_timeline(scenes, video_duration)

    SRT.parent.mkdir(parents=True, exist_ok=True)
    SRT.write_text(make_srt(entries), encoding="utf-8")
    colours = write_ass(ffmpeg, VIDEO, entries, ASS)

    TEMP_VIDEO.unlink(missing_ok=True)
    subtitle_filter = f"ass=filename='{escape_filter_path(ASS)}'"

    run([
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i", str(VIDEO),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", subtitle_filter,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(TEMP_VIDEO),
    ])

    if not TEMP_VIDEO.is_file() or TEMP_VIDEO.stat().st_size < 1000:
        raise RuntimeError("Captioned MP4 was not created correctly.")

    probe(ffprobe, TEMP_VIDEO)
    TEMP_VIDEO.replace(VIDEO)

    alias = get_format_alias()
    if alias != VIDEO and alias.exists():
        shutil.copy2(VIDEO, alias)
        if alias.stat().st_size != VIDEO.stat().st_size:
            raise RuntimeError(
                "Captioned format alias failed verification."
            )

    print("=" * 55)
    print("CAPTION BURN-IN: SUCCESS")
    print("Caption mode:", caption_data.get("mode", "unknown"))
    print("Scenes:", len(scenes))
    print("Caption cues:", len(entries))
    print("Distinct colours used:", ", ".join(sorted(set(colours))))
    print("Font size:", FONT_SIZE)
    print("Maximum lines per cue:", MAX_LINES)
    print("Duration:", f"{video_duration:.2f}s")
    print("Final MP4:", VIDEO.relative_to(ROOT))
    print("SRT subtitles:", SRT.relative_to(ROOT))
    print("ASS dynamic-colour subtitles:", ASS.relative_to(ROOT))
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CAPTION BURN-IN FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
