
#!/usr/bin/env python3
"""Generate one validated, topic-based thumbnail for the requested format."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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
        title = clean_text(
            TITLE_FILE.read_text(encoding="utf-8-sig").splitlines()[0],
            160,
        )
        if title:
            return title

    return clean_text(get_title_source(config), 160)


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
        data = read_json(manifest_path)

        for row in rows_from(data):
            if not isinstance(row, dict):
                continue

            status = str(row.get("status", "")).lower()
            if status and status not in {
                "completed", "complete", "success", "done",
                "generated", "ready", "reused"
            }:
                continue

            image_path = None
            for key in ("image_path", "image", "asset_path", "image_file"):
                image_path = resolve_image(row.get(key))
                if image_path:
                    break

            if image_path and image_path not in candidates:
                candidates.append(image_path)

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
            print(f"WARNING: Skipping invalid image {path.name}: {exc}")

    fail("No valid generated scene image is available for the thumbnail.")


def find_font(size):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
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
    lines = []
    current = ""

    for word in text.split():
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

    return lines or ["Katha Lok AI"]


def render_thumbnail(source, title, mode, destination):
    size = (1080, 1920) if mode == "short" else (1280, 720)
    ratio = size[0] / size[1]

    with Image.open(source) as original:
        image = original.convert("RGB")

    image = crop_to_ratio(image, ratio).resize(
        size, Image.Resampling.LANCZOS
    )

    width, height = size
    panel_height = int(height * (0.34 if mode == "short" else 0.43))

    base = image.convert("RGBA")
    overlay = Image.new("RGBA", (width, panel_height))
    draw_overlay = ImageDraw.Draw(overlay)

    for y in range(panel_height):
        alpha = int(220 * (1 - y / max(1, panel_height)))
        draw_overlay.line(
            (0, y, width, y),
            fill=(0, 0, 0, alpha),
        )

    base.alpha_composite(overlay, (0, 0))
    image = base.convert("RGB")
    draw = ImageDraw.Draw(image)

    font_size = max(26, width // (17 if mode == "short" else 18))
    max_width = int(width * 0.86)

    while True:
        font = find_font(font_size)
        lines = wrap_text(draw, title, font, max_width)
        if len(lines) <= 4 or font_size <= 22:
            break
        font_size -= 2

    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip() + "…"

    spacing = max(5, font_size // 6)
    line_sizes = []

    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        line_sizes.append(max(1, bounds[3] - bounds[1]))

    total_height = sum(line_sizes) + spacing * (len(lines) - 1)
    y = max(12, (panel_height - total_height) // 2)

    for line, line_height in zip(lines, line_sizes):
        bounds = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        text_width = bounds[2] - bounds[0]
        x = max(8, (width - text_width) // 2)

        draw.text(
            (x + 3, y + 3), line, font=font,
            fill="black", stroke_width=5, stroke_fill="black",
        )
        draw.text(
            (x, y), line, font=font,
            fill="white", stroke_width=2, stroke_fill="black",
        )
        y += line_height + spacing

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.jpg")

    try:
        image.save(temp, "JPEG", quality=92, optimize=True)

        with Image.open(temp) as check:
            check.verify()

        with Image.open(temp) as check:
            if check.size != size:
                fail(f"Thumbnail dimensions incorrect: {check.size}")

        if temp.stat().st_size < 1000:
            fail("Generated thumbnail is unexpectedly small.")

        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def main():
    config = load_and_validate(TOPIC_FILE)
    mode = get_format(config)
    title = load_title(config)

    if not title:
        fail("A topic-based title is required.")

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
        and previous.get("path") == str(THUMBNAIL.relative_to(ROOT))
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
        print("Reusing validated thumbnail; no regeneration needed.")
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
    }

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    temp_manifest = MANIFEST.with_suffix(".json.tmp")
    temp_manifest.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_manifest.replace(MANIFEST)

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
