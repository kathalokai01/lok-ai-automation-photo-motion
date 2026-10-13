
#!/usr/bin/env python3
"""Create a visual preview sheet for free Hindi caption fonts and sizes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


VERSION = 1

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


def find_font_file(option: dict) -> Path | None:
    """Find an installed/downloaded font file without using a network."""
    filename = option["filename"]

    for directory in FONT_DIRS:
        candidate = directory / filename
        if candidate.is_file():
            return candidate

    if shutil.which("fc-match"):
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

            candidate_text = result.stdout.strip().splitlines()
            if result.returncode == 0 and candidate_text:
                candidate = Path(candidate_text[0].strip())
                if candidate.is_file():
                    # Do not silently accept a different fallback family.
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

                    if requested in families:
                        return candidate
        except (OSError, subprocess.SubprocessError):
            pass

    return None


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType/OpenType font and fail clearly if it is invalid."""
    try:
        return ImageFont.truetype(str(path), size=size)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Font file could not be loaded: {path}"
        ) from exc


def text_dimensions(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
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

        available_fonts.append(
            {
                **option,
                "path": font_path,
            }
        )

    if not available_fonts:
        expected = ", ".join(item["filename"] for item in FONT_OPTIONS)
        raise RuntimeError(
            "No requested caption fonts were found. Run "
            "scripts/install_caption_fonts.py first. Expected font files: "
            + expected
        )

    # Render a temporary image first so Devanagari shaping and sizing can
    # be measured using the same Pillow installation as the pipeline.
    measure_image = Image.new("RGB", (8, 8), "white")
    measure_draw = ImageDraw.Draw(measure_image)

    margin = 36
    label_height = 40
    sample_gap = 12
    row_gap = 24
    card_gap = 18
    card_width = 1080

    prepared_rows = []
    maximum_height = 0

    for option in available_fonts:
        for size in FONT_SIZES:
            font = load_font(option["path"], size)
            sample_font = load_font(option["path"], max(16, size - 2))

            first_w, first_h = text_dimensions(
                measure_draw, SAMPLE_TEXT, font
            )
            second_w, second_h = text_dimensions(
                measure_draw, SECOND_SAMPLE, sample_font
            )

            row_height = (
                label_height
                + max(first_h, second_h)
                + sample_gap
                + 12
            )
            maximum_height = max(maximum_height, row_height)

            prepared_rows.append(
                {
                    "option": option,
                    "size": size,
                    "font": font,
                    "sample_font": sample_font,
                    "first_width": first_w,
                    "first_height": first_h,
                    "second_width": second_w,
                    "second_height": second_h,
                    "height": row_height,
                }
            )

    header_height = 150
    footer_height = 70
    card_height = maximum_height + 2 * card_gap
    image_height = (
        header_height
        + len(prepared_rows) * card_height
        + footer_height
        + margin
    )

    image = Image.new(
        "RGB",
        (card_width + 2 * margin, image_height),
        "#F3F4F6",
    )
    draw = ImageDraw.Draw(image)

    title_font = ImageFont.truetype(
        str(prepared_rows[0]["option"]["path"]), 38
    )
    subtitle_font = ImageFont.truetype(
        str(prepared_rows[0]["option"]["path"]), 22
    )
    label_font = ImageFont.truetype(
        str(prepared_rows[0]["option"]["path"]), 20
    )

    draw.text(
        (margin, 24),
        "Hindi Caption Font Preview",
        font=title_font,
        fill="#111827",
    )
    draw.text(
        (margin, 78),
        f"Example: {SAMPLE_TEXT}",
        font=subtitle_font,
        fill="#374151",
    )
    draw.text(
        (margin, 112),
        "Compare the same Hindi text at sizes 22, 28 and 34.",
        font=label_font,
        fill="#4B5563",
    )

    manifest_rows = []
    y = header_height

    for row in prepared_rows:
        option = row["option"]
        size = row["size"]

        left = margin
        right = margin + card_width
        top = y
        bottom = y + card_height

        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=14,
            fill="#FFFFFF",
            outline="#D1D5DB",
            width=2,
        )

        label = (
            f'{option["name"]}  |  Size {size}  |  '
            f'{option["description"]}'
        )
        draw.text(
            (left + 20, top + 12),
            label,
            font=label_font,
            fill="#111827",
        )

        sample_y = top + label_height
        draw.text(
            (left + 20, sample_y),
            SAMPLE_TEXT,
            font=row["font"],
            fill="#111827",
            stroke_width=0,
        )

        second_y = sample_y + row["first_height"] + sample_gap
        draw.text(
            (left + 20, second_y),
            SECOND_SAMPLE,
            font=row["sample_font"],
            fill="#374151",
            stroke_width=0,
        )

        manifest_rows.append(
            {
                "font": option["name"],
                "font_file": option["path"].name,
                "description": option["description"],
                "size": size,
                "sample_text": SAMPLE_TEXT,
                "second_sample_text": SECOND_SAMPLE,
                "preview_image": str(OUTPUT_IMAGE),
            }
        )

        y = bottom + 14

    draw.text(
        (margin, image_height - footer_height + 8),
        "Preview only: final appearance depends on video resolution, "
        "outline and caption renderer.",
        font=label_font,
        fill="#4B5563",
    )

    image.save(OUTPUT_IMAGE, format="PNG", optimize=True)

    manifest = {
        "version": VERSION,
        "status": "partial" if missing_fonts else "ok",
        "preview_image": str(OUTPUT_IMAGE),
        "sample_text": SAMPLE_TEXT,
        "font_sizes": FONT_SIZES,
        "available_fonts": [item["name"] for item in available_fonts],
        "missing_fonts": missing_fonts,
        "examples": manifest_rows,
        "note": (
            "This is a visual font preview. It does not change the "
            "selected production caption font or size."
        ),
    }

    OUTPUT_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Preview image: {OUTPUT_IMAGE}")
    print(f"Preview manifest: {OUTPUT_MANIFEST}")
    print(f"Available fonts: {len(available_fonts)} of {len(FONT_OPTIONS)}")

    if missing_fonts:
        print("Missing fonts: " + ", ".join(missing_fonts))
        print(
            "Run scripts/install_caption_fonts.py before creating "
            "the complete preview."
        )

    return manifest


def main() -> int:
    try:
        manifest = build_preview()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
