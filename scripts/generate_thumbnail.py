#!/usr/bin/env python3
"""Generate one verified, format-correct, story-driven cinematic thumbnail."""

from __future__ import annotations

import hashlib
import json
import os
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

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_parts_range,
    get_topic,
)

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

OUTPUT_FILE = ROOT / "output" / "thumbnail.jpg"
OUTPUT_DIR = ROOT / "output" / "thumbnail"
MANIFEST_FILE = OUTPUT_DIR / "thumbnail_manifest.json"

VERSION = "thumbnail-v50-format-correct-story-grounded"
MIN_IMAGE_BYTES = 1000
SHORT_SIZE = (720, 1280)
FULL_SIZE = (1280, 720)
MAX_TITLE_CHARS = 100

TITLE_KEYS = (
    "final_title",
    "generated_title",
    "story_title",
    "title",
)

SUMMARY_KEYS = (
    "story_summary",
    "summary",
    "synopsis",
    "logline",
    "short_summary",
)

STORY_KEYS = (
    "story_text",
    "full_story",
    "complete_story",
    "original_story",
    "content",
    "narration",
    "script",
    "source_text",
)

IMAGE_KEYS = (
    "image_path",
    "output_path",
    "visual_path",
    "asset_path",
    "image",
    "file",
    "path",
    "output",
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


def clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().strip("\"'“”‘’ ")


def multiline(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def read_json(path: Path, required: bool = False) -> Any:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc
        log("Ignoring invalid optional JSON:", path, exc)
        return None


def nested_objects(data: dict) -> list[dict]:
    result = [data]
    seen = {id(data)}

    for key in ("story", "metadata", "story_metadata", "result", "data"):
        value = data.get(key)
        if isinstance(value, dict) and id(value) not in seen:
            result.append(value)
            seen.add(id(value))

    return result


def first_text(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = clean(data.get(key))
        if value:
            return value
    return ""


def load_story_document() -> tuple[dict, Path]:
    for path in STORY_FILES:
        data = read_json(path)

        if isinstance(data, dict):
            return data, path

    raise RuntimeError(
        "Neither ai_story.json nor story.json exists. "
        "Thumbnail generation requires a completed story."
    )


def extract_story_text(data: dict) -> str:
    for obj in nested_objects(data):
        for key in STORY_KEYS:
            value = multiline(obj.get(key))
            if len(value) >= 100:
                return value

    parts = data.get("parts")

    if not isinstance(parts, list):
        nested = data.get("story")
        parts = nested.get("parts") if isinstance(nested, dict) else None

    if isinstance(parts, list):
        sections = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            value = multiline(
                part.get("narration")
                or part.get("text")
                or part.get("story")
                or part.get("content")
            )

            if value:
                sections.append(value)
                continue

            scenes = part.get("scenes")
            if isinstance(scenes, list):
                for scene in scenes:
                    if not isinstance(scene, dict):
                        continue
                    narration = multiline(
                        scene.get("narration")
                        or scene.get("narration_text")
                        or scene.get("text")
                    )
                    if narration:
                        sections.append(narration)

        reconstructed = "\n\n".join(sections)
        if len(reconstructed) >= 100:
            return reconstructed

    scenes = data.get("scenes")

    if isinstance(scenes, list):
        sections = []

        for scene in scenes:
            if not isinstance(scene, dict):
                continue

            narration = multiline(
                scene.get("narration")
                or scene.get("narration_text")
                or scene.get("text")
            )

            if narration:
                sections.append(narration)

        reconstructed = "\n\n".join(sections)
        if len(reconstructed) >= 100:
            return reconstructed

    raise RuntimeError(
        "Complete story narration is missing or too short. "
        "A first-line-only title is not permitted."
    )


def extract_story_summary(data: dict) -> str:
    for obj in nested_objects(data):
        summary = first_text(obj, SUMMARY_KEYS)
        if summary:
            return summary

    return ""


def derive_title_from_complete_story(
    story_text: str,
    summary: str,
) -> str:
    """Create a compact title from the full story, not its opening line."""

    if summary:
        sentences = [
            clean(item)
            for item in re.split(r"(?<=[.!?।])\s+", summary)
            if clean(item)
        ]

        if sentences:
            candidate = " ".join(sentences[:2])
            words = candidate.split()

            if len(words) > 11:
                candidate = " ".join(words[:11])

            if len(candidate) >= 8:
                return candidate.rstrip(" ,:;।.!?")

    sentences = [
        clean(item)
        for item in re.split(r"(?<=[.!?।])\s+", story_text)
        if len(clean(item)) >= 20
    ]

    if not sentences:
        raise RuntimeError("Cannot derive a title from the complete story.")

    # Use the middle conflict and ending context together.
    selected = []

    if len(sentences) >= 3:
        selected.extend(sentences[1:3])
        selected.extend(sentences[-2:])
    else:
        selected.extend(sentences)

    candidate = " ".join(selected)
    candidate = re.sub(r"\([^)]*\)", "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()

    words = candidate.split()

    if len(words) > 12:
        candidate = " ".join(words[:12])

    candidate = candidate.strip(" ,:;।.!?\"'")

    if len(candidate) < 8:
        raise RuntimeError("Derived story title is not meaningful.")

    return candidate


def load_title(
    data: dict,
    config: dict,
    story_text: str,
    summary: str,
) -> tuple[str, str]:
    for obj in nested_objects(data):
        title = first_text(obj, TITLE_KEYS)

        if title:
            return title, "completed_story_metadata"

    title_file = ROOT / "output" / "story" / "final_title.txt"

    if title_file.is_file():
        title = clean(
            title_file.read_text(encoding="utf-8-sig")
        )
        if title:
            return title, "final_title.txt"

    explicit_title = clean(str(config.get("TITLE", "")))

    if explicit_title:
        return explicit_title, "Input/topic.txt:TITLE"

    derived = derive_title_from_complete_story(
        story_text,
        summary,
    )

    return derived, "derived_from_complete_story_summary"


def validate_story_status(data: dict) -> None:
    statuses = [
        clean(obj.get("status")).lower()
        for obj in nested_objects(data)
        if obj.get("status")
    ]

    if statuses and not any(
        status in {"completed", "complete", "success", "done"}
        for status in statuses
    ):
        raise RuntimeError(
            f"Story generation is not complete: {statuses}"
        )

    for obj in nested_objects(data):
        if obj.get("error"):
            raise RuntimeError("Story file contains an error field.")


def safe_local_path(value: Any) -> Path | None:
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


def extract_image_path(row: dict) -> Path | None:
    for key in IMAGE_KEYS:
        path = safe_local_path(row.get(key))
        if path:
            return path

    for key in ("image", "visual", "asset", "result"):
        nested = row.get(key)

        if isinstance(nested, dict):
            for image_key in IMAGE_KEYS:
                path = safe_local_path(nested.get(image_key))
                if path:
                    return path

    nested_scene = row.get("source_scene")

    if isinstance(nested_scene, dict):
        for key in IMAGE_KEYS:
            path = safe_local_path(nested_scene.get(key))
            if path:
                return path

    return None


def rows_from_manifest(path: Path) -> list[dict]:
    data = read_json(path)

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("scenes")
            or data.get("jobs")
            or data.get("visuals")
            or data.get("items")
            or data.get("images")
            or []
        )

        if isinstance(rows, dict):
            rows = list(rows.values())
    else:
        return []

    if not isinstance(rows, list):
        return []

    return [
        {**row, "_manifest": path.relative_to(ROOT).as_posix()}
        for row in rows
        if isinstance(row, dict)
    ]


def row_part(row: dict) -> int:
    for key in ("part", "part_number", "part_index"):
        try:
            value = int(row.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue

    scene_id = clean(row.get("scene_id") or row.get("id"))
    match = re.search(r"part[_-]?(\d+)", scene_id, re.I)

    if match:
        return int(match.group(1))

    return 1


def row_scene(row: dict, fallback: int) -> int:
    for key in (
        "global_scene",
        "global_scene_number",
        "scene_index",
        "scene",
        "local_scene",
    ):
        try:
            value = int(row.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue

    return fallback


def role_name(row: dict) -> str:
    value = clean(
        row.get("role")
        or row.get("scene_role")
        or row.get("kind")
    ).lower()

    aliases = {
        "main_scene": "main",
        "main-scene": "main",
        "opening_hook": "hook",
        "opening-hook": "hook",
        "final_suspense": "suspense",
        "ending_suspense": "suspense",
        "final_resolution": "resolution",
    }

    return aliases.get(value, value)


def load_visual_candidates(
    minimum_part: int,
    maximum_part: int,
) -> list[dict]:
    merged: dict[tuple[int, str], dict] = {}

    for path in reversed(VISUAL_MANIFESTS):
        for index, row in enumerate(rows_from_manifest(path), 1):
            part = row_part(row)

            if not minimum_part <= part <= maximum_part:
                continue

            scene_id = clean(
                row.get("scene_id")
                or row.get("id")
                or f"part_{part:02d}_scene_{row_scene(row, index):02d}"
            )

            key = (part, scene_id)
            merged[key] = {**merged.get(key, {}), **row}

    accepted_statuses = {
        "", "completed", "complete", "success", "done", "generated", "reused"
    }

    candidates = []

    for (part, scene_id), row in merged.items():
        status = clean(row.get("status")).lower()

        if status not in accepted_statuses:
            continue

        path = extract_image_path(row)

        if path is None:
            continue

        declared_hash = clean(
            row.get("sha256")
            or row.get("image_sha256")
            or row.get("output_sha256")
        )

        actual_hash = sha256_file(path)

        if declared_hash and declared_hash.lower() != actual_hash:
            log("Skipping image with SHA256 mismatch:", path)
            continue

        candidates.append({
            "path": path,
            "part": part,
            "scene_id": scene_id,
            "scene": row_scene(row, len(candidates) + 1),
            "role": role_name(row),
            "row": row,
            "manifest": row.get("_manifest", ""),
            "sha256": actual_hash,
        })

    if not candidates:
        raise RuntimeError(
            "No valid generated scene image found for the selected parts."
        )

    return candidates


def description_text(row: dict) -> str:
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
        "character_summary",
    )

    values = [
        clean(row.get(key))
        for key in fields
    ]

    nested = row.get("source_scene")

    if isinstance(nested, dict):
        values.extend(
            clean(nested.get(key))
            for key in fields
        )

    return " ".join(value for value in values if value).lower()


def score_candidate(candidate: dict) -> int:
    row = candidate["row"]
    description = description_text(row)
    role = candidate["role"]

    score = 0

    role_scores = {
        "hook": 13,
        "suspense": 18,
        "resolution": 10,
        "main": 4,
    }

    score += role_scores.get(role, 0)

    groups = (
        ("mystery", "secret", "unknown", "रहस्य", "राज"),
        ("climax", "finale", "reveal", "confrontation", "टकराव"),
        ("fear", "danger", "threat", "haunted", "भय", "खतरा"),
        ("dramatic", "intense", "emotional", "shock", "नाटकीय"),
        ("close-up", "close up", "character focus", "face", "चेहरा"),
        ("fire", "storm", "chase", "battle", "conflict", "आग", "युद्ध"),
        ("crying", "fearful", "angry", "surprised", "shocked"),
        ("hero", "villain", "protagonist", "antagonist", "नायक", "खलनायक"),
    )

    for group in groups:
        if any(term in description for term in group):
            score += 3

    negative_terms = (
        "empty room",
        "plain background",
        "title card",
        "text overlay",
        "empty landscape",
        "blank background",
    )

    score -= 4 * sum(
        term in description
        for term in negative_terms
    )

    if row.get("opening_hook_required"):
        score += 4
    if row.get("suspense_required"):
        score += 7
    if row.get("climax_required"):
        score += 5

    return score


def choose_hero(
    minimum_part: int,
    maximum_part: int,
) -> dict:
    candidates = load_visual_candidates(
        minimum_part,
        maximum_part,
    )

    for candidate in candidates:
        candidate["score"] = score_candidate(candidate)

    # Prefer story-relevant dramatic images, not simply the last image.
    candidates.sort(
        key=lambda item: (
            item["score"],
            item["role"] in {"hook", "suspense"},
            -item["part"],
            -item["scene"],
            item["path"].stat().st_size,
        ),
        reverse=True,
    )

    selected = candidates[0]

    log("Selected hero part:", selected["part"])
    log("Selected hero scene:", selected["scene_id"])
    log("Selected hero role:", selected["role"])
    log("Selected hero score:", selected["score"])

    return selected


def find_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)

        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue

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
                return ImageFont.truetype(
                    font_path,
                    size=size,
                )
        except Exception:
            continue

    raise RuntimeError(
        "No usable Hindi font found. "
        "Install Noto Sans Devanagari, Mukta or Hind."
    )


def shorten_title(title: str) -> str:
    title = clean(title)

    if len(title) <= MAX_TITLE_CHARS:
        return title

    shortened = title[:MAX_TITLE_CHARS]

    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]

    return shortened.rstrip(" ,.!?;:।") + "…"


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
        max(0.12, min(0.88, weighted_x / total / width)),
        max(0.12, min(0.88, weighted_y / total / height)),
    )


def crop_to_ratio(
    image: Image.Image,
    ratio: float,
    focal: tuple[float, float],
) -> Image.Image:
    width, height = image.size
    fx, fy = focal

    if width / height > ratio:
        new_width = max(1, int(height * ratio))
        center = int(fx * width)
        left = max(0, min(width - new_width, center - new_width // 2))

        return image.crop((
            left,
            0,
            left + new_width,
            height,
        ))

    new_height = max(1, int(width / ratio))
    center = int(fy * height)
    top = max(0, min(height - new_height, center - new_height // 2))

    return image.crop((
        0,
        top,
        width,
        top + new_height,
    ))


def scene_palette(image: Image.Image) -> dict:
    sample = image.convert("RGB").resize((48, 48))
    pixels = list(sample.getdata())
    count = max(1, len(pixels))

    red = sum(pixel[0] for pixel in pixels) / count
    green = sum(pixel[1] for pixel in pixels) / count
    blue = sum(pixel[2] for pixel in pixels) / count
    brightness = (red + green + blue) / 3

    if blue > red * 1.12 and blue > green * 1.05:
        accent = (92, 190, 255)
    elif green > red * 1.12:
        accent = (105, 225, 155)
    elif red > blue * 1.20:
        accent = (255, 125, 88)
    elif brightness < 78:
        accent = (255, 190, 75)
    elif brightness > 185:
        accent = (145, 115, 255)
    else:
        accent = (255, 215, 100)

    return {
        "accent": accent,
        "text": (255, 253, 246),
        "outline": (8, 9, 13),
        "brightness": round(brightness, 2),
        "average_rgb": [
            round(red),
            round(green),
            round(blue),
        ],
    }


def cinematic_grade(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Color(image).enhance(1.15)
    image = ImageEnhance.Contrast(image).enhance(1.13)
    image = ImageEnhance.Sharpness(image).enhance(1.20)

    return image.filter(
        ImageFilter.UnsharpMask(
            radius=1.5,
            percent=120,
            threshold=3,
        )
    )


def gradient_overlay(
    size: tuple[int, int],
    placement: str,
) -> Image.Image:
    width, height = size
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if placement == "bottom":
        start = int(height * 0.43)

        for y in range(start, height):
            progress = (y - start) / max(1, height - start)
            alpha = int(15 + 220 * progress)
            draw.line(
                (0, y, width, y),
                fill=(0, 0, 0, alpha),
            )

    elif placement == "top":
        end = int(height * 0.52)

        for y in range(end):
            progress = 1 - y / max(1, end)
            alpha = int(15 + 215 * progress)
            draw.line(
                (0, y, width, y),
                fill=(0, 0, 0, alpha),
            )

    elif placement == "left":
        band = int(width * 0.64)

        for x in range(band):
            progress = 1 - x / max(1, band)
            alpha = int(15 + 215 * progress)
            draw.line(
                (x, 0, x, height),
                fill=(0, 0, 0, alpha),
            )

    else:
        start = int(width * 0.36)

        for x in range(start, width):
            progress = (x - start) / max(1, width - start)
            alpha = int(15 + 215 * progress)
            draw.line(
                (x, 0, x, height),
                fill=(0, 0, 0, alpha),
            )

    return overlay


def choose_placement(
    focal: tuple[float, float],
    vertical: bool,
) -> str:
    fx, fy = focal

    if vertical:
        return "top" if fy > 0.60 else "bottom"

    if fx > 0.64:
        return "left"

    if fx < 0.36:
        return "right"

    return "bottom"


def draw_title(
    image: Image.Image,
    title: str,
    vertical: bool,
    focal: tuple[float, float],
) -> tuple[Image.Image, dict]:
    image = image.convert("RGB")
    width, height = image.size
    palette = scene_palette(image)

    placement = choose_placement(focal, vertical)

    image = Image.alpha_composite(
        image.convert("RGBA"),
        gradient_overlay(image.size, placement),
    ).convert("RGB")

    draw = ImageDraw.Draw(image)
    title = shorten_title(title)

    max_width = int(
        width * (
            0.82
            if placement in {"left", "right"}
            else 0.90
        )
    )

    base_size = max(
        28,
        width // (13 if vertical else 15),
    )
    min_size = max(20, width // 42)

    selected_font = None
    selected_lines = []
    selected_size = min_size

    for size in range(base_size, min_size - 1, -1):
        font = find_font(size)
        lines = wrap_text(
            draw,
            title,
            font,
            max_width,
        )

        if 1 <= len(lines) <= 3:
            selected_font = font
            selected_lines = lines
            selected_size = size
            break

    if selected_font is None:
        selected_font = find_font(min_size)
        selected_lines = wrap_text(
            draw,
            title,
            selected_font,
            max_width,
        )[:3]

    if not selected_lines:
        raise RuntimeError("Thumbnail title layout failed.")

    spacing = max(6, selected_size // 5)
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
        + spacing * (len(line_heights) - 1)
    )

    if placement == "top":
        start_y = int(height * 0.07)
    elif placement == "bottom":
        start_y = int(height * 0.68)
    else:
        start_y = int(height * 0.34)

    start_y = max(
        18,
        min(start_y, height - total_height - 25),
    )

    if placement == "left":
        x_anchor, alignment = int(width * 0.07), "left"
    elif placement == "right":
        x_anchor, alignment = int(width * 0.93), "right"
    else:
        x_anchor, alignment = width // 2, "center"

    accent_width = max(65, int(width * 0.15))
    accent_height = max(5, selected_size // 12)

    if alignment == "left":
        accent_x = x_anchor
    elif alignment == "right":
        accent_x = x_anchor - accent_width
    else:
        accent_x = (width - accent_width) // 2

    accent_y = max(
        10,
        start_y - max(18, selected_size // 2),
    )

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

        x = max(
            12,
            min(x, width - text_width - 12),
        )

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
        "average_scene_rgb": palette["average_rgb"],
        "scene_brightness": palette["brightness"],
    }

    return image, design


def verify_image(
    path: Path,
    expected_size: tuple[int, int],
) -> None:
    if not path.is_file():
        raise RuntimeError(f"Thumbnail missing: {path}")

    if path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError("Thumbnail file is unexpectedly small.")

    try:
        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            if image.format != "JPEG":
                raise RuntimeError("Thumbnail is not JPEG.")

            if image.size != expected_size:
                raise RuntimeError(
                    f"Wrong thumbnail dimensions: {image.size}; "
                    f"expected {expected_size}."
                )

    except Exception as exc:
        raise RuntimeError(
            f"Thumbnail verification failed: {exc}"
        ) from exc


def create_thumbnail(
    source: Path,
    title: str,
    dimensions: tuple[int, int],
) -> dict:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")

    focal = focal_point(image)

    image = crop_to_ratio(
        image,
        dimensions[0] / dimensions[1],
        focal,
    )

    image = image.resize(
        dimensions,
        Image.Resampling.LANCZOS,
    )

    image = cinematic_grade(image)

    image, design = draw_title(
        image=image,
        title=title,
        vertical=dimensions[1] > dimensions[0],
        focal=focal,
    )

    temporary = OUTPUT_FILE.with_name(
        OUTPUT_FILE.stem + ".tmp.jpg"
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

        verify_image(temporary, dimensions)
        os.replace(temporary, OUTPUT_FILE)

    finally:
        temporary.unlink(missing_ok=True)

    return {
        "file": OUTPUT_FILE.relative_to(ROOT).as_posix(),
        "width": dimensions[0],
        "height": dimensions[1],
        "bytes": OUTPUT_FILE.stat().st_size,
        "sha256": sha256_file(OUTPUT_FILE),
        "success": True,
        "design": design,
    }


def verify_saved_manifest(
    manifest: dict,
    expected_format: str,
    expected_size: tuple[int, int],
) -> None:
    if manifest.get("status") != "completed":
        raise RuntimeError("Thumbnail manifest status is incomplete.")

    if manifest.get("format") != expected_format:
        raise RuntimeError("Thumbnail manifest FORMAT mismatch.")

    declared = (
        manifest.get("thumbnail")
        or manifest.get("path")
        or manifest.get("output")
    )

    if declared != OUTPUT_FILE.relative_to(ROOT).as_posix():
        raise RuntimeError(
            "Thumbnail manifest does not point to output/thumbnail.jpg."
        )

    outputs = manifest.get("thumbnails")

    if not isinstance(outputs, list) or len(outputs) != 1:
        raise RuntimeError(
            "Manifest must contain exactly one format-correct thumbnail."
        )

    item = outputs[0]

    if not isinstance(item, dict):
        raise RuntimeError("Invalid thumbnail manifest output record.")

    if item.get("file") != declared:
        raise RuntimeError("Thumbnail manifest output path mismatch.")

    if item.get("success") is not True:
        raise RuntimeError("Thumbnail is not marked successful.")

    if (
        int(item.get("width", 0)),
        int(item.get("height", 0)),
    ) != expected_size:
        raise RuntimeError("Thumbnail manifest dimensions mismatch.")

    verify_image(OUTPUT_FILE, expected_size)

    actual_hash = sha256_file(OUTPUT_FILE)

    if item.get("sha256") != actual_hash:
        raise RuntimeError("Thumbnail SHA256 verification failed.")

    if manifest.get("source_image_sha256") != manifest.get(
        "selected_image_sha256"
    ):
        raise RuntimeError("Source image SHA256 metadata mismatch.")


def main() -> None:
    log("=" * 65)
    log("KATHA LOK AI — STORY-DRIVEN CINEMATIC THUMBNAIL")
    log("=" * 65)

    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_FILE)
    mode = get_format(config)

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    minimum_part, maximum_part = get_parts_range(config)
    selected_parts = get_parts(config)

    if not minimum_part <= selected_parts <= maximum_part:
        raise RuntimeError("Selected PARTS is outside configured range.")

    story_data, story_path = load_story_document()
    validate_story_status(story_data)

    story_text = extract_story_text(story_data)
    summary = extract_story_summary(story_data)

    title, title_source = load_title(
        story_data,
        config,
        story_text,
        summary,
    )

    title = shorten_title(title)

    if not title:
        raise RuntimeError("Story-derived thumbnail title is empty.")

    hero = choose_hero(
        minimum_part,
        maximum_part,
    )

    source = hero["path"]

    if not source.is_file():
        raise RuntimeError("Selected story image disappeared.")

    dimensions = SHORT_SIZE if mode == "short" else FULL_SIZE

    thumbnail_record = create_thumbnail(
        source=source,
        title=title,
        dimensions=dimensions,
    )

    verify_image(OUTPUT_FILE, dimensions)

    source_hash = sha256_file(source)

    manifest = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "title": title,
        "title_source": title_source,
        "story_source": story_path.relative_to(ROOT).as_posix(),
        "story_word_count": len(re.findall(r"\S+", story_text)),
        "configured_part_range": [
            minimum_part,
            maximum_part,
        ],
        "selected_parts": selected_parts,
        "thumbnail": OUTPUT_FILE.relative_to(ROOT).as_posix(),
        "path": OUTPUT_FILE.relative_to(ROOT).as_posix(),
        "output": OUTPUT_FILE.relative_to(ROOT).as_posix(),
        "width": dimensions[0],
        "height": dimensions[1],
        "source_image": source.relative_to(ROOT).as_posix(),
        "source_manifest": hero["manifest"],
        "source_image_sha256": source_hash,
        "selected_image_sha256": source_hash,
        "selected_part": hero["part"],
        "selected_scene": hero["scene"],
        "selected_scene_id": hero["scene_id"],
        "selected_role": hero["role"],
        "scene_score": hero["score"],
        "design": {
            "story_specific_title": True,
            "title_not_taken_from_first_sentence": True,
            "complete_story_read": True,
            "dramatic_scene_selection": True,
            "story_role_aware_selection": True,
            "adaptive_focal_crop": True,
            "adaptive_title_position": True,
            "adaptive_scene_palette": True,
            "cinematic_color_grade": True,
            "adaptive_gradient_for_readability": True,
            "bold_hindi_font": True,
            "fixed_red_gold_template": False,
            "duplicate_thumbnail_copies": False,
            "format_specific_dimensions": True,
            "ai_character_generation": False,
        },
        "thumbnails": [
            thumbnail_record,
        ],
    }

    write_json(MANIFEST_FILE, manifest)

    saved = read_json(MANIFEST_FILE, required=True)

    if not isinstance(saved, dict):
        raise RuntimeError("Thumbnail manifest could not be reloaded.")

    verify_saved_manifest(
        saved,
        mode,
        dimensions,
    )

    log("-" * 65)
    log("FORMAT:", mode)
    log("TITLE:", title)
    log("TITLE SOURCE:", title_source)
    log("STORY SOURCE:", story_path.relative_to(ROOT))
    log("PART RANGE:", f"{minimum_part}-{maximum_part}")
    log("SELECTED PART COUNT:", selected_parts)
    log("HERO PART:", hero["part"])
    log("HERO SCENE:", hero["scene_id"])
    log("HERO ROLE:", hero["role"])
    log("THUMBNAIL:", OUTPUT_FILE.relative_to(ROOT))
    log("DIMENSIONS:", f"{dimensions[0]}x{dimensions[1]}")
    log("SHA256:", thumbnail_record["sha256"])
    log("MANIFEST:", MANIFEST_FILE.relative_to(ROOT))
    log("THUMBNAIL GENERATION: SUCCESS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Thumbnail generation cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("THUMBNAIL GENERATION FAILED:", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)