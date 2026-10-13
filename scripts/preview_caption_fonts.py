#!/usr/bin/env python3
"""Create a visual preview sheet for free Hindi caption fonts and sizes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


VERSION = 2

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "captions"
OUTPUT_IMAGE = OUTPUT_DIR / "caption_font_preview.png"
OUTPUT_MANIFEST = OUTPUT_DIR / "caption_font_preview.json"

SAMPLE_TEXT = "कहानी में एक रहस्य छिपा था।"
SECOND_SAMPLE = "सच्चाई सामने आते ही सब हैरान रह गए।"

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

MARGIN = 36
HEADER_HEIGHT = 150
FOOTER_HEIGHT = 82
CARD_WIDTH = 1080
CARD_GAP = 18
ROW_GAP = 14
LABEL_HEIGHT = 42
SAMPLE_GAP = 12


def find_font_file(option: dict) -> Path | None:
    """Find the requested font without silently accepting a substitute."""
    filename = option["filename"]

    for directory in FONT_DIRS:
        candidate = directory / filename
        if candidate.is_file():
            return candidate

    if not shutil.which("fc-match"):
        return None

    try:
        result = subprocess.run(
            [
                "fc-match",
                "-f",
                "%{file}\n",
                option["name"],
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return None

        candidate = Path(result.stdout.strip().splitlines()[0])

        family_result = subprocess.run(
            [
                "fc-match",
                "-f",
                "%{family}\n",
                option["name"],
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

        families = family_result.stdout.strip().lower()
        requested = option["name"].lower()

        if (
            family_result.returncode == 0
            and requested in families
            and candidate.is_file()
        ):
            return candidate

    except (OSError, subprocess.SubprocessError):
        return None

    return None


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    """Load a font and report a useful error if it cannot be rendered."""
    try:
        return ImageFont.truetype(str(path), size=size)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Could not load caption font: {path}"
        ) from exc


def text_dimensions(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return max(1, box[2] - box[0]), max(1, box[3] - box[1])


def build_preview() -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    available_fonts = []
    missing_fonts = []

    for option in FONT_OPTIONS:
        font_path = find_font_file(option)

        if font_path is None:
            missing_fonts.append(option["name"])
            continue

        available_fonts.append({
            **option,
            "path": font_path,
        })

    if missing_fonts:
        raise RuntimeError(
            "Some requested fonts are missing: "
            + ", ".join(missing_fonts)
            + ". Run scripts/install_caption_fonts.py first."
        )

    if len(available_fonts) != len(FONT_OPTIONS):
        raise RuntimeError(
            "The preview requires all four requested font families."
        )

    measure_image = Image.new("RGB", (8, 8), "white")
    measure_draw = ImageDraw.Draw(measure_image)

    prepared_rows = []

    for option in available_fonts:
        for size in FONT_SIZES:
            font = load_font(option["path"], size)
            sample_font = load_font(
                option["path"],
                max(16, size - 2),
            )

            _, first_height = text_dimensions(
                measure_draw,
                SAMPLE_TEXT,
                font,
            )

            _, second_height = text_dimensions(
                measure_draw,
                SECOND_SAMPLE,
                sample_font,
            )

            content_height = (
                max(first_height, second_height)
                + SAMPLE_GAP
            )

            card_height = (
                LABEL_HEIGHT
                + content_height
                + 2 * CARD_GAP
            )

            prepared_rows.append({
                "option": option,
                "size": size,
                "font": font,
                "sample_font": sample_font,
                "first_height": first_height,
                "second_height": second_height,
                "card_height": card_height,
            })

    # Include every inter-card gap in the canvas height calculation.
    cards_height = sum(
        row["card_height"] for row in prepared_rows
    )
    gaps_height = max(0, len(prepared_rows) - 1) * ROW_GAP

    image_height = (
        HEADER_HEIGHT
        + cards_height
        + gaps_height
        + FOOTER_HEIGHT
        + MARGIN
    )

    image = Image.new(
        "RGB",
        (CARD_WIDTH + 2 * MARGIN, image_height),
        "#F3F4F6",
    )
    draw = ImageDraw.Draw(image)

    title_font = load_font(
        available_fonts[0]["path"],
        34,
    )
    subtitle_font = load_font(
        available_fonts[0]["path"],
        22,
    )
    label_font = load_font(
        available_fonts[0]["path"],
        18,
    )

    draw.text(
        (MARGIN, 20),
        "Hindi Caption Font Preview",
        font=title_font,
        fill="#111827",
    )

    draw.text(
        (MARGIN, 70),
        f"उदाहरण: {SAMPLE_TEXT}",
        font=subtitle_font,
        fill="#374151",
    )

    draw.text(
        (MARGIN, 110),
        "Compare all four fonts at sizes 22, 28 and 34.",
        font=label_font,
        fill="#4B5563",
    )

    manifest_rows = []
    y = HEADER_HEIGHT

    for row in prepared_rows:
        option = row["option"]
        size = row["size"]
        card_height = row["card_height"]

        left = MARGIN
        right = MARGIN + CARD_WIDTH
        top = y
        bottom = top + card_height

        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=14,
            fill="#FFFFFF",
            outline="#D1D5DB",
            width=2,
        )

        label = (
            f'{option["name"]} | Size {size} | '
            f'{option["description"]}'
        )

        draw.text(
            (left + CARD_GAP, top + 10),
            label,
            font=label_font,
            fill="#111827",
        )

        sample_y = top + LABEL_HEIGHT

        draw.text(
            (left + CARD_GAP, sample_y),
            SAMPLE_TEXT,
            font=row["font"],
            fill="#111827",
        )

        second_y = (
            sample_y
            + row["first_height"]
            + SAMPLE_GAP
        )

        draw.text(
            (left + CARD_GAP, second_y),
            SECOND_SAMPLE,
            font=row["sample_font"],
            fill="#374151",
        )

        manifest_rows.append({
            "font": option["name"],
            "font_file": option["path"].name,
            "description": option["description"],
            "size": size,
            "sample_text": SAMPLE_TEXT,
            "second_sample_text": SECOND_SAMPLE,
        })

        y = bottom + ROW_GAP

    footer_y = image_height - FOOTER_HEIGHT + 8

    draw.text(
        (MARGIN, footer_y),
        "Preview only: final appearance depends on video resolution,",
        font=label_font,
        fill="#4B5563",
    )

    draw.text(
        (MARGIN, footer_y + 28),
        "caption outline, renderer and selected font size.",
        font=label_font,
        fill="#4B5563",
    )

    image.save(
        OUTPUT_IMAGE,
        format="PNG",
        optimize=True,
    )

    manifest = {
        "version": VERSION,
        "status": "ok",
        "preview_image": str(OUTPUT_IMAGE),
        "sample_text": SAMPLE_TEXT,
        "second_sample_text": SECOND_SAMPLE,
        "font_sizes": FONT_SIZES,
        "available_fonts": [
            item["name"] for item in available_fonts
        ],
        "missing_fonts": [],
        "examples": manifest_rows,
        "note": (
            "This preview compares font families and sizes. "
            "It does not change the production caption settings."
        ),
    }

    OUTPUT_MANIFEST.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print(f"Preview image: {OUTPUT_IMAGE}")
    print(f"Preview manifest: {OUTPUT_MANIFEST}")
    print(
        f"Preview rows: {len(manifest_rows)} "
        f"({len(available_fonts)} fonts x {len(FONT_SIZES)} sizes)"
    )

    return manifest


def main() -> int:
    try:
        build_preview()
        return 0
    except Exception as exc:
        print(f"CAPTION FONT PREVIEW FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())