
#!/usr/bin/env python3
"""Create a topic-based thumbnail using existing local images only."""

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

OUTPUT = ROOT / "output"
VISUAL_DIR = OUTPUT / "visuals"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
THUMBNAIL_DIR = OUTPUT / "thumbnail"
MANIFEST = THUMBNAIL_DIR / "thumbnail_manifest.json"

FONT_DIRS = (
    ROOT / "assets" / "fonts",
    Path("/usr/share/fonts/truetype"),
    Path("/usr/local/share/fonts"),
)

FONT_CANDIDATES = {
    "regular": [
        "NotoSansDevanagari-Regular.ttf",
        "Mukta-Regular.ttf",
        "Hind-Regular.ttf",
    ],
    "bold": [
        "NotoSansDevanagari-Bold.ttf",
        "Mukta-Bold.ttf",
        "Hind-Bold.ttf",
    ],
}

MIN_IMAGE_BYTES = 1000


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_existing_file(value):
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
        return path
    except OSError:
        return None


def find_font(names):
    for base in FONT_DIRS:
        if not base.exists():
            continue
        for name in names:
            direct = base / name
            if direct.is_file():
                return direct
            try:
                matches = list(base.rglob(name))
            except OSError:
                matches = []
            if matches:
                return matches[0]
    return None


def load_font(size, bold=False):
    names = FONT_CANDIDATES["bold" if bold else "regular"]
    font_path = find_font(names)
    if not font_path:
        raise RuntimeError(
            "Hindi font not found. Run install_caption_fonts.py first."
        )

    try:
        return ImageFont.truetype(str(font_path), size=size, layout_engine=ImageFont.Layout.RAQM)
    except (OSError, AttributeError) as exc:
        raise RuntimeError(
            f"Cannot load Hindi font with proper text shaping: {font_path}"
        ) from exc


def get_title(config):
    title = str(get_title_source(config) or "").strip()
    if not title:
        title_file = OUTPUT / "story" / "final_title.txt"
        if title_file.is_file():
            title = title_file.read_text(encoding="utf-8-sig").splitlines()[0].strip()

    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        raise RuntimeError(
            "No title found. Set TOPIC or provide a story title in Input/topic.txt."
        )

    # Avoid accidentally placing the full story on the thumbnail.
    return title[:160]


def collect_images():
    candidates = []

    manifest_paths = [
        VISUAL_DIR / "image_manifest.json",
        VISUAL_DIR / "visual_jobs.json",
        VISUAL_DIR / "images_manifest.json",
    ]

    for manifest_path in manifest_paths:
        data = read_json(manifest_path)
        if data is None:
            continue

        rows = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ("images", "jobs", "visual_jobs", "completed_images", "items"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break

        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("image_path", "image", "image_file", "output_image", "path"):
                image_path = safe_existing_file(row.get(key))
                if image_path and image_path.suffix.lower() in (
                    ".png", ".jpg", ".jpeg", ".webp", ".bmp"
                ):
                    candidates.append(image_path)
                    break

    # Fallback: reuse local images if the manifest has no explicit image path.
    if not candidates and VISUAL_DIR.exists():
        for path in sorted(VISUAL_DIR.rglob("*")):
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                continue
            if "depth" in str(path).lower() or "thumbnail" in str(path).lower():
                continue
            image_path = safe_existing_file(str(path))
            if image_path:
                candidates.append(image_path)

    unique = []
    seen = set()
    for path in candidates:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)

    return unique


def fit_cover(image, width, height):
    image = image.convert("RGB")
    source_ratio = image.width / image.height
    target_ratio = width / height

    if source_ratio > target_ratio:
        new_height = height
        new_width = round(height * source_ratio)
    else:
        new_width = width
        new_height = round(width / source_ratio)

    image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    left = (new_width - width) // 2
    top = (new_height - height) // 2
    return image.crop((left, top, left + width, top + height))


