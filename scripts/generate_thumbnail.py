#!/usr/bin/env python3
"""Create validated, story-specific thumbnails for Katha Lok AI."""

import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(".")
TOPIC_FILE = ROOT / "Input/topic.txt"
VISUAL_JOBS = ROOT / "output/visuals/visual_jobs.json"
IMAGE_MANIFEST = ROOT / "output/visuals/image_manifest.json"

OUTPUT_DIR = ROOT / "output/thumbnail"
LANDSCAPE = OUTPUT_DIR / "thumbnail_1280x720.jpg"
VERTICAL = OUTPUT_DIR / "thumbnail_1080x1920.jpg"
MANIFEST = OUTPUT_DIR / "thumbnail_manifest.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def load_topic():
    if not TOPIC_FILE.is_file():
        raise RuntimeError(f"Missing input file: {TOPIC_FILE}")

    for line in TOPIC_FILE.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*TOPIC\s*=\s*(.*?)\s*$", line)
        if not match:
            continue

        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        value = value.split(" #", 1)[0].strip()
        if value:
            return value

    raise RuntimeError("TOPIC is missing or empty in Input/topic.txt")


def read_json(path):
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Cannot read {path}: {exc}")
        return None


def resolve_image(value):
    if not isinstance(value, str) or not value.strip():
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()
        output_root = (ROOT / "output").resolve()
        path.relative_to(output_root)
    except (ValueError, OSError):
        return None

    if (
        path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
        and "depth_maps" not in path.parts
        and "thumbnail" not in path.parts
    ):
        return path

    return None


def rows_from_manifest(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("scenes", "jobs", "visuals", "items"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows

    return []


def choose_source_image():
    # Prefer the generated image manifest, which identifies actual scene images.
    candidates = []

    for manifest_path in (IMAGE_MANIFEST, VISUAL_JOBS):
        data = read_json(manifest_path)

        for row in rows_from_manifest(data):
            if not isinstance(row, dict):
                continue

            status = str(row.get("status", "")).lower()
            if status and status not in {
                "completed", "complete", "success", "done", "generated", "ready"
            }:
                continue

            # Prefer image-specific fields; never use depth_map as the source.
            for key in ("image_path", "image", "asset_path", "image_file"):
                path = resolve_image(row.get(key))
                if path:
                    candidates.append(path)
                    break

        if candidates:
            break

    # Fallback only to numbered scene images, never arbitrary output images.
    if not candidates:
        visual_dir = ROOT / "output/visuals"
        if visual_dir.is_dir():
            for path in sorted(visual_dir.glob("scene_*")):
                resolved = resolve_image(str(path))
                if resolved:
                    candidates.append(resolved)

    # Verify the selected image is decodable and non-empty.
    for path in candidates:
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                if im.width >= 64 and im.height >= 64:
                    return path
        except Exception as exc:
            print(f"WARNING: Skipping invalid image {path}: {exc}")

    raise RuntimeError(
        "No valid scene image found. Check image_manifest.json and visual_jobs.json."
    )


def find_font(size):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass

    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def crop_to_ratio(image, ratio):
    width, height = image.size
    current = width / height

    if current > ratio:
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
        proposed = word if not current else current + " " + word
        bounds = draw.textbbox((0, 0), proposed, font=font, stroke_width=1)

        if bounds[2] - bounds[0] <= max_width:
            current = proposed
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def draw_thumbnail(source, title, size, destination):
    with Image.open(source) as original:
        image = original.convert("RGB")

    image = crop_to_ratio(image, size[0] / size[1])
    image = image.resize(size, Image.Resampling.LANCZOS)

    width, height = image.size
    draw = ImageDraw.Draw(image)

    # Add a dark translucent title panel for readable text.
    panel_height = int(height * (0.42 if width > height else 0.31))
    panel = Image.new("RGBA", (width, panel_height), (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)

    for y in range(panel_height):
        alpha = int(205 * (1 - y / max(1, panel_height)))
        panel_draw.line((0, y, width, y), fill=(0, 0, 0, alpha))

    image_rgba = image.convert("RGBA")
    image_rgba.alpha_composite(panel, (0, 0))
    image = image_rgba.convert("RGB")
    draw = ImageDraw.Draw(image)

    vertical = height > width
    font_size = max(26, width // (15 if vertical else 18))
    max_text_width = int(width * 0.86)
    font = find_font(font_size)

    lines = wrap_text(draw, title, font, max_text_width)

    # Keep long titles readable without allowing an unbounded text block.
    if len(lines) > 4:
        lines = lines[:4]
        last = lines[-1]
        while last and draw.textbbox((0, 0), last + "…", font=font)[2] > max_text_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"

    spacing = max(4, font_size // 6)
    line_heights = [
        draw.textbbox((0, 0), line, font=font, stroke_width=2)[3]
        - draw.textbbox((0, 0), line, font=font, stroke_width=2)[1]
        for line in lines
    ]

    total_height = sum(line_heights) + spacing * max(0, len(lines) - 1)
    y = max(16, (panel_height - total_height) // 2)

    for line, line_height in zip(lines, line_heights):
        bounds = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        text_width = bounds[2] - bounds[0]
        x = max(8, (width - text_width) // 2)

        draw.text(
            (x + 3, y + 3), line, font=font,
            fill="black", stroke_width=5, stroke_fill="black"
        )
        draw.text(
            (x, y), line, font=font,
            fill="white", stroke_width=2, stroke_fill="black"
        )
        y += line_height + spacing

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "JPEG", quality=92, optimize=True)

    # Verify the saved output, not just its existence.
    with Image.open(destination) as check:
        check.verify()

    if destination.stat().st_size < 1000:
        raise RuntimeError(f"Thumbnail file is unexpectedly small: {destination}")


def main():
    print("===== KATHA LOK AI THUMBNAILS =====")

    topic = load_topic()
    source = choose_source_image()

    print(f"Topic: {topic}")
    print(f"Source scene image: {source}")

    draw_thumbnail(source, topic, (1280, 720), LANDSCAPE)
    draw_thumbnail(source, topic, (1080, 1920), VERTICAL)

    result = {
        "status": "completed",
        "topic": topic,
        "source_visual": str(source),
        "thumbnails": [
            {
                "type": "16:9",
                "width": 1280,
                "height": 720,
                "path": str(LANDSCAPE),
                "size_bytes": LANDSCAPE.stat().st_size,
            },
            {
                "type": "9:16",
                "width": 1080,
                "height": 1920,
                "path": str(VERTICAL),
                "size_bytes": VERTICAL.stat().st_size,
            },
        ],
    }

    MANIFEST.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Landscape thumbnail: {LANDSCAPE}")
    print(f"Vertical thumbnail: {VERTICAL}")
    print(f"Thumbnail manifest: {MANIFEST}")
    print("Thumbnail generation completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)