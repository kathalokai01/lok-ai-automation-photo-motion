
#!/usr/bin/env python3
"""Create a resumable, topic-based thumbnail without external API calls."""

from __future__ import annotations

import hashlib
import json
import re
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

MIN_IMAGE_BYTES = 1000
VERSION = "thumbnail-v6"

FONT_DIRS = (
    ROOT / "assets" / "fonts",
    Path("/usr/share/fonts/truetype"),
    Path("/usr/local/share/fonts"),
)

FONT_CANDIDATES = {
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
            ".png", ".jpg", ".jpeg", ".webp", ".bmp"
        }:
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
    path = find_font(FONT_CANDIDATES[bold])
    if path is None:
        raise RuntimeError(
            "Hindi font not found. Run scripts/install_caption_fonts.py first."
        )

    try:
        return ImageFont.truetype(
            str(path),
            size=size,
            layout_engine=ImageFont.Layout.RAQM,
        )
    except (OSError, AttributeError) as exc:
        raise RuntimeError(
            f"Cannot load Hindi font with proper shaping: {path}"
        ) from exc


def get_title(config):
    title = str(get_title_source(config) or "").strip()

    if not title:
        title_file = OUTPUT / "story" / "final_title.txt"
        if title_file.is_file():
            lines = title_file.read_text(
                encoding="utf-8-sig"
            ).splitlines()
            title = lines[0].strip() if lines else ""

    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        raise RuntimeError(
            "Title missing. Set TOPIC or provide a story title."
        )

    # Never use the entire story as a thumbnail title.
    return title[:160]


def collect_images():
    candidates = []

    manifest_paths = (
        VISUAL_DIR / "image_manifest.json",
        VISUAL_DIR / "visual_jobs.json",
        VISUAL_DIR / "images_manifest.json",
    )

    fields = (
        "image_path", "image", "image_file", "output_image", "path"
    )
    row_keys = (
        "images", "jobs", "visual_jobs", "completed_images", "items"
    )

    for manifest_path in manifest_paths:
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
            for field in fields:
                image_path = safe_image(row.get(field))
                if image_path:
                    candidates.append(image_path)
                    break

    if not candidates and VISUAL_DIR.exists():
        for path in sorted(VISUAL_DIR.rglob("*")):
            if "depth" in str(path).lower():
                continue
            image_path = safe_image(str(path))
            if image_path:
                candidates.append(image_path)

    unique = []
    seen = set()

    for path in candidates:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
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

    image = image.resize(
        (new_width, new_height),
        Image.Resampling.LANCZOS,
    )
    left = (new_width - width) // 2
    top = (new_height - height) // 2
    return image.crop((left, top, left + width, top + height))


def text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=2)
    return box[2] - box[0]


def wrap_title(draw, title, font, max_width):
    """Wrap every word; split oversized tokens instead of clipping them."""
    words = title.split()
    lines = []
    current = ""

    for original_word in words:
        word = original_word

        # If one unbroken token is wider than the canvas, split it safely.
        pieces = []
        while word and text_width(draw, word, font) > max_width:
            low, high = 1, len(word)
            best = 1

            while low <= high:
                middle = (low + high) // 2
                if text_width(draw, word[:middle], font) <= max_width:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1

            pieces.append(word[:best])
            word = word[best:]

        if word:
            pieces.append(word)

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


def choose_title_layout(draw, title, width, height, max_lines):
    max_width = int(width * 0.84)
    initial_size = int(height * (0.065 if height > width else 0.105))
    minimum_size = max(18, int(height * 0.025))

    # Prefer a readable font and a small number of lines.
    for size in range(initial_size, minimum_size - 1, -2):
        font = load_font(size, bold=True)
        lines = wrap_title(draw, title, font, max_width)

        if len(lines) <= max_lines and all(
            text_width(draw, line, font) <= max_width for line in lines
        ):
            heights = [
                draw.textbbox((0, 0), line, font=font, stroke_width=2)[3]
                - draw.textbbox((0, 0), line, font=font, stroke_width=2)[1]
                for line in lines
            ]
            return font, lines, heights

    # Extremely long titles: keep all text, with more lines at a smaller size.
    font = load_font(minimum_size, bold=True)
    lines = wrap_title(draw, title, font, int(width * 0.88))
    heights = [
        draw.textbbox((0, 0), line, font=font, stroke_width=2)[3]
        - draw.textbbox((0, 0), line, font=font, stroke_width=2)[1]
        for line in lines
    ]
    return font, lines, heights


