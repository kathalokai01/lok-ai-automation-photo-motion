#!/usr/bin/env python3
"""Story-aware cinematic thumbnail generator with safe title selection."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format

OUT = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = OUT / "story" / "ai_story.json"
TITLE_FILE = OUT / "story" / "final_title.txt"
VISUAL_MANIFEST = OUT / "visuals" / "image_manifest.json"
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"

THUMBNAIL = OUT / "thumbnail.jpg"
WORK_DIR = OUT / "thumbnail"
MANIFEST = WORK_DIR / "thumbnail_manifest.json"

VERSION = "thumbnail-v13-title-priority-manifest-aligned"
WIDTH, HEIGHT = 1280, 720
MIN_BYTES = 1000

STYLES = {
    "horror": {
        "accent": (235, 83, 91),
        "text": (255, 246, 242),
        "shadow": (26, 14, 23),
    },
    "mystery": {
        "accent": (78, 215, 225),
        "text": (247, 253, 255),
        "shadow": (10, 25, 40),
    },
    "devotional": {
        "accent": (255, 190, 75),
        "text": (255, 250, 230),
        "shadow": (57, 32, 17),
    },
    "historical": {
        "accent": (218, 174, 112),
        "text": (255, 246, 225),
        "shadow": (39, 28, 22),
    },
    "emotional": {
        "accent": (244, 148, 173),
        "text": (255, 249, 248),
        "shadow": (43, 25, 35),
    },
    "nature": {
        "accent": (139, 216, 143),
        "text": (249, 255, 243),
        "shadow": (17, 39, 28),
    },
    "general": {
        "accent": (112, 194, 255),
        "text": (255, 252, 244),
        "shadow": (17, 24, 36),
    },
}

KEYWORDS = {
    "horror": (
        "भूत", "प्रेत", "डर", "खौफ", "चुड़ैल", "आत्मा", "हवेली",
        "शैतान", "horror", "ghost", "haunted", "witch", "demon",
    ),
    "mystery": (
        "रहस्य", "गुमशुदा", "गायब", "पहेली", "अनजान", "खोज",
        "mystery", "secret", "unknown", "disappearance",
    ),
    "devotional": (
        "भक्ति", "भगवान", "देवी", "मंदिर", "पूजा", "कृष्ण", "राम",
        "शिव", "विष्णु", "हनुमान", "दुर्गा", "गणेश", "devotional",
    ),
    "historical": (
        "राजा", "रानी", "सम्राट", "महाराज", "युद्ध", "किला", "इतिहास",
        "योद्धा", "साम्राज्य", "historical", "king", "queen", "warrior",
    ),
    "emotional": (
        "माँ", "मां", "पिता", "परिवार", "बेटा", "बेटी", "प्यार",
        "प्रेम", "बलिदान", "दर्द", "mother", "father", "family", "love",
    ),
    "nature": (
        "जंगल", "वन", "पहाड़", "नदी", "प्रकृति", "गाँव", "गांव",
        "किसान", "forest", "nature", "mountain", "river", "village",
    ),
}

FONT_DIRS = (
    ROOT / "assets" / "fonts",
    Path("/usr/share/fonts/truetype"),
    Path("/usr/local/share/fonts"),
)

FONT_NAMES = (
    "NotoSansDevanagari-Bold.ttf",
    "Mukta-Bold.ttf",
    "Hind-Bold.ttf",
    "NotoSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)

_FONT_PATH = None


def log(*values):
    print(*values, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def clean(value, limit=500):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_image(value):
    if not isinstance(value, str) or not value.strip():
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()

        if not path.is_relative_to(ROOT.resolve()):
            return None
        if not path.is_file() or path.stat().st_size < MIN_BYTES:
            return None
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            return None

        with Image.open(path) as image:
            image.verify()

        return path
    except (OSError, ValueError):
        return None


def read_input_topic(config):
    # Only a genuine TOPIC/TITLE field counts as an explicit title.
    for key in ("TOPIC", "TITLE"):
        value = clean(str(config.get(key, "")), 180)
        if value:
            return value
    return ""


def title_from_story_json(story):
    if not isinstance(story, dict):
        return ""

    # Prefer an explicit generated title over a narrative sentence.
    for key in ("title", "story_title", "generated_title"):
        value = clean(story.get(key), 180)
        if value:
            return value

    for container_key in ("metadata", "story_metadata"):
        metadata = story.get(container_key)
        if not isinstance(metadata, dict):
            continue

        for key in ("title", "story_title", "generated_title"):
            value = clean(metadata.get(key), 180)
            if value:
                return value

    return ""


def title_from_summary(story):
    if not isinstance(story, dict):
        return ""

    candidates = []

    for key in ("logline", "summary", "synopsis", "description"):
        value = clean(story.get(key), 500)
        if value:
            candidates.append(value)

    if not candidates:
        return ""

    text = candidates[0]
    first_sentence = re.split(r"(?<=[.!?।])\s+", text, maxsplit=1)[0]
    first_sentence = clean(first_sentence, 65).rstrip(" ,;:-।")

    if len(first_sentence) < 8:
        return ""

    return clean(f"कहानी: {first_sentence}", 100)


def get_title(config):
    story = read_json(STORY_FILE)

    # 1. Pipeline-generated title takes priority when topic is absent.
    explicit_topic = read_input_topic(config)
    if not explicit_topic:
        generated_title = clean(
            read_title_file(), 180
        )
        if generated_title:
            return generated_title

        generated_title = title_from_story_json(story)
        if generated_title:
            return generated_title

        summary_title = title_from_summary(story)
        if summary_title:
            return summary_title

    # 2. Explicit topic/title is authoritative when supplied.
    if explicit_topic:
        return explicit_topic

    # 3. Last resort: use an existing title file, never the first story line.
    generated_title = clean(read_title_file(), 180)
    if generated_title:
        return generated_title

    raise RuntimeError(
        "No usable story title found. Generate output/story/final_title.txt "
        "or provide TOPIC/TITLE in Input/topic.txt."
    )


def read_title_file():
    if not TITLE_FILE.is_file():
        return ""

    try:
        for line in TITLE_FILE.read_text(
            encoding="utf-8-sig"
        ).splitlines():
            value = clean(line, 180)
            if value and not value.startswith(("#", "[")):
                return value
    except OSError:
        pass

    return ""


def detect_category(text):
    normalized = str(text or "").casefold()

    for category, words in KEYWORDS.items():
        if any(word.casefold() in normalized for word in words):
            return category

    return "general"


def find_font():
    global _FONT_PATH

    if _FONT_PATH is not None:
        return _FONT_PATH

    fc_match = shutil.which("fc-match")

    if fc_match:
        for family in (
            "Noto Sans Devanagari:style=Bold",
            "Mukta:style=Bold",
            "Hind:style=Bold",
            "DejaVu Sans:style=Bold",
        ):
            try:
                result = subprocess.run(
                    [fc_match, "-f", "%{file}", family],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                candidate = Path(result.stdout.strip())

                if result.returncode == 0 and candidate.is_file():
                    _FONT_PATH = candidate
                    return candidate
            except (OSError, subprocess.TimeoutExpired):
                pass

    for base in FONT_DIRS:
        if not base.is_dir():
            continue

        for name in FONT_NAMES:
            candidate = base / name
            if candidate.is_file():
                _FONT_PATH = candidate
                return candidate

    for base in FONT_DIRS[:2]:
        if not base.is_dir():
            continue

        try:
            for candidate in base.glob("**/*Bold*.ttf"):
                if candidate.is_file():
                    _FONT_PATH = candidate
                    return candidate
        except OSError:
            continue

    _FONT_PATH = None
    return None


def load_font(size):
    path = find_font()

    if path:
        try:
            return ImageFont.truetype(
                str(path),
                size=size,
                layout_engine=ImageFont.Layout.RAQM,
            ), str(path)
        except (OSError, ValueError, AttributeError):
            try:
                return ImageFont.truetype(str(path), size=size), str(path)
            except OSError:
                pass

    try:
        return ImageFont.load_default(size=size), None
    except TypeError:
        return ImageFont.load_default(), None


def image_rows():
    data = read_json(VISUAL_MANIFEST)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        rows = data.get("scenes")
        if isinstance(rows, list):
            return rows

    return []


def collect_images():
    result = []
    seen = set()

    for row in image_rows():
        if not isinstance(row, dict):
            continue

        status = str(row.get("status", "")).casefold()
        if status and status not in {
            "generated", "existing", "reused", "completed", "success", "ready"
        }:
            continue

        value = next(
            (
                row.get(key)
                for key in ("image_path", "image", "image_file", "output_image", "path")
                if isinstance(row.get(key), str) and row.get(key).strip()
            ),
            None,
        )

        path = safe_image(value)
        if path is None:
            continue

        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)

        context = " ".join(
            clean(str(row.get(key, "")), 1000)
            for key in (
                "prompt", "description", "visual_prompt", "scene_id",
                "global_scene", "scene", "part",
            )
        )

        try:
            scene_number = int(row.get("global_scene", row.get("scene", 999999)))
        except (TypeError, ValueError):
            scene_number = 999999

        result.append({
            "path": path,
            "context": context,
            "scene": scene_number,
        })

    return result


def photo_motion_frame(mode):
    data = read_json(PHOTO_MANIFEST)
    rows = data.get("scenes", []) if isinstance(data, dict) else []

    if not isinstance(rows, list):
        return None

    # If the original image is unavailable, extract a frame from a real
    # photo-motion scene instead of making a generic unrelated background.
    for row in rows:
        if not isinstance(row, dict):
            continue

        path_value = next(
            (
                row.get(key)
                for key in ("source_video", "video", "output", "path")
                if isinstance(row.get(key), str) and row.get(key).strip()
            ),
            None,
        )
        video = safe_video(path_value)

        if video:
            frame = extract_frame(video)
            if frame:
                return frame

    return extract_final_video_frame(mode)


def safe_video(value):
    if not isinstance(value, str) or not value.strip():
        return None

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    try:
        path = path.resolve()
        if not path.is_relative_to(ROOT.resolve()):
            return None
        if not path.is_file() or path.stat().st_size < MIN_BYTES:
            return None
        return path
    except OSError:
        return None


def extract_frame(video):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        return None

    try:
        probe = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        duration = float(probe.stdout.strip())
        if probe.returncode or duration <= 0:
            return None

        WORK_DIR.mkdir(parents=True, exist_ok=True)
        target = WORK_DIR / "video_frame.jpg"
        temporary = WORK_DIR / "video_frame.tmp.jpg"

        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{min(duration * 0.3, max(0, duration - 0.1)):.3f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720",
                "-q:v", "2",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

        if result.returncode == 0 and safe_image(str(temporary)):
            temporary.replace(target)
            return target

        temporary.unlink(missing_ok=True)

    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass

    return None


def extract_final_video_frame(mode):
    names = [
        "katha_lok_ai_short.mp4" if mode == "short" else "katha_lok_ai_full.mp4",
        "katha_lok_ai_final.mp4",
    ]

    for name in names:
        video = safe_video(str(OUT / "videos" / name))
        if video:
            frame = extract_frame(video)
            if frame:
                return frame

    return None


def score_background(item, title, category):
    context = item["context"].casefold()
    score = 0

    for word in KEYWORDS.get(category, ()):
        if word.casefold() in context:
            score += 2

    stop_words = {
        "कहानी", "एक", "और", "था", "थी", "the", "a", "an", "story",
        "में", "का", "की", "के", "से", "को", "पर",
    }

    tokens = re.findall(r"[\w\u0900-\u097f]+", title.casefold())

    for token in tokens:
        if len(token) >= 3 and token not in stop_words and token in context:
            score += 5

    scene = item.get("scene", 999999)
    try:
        scene = int(scene)
    except (TypeError, ValueError):
        scene = 999999

    # Slight preference for early scenes only when relevance is similar.
    score += max(0, 3 - min(scene, 3))
    return score


def fit_cover(image):
    image = image.convert("RGB")
    scale = max(WIDTH / image.width, HEIGHT / image.height)

    image = image.resize(
        (
            max(WIDTH, round(image.width * scale)),
            max(HEIGHT, round(image.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )

    left = (image.width - WIDTH) // 2
    top = (image.height - HEIGHT) // 2
    return image.crop((left, top, left + WIDTH, top + HEIGHT))


def compact_title(title):
    title = re.sub(r"\s+", " ", title).strip()

    for separator in (" | ", " — ", " – ", ": "):
        if separator in title:
            candidate = title.split(separator, 1)[0].strip()
            if len(candidate) >= 5:
                title = candidate
                break

    if len(title) > 72:
        title = title[:72].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"

    return title


def wrap_text(draw, text, font, max_width):
    lines = []
    current = ""

    for word in text.split():
        candidate = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), candidate, font=font, stroke_width=2)

        if current and box[2] - box[0] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate

    if current:
        lines.append(current)

    final = []

    for line in lines:
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)

        if box[2] - box[0] <= max_width:
            final.append(line)
            continue

        chunk = ""

        for char in line:
            candidate = chunk + char
            box = draw.textbbox((0, 0), candidate, font=font, stroke_width=2)

            if chunk and box[2] - box[0] > max_width:
                final.append(chunk)
                chunk = char
            else:
                chunk = candidate

        if chunk:
            final.append(chunk)

    return final


def make_background(background_path, category):
    shadow = STYLES[category]["shadow"]

    if background_path:
        with Image.open(background_path) as source:
            image = fit_cover(source)
    else:
        image = Image.new("RGB", (WIDTH, HEIGHT), shadow)

    # Keep the actual scene visible and avoid harsh, overprocessed contrast.
    image = ImageEnhance.Color(image).enhance(0.98)
    image = ImageEnhance.Contrast(image).enhance(1.02)
    image = ImageEnhance.Brightness(image).enhance(1.02)
    return image.filter(
        ImageFilter.UnsharpMask(radius=1.0, percent=45, threshold=5)
    )


def add_gradient(image, category):
    shadow = STYLES[category]["shadow"]
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Smoothly darken the lower region so the title remains readable.
    bands = 36

    for index in range(bands):
        y1 = int(HEIGHT * (0.44 + index * 0.56 / bands))
        y2 = int(HEIGHT * (0.44 + (index + 1) * 0.56 / bands))
        alpha = min(205, 8 + index * 6)

        draw.rectangle(
            (0, y1, WIDTH, y2),
            fill=(*shadow, alpha),
        )

    return Image.alpha_composite(image.convert("RGBA"), overlay)


def create_thumbnail(title, background_path, category):
    style = STYLES[category]
    background = make_background(background_path, category)
    canvas = add_gradient(background, category)
    draw = ImageDraw.Draw(canvas)

    display_title = compact_title(title)
    max_width = int(WIDTH * 0.88)
    chosen = None

    for size in range(88, 35, -2):
        font, font_path = load_font(size)
        lines = wrap_text(draw, display_title, font, max_width)

        if not 1 <= len(lines) <= 3:
            continue

        boxes = [
            draw.textbbox((0, 0), line, font=font, stroke_width=2)
            for line in lines
        ]
        heights = [max(1, box[3] - box[1]) for box in boxes]
        gap = max(5, size // 8)
        total_height = sum(heights) + gap * (len(lines) - 1)

        if total_height <= int(HEIGHT * 0.32):
            chosen = (font, font_path, lines, heights, gap)
            break

    if chosen:
        font, font_path, lines, heights, gap = chosen
    else:
        font, font_path = load_font(38)
        lines = wrap_text(draw, display_title, font, max_width)[:3]
        boxes = [
            draw.textbbox((0, 0), line, font=font, stroke_width=2)
            for line in lines
        ]
        heights = [max(1, box[3] - box[1]) for box in boxes]
        gap = 6

    total_height = sum(heights) + gap * max(0, len(lines) - 1)
    margin_x = 42
    text_y = max(int(HEIGHT * 0.60), HEIGHT - total_height - 58)
    panel_top = max(int(HEIGHT * 0.53), text_y - 22)
    panel_bottom = min(HEIGHT - 16, text_y + total_height + 24)

    draw.rounded_rectangle(
        (margin_x, panel_top, WIDTH - margin_x, panel_bottom),
        radius=22,
        fill=(*style["shadow"], 202),
        outline=(*style["accent"], 240),
        width=4,
    )

    draw.rounded_rectangle(
        (margin_x + 28, panel_top + 12, margin_x + 130, panel_top + 19),
        radius=4,
        fill=(*style["accent"], 255),
    )

    y = text_y

    for line, line_height in zip(lines, heights):
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        line_width = box[2] - box[0]
        x = max(12, (WIDTH - line_width) // 2)

        draw.text(
            (x + 2, y + 3),
            line,
            font=font,
            fill=(0, 0, 0, 220),
            stroke_width=5,
            stroke_fill=(0, 0, 0, 200),
        )
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(*style["text"], 255),
            stroke_width=2,
            stroke_fill=(*style["shadow"], 255),
        )

        y += line_height + gap

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    temporary = THUMBNAIL.with_suffix(".tmp.jpg")

    try:
        canvas.convert("RGB").save(
            temporary,
            format="JPEG",
            quality=94,
            optimize=True,
        )

        with Image.open(temporary) as image:
            image.verify()

        with Image.open(temporary) as image:
            if image.size != (WIDTH, HEIGHT) or image.format != "JPEG":
                raise RuntimeError("Thumbnail must be a 1280x720 JPEG.")

        if temporary.stat().st_size < MIN_BYTES:
            raise RuntimeError("Thumbnail output is unexpectedly small.")

        temporary.replace(THUMBNAIL)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "width": WIDTH,
        "height": HEIGHT,
        "font_size": getattr(font, "size", None),
        "font_file": font_path,
        "title_lines": len(lines),
        "display_title": display_title,
        "category_style": category,
        "design_mood": f"cinematic-{category}",
        "background_selection": (
            "story-context-score" if background_path else "fallback"
        ),
    }


def main():
    config = load_and_validate(INPUT_FILE)
    mode = str(get_format(config)).strip().lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode}")

    title = get_title(config)
    story = read_json(STORY_FILE)
    story_context = json.dumps(
        story if story is not None else {},
        ensure_ascii=False,
    )[:12000]

    category = detect_category(f"{title} {story_context}")
    images = collect_images()

    selected = max(
        images,
        key=lambda item: score_background(item, title, category),
        default=None,
    )

    background = selected["path"] if selected else None

    if background is None:
        background = photo_motion_frame(mode)

    if background is not None:
        background = safe_image(str(background))

    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    background_hash = (
        sha256_file(background) if background else "solid-color-fallback"
    )

    fingerprint = hashlib.sha256(
        (
            f"{VERSION}|{mode}|{WIDTH}x{HEIGHT}|{title_hash}|"
            f"{category}|{background_hash}"
        ).encode("utf-8")
    ).hexdigest()

    old = read_json(MANIFEST)

    if (
        isinstance(old, dict)
        and old.get("status") == "completed"
        and old.get("version") == VERSION
        and old.get("fingerprint") == fingerprint
        and old.get("format") == mode
        and THUMBNAIL.is_file()
        and THUMBNAIL.stat().st_size >= MIN_BYTES
    ):
        try:
            with Image.open(THUMBNAIL) as image:
                image.verify()

            with Image.open(THUMBNAIL) as image:
                if image.size == (WIDTH, HEIGHT) and image.format == "JPEG":
                    log("RESUME: matching valid thumbnail reused")
                    return
        except (OSError, ValueError):
            pass

    details = create_thumbnail(title, background, category)

    write_json(MANIFEST, {
        "status": "completed",
        "version": VERSION,
        "fingerprint": fingerprint,
        "format": mode,
        "title": title,
        "title_sha256": title_hash,
        "category": category,
        "background_image": (
            background.relative_to(ROOT).as_posix() if background else None
        ),
        "background_sha256": background_hash,
        "thumbnail": THUMBNAIL.relative_to(ROOT).as_posix(),
        **details,
        "external_api_calls": 0,
        "paid_services_used": False,
    })

    log("THUMBNAIL CREATED")
    log("FORMAT:", mode)
    log("TITLE:", title)
    log("CATEGORY:", category)
    log("BACKGROUND:", background or "solid-color fallback")
    log("DIMENSIONS:", f"{WIDTH}x{HEIGHT}")
    log("EXTERNAL API CALLS: 0")
    log("OUTPUT:", THUMBNAIL.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", str(exc))
        sys.exit(1)