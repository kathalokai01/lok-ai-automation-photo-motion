#!/usr/bin/env python3
"""Generate a readable Hindi caption font comparison preview."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "captions"
OUTPUT_IMAGE = OUTPUT_DIR / "caption_font_preview.png"
OUTPUT_MANIFEST = OUTPUT_DIR / "caption_font_preview.json"

VERSION = 4

FONT_OPTIONS = [
    {
        "name": "Noto Sans Devanagari",
        "filename": "NotoSansDevanagari-Regular.ttf",
        "description": "साफ और आधुनिक",
    },
    {
        "name": "Mukta",
        "filename": "Mukta-Regular.ttf",
        "description": "आधुनिक और सहज",
    },
    {
        "name": "Hind",
        "filename": "Hind-Regular.ttf",
        "description": "स्क्रीन पर पढ़ने में सुविधाजनक",
    },
    {
        "name": "Tiro Devanagari Hindi",
        "filename": "TiroDevanagariHindi-Regular.ttf",
        "description": "पारंपरिक और साहित्यिक",
    },
]

FONT_SIZES = [22, 28, 34]

FONT_DIRS = [
    Path.home() / ".local" / "share" / "fonts" / "katha-caption",
    ROOT / "assets" / "fonts",
    ROOT / "fonts",
]

SAMPLES = [
    "कहानी में एक रहस्य छिपा था।",
    "सच्चाई सामने आते ही सब हैरान रह गए।",
]

CANVAS_WIDTH = 1080
MARGIN = 36
CARD_GAP = 16
ROW_GAP = 14
CARD_PADDING = 18
LABEL_HEIGHT = 38
SAMPLE_GAP = 14
HEADER_HEIGHT = 145
FOOTER_HEIGHT = 88

BACKGROUND = "#F3F4F6"
CARD_BACKGROUND = "#FFFFFF"
TEXT_COLOR = "#111827"
SECONDARY_COLOR = "#4B5563"
BORDER_COLOR = "#D1D5DB"


def log(*items):
    print(*items, flush=True)


def find_font_file(option):
    for directory in FONT_DIRS:
        candidate = directory / option["filename"]
        if candidate.is_file():
            return candidate

    matcher = shutil.which("fc-match")
    if not matcher:
        return None

    try:
        result = subprocess.run(
            [
                matcher,
                "-f",
                "%{family}\n%{file}\n",
                option["name"],
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        if result.returncode != 0:
            return None

        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return None

        expected = option["name"].casefold()
        families = [
            family.strip().casefold()
            for family in lines[0].split(",")
            if family.strip()
        ]

        if not any(
            family == expected or family.startswith(expected + " ")
            for family in families
        ):
            return None

        path = Path(lines[1].strip())
        return path if path.is_file() else None

    except (OSError, subprocess.SubprocessError):
        return None


def require_hindi_shaping():
    try:
        available = features.check_feature("raqm")
    except (ValueError, AttributeError):
        available = False

    if not available:
        raise RuntimeError(
            "Hindi text shaping support (Pillow RAQM) is unavailable. "
            "Use a Pillow installation built with libraqm support."
        )


def load_font(path, size):
    try:
        return ImageFont.truetype(
            str(path),
            size=size,
            layout_engine=ImageFont.Layout.RAQM,
        )
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError(
            f"Cannot load font at size {size}: {path}"
        ) from exc


def text_width(draw, text, font):
    box = draw.textbbox(
        (0, 0),
        text,
        font=font,
        direction="ltr",
        language="hi",
    )
    return max(0, box[2] - box[0])


def text_height(draw, text, font):
    box = draw.textbbox(
        (0, 0),
        text,
        font=font,
        direction="ltr",
        language="hi",
    )
    return max(1, box[3] - box[1])


def wrap_text(draw, text, font, max_width):
    """Wrap Hindi text at word boundaries without clipping."""
    words = text.split()
    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()

        if text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        # Keep a long individual token intact rather than silently
        # deleting characters. Report if even that token is too wide.
        if text_width(draw, word, font) > max_width:
            log("WARNING: A word exceeds the preview text width:", word)

        current = word

    if current:
        lines.append(current)

    return lines


def draw_wrapped_text(
    draw,
    x,
    y,
    text,
    font,
    fill,
    max_width,
    line_gap=5,
):
    lines = wrap_text(draw, text, font, max_width)
    line_heights = [
        text_height(draw, line, font)
        for line in lines
    ]

    for line, height in zip(lines, line_heights):
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            direction="ltr",
            language="hi",
        )
        y += height + line_gap

    return y, lines


def build_preview():
    require_hindi_shaping()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    available = []
    missing = []

    for option in FONT_OPTIONS:
        path = find_font_file(option)

        if path is None:
            missing.append(option["name"])
            continue

        available.append({
            **option,
            "path": path,
        })

    if not available:
        raise RuntimeError(
            "No requested caption fonts were found. "
            "Run scripts/install_caption_fonts.py first."
        )

    measure_image = Image.new("RGB", (8, 8), "white")
    measure_draw = ImageDraw.Draw(measure_image)

    reference_path = available[0]["path"]
    heading_font = load_font(reference_path, 30)
    subtitle_font = load_font(reference_path, 20)
    label_font = load_font(reference_path, 17)

    content_width = CANVAS_WIDTH - 2 * MARGIN
    inner_width = content_width - 2 * CARD_PADDING

    rows = []

    for option in available:
        for size in FONT_SIZES:
            font = load_font(option["path"], size)
            second_font = load_font(
                option["path"],
                max(16, size - 2),
            )

            sample_layout = []

            for sample, sample_font in zip(
                SAMPLES,
                (font, second_font),
            ):
                lines = wrap_text(
                    measure_draw,
                    sample,
                    sample_font,
                    inner_width,
                )

                height = sum(
                    text_height(measure_draw, line, sample_font) + 5
                    for line in lines
                )

                sample_layout.append({
                    "text": sample,
                    "font": sample_font,
                    "lines": lines,
                    "height": max(1, height),
                })

            content_height = sum(
                item["height"] for item in sample_layout
            ) + SAMPLE_GAP

            card_height = (
                LABEL_HEIGHT
                + content_height
                + 2 * CARD_PADDING
            )

            rows.append({
                "option": option,
                "size": size,
                "font": font,
                "samples": sample_layout,
                "card_height": card_height,
            })

    image_height = (
        HEADER_HEIGHT
        + sum(row["card_height"] for row in rows)
        + ROW_GAP * max(0, len(rows) - 1)
        + FOOTER_HEIGHT
        + MARGIN
    )

    image = Image.new(
        "RGB",
        (CANVAS_WIDTH, image_height),
        BACKGROUND,
    )
    draw = ImageDraw.Draw(image)

    draw.text(
        (MARGIN, 18),
        "Hindi Caption Font Preview",
        font=heading_font,
        fill=TEXT_COLOR,
    )

    draw.text(
        (MARGIN, 62),
        "एक ही वाक्य को अलग-अलग fonts और sizes में देखें",
        font=subtitle_font,
        fill=SECONDARY_COLOR,
        direction="ltr",
        language="hi",
    )

    draw.text(
        (MARGIN, 101),
        "Size 22, 28 और 34 | यह preview production settings नहीं बदलता",
        font=label_font,
        fill=SECONDARY_COLOR,
    )

    manifest_rows = []
    y = HEADER_HEIGHT

    for row in rows:
        option = row["option"]
        size = row["size"]
        top = y
        bottom = top + row["card_height"]

        draw.rounded_rectangle(
            (
                MARGIN,
                top,
                CANVAS_WIDTH - MARGIN,
                bottom,
            ),
            radius=12,
            fill=CARD_BACKGROUND,
            outline=BORDER_COLOR,
            width=2,
        )

        label = (
            f'{option["name"]} | Size {size} | '
            f'{option["description"]}'
        )

        draw.text(
            (MARGIN + CARD_PADDING, top + 9),
            label,
            font=label_font,
            fill=TEXT_COLOR,
        )

        text_y = top + LABEL_HEIGHT + CARD_PADDING
        sample_records = []

        for sample in row["samples"]:
            start_y = text_y
            text_y, actual_lines = draw_wrapped_text(
                draw,
                MARGIN + CARD_PADDING,
                text_y,
                sample["text"],
                sample["font"],
                TEXT_COLOR,
                inner_width,
                line_gap=5,
            )

            sample_records.append({
                "text": sample["text"],
                "lines": actual_lines,
            })

            text_y = max(
                text_y,
                start_y + sample["height"],
            ) + SAMPLE_GAP

        manifest_rows.append({
            "font": option["name"],
            "font_file": option["path"].name,
            "description": option["description"],
            "size": size,
            "samples": sample_records,
        })

        y = bottom + ROW_GAP

    footer_y = image_height - FOOTER_HEIGHT + 8

    draw.text(
        (MARGIN, footer_y),
        "Final video में readability resolution, outline और background पर भी निर्भर करती है।",
        font=label_font,
        fill=SECONDARY_COLOR,
        direction="ltr",
        language="hi",
    )

    draw.text(
        (MARGIN, footer_y + 30),
        "Missing fonts: " + (
            ", ".join(missing) if missing else "कोई नहीं"
        ),
        font=label_font,
        fill=SECONDARY_COLOR,
    )

    temporary_image = OUTPUT_IMAGE.with_name(
        OUTPUT_IMAGE.name + ".tmp.png"
    )
    image.save(temporary_image, format="PNG", optimize=True)
    temporary_image.replace(OUTPUT_IMAGE)

    manifest = {
        "version": VERSION,
        "status": "ok",
        "preview_image": str(OUTPUT_IMAGE),
        "font_sizes": FONT_SIZES,
        "available_fonts": [
            option["name"] for option in available
        ],
        "missing_fonts": missing,
        "examples": manifest_rows,
        "hindi_shaping": "Pillow RAQM",
        "production_settings_changed": False,
        "note": (
            "This is a visual comparison only. It does not modify "
            "Input/topic.txt or production caption settings."
        ),
    }

    temporary_manifest = OUTPUT_MANIFEST.with_name(
        OUTPUT_MANIFEST.name + ".tmp"
    )
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.replace(OUTPUT_MANIFEST)

    log("CAPTION FONT PREVIEW READY")
    log("Image:", OUTPUT_IMAGE)
    log("Manifest:", OUTPUT_MANIFEST)
    log("Fonts available:", len(available))
    log("Missing fonts:", len(missing))
    log("Preview combinations:", len(rows))

    return manifest


def main():
    try:
        build_preview()
        return 0
    except Exception as exc:
        print(
            f"CAPTION FONT PREVIEW FAILED: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())