def create_thumbnail(title, mode, image_path):
    width, height = (
        (1080, 1920) if mode == "short" else (1280, 720)
    )

    if image_path:
        with Image.open(image_path) as source:
            background = fit_cover(source, width, height)
    else:
        # Offline fallback: no paid or external image API.
        background = Image.new(
            "RGB", (width, height), (22, 24, 34)
        )

    background = ImageEnhance.Color(background).enhance(0.9)
    background = ImageEnhance.Contrast(background).enhance(1.15)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    panel_top = int(height * (0.55 if mode == "short" else 0.40))

    for y in range(panel_top, height):
        ratio = (y - panel_top) / max(1, height - panel_top)
        alpha = min(225, int(20 + 205 * ratio))
        overlay_draw.line(
            (0, y, width, y),
            fill=(0, 0, 0, alpha),
        )

    background = Image.alpha_composite(
        background.convert("RGBA"), overlay
    ).convert("RGB")

    draw = ImageDraw.Draw(background)
    max_lines = 4 if mode == "short" else 3
    font, lines, line_heights = choose_title_layout(
        draw, title, width, height, max_lines
    )

    line_gap = max(8, int(height * 0.010))
    total_height = sum(line_heights) + line_gap * max(0, len(lines) - 1)
    text_y = max(
        panel_top + 12,
        height - total_height - int(height * 0.075),
    )

    margin = int(width * 0.055)
    plaque_left = margin
    plaque_right = width - margin
    plaque_top = max(panel_top, text_y - int(height * 0.035))
    plaque_bottom = min(
        height - int(height * 0.02),
        text_y + total_height + int(height * 0.04),
    )

    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (
            plaque_left + 5, plaque_top + 7,
            plaque_right + 5, plaque_bottom + 7,
        ),
        radius=max(16, int(width * 0.02)),
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(
        ImageFilter.GaussianBlur(radius=max(4, width // 180))
    )
    background = Image.alpha_composite(background.convert("RGBA"), shadow)

    draw = ImageDraw.Draw(background)
    draw.rounded_rectangle(
        (plaque_left, plaque_top, plaque_right, plaque_bottom),
        radius=max(16, int(width * 0.02)),
        fill=(10, 12, 18, 215),
        outline=(238, 190, 94, 230),
        width=max(2, width // 360),
    )

    y = text_y
    for line, line_height in zip(lines, line_heights):
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
    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported thumbnail format: {mode}")

    title = get_title(config)
    images = collect_images()
    image_path = images[0] if images else None

    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    source_hash = sha256_file(image_path) if image_path else "no-local-image"
    fingerprint = hashlib.sha256(
        f"{VERSION}|{mode}|{title_hash}|{source_hash}".encode("utf-8")
    ).hexdigest()

    old = read_json(MANIFEST)
    if (
        isinstance(old, dict)
        and old.get("fingerprint") == fingerprint
        and old.get("status") == "completed"
        and THUMBNAIL.is_file()
        and THUMBNAIL.stat().st_size >= MIN_IMAGE_BYTES
    ):
        try:
            with Image.open(THUMBNAIL) as image:
                image.verify()
            log("RESUME: valid thumbnail reused; no regeneration")
            return
        except Exception:
            pass

    width, height, font_size, line_count = create_thumbnail(
        title, mode, image_path
    )

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
                image_path.relative_to(ROOT).as_posix()
                if image_path else None
            ),
            "background_sha256": source_hash,
            "thumbnail": THUMBNAIL.relative_to(ROOT).as_posix(),
            "width": width,
            "height": height,
            "font_size": font_size,
            "title_lines": line_count,
            "external_api_calls": 0,
        },
    )

    log("THUMBNAIL CREATED")
    log("FORMAT:", mode)
    log("TITLE:", title)
    log("DIMENSIONS:", f"{width}x{height}")
    log("FONT SIZE:", font_size)
    log("TITLE LINES:", line_count)
    log("OUTPUT:", THUMBNAIL.relative_to(ROOT))
    log("EXTERNAL API CALLS: 0")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", str(exc))
        sys.exit(1)
