#!/usr/bin/env python3
"""Create a shaped Hindi caption preview for four free fonts and three sizes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

VERSION = 3

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
HEADER_HEIGHT = 160
FOOTER_HEIGHT = 90
CARD_WIDTH = 1080
CARD_GAP = 18
ROW_GAP = 14
LABEL_HEIGHT = 42
SAMPLE_GAP = 16


def find_font_file(option: dict) -> Path | None:
    """Find the requested font file without silently substituting another."""
    for directory in FONT_DIRS:
        candidate = directory / option["filename"]
        if candidate.is_file():
            return candidate

    fc_match = shutil.which("fc-match")
    if not fc_match:
        return None

    try:
        result = subprocess.run(
            [
                fc_match,
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

        matched_families = [
            family.strip().casefold()
            for family in lines[0].split(",")
            if family.strip()
        ]
        expected = option["name"].casefold()

        if not any(
            family == expected or family.startswith(expected + " ")
            for family in matched_families
        ):
            return None

        path = Path(lines[1].strip())
        return path if path.is_file() else None

    except (OSError, subprocess.SubprocessError):
        return None


def require_hindi_shaping() -> None:
    """Require Pillow's RAQM layout engine for correct complex-script shaping."""
    try:
        available = features.check_feature("raqm")
    except (ValueError, AttributeError):
        available = False

    if not available:
        raise RuntimeError(
            "Pillow RAQM support is unavailable. Install a Pillow build "
            "with libraqm support before creating the Hindi font preview."
        )


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(
            str(path),
            size=size,
            layout_engine=ImageFont.Layout.RAQM,
        )
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError(
            f"Could not load font with Hindi shaping enabled: {path}"
        ) from exc


def text_bbox(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, int, int]:
    return draw.textbbox(
        (0, 0),
        text,
        font=font,
        direction="ltr",
        language="hi",
    )


def text_height(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> int:
    box = text_bbox(draw, text, font)
    return max(1, box[3] - box[1])


def draw_hindi_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
) -> None:
    draw.text(
        position,
        text,
        font=font,
        fill=fill,
        direction="ltr",
        language="hi",
    )


def build_preview() -> dict:
    require_hindi_shaping()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    available_fonts = []
    missing_fonts = []

    for option in FONT_OPTIONS:
        path = find_font_file(option)
        if path is None:
            missing_fonts.append(option["name"])
        else:
            available_fonts.append({**option, "path": path})

    if missing_fonts:
        raise RuntimeError(
            "Required caption fonts are missing: "
            + ", ".join(missing_fonts)
            + ". Run scripts/install_caption_fonts.py first."
        )

    measure_image = Image.new("RGB", (8, 8), "white")
    measure_draw = ImageDraw.Draw(measure_image)

    title_font = load_font(available_fonts[0]["path"], 34)
    subtitle_font = load_font(available_fonts[0]["path"], 22)
    label_font = load_font(available_fonts[0]["path"], 18)

    prepared_rows = []

    for option in available_fonts:
        for size in FONT_SIZES:
            font = load_font(option["path"], size)
            sample_font = load_font(option["path"], max(16, size - 2))

            first_height = text_height(
                measure_draw,
                SAMPLE_TEXT,
                font,
            )
            second_height = text_height(
                measure_draw,
                SECOND_SAMPLE,
                sample_font,
            )

            content_height = (
                first_height + SAMPLE_GAP + second_height
            )
            card_height = LABEL_HEIGHT + content_height + 2 * CARD_GAP

            prepared_rows.append({
                "option": option,
                "size": size,
                "font": font,
                "sample_font": sample_font,
                "first_height": first_height,
                "second_height": second_height,
                "card_height": card_height,
            })

    cards_height = sum(row["card_height"] for row in prepared_rows)
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

    draw_hindi_text(
        draw,
        (MARGIN, 18),
        "Hindi Caption Font Preview",
        title_font,
        "#111827",
    )
    draw_hindi_text(
        draw,
        (MARGIN, 68),
        f"उदाहरण: {SAMPLE_TEXT}",
        subtitle_font,
        "#374151",
    )
    draw_hindi_text(
        draw,
        (MARGIN, 112),
        "चार fonts की तुलना: Size 22, 28 और 34",
        label_font,
        "#4B5563",
    )

    manifest_rows = []
    y = HEADER_HEIGHT

    for row in prepared_rows:
        option = row["option"]
        size = row["size"]
        left = MARGIN
        right = MARGIN + CARD_WIDTH
        top = y
        bottom = top + row["card_height"]

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
        draw_hindi_text(
            draw,
            (left + CARD_GAP, top + 10),
            label,
            label_font,
            "#111827",
        )

        sample_y = top + LABEL_HEIGHT

        draw_hindi_text(
            draw,
            (left + CARD_GAP, sample_y),
            SAMPLE_TEXT,
            row["font"],
            "#111827",
        )

        second_y = (
            sample_y
            + row["first_height"]
            + SAMPLE_GAP
        )
        draw_hindi_text(
            draw,
            (left + CARD_GAP, second_y),
            SECOND_SAMPLE,
            row["sample_font"],
            "#374151",
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

    draw_hindi_text(
        draw,
        (MARGIN, footer_y),
        "यह preview है; final appearance video resolution और outline पर भी निर्भर करेगा।",
        label_font,
        "#4B5563",
    )
    draw_hindi_text(
        draw,
        (MARGIN, footer_y + 30),
        "Preview बनाने से production caption settings नहीं बदलतीं।",
        label_font,
        "#4B5563",
    )

    temp_image = OUTPUT_IMAGE.with_name(OUTPUT_IMAGE.name + ".tmp.png")
    image.save(temp_image, format="PNG", optimize=True)
    temp_image.replace(OUTPUT_IMAGE)

    manifest = {
        "version": VERSION,
        "status": "ok",
        "preview_image": str(OUTPUT_IMAGE),
        "sample_text": SAMPLE_TEXT,
        "second_sample_text": SECOND_SAMPLE,
        "font_sizes": FONT_SIZES,
        "hindi_shaping": "Pillow RAQM",
        "available_fonts": [item["name"] for item in available_fonts],
        "missing_fonts": [],
        "examples": manifest_rows,
        "note": (
            "Preview compares the installed font files at three sizes. "
            "It does not change production caption settings."
        ),
    }

    temp_manifest = OUTPUT_MANIFEST.with_name(
        OUTPUT_MANIFEST.name + ".tmp"
    )
    temp_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_manifest.replace(OUTPUT_MANIFEST)

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