#!/usr/bin/env python3
"""Story-aware thumbnail generation with visual-prompt matching and safe resume."""

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
IMAGE_MANIFEST = OUT / "visuals" / "image_manifest.json"
VISUAL_JOBS = OUT / "visuals" / "visual_jobs.json"
PHOTO_MANIFEST = OUT / "photo_motion" / "photo_motion_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
WORK_DIR = OUT / "thumbnail"
MANIFEST = WORK_DIR / "thumbnail_manifest.json"

VERSION = "thumbnail-v14-prompt-matched-story-design"
WIDTH, HEIGHT = 1280, 720
MIN_BYTES = 1000

STYLES = {
    "horror": ((238, 77, 91), (255, 246, 242), (25, 13, 25)),
    "mystery": ((72, 216, 226), (247, 253, 255), (8, 24, 39)),
    "devotional": ((255, 190, 75), (255, 250, 230), (57, 32, 17)),
    "historical": ((218, 174, 112), (255, 246, 225), (39, 28, 22)),
    "emotional": ((244, 148, 173), (255, 249, 248), (43, 25, 35)),
    "nature": ((139, 216, 143), (249, 255, 243), (17, 39, 28)),
    "general": ((112, 194, 255), (255, 252, 244), (17, 24, 36)),
}

KEYWORDS = {
    "horror": (
        "भूत", "प्रेत", "डर", "खौफ", "चुड़ैल", "आत्मा", "हवेली",
        "शैतान", "horror", "ghost", "haunted", "witch", "demon",
    ),
    "mystery": (
        "रहस्य", "गुमशुदा", "गायब", "पहेली", "अनजान", "खोज",
        "mystery", "secret", "unknown", "disappearance", "सस्पेंस",
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


def log(*values):
    print(*values, flush=True)


def clean(value, limit=1000):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


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


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_media(value, image_only=False):
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

        if image_only:
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                return None
            with Image.open(path) as image:
                image.verify()

        return path
    except (OSError, ValueError):
        return None


def read_input_title(config):
    for key in ("TOPIC", "TITLE"):
        value = clean(str(config.get(key, "")), 180)
        if value:
            return value
    return ""


def read_title_file():
    if not TITLE_FILE.is_file():
        return ""

    try:
        for line in TITLE_FILE.read_text(encoding="utf-8-sig").splitlines():
            value = clean(line, 180)
            if value and not value.startswith(("#", "[")):
                return value
    except OSError:
        pass

    return ""


def get_title(config, story):
    explicit = read_input_title(config)
    if explicit:
        return explicit

    generated = read_title_file()
    if generated:
        return generated

    if isinstance(story, dict):
        for container in (
            story,
            story.get("metadata", {}),
            story.get("story_metadata", {}),
        ):
            if not isinstance(container, dict):
                continue
            for key in ("title", "story_title", "generated_title"):
                value = clean(container.get(key), 180)
                if value:
                    return value

        for key in ("logline", "summary", "synopsis", "description"):
            value = clean(story.get(key), 500)
            if value:
                sentence = re.split(r"(?<=[.!?।])\s+", value, maxsplit=1)[0]
                sentence = clean(sentence, 65).rstrip(" ,;:-।")
                if len(sentence) >= 8:
                    return clean(f"कहानी: {sentence}", 100)

    raise RuntimeError(
        "Story title unavailable: provide TOPIC/TITLE or generate "
        "output/story/final_title.txt."
    )


def detect_category(text):
    normalized = str(text or "").casefold()
    for category, keywords in KEYWORDS.items():
        if any(word.casefold() in normalized for word in keywords):
            return category
    return "general"


def get_rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def scene_number(row):
    for key in ("global_scene", "global_scene_number", "scene"):
        try:
            number = int(row.get(key))
            if number > 0:
                return number
        except (TypeError, ValueError):
            pass
    return None


def collect_images():
    """Join image paths to their actual visual prompts by scene number."""
    image_data = read_json(IMAGE_MANIFEST)
    jobs_data = read_json(VISUAL_JOBS)

    image_rows = get_rows(image_data, "scenes", "images")
    job_rows = get_rows(jobs_data, "jobs", "scenes", "visual_jobs")

    prompts = {}
    for row in job_rows:
        if not isinstance(row, dict):
            continue
        number = scene_number(row)
        if number is None:
            continue

        prompt = " ".join(
            clean(str(row.get(key, "")), 3000)
            for key in ("prompt", "description", "visual_prompt", "negative_prompt")
            if row.get(key)
        )
        if prompt:
            prompts[number] = prompt

    result = []
    seen = set()

    for row in image_rows:
        if not isinstance(row, dict):
            continue

        status = str(row.get("status", "")).casefold()
        if status and status not in {
            "generated", "existing", "reused", "completed", "success", "ready"
        }:
            continue

        value = next(
            (
                row.get(key) for key in (
                    "image_path", "image", "image_file", "output_image", "path"
                )
                if isinstance(row.get(key), str) and row.get(key).strip()
            ),
            None,
        )
        path = safe_media(value, image_only=True)
        if path is None:
            continue

        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)

        number = scene_number(row)
        manifest_context = " ".join(
            clean(str(row.get(key, "")), 1000)
            for key in ("prompt", "description", "visual_prompt", "scene_id")
            if row.get(key)
        )
        context = " ".join(
            part for part in (
                prompts.get(number, ""),
                manifest_context,
                str(row.get("scene_id", "")),
            ) if part
        )

        result.append({
            "path": path,
            "scene": number or 999999,
            "context": context,
        })

    return result


def score_image(item, title, category, story_context):
    context = item["context"].casefold()
    score = 0

    for word in KEYWORDS.get(category, ()):
        if word.casefold() in context:
            score += 3

    stop_words = {
        "कहानी", "एक", "और", "था", "थी", "the", "a", "an", "story",
        "में", "का", "की", "के", "से", "को", "पर", "this", "that",
    }

    for token in re.findall(r"[\w\u0900-\u097f]+", title.casefold()):
        if len(token) >= 3 and token not in stop_words and token in context:
            score += 7

    # Reward scene prompts that share meaningful story vocabulary.
    story_tokens = set(
        token for token in re.findall(r"[\w\u0900-\u097f]+", story_context.casefold())
        if len(token) >= 4 and token not in stop_words
    )
    context_tokens = set(
        re.findall(r"[\w\u0900-\u097f]+", context)
    )
    score += min(12, len(story_tokens & context_tokens))

    try:
        scene = int(item["scene"])
    except (TypeError, ValueError):
        scene = 999999

    # Prefer early scenes only as a tie-breaker.
    score += max(0, 3 - min(scene, 3))
    return score


def find_font(size):
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
                    capture_output=True, text=True, timeout=8, check=False,
                )
                candidate = Path(result.stdout.strip())
                if result.returncode == 0 and candidate.is_file():
                    return ImageFont.truetype(str(candidate), size=size), str(candidate)
            except (OSError, subprocess.TimeoutExpired):
                pass

    for base in FONT_DIRS:
        if not base.is_dir():
            continue
        for name in FONT_NAMES:
            candidate = base / name
            if candidate.is_file():
                try:
                    return ImageFont.truetype(str(candidate), size=size), str(candidate)
                except OSError:
                    pass

    return ImageFont.load_default(), None


