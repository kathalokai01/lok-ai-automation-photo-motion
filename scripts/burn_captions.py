#!/usr/bin/env python3
"""Validate, burn scene captions, create SRT, and assemble final videos."""

from __future__ import annotations

import hashlib
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
CHECKPOINT = CAPTIONED_DIR / "caption_checkpoint.json"
PARTS_DIR = OUTPUT / "parts"
VIDEOS_DIR = OUTPUT / "videos"

MIN_BYTES = 1000
FONT_NAME = "Noto Sans Devanagari"
FONT_SIZE = 14
MAX_LINE_CHARS = 30
MAX_LINES = 2
BOTTOM_MARGIN = 65
OUTLINE_SIZE = 2

PALETTE = [
    (255, 225, 0),
    (0, 240, 255),
    (130, 255, 0),
    (255, 145, 0),
    (255, 50, 210),
    (190, 110, 255),
    (255, 115, 160),
    (0, 220, 120),
    (255, 255, 255),
    (0, 0, 0),
]


def log(*args):
    print(*args, flush=True)


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
        check=False,
    )
    if result.stdout:
        log(result.stdout.rstrip())
    if result.returncode:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}."
        )


def rows_from_json(data, label, keys):
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next(
            (
                data.get(key) for key in keys
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


def identity(row, index, label):
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


def load_captions():
    data = read_json(CAPTIONS_JSON)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("captions.json is not completed.")

    rows = data.get("captions")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("No caption records found.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid caption record.")

        key = (
            positive_int(row.get("part"), "caption part"),
            positive_int(row.get("scene"), "caption scene"),
        )
        text = clean_text(row.get("text"))
        if not text:
            raise RuntimeError(f"Empty caption for {key}.")
        if key in result:
            raise RuntimeError(f"Duplicate caption for {key}.")
        result[key] = text

    return result


def probe(path):
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError(f"Video missing or too small: {path}")

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
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed: {path.name}")

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media information: {path}") from exc

    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    has_audio = any(
        stream.get("codec_type") == "audio" for stream in streams
    )

    if not video or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video stream: {path}")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width < 1 or height < 1:
        raise RuntimeError(f"Invalid video resolution: {path}")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": has_audio,
    }


def wrap_text(text):
    words = clean_text(text).split()
    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > MAX_LINE_CHARS:
            lines.append(current)
            current = word
        else:
            current = candidate

    if current:
        lines.append(current)

    # Split into subtitle cues, each with at most two lines.
    return [
        r"\N".join(lines[start:start + MAX_LINES])
        for start in range(0, len(lines), MAX_LINES)
    ]


def ass_time(seconds):
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centiseconds:02}"


def srt_time(seconds):
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def linear_channel(value):
    value /= 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (linear_channel(value) for value in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(first, second):
    a, b = luminance(first), luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def sample_background(ffmpeg, video, seconds):
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0, seconds):.3f}", "-i", str(video),
        "-vf",
        "crop=iw:ih*0.30:0:ih*0.65,"
        "scale=1:1:flags=area,format=rgb24",
        "-frames:v", "1", "-f", "rawvideo",
        "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=25, check=False,
        )
        if result.returncode == 0 and len(result.stdout) >= 3:
            return tuple(result.stdout[:3])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return (45, 45, 45)


