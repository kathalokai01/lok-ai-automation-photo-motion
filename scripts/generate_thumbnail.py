#!/usr/bin/env python3

import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter


TOPIC_FILE = Path("Input/topic.txt")
AI_STORY_FILE = Path("output/story/ai_story.json")
STORY_FILE = Path("output/story/story.json")
FINAL_TITLE_FILE = Path("output/story/final_title.txt")

VISUAL_MANIFEST = Path("output/visuals/visual_jobs.json")
SCENES_FILE = Path("output/scenes/scenes.json")

OUTPUT_DIR = Path("output/thumbnail")

THUMBNAIL_16_9 = OUTPUT_DIR / "thumbnail_1280x720.jpg"
THUMBNAIL_VERTICAL = OUTPUT_DIR / "thumbnail_1080x1920.jpg"
THUMBNAIL_MANIFEST = OUTPUT_DIR / "thumbnail_manifest.json"


def load_json(path):
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: Could not read {path}: {exc}")
        return None


def clean_text(value):
    if not isinstance(value, str):
        return ""

    value = re.sub(r"\s+", " ", value).strip()
    return value.strip("\"' ")


def read_topic():
    if not TOPIC_FILE.is_file():
        return ""

    for line in TOPIC_FILE.read_text(
        encoding="utf-8"
    ).splitlines():

        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        if key.strip().upper() == "TOPIC":
            return clean_text(value)

    return ""


def get_story_title():
    for path in (AI_STORY_FILE, STORY_FILE):
        data = load_json(path)

        if not isinstance(data, dict):
            continue

        for key in (
            "title",
            "final_title",
            "story_title",
            "generated_title",
        ):
            title = clean_text(data.get(key, ""))
            if title:
                return title, str(path)

    if FINAL_TITLE_FILE.is_file():
        title = clean_text(
            FINAL_TITLE_FILE.read_text(encoding="utf-8")
        )
        if title:
            return title, str(FINAL_TITLE_FILE)

    topic = read_topic()

    if topic:
        print(
            "WARNING: Story title unavailable; "
            "using TOPIC as fallback."
        )
        return topic, "Input/topic.txt fallback"

    raise RuntimeError(
        "No usable story title found in AI story, "
        "story file, final_title.txt or TOPIC."
    )


def valid_image(path):
    if not path.is_file():
        return False

    if path.stat().st_size < 1000:
        return False

    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def get_visual_path(job):
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
        value = job.get(key)

        if not value:
            continue

        path = Path(str(value))

        if not path.is_absolute():
            path = Path(".") / path

        if valid_image(path):
            return path

    return None


def load_visual_jobs():
    data = load_json(VISUAL_MANIFEST)

    if not isinstance(data, dict):
        raise RuntimeError(
            "Visual manifest missing or invalid: "
            f"{VISUAL_MANIFEST}"
        )

    jobs = data.get("jobs", {})

    # Current prepare_visuals.py writes jobs as a dictionary:
    # part_01/scene_01 -> {status, visual_path, ...}
    if isinstance(jobs, dict):
        result = []

        for key, job in jobs.items():
            if not isinstance(job, dict):
                continue

            result.append((str(key), job))

        return result

    # Backward compatibility for list-based manifests.
    if isinstance(jobs, list):
        return [
            (str(index), job)
            for index, job in enumerate(jobs)
            if isinstance(job, dict)
        ]

    for key in ("scenes", "visuals", "items"):
        value = data.get(key)

        if isinstance(value, list):
            return [
                (str(index), job)
                for index, job in enumerate(value)
                if isinstance(job, dict)
            ]

    raise RuntimeError(
        "Visual manifest contains no supported jobs collection."
    )


def scene_image_candidates():
    entries = load_visual_jobs()

    candidates = []

    for key, job in entries:
        status = str(job.get("status", "")).lower()

        if status and status not in {
            "completed",
            "complete",
            "success",
            "done",
            "generated",
        }:
            continue

        path = get_visual_path(job)

        if path is None:
            continue

        try:
            part = int(job.get("part", 0))
            scene = int(job.get("scene", 0))
        except (TypeError, ValueError):
            match = re.search(
                r"part[_/](\d+).*scene[_/](\d+)",
                key,
                re.IGNORECASE,
            )

            if match:
                part = int(match.group(1))
                scene = int(match.group(2))
            else:
                part, scene = 999999, 999999

        candidates.append({
            "part": part,
            "scene": scene,
            "path": path,
            "key": key,
            "job": job,
        })

    candidates.sort(
        key=lambda item: (
            item["part"],
            item["scene"],
        )
    )

    if candidates:
        return candidates

    # Fallback for old projects with missing/incomplete manifest.
    search_dirs = [
        Path("output/visuals"),
        Path("output"),
    ]

    extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
    }

    found = []

    for directory in search_dirs:
        if not directory.exists():
            continue

        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and path.suffix.lower() in extensions
                and valid_image(path)
            ):
                found.append(path)

    if found:
        print(
            "WARNING: No usable manifest job found; "
            "using first valid image as fallback."
        )

        return [{
            "part": 999999,
            "scene": 999999,
            "path": found[0],
            "key": "fallback",
            "job": {},
        }]

    raise RuntimeError(
        "No completed visual image found."
    )


def choose_hero_visual():
    candidates = scene_image_candidates()

    # Prefer the opening scene because it normally carries
    # the story hook and establishes the main characters.
    first_part = min(item["part"] for item in candidates)

    first_part_items = [
        item for item in candidates
        if item["part"] == first_part
    ]

    chosen = min(
        first_part_items,
        key=lambda item: item["scene"],
    )

    print(
        "Selected thumbnail visual: "
        f"part={chosen['part']} "
        f"scene={chosen['scene']} "
        f"path={chosen['path']}"
    )

    return chosen


