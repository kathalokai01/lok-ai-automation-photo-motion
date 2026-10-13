
#!/usr/bin/env python3
"""Create a cinematic, high-contrast Hindi story thumbnail."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_title_source

OUTPUT = ROOT / "output"
TOPIC_FILE = ROOT / "Input" / "topic.txt"
VISUAL_JOBS = OUTPUT / "visuals" / "visual_jobs.json"
IMAGE_MANIFEST = OUTPUT / "visuals" / "image_manifest.json"
TITLE_FILE = OUTPUT / "story" / "final_title.txt"

THUMB_DIR = OUTPUT / "thumbnail"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
MANIFEST = THUMB_DIR / "thumbnail_manifest.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def fail(message):
    raise RuntimeError(message)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def load_title(config):
    if TITLE_FILE.is_file():
        try:
            for line in TITLE_FILE.read_text(
                encoding="utf-8-sig"
            ).splitlines():
                title = clean_text(line, 160)
                if title:
                    return title
        except OSError as exc:
            print(f"WARNING: Cannot read title file: {exc}")

    title = clean_text(get_title_source(config), 160)
    if title:
        return title

    fail("No usable title in final_title.txt or Input/topic.txt.")


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

    for manifest_path in (IMAGE_MANIFEST, VISUAL_JOBS):
        for row in rows_from(read_json(manifest_path)):
            if not isinstance(row, dict):
                continue

            status = str(row.get("status", "")).lower()
            if status and status not in {
                "completed", "complete", "success", "done",
                "generated", "ready", "reused"
            }:
                continue

            for key in (
                "image_path", "image", "asset_path", "image_file"
            ):
                candidate = resolve_image(row.get(key))
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
                    break

    visual_dir = OUTPUT / "visuals"
    if visual_dir.is_dir():
        for path in sorted(visual_dir.glob("scene_*")):
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
            print(f"WARNING: Invalid image {path.name}: {exc}")

    fail("No valid scene image found for the thumbnail.")


def find_font(size, bold=True):
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
        ]

    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
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


def wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words:
        return ["Katha Lok AI"]

    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word
        bounds = draw.textbbox(
            (0, 0), candidate, font=font, stroke_width=2
        )

        if bounds[2] - bounds[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def fit_title(draw, title, max_width, max_lines, start_size):
    size = start_size

    while size >= 24:
        font = find_font(size)
        lines = wrap_text(draw, title, font, max_width)

        if len(lines) <= max_lines:
            return font, lines, size

        size -= 3

    font = find_font(24)
    lines = wrap_text(draw, title, font, max_width)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textbbox(
            (0, 0), last + "…", font=font, stroke_width=2
        )[2] > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"

    return font, lines, 24


def gradient_background(width, height):
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    for y in range(height):
        ratio = y / max(1, height - 1)
        alpha = int(175 * (1.0 - ratio) ** 1.25)
        draw.line(
            (0, y, width, y),
            fill=(12, 5, 15, alpha),
        )

    return layer


def draw_gold_frame(draw, box, radius, width=5):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(
        (x1 + 5, y1 + 7, x2 + 5, y2 + 7),
        radius=radius,
        fill=(0, 0, 0, 155),
    )
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=(85, 9, 10, 248),
        outline=(255, 197, 54, 255),
        width=width,
    )
    inset = max(8, width * 2)
    draw.rounded_rectangle(
        (x1 + inset, y1 + inset, x2 - inset, y2 - inset),
        radius=max(4, radius - inset),
        outline=(255, 224, 126, 230),
        width=max(1, width // 2),
    )


def draw_centered_text(draw, box, text, font, fill, stroke_fill,
                       stroke_width=3, shadow=True):
    x1, y1, x2, y2 = box
    bounds = draw.textbbox(
        (0, 0), text, font=font, stroke_width=stroke_width
    )
    tw = bounds[2] - bounds[0]
    th = bounds[3] - bounds[1]

    x = x1 + max(0, (x2 - x1 - tw) // 2) - bounds[0]
    y = y1 + max(0, (y2 - y1 - th) // 2) - bounds[1]

    if shadow:
        draw.text(
            (x + 4, y + 5),
            text,
            font=font,
            fill=(0, 0, 0, 255),
            stroke_width=stroke_width + 3,
            stroke_fill=(0, 0, 0, 255),
        )

    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def render_thumbnail(source, title, mode, destination):
    # Short thumbnails are portrait; Full thumbnails are 16:9 landscape.
    size = (1080, 1920) if mode == "short" else (1280, 720)
    width, height = size
    ratio = width / height

    with Image.open(source) as original:
        image = original.convert("RGB")

    image = crop_to_ratio(image, ratio).resize(
        size, Image.Resampling.LANCZOS
    )

    # Increase visual depth and contrast without another API call.
    image = image.filter(ImageFilter.UnsharpMask(
        radius=2, percent=125, threshold=3
    ))
    base = image.convert("RGBA")
    base.alpha_composite(gradient_background(width, height), (0, 0))

    # Warm cinematic tint along the bottom.
    warm = Image.new("RGBA", size, (0, 0, 0, 0))
    warm_draw = ImageDraw.Draw(warm)
    warm_height = int(height * 0.34)
    for y in range(warm_height):
        alpha = int(55 * (y / max(1, warm_height - 1)))
        warm_draw.line(
            (0, height - warm_height + y, width, height - warm_height + y),
            fill=(255, 107, 15, alpha),
        )
    base.alpha_composite(warm)

    image = base.convert("RGB")
    draw = ImageDraw.Draw(image)

    # The plaque stays away from the most important image edges.
    if mode == "short":
        plaque = (
            int(width * 0.055),
            int(height * 0.245),
            int(width * 0.945),
            int(height * 0.485),
        )
        title_size = int(width * 0.105)
        max_lines = 4
        border_width = 6
    else:
        plaque = (
            int(width * 0.255),
            int(height * 0.115),
            int(width * 0.745),
            int(height * 0.615),
        )
        title_size = int(width * 0.073)
        max_lines = 3
        border_width = 5

    x1, y1, x2, y2 = plaque
    plaque_width = x2 - x1
    plaque_height = y2 - y1

    # Soft outer glow gives the gold frame a cinematic shine.
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.rounded_rectangle(
        (x1 - 8, y1 - 8, x2 + 8, y2 + 8),
        radius=28,
        fill=(255, 155, 20, 100),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(16))
    image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(image)

    draw_gold_frame(draw, plaque, radius=24, width=border_width)

    # A subtle center highlight creates a richer red enamel effect.
    inner = Image.new("RGBA", (plaque_width, plaque_height), (0, 0, 0, 0))
    inner_draw = ImageDraw.Draw(inner)

    for y in range(plaque_height):
        ratio_y = y / max(1, plaque_height - 1)
        red = int(112 + 32 * (1 - abs(0.5 - ratio_y) * 2))
        inner_draw.line(
            (0, y, plaque_width, y),
            fill=(red, 7, 13, 45),
        )

    image_rgba = image.convert("RGBA")
    image_rgba.alpha_composite(inner, (x1, y1))
    image = image_rgba.convert("RGB")
    draw = ImageDraw.Draw(image)

    # Leave a small interior margin so text never touches the frame.
    text_margin_x = int(plaque_width * 0.065)
    text_margin_y = int(plaque_height * 0.07)
    text_box = (
        x1 + text_margin_x,
        y1 + text_margin_y,
        x2 - text_margin_x,
        y2 - text_margin_y,
    )

    font, lines, chosen_size = fit_title(
        draw,
        title,
        text_box[2] - text_box[0],
        max_lines,
        title_size,
    )

    line_gap = max(4, chosen_size // 10)
    heights = []
    for line in lines:
        b = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        heights.append(max(1, b[3] - b[1]))

    total_text_height = sum(heights) + line_gap * (len(lines) - 1)
    available_height = text_box[3] - text_box[1]

    # If the title is tall, shrink font before rendering.
    while total_text_height > available_height and chosen_size > 22:
        chosen_size -= 2
        font = find_font(chosen_size)
        heights = []
        for line in lines:
            b = draw.textbbox((0, 0), line, font=font, stroke_width=3)
            heights.append(max(1, b[3] - b[1]))
        line_gap = max(3, chosen_size // 10)
        total_text_height = sum(heights) + line_gap * (len(lines) - 1)

    y = text_box[1] + max(
        0, (available_height - total_text_height) // 2
    )

    for line, line_height in zip(lines, heights):
        bounds = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        text_width = bounds[2] - bounds[0]
        x = (width - text_width) // 2 - bounds[0]

        # Black extrusion + bright gold fill + dark red edge.
        draw.text(
            (x + 5, y + 6),
            line,
            font=font,
            fill=(0, 0, 0),
            stroke_width=6,
            stroke_fill=(20, 0, 0),
        )
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 207, 43),
            stroke_width=3,
            stroke_fill=(92, 25, 0),
        )
        draw.text(
            (x, y - 1),
            line,
            font=font,
            fill=(255, 224, 89),
            stroke_width=1,
            stroke_fill=(255, 246, 184),
        )

        y += line_height + line_gap

    # Fine gold corner ornaments, kept simple for mobile readability.
    ornament_font = find_font(max(22, width // 24))
    draw.text(
        (x1 + 12, y1 + 4),
        "✦",
        font=ornament_font,
        fill=(255, 214, 105),
        stroke_width=1,
        stroke_fill=(105, 35, 0),
    )
    ornament = "✦"
    ornament_bounds = draw.textbbox(
        (0, 0), ornament, font=ornament_font
    )
    draw.text(
        (x2 - (ornament_bounds[2] - ornament_bounds[0]) - 12, y1 + 4),
        ornament,
        font=ornament_font,
        fill=(255, 214, 105),
        stroke_width=1,
        stroke_fill=(105, 35, 0),
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
                fail(f"Wrong thumbnail dimensions: {check.size}")

        if temp.stat().st_size < 1000:
            fail("Thumbnail file is unexpectedly small.")

        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def main():
    config = load_and_validate(TOPIC_FILE)
    mode = get_format(config)
    title = load_title(config)

    source = choose_source_image()
    source_hash = sha256_file(source)
    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    size = (1080, 1920) if mode == "short" else (1280, 720)

    previous = read_json(MANIFEST)
    can_reuse = (
        isinstance(previous, dict)
        and previous.get("status") == "completed"
        and previous.get("format") == mode
        and previous.get("title_sha256") == title_hash
        and previous.get("source_sha256") == source_hash
        and previous.get("path") == THUMBNAIL.relative_to(ROOT).as_posix()
        and THUMBNAIL.is_file()
    )

    if can_reuse:
        try:
            with Image.open(THUMBNAIL) as image:
                image.verify()
            with Image.open(THUMBNAIL) as image:
                can_reuse = image.size == size
        except Exception:
            can_reuse = False

    if can_reuse:
        print("Reusing valid thumbnail; no image generation required.")
    else:
        render_thumbnail(source, title, mode, THUMBNAIL)

    with Image.open(THUMBNAIL) as image:
        image.verify()

    result = {
        "status": "completed",
        "format": mode,
        "title": title,
        "title_sha256": title_hash,
        "source_visual": str(source.relative_to(ROOT)),
        "source_sha256": source_hash,
        "path": str(THUMBNAIL.relative_to(ROOT)),
        "width": size[0],
        "height": size[1],
        "size_bytes": THUMBNAIL.stat().st_size,
        "reused": bool(can_reuse),
        "style": "cinematic_red_gold_hindi_plaque_v2",
    }

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    temp_manifest = MANIFEST.with_suffix(".json.tmp")

    try:
        temp_manifest.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_manifest.replace(MANIFEST)
    finally:
        temp_manifest.unlink(missing_ok=True)

    print("THUMBNAIL GENERATION SUCCESS")
    print("Format:", mode)
    print("Dimensions:", f"{size[0]}x{size[1]}")
    print("Thumbnail:", THUMBNAIL.relative_to(ROOT))
    print("Manifest:", MANIFEST.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"THUMBNAIL GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