def ass_colour(rgb):
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def ass_escape(text):
    return (
        str(text).replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def filter_path(path):
    value = str(path.resolve())
    for old, new in (
        ("\\", "\\\\"), (":", "\\:"), ("'", "\\'"),
        (",", "\\,"), ("[", "\\["), ("]", "\\]"),
    ):
        value = value.replace(old, new)
    return value


def create_ass(ffmpeg, scene, destination):
    cues = wrap_text(scene["text"])
    if not cues:
        raise RuntimeError(
            f"Empty caption at scene {scene['global_scene']}."
        )

    weights = [
        max(1, len(cue.replace(r"\N", " ").split()))
        for cue in cues
    ]
    total_weight = sum(weights)
    cursor = 0.0
    events = []

    for index, (cue, weight) in enumerate(zip(cues, weights)):
        end = (
            scene["duration"] if index == len(cues) - 1
            else cursor + scene["duration"] * weight / total_weight
        )
        bg = sample_background(
            ffmpeg, scene["path"], (cursor + end) / 2
        )
        colour = max(PALETTE, key=lambda rgb: contrast(rgb, bg))
        outline = (
            (0, 0, 0)
            if contrast(colour, (0, 0, 0))
            >= contrast(colour, (255, 255, 255))
            else (255, 255, 255)
        )
        override = (
            f"{{\\1c{ass_colour(colour)}"
            f"\\3c{ass_colour(outline)}\\bord{OUTLINE_SIZE}}}"
        )
        events.append(
            f"Dialogue: 0,{ass_time(cursor)},{ass_time(end)},Default,"
            f",0,0,0,,{override}{ass_escape(cue)}\n"
        )
        cursor = end

    header = f"""[Script Info]
Title: Scene {scene['global_scene']} captions
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
    destination.write_text(header + "".join(events), encoding="utf-8")


def read_checkpoint():
    if not CHECKPOINT.is_file():
        return {}
    try:
        data = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log("NOTICE: Invalid caption checkpoint; rebuilding it.")
        return {}


def render_scene(ffmpeg, scene, index, total, signature):
    destination = CAPTIONED_DIR / f"scene_{index:04d}.mp4"
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    ass_path = CAPTION_DIR / "scene_ass" / f"scene_{index:04d}.ass"

    checkpoint = read_checkpoint()
    entry = checkpoint.get(str(scene["global_scene"]), {})

    if (
        isinstance(entry, dict)
        and entry.get("signature") == signature
        and destination.is_file()
        and destination.stat().st_size >= MIN_BYTES
    ):
        try:
            info = probe(destination)
            if (
                info["has_audio"]
                and abs(info["duration"] - scene["duration"]) <= 1.0
                and (info["width"], info["height"])
                == (scene["width"], scene["height"])
            ):
                log(f"REUSE captioned scene {index}/{total}: {destination.name}")
                return destination, info
        except (RuntimeError, OSError):
            pass

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    create_ass(ffmpeg, scene, ass_path)

    run([
        ffmpeg, "-hide_banner", "-y",
        "-i", str(scene["path"]),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", f"ass=filename='{filter_path(ass_path)}'",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart",
        str(temporary),
    ])

    info = probe(temporary)
    if not info["has_audio"]:
        raise RuntimeError(f"Captioned scene {index} has no audio.")
    if (info["width"], info["height"]) != (
        scene["width"], scene["height"]
    ):
        raise RuntimeError(f"Captioned scene {index} has wrong resolution.")
    if abs(info["duration"] - scene["duration"]) > 1.0:
        raise RuntimeError(f"Captioned scene {index} duration changed.")

    temporary.replace(destination)

    checkpoint[str(scene["global_scene"])] = {
        "signature": signature,
        "source_sha256": sha256_file(scene["path"]),
        "video": destination.relative_to(ROOT).as_posix(),
        "size_bytes": destination.stat().st_size,
        "status": "completed",
    }
    write_json(CHECKPOINT, checkpoint)
    log(f"CHECKPOINT saved: scene {index}/{total}")
    return destination, info


def write_srt(scenes):
    entries = []
    offset = 0.0
    number = 1

    for scene in scenes:
        cues = wrap_text(scene["text"])
        weights = [
            max(1, len(cue.replace(r"\N", " ").split()))
            for cue in cues
        ]
        total_weight = sum(weights)
        cursor = 0.0

        for index, (cue, weight) in enumerate(zip(cues, weights)):
            end = (
                scene["duration"] if index == len(cues) - 1
                else cursor + scene["duration"] * weight / total_weight
            )
            # Avoid a backslash-containing expression inside an f-string
            # (which is incompatible with Python 3.11).
            subtitle_text = cue.replace(r"\N", "\n")
            entries.append(
                f"{number}\n"
                f"{srt_time(offset + cursor)} --> "
                f"{srt_time(offset + end)}\n"
                f"{subtitle_text}\n"
            )
            number += 1
            cursor = end

        offset += scene["duration"]

    if not entries:
        raise RuntimeError("No SRT captions generated.")

    destination = CAPTION_DIR / "final_captions.srt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    temp.write_text("\n".join(entries) + "\n", encoding="utf-8-sig")
    temp.replace(destination)


def write_concat_file(paths, destination):
    if not paths:
        raise RuntimeError("Cannot concatenate an empty list.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = []

    for path in paths:
        value = str(path.resolve())
        if "\n" in value or "\r" in value:
            raise RuntimeError("Invalid newline in video path.")
        lines.append("file '" + value.replace("'", "'\\''") + "'")

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
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", str(temporary),
        ])
        info = probe(temporary)
        if not info["has_audio"]:
            raise RuntimeError(f"Output has no audio: {destination.name}")
        temporary.replace(destination)
        return probe(destination)
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
    expected_resolution = (
        (1280, 720) if mode == "full" else (720, 1280)
    )

    motion_rows = rows_from_json(
        read_json(MOTION_MANIFEST), "Motion manifest",
        ("scenes", "clips", "jobs", "items"),
    )
    visual_rows = rows_from_json(
        read_json(VISUAL_JOBS), "Visual jobs",
        ("jobs", "visual_jobs", "scenes"),
    )
    captions = load_captions()

    visuals = {}
    for index, row in enumerate(visual_rows, 1):
        part, local, global_scene = identity(row, index, "visual")
        key = (part, local)
        if key in visuals:
            raise RuntimeError(f"Duplicate visual scene: {key}")
        visuals[key] = global_scene

    motions = {}
    for index, row in enumerate(motion_rows, 1):
        part, local, global_scene = identity(row, index, "motion")
        key = (part, local)
        if key in motions:
            raise RuntimeError(f"Duplicate motion scene: {key}")

        path = safe_path(row.get("video"), f"scene {global_scene} video")
        info = probe(path)
        if not info["has_audio"]:
            raise RuntimeError(
                f"Scene {global_scene} has no audio; render narration first."
            )

        motions[key] = {
            "part": part,
            "local_scene": local,
            "global_scene": global_scene,
            "path": path,
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "row": dict(row),
        }

    if set(visuals) != set(motions) or set(captions) != set(visuals):
        raise RuntimeError(
            "Visual, motion and caption scene lists do not match."
        )

    scenes = []
    for key, global_scene in visuals.items():
        scene = motions[key]

        if scene["global_scene"] != global_scene:
            raise RuntimeError(f"Scene numbering mismatch: {key}")

        if (scene["width"], scene["height"]) != expected_resolution:
            raise RuntimeError(
                f"Scene {global_scene} resolution is "
                f"{scene['width']}x{scene['height']}; expected "
                f"{expected_resolution[0]}x{expected_resolution[1]}."
            )

        scene["text"] = captions[key]
        scenes.append(scene)

    scenes.sort(key=lambda scene: scene["global_scene"])
    numbers = [scene["global_scene"] for scene in scenes]

    if numbers != list(range(1, len(scenes) + 1)):
        raise RuntimeError("Global scene numbers must be continuous.")

    if len({scene["part"] for scene in scenes}) > requested_parts:
        raise RuntimeError(
            "Detected more parts than configured PARTS."
        )

    CAPTIONED_DIR.mkdir(parents=True, exist_ok=True)
    log("Caption mode:", mode, "| scenes:", len(scenes))
    log("Checkpoint:", CHECKPOINT.relative_to(ROOT))

    captioned_paths = []
    updated_rows = {}

    for index, scene in enumerate(scenes, 1):
        source_hash = sha256_file(scene["path"])
        signature = fingerprint({
            "version": 3,
            "source_sha256": source_hash,
            "caption": scene["text"],
            "duration": round(scene["duration"], 4),
            "width": scene["width"],
            "height": scene["height"],
            "font": FONT_NAME,
            "font_size": FONT_SIZE,
            "max_line_chars": MAX_LINE_CHARS,
            "max_lines": MAX_LINES,
            "bottom_margin": BOTTOM_MARGIN,
            "outline_size": OUTLINE_SIZE,
        })

        path, info = render_scene(
            ffmpeg, scene, index, len(scenes), signature
        )
        captioned_paths.append(path)

        updated = dict(scene["row"])
        updated["video"] = path.relative_to(ROOT).as_posix()
        updated["duration"] = round(info["duration"], 6)
        updated["captions_burned"] = True
        updated_rows[scene["global_scene"]] = updated

        checkpoint = read_checkpoint()
        entry = checkpoint.get(str(scene["global_scene"]), {})
        if isinstance(entry, dict):
            entry["source_sha256"] = source_hash
            checkpoint[str(scene["global_scene"])] = entry
            write_json(CHECKPOINT, checkpoint)

    write_srt(scenes)

    grouped = defaultdict(list)
    for scene, path in zip(scenes, captioned_paths):
        grouped[scene["part"]].append((scene["global_scene"], path))

    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    # Remove stale part outputs so old parts cannot be mistaken for current ones.
    for stale in PARTS_DIR.glob("part_*.mp4"):
        stale.unlink(missing_ok=True)

    for part in sorted(grouped):
        paths = [
            path for _, path in sorted(
                grouped[part], key=lambda item: item[0]
            )
        ]
        destination = PARTS_DIR / f"part_{part:02d}.mp4"
        concatenate(
            ffmpeg, paths, destination,
            CAPTION_DIR / f"concat_part_{part:02d}.txt",
        )
        log("PART READY:", destination.relative_to(ROOT))

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
        if mode == "full" else "katha_lok_ai_short.mp4"
    )
    temp_alias = alias.with_name(alias.stem + ".tmp.mp4")
    shutil.copy2(final_video, temp_alias)
    probe(temp_alias)
    temp_alias.replace(alias)

    opposite = VIDEOS_DIR / (
        "katha_lok_ai_short.mp4"
        if mode == "full" else "katha_lok_ai_full.mp4"
    )
    opposite.unlink(missing_ok=True)

    write_json(
        MOTION_MANIFEST,
        [updated_rows[number] for number in sorted(updated_rows)],
    )

    srt = CAPTION_DIR / "final_captions.srt"
    if not srt.is_file() or srt.stat().st_size < 10:
        raise RuntimeError("Final SRT is missing or empty.")

    log("=" * 55)
    log("CAPTIONS AND FINAL RENDER COMPLETED")
    log("Scenes:", len(captioned_paths))
    log("Parts:", len(grouped))
    log("Duration:", f"{final_info['duration']:.2f}s")
    log("Final:", final_video.relative_to(ROOT))
    log("Mode-specific output:", alias.relative_to(ROOT))
    log("SRT:", srt.relative_to(ROOT))
    log("Checkpoint:", CHECKPOINT.relative_to(ROOT))
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