def find_font(size):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    for font_path in candidates:
        path = Path(font_path)

        if path.is_file():
            try:
                return ImageFont.truetype(
                    str(path),
                    size=size,
                )
            except Exception:
                continue

    try:
        return ImageFont.truetype(
            "DejaVuSans-Bold.ttf",
            size=size,
        )
    except Exception as exc:
        raise RuntimeError(
            "No usable thumbnail font is installed."
        ) from exc


def crop_to_ratio(image, target_ratio):
    width, height = image.size
    current_ratio = width / height

    if current_ratio > target_ratio:
        new_width = int(height * target_ratio)
        left = (width - new_width) // 2

        return image.crop((
            left,
            0,
            left + new_width,
            height,
        ))

    new_height = int(width / target_ratio)
    top = (height - new_height) // 2

    return image.crop((
        0,
        top,
        width,
        top + new_height,
    ))


def wrap_text(draw, text, font, max_width):
    words = text.split()

    if not words:
        return []

    lines = []
    current = ""

    for word in words:
        candidate = (
            word if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            candidate,
            font=font,
        )

        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)

            current = word

    if current:
        lines.append(current)

    return lines


def sample_accent(image):
    small = image.resize((1, 1)).convert("RGB")
    red, green, blue = small.getpixel((0, 0))

    # Derive an accent from the actual scene instead of
    # applying the same red/gold treatment to every story.
    if blue > red * 1.15:
        return (100, 190, 255)

    if green > red * 1.15:
        return (130, 230, 160)

    if red > blue * 1.25:
        return (255, 145, 105)

    return (255, 205, 115)


def add_cinematic_overlay(image, title, vertical=False):
    image = image.convert("RGB")
    width, height = image.size

    accent = sample_accent(image)

    overlay = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0),
    )

    draw_overlay = ImageDraw.Draw(overlay)

    # Darken only the title area, retaining the original
    # scene image and its important visual details.
    if vertical:
        start_y = int(height * 0.55)
    else:
        start_y = int(height * 0.56)

    for y in range(start_y, height):
        progress = (y - start_y) / max(1, height - start_y)

        alpha = int(15 + progress * 185)

        draw_overlay.line(
            [(0, y), (width, y)],
            fill=(0, 0, 0, alpha),
        )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        overlay,
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

    # Reduce font size until the full title fits.
    for size in range(base_size, max(18, base_size // 2), -2):
        candidate_font = find_font(size)

        candidate_lines = wrap_text(
            draw,
            title,
            candidate_font,
            max_width,
        )

        line_height = int(size * 1.28)

        if (
            len(candidate_lines) <= 3
            and len(candidate_lines) * line_height <= max_height
        ):
            font = candidate_font
            lines = candidate_lines
            break

    if font is None:
        font = find_font(max(18, base_size // 2))
        lines = wrap_text(
            draw,
            title,
            font,
            max_width,
        )[:3]

    line_height = int(font.size * 1.28)

    # Accent line creates a cinematic title treatment.
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
        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font,
            stroke_width=3,
        )

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


def create_thumbnail(source, title, size, output):
    if not valid_image(source):
        raise RuntimeError(
            f"Invalid thumbnail source image: {source}"
        )

    with Image.open(source) as opened:
        image = opened.convert("RGB")

    image = crop_to_ratio(
        image,
        size[0] / size[1],
    )

    image = image.resize(
        size,
        Image.Resampling.LANCZOS,
    )

    vertical = size[1] > size[0]

    image = add_cinematic_overlay(
        image,
        title,
        vertical=vertical,
    )

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = output.with_suffix(".tmp.jpg")

    image.save(
        temporary,
        "JPEG",
        quality=94,
        optimize=True,
        progressive=True,
    )

    if not valid_image(temporary):
        raise RuntimeError(
            f"Thumbnail validation failed: {temporary}"
        )

    temporary.replace(output)


def main():
    print("=" * 65)
    print("       STORY-BASED CINEMATIC THUMBNAILS")
    print("=" * 65)

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    title, title_source = get_story_title()

    print(f"Story title: {title}")
    print(f"Title source: {title_source}")

    hero = choose_hero_visual()
    source = hero["path"]

    print(f"Hero image: {source}")

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

    manifest = {
        "status": "completed",
        "title": title,
        "title_source": title_source,
        "source_visual": str(source),
        "source_part": hero["part"],
        "source_scene": hero["scene"],
        "thumbnails": [
            {
                "type": "16:9",
                "width": 1280,
                "height": 720,
                "path": str(THUMBNAIL_16_9),
                "size_bytes": THUMBNAIL_16_9.stat().st_size,
            },
            {
                "type": "9:16",
                "width": 1080,
                "height": 1920,
                "path": str(THUMBNAIL_VERTICAL),
                "size_bytes": THUMBNAIL_VERTICAL.stat().st_size,
            },
        ],
    }

    temporary = THUMBNAIL_MANIFEST.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    temporary.replace(THUMBNAIL_MANIFEST)

    print("\n========== THUMBNAILS COMPLETE ==========")
    print(f"16:9: {THUMBNAIL_16_9}")
    print(f"9:16: {THUMBNAIL_VERTICAL}")
    print(f"Manifest: {THUMBNAIL_MANIFEST}")
    print("==========================================")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)