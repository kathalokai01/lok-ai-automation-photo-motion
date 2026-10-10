
#!/usr/bin/env python3
"""Generate validated landscape and vertical Katha Lok AI thumbnails."""

import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
TOPIC_FILE = ROOT / "Input" / "topic.txt"
VISUAL_JOBS = OUTPUT / "visuals" / "visual_jobs.json"
IMAGE_MANIFEST = OUTPUT / "visuals" / "image_manifest.json"
TITLE_FILE = OUTPUT / "story" / "final_title.txt"

THUMB_DIR = OUTPUT / "thumbnail"
LANDSCAPE = THUMB_DIR / "thumbnail_1280x720.jpg"
VERTICAL = THUMB_DIR / "thumbnail_1080x1920.jpg"
MANIFEST = THUMB_DIR / "thumbnail_manifest.json"

# Also create the common paths checked by the workflow.
LANDSCAPE_ALIASES = [
    OUTPUT / "thumbnail.jpg",
    OUTPUT / "thumbnail_16_9.jpg",
]
VERTICAL_ALIASES = [
    OUTPUT / "thumbnail_vertical.jpg",
]

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"WARNING: Could not read {path}: {exc}")
        return None


def load_topic():
    if not TOPIC_FILE.is_file():
        raise RuntimeError(f"Missing input file: {TOPIC_FILE}")

    for line in TOPIC_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip().upper() not in {"TOPIC", "TITLE", "STORY_TOPIC"}:
                continue
            value = value.split("#", 1)[0].strip().strip("'\"")
        else:
            value = line.strip().strip("'\"")

        if value:
            return re.sub(r"\s+", " ", value)[:300]

    raise RuntimeError("No topic found in Input/topic.txt")


def load_title(topic):
    if TITLE_FILE.is_file():
        try:
            title = TITLE_FILE.read_text(encoding="utf-8-sig").strip()
            if title:
                return re.sub(r"\s+", " ", title.splitlines()[0])[:160]
        except OSError as exc:
            print(f"WARNING: Could not read story title: {exc}")
    return topic


def get_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("scenes", "jobs", "visuals", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
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
    for manifest_path in (IMAGE_MANIFEST, VISUAL_JOBS):
        data = read_json(manifest_path)
        candidates = []

        for row in get_rows(data):
            if not isinstance(row, dict):
                continue

            status = str(row.get("status", "")).lower()
            if status and status not in {
                "completed", "complete", "success", "done",
                "generated", "ready"
            }:
                continue

            for key in ("image_path", "image", "asset_path", "image_file"):
                candidate = resolve_image(row.get(key))
                if candidate:
                    candidates.append(candidate)
                    break

        for path in candidates:
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    if image.width >= 64 and image.height >= 64:
                        return path
            except Exception as exc:
                print(f"WARNING: Skipping invalid image {path}: {exc}")

    # Fallback to numbered scene images only.
    visual_dir = OUTPUT / "visuals"
    if visual_dir.is_dir():
        for path in sorted(visual_dir.glob("scene_*")):
            candidate = resolve_image(str(path))
            if not candidate:
                continue
            try:
                with Image.open(candidate) as image:
                    image.verify()
                with Image.open(candidate) as image:
                    if image.width >= 64 and image.height >= 64:
                        return candidate
            except Exception:
                continue

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
    for filename in candidates:
        try:
            return ImageFont.truetype(filename, size=size)
        except OSError:
            continue
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
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
    lines = []
    current = ""

    for word in words:
        proposed = word if not current else current + " " + word
        bounds = draw.textbbox((0, 0), proposed, font=font, stroke_width=2)
        if bounds[2] - bounds[0] <= max_width:
            current = proposed
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)
    return lines or ["Katha Lok AI"]