def fit_cover(source):
    image = source.convert("RGB")
    scale = max(WIDTH / image.width, HEIGHT / image.height)
    image = image.resize(
        (max(WIDTH, round(image.width * scale)),
         max(HEIGHT, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (image.width - WIDTH) // 2
    top = (image.height - HEIGHT) // 2
    return image.crop((left, top, left + WIDTH, top + HEIGHT))


def extract_video_frame(mode):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return None

    names = [
        "katha_lok_ai_short.mp4" if mode == "short" else "katha_lok_ai_full.mp4",
        "katha_lok_ai_final.mp4",
    ]
    candidates = []

    photo_data = read_json(PHOTO_MANIFEST)
    for row in get_rows(photo_data, "scenes"):
        if isinstance(row, dict):
            for key in ("video", "source_video", "output", "path"):
                candidate = safe_media(row.get(key))
                if candidate:
                    candidates.append(candidate)
                    break

    candidates.extend(
        candidate for name in names
        if (candidate := safe_media(str(OUT / "videos" / name)))
    )

    for video in candidates:
        try:
            probe = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(video),
                ],
                capture_output=True, text=True, timeout=20, check=False,
            )
            duration = float(probe.stdout.strip())
            if probe.returncode or duration <= 0:
                continue

            WORK_DIR.mkdir(parents=True, exist_ok=True)
            target = WORK_DIR / "video_frame.jpg"
            temporary = WORK_DIR / "video_frame.tmp.jpg"
            result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{min(duration * 0.3, max(0, duration - 0.1)):.3f}",
                    "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                    "-q:v", "2", str(temporary),
                ],
                capture_output=True, text=True, timeout=45, check=False,
            )
            if result.returncode == 0 and safe_media(str(temporary), image_only=True):
                temporary.replace(target)
                return target
            temporary.unlink(missing_ok=True)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue

    return None


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

    output = []
    for line in lines:
        chunk = ""
        for char in line:
            candidate = chunk + char
            box = draw.textbbox((0, 0), candidate, font=font, stroke_width=2)
            if chunk and box[2] - box[0] > max_width:
                output.append(chunk)
                chunk = char
            else:
                chunk = candidate
        if chunk:
            output.append(chunk)

    return output


def compact_title(title):
    title = clean(title, 180)
    for separator in (" | ", " — ", " – ", ": "):
        if separator in title:
            candidate = title.split(separator, 1)[0].strip()
            if len(candidate) >= 5:
                title = candidate
                break
    if len(title) > 72:
        title = title[:72].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return title


