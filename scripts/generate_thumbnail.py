#!/usr/bin/env python3
"""Generate story-based cinematic thumbnails compatible with final validation."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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


def log(*args):
    print(*args, flush=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def write_json(path, data):
    path = Path(path)
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


def read_topic(config):
    return clean_text(config.get("TOPIC", ""))


def get_story_title(config):
    for path in (AI_STORY, STORY):
        data = read_json(path)

        if not isinstance(data, dict):
            continue

        for key in (
            "title",
            "final_title",
            "story_title",
            "generated_title",
        ):
            title = clean_text(data.get(key))
            if title:
                return title, path.relative_to(ROOT).as_posix()

    if FINAL_TITLE.is_file():
        title = clean_text(
            FINAL_TITLE.read_text(encoding="utf-8-sig")
        )
        if title:
            return title, FINAL_TITLE.relative_to(ROOT).as_posix()

    topic = read_topic(config)

    if topic:
        log("WARNING: Story title unavailable; TOPIC fallback used.")
        return topic, "Input/topic.txt fallback"

    raise RuntimeError(
        "No story title found in AI story, story file, "
        "final_title.txt or TOPIC."
    )


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
    if not isinstance(job, dict):
        return None

    for key in (
        "visual_path",
        "asset_path",
        "output",
        "file",
        "path",
        "image_path",
    ):
        path = resolve_image(job.get(key))
        if path:
            return path

    return None


def load_visual_jobs():
    data = read_json(VISUAL_MANIFEST)

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Visual manifest missing or invalid: {VISUAL_MANIFEST}"
        )

    jobs = data.get("jobs")

    if isinstance(jobs, dict):
        return [(str(key), value) for key, value in jobs.items()
                if isinstance(value, dict)]

    if isinstance(jobs, list):
        return [(str(index), value) for index, value in enumerate(jobs)
                if isinstance(value, dict)]

    for key in ("scenes", "visuals", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [(str(index), value) for index, value in enumerate(rows)
                    if isinstance(value, dict)]

    raise RuntimeError("Visual manifest has no supported jobs collection.")


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
            scene = int(job.get("scene", job.get("local_scene", 0)))
        except (TypeError, ValueError):
            match = re.search(
                r"part[_/](\d+).*scene[_/](\d+)",
                key,
                re.IGNORECASE,
            )
            if match:
                part, scene = int(match.group(1)), int(match.group(2))
            else:
                part, scene = 999999, 999999

        candidates.append({
            "part": part,
            "scene": scene,
            "path": path,
            "key": key,
        })

    if candidates:
        candidates.sort(
            key=lambda item: (item["part"], item["scene"])
        )
        chosen = candidates[0]
        log(
            "Selected hero visual:",
            chosen["part"],
            chosen["scene"],
            chosen["path"],
        )
        return chosen

    # Fallback only when the visual manifest has no usable completed image.
    search_dirs = [
        ROOT / "output" / "visuals",
        ROOT / "output" / "images",
    ]

    extensions = {".png", ".jpg", ".jpeg", ".webp"}
    found = []

    for directory in search_dirs:
        if not directory.is_dir():
            continue

        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in extensions:
                if valid_image(path):
                    found.append(path)

    if not found:
        raise RuntimeError("No valid completed visual image found.")

    log("WARNING: Using first valid image as fallback:", found[0])

    return {
        "part": 0,
        "scene": 0,
        "path": found[0],
        "key": "fallback",
    }


def find_font(size):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/mukta/Mukta-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    for value in candidates:
        path = Path(value)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                pass

    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except Exception as exc:
        raise RuntimeError("No usable thumbnail font is installed.") from exc


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


def sample_accent(image):
    small = image.resize((1, 1)).convert("RGB")
    red, green, blue = small.getpixel((0, 0))

    if blue > red * 1.15:
        return (100, 190, 255)
    if green > red * 1.15:
        return (130, 230, 160)
    if red > blue * 1.25:
        return (255, 145, 105)

    return (255, 205, 115)


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


def add_cinematic_overlay(image, title, vertical=False):
    image = image.convert("RGB")
    width, height = image.size
    accent = sample_accent(image)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    start_y = int(height * (0.55 if vertical else 0.56))

    for y in range(start_y, height):
        progress = (y - start_y) / max(1, height - start_y)
        alpha = int(15 + progress * 190)
        overlay_draw.line(
            [(0, y), (width, y)],
            fill=(0, 0, 0, alpha),
        )

    image = Image.alpha_composite(
        image.convert("RGBA"), overlay
    ).convert("RGB")

    draw = ImageDraw.Draw(image)

    if vertical:
        max_width = int(width * 0.86)
        max_height = int(height * 0.27)
        base_size = max(38, width // 12)
        title_y = int(height * 0.69)
    else:
        max_width = int(width * 0.88)
        max_height = int(height * 0.29)
        base_size = max(34, width // 19)
        title_y = int(height * 0.68)

    font = None
    lines = []

    for size in range(base_size, max(18, base_size // 2), -2):
        candidate = find_font(size)
        candidate_lines = wrap_text(
            draw, title, candidate, max_width
        )
        line_height = int(size * 1.28)

        if (
            len(candidate_lines) <= 3
            and len(candidate_lines) * line_height <= max_height
        ):
            font = candidate
            lines = candidate_lines
            break

    if font is None:
        font = find_font(max(18, base_size // 2))
        lines = wrap_text(draw, title, font, max_width)[:3]

    line_height = int(font.size * 1.28)
    accent_y = title_y - int(font.size * 0.45)

    draw.rounded_rectangle(
        (
            int(width * 0.08),
            accent_y,
            int(width * 0.30),
            accent_y + max(5, font.size // 16),
        ),
        radius=5,
        fill=accent,
    )

    y = title_y

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) // 2

        draw.text(
            (x + 4, y + 5),
            line,
            font=font,
            fill=(0, 0, 0),
            stroke_width=6,
            stroke_fill=(0, 0, 0),
        )

        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 255, 255),
            stroke_width=3,
            stroke_fill=(20, 20, 20),
        )

        y += line_height

    return image


def create_thumbnail(source, title, dimensions, destination):
    if not valid_image(source):
        raise RuntimeError(f"Invalid thumbnail source: {source}")

    with Image.open(source) as opened:
        image = opened.convert("RGB")

    ratio = dimensions[0] / dimensions[1]
    image = crop_to_ratio(image, ratio)
    image = image.resize(dimensions, Image.Resampling.LANCZOS)

    image = add_cinematic_overlay(
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
            raise RuntimeError(
                f"Generated thumbnail failed image validation: {temporary}"
            )

        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    log("=" * 65)
    log("KATHA LOK AI — STORY-BASED CINEMATIC THUMBNAILS")
    log("=" * 65)

    config = load_and_validate(INPUT)
    mode = get_format(config)

    title, title_source = get_story_title(config)
    hero = choose_hero_visual()
    source = hero["path"]

    log("FORMAT:", mode)
    log("TITLE:", title)
    log("TITLE SOURCE:", title_source)
    log("HERO IMAGE:", source)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # The final validator and ZIP packager require this exact primary path.
    create_thumbnail(
        source,
        title,
        (1280, 720),
        THUMBNAIL_MAIN,
    )

    # Keep the organized 16:9 copy and vertical thumbnail too.
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

    for path, expected_size in (
        (THUMBNAIL_MAIN, (1280, 720)),
        (THUMBNAIL_16_9, (1280, 720)),
        (THUMBNAIL_VERTICAL, (1080, 1920)),
    ):
        if not valid_image(path):
            raise RuntimeError(f"Thumbnail output invalid: {path}")

        with Image.open(path) as image:
            if image.size != expected_size:
                raise RuntimeError(
                    f"Thumbnail dimensions mismatch: {path}: "
                    f"{image.size}, expected {expected_size}"
                )

    manifest = {
        "status": "completed",
        "version": "thumbnail-v30-validator-compatible",
        "format": mode,
        "title": title,
        "title_source": title_source,
        "source_visual": source.relative_to(ROOT).as_posix(),
        "source_part": hero["part"],
        "source_scene": hero["scene"],
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

    log("")
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