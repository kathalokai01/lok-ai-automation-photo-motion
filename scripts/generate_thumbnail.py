#!/usr/bin/env python3
"""Generate story-aware cinematic thumbnails for Katha Lok AI."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, normalize_format

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILES = (
    ROOT / "output" / "story" / "ai_story.json",
    ROOT / "output" / "story" / "story.json",
)
VISUAL_MANIFESTS = (
    ROOT / "output" / "visuals" / "visual_jobs.json",
    ROOT / "output" / "visuals" / "image_generation_manifest.json",
    ROOT / "output" / "visuals" / "image_manifest.json",
)

OUTPUT_DIR = ROOT / "output" / "thumbnail"
MAIN_OUTPUT = ROOT / "output" / "thumbnail.jpg"
LANDSCAPE_OUTPUT = OUTPUT_DIR / "thumbnail_1280x720.jpg"
VERTICAL_OUTPUT = OUTPUT_DIR / "thumbnail_1080x1920.jpg"
MANIFEST_OUTPUT = OUTPUT_DIR / "thumbnail_manifest.json"

VERSION = "thumbnail-v33-fixed-story-aware"
MIN_IMAGE_BYTES = 1000


def log(*args: Any) -> None:
    print(*args, flush=True)


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().strip("\"' ")


def read_json(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        log(f"WARNING: Cannot read {path.relative_to(ROOT)}: {exc}")
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


def first_text(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = clean_text(data.get(key))
        if value:
            return value
    return ""


def get_story_title(config: dict) -> tuple[str, str]:
    # Use the title produced after the complete story has been generated.
    title_keys = (
        "final_title",
        "generated_title",
        "story_title",
        "title",
    )

    for path in STORY_FILES:
        data = read_json(path)
        if not isinstance(data, dict):
            continue

        title = first_text(data, title_keys)
        if title:
            return title, path.relative_to(ROOT).as_posix()

    title_file = ROOT / "output" / "story" / "final_title.txt"
    if title_file.is_file():
        title = clean_text(title_file.read_text(encoding="utf-8-sig"))
        if title:
            return title, title_file.relative_to(ROOT).as_posix()

    topic = clean_text(config.get("TOPIC", ""))
    if topic:
        log("WARNING: Generated story title missing; using TOPIC.")
        return topic, "Input/topic.txt"

    # Do not derive a title from the first sentence of a pasted story.
    raise RuntimeError(
        "No completed story title found. Refusing to use the first story sentence."
    )


def resolve_image(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()
        if not path.is_relative_to(ROOT.resolve()):
            return None
    except (OSError, ValueError):
        return None

    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        return None

    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                return None
            if image.width < 64 or image.height < 64:
                return None
    except Exception:
        return None

    return path


def row_image(row: dict) -> Path | None:
    # These are the output-path fields used by the visual queue and providers.
    for key in (
        "output_path",
        "image_path",
        "visual_path",
        "asset_path",
        "file",
        "path",
        "image",
    ):
        path = resolve_image(row.get(key))
        if path:
            return path
    return None


def load_visual_rows() -> list[dict]:
    rows: list[dict] = []

    for manifest_path in VISUAL_MANIFESTS:
        document = read_json(manifest_path)
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
            continue

        if not isinstance(source, list):
            continue

        for row in source:
            if isinstance(row, dict):
                copied = dict(row)
                copied["_manifest"] = manifest_path.name
                rows.append(copied)

    return rows


def scene_number(row: dict) -> tuple[int, int, int]:
    try:
        part = int(row.get("part", 0))
    except (TypeError, ValueError):
        part = 0

    try:
        scene = int(
            row.get("global_scene")
            or row.get("global_scene_number")
            or row.get("scene_index")
            or row.get("scene")
            or 0
        )
    except (TypeError, ValueError):
        scene = 0

    try:
        local = int(row.get("local_scene") or row.get("scene") or 0)
    except (TypeError, ValueError):
        local = 0

    return part, scene, local


def scene_text(row: dict) -> str:
    fields = (
        "title",
        "role",
        "purpose",
        "visual_prompt",
        "image_prompt",
        "prompt",
        "scene_prompt",
        "description",
        "mood",
        "suspense_prompt",
        "story_plan_beat",
        "story_plan_purpose",
    )
    return " ".join(clean_text(row.get(key)) for key in fields).lower()


def scene_score(row: dict) -> int:
    text = scene_text(row)
    score = 0

    positive_groups = (
        ("hook", "opening", "opening_hook"),
        ("suspense", "mystery", "secret", "twist"),
        ("climax", "finale", "reveal", "confrontation"),
        ("fear", "danger", "threat", "haunted", "ghost"),
        ("dramatic", "intense", "emotional", "shock"),
        ("close-up", "close up", "character focus", "face"),
        ("fire", "storm", "chase", "battle", "conflict"),
    )

    for group in positive_groups:
        if any(term in text for term in group):
            score += 3

    negative_terms = (
        "empty room",
        "plain background",
        "title card",
        "text overlay",
        "wide establishing shot",
    )
    score -= sum(3 for term in negative_terms if term in text)

    if row.get("opening_hook_required"):
        score += 5
    if row.get("suspense_required"):
        score += 7
    if row.get("final_resolution_required"):
        score += 2

    return score


def choose_hero_visual() -> dict:
    candidates = []

    for row in load_visual_rows():
        status = clean_text(row.get("status")).lower()
        if status and status not in {
            "completed", "complete", "success", "done", "generated"
        }:
            continue

        path = row_image(row)
        if not path:
            continue

        part, global_scene, local_scene = scene_number(row)

        candidates.append({
            "path": path,
            "part": part,
            "scene": global_scene or local_scene,
            "score": scene_score(row),
            "manifest": row.get("_manifest", ""),
            "prompt": clean_text(
                row.get("visual_prompt")
                or row.get("image_prompt")
                or row.get("prompt")
            ),
        })

    if not candidates:
        raise RuntimeError(
            "No valid completed scene image found in visual manifests."
        )

    # Prefer dramatic story scenes; tie-break by earlier story order.
    candidates.sort(
        key=lambda item: (
            item["score"],
            -item["part"],
            -item["scene"],
        ),
        reverse=True,
    )

    chosen = candidates[0]
    log("Selected thumbnail scene:", chosen["scene"])
    log("Selected image:", chosen["path"].relative_to(ROOT))
    log("Scene selection score:", chosen["score"])
    return chosen


def find_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/mukta/Mukta-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                pass

    # Fontconfig resolves installed Hindi font families on the GitHub runner.
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

    raise RuntimeError("No usable thumbnail font found.")


def shorten_title(title: str, limit: int = 100) -> str:
    title = clean_text(title)
    if len(title) <= limit:
        return title

    value = title[:limit]
    if " " in value:
        value = value.rsplit(" ", 1)[0]
    return value.rstrip(" ,.!?;:") + "…"


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
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def focal_point(image: Image.Image) -> tuple[float, float]:
    """Estimate a visually detailed focal area without face recognition."""
    sample = image.convert("RGB")
    sample.thumbnail((240, 240))
    edges = sample.convert("L").filter(ImageFilter.FIND_EDGES)

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
        max(0.15, min(0.85, weighted_x / total / width)),
        max(0.15, min(0.85, weighted_y / total / height)),
    )


def crop_to_ratio(
    image: Image.Image,
    ratio: float,
    focal: tuple[float, float],
) -> Image.Image:
    width, height = image.size
    current_ratio = width / height
    fx, fy = focal

    if current_ratio > ratio:
        new_width = max(1, int(height * ratio))
        center_x = int(fx * width)
        left = max(0, min(width - new_width, center_x - new_width // 2))
        return image.crop((left, 0, left + new_width, height))

    new_height = max(1, int(width / ratio))
    center_y = int(fy * height)
    top = max(0, min(height - new_height, center_y - new_height // 2))
    return image.crop((0, top, width, top + new_height))


def adaptive_palette(image: Image.Image) -> dict:
    sample = image.convert("RGB").resize((40, 40))
    pixels = list(sample.getdata())

    red = sum(pixel[0] for pixel in pixels) / len(pixels)
    green = sum(pixel[1] for pixel in pixels) / len(pixels)
    blue = sum(pixel[2] for pixel in pixels) / len(pixels)
    brightness = (red + green + blue) / 3

    if brightness < 75:
        return {
            "accent": (255, 190, 65),
            "text": (255, 250, 232),
            "outline": (20, 12, 5),
        }
    if blue > red * 1.12:
        return {
            "accent": (110, 200, 255),
            "text": (250, 252, 255),
            "outline": (5, 15, 32),
        }
    if green > red * 1.12:
        return {
            "accent": (125, 240, 170),
            "text": (248, 255, 248),
            "outline": (5, 25, 15),
        }
    if red > blue * 1.2:
        return {
            "accent": (255, 135, 95),
            "text": (255, 248, 240),
            "outline": (35, 8, 5),
        }

    return {
        "accent": (255, 210, 105),
        "text": (255, 250, 238),
        "outline": (25, 15, 5),
    }
def add_gradient(image: Image.Image, vertical: bool = False) -> Image.Image:
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    start = int(height * (0.48 if vertical else 0.52))

    for y in range(start, height):
        progress = (y - start) / max(1, height - start)
        alpha = int(15 + progress * 205)
        draw.line(
            [(0, y), (width, y)],
            fill=(0, 0, 0, alpha),
        )

    return Image.alpha_composite(
        image.convert("RGBA"), overlay
    ).convert("RGB")


def add_side_vignette(image: Image.Image) -> Image.Image:
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    band = max(1, int(width * 0.10))

    for x in range(band):
        alpha = int(75 * (1 - x / band))
        draw.line(
            [(x, 0), (x, height)],
            fill=(0, 0, 0, alpha),
        )
        draw.line(
            [(width - x - 1, 0), (width - x - 1, height)],
            fill=(0, 0, 0, alpha),
        )

    return Image.alpha_composite(
        image.convert("RGBA"), overlay
    ).convert("RGB")


def draw_title(
    image: Image.Image,
    title: str,
    vertical: bool = False,
) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size

    palette = adaptive_palette(image)
    image = add_gradient(image, vertical)
    image = add_side_vignette(image)

    draw = ImageDraw.Draw(image)
    title = shorten_title(title)

    max_width = int(width * 0.88)
    base_size = max(22, width // (12 if vertical else 17))
    min_size = max(18, width // 38)

    selected_font = None
    selected_lines = None
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
        selected_lines = wrap_text(
            draw, title, selected_font, max_width
        )[:3]
        selected_size = min_size

    line_spacing = max(5, selected_size // 5)
    line_heights = []

    for line in selected_lines:
        bounds = draw.textbbox(
            (0, 0),
            line,
            font=selected_font,
            stroke_width=2,
        )
        line_heights.append(max(1, bounds[3] - bounds[1]))

    total_height = (
        sum(line_heights)
        + line_spacing * max(0, len(selected_lines) - 1)
    )

    start_y = int(height * 0.68)
    start_y = min(
        start_y,
        max(20, height - total_height - 35),
    )

    accent_y = max(18, start_y - max(18, selected_size // 2))
    accent_x = int(width * 0.06)
    accent_width = max(55, int(width * 0.13))
    accent_height = max(5, selected_size // 12)

    draw.rounded_rectangle(
        (
            accent_x,
            accent_y,
            accent_x + accent_width,
            accent_y + accent_height,
        ),
        radius=max(2, selected_size // 20),
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
        x = max(18, (width - text_width) // 2)

        # Dark outline and shadow preserve readability on bright scenes.
        draw.text(
            (x + 3, y + 4),
            line,
            font=selected_font,
            fill=(0, 0, 0),
            stroke_width=max(3, selected_size // 12),
            stroke_fill=(0, 0, 0),
        )

        draw.text(
            (x, y),
            line,
            font=selected_font,
            fill=palette["text"],
            stroke_width=max(2, selected_size // 18),
            stroke_fill=palette["outline"],
        )

        y += line_height + line_spacing

    return image


def create_thumbnail(
    source: Path,
    title: str,
    dimensions: tuple[int, int],
    destination: Path,
) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as original:
        image = original.convert("RGB")

    focal = focal_point(image)
    ratio = dimensions[0] / dimensions[1]

    image = crop_to_ratio(image, ratio, focal)
    image = image.resize(
        dimensions,
        Image.Resampling.LANCZOS,
    )

    image = ImageEnhance.Color(image).enhance(1.08)
    image = ImageEnhance.Contrast(image).enhance(1.06)
    image = draw_title(
        image,
        title,
        vertical=(dimensions[1] > dimensions[0]),
    )

    temporary = destination.with_name(
        destination.stem + ".tmp.jpg"
    )

    try:
        image.save(
            temporary,
            format="JPEG",
            quality=94,
            optimize=True,
            progressive=True,
        )

        with Image.open(temporary) as check:
            check.verify()

        temporary.replace(destination)

        with Image.open(destination) as check:
            if check.size != dimensions:
                raise RuntimeError(
                    f"Wrong thumbnail size: {check.size}; "
                    f"expected {dimensions}"
                )

        if destination.stat().st_size < MIN_IMAGE_BYTES:
            raise RuntimeError("Generated thumbnail is unexpectedly small.")

    finally:
        temporary.unlink(missing_ok=True)

    return {
        "file": destination.relative_to(ROOT).as_posix(),
        "width": dimensions[0],
        "height": dimensions[1],
        "bytes": destination.stat().st_size,
        "success": True,
    }


def main() -> None:
    log("=" * 60)
    log("KATHA LOK AI — STORY-AWARE THUMBNAIL GENERATOR")
    log("=" * 60)

    config = load_and_validate(INPUT_FILE)
    format_name = normalize_format(config)

    title, title_source = get_story_title(config)
    hero = choose_hero_visual()

    source = hero["path"]
    if not source.is_file():
        raise FileNotFoundError(
            f"Selected thumbnail image disappeared: {source}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    outputs = [
        (
            MAIN_OUTPUT,
            (1280, 720),
        ),
        (
            LANDSCAPE_OUTPUT,
            (1280, 720),
        ),
        (
            VERTICAL_OUTPUT,
            (1080, 1920),
        ),
    ]

    results = []

    for destination, dimensions in outputs:
        log("Generating:", destination.relative_to(ROOT))
        result = create_thumbnail(
            source=source,
            title=title,
            dimensions=dimensions,
            destination=destination,
        )
        results.append(result)

    if len(results) != len(outputs) or not all(
        item.get("success") for item in results
    ):
        raise RuntimeError("One or more thumbnail outputs failed.")

    manifest = {
        "status": "completed",
        "version": VERSION,
        "format": format_name,
        "title": title,
        "title_source": title_source,
        "source_image": source.relative_to(ROOT).as_posix(),
        "source_manifest": hero["manifest"],
        "selected_scene": hero["scene"],
        "scene_score": hero["score"],
        "design": {
            "story_aware_title": True,
            "adaptive_scene_palette": True,
            "adaptive_focal_crop": True,
            "gradient_and_outline": True,
            "fixed_red_gold_theme": False,
        },
        "thumbnails": results,
    }

    write_json(MANIFEST_OUTPUT, manifest)

    # Verify every declared output after writing the manifest.
    for item in results:
        path = ROOT / item["file"]
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            raise RuntimeError(
                f"Thumbnail manifest points to a missing/invalid file: "
                f"{item['file']}"
            )

    log("Format:", format_name)
    log("Title:", title)
    log("Title source:", title_source)
    log("Selected scene:", hero["scene"])
    log("Generated thumbnail files:", len(results))
    log("Manifest:", MANIFEST_OUTPUT.relative_to(ROOT))
    log("THUMBNAIL GENERATION: SUCCESS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Thumbnail generation cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"THUMBNAIL GENERATION FAILED: {exc}")
        raise