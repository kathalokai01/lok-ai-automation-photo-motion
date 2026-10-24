
#!/usr/bin/env python3
"""Cinematic, story-matched thumbnail generator; no external API calls."""

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

VERSION = "thumbnail-v15-cinematic-story-matched-composition"
W, H = 1280, 720
MIN_BYTES = 1000

# Separate looks; deliberately not a fixed red-and-gold design.
PALETTES = {
    "horror": ((239, 72, 88), (255, 245, 244), (18, 10, 23)),
    "mystery": ((62, 220, 231), (250, 255, 255), (8, 20, 34)),
    "devotional": ((255, 199, 87), (255, 251, 232), (44, 28, 17)),
    "historical": ((210, 180, 132), (255, 248, 231), (38, 27, 23)),
    "emotional": ((255, 157, 185), (255, 249, 250), (38, 20, 31)),
    "nature": ((151, 228, 156), (250, 255, 244), (15, 35, 27)),
    "general": ((115, 198, 255), (255, 253, 245), (14, 24, 39)),
}

KEYWORDS = {
    "horror": "भूत प्रेत डर खौफ चुड़ैल आत्मा हवेली शैतान horror ghost haunted witch demon".split(),
    "mystery": "रहस्य गुमशुदा गायब पहेली अनजान खोज सस्पेंस mystery secret unknown disappearance".split(),
    "devotional": "भक्ति भगवान देवी मंदिर पूजा कृष्ण राम शिव विष्णु हनुमान दुर्गा गणेश devotional".split(),
    "historical": "राजा रानी सम्राट महाराज युद्ध किला इतिहास योद्धा साम्राज्य historical king queen warrior".split(),
    "emotional": "माँ मां पिता परिवार बेटा बेटी प्यार प्रेम बलिदान दर्द mother father family love".split(),
    "nature": "जंगल वन पहाड़ नदी प्रकृति गाँव गांव किसान forest nature mountain river village".split(),
}

FONT_FAMILIES = (
    "Noto Sans Devanagari:style=Bold",
    "Mukta:style=Bold",
    "Hind:style=Bold",
    "Noto Sans:style=Bold",
    "DejaVu Sans:style=Bold",
)


def log(*args):
    print(*args, flush=True)


def clean(value, limit=2000):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, (dict, list)) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_path(value, image_only=False):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    try:
        path = path.resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            return None
        if path.stat().st_size < MIN_BYTES:
            return None
        if image_only:
            with Image.open(path) as im:
                im.verify()
        return path
    except (OSError, ValueError):
        return None


def all_rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def scene_no(row):
    for key in ("global_scene", "global_scene_number", "scene"):
        try:
            value = int(row.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError, AttributeError):
            pass
    return 999999


def get_title(config, story):
    for key in ("TOPIC", "TITLE"):
        value = clean(str(config.get(key, "")), 180)
        if value:
            return value

    if TITLE_FILE.is_file():
        for line in TITLE_FILE.read_text(encoding="utf-8-sig").splitlines():
            value = clean(line, 180)
            if value and not value.startswith("#"):
                return value

    if isinstance(story, dict):
        for obj in (story, story.get("metadata", {}), story.get("story_metadata", {})):
            if isinstance(obj, dict):
                for key in ("title", "story_title", "generated_title"):
                    value = clean(obj.get(key), 180)
                    if value:
                        return value
        for key in ("logline", "summary", "synopsis", "description"):
            value = clean(story.get(key), 300)
            if value:
                return clean(re.split(r"(?<=[.!?।])\s+", value)[0], 75)

    raise RuntimeError("Story title missing; expected TOPIC/TITLE or final_title.txt")


def detect_category(text):
    low = text.casefold()
    scores = {
        category: sum(1 for word in words if word.casefold() in low)
        for category, words in KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else "general"


def collect_images():
    image_data = read_json(IMAGE_MANIFEST)
    jobs_data = read_json(VISUAL_JOBS)
    prompt_map = {}

    for row in all_rows(jobs_data, "jobs", "scenes", "visual_jobs"):
        if not isinstance(row, dict):
            continue
        number = scene_no(row)
        prompt_map[number] = " ".join(
            clean(str(row.get(key, "")), 2000)
            for key in ("prompt", "visual_prompt", "description", "scene_id")
            if row.get(key)
        )

    found, seen = [], set()
    for row in all_rows(image_data, "scenes", "images"):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "")).casefold()
        if status and status not in {
            "generated", "existing", "reused", "completed", "success", "ready"
        }:
            continue

        value = next(
            (row.get(k) for k in (
                "image_path", "image", "image_file", "output_image", "path"
            ) if isinstance(row.get(k), str) and row.get(k).strip()),
            None,
        )
        path = safe_path(value, image_only=True)
        if path is None or str(path) in seen:
            continue
        seen.add(str(path))

        number = scene_no(row)
        context = " ".join((
            prompt_map.get(number, ""),
            clean(str(row.get("prompt", "")), 2000),
            clean(str(row.get("visual_prompt", "")), 2000),
            clean(str(row.get("description", "")), 1000),
            clean(str(row.get("scene_id", "")), 300),
        ))
        found.append({"path": path, "scene": number, "context": context})

    return found


