
#!/usr/bin/env python3
"""Create story-aware cinematic thumbnails with adaptive title colors."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageStat

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

# Semantic palette selection is a style guide, not a fixed color.
PALETTES = {
    "devotional": {
        "name": "divine_gold",
        "board": (64, 20, 77),
        "board_edge": (255, 194, 69),
        "title": (255, 226, 125),
        "title_edge": (88, 36, 8),
        "accent": (255, 146, 45),
    },
    "mystery": {
        "name": "mystery_violet",
        "board": (20, 20, 61),
        "board_edge": (149, 117, 255),
        "title": (230, 221, 255),
        "title_edge": (42, 19, 91),
        "accent": (93, 190, 255),
    },
    "horror": {
        "name": "dark_crimson",
        "board": (24, 12, 21),
        "board_edge": (210, 48, 63),
        "title": (255, 221, 217),
        "title_edge": (91, 5, 21),
        "accent": (255, 62, 52),
    },
    "historical": {
        "name": "royal_bronze",
        "board": (53, 28, 21),
        "board_edge": (214, 148, 72),
        "title": (255, 218, 157),
        "title_edge": (70, 27, 8),
        "accent": (180, 89, 43),
    },
    "emotional": {
        "name": "warm_rose",
        "board": (76, 29, 48),
        "board_edge": (255, 174, 160),
        "title": (255, 240, 220),
        "title_edge": (91, 25, 42),
        "accent": (255, 130, 102),
    },
    "nature": {
        "name": "forest_emerald",
        "board": (15, 46, 40),
        "board_edge": (125, 222, 166),
        "title": (229, 255, 221),
        "title_edge": (11, 53, 38),
        "accent": (102, 216, 174),
    },
    "general": {
        "name": "cinematic_cobalt",
        "board": (19, 35, 66),
        "board_edge": (100, 196, 255),
        "title": (255, 242, 207),
        "title_edge": (24, 34, 65),
        "accent": (255, 180, 73),
    },
}

KEYWORDS = {
    "devotional": (
        "भगवान", "भगवान्", "देव", "देवी", "मंदिर", "भक्ति", "कृष्ण",
        "राम", "शिव", "विष्णु", "हनुमान", "बालाजी", "दुर्गा", "गणेश",
        "कथा", "पुराण", "अवतार", "पूजा", "तीर्थ", "divine", "god",
        "goddess", "temple", "devotional", "krishna", "rama", "shiva",
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


def log(*values):
    print(*values, flush=True)


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


def clean_text(value, limit=250):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            log("WARNING: Could not read final title:", exc)

    title = clean_text(get_title_source(config))
    if title:
        return title

    raise RuntimeError(
        "No title found in final_title.txt or Input/topic.txt."
    )


def choose_palette(title, topic):
    text = f"{title} {topic}".casefold()

    # Most specific categories take precedence.
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

            for key in (
                "image_path", "image", "asset_path", "image_file",
            ):
                path = resolve_image(row.get(key))
                if path and path not in candidates:
                    candidates.append(path)
                    break

    if VISUAL_DIR.is_dir():
        for path in sorted(VISUAL_DIR.glob("scene_*")):
            candidate = resolve_image(str(path))
            if candidate and candidate not in candidates:
                candidates.append(candidate)

    for path in candidates:
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                if im.width >= 64 and im.height >= 64:
                    return path
        except Exception as exc:
            log("WARNING: Skipping invalid image:", path.name, exc)

    raise RuntimeError("No valid scene image found for thumbnail.")


def find_font(size, bold=True):
    if bold:
        candidates = (
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        )
    else:
        candidates = (
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
        )

    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            pass

    return ImageFont.load_default()


def crop_to_ratio(image, ratio):
    width, height = image.size
    if width / height > ratio:
        new_width = max(1, round(height * ratio))
        left = (width - new_width) // 2
        return image.crop((left, 0, left + new_width, height))

    new_height = max(1, round(width / ratio))
    top = (height - new_height) // 2
    return image.crop((0, top, width, top + new_height))


def fit_lines(draw, title, font, max_width):
    words = title.split()
    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word
        box = draw.textbbox((0, 0), candidate, font=font, stroke_width=2)
        if box[2] - box[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines or [title]


def fit_title(draw, title, max_width, max_lines, start_size):
    size = start_size
    while size >= 24:
        font = find_font(size)
        lines = fit_lines(draw, title, font, max_width)
        if len(lines) <= max_lines:
            return font, lines, size
        size -= 3

    font = find_font(24)
    lines = fit_lines(draw, title, font, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textbbox(
            (0, 0), last + "…", font=font, stroke_width=2
        )[2] > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"

    return font, lines, 24


def image_palette_adjustment(image, palette):
    """Use the scene's overall brightness to tune title contrast."""
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
        result["title_edge"] = (12, 12, 22)

    return result, round(brightness, 2)


def make_overlay(width, height, mode):
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Darken the title region while preserving the original scene.
    if mode == "short":
        start = int(height * 0.22)
        end = int(height * 0.53)
    else:
        start = int(height * 0.08)
        end = int(height * 0.64)

    span = max(1, end - start)
    for y in range(start, end):
        distance = min(y - start, end - y) / max(1, span / 2)
        alpha = int(42 + 72 * max(0, 1 - distance))
        draw.line((0, y, width, y), fill=(0, 0, 0, alpha))

    return overlay


