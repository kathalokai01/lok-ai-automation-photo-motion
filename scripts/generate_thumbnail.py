#!/usr/bin/env python3
"""Create story-aware cinematic thumbnails from completed visual assets."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format

INPUT = ROOT / "Input" / "topic.txt"
AI_STORY = ROOT / "output" / "story" / "ai_story.json"
STORY = ROOT / "output" / "story" / "story.json"
FINAL_TITLE = ROOT / "output" / "story" / "final_title.txt"
VISUAL_MANIFEST = ROOT / "output" / "visuals" / "visual_jobs.json"

OUTPUT_DIR = ROOT / "output" / "thumbnail"
THUMBNAIL_MAIN = ROOT / "output" / "thumbnail.jpg"
THUMBNAIL_16_9 = OUTPUT_DIR / "thumbnail_1280x720.jpg"
THUMBNAIL_VERTICAL = OUTPUT_DIR / "thumbnail_1080x1920.jpg"
MANIFEST = OUTPUT_DIR / "thumbnail_manifest.json"

MIN_IMAGE_BYTES = 1000
VERSION = "thumbnail-v31-story-hero-cinematic-adaptive"


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def write_json(path, data):
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


def clean_text(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().strip("\"' ")


def first_text(data, keys):
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = clean_text(data.get(key))
        if value:
            return value
    return ""


def get_story_data():
    for path in (AI_STORY, STORY):
        data = read_json(path)
        if isinstance(data, dict):
            return data, path.relative_to(ROOT).as_posix()
    return {}, ""


def get_story_title(config):
    data, source = get_story_data()

    title = first_text(data, (
        "title",
        "final_title",
        "story_title",
        "generated_title",
    ))

    if title:
        return title, source

    if FINAL_TITLE.is_file():
        title = clean_text(FINAL_TITLE.read_text(encoding="utf-8-sig"))
        if title:
            return title, FINAL_TITLE.relative_to(ROOT).as_posix()

    topic = clean_text(config.get("TOPIC", ""))
    if topic:
        log("WARNING: Story title missing; TOPIC fallback used.")
        return topic, "Input/topic.txt"

    raise RuntimeError("Story title is missing.")


def valid_image(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        return False

    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def resolve_image(value):
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

    return path if valid_image(path) else None


def job_image(job):
    for key in (
        "visual_path",
        "asset_path",
        "output",
        "file",
        "path",
        "image_path",
        "image",
    ):
        path = resolve_image(job.get(key))
        if path:
            return path
    return None


def load_visual_jobs():
    data = read_json(VISUAL_MANIFEST)

    if not isinstance(data, dict):
        raise RuntimeError("Visual jobs manifest is missing or invalid.")

    jobs = data.get("jobs")

    if isinstance(jobs, dict):
        return [(str(k), v) for k, v in jobs.items()
                if isinstance(v, dict)]

    if isinstance(jobs, list):
        return [(str(i), v) for i, v in enumerate(jobs)
                if isinstance(v, dict)]

    for key in ("scenes", "visuals", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [(str(i), v) for i, v in enumerate(rows)
                    if isinstance(v, dict)]

    raise RuntimeError("Visual jobs collection is unsupported.")


def scene_score(job):
    """Prefer visually dramatic scenes instead of always selecting scene 1."""
    text_fields = (
        "prompt",
        "image_prompt",
        "visual_prompt",
        "scene_prompt",
        "description",
        "mood",
        "suspense",
        "hook",
        "camera",
    )

    text = " ".join(
        clean_text(job.get(key)) for key in text_fields
    ).lower()

    positive = (
        "dramatic", "cinematic", "suspense", "mystery",
        "danger", "fear", "shock", "reveal", "climax",
        "storm", "fire", "dark", "night", "shadow",
        "chase", "battle", "crying", "emotional",
        "surprise", "secret", "haunted", "ghost",
        "expression", "close-up", "close up",
        "intense", "threat", "conflict", "twist",
    )

    negative = (
        "empty room", "plain background", "establishing shot",
        "wide landscape", "title card", "text overlay",
    )

    score = sum(3 for word in positive if word in text)
    score -= sum(2 for word in negative if word in text)

    if job.get("hook") or job.get("is_hook"):
        score += 8

    if job.get("is_climax") or job.get("climax"):
        score += 10

    if job.get("is_suspense") or job.get("suspense"):
        score += 5

    if job.get("character_focus") or job.get("close_up"):
        score += 3

    return score


def choose_hero_visual():
    candidates = []

    for key, job in load_visual_jobs():
        status = str(job.get("status", "")).lower()

        if status and status not in {
            "completed", "complete", "success", "done", "generated"
        }:
            continue

        path = job_image(job)
        if not path:
            continue

        try:
            part = int(job.get("part", 0))
            scene = int(job.get(
                "global_scene",
                job.get("scene", job.get("local_scene", 0)),
            ))
        except (TypeError, ValueError):
            part, scene = 999999, 999999

        candidates.append({
            "part": part,
            "scene": scene,
            "path": path,
            "key": key,
            "score": scene_score(job),
            "prompt": clean_text(
                job.get("visual_prompt")
                or job.get("image_prompt")
                or job.get("prompt")
                or job.get("description")
            ),
        })

    if not candidates:
        raise RuntimeError(
            "No valid completed image found in visual_jobs.json."
        )

    candidates.sort(
        key=lambda item: (
            item["score"],
            -item["part"],
            -item["scene"],
        ),
        reverse=True,
    )

    chosen = candidates[0]

    log("HERO IMAGE SELECTED:", chosen["path"])
    log("HERO SCENE:", chosen["scene"])
    log("HERO SCORE:", chosen["score"])

    return chosen


def find_font(size):
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

    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except Exception as exc:
        raise RuntimeError("No usable thumbnail font found.") from exc


def crop_to_ratio(image, ratio):
    width, height = image.size
    current = width / height

    if current > ratio:
        new_width = int(height * ratio)
        left = (width - new_width) // 2
        return image.crop((left, 0, left + new_width, height))

    new_height = int(width / ratio)
    top = (height - new_height) // 2
    return image.crop((0, top, width, top + new_height))


def sample_palette(image):
    small = image.resize((32, 32)).convert("RGB")
    pixels = list(small.getdata())

    red = sum(p[0] for p in pixels) / len(pixels)
    green = sum(p[1] for p in pixels) / len(pixels)
    blue = sum(p[2] for p in pixels) / len(pixels)
    brightness = (red + green + blue) / 3

    if brightness < 75:
        return {
            "accent": (255, 190, 65),
            "text": (255, 248, 225),
            "shadow": (0, 0, 0),
        }

    if blue > red * 1.12:
        return {
            "accent": (110, 195, 255),
            "text": (245, 250, 255),
            "shadow": (5, 12, 30),
        }

    if green > red * 1.12:
        return {
            "accent": (125, 235, 170),
            "text": (245, 255, 245),
            "shadow": (5, 25, 15),
        }

    if red > blue * 1.2:
        return {
            "accent": (255, 135, 95),
            "text": (255, 245, 235),
            "shadow": (35, 5, 5),
        }

    return {
        "accent": (255, 205, 100),
        "text": (255, 250, 235),
        "shadow": (25, 15, 5),
    }


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word
        box = draw.textbbox((0, 0), candidate, font=font)

        if box[2] - box[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def shorten_title(title, max_chars=85):
    title = clean_text(title)
    if len(title) <= max_chars:
        return title

    shortened = title[:max_chars]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]

    return shortened.rstrip(" ,.!?;:") + "…"


def add_gradient(image, vertical=False):
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    start = int(height * (0.48 if vertical else 0.52))

    for y in range(start, height):
        progress = (y - start) / max(1, height - start)
        alpha = int(10 + progress * 205)
        draw.line(
            [(0, y), (width, y)],
            fill=(0, 0, 0, alpha),
        )

    return Image.alpha_composite(
        image.convert("RGBA"), overlay
    ).convert("RGB")


def add_side_vignette(image):
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    band = max(1, int(width * 0.12))

    for x in range(band):
        alpha = int(95 * (1 - x / band))
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


def draw_title(image, title, vertical=False):
    width, height = image.size
    palette = sample_palette(image)

    image = add_gradient(image, vertical)
    image = add_side_vignette(image)
    draw = ImageDraw.Draw(image)

    title = shorten_title(title, 75 if vertical else 65)

    if vertical:
        max_width = int(width * 0.88)
        max_height = int(height * 0.24)
        base_size = max(38, width // 11)
        title_y = int(height * 0.70)
    else:
        max_width = int(width * 0.88)
        max_height = int(height * 0.30)
        base_size = max(34, width // 18)
        title_y = int(height * 0.67)

    selected_font = None
    selected_lines = []

    for size in range(base_size, 17, -2):
        font = find_font(size)
        lines = wrap_text(draw, title, font, max_width)
        line_height = int(size * 1.22)

        if len(lines) <= 3 and len(lines) * line_height <= max_height:
            selected_font = font
            selected_lines = lines
            break

    if selected_font is None:
        selected_font = find_font(20)
        selected_lines = wrap_text(
            draw, title, selected_font, max_width
        )[:3]

    font = selected_font
    line_height = int(font.size * 1.22)

    accent_y = title_y - max(12, font.size // 2)
    draw.rounded_rectangle(
        (
            int(width * 0.07),
            accent_y,
            int(width * 0.34),
            accent_y + max(5, font.size // 15),
        ),
        radius=5,
        fill=palette["accent"],
    )

    y = title_y

    for line in selected_lines:
        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font,
            stroke_width=4,
        )
        text_width = bbox[2] - bbox[0]
        x = max(8, (width - text_width) // 2)

        draw.text(
            (x + 3, y + 5),
            line,
            font=font,
            fill=palette["shadow"],
            stroke_width=8,
            stroke_fill=palette["shadow"],
        )

        draw.text(
            (x, y),
            line,
            font=font,
            fill=palette["text"],
            stroke_width=4,
            stroke_fill=palette["shadow"],
        )

        y += line_height

    return image


def create_thumbnail(source, title, dimensions, destination):
    if not valid_image(source):
        raise RuntimeError(f"Invalid thumbnail source: {source}")

    with Image.open(source) as opened:
        image = opened.convert("RGB")

    image = ImageEnhance.Color(image).enhance(1.12)
    image = ImageEnhance.Contrast(image).enhance(1.10)
    image = ImageEnhance.Sharpness(image).enhance(1.15)

    ratio = dimensions[0] / dimensions[1]
    image = crop_to_ratio(image, ratio)
    image = image.resize(dimensions, Image.Resampling.LANCZOS)

    image = draw_title(
        image,
        title,
        vertical=dimensions[1] > dimensions[0],
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.jpg")

    try:
        image.save(
            temporary,
            "JPEG",
            quality=94,
            optimize=True,
            progressive=True,
        )

        if not valid_image(temporary):
            raise RuntimeError("Generated thumbnail failed validation.")

        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    log("=" * 65)
    log("KATHA LOK AI — STORY-AWARE CINEMATIC THUMBNAILS")
    log("=" * 65)

    config = load_and_validate(INPUT)
    mode = str(get_format(config)).strip().lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    title, title_source = get_story_title(config)
    hero = choose_hero_visual()
    source = hero["path"]

    log("FORMAT:", mode)
    log("TITLE:", title)
    log("TITLE SOURCE:", title_source)
    log("HERO IMAGE:", source)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    create_thumbnail(
        source,
        title,
        (1280, 720),
        THUMBNAIL_MAIN,
    )

    create_thumbnail(
        source,
        title,
        (1280, 720),
        THUMBNAIL_16_9,
    )

    create_thumbnail(
        source,
        title,
        (1080, 1920),
        THUMBNAIL_VERTICAL,
    )

    outputs = (
        (THUMBNAIL_MAIN, (1280, 720)),
        (THUMBNAIL_16_9, (1280, 720)),
        (THUMBNAIL_VERTICAL, (1080, 1920)),
    )

    for path, expected_size in outputs:
        if not valid_image(path):
            raise RuntimeError(f"Invalid thumbnail output: {path}")

        with Image.open(path) as image:
            if image.size != expected_size:
                raise RuntimeError(
                    f"Thumbnail dimensions mismatch: {path}; "
                    f"found {image.size}, expected {expected_size}"
                )

    manifest = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "title": title,
        "title_source": title_source,
        "source_visual": source.relative_to(ROOT).as_posix(),
        "source_part": hero["part"],
        "source_scene": hero["scene"],
        "source_scene_score": hero["score"],
        "source_visual_prompt": hero["prompt"],
        "thumbnail": THUMBNAIL_MAIN.relative_to(ROOT).as_posix(),
        "path": THUMBNAIL_MAIN.relative_to(ROOT).as_posix(),
        "output": THUMBNAIL_MAIN.relative_to(ROOT).as_posix(),
        "thumbnails": [
            {
                "type": "primary-16:9",
                "width": 1280,
                "height": 720,
                "path": THUMBNAIL_MAIN.relative_to(ROOT).as_posix(),
                "size_bytes": THUMBNAIL_MAIN.stat().st_size,
            },
            {
                "type": "16:9",
                "width": 1280,
                "height": 720,
                "path": THUMBNAIL_16_9.relative_to(ROOT).as_posix(),
                "size_bytes": THUMBNAIL_16_9.stat().st_size,
            },
            {
                "type": "9:16",
                "width": 1080,
                "height": 1920,
                "path": THUMBNAIL_VERTICAL.relative_to(ROOT).as_posix(),
                "size_bytes": THUMBNAIL_VERTICAL.stat().st_size,
            },
        ],
    }

    write_json(MANIFEST, manifest)

    log("THUMBNAILS COMPLETE")
    log("FORMAT:", mode)
    log("PRIMARY:", THUMBNAIL_MAIN.relative_to(ROOT))
    log("16:9:", THUMBNAIL_16_9.relative_to(ROOT))
    log("9:16:", THUMBNAIL_VERTICAL.relative_to(ROOT))
    log("MANIFEST:", MANIFEST.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("THUMBNAIL GENERATION INTERRUPTED")
        sys.exit(130)
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", exc)
        sys.exit(1)