def choose_image(images, title, category, story):
    if not images:
        return None

    ignored = {
        "कहानी", "एक", "और", "था", "थी", "the", "story", "this",
        "that", "with", "from", "into", "में", "का", "की", "के",
    }
    title_tokens = {
        x for x in re.findall(r"[\w\u0900-\u097f]+", title.casefold())
        if len(x) >= 3 and x not in ignored
    }
    story_tokens = {
        x for x in re.findall(r"[\w\u0900-\u097f]+", story.casefold())
        if len(x) >= 4 and x not in ignored
    }

    def score(item):
        context = item["context"].casefold()
        score_value = sum(
            4 for word in KEYWORDS.get(category, ())
            if word.casefold() in context
        )
        score_value += 8 * sum(1 for word in title_tokens if word in context)
        context_tokens = set(re.findall(r"[\w\u0900-\u097f]+", context))
        score_value += min(18, len(story_tokens & context_tokens))
        # Earlier scenes only break ties; do not outweigh story relevance.
        if item["scene"] <= 3:
            score_value += 1
        return score_value

    return max(images, key=score)


def fit_cover(image):
    image = image.convert("RGB")
    scale = max(W / image.width, H / image.height)
    image = image.resize(
        (max(W, round(image.width * scale)),
         max(H, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (image.width - W) // 2
    top = (image.height - H) // 2
    return image.crop((left, top, left + W, top + H))


def extract_video_frame(mode):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return None

    candidates = [
        OUT / "videos" / (
            "katha_lok_ai_short.mp4" if mode == "short"
            else "katha_lok_ai_full.mp4"
        ),
        OUT / "videos" / "katha_lok_ai_final.mp4",
    ]
    for row in all_rows(read_json(PHOTO_MANIFEST), "scenes"):
        if isinstance(row, dict):
            for key in ("video", "source_video", "output", "path"):
                path = safe_path(row.get(key))
                if path:
                    candidates.append(path)
                    break

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    target = WORK_DIR / "video_frame.jpg"

    for video in candidates:
        video = safe_path(str(video))
        if not video:
            continue
        try:
            probe = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(video),
                ],
                capture_output=True, text=True, timeout=25, check=False,
            )
            duration = float(probe.stdout.strip())
            if probe.returncode or duration <= 0:
                continue
            temp = target.with_suffix(".tmp.jpg")
            result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{min(duration * 0.3, max(0, duration - 0.1)):.3f}",
                    "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                    "-q:v", "2", str(temp),
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if result.returncode == 0 and safe_path(str(temp), image_only=True):
                temp.replace(target)
                return target
            temp.unlink(missing_ok=True)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return None


def find_font(size):
    matcher = shutil.which("fc-match")
    if matcher:
        for family in FONT_FAMILIES:
            try:
                result = subprocess.run(
                    [matcher, "-f", "%{file}", family],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                path = Path(result.stdout.strip())
                if result.returncode == 0 and path.is_file():
                    return ImageFont.truetype(str(path), size), str(path)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return ImageFont.load_default(), None


def wrap_title(draw, title, font, max_width):
    words = title.split()
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    # Split long unbroken words safely rather than clipping the title.
    result = []
    for line in lines:
        part = ""
        for char in line:
            candidate = part + char
            if part and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                result.append(part)
                part = char
            else:
                part = candidate
        if part:
            result.append(part)
    return result


def compact_title(title):
    title = clean(title, 180)
    for separator in (" | ", " — ", " – ", ": "):
        if separator in title:
            first = title.split(separator, 1)[0].strip()
            if len(first) >= 5:
                title = first
                break
    if len(title) > 70:
        title = title[:70].rsplit(" ", 1)[0].rstrip(" ,;:-।") + "…"
    return title


def render_thumbnail(title, background, category):
    accent, text_color, dark = PALETTES[category]

    if background:
        with Image.open(background) as source:
            image = fit_cover(source)
    else:
        image = Image.new("RGB", (W, H), dark)

    # Avoid aggressive contrast/saturation changes that destroy image detail.
    image = ImageEnhance.Color(image).enhance(0.99)
    image = ImageEnhance.Contrast(image).enhance(1.025)
    image = ImageEnhance.Brightness(image).enhance(1.015)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay = ImageDraw.Draw(layer)

    # Cinematic bottom gradient and a subtle side vignette.
    for y in range(H):
        strength = max(0, (y - 265) / (H - 265))
        alpha = int(min(205, strength * 205))
        overlay.line((0, y, W, y), fill=(*dark, alpha))
    for x in range(W):
        edge = max(0, (130 - min(x, W - x)) / 130)
        if edge > 0:
            overlay.line((x, 0, x, H), fill=(*dark, int(edge * 55)))

    image = Image.alpha_composite(image.convert("RGBA"), layer)
    draw = ImageDraw.Draw(image)
    title = compact_title(title)
    chosen = None

    for size in range(88, 35, -2):
        font, font_path = find_font(size)
        lines = wrap_title(draw, title, font, int(W * 0.82))
        if not 1 <= len(lines) <= 3:
            continue
        boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
        heights = [max(1, box[3] - box[1]) for box in boxes]
        gap = max(4, size // 9)
        total = sum(heights) + gap * (len(lines) - 1)
        if total <= 225:
            chosen = font, font_path, lines, heights, gap, size
            break

    if chosen:
        font, font_path, lines, heights, gap, size = chosen
    else:
        font, font_path = find_font(36)
        lines = wrap_title(draw, title, font, int(W * 0.82))[:3]
        heights = [
            max(1, draw.textbbox((0, 0), line, font=font)[3])
            for line in lines
        ]
        gap, size = 5, 36

    total_height = sum(heights) + gap * max(0, len(heights) - 1)
    text_y = max(420, H - total_height - 45)
    panel_top = max(395, text_y - 22)
    panel_bottom = min(H - 16, text_y + total_height + 26)

    draw.rounded_rectangle(
        (34, panel_top, W - 34, panel_bottom),
        radius=24, fill=(*dark, 218), outline=(*accent, 245), width=4,
    )
    draw.rounded_rectangle(
        (58, panel_top + 13, 180, panel_top + 21),
        radius=4, fill=(*accent, 255),
    )

    y = text_y
    for line, height in zip(lines, heights):
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        x = max(12, (W - (box[2] - box[0])) // 2)
        draw.text(
            (x + 2, y + 3), line, font=font,
            fill=(0, 0, 0, 220), stroke_width=5,
            stroke_fill=(0, 0, 0, 220),
        )
        draw.text(
            (x, y), line, font=font, fill=(*text_color, 255),
            stroke_width=2, stroke_fill=(*dark, 255),
        )
        y += height + gap

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    temp = THUMBNAIL.with_suffix(".tmp.jpg")
    try:
        image.convert("RGB").filter(
            ImageFilter.UnsharpMask(radius=0.8, percent=35, threshold=5)
        ).save(temp, "JPEG", quality=94, optimize=True)
        with Image.open(temp) as check:
            check.verify()
        with Image.open(temp) as check:
            if check.size != (W, H) or check.format != "JPEG":
                raise RuntimeError("Thumbnail must be 1280x720 JPEG")
        if temp.stat().st_size < MIN_BYTES:
            raise RuntimeError("Thumbnail output is too small")
        temp.replace(THUMBNAIL)
    finally:
        temp.unlink(missing_ok=True)

    return {
        "width": W,
        "height": H,
        "font_size": size,
        "font_file": font_path,
        "title_lines": len(lines),
        "display_title": title,
        "category_style": category,
        "design_mood": f"cinematic-{category}-scene-matched",
        "background_selection": "prompt-matched-scene" if background else "solid-fallback",
    }


def main():
    config = load_and_validate(INPUT_FILE)
    mode = str(get_format(config)).strip().lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    story = read_json(STORY_FILE)
    title = get_title(config, story)
    story_text = json.dumps(story or {}, ensure_ascii=False)[:25000]
    category = detect_category(title + " " + story_text)

    images = collect_images()
    selected = choose_image(images, title, category, story_text)
    background = selected["path"] if selected else extract_video_frame(mode)
    if background:
        background = safe_path(str(background), image_only=True)

    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    background_hash = sha256(background) if background else "solid-color-fallback"
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
                if check.size == (W, H) and check.format == "JPEG":
                    log("RESUME: valid matching thumbnail reused")
                    return
        except (OSError, ValueError):
            pass

    details = render_thumbnail(title, background, category)
    write_json(MANIFEST, {
        "status": "completed",
        "version": VERSION,
        "fingerprint": fingerprint,
        "format": mode,
        "title": title,
        "title_sha256": title_hash,
        "category": category,
        "background_image": background.relative_to(ROOT).as_posix() if background else None,
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
    log("DIMENSIONS:", f"{W}x{H}")
    log("EXTERNAL API CALLS: 0")
    log("OUTPUT:", THUMBNAIL.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", str(exc))
        sys.exit(1)