def wrap_title(draw, title, font, max_width, max_lines):
    words = title.split()
    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), candidate, font=font, stroke_width=0)
        if box[2] - box[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    if len(lines) > max_lines:
        # Try narrower font sizes before dropping any title words.
        return None

    return lines


def choose_title_layout(draw, title, width, height, max_lines):
    max_text_width = int(width * 0.84)
    initial_size = int(height * (0.065 if height > width else 0.105))
    minimum_size = max(24, int(height * 0.035))

    for size in range(initial_size, minimum_size - 1, -2):
        font = load_font(size, bold=True)
        lines = wrap_title(draw, title, font, max_text_width, max_lines)
        if lines:
            line_heights = []
            line_widths = []
            for line in lines:
                box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
                line_widths.append(box[2] - box[0])
                line_heights.append(box[3] - box[1])

            if max(line_widths, default=0) <= max_text_width:
                return font, lines, line_heights

    # Long titles get a compact layout; all title words are retained.
    font = load_font(minimum_size, bold=True)
    words = title.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > 0 and draw.textbbox((0, 0), candidate, font=font)[2] <= width * 0.90:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    return font, lines[:max_lines], [
        draw.textbbox((0, 0), line, font=font)[3]
        - draw.textbbox((0, 0), line, font=font)[1]
        for line in lines[:max_lines]
    ]


def create_thumbnail(title, mode, image_path):
    width, height = (1080, 1920) if mode == "short" else (1280, 720)

    if image_path:
        with Image.open(image_path) as source:
            background = fit_cover(source, width, height)
    else:
        # No external image generation: a dark, textured fallback background.
        background = Image.new("RGB", (width, height), (22, 24, 34))

    background = ImageEnhance.Color(background).enhance(0.9)
    background = ImageEnhance.Contrast(background).enhance(1.15)

    # A subtle dark overlay improves text contrast without covering the image.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    panel_top = int(height * (0.55 if mode == "short" else 0.45))
    for y in range(panel_top, height):
        alpha = int(20 + 205 * ((y - panel_top) / max(1, height - panel_top)))
        overlay_draw.line((0, y, width, y), fill=(0, 0, 0, min(alpha, 225)))
    background = Image.alpha_composite(background.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(background)
    max_lines = 4 if mode == "short" else 3
    font, lines, line_heights = choose_title_layout(
        draw, title, width, height, max_lines
    )

    line_gap = max(10, int(height * 0.012))
    total_height = sum(line_heights) + line_gap * max(0, len(lines) - 1)
    text_y = max(panel_top + 12, height - total_height - int(height * 0.075))

    plaque_margin = int(width * 0.055)
    plaque_left = plaque_margin
    plaque_right = width - plaque_margin
    plaque_top = max(panel_top, text_y - int(height * 0.035))
    plaque_bottom = min(height - int(height * 0.025), text_y + total_height + int(height * 0.04))

    # Soft shadow and a dark translucent plaque.
    plaque = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    plaque_draw = ImageDraw.Draw(plaque)
    plaque_draw.rounded_rectangle(
        (plaque_left + 6, plaque_top + 8, plaque_right + 6, plaque_bottom + 8),
        radius=max(16, int(width * 0.02)),
        fill=(0, 0, 0, 120),
    )
    plaque = plaque.filter(ImageFilter.GaussianBlur(radius=max(4, width // 180)))
    background = Image.alpha_composite(background.convert("RGBA"), plaque)

    plaque_draw = ImageDraw.Draw(background)
    plaque_draw.rounded_rectangle(
        (plaque_left, plaque_top, plaque_right, plaque_bottom),
        radius=max(16, int(width * 0.02)),
        fill=(10, 12, 18, 205),
        outline=(238, 190, 94, 230),
        width=max(2, width // 360),
    )

    draw = ImageDraw.Draw(background)
    y = text_y
    for line, line_height in zip(lines, line_heights):
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        text_width = box[2] - box[0]
        x = (width - text_width) // 2

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
        y += line_height + line_gap

    THUMBNAIL.parent.mkdir(parents=True, exist_ok=True)
    temporary = THUMBNAIL.with_suffix(".tmp.jpg")
    background.convert("RGB").save(
        temporary,
        format="JPEG",
        quality=93,
        optimize=True,
        progressive=True,
    )
    temporary.replace(THUMBNAIL)

    with Image.open(THUMBNAIL) as check:
        check.verify()
    with Image.open(THUMBNAIL) as check:
        if check.size != (width, height):
            raise RuntimeError("Thumbnail dimensions failed verification")

    return width, height, font.size, len(lines)


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    title = get_title(config)
    images = collect_images()
    image_path = images[0] if images else None

    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    source_hash = sha256_file(image_path) if image_path else "no-local-image"
    fingerprint = hashlib.sha256(
        f"thumbnail-v5|{mode}|{title_hash}|{source_hash}".encode("utf-8")
    ).hexdigest()

    old = read_json(MANIFEST)
    if (
        isinstance(old, dict)
        and old.get("fingerprint") == fingerprint
        and THUMBNAIL.is_file()
        and THUMBNAIL.stat().st_size >= MIN_IMAGE_BYTES
    ):
        try:
            with Image.open(THUMBNAIL) as image:
                image.verify()
            log("RESUME: existing thumbnail is valid; no regeneration needed")
            return
        except Exception:
            pass

    width, height, font_size, line_count = create_thumbnail(
        title=title,
        mode=mode,
        image_path=image_path,
    )

    write_json(
        MANIFEST,
        {
            "status": "completed",
            "fingerprint": fingerprint,
            "format": mode,
            "title": title,
            "title_sha256": title_hash,
            "background_image": str(image_path.relative_to(ROOT)) if image_path else None,
            "background_sha256": source_hash,
            "thumbnail": str(THUMBNAIL.relative_to(ROOT)),
            "width": width,
            "height": height,
            "font_size": font_size,
            "title_lines": line_count,
            "external_api_calls": 0,
        },
    )

    log("THUMBNAIL CREATED")
    log(f"FORMAT: {mode}")
    log(f"TITLE: {title}")
    log(f"SIZE: {width}x{height}")
    log(f"FONT SIZE: {font_size}")
    log(f"OUTPUT: {THUMBNAIL.relative_to(ROOT)}")
    log("EXTERNAL API CALLS: 0")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", str(exc))
        sys.exit(1)
