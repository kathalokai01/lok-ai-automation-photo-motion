
#!/usr/bin/env python3
"""Create a resumable, topic-specific thumbnail without external API calls."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_title_source

OUT = ROOT / "output"
VISUAL_DIR = OUT / "visuals"
VIDEO_DIR = OUT / "videos"
THUMBNAIL = OUT / "thumbnail.jpg"
WORK_DIR = OUT / "thumbnail"
MANIFEST = WORK_DIR / "thumbnail_manifest.json"
FRAME_CACHE = WORK_DIR / "video_frame.jpg"

VERSION = "thumbnail-v8-video-frame-fallback"
MIN_IMAGE_BYTES = 1000

FONT_NAMES = {
    False: (
        "NotoSansDevanagari-Regular.ttf",
        "Mukta-Regular.ttf",
        "Hind-Regular.ttf",
        "TiroDevanagariHindi-Regular.ttf",
    ),
    True: (
        "NotoSansDevanagari-Bold.ttf",
        "Mukta-Bold.ttf",
        "Hind-Bold.ttf",
        "TiroDevanagariHindi-Bold.ttf",
    ),
}

FONT_DIRS = (
    ROOT / "assets" / "fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_hash(path):
    return sha256_file(path) if path and path.is_file() else None


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
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return None
        if path.suffix.lower() not in {
            ".jpg", ".jpeg", ".png", ".webp", ".bmp",
        }:
            return None

        with Image.open(path) as image:
            image.verify()

        return path
    except (OSError, ValueError):
        return None


def find_font(bold=False):
    for base in FONT_DIRS:
        if not base.exists():
            continue

        for name in FONT_NAMES[bold]:
            direct = base / name
            if direct.is_file():
                return direct

            try:
                for path in base.rglob(name):
                    if path.is_file():
                        return path
            except OSError:
                continue

    return None


def load_font(size, bold=False):
    path = find_font(bold)

    if path:
        try:
            font = ImageFont.truetype(
                str(path),
                size=size,
                layout_engine=ImageFont.Layout.RAQM,
            )
            return font, path
        except (OSError, AttributeError, ValueError):
            pass

    # The Pillow default font is only a fallback; Hindi shaping may be poor.
    try:
        return ImageFont.load_default(size=size), None
    except TypeError:
        return ImageFont.load_default(), None


def get_title(config):
    title = str(get_title_source(config) or "").strip()

    if not title:
        title_file = OUT / "story" / "final_title.txt"
        if title_file.is_file():
            lines = title_file.read_text(
                encoding="utf-8-sig"
            ).splitlines()
            title = lines[0].strip() if lines else ""

    title = re.sub(r"\s+", " ", title).strip()

    if not title:
        raise RuntimeError(
            "Title missing. Set TOPIC or provide output/story/final_title.txt."
        )

    # Preserve the complete title; layout logic handles long titles.
    return title


def collect_images():
    found = []

    manifests = (
        VISUAL_DIR / "image_manifest.json",
        VISUAL_DIR / "visual_jobs.json",
        VISUAL_DIR / "images_manifest.json",
    )

    row_keys = (
        "images", "jobs", "visual_jobs", "completed_images", "items",
    )
    path_keys = (
        "image_path", "image", "image_file", "output_image", "path",
    )

    for manifest_path in manifests:
        data = read_json(manifest_path)
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

            for key in path_keys:
                image = safe_image(row.get(key))
                if image:
                    found.append(image)
                    break

    if not found and VISUAL_DIR.exists():
        for path in sorted(VISUAL_DIR.rglob("*")):
            if "depth" in str(path).lower():
                continue

            image = safe_image(str(path))
            if image:
                found.append(image)

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
    if not ffmpeg:
        return None

    candidates = (
        VIDEO_DIR / "katha_lok_ai_final.mp4",
        VIDEO_DIR / (
            "katha_lok_ai_short.mp4"
            if mode == "short"
            else "katha_lok_ai_full.mp4"
        ),
        OUT / "scene_renders" / "part_01_scene_01.mp4",
    )

    for video in candidates:
        if not video.is_file() or video.stat().st_size < MIN_IMAGE_BYTES:
            continue

        probe = shutil.which("ffprobe")
        if not probe:
            return None

        result = subprocess.run(
            [
                probe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

        try:
            duration = float(result.stdout.strip())
        except (ValueError, TypeError):
            continue

        if result.returncode != 0 or duration <= 0:
            continue

        timestamp = max(0.0, min(duration * 0.30, duration - 0.1))
        temporary = FRAME_CACHE.with_suffix(".tmp.jpg")
        WORK_DIR.mkdir(parents=True, exist_ok=True)

        frame = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{timestamp:.3f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf", "scale=1280:-2",
                "-q:v", "2",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

        if (
            frame.returncode == 0
            and temporary.is_file()
            and temporary.stat().st_size >= MIN_IMAGE_BYTES
        ):
            try:
                with Image.open(temporary) as image:
                    image.verify()

                temporary.replace(FRAME_CACHE)
                log("BACKGROUND: extracted from", video.relative_to(ROOT))
                return FRAME_CACHE
            except (OSError, ValueError):
                pass

        temporary.unlink(missing_ok=True)

    return None


def fit_cover(image, width, height):
    image = image.convert("RGB")
    if image.width < 1 or image.height < 1:
        raise RuntimeError("Background image has invalid dimensions")

    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (
            max(width, round(image.width * scale)),
            max(height, round(image.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )

    left = (resized.width - width) // 2
    top = (resized.height - height) // 2

    return resized.crop((left, top, left + width, top + height))


def text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=2)
    return box[2] - box[0]


def split_long_word(draw, word, font, max_width):
    """Split only if a single unbroken word cannot fit."""
    chunks = []
    current = ""

    for char in word:
        candidate = current + char

        if current and text_width(draw, candidate, font) > max_width:
            chunks.append(current)
            current = char
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


def wrap_title(draw, title, font, max_width):
    lines = []
    current = ""

    for word in title.split():
        pieces = (
            split_long_word(draw, word, font, max_width)
            if text_width(draw, word, font) > max_width
            else [word]
        )

        for piece in pieces:
            candidate = f"{current} {piece}".strip()

            if current and text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = piece
            else:
                current = candidate

    if current:
        lines.append(current)

    return lines


def choose_layout(draw, title, width, height, mode):
    max_width = int(width * 0.84)
    max_lines = 5 if mode == "short" else 3
    initial_size = int(height * (0.065 if mode == "short" else 0.105))
    minimum_size = max(18, int(height * 0.025))

    for size in range(initial_size, minimum_size - 1, -2):
        font, font_path = load_font(size, bold=True)
        lines = wrap_title(draw, title, font, max_width)

        heights = [
            max(
                1,
                draw.textbbox(
                    (0, 0), line, font=font, stroke_width=2
                )[3]
                - draw.textbbox(
                    (0, 0), line, font=font, stroke_width=2
                )[1],
            )
            for line in lines
        ]

        if len(lines) <= max_lines and all(
            text_width(draw, line, font) <= max_width
            for line in lines
        ):
            return font, font_path, lines, heights

    font, font_path = load_font(minimum_size, bold=True)
    lines = wrap_title(draw, title, font, int(width * 0.90))
    heights = [
        max(
            1,
            draw.textbbox((0, 0), line, font=font, stroke_width=2)[3]
            - draw.textbbox((0, 0), line, font=font, stroke_width=2)[1],
        )
        for line in lines
    ]

    return font, font_path, lines, heights


def create_thumbnail(title, mode, image_path):
    width, height = (
        (1080, 1920) if mode == "short" else (1280, 720)
    )

    if image_path:
        with Image.open(image_path) as image:
            background = fit_cover(image, width, height)
    else:
        background = Image.new("RGB", (width, height), (22, 24, 34))

    background = ImageEnhance.Color(background).enhance(0.92)
    background = ImageEnhance.Contrast(background).enhance(1.12)

    # Dark gradient behind title for reliable text contrast.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gradient = ImageDraw.Draw(overlay)
    start_y = int(height * (0.38 if mode == "full" else 0.50))

    for y in range(start_y, height):
        ratio = (y - start_y) / max(1, height - start_y)
        alpha = min(235, int(25 + 210 * ratio))
        gradient.line((0, y, width, y), fill=(0, 0, 0, alpha))

    canvas = Image.alpha_composite(background.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    font, font_path, lines, heights = choose_layout(
        draw, title, width, height, mode
    )

    gap = max(8, int(height * 0.010))
    text_height = sum(heights) + gap * max(0, len(lines) - 1)
    margin_x = int(width * 0.055)
    text_y = max(
        start_y + 16,
        height - text_height - int(height * 0.075),
    )

    panel_top = max(start_y, text_y - int(height * 0.035))
    panel_bottom = min(
        height - int(height * 0.025),
        text_y + text_height + int(height * 0.04),
    )

    draw.rounded_rectangle(
        (margin_x, panel_top, width - margin_x, panel_bottom),
        radius=max(16, int(width * 0.02)),
        fill=(10, 12, 18, 230),
        outline=(238, 190, 94, 245),
        width=max(2, width // 360),
    )

    y = text_y

    for line, line_height in zip(lines, heights):
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        line_width = box[2] - box[0]
        x = max(8, (width - line_width) // 2)

        draw.text(
            (x + 2, y + 3),
            line,
            font=font,
            fill=(0, 0, 0),
            stroke_width=4,
            stroke_fill=(0, 0, 0),
        )
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 245, 220),
            stroke_width=2,
            stroke_fill=(30, 20, 15),
        )

        y += line_height + gap

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    temporary = THUMBNAIL.with_suffix(".tmp.jpg")

    canvas.convert("RGB").save(
        temporary,
        format="JPEG",
        quality=93,
        optimize=True,
        progressive=True,
    )
    temporary.replace(THUMBNAIL)

    with Image.open(THUMBNAIL) as image:
        image.verify()

    with Image.open(THUMBNAIL) as image:
        if image.size != (width, height):
            raise RuntimeError(
                f"Unexpected thumbnail dimensions: {image.size}"
            )

    if THUMBNAIL.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError("Generated thumbnail is unexpectedly small")

    return {
        "width": width,
        "height": height,
        "font_size": getattr(font, "size", None),
        "font_file": str(font_path) if font_path else None,
        "title_lines": len(lines),
    }


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported format: {mode}")

    title = get_title(config)
    images = collect_images()
    background = images[0] if images else extract_video_frame(mode)

    image_hash = file_hash(background) or "solid-color-fallback"
    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()

    width, height = (
        (1080, 1920) if mode == "short" else (1280, 720)
    )

    fingerprint = hashlib.sha256(
        (
            f"{VERSION}|{mode}|{width}x{height}|"
            f"{title_hash}|{image_hash}"
        ).encode("utf-8")
    ).hexdigest()

    old = read_json(MANIFEST)

    if (
        isinstance(old, dict)
        and old.get("status") == "completed"
        and old.get("fingerprint") == fingerprint
        and THUMBNAIL.is_file()
        and THUMBNAIL.stat().st_size >= MIN_IMAGE_BYTES
    ):
        try:
            with Image.open(THUMBNAIL) as image:
                image.verify()
            with Image.open(THUMBNAIL) as image:
                if image.size == (width, height):
                    log("RESUME: existing thumbnail is valid")
                    return
        except (OSError, ValueError):
            pass

    details = create_thumbnail(title, mode, background)

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
            "background_sha256": image_hash,
            "thumbnail": THUMBNAIL.relative_to(ROOT).as_posix(),
            **details,
            "external_api_calls": 0,
            "paid_services_used": False,
        },
    )

    log("THUMBNAIL CREATED")
    log("FORMAT:", mode)
    log("DIMENSIONS:", f"{width}x{height}")
    log("TITLE LINES:", details["title_lines"])
    log("OUTPUT:", THUMBNAIL.relative_to(ROOT))
    log("EXTERNAL API CALLS: 0")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", str(exc))
        sys.exit(1)
