#!/usr/bin/env python3
"""Story-aware cinematic thumbnail generator."""

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
VERSION = "thumbnail-v32-story-aware-focal-crop"


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
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


def get_story_title(config):
    # Prefer the completed story title, not an input fragment.
    for path in (AI_STORY, STORY):
        data = read_json(path)
        title = first_text(data, (
            "title", "final_title", "story_title", "generated_title"
        ))
        if title:
            return title, path.relative_to(ROOT).as_posix()

    if FINAL_TITLE.is_file():
        title = clean_text(FINAL_TITLE.read_text(encoding="utf-8-sig"))
        if title:
            return title, FINAL_TITLE.relative_to(ROOT).as_posix()

    topic = clean_text(config.get("TOPIC", ""))
    if topic:
        log("WARNING: completed story title missing; TOPIC fallback used.")
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
        "visual_path", "asset_path", "output", "file",
        "path", "image_path", "image",
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
    fields = (
        "prompt", "image_prompt", "visual_prompt", "scene_prompt",
        "description", "mood", "suspense", "hook", "camera",
    )
    text = " ".join(clean_text(job.get(k)) for k in fields).lower()

    positive = (
        "dramatic", "cinematic", "suspense", "mystery", "danger",
        "fear", "shock", "reveal", "climax", "storm", "fire",
        "dark", "night", "shadow", "chase", "battle", "emotional",
        "surprise", "secret", "haunted", "ghost", "expression",
        "close-up", "close up", "intense", "threat", "conflict",
        "twist", "face", "character",
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
        score += 4

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
        raise RuntimeError("No valid completed image found in visual_jobs.json.")

    candidates.sort(
        key=lambda item: (
            item["score"], -item["part"], -item["scene"]
        ),
        reverse=True,
    )

    chosen = candidates[0]
    log("HERO IMAGE:", chosen["path"])
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


def focal_point(image):
    """
    Estimate a visual focal point from image contrast and sharpness.
    This is an image-processing heuristic, not face recognition.
    """
    sample = image.convert("RGB")
    sample.thumbnail((240, 240))
    gray = sample.convert("L").filter(ImageFilter.FIND_EDGES)

    width, height = gray.size
    pixels = gray.load()

    weighted_x = 0.0
    weighted_y = 0.0
    total = 0.0

    for y in range(height):
        for x in range(width):
            weight = float(pixels[x, y]) + 1.0
            weighted_x += x * weight
            weighted_y += y * weight
            total += weight

    if total <= 0:
        return 0.5, 0.5

    return (
        max(0.15, min(0.85, weighted_x / total / width)),
        max(0.15, min(0.85, weighted_y / total / height)),
    )


def crop_to_ratio(image, ratio, focal=(0.5, 0.5)):
    width, height = image.size
    current = width / height
    fx, fy = focal

    if current > ratio:
        new_width = int(height * ratio)
        center_x = int(fx * width)
        left = max(0, min(width - new_width, center_x - new_width // 2))
        return image.crop((left, 0, left + new_width, height))

    new_height = int(width / ratio)
    center_y = int(fy * height)
    top = max(0, min(height - new_height, center_y - new_height // 2))
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
    lines = []
    current = ""

    for word in text.split():
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
        draw.line([(x, 0), (x, height)], fill=(0, 0, 0, alpha))
        draw.line(
            [(width - x - 1, 0), (width - x - 1, height)],
            fill=(0, 0, 0, alpha),
        )

    return Image.alpha_composite(
        image.convert("RGBA"), overlay
    ).convert("RGB")

def draw_title(image, title, vertical=False):
    image = image.convert("RGB")
    width, height = image.size

    palette = adaptive_palette(image)
    image = add_gradient(image, palette)
    image = add_side_vignette(image)

    draw = ImageDraw.Draw(image)
    title = shorten_title(title)

    max_width = int(width * 0.88)
    base_size = max(20, width // (11 if vertical else 18))
    min_size = max(17, width // 38)

    best_font = None
    best_lines = None
    best_size = base_size

    for size in range(base_size, min_size - 1, -1):
        font = get_font(size)
        lines = wrap_text(draw, title, font, max_width)

        if len(lines) <= 3:
            best_font = font
            best_lines = lines
            best_size = size
            break

    if best_font is None:
        best_font = get_font(min_size)
        best_lines = wrap_text(draw, title, best_font, max_width)[:3]
        best_size = min_size

    line_spacing = max(5, best_size // 5)
    line_heights = [
        draw.textbbox((0, 0), line, font=best_font, stroke_width=2)[3]
        - draw.textbbox((0, 0), line, font=best_font, stroke_width=2)[1]
        for line in best_lines
    ]

    total_height = sum(line_heights) + line_spacing * max(0, len(best_lines) - 1)

    if vertical:
        start_y = int(height * 0.68)
    else:
        start_y = int(height * 0.68)

    start_y = min(start_y, height - total_height - 35)

    accent_y = max(20, start_y - max(18, best_size // 2))
    accent_width = max(55, int(width * 0.13))

    draw.rounded_rectangle(
        (
            int(width * 0.06),
            accent_y,
            int(width * 0.06) + accent_width,
            accent_y + max(5, best_size // 12),
        ),
        radius=max(2, best_size // 20),
        fill=palette["accent"],
    )

    y = start_y

    for line, line_height in zip(best_lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=best_font, stroke_width=2)
        text_width = bbox[2] - bbox[0]

        x = max(24, (width - text_width) // 2)

        draw.text(
            (x + 3, y + 4),
            line,
            font=best_font,
            fill=(0, 0, 0),
            stroke_width=max(3, best_size // 12),
            stroke_fill=(0, 0, 0),
        )

        draw.text(
            (x, y),
            line,
            font=best_font,
            fill=palette["text"],
            stroke_width=max(2, best_size // 18),
            stroke_fill=palette["outline"],
        )

        y += line_height + line_spacing

    return image


def create_thumbnail(source, title, dimensions, destination):
    try:
        with Image.open(source) as original:
            image = original.convert("RGB")

        focal = focal_point(image)
        ratio = dimensions[0] / dimensions[1]

        image = crop_to_ratio(image, ratio, focal=focal)
        image = image.resize(dimensions, Image.Resampling.LANCZOS)
        image = draw_title(image, title, vertical=(dimensions[1] > dimensions[0]))

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        temp_path = destination.with_name(destination.stem + ".tmp.jpg")
        image.save(
            temp_path,
            format="JPEG",
            quality=94,
            optimize=True,
            progressive=True,
        )

        with Image.open(temp_path) as check:
            check.verify()

        temp_path.replace(destination)

        with Image.open(destination) as check:
            if check.size != dimensions:
                raise RuntimeError(
                    f"Thumbnail dimension mismatch: {check.size}, expected {dimensions}"
                )

        return True

    except Exception as exc:
        print(f"[THUMBNAIL] Failed: {destination}: {exc}")
        return False


def main():
    root = Path(__file__).resolve().parent.parent
    output_dir = root / "output"
    thumbnail_dir = output_dir / "thumbnail"

    output_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(root)

    mode = str(
        config.get("FORMAT")
        or config.get("OUTPUT_MODE")
        or "short"
    ).strip().lower()

    if mode not in ("short", "full"):
        mode = "short"

    title = get_story_title(root, config)

    if not title:
        title = str(config.get("TOPIC") or config.get("TITLE") or "Katha Lok AI")

    hero = choose_hero_visual(root, output_dir, mode)

    if not hero or not Path(hero).is_file():
        raise FileNotFoundError(
            "Thumbnail source image not found. Hero visual could not be selected."
        )

    outputs = [
        (
            output_dir / "thumbnail.jpg",
            (1280, 720),
        ),
        (
            thumbnail_dir / "thumbnail_1280x720.jpg",
            (1280, 720),
        ),
        (
            thumbnail_dir / "thumbnail_1080x1920.jpg",
            (1080, 1920),
        ),
    ]

    results = []

    for destination, dimensions in outputs:
        success = create_thumbnail(
            hero,
            title,
            dimensions,
            destination,
        )
        results.append(
            {
                "file": str(destination.relative_to(root)),
                "width": dimensions[0],
                "height": dimensions[1],
                "success": success,
            }
        )

    if not all(item["success"] for item in results):
        failed = [item["file"] for item in results if not item["success"]]
        raise RuntimeError(
            "Thumbnail generation failed: " + ", ".join(failed)
        )

    manifest = {
        "version": "thumbnail-v32-story-aware-focal-crop",
        "mode": mode,
        "title": title,
        "source_image": str(Path(hero).resolve().relative_to(root)),
        "thumbnails": results,
    }

    manifest_path = thumbnail_dir / "thumbnail_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[THUMBNAIL] Title: {title}")
    print(f"[THUMBNAIL] Source: {hero}")
    print(f"[THUMBNAIL] Generated {len(results)} thumbnail files.")
    print(f"[THUMBNAIL] Manifest: {manifest_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[THUMBNAIL] Interrupted.")
        raise
    except Exception as exc:
        print(f"[THUMBNAIL] ERROR: {exc}")
        raise