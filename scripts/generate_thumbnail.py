#!/usr/bin/env python3
"""Story-aware cinematic thumbnail generator for Katha Lok AI."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, normalize_format

INPUT_FILE = ROOT / "Input" / "topic.txt"

STORY_FILES = (
    ROOT / "output" / "story" / "ai_story.json",
    ROOT / "output" / "story" / "story.json",
)

VISUAL_MANIFESTS = (
    ROOT / "output" / "visuals" / "image_generation_manifest.json",
    ROOT / "output" / "visuals" / "image_manifest.json",
    ROOT / "output" / "visuals" / "visual_jobs.json",
)

OUTPUT_DIR = ROOT / "output" / "thumbnail"
MAIN_OUTPUT = ROOT / "output" / "thumbnail.jpg"
LANDSCAPE_OUTPUT = OUTPUT_DIR / "thumbnail_1280x720.jpg"
VERTICAL_OUTPUT = OUTPUT_DIR / "thumbnail_1080x1920.jpg"
MANIFEST_OUTPUT = OUTPUT_DIR / "thumbnail_manifest.json"

VERSION = "thumbnail-v40-story-composition-adaptive"
MIN_IMAGE_BYTES = 1000

TITLE_KEYS = (
    "final_title",
    "generated_title",
    "story_title",
    "title",
)

IMAGE_KEYS = (
    "output_path",
    "image_path",
    "visual_path",
    "asset_path",
    "image",
    "file",
    "path",
)

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/truetype/mukta/Mukta-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().strip("\"' ")


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        log("WARNING: Invalid JSON:", path, exc)
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def first_text(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = clean_text(data.get(key))
        if value:
            return value
    return ""


def get_story_title(config: dict) -> tuple[str, str]:
    for path in STORY_FILES:
        data = read_json(path)
        if not isinstance(data, dict):
            continue

        title = first_text(data, TITLE_KEYS)
        if title:
            return title, path.relative_to(ROOT).as_posix()

        nested = data.get("story")
        if isinstance(nested, dict):
            title = first_text(nested, TITLE_KEYS)
            if title:
                return title, path.relative_to(ROOT).as_posix()

    title_file = ROOT / "output" / "story" / "final_title.txt"
    if title_file.is_file():
        title = clean_text(title_file.read_text(encoding="utf-8-sig"))
        if title:
            return title, title_file.relative_to(ROOT).as_posix()

    topic = clean_text(str(config.get("TOPIC", "")))
    if topic:
        log("WARNING: Generated story title missing; using configured TOPIC.")
        return topic, "Input/topic.txt"

    raise RuntimeError(
        "Story title missing. Refusing to create a thumbnail from "
        "the first sentence of the story."
    )


def resolve_image(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None

    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    try:
        candidate = candidate.resolve()
        if not candidate.is_relative_to(ROOT.resolve()):
            return None
    except (OSError, ValueError):
        return None

    if not candidate.is_file():
        return None

    if candidate.stat().st_size < MIN_IMAGE_BYTES:
        return None

    try:
        with Image.open(candidate) as image:
            image.verify()
        with Image.open(candidate) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                return None
            if image.width < 64 or image.height < 64:
                return None
    except Exception:
        return None

    return candidate


def get_row_image(row: dict) -> Path | None:
    for key in IMAGE_KEYS:
        path = resolve_image(row.get(key))
        if path:
            return path

    source = row.get("source_scene")
    if isinstance(source, dict):
        for key in IMAGE_KEYS:
            path = resolve_image(source.get(key))
            if path:
                return path

    return None


def manifest_rows(path: Path) -> list[dict]:
    document = read_json(path)

    if isinstance(document, list):
        source = document
    elif isinstance(document, dict):
        source = (
            document.get("scenes")
            or document.get("jobs")
            or document.get("visuals")
            or document.get("items")
            or []
        )
        if isinstance(source, dict):
            source = list(source.values())
    else:
        return []

    if not isinstance(source, list):
        return []

    result = []
    for row in source:
        if isinstance(row, dict):
            copied = dict(row)
            copied["_manifest"] = path.name
            result.append(copied)

    return result


def load_visual_candidates() -> list[dict]:
    merged: dict[tuple[int, int], dict] = {}

    for manifest_path in reversed(VISUAL_MANIFESTS):
        for row in manifest_rows(manifest_path):
            try:
                part = int(row.get("part", 0))
                scene = int(
                    row.get("global_scene")
                    or row.get("global_scene_number")
                    or row.get("scene_index")
                    or row.get("scene")
                    or 0
                )
            except (TypeError, ValueError):
                continue

            if part < 1 or scene < 1:
                continue

            key = (part, scene)
            merged[key] = {**merged.get(key, {}), **row}

    candidates = []

    for (part, scene), row in merged.items():
        status = clean_text(row.get("status")).lower()
        if status and status not in {
            "completed", "complete", "success", "done", "generated"
        }:
            continue

        image_path = get_row_image(row)
        if not image_path:
            continue

        candidates.append({
            "path": image_path,
            "part": part,
            "scene": scene,
            "row": row,
            "manifest": row.get("_manifest", ""),
        })

    return candidates


def scene_description(row: dict) -> str:
    fields = (
        "title",
        "role",
        "purpose",
        "beat",
        "mood",
        "visual_prompt",
        "image_prompt",
        "prompt",
        "scene_prompt",
        "description",
        "action",
        "story_anchor",
        "suspense_prompt",
    )

    values = [
        clean_text(row.get(key))
        for key in fields
    ]

    source = row.get("source_scene")
    if isinstance(source, dict):
        values.extend(
            clean_text(source.get(key))
            for key in fields
        )

    return " ".join(value for value in values if value).lower()


def scene_score(candidate: dict) -> int:
    row = candidate["row"]
    description = scene_description(row)

    score = 0

    groups = (
        ("hook", "opening", "opening_hook", "first reveal"),
        ("suspense", "mystery", "secret", "twist", "unknown"),
        ("climax", "finale", "reveal", "confrontation"),
        ("fear", "danger", "threat", "haunted", "ghost"),
        ("dramatic", "intense", "emotional", "shock"),
        ("close-up", "close up", "character focus", "face"),
        ("fire", "storm", "chase", "battle", "conflict"),
        ("crying", "fearful", "angry", "surprised", "shocked"),
    )

    for group in groups:
        if any(term in description for term in group):
            score += 3

    negative = (
        "empty room",
        "plain background",
        "title card",
        "text overlay",
        "wide establishing shot",
        "empty landscape",
    )

    score -= 4 * sum(term in description for term in negative)

    if row.get("opening_hook_required"):
        score += 4
    if row.get("suspense_required"):
        score += 7
    if row.get("climax_required"):
        score += 5

    # Prefer actual generated images with completed status.
    if clean_text(row.get("status")).lower() in {
        "completed", "complete", "success", "done", "generated"
    }:
        score += 2

    return score


def choose_hero_visual() -> dict:
    candidates = load_visual_candidates()

    if not candidates:
        raise RuntimeError(
            "No valid generated scene image found. "
            "Checked image_generation_manifest.json, image_manifest.json "
            "and visual_jobs.json."
        )

    for item in candidates:
        item["score"] = scene_score(item)

    # Select the strongest story moment, not automatically scene one.
    candidates.sort(
        key=lambda item: (
            item["score"],
            -item["part"],
            -item["scene"],
            item["path"].stat().st_size,
        ),
        reverse=True,
    )

    chosen = candidates[0]

    log("Thumbnail hero scene:", chosen["scene"])
    log("Thumbnail hero part:", chosen["part"])
    log("Thumbnail hero score:", chosen["score"])
    log("Thumbnail hero image:", chosen["path"].relative_to(ROOT))

    return chosen


def find_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                pass

    for family in (
        "Noto Sans Devanagari:style=Bold",
        "Mukta:style=Bold",
        "Hind:style=Bold",
        "Tiro Devanagari Hindi:style=Bold",
        "DejaVu Sans:style=Bold",
    ):
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", family],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            font_path = result.stdout.strip()
            if font_path and Path(font_path).is_file():
                return ImageFont.truetype(font_path, size=size)
        except Exception:
            continue

    raise RuntimeError("No usable Hindi thumbnail font found.")


def shorten_title(title: str, limit: int = 100) -> str:
    title = clean_text(title)
    if len(title) <= limit:
        return title

    shortened = title[:limit]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]

    return shortened.rstrip(" ,.!?;:") + "…"


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    lines = []
    current = ""

    for word in text.split():
        candidate = word if not current else current + " " + word
        bounds = draw.textbbox((0, 0), candidate, font=font)

        if bounds[2] - bounds[0] <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
        current = word

    if current:
        lines.append(current)

    return lines


def estimate_focal_point(image: Image.Image) -> tuple[float, float]:
    sample = image.convert("RGB")
    sample.thumbnail((240, 240))

    gray = sample.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    width, height = edges.size
    pixels = edges.load()

    total = 0.0
    weighted_x = 0.0
    weighted_y = 0.0

    for y in range(height):
        for x in range(width):
            weight = float(pixels[x, y]) + 1.0
            total += weight
            weighted_x += x * weight
            weighted_y += y * weight

    if total <= 0:
        return 0.5, 0.5

    return (
        max(0.12, min(0.88, weighted_x / total / width)),
        max(0.12, min(0.88, weighted_y / total / height)),
    )


def crop_to_ratio(
    image: Image.Image,
    ratio: float,
    focal: tuple[float, float],
) -> Image.Image:
    width, height = image.size
    current = width / height
    fx, fy = focal

    if current > ratio:
        new_width = max(1, int(height * ratio))
        center_x = int(fx * width)
        left = max(0, min(width - new_width, center_x - new_width // 2))
        return image.crop((left, 0, left + new_width, height))

    new_height = max(1, int(width / ratio))
    center_y = int(fy * height)
    top = max(0, min(height - new_height, center_y - new_height // 2))
    return image.crop((0, top, width, top + new_height))


def sample_palette(image: Image.Image) -> dict:
    sample = image.convert("RGB").resize((48, 48))
    pixels = list(sample.getdata())

    red = sum(pixel[0] for pixel in pixels) / len(pixels)
    green = sum(pixel[1] for pixel in pixels) / len(pixels)
    blue = sum(pixel[2] for pixel in pixels) / len(pixels)
    brightness = (red + green + blue) / 3

    if brightness < 78:
        accent = (255, 185, 65)
    elif blue > red * 1.12:
        accent = (95, 195, 255)
    elif green > red * 1.12:
        accent = (115, 235, 155)
    elif red > blue * 1.20:
        accent = (255, 125, 85)
    else:
        accent = (255, 215, 100)

    return {
        "accent": accent,
        "text": (255, 252, 242),
        "outline": (10, 10, 14),
        "brightness": brightness,
    }


def apply_cinematic_grade(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Color(image).enhance(1.12)
    image = ImageEnhance.Contrast(image).enhance(1.10)
    image = ImageEnhance.Sharpness(image).enhance(1.12)

    image = image.filter(ImageFilter.UnsharpMask(
        radius=1.5,
        percent=115,
        threshold=3,
    ))

    return image


def make_gradient_overlay(
    size: tuple[int, int],
    vertical: bool,
    placement: str,
) -> Image.Image:
    width, height = size
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if placement == "bottom":
        start = int(height * 0.43)
        for y in range(start, height):
            p = (y - start) / max(1, height - start)
            alpha = int(15 + 210 * p)
            draw.line((0, y, width, y), fill=(0, 0, 0, alpha))

    elif placement == "top":
        end = int(height * 0.52)
        for y in range(end):
            p = 1 - y / max(1, end)
            alpha = int(15 + 195 * p)
            draw.line((0, y, width, y), fill=(0, 0, 0, alpha))

    else:
        band = int(width * 0.55)
        for x in range(band):
            p = 1 - x / max(1, band)
            alpha = int(15 + 195 * p)
            draw.line((x, 0, x, height), fill=(0, 0, 0, alpha))

    return overlay


def choose_title_placement(
    focal: tuple[float, float],
    vertical: bool,
) -> str:
    fx, fy = focal

    if vertical:
        return "top" if fy > 0.62 else "bottom"

    if fx > 0.64:
        return "left"
    if fx < 0.36:
        return "right"
    return "bottom"


def draw_cinematic_title(
    image: Image.Image,
    title: str,
    vertical: bool,
    focal: tuple[float, float],
) -> tuple[Image.Image, dict]:
    image = image.convert("RGB")
    width, height = image.size
    palette = sample_palette(image)

    placement = choose_title_placement(focal, vertical)
    overlay = make_gradient_overlay(
        image.size,
        vertical,
        placement,
    )
    image = Image.alpha_composite(
        image.convert("RGBA"),
        overlay,
    ).convert("RGB")

    draw = ImageDraw.Draw(image)
    title = shorten_title(title)

    max_width = int(width * (0.84 if placement in {"left", "right"} else 0.90))
    base_size = max(26, width // (13 if vertical else 16))
    min_size = max(18, width // 40)

    selected_font = None
    selected_lines = []
    selected_size = min_size

    for size in range(base_size, min_size - 1, -1):
        font = find_font(size)
        lines = wrap_text(draw, title, font, max_width)
        if len(lines) <= 3:
            selected_font = font
            selected_lines = lines
            selected_size = size
            break

    if selected_font is None:
        selected_font = find_font(min_size)
        selected_lines = wrap_text(draw, title, selected_font, max_width)[:3]
        selected_size = min_size

    spacing = max(5, selected_size // 5)
    line_heights = []

    for line in selected_lines:
        bounds = draw.textbbox(
            (0, 0),
            line,
            font=selected_font,
            stroke_width=2,
        )
        line_heights.append(max(1, bounds[3] - bounds[1]))

    total_height = sum(line_heights) + spacing * max(0, len(line_heights) - 1)

    if placement == "top":
        start_y = int(height * 0.07)
    elif placement == "bottom":
        start_y = int(height * 0.68)
    else:
        start_y = int(height * 0.34)

    start_y = max(18, min(start_y, height - total_height - 25))

    if placement == "left":
        x_anchor = int(width * 0.07)
        alignment = "left"
    elif placement == "right":
        x_anchor = int(width * 0.93)
        alignment = "right"
    else:
        x_anchor = width // 2
        alignment = "center"

    accent_width = max(60, int(width * 0.14))
    accent_height = max(5, selected_size // 12)

    if alignment == "left":
        accent_x = x_anchor
    elif alignment == "right":
        accent_x = x_anchor - accent_width
    else:
        accent_x = (width - accent_width) // 2

    accent_y = max(10, start_y - max(18, selected_size // 2))

    draw.rounded_rectangle(
        (
            accent_x,
            accent_y,
            accent_x + accent_width,
            accent_y + accent_height,
        ),
        radius=max(2, selected_size // 18),
        fill=palette["accent"],
    )

    y = start_y

    for line, line_height in zip(selected_lines, line_heights):
        bounds = draw.textbbox(
            (0, 0),
            line,
            font=selected_font,
            stroke_width=2,
        )
        text_width = bounds[2] - bounds[0]

        if alignment == "left":
            x = x_anchor
        elif alignment == "right":
            x = x_anchor - text_width
        else:
            x = (width - text_width) // 2

        x = max(12, min(x, width - text_width - 12))

        draw.text(
            (x + 3, y + 4),
            line,
            font=selected_font,
            fill=(0, 0, 0),
            stroke_width=max(4, selected_size // 10),
            stroke_fill=(0, 0, 0),
        )

        draw.text(
            (x, y),
            line,
            font=selected_font,
            fill=palette["text"],
            stroke_width=max(2, selected_size // 17),
            stroke_fill=palette["outline"],
        )

        y += line_height + spacing

    design = {
        "title_placement": placement,
        "title_lines": len(selected_lines),
        "title_font_size": selected_size,
        "adaptive_accent_rgb": list(palette["accent"]),
        "adaptive_focal_point": [
            round(focal[0], 4),
            round(focal[1], 4),
        ],
    }

    return image, design

def create_thumbnail(
    source: Path,
    title: str,
    dimensions: tuple[int, int],
    destination: Path,
) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")

    focal = estimate_focal_point(image)
    image = crop_to_ratio(
        image,
        dimensions[0] / dimensions[1],
        focal,
    )

    image = image.resize(
        dimensions,
        Image.Resampling.LANCZOS,
    )

    image = apply_cinematic_grade(image)

    image, design = draw_cinematic_title(
        image=image,
        title=title,
        vertical=dimensions[1] > dimensions[0],
        focal=focal,
    )

    temporary = destination.with_name(
        destination.stem + ".tmp.jpg"
    )

    try:
        image.save(
            temporary,
            format="JPEG",
            quality=95,
            optimize=True,
            progressive=True,
            subsampling=0,
        )

        with Image.open(temporary) as check:
            check.verify()

        with Image.open(temporary) as check:
            if check.size != dimensions:
                raise RuntimeError(
                    f"Wrong image dimensions: {check.size}; "
                    f"expected {dimensions}"
                )
            if check.format != "JPEG":
                raise RuntimeError("Thumbnail is not JPEG.")

        if temporary.stat().st_size < MIN_IMAGE_BYTES:
            raise RuntimeError("Thumbnail output is unexpectedly small.")

        temporary.replace(destination)

    finally:
        temporary.unlink(missing_ok=True)

    return {
        "file": destination.relative_to(ROOT).as_posix(),
        "width": dimensions[0],
        "height": dimensions[1],
        "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "success": True,
        "design": design,
    }


def verify_thumbnail(path: Path, expected_size: tuple[int, int]) -> None:
    if not path.is_file():
        raise RuntimeError(f"Thumbnail missing: {path}")

    if path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(f"Thumbnail file is too small: {path}")

    try:
        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            if image.format != "JPEG":
                raise RuntimeError(
                    f"Invalid thumbnail format: {image.format}"
                )

            if image.size != expected_size:
                raise RuntimeError(
                    f"Invalid thumbnail dimensions: {image.size}; "
                    f"expected {expected_size}"
                )

    except Exception as exc:
        raise RuntimeError(
            f"Thumbnail verification failed: {path}: {exc}"
        ) from exc


def verify_manifest_outputs(manifest: dict) -> None:
    outputs = manifest.get("thumbnails")

    if not isinstance(outputs, list) or not outputs:
        raise RuntimeError("Thumbnail manifest contains no outputs.")

    for item in outputs:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid thumbnail manifest record.")

        relative = item.get("file")
        if not isinstance(relative, str) or not relative.strip():
            raise RuntimeError("Thumbnail manifest has no output path.")

        path = resolve_image(relative)
        if not path:
            raise RuntimeError(
                f"Thumbnail manifest points to an invalid file: {relative}"
            )

        declared_hash = item.get("sha256")
        if declared_hash and sha256_file(path) != declared_hash:
            raise RuntimeError(
                f"Thumbnail SHA256 mismatch: {relative}"
            )

        expected = (
            int(item.get("width", 0)),
            int(item.get("height", 0)),
        )

        verify_thumbnail(path, expected)


def main() -> None:
    log("=" * 65)
    log("KATHA LOK AI — CINEMATIC STORY THUMBNAIL GENERATOR")
    log("=" * 65)

    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_FILE)
    format_name = normalize_format(config)

    if format_name not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT in Input/topic.txt: {format_name}"
        )

    title, title_source = get_story_title(config)
    title = clean_text(title)

    if not title:
        raise RuntimeError("Story title is empty.")

    hero = choose_hero_visual()
    source = hero["path"]

    if not source.is_file():
        raise RuntimeError(
            f"Selected scene image disappeared: {source}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # YouTube thumbnail is always landscape 1280x720.
    # The vertical image is an additional SHORT-format asset.
    outputs = [
        (MAIN_OUTPUT, (1280, 720)),
        (LANDSCAPE_OUTPUT, (1280, 720)),
    ]

    if format_name == "short":
        outputs.append(
            (VERTICAL_OUTPUT, (1080, 1920))
        )

    results = []

    for destination, dimensions in outputs:
        log("Creating:", destination.relative_to(ROOT))
        result = create_thumbnail(
            source=source,
            title=title,
            dimensions=dimensions,
            destination=destination,
        )
        results.append(result)

    if len(results) != len(outputs):
        raise RuntimeError("Thumbnail output count mismatch.")

    if not all(item.get("success") for item in results):
        raise RuntimeError("One or more thumbnail outputs failed.")

    # The main thumbnail is the file consumed by final validation.
    verify_thumbnail(MAIN_OUTPUT, (1280, 720))

    if format_name == "short":
        verify_thumbnail(
            VERTICAL_OUTPUT,
            (1080, 1920),
        )

    manifest = {
        "status": "completed",
        "version": VERSION,
        "format": format_name,
        "title": title,
        "title_source": title_source,
        "thumbnail": MAIN_OUTPUT.relative_to(ROOT).as_posix(),
        "path": MAIN_OUTPUT.relative_to(ROOT).as_posix(),
        "output": MAIN_OUTPUT.relative_to(ROOT).as_posix(),
        "source_image": source.relative_to(ROOT).as_posix(),
        "source_manifest": hero["manifest"],
        "selected_part": hero["part"],
        "selected_scene": hero["scene"],
        "scene_score": hero["score"],
        "source_image_sha256": sha256_file(source),
        "design": {
            "story_specific_title": True,
            "dramatic_scene_selection": True,
            "adaptive_focal_crop": True,
            "adaptive_title_position": True,
            "adaptive_scene_palette": True,
            "cinematic_color_grade": True,
            "dark_gradient_for_readability": True,
            "bold_hindi_font": True,
            "fixed_red_gold_template": False,
            "ai_character_generation": False,
        },
        "thumbnails": results,
    }

    write_json(MANIFEST_OUTPUT, manifest)

    # Re-read the written manifest and verify every declared output.
    saved_manifest = read_json(MANIFEST_OUTPUT)

    if not isinstance(saved_manifest, dict):
        raise RuntimeError("Thumbnail manifest could not be reloaded.")

    if saved_manifest.get("status") != "completed":
        raise RuntimeError("Thumbnail manifest status is invalid.")

    if saved_manifest.get("format") != format_name:
        raise RuntimeError("Thumbnail manifest format mismatch.")

    verify_manifest_outputs(saved_manifest)

    log("-" * 65)
    log("FORMAT:", format_name)
    log("TITLE:", title)
    log("TITLE SOURCE:", title_source)
    log("SELECTED PART:", hero["part"])
    log("SELECTED SCENE:", hero["scene"])
    log("SOURCE IMAGE:", source.relative_to(ROOT))
    log("LANDSCAPE THUMBNAIL:", MAIN_OUTPUT.relative_to(ROOT))

    if format_name == "short":
        log("VERTICAL THUMBNAIL:", VERTICAL_OUTPUT.relative_to(ROOT))

    log("OUTPUT COUNT:", len(results))
    log("MANIFEST:", MANIFEST_OUTPUT.relative_to(ROOT))
    log("THUMBNAIL GENERATION: SUCCESS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Thumbnail generation cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", str(exc))
        raise