def create_thumbnail(title, background_path, category):
    accent, text_color, shadow = STYLES[category]

    if background_path:
        with Image.open(background_path) as source:
            image = fit_cover(source)
    else:
        image = Image.new("RGB", (WIDTH, HEIGHT), shadow)

    # Preserve the scene; use only mild corrections.
    image = ImageEnhance.Color(image).enhance(0.98)
    image = ImageEnhance.Contrast(image).enhance(1.02)
    image = ImageEnhance.Brightness(image).enhance(1.02)

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for index in range(36):
        y1 = int(HEIGHT * (0.43 + index * 0.57 / 36))
        y2 = int(HEIGHT * (0.43 + (index + 1) * 0.57 / 36))
        alpha = min(200, 8 + index * 5)
        draw.rectangle((0, y1, WIDTH, y2), fill=(*shadow, alpha))

    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(image)
    display_title = compact_title(title)
    chosen = None

    for size in range(84, 33, -2):
        font, font_path = find_font(size)
        lines = wrap_text(draw, display_title, font, int(WIDTH * 0.86))
        if not 1 <= len(lines) <= 3:
            continue

        boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
        heights = [max(1, box[3] - box[1]) for box in boxes]
        gap = max(5, size // 8)
        total = sum(heights) + gap * (len(lines) - 1)

        if total <= int(HEIGHT * 0.31):
            chosen = (font, font_path, lines, heights, gap, size)
            break

    if chosen:
        font, font_path, lines, heights, gap, size = chosen
    else:
        font, font_path = find_font(36)
        lines = wrap_text(draw, display_title, font, int(WIDTH * 0.86))[:3]
        boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
        heights = [max(1, box[3] - box[1]) for box in boxes]
        gap, size = 6, 36

    total_height = sum(heights) + gap * max(0, len(lines) - 1)
    text_y = max(int(HEIGHT * 0.60), HEIGHT - total_height - 58)
    panel_top = max(int(HEIGHT * 0.52), text_y - 22)
    panel_bottom = min(HEIGHT - 14, text_y + total_height + 24)

    draw.rounded_rectangle(
        (38, panel_top, WIDTH - 38, panel_bottom),
        radius=22, fill=(*shadow, 207), outline=(*accent, 245), width=4,
    )
    draw.rounded_rectangle(
        (64, panel_top + 12, 170, panel_top + 19),
        radius=4, fill=(*accent, 255),
    )

    y = text_y
    for line, line_height in zip(lines, heights):
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        x = max(12, (WIDTH - (box[2] - box[0])) // 2)
        draw.text(
            (x + 2, y + 3), line, font=font,
            fill=(0, 0, 0, 220), stroke_width=5, stroke_fill=(0, 0, 0, 200),
        )
        draw.text(
            (x, y), line, font=font,
            fill=(*text_color, 255), stroke_width=2, stroke_fill=(*shadow, 255),
        )
        y += line_height + gap

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    temporary = THUMBNAIL.with_suffix(".tmp.jpg")

    try:
        image.convert("RGB").filter(
            ImageFilter.UnsharpMask(radius=0.8, percent=35, threshold=5)
        ).save(temporary, format="JPEG", quality=94, optimize=True)

        with Image.open(temporary) as check:
            check.verify()
        with Image.open(temporary) as check:
            if check.size != (WIDTH, HEIGHT) or check.format != "JPEG":
                raise RuntimeError("Thumbnail must be a 1280x720 JPEG.")

        if temporary.stat().st_size < MIN_BYTES:
            raise RuntimeError("Thumbnail output is unexpectedly small.")

        temporary.replace(THUMBNAIL)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "width": WIDTH,
        "height": HEIGHT,
        "font_size": size,
        "font_file": font_path,
        "title_lines": len(lines),
        "display_title": display_title,
        "category_style": category,
        "design_mood": f"cinematic-{category}",
        "background_selection": "prompt-matched-scene" if background_path else "fallback",
    }


def main():
    config = load_and_validate(INPUT_FILE)
    mode = str(get_format(config)).strip().lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    story = read_json(STORY_FILE)
    title = get_title(config, story)
    story_context = json.dumps(story or {}, ensure_ascii=False)[:20000]
    category = detect_category(f"{title} {story_context}")

    images = collect_images()
    selected = max(
        images,
        key=lambda item: score_image(item, title, category, story_context),
        default=None,
    )

    background = selected["path"] if selected else None
    if background is None:
        background = extract_video_frame(mode)
    if background is not None:
        background = safe_media(str(background), image_only=True)

    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    background_hash = sha256_file(background) if background else "solid-color-fallback"
    fingerprint = hashlib.sha256(
        f"{VERSION}|{mode}|{title_hash}|{category}|{background_hash}".encode("utf-8")
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
            with Image.open(THUMBNAIL) as check:
                check.verify()
            with Image.open(THUMBNAIL) as check:
                if check.size == (WIDTH, HEIGHT) and check.format == "JPEG":
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