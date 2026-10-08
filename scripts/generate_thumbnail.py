#!/usr/bin/env python3

import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter


TOPIC_FILE = Path("Input/topic.txt")
VISUAL_MANIFEST = Path("output/visuals/visual_jobs.json")
OUTPUT_DIR = Path("output/thumbnail")

THUMBNAIL_16_9 = OUTPUT_DIR / "thumbnail_1280x720.jpg"
THUMBNAIL_VERTICAL = OUTPUT_DIR / "thumbnail_1080x1920.jpg"
THUMBNAIL_MANIFEST = OUTPUT_DIR / "thumbnail_manifest.json"


def load_topic():
    if not TOPIC_FILE.exists():
        raise SystemExit(f"ERROR: Topic file not found: {TOPIC_FILE}")

    for line in TOPIC_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        if key.strip() == "TOPIC":
            value = value.strip()

            value = re.sub(
                r'^(["\']).*\1$',
                lambda m: m.group(0)[1:-1],
                value,
            )

            if value:
                return value

    raise SystemExit("ERROR: TOPIC not found in Input/topic.txt")


def load_visual():
    if not VISUAL_MANIFEST.exists():
        raise SystemExit(
            f"ERROR: Visual manifest not found: {VISUAL_MANIFEST}"
        )

    data = json.loads(
        VISUAL_MANIFEST.read_text(encoding="utf-8")
    )

    jobs = []

    if isinstance(data, list):
        jobs = data

    elif isinstance(data, dict):
        for key in ("jobs", "scenes", "visuals", "items"):
            value = data.get(key)

            if isinstance(value, list):
                jobs = value
                break

    for job in jobs:
        if not isinstance(job, dict):
            continue

        status = str(job.get("status", "")).lower()

        asset = (
            job.get("asset_path")
            or job.get("output")
            or job.get("file")
            or job.get("path")
        )

        if not asset:
            continue

        if status and status not in (
            "completed",
            "complete",
            "success",
            "done",
            "generated",
        ):
            continue

        path = Path(str(asset))

        if not path.is_absolute():
            path = Path(".") / path

        if path.exists() and path.is_file():
            return path

    # Fallback: find any generated image.
    search_dirs = [
        Path("output/visuals"),
        Path("output"),
    ]

    extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
    }

    for directory in search_dirs:
        if not directory.exists():
            continue

        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and path.suffix.lower() in extensions
            ):
                return path

    raise SystemExit(
        "ERROR: No completed visual image found."
    )


def find_font(size):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    for font_path in candidates:
        path = Path(font_path)

        if path.exists():
            return ImageFont.truetype(
                str(path),
                size,
            )

    raise SystemExit(
        "ERROR: No suitable font found."
    )


def crop_to_ratio(image, target_ratio):
    width, height = image.size
    current_ratio = width / height

    if current_ratio > target_ratio:
        new_width = int(height * target_ratio)
        left = (width - new_width) // 2

        return image.crop(
            (
                left,
                0,
                left + new_width,
                height,
            )
        )

    new_height = int(width / target_ratio)
    top = (height - new_height) // 2

    return image.crop(
        (
            0,
            top,
            width,
            top + new_height,
        )
    )


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = (
            word
            if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            test,
            font=font,
        )

        width = bbox[2] - bbox[0]

        if width <= max_width:
            current = test
        else:
            if current:
                lines.append(current)

            current = word

    if current:
        lines.append(current)

    return lines


def add_text(image, title, vertical=False):
    image = image.convert("RGB")

    width, height = image.size

    overlay = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0),
    )

    draw_overlay = ImageDraw.Draw(overlay)

    # Dark gradient-style overlay.
    overlay_height = int(height * 0.48)

    for y in range(overlay_height):
        alpha = int(
            210 * (1 - y / overlay_height)
        )

        draw_overlay.line(
            [(0, y), (width, y)],
            fill=(0, 0, 0, alpha),
        )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        overlay,
    )

    image = image.convert("RGB")

    draw = ImageDraw.Draw(image)

    if vertical:
        font_size = max(54, width // 16)
        max_text_width = int(width * 0.86)
        text_y = int(height * 0.10)
    else:
        font_size = max(44, width // 17)
        max_text_width = int(width * 0.84)
        text_y = int(height * 0.09)

    font = find_font(font_size)

    lines = wrap_text(
        draw,
        title,
        font,
        max_text_width,
    )

    # Limit title to 4 lines.
    if len(lines) > 4:
        lines = lines[:4]

        last = lines[-1]

        if len(last) > 3:
            lines[-1] = last[:-3] + "..."

    line_spacing = int(font_size * 0.18)

    y = text_y

    for line in lines:
        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font,
            stroke_width=2,
        )

        text_width = bbox[2] - bbox[0]

        x = (width - text_width) // 2

        # Shadow.
        draw.text(
            (x + 4, y + 4),
            line,
            font=font,
            fill=(0, 0, 0),
            stroke_width=5,
            stroke_fill=(0, 0, 0),
        )

        # Main text.
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )

        y += font_size + line_spacing

    return image


def create_thumbnail(source, title, size, output):
    image = Image.open(source).convert("RGB")

    target_ratio = size[0] / size[1]

    image = crop_to_ratio(
        image,
        target_ratio,
    )

    image = image.resize(
        size,
        Image.Resampling.LANCZOS,
    )

    vertical = size[1] > size[0]

    image = add_text(
        image,
        title,
        vertical=vertical,
    )

    image.save(
        output,
        "JPEG",
        quality=92,
        optimize=True,
    )

    if not output.exists() or output.stat().st_size == 0:
        raise SystemExit(
            f"ERROR: Thumbnail was not created: {output}"
        )


def main():
    print("======================================")
    print("       GENERATING THUMBNAILS")
    print("======================================")

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    topic = load_topic()

    print(f"Topic: {topic}")

    source = load_visual()

    print(f"Source visual: {source}")

    create_thumbnail(
        source,
        topic,
        (1280, 720),
        THUMBNAIL_16_9,
    )

    create_thumbnail(
        source,
        topic,
        (1080, 1920),
        THUMBNAIL_VERTICAL,
    )

    manifest = {
        "status": "completed",
        "source_visual": str(source),
        "topic": topic,
        "thumbnails": [
            {
                "type": "16:9",
                "width": 1280,
                "height": 720,
                "path": str(THUMBNAIL_16_9),
            },
            {
                "type": "9:16",
                "width": 1080,
                "height": 1920,
                "path": str(THUMBNAIL_VERTICAL),
            },
        ],
    }

    THUMBNAIL_MANIFEST.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print()
    print("===== GENERATED THUMBNAILS =====")
    print(f"16:9  : {THUMBNAIL_16_9}")
    print(f"9:16  : {THUMBNAIL_VERTICAL}")
    print(f"Manifest: {THUMBNAIL_MANIFEST}")
    print()
    print("Thumbnail generation completed successfully.")


if __name__ == "__main__":
    main()