def draw_frame(draw, box, radius, palette, width):
    x1, y1, x2, y2 = box
    edge = palette["board_edge"]

    draw.rounded_rectangle(
        (x1 + 6, y1 + 8, x2 + 6, y2 + 8),
        radius=radius,
        fill=(0, 0, 0, 190),
    )
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=(*palette["board"], 244),
        outline=(*edge, 255),
        width=width,
    )

    inset = max(8, width * 2)
    draw.rounded_rectangle(
        (x1 + inset, y1 + inset, x2 - inset, y2 - inset),
        radius=max(5, radius - inset),
        outline=(*edge, 180),
        width=max(1, width // 2),
    )


def render_thumbnail(source, title, mode, destination, palette):
    size = (1080, 1920) if mode == "short" else (1280, 720)
    width, height = size
    ratio = width / height

    with Image.open(source) as original:
        image = original.convert("RGB")

    image = crop_to_ratio(image, ratio).resize(
        size, Image.Resampling.LANCZOS
    )
    image = image.filter(
        ImageFilter.UnsharpMask(radius=2, percent=115, threshold=3)
    )

    palette, brightness = image_palette_adjustment(image, palette)

    base = image.convert("RGBA")
    base.alpha_composite(make_overlay(width, height, mode))
    image = base.convert("RGB")

    if mode == "short":
        plaque = (
            int(width * 0.055), int(height * 0.235),
            int(width * 0.945), int(height * 0.485),
        )
        start_size = int(width * 0.105)
        max_lines = 4
        border_width = 6
    else:
        plaque = (
            int(width * 0.235), int(height * 0.105),
            int(width * 0.765), int(height * 0.625),
        )
        start_size = int(width * 0.075)
        max_lines = 3
        border_width = 5

    x1, y1, x2, y2 = plaque
    plaque_width = x2 - x1
    plaque_height = y2 - y1

    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.rounded_rectangle(
        (x1 - 7, y1 - 7, x2 + 7, y2 + 7),
        radius=28,
        fill=(*palette["accent"], 105),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(17))
    image = Image.alpha_composite(image.convert("RGBA"), glow)
    draw = ImageDraw.Draw(image)

    draw_frame(draw, plaque, 25, palette, border_width)

    margin_x = int(plaque_width * 0.065)
    margin_y = int(plaque_height * 0.075)
    text_box = (
        x1 + margin_x, y1 + margin_y,
        x2 - margin_x, y2 - margin_y,
    )

    font, lines, chosen_size = fit_title(
        draw, title, text_box[2] - text_box[0],
        max_lines, start_size,
    )

    line_gap = max(4, chosen_size // 10)

    def measure():
        heights = []
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font, stroke_width=3)
            heights.append(max(1, box[3] - box[1]))
        return heights

    heights = measure()
    available_height = text_box[3] - text_box[1]

    while sum(heights) + line_gap * (len(lines) - 1) > available_height and chosen_size > 22:
        chosen_size -= 2
        font = find_font(chosen_size)
        line_gap = max(3, chosen_size // 10)
        heights = measure()

    total_height = sum(heights) + line_gap * (len(lines) - 1)
    y = text_box[1] + max(0, (available_height - total_height) // 2)

    for line, line_height in zip(lines, heights):
        bounds = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        text_width = bounds[2] - bounds[0]
        x = (width - text_width) // 2 - bounds[0]

        draw.text(
            (x + 5, y + 6), line, font=font,
            fill=(0, 0, 0, 255), stroke_width=7,
            stroke_fill=(0, 0, 0, 255),
        )
        draw.text(
            (x, y), line, font=font,
            fill=(*palette["title"], 255), stroke_width=4,
            stroke_fill=(*palette["title_edge"], 255),
        )
        draw.text(
            (x, y - 1), line, font=font,
            fill=(*palette["title"], 255), stroke_width=1,
            stroke_fill=(255, 255, 230, 255),
        )
        y += line_height + line_gap

    # Small story-colored corner accents.
    ornament_font = find_font(max(22, width // 25))
    for x in (
        x1 + 13,
        x2 - width // 18,
    ):
        draw.text(
            (x, y1 + 5), "✦", font=ornament_font,
            fill=(*palette["board_edge"], 255),
            stroke_width=1,
            stroke_fill=(*palette["title_edge"], 255),
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
                    f"Unexpected thumbnail size: {check.size}"
                )
        if temp.stat().st_size < 1000:
            raise RuntimeError("Thumbnail output is unexpectedly small.")
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)

    return palette, brightness, size


def main():
    config = load_and_validate(INPUT_FILE)
    mode = str(get_format(config)).strip().lower()
    mode = "full" if mode in {"full", "long", "landscape", "youtube"} else "short"

    title = load_title(config)
    topic = clean_text(config.get("TOPIC") or config.get("TITLE") or "")
    if not topic:
        try:
            topic = INPUT_FILE.read_text(
                encoding="utf-8-sig"
            )[:600]
        except OSError:
            topic = title

    palette = choose_palette(title, topic)
    source = choose_source_image()

    source_hash = sha256_file(source)
    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    topic_hash = hashlib.sha256(topic.encode("utf-8")).hexdigest()
    style_hash = hashlib.sha256(
        json.dumps(palette, sort_keys=True).encode("utf-8")
    ).hexdigest()

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

    brightness = None
    if reusable:
        final_palette = palette
        log("Reusing valid matching thumbnail.")
        with Image.open(source) as image:
            _, brightness = image_palette_adjustment(
                image.convert("RGB"), palette
            )
        final_size = expected_size
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
        "source_visual": str(source.relative_to(ROOT)),
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
        "palette_rule": "story-keywords-plus-background-brightness",
    }

    write_json(MANIFEST, result)

    log("THUMBNAIL GENERATION SUCCESS")
    log("Format:", mode)
    log("Title:", title)
    log("Selected style:", final_palette["name"])
    log("Image brightness:", brightness)
    log("Dimensions:", f"{final_size[0]}x{final_size[1]}")
    log("Thumbnail:", THUMBNAIL.relative_to(ROOT))
    log("Manifest:", MANIFEST.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"THUMBNAIL GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
