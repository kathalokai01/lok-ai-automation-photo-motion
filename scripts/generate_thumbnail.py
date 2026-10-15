
#!/usr/bin/env python3
"""Generate a resumable, YouTube-standard 16:9 thumbnail without paid APIs."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_title_source

OUT = ROOT / "output"
VISUAL_DIR = OUT / "visuals"
THUMBNAIL = OUT / "thumbnail.jpg"
WORK_DIR = OUT / "thumbnail"
MANIFEST = WORK_DIR / "thumbnail_manifest.json"

VERSION = "thumbnail-v10-youtube-16x9"
WIDTH, HEIGHT = 1280, 720
MIN_BYTES = 1000

FONT_DIRS = (
    ROOT / "assets" / "fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)
FONT_NAMES = (
    "NotoSansDevanagari-Bold.ttf",
    "Mukta-Bold.ttf",
    "Hind-Bold.ttf",
    "NotoSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_title(config):
    title = str(get_title_source(config) or "").strip()

    if not title:
        title_file = OUT / "story" / "final_title.txt"
        if title_file.is_file():
            title = title_file.read_text(
                encoding="utf-8-sig"
            ).splitlines()[0].strip()

    title = re.sub(r"\s+", " ", title).strip(" \t-–—:|\"'“”‘’")
    if not title:
        raise RuntimeError(
            "Title missing: set TOPIC in Input/topic.txt "
            "or provide output/story/final_title.txt"
        )
    return title


def find_font():
    for base in FONT_DIRS:
        if not base.exists():
            continue
        for name in FONT_NAMES:
            direct = base / name
            if direct.is_file():
                return direct
            try:
                for candidate in base.rglob(name):
                    if candidate.is_file():
                        return candidate
            except OSError:
                continue
    return None


def load_font(size):
    path = find_font()
    if path:
        try:
            return ImageFont.truetype(
                str(path),
                size=size,
                layout_engine=ImageFont.Layout.RAQM,
            ), str(path)
        except (OSError, ValueError, AttributeError):
            try:
                return ImageFont.truetype(str(path), size=size), str(path)
            except OSError:
                pass

    try:
        return ImageFont.load_default(size=size), None
    except TypeError:
        return ImageFont.load_default(), None


def safe_image(value):
    if not isinstance(value, str) or not value.strip():
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()
        if not path.is_relative_to(ROOT.resolve()):
            return None
        if not path.is_file() or path.stat().st_size < MIN_BYTES:
            return None
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            return None
        with Image.open(path) as image:
            image.verify()
        return path
    except (OSError, ValueError):
        return None


def collect_images():
    found = []
    manifests = (
        VISUAL_DIR / "image_manifest.json",
        VISUAL_DIR / "visual_jobs.json",
        VISUAL_DIR / "images_manifest.json",
    )
    row_keys = ("images", "jobs", "visual_jobs", "completed_images", "items")
    path_keys = ("image_path", "image", "image_file", "output_image", "path")

    for manifest in manifests:
        data = read_json(manifest)
        if data is None:
            continue

        rows = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in row_keys:
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break

        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status", "")).lower()
            if status and status not in {
                "completed", "success", "succeeded", "ready"
            }:
                continue

            for key in path_keys:
                path = safe_image(row.get(key))
                if path and "depth" not in str(path).lower():
                    found.append(path)
                    break

    unique = []
    seen = set()
    for path in found:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def extract_video_frame(mode):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return None

    videos = (
        OUT / "videos" / (
            "katha_lok_ai_short.mp4"
            if mode == "short"
            else "katha_lok_ai_full.mp4"
        ),
        OUT / "videos" / "katha_lok_ai_final.mp4",
        OUT / "scene_renders" / "part_01_scene_01.mp4",
    )

    for video in videos:
        if not video.is_file() or video.stat().st_size < MIN_BYTES:
            continue
        try:
            probe = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(video),
                ],
                capture_output=True, text=True, timeout=45, check=False,
            )
            duration = float(probe.stdout.strip())
            if probe.returncode or duration <= 0:
                continue

            WORK_DIR.mkdir(parents=True, exist_ok=True)
            temp = WORK_DIR / "frame.tmp.jpg"
            result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{min(duration * 0.3, max(0, duration - 0.1)):.3f}",
                    "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=1280:720:force_original_aspect_ratio=increase,"
                           "crop=1280:720",
                    "-q:v", "2", str(temp),
                ],
                capture_output=True, text=True, timeout=90, check=False,
            )
            if result.returncode == 0 and temp.is_file():
                image = safe_image(str(temp))
                if image:
                    cached = WORK_DIR / "video_frame.jpg"
                    temp.replace(cached)
                    return cached
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return None


def fit_cover(image, width, height):
    image = image.convert("RGB")
    scale = max(width / image.width, height / image.height)
    image = image.resize(
        (
            max(width, round(image.width * scale)),
            max(height, round(image.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )
    left = (image.width - width) // 2
    top = (image.height - height) // 2
    return image.crop((left, top, left + width, top + height))


def text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=2)
    return box[2] - box[0]


def wrap_text(draw, text, font, max_width):
    lines = []
    current = ""

    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate

    if current:
        lines.append(current)

    # Prevent a single extremely long title from overflowing.
    result = []
    for line in lines:
        if text_width(draw, line, font) <= max_width:
            result.append(line)
            continue

        chunk = ""
        for char in line:
            candidate = chunk + char
            if chunk and text_width(draw, candidate, font) > max_width:
                result.append(chunk)
                chunk = char
            else:
                chunk = candidate
        if chunk:
            result.append(chunk)

    return result


def compact_title(title):
    title = re.sub(r"\s+", " ", title).strip()
    for separator in (" | ", " — ", " – ", ": "):
        if separator in title:
            first = title.split(separator, 1)[0].strip()
            if len(first) >= 5:
                title = first
                break

    if len(title) > 68:
        title = title[:68].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return title


def create_thumbnail(title, background_path):
    if background_path:
        with Image.open(background_path) as source:
            background = fit_cover(source, WIDTH, HEIGHT)
    else:
        background = Image.new("RGB", (WIDTH, HEIGHT), (25, 27, 38))

    background = ImageEnhance.Color(background).enhance(0.92)
    background = ImageEnhance.Contrast(background).enhance(1.12)

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    pixels = overlay.load()
    start_y = int(HEIGHT * 0.34)

    for y in range(start_y, HEIGHT):
        alpha = min(232, int(18 + 215 * (y - start_y) / (HEIGHT - start_y)))
        for x in range(WIDTH):
            pixels[x, y] = (0, 0, 0, alpha)

    canvas = Image.alpha_composite(background.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    display_title = compact_title(title)
    max_width = int(WIDTH * 0.86)
    chosen = None

    for size in range(82, 35, -2):
        font, font_path = load_font(size)
        lines = wrap_text(draw, display_title, font, max_width)
        if 1 <= len(lines) <= 3:
            boxes = [
                draw.textbbox((0, 0), line, font=font, stroke_width=2)
                for line in lines
            ]
            heights = [max(1, b[3] - b[1]) for b in boxes]
            gap = 12
            total_height = sum(heights) + gap * (len(lines) - 1)
            if total_height <= int(HEIGHT * 0.36):
                chosen = (font, font_path, lines, heights, gap)
                break

    if chosen is None:
        font, font_path = load_font(36)
        lines = wrap_text(draw, display_title, font, max_width)[:3]
        heights = [
            max(1, draw.textbbox((0, 0), line, font=font)[3])
            for line in lines
        ]
        gap = 8
    else:
        font, font_path, lines, heights, gap = chosen

    total_height = sum(heights) + gap * max(0, len(lines) - 1)
    margin_x = 55
    text_y = max(start_y + 18, HEIGHT - total_height - 62)
    panel_top = max(start_y, text_y - 24)
    panel_bottom = min(HEIGHT - 20, text_y + total_height + 28)

    draw.rounded_rectangle(
        (margin_x, panel_top, WIDTH - margin_x, panel_bottom),
        radius=22,
        fill=(8, 10, 16, 232),
        outline=(238, 190, 94, 245),
        width=4,
    )

    y = text_y
    for line, line_height in zip(lines, heights):
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        x = max(12, (WIDTH - (box[2] - box[0])) // 2)
        draw.text(
            (x + 3, y + 3), line, font=font, fill=(0, 0, 0),
            stroke_width=5, stroke_fill=(0, 0, 0),
        )
        draw.text(
            (x, y), line, font=font, fill=(255, 245, 220),
            stroke_width=2, stroke_fill=(25, 17, 12),
        )
        y += line_height + gap

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    temp = THUMBNAIL.with_suffix(".tmp.jpg")
    try:
        canvas.convert("RGB").save(
            temp, format="JPEG", quality=93, optimize=True,
        )
        with Image.open(temp) as check:
            check.verify()
        with Image.open(temp) as check:
            if check.size != (WIDTH, HEIGHT):
                raise RuntimeError(f"Unexpected thumbnail size: {check.size}")
        if temp.stat().st_size < MIN_BYTES:
            raise RuntimeError("Thumbnail output is unexpectedly small")
        temp.replace(THUMBNAIL)
    finally:
        temp.unlink(missing_ok=True)

    return {
        "width": WIDTH,
        "height": HEIGHT,
        "font_size": getattr(font, "size", None),
        "font_file": font_path,
        "title_lines": len(lines),
        "display_title": display_title,
    }


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")

    title = get_title(config)
    images = collect_images()
    background = images[0] if images else extract_video_frame(mode)

    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    background_hash = (
        sha256_file(background) if background else "solid-color-fallback"
    )
    fingerprint = hashlib.sha256(
        f"{VERSION}|{mode}|{WIDTH}x{HEIGHT}|{title_hash}|{background_hash}".encode()
    ).hexdigest()

    old = read_json(MANIFEST)
    if (
        isinstance(old, dict)
        and old.get("status") == "completed"
        and old.get("fingerprint") == fingerprint
        and THUMBNAIL.is_file()
        and THUMBNAIL.stat().st_size >= MIN_BYTES
    ):
        try:
            with Image.open(THUMBNAIL) as image:
                image.verify()
            with Image.open(THUMBNAIL) as image:
                if image.size == (WIDTH, HEIGHT):
                    log("RESUME: existing thumbnail is valid")
                    return
        except (OSError, ValueError):
            pass

    details = create_thumbnail(title, background)
    write_json(
        MANIFEST,
        {
            "status": "completed",
            "version": VERSION,
            "fingerprint": fingerprint,
            "format": mode,
            "title": title,
            "title_sha256": title_hash,
            "background_image": (
                background.relative_to(ROOT).as_posix()
                if background else None
            ),
            "background_sha256": background_hash,
            "thumbnail": THUMBNAIL.relative_to(ROOT).as_posix(),
            **details,
            "external_api_calls": 0,
            "paid_services_used": False,
        },
    )

    log("THUMBNAIL CREATED")
    log("FORMAT:", mode)
    log("DIMENSIONS:", f"{WIDTH}x{HEIGHT}")
    log("TITLE LINES:", details["title_lines"])
    log("OUTPUT:", THUMBNAIL.relative_to(ROOT))
    log("EXTERNAL API CALLS: 0")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", str(exc))
        sys.exit(1)