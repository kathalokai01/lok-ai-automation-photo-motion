#!/usr/bin/env python3
"""Generate a verified Hindi caption font comparison preview."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "captions"
OUTPUT_IMAGE = OUTPUT_DIR / "caption_font_preview.png"
OUTPUT_MANIFEST = OUTPUT_DIR / "caption_font_preview.json"
FONT_MANIFEST = (
    Path.home()
    / ".local" / "share" / "fonts" / "katha-caption"
    / "font_install_manifest.json"
)

VERSION = 5
FONT_SIZES = [22, 28, 34]
CANVAS_WIDTH = 1080
MARGIN = 36
CARD_GAP = 16
CARD_PADDING = 18
LABEL_HEIGHT = 38
SAMPLE_GAP = 14
HEADER_HEIGHT = 145
FOOTER_HEIGHT = 100

BACKGROUND = "#F3F4F6"
CARD_BACKGROUND = "#FFFFFF"
TEXT_COLOR = "#111827"
SECONDARY_COLOR = "#4B5563"
BORDER_COLOR = "#D1D5DB"

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

FONT_DIRS = [
    Path.home() / ".local" / "share" / "fonts" / "katha-caption",
    ROOT / "assets" / "fonts",
    ROOT / "fonts",
]

SAMPLES = [
    "कहानी में एक रहस्य छिपा था।",
    "सच्चाई सामने आते ही सब हैरान रह गए।",
]


def log(*items):
    print(*items, flush=True)


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_hindi_shaping():
    try:
        available = features.check_feature("raqm")
    except (ValueError, AttributeError):
        available = False

    if not available:
        raise RuntimeError(
            "Hindi text shaping support (Pillow RAQM) is unavailable. "
            "Install a Pillow build with libraqm support before generating "
            "the preview. The script will not create a misleading preview."
        )


def family_matches(actual, expected):
    families = [
        item.strip().casefold()
        for item in actual.split(",")
        if item.strip()
    ]
    expected = expected.casefold()

    return any(
        family == expected or family.startswith(expected + " ")
        for family in families
    )


def verify_manifest_font(filename, expected_family, path):
    try:
        manifest = json.loads(
            FONT_MANIFEST.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False

    if not isinstance(manifest, dict):
        return False

    fonts = manifest.get("fonts", {})
    if not isinstance(fonts, dict):
        return False

    record = fonts.get(filename)
    if not isinstance(record, dict):
        return False

    if manifest.get("status") != "completed":
        return False
    if record.get("family") != expected_family:
        return False
    if record.get("sha256") != sha256_file(path):
        return False

    return True


def find_font_file(option):
    for directory in FONT_DIRS:
        candidate = directory / option["filename"]
        if candidate.is_file():
            try:
                font = ImageFont.truetype(str(candidate), size=24)
                font.getbbox(
                    SAMPLES[0],
                    direction="ltr",
                    language="hi",
                )
                return candidate, "file"
            except (OSError, ValueError, TypeError):
                log("INVALID FONT FILE:", candidate)

    matcher = shutil.which("fc-match")
    if not matcher:
        return None, "not_found"

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
            return None, "fontconfig_error"

        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return None, "fontconfig_empty"

        if not family_matches(lines[0], option["name"]):
            return None, "fontconfig_substitution"

        path = Path(lines[1].strip())
        if not path.is_file():
            return None, "missing_file"

        font = ImageFont.truetype(str(path), size=24)
        font.getbbox(
            SAMPLES[0],
            direction="ltr",
            language="hi",
        )
        return path, "fontconfig"

    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None, "fontconfig_error"


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


def text_bbox(draw, text, font):
    return draw.textbbox(
        (0, 0),
        text,
        font=font,
        direction="ltr",
        language="hi",
    )


def text_width(draw, text, font):
    box = text_bbox(draw, text, font)
    return max(0, box[2] - box[0])


def text_height(draw, text, font):
    box = text_bbox(draw, text, font)
    return max(1, box[3] - box[1])


def wrap_text(draw, text, font, max_width):
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

        if text_width(draw, word, font) > max_width:
            raise RuntimeError(
                "A single Hindi word exceeds the preview width; "
                f"cannot safely wrap it: {word}"
            )

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
    start_y = y

    for line in lines:
        box = text_bbox(draw, line, font)
        height = max(1, box[3] - box[1])

        draw.text(
            (x, y - box[1]),
            line,
            font=font,
            fill=fill,
            direction="ltr",
            language="hi",
        )
        y += height + line_gap

    return y, {
        "lines": lines,
        "height": y - start_y,
    }


def build_preview():
    require_hindi_shaping()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    available = []
    missing = []

    for option in FONT_OPTIONS:
        path, source = find_font_file(option)

        if path is None:
            missing.append({
                "name": option["name"],
                "reason": source,
            })
            continue

        manifest_verified = verify_manifest_font(
            option["filename"],
            option["name"],
            path,
        )

        available.append({
            **option,
            "path": path,
            "source": source,
            "manifest_verified": manifest_verified,
        })

    if not available:
        raise RuntimeError(
            "No usable caption fonts were found. "
            "Run scripts/install_caption_fonts.py first."
        )

    measure_image = Image.new("RGB", (8, 8), "white")
    measure_draw = ImageDraw.Draw(measure_image)

    heading_font = load_font(available[0]["path"], 30)
    subtitle_font = load_font(available[0]["path"], 20)
    label_font = load_font(available[0]["path"], 17)

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
                    "height": max(1, height),
                })

            content_height = (
                sum(item["height"] for item in sample_layout)
                + SAMPLE_GAP
            )
            card_height = (
                LABEL_HEIGHT
                + content_height
                + 2 * CARD_PADDING
            )

            rows.append({
                "option": option,
                "size": size,
                "samples": sample_layout,
                "card_height": card_height,
            })

    image_height = (
        HEADER_HEIGHT
        + sum(row["card_height"] for row in rows)
        + CARD_GAP * max(0, len(rows) - 1)
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
        "Size 22, 28 और 34 | केवल तुलना, production settings नहीं",
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
            text_y, layout = draw_wrapped_text(
                draw,
                MARGIN + CARD_PADDING,
                text_y,
                sample["text"],
                sample["font"],
                TEXT_COLOR,
                inner_width,
            )

            sample_records.append({
                "text": sample["text"],
                "lines": layout["lines"],
            })
            text_y += SAMPLE_GAP

        manifest_rows.append({
            "font": option["name"],
            "font_file": option["path"].name,
            "font_sha256": sha256_file(option["path"]),
            "font_source": option["source"],
            "installer_manifest_verified": option["manifest_verified"],
            "description": option["description"],
            "size": size,
            "samples": sample_records,
        })

        y = bottom + CARD_GAP

    footer_y = image_height - FOOTER_HEIGHT + 8

    draw.text(
        (MARGIN, footer_y),
        "Readability resolution, outline और background पर भी निर्भर करती है।",
        font=label_font,
        fill=SECONDARY_COLOR,
        direction="ltr",
        language="hi",
    )
    draw.text(
        (MARGIN, footer_y + 30),
        "Missing fonts: " + (
            ", ".join(item["name"] for item in missing)
            if missing else "कोई नहीं"
        ),
        font=label_font,
        fill=SECONDARY_COLOR,
    )

    temporary_image = OUTPUT_IMAGE.with_name(
        OUTPUT_IMAGE.name + ".tmp.png"
    )

    try:
        image.save(temporary_image, format="PNG", optimize=True)

        # Reopen the saved image to detect incomplete or invalid output.
        with Image.open(temporary_image) as check:
            check.verify()

        os.replace(temporary_image, OUTPUT_IMAGE)
    finally:
        temporary_image.unlink(missing_ok=True)

    manifest = {
        "version": VERSION,
        "status": "completed",
        "preview_image": str(OUTPUT_IMAGE),
        "preview_sha256": sha256_file(OUTPUT_IMAGE),
        "preview_bytes": OUTPUT_IMAGE.stat().st_size,
        "font_sizes": FONT_SIZES,
        "available_fonts": [
            item["name"] for item in available
        ],
        "missing_fonts": missing,
        "examples": manifest_rows,
        "hindi_shaping": "Pillow RAQM",
        "production_settings_changed": False,
        "external_api_calls": 0,
        "paid_services_used": False,
        "note": (
            "Visual comparison only. This script does not modify "
            "Input/topic.txt or production caption settings."
        ),
    }

    atomic_write_json(OUTPUT_MANIFEST, manifest)

    log("CAPTION FONT PREVIEW READY")
    log("Image:", OUTPUT_IMAGE)
    log("Manifest:", OUTPUT_MANIFEST)
    log("Fonts available:", len(available))
    log("Missing fonts:", len(missing))
    log("Preview combinations:", len(rows))


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