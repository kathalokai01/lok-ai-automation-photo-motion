#!/usr/bin/env python3
"""Generate story-aware thumbnails with Hindi shaping and safe title layout."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageStat, features

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_title_source

INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT = ROOT / "output"
TITLE_FILE = OUTPUT / "story" / "final_title.txt"
VISUAL_DIR = OUTPUT / "visuals"
IMAGE_MANIFEST = VISUAL_DIR / "image_manifest.json"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
THUMB_DIR = OUTPUT / "thumbnail"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
MANIFEST = THUMB_DIR / "thumbnail_manifest.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
TEXT_STROKE = 4

PALETTES = {
    "devotional": {
        "name": "divine_gold",
        "board": (64, 20, 77),
        "edge": (255, 194, 69),
        "title": (255, 226, 125),
        "outline": (70, 27, 8),
        "accent": (255, 146, 45),
    },
    "mystery": {
        "name": "mystery_violet",
        "board": (20, 20, 61),
        "edge": (149, 117, 255),
        "title": (230, 221, 255),
        "outline": (42, 19, 91),
        "accent": (93, 190, 255),
    },
    "horror": {
        "name": "dark_crimson",
        "board": (24, 12, 21),
        "edge": (210, 48, 63),
        "title": (255, 221, 217),
        "outline": (91, 5, 21),
        "accent": (255, 62, 52),
    },
    "historical": {
        "name": "royal_bronze",
        "board": (53, 28, 21),
        "edge": (214, 148, 72),
        "title": (255, 218, 157),
        "outline": (70, 27, 8),
        "accent": (180, 89, 43),
    },
    "emotional": {
        "name": "warm_rose",
        "board": (76, 29, 48),
        "edge": (255, 174, 160),
        "title": (255, 240, 220),
        "outline": (91, 25, 42),
        "accent": (255, 130, 102),
    },
    "nature": {
        "name": "forest_emerald",
        "board": (15, 46, 40),
        "edge": (125, 222, 166),
        "title": (229, 255, 221),
        "outline": (11, 53, 38),
        "accent": (102, 216, 174),
    },
    "general": {
        "name": "cinematic_cobalt",
        "board": (19, 35, 66),
        "edge": (100, 196, 255),
        "title": (255, 242, 207),
        "outline": (24, 34, 65),
        "accent": (255, 180, 73),
    },
}

KEYWORDS = {
    "devotional": (
        "भगवान", "देव", "देवी", "मंदिर", "भक्ति", "कृष्ण", "राम",
        "शिव", "विष्णु", "हनुमान", "दुर्गा", "गणेश", "कथा", "पुराण",
        "पूजा", "divine", "god", "goddess", "temple", "devotional",
        "krishna", "rama", "shiva",
    ),
    "mystery": (
        "रहस्य", "राज", "गुमशुदा", "गायब", "पहेली", "सच", "सच्चाई",
        "खोज", "अनोखा", "अज्ञात", "mystery", "secret", "unknown",
        "disappear", "hidden", "truth", "enigma",
    ),
    "horror": (
        "भूत", "प्रेत", "डर", "डरावनी", "भय", "चुड़ैल", "आत्मा",
        "शैतान", "खौफ", "हवेली", "haunted", "horror", "ghost",
        "witch", "demon", "scary", "terror",
    ),
    "historical": (
        "राजा", "रानी", "राज्य", "युद्ध", "महाराज", "सम्राट", "किला",
        "इतिहास", "योद्धा", "साम्राज्य", "historical", "king", "queen",
        "emperor", "warrior", "battle", "kingdom", "fort",
    ),
    "emotional": (
        "माँ", "मां", "पिता", "बेटा", "बेटी", "परिवार", "प्रेम",
        "प्यार", "विरह", "बलिदान", "आंसू", "दर्द", "emotional",
        "mother", "father", "family", "love", "sacrifice", "heart",
    ),
    "nature": (
        "जंगल", "वन", "पहाड़", "नदी", "प्रकृति", "गाँव", "गांव",
        "किसान", "पशु", "पक्षी", "forest", "village", "nature",
        "mountain", "river", "wildlife", "farmer",
    ),
}

FONT_CANDIDATES = (
    Path.home() / ".local/share/fonts/katha-caption/NotoSansDevanagari-Bold.ttf",
    ROOT / "assets/fonts/NotoSansDevanagari-Bold.ttf",
    ROOT / "fonts/NotoSansDevanagari-Bold.ttf",
    Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)


def log(*args):
    print(*args, flush=True)


def sha_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value
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


def clean_text(value, limit=250):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def load_title(config):
    if TITLE_FILE.is_file():
        try:
            for line in TITLE_FILE.read_text(
                encoding="utf-8-sig"
            ).splitlines():
                title = clean_text(line)
                if title:
                    return title
        except OSError as exc:
            log("WARNING: Cannot read final title:", exc)

    title = clean_text(get_title_source(config))
    if title:
        return title

    raise RuntimeError(
        "Title missing: set TOPIC/TITLE or generate final_title.txt."
    )


def choose_palette(title, topic):
    text = f"{title} {topic}".casefold()

    for category in (
        "horror", "mystery", "devotional",
        "historical", "emotional", "nature",
    ):
        if any(word.casefold() in text for word in KEYWORDS[category]):
            return dict(PALETTES[category])

    return dict(PALETTES["general"])


def rows_from(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("scenes", "jobs", "visuals", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def resolve_image(value):
    if not isinstance(value, str) or not value.strip():
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()
        path.relative_to(OUTPUT.resolve())
    except (ValueError, OSError):
        return None

    if (
        not path.is_file()
        or path.suffix.lower() not in IMAGE_EXTENSIONS
        or "depth_maps" in path.parts
        or "thumbnail" in path.parts
    ):
        return None

    return path


def choose_source_image():
    candidates = []

    for manifest_path in (IMAGE_MANIFEST, JOBS_FILE):
        data = read_json(manifest_path)

        for row in rows_from(data):
            if not isinstance(row, dict):
                continue

            status = str(row.get("status", "")).lower()
            if status and status not in {
                "completed", "complete", "success", "done",
                "generated", "ready", "reused", "existing",
            }:
                continue

            for key in ("image_path", "image", "asset_path", "image_file"):
                candidate = resolve_image(row.get(key))
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
                    break

    if VISUAL_DIR.is_dir():
        for path in sorted(VISUAL_DIR.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                candidate = resolve_image(str(path))
                if candidate and candidate not in candidates:
                    candidates.append(candidate)

    for path in candidates:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                if image.width >= 64 and image.height >= 64:
                    return path
        except Exception as exc:
            log("WARNING: Skipping invalid image:", path.name, exc)

    raise RuntimeError("No valid scene image found for thumbnail.")


def find_font(size):
    layout = None
    if hasattr(ImageFont, "Layout") and features.check_feature("raqm"):
        layout = ImageFont.Layout.RAQM

    for path in FONT_CANDIDATES:
        if not path.is_file():
            continue
        try:
            kwargs = {"size": size}
            if layout is not None:
                kwargs["layout_engine"] = layout
            return ImageFont.truetype(str(path), **kwargs)
        except (OSError, TypeError):
            continue

    # Pillow's default font may not shape Hindi correctly. Fail explicitly
    # rather than silently creating a broken Devanagari title.
    raise RuntimeError(
        "No suitable Hindi title font found. Install Noto Sans Devanagari "
        "or place its bold TTF in assets/fonts/."
    )


def text_width(draw, text, font):
    box = draw.textbbox(
        (0, 0), text, font=font, stroke_width=TEXT_STROKE
    )
    return box[2] - box[0]


def wrap_title(draw, title, font, max_width):
    words = title.split()
    if not words:
        return ["Katha Lok AI"]

    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word

        if text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        # If a single word is wider than the available area, keep it intact;
        # font-size reduction will be attempted by fit_title().
        current = word

    if current:
        lines.append(current)

    return lines


def fit_title(draw, title, max_width, max_lines, start_size):
    size = start_size

    while size >= 24:
        font = find_font(size)
        lines = wrap_title(draw, title, font, max_width)

        if (
            len(lines) <= max_lines
            and all(text_width(draw, line, font) <= max_width for line in lines)
        ):
            return font, lines, size

        size -= 2

    font = find_font(24)
    words = title.split()
    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]

        while last and text_width(draw, last + "…", font) > max_width:
            last = last[:-1].rstrip()

        lines[-1] = (last + "…") if last else "…"

    return font, lines, 24


def crop_to_ratio(image, ratio):
    width, height = image.size

    if width / height > ratio:
        new_width = max(1, round(height * ratio))
        left = (width - new_width) // 2
        return image.crop((left, 0, left + new_width, height))

    new_height = max(1, round(width / ratio))
    top = (height - new_height) // 2
    return image.crop((0, top, width, top + new_height))


def adjust_palette(image, palette):
    sample = image.resize((64, 64)).convert("RGB")
    brightness = sum(ImageStat.Stat(sample).mean) / 3
    result = dict(palette)

    if brightness < 78:
        result["board"] = tuple(
            min(255, int(channel * 1.16 + 8))
            for channel in palette["board"]
        )
        result["title"] = (255, 244, 205)
    elif brightness > 185:
        result["board"] = tuple(
            max(0, int(channel * 0.72))
            for channel in palette["board"]
        )
        result["outline"] = (12, 12, 22)

    return result, round(brightness, 2)


def make_overlay(width, height, mode):
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if mode == "short":
        start, end = int(height * 0.20), int(height * 0.55)
    else:
        start, end = int(height * 0.07), int(height * 0.66)

    span = max(1, end - start)

    for y in range(start, end):
        distance = min(y - start, end - y) / max(1, span / 2)
        alpha = int(36 + 82 * max(0, 1 - distance))
        draw.line((0, y, width, y), fill=(0, 0, 0, alpha))

    return overlay


def draw_plaque(draw, box, palette, border_width):
    x1, y1, x2, y2 = box
    radius = max(18, border_width * 4)

    draw.rounded_rectangle(
        (x1 + 6, y1 + 8, x2 + 6, y2 + 8),
        radius=radius,
        fill=(0, 0, 0, 195),
    )
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=(*palette["board"], 246),
        outline=(*palette["edge"], 255),
        width=border_width,
    )

    inset = max(8, border_width * 2)
    inner = (x1 + inset, y1 + inset, x2 - inset, y2 - inset)
    draw.rounded_rectangle(
        inner,
        radius=max(5, radius - inset),
        outline=(*palette["edge"], 180),
        width=max(1, border_width // 2),
    )


def render_thumbnail(source, title, mode, destination, palette):
    size = (1080, 1920) if mode == "short" else (1280, 720)
    width, height = size

    with Image.open(source) as original:
        image = original.convert("RGB")

    image = crop_to_ratio(image, width / height).resize(
        size, Image.Resampling.LANCZOS
    )
    image = image.filter(
        ImageFilter.UnsharpMask(radius=2, percent=115, threshold=3)
    )

    palette, brightness = adjust_palette(image, palette)

    base = image.convert("RGBA")
    base.alpha_composite(make_overlay(width, height, mode))
    image = base
    draw = ImageDraw.Draw(image)

    if mode == "short":
        plaque = (
            int(width * 0.055), int(height * 0.225),
            int(width * 0.945), int(height * 0.495),
        )
        start_size, max_lines, border_width = int(width * 0.10), 4, 6
    else:
        plaque = (
            int(width * 0.20), int(height * 0.08),
            int(width * 0.80), int(height * 0.65),
        )
        start_size, max_lines, border_width = int(width * 0.068), 3, 5

    x1, y1, x2, y2 = plaque
    plaque_width, plaque_height = x2 - x1, y2 - y1

    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        (x1 - 7, y1 - 7, x2 + 7, y2 + 7),
        radius=30,
        fill=(*palette["accent"], 100),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(17))
    image = Image.alpha_composite(image, glow)
    draw = ImageDraw.Draw(image)

    draw_plaque(draw, plaque, palette, border_width)

    margin_x = int(plaque_width * 0.065)
    margin_y = int(plaque_height * 0.08)
    text_box = (
        x1 + margin_x, y1 + margin_y,
        x2 - margin_x, y2 - margin_y,
    )

    font, lines, chosen_size = fit_title(
        draw,
        title,
        text_box[2] - text_box[0],
        max_lines,
        start_size,
    )

    line_gap = max(4, chosen_size // 9)

    def line_heights():
        return [
            max(
                1,
                draw.textbbox(
                    (0, 0), line, font=font, stroke_width=TEXT_STROKE
                )[3]
                - draw.textbbox(
                    (0, 0), line, font=font, stroke_width=TEXT_STROKE
                )[1],
            )
            for line in lines
        ]

    heights = line_heights()
    available_height = text_box[3] - text_box[1]

    while (
        sum(heights) + line_gap * max(0, len(lines) - 1) > available_height
        and chosen_size > 24
    ):
        chosen_size -= 2
        font = find_font(chosen_size)
        line_gap = max(3, chosen_size // 9)
        heights = line_heights()

    total_height = sum(heights) + line_gap * max(0, len(lines) - 1)
    y = text_box[1] + max(0, (available_height - total_height) // 2)

    for line, line_height in zip(lines, heights):
        bounds = draw.textbbox(
            (0, 0), line, font=font, stroke_width=TEXT_STROKE
        )
        line_width = bounds[2] - bounds[0]
        x = (width - line_width) // 2 - bounds[0]

        # Dark shadow improves readability on busy backgrounds.
        draw.text(
            (x + 4, y + 5),
            line,
            font=font,
            fill=(0, 0, 0, 255),
            stroke_width=TEXT_STROKE + 3,
            stroke_fill=(0, 0, 0, 255),
        )
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(*palette["title"], 255),
            stroke_width=TEXT_STROKE,
            stroke_fill=(*palette["outline"], 255),
        )

        y += line_height + line_gap

    ornament_font = find_font(max(22, width // 28))
    for x in (x1 + 12, x2 - width // 18):
        draw.text(
            (x, y1 + 6),
            "✦",
            font=ornament_font,
            fill=(*palette["edge"], 255),
            stroke_width=1,
            stroke_fill=(*palette["outline"], 255),
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.jpg")

    try:
        image.convert("RGB").save(
            temp, "JPEG", quality=94, optimize=True
        )

        with Image.open(temp) as check:
            check.verify()

        with Image.open(temp) as check:
            if check.size != size:
                raise RuntimeError(
                    f"Thumbnail dimensions are incorrect: {check.size}"
                )

        if temp.stat().st_size < 1000:
            raise RuntimeError("Thumbnail file is unexpectedly small.")

        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)

    return palette, brightness, size


def main():
    config = load_and_validate(INPUT_FILE)
    mode_value = str(get_format(config)).strip().lower()
    mode = (
        "full"
        if mode_value in {"full", "long", "landscape", "youtube"}
        else "short"
    )

    title = load_title(config)
    topic = clean_text(config.get("TOPIC") or config.get("TITLE") or "")

    if not topic:
        topic = title

    palette = choose_palette(title, topic)
    source = choose_source_image()

    source_hash = sha_file(source)
    title_hash = sha_text(title)
    topic_hash = sha_text(topic)
    style_hash = sha_text(json.dumps(palette, sort_keys=True))
    font_hash = sha_text(
        "|".join(str(path) for path in FONT_CANDIDATES)
        + f"|raqm={features.check_feature('raqm')}"
    )

    expected_size = (1080, 1920) if mode == "short" else (1280, 720)
    previous = read_json(MANIFEST)

    reusable = (
        isinstance(previous, dict)
        and previous.get("status") == "completed"
        and previous.get("format") == mode
        and previous.get("title_sha256") == title_hash
        and previous.get("topic_sha256") == topic_hash
        and previous.get("source_sha256") == source_hash
        and previous.get("style_sha256") == style_hash
        and previous.get("font_fingerprint") == font_hash
        and previous.get("path") == "output/thumbnail.jpg"
        and THUMBNAIL.is_file()
    )

    if reusable:
        try:
            with Image.open(THUMBNAIL) as image:
                image.verify()
            with Image.open(THUMBNAIL) as image:
                reusable = image.size == expected_size
        except Exception:
            reusable = False

    if reusable:
        final_palette = palette
        with Image.open(source) as image:
            _, brightness = adjust_palette(image.convert("RGB"), palette)
        final_size = expected_size
        log("Reusing matching, validated thumbnail.")
    else:
        final_palette, brightness, final_size = render_thumbnail(
            source, title, mode, THUMBNAIL, palette
        )

    result = {
        "status": "completed",
        "format": mode,
        "title": title,
        "title_sha256": title_hash,
        "topic_sha256": topic_hash,
        "source_visual": source.relative_to(ROOT).as_posix(),
        "source_sha256": source_hash,
        "path": "output/thumbnail.jpg",
        "width": final_size[0],
        "height": final_size[1],
        "size_bytes": THUMBNAIL.stat().st_size,
        "reused": bool(reusable),
        "style": final_palette["name"],
        "palette": final_palette,
        "source_brightness": brightness,
        "style_sha256": style_hash,
        "font_fingerprint": font_hash,
        "palette_rule": "story-keywords-plus-background-brightness",
    }

    write_json(MANIFEST, result)

    log("THUMBNAIL GENERATION SUCCESS")
    log("Format:", mode)
    log("Title:", title)
    log("Style:", final_palette["name"])
    log("Dimensions:", f"{final_size[0]}x{final_size[1]}")
    log("Thumbnail:", THUMBNAIL.relative_to(ROOT))
    log("Manifest:", MANIFEST.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"THUMBNAIL GENERATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)