def render_thumbnail(source, title, size, destination):
    with Image.open(source) as original:
        image = original.convert("RGB")

    image = crop_to_ratio(image, size[0] / size[1])
    image = image.resize(size, Image.Resampling.LANCZOS)

    width, height = size
    panel_height = int(height * (0.42 if width > height else 0.31))

    overlay = Image.new("RGBA", (width, panel_height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    for y in range(panel_height):
        alpha = int(210 * (1 - y / max(1, panel_height)))
        overlay_draw.line(
            (0, y, width, y),
            fill=(0, 0, 0, alpha),
        )

    base = image.convert("RGBA")
    base.alpha_composite(overlay, (0, 0))
    image = base.convert("RGB")
    draw = ImageDraw.Draw(image)

    font_size = max(26, width // (15 if height > width else 18))
    font = find_font(font_size)
    max_text_width = int(width * 0.86)
    lines = wrap_text(draw, title, font, max_text_width)

    while len(lines) > 4 and font_size > 20:
        font_size -= 2
        font = find_font(font_size)
        lines = wrap_text(draw, title, font, max_text_width)

    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip() + "…"

    spacing = max(4, font_size // 6)
    heights = []
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        heights.append(max(1, box[3] - box[1]))

    total_height = sum(heights) + spacing * max(0, len(lines) - 1)
    y = max(12, (panel_height - total_height) // 2)

    for line, line_height in zip(lines, heights):
        bounds = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        text_width = bounds[2] - bounds[0]
        x = max(8, (width - text_width) // 2)

        draw.text(
            (x + 3, y + 3),
            line,
            font=font,
            fill="black",
            stroke_width=5,
            stroke_fill="black",
        )
        draw.text(
            (x, y),
            line,
            font=font,
            fill="white",
            stroke_width=2,
            stroke_fill="black",
        )
        y += line_height + spacing

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "JPEG", quality=92, optimize=True)

    with Image.open(destination) as check:
        check.verify()

    if destination.stat().st_size < 1000:
        raise RuntimeError(f"Thumbnail is unexpectedly small: {destination}")

    with Image.open(destination) as check:
        if check.size != size:
            raise RuntimeError(
                f"Wrong thumbnail dimensions in {destination}: {check.size}"
            )


def copy_alias(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.convert("RGB").save(
            destination, "JPEG", quality=92, optimize=True
        )

    with Image.open(destination) as check:
        check.verify()

    if destination.stat().st_size < 1000:
        raise RuntimeError(f"Thumbnail alias is invalid: {destination}")


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    topic = load_topic()
    title = load_title(topic)
    source = choose_source_image()

    print("===== KATHA LOK AI THUMBNAILS =====")
    print("Topic:", topic)
    print("Thumbnail title:", title)
    print("Source image:", source)

    render_thumbnail(source, title, (1280, 720), LANDSCAPE)
    render_thumbnail(source, title, (1080, 1920), VERTICAL)

    for alias in LANDSCAPE_ALIASES:
        copy_alias(LANDSCAPE, alias)
    for alias in VERTICAL_ALIASES:
        copy_alias(VERTICAL, alias)

    result = {
        "status": "completed",
        "topic": topic,
        "title": title,
        "source_visual": str(source.relative_to(ROOT)),
        "thumbnails": [
            {
                "type": "16:9",
                "width": 1280,
                "height": 720,
                "path": str(LANDSCAPE.relative_to(ROOT)),
                "size_bytes": LANDSCAPE.stat().st_size,
            },
            {
                "type": "9:16",
                "width": 1080,
                "height": 1920,
                "path": str(VERTICAL.relative_to(ROOT)),
                "size_bytes": VERTICAL.stat().st_size,
            },
        ],
        "workflow_aliases": [
            str(path.relative_to(ROOT))
            for path in LANDSCAPE_ALIASES + VERTICAL_ALIASES
        ],
    }

    MANIFEST.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("Landscape thumbnail:", LANDSCAPE)
    print("Vertical thumbnail:", VERTICAL)
    print("Workflow-compatible aliases created.")
    print("Manifest:", MANIFEST)
    print("THUMBNAIL GENERATION SUCCESS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"THUMBNAIL GENERATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
