#!/usr/bin/env python3
"""Create validated, resumable image-generation jobs for Photo Motion."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUT = ROOT / "output"
SCENES_FILE = OUT / "scenes" / "scenes.json"
BIBLE_FILE = OUT / "story" / "character_bible.json"
VISUALS_DIR = OUT / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
CHECKPOINT_FILE = OUT / "checkpoints" / "visual_jobs_progress.json"

VERSION = "photo-motion-visual-jobs-v3"
MIN_PROMPT_LENGTH = 12


def log(*values):
    print(*values, flush=True)


def read_json(path, required=False):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc
    return data


def atomic_json(path, data):
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


def digest(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clean(value):
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def safe_number(value, default=None):
    try:
        number = float(value)
        if number > 0 and number < 3601:
            return round(number, 3)
    except (TypeError, ValueError):
        pass
    return default


def load_character_bible():
    data = read_json(BIBLE_FILE)
    if data is None:
        return {"status": "disabled", "characters": [], "world": {}}

    if not isinstance(data, dict):
        raise RuntimeError("character_bible.json root must be an object.")

    status = clean(data.get("status")).lower()
    if status not in {"completed", "disabled"}:
        raise RuntimeError(
            f"Character Bible is not ready: status={status or 'missing'}"
        )

    characters = data.get("characters", [])
    world = data.get("world", {})

    if not isinstance(characters, list) or not isinstance(world, dict):
        raise RuntimeError("Character Bible structure is invalid.")

    return data


def character_context(scene, bible):
    references = (
        scene.get("characters")
        or scene.get("character_ids")
        or scene.get("character_names")
        or []
    )
    if not isinstance(references, list):
        references = [references]

    wanted = {clean(item).casefold() for item in references if clean(item)}
    if not wanted:
        return ""

    selected = []
    for item in bible.get("characters", []):
        if not isinstance(item, dict):
            continue
        candidates = {
            clean(item.get("id")).casefold(),
            clean(item.get("name")).casefold(),
            clean(item.get("character_id")).casefold(),
        }
        if wanted.intersection(candidates):
            selected.append({
                key: item[key]
                for key in (
                    "id", "name", "age", "appearance", "face",
                    "hair", "clothing", "distinctive_features",
                    "visual_identity", "description",
                )
                if key in item and clean(item[key])
            })

    return json.dumps(selected, ensure_ascii=False) if selected else ""


def build_negative_prompt(config, source):
    values = [
        "unrealistic anatomy",
        "distorted face",
        "extra fingers",
        "missing fingers",
        "duplicated people",
        "plastic skin",
        "waxy face",
        "floating objects",
        "unwanted text",
        "captions",
        "subtitles",
        "watermark",
        "logo",
        "low resolution",
        "blurred subject",
    ]

    if as_bool(config.get("AVOID_CARTOON_LOOK"), True):
        values.extend([
            "cartoon", "anime", "comic-book illustration",
            "2D drawing", "3D animated character", "painting",
        ])

    if as_bool(config.get("AVOID_NEON"), True):
        values.extend(["unnecessary neon", "artificial neon glow"])

    if as_bool(config.get("AVOID_GLITCH_EFFECTS"), True):
        values.extend(["glitch effect", "digital distortion", "frame tearing"])

    original = clean(source.get("negative_prompt"))
    if original:
        values.append(original)

    unique = []
    seen = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)

    return ", ".join(unique)


def build_prompt(config, source, bible):
    visual = clean(
        source.get("visual_prompt")
        or source.get("visual")
        or source.get("description")
    )
    if len(visual) < MIN_PROMPT_LENGTH:
        raise RuntimeError(
            f"Scene {source.get('part')}/{source.get('scene')} "
            "has no usable visual prompt."
        )

    sections = [
        visual,
        (
            "Photorealistic live-action cinematic film still. "
            "Real human actors, believable physical environment, "
            "natural skin texture, realistic eyes and anatomy."
        ),
        "Use physically plausible lighting, shadows, reflections and materials.",
        "Use purposeful cinematic composition with clear subject separation.",
        "Do not change the approved story event or invent unrelated details.",
        "Do not include captions, subtitles, logos, labels or written text.",
    ]

    style = clean(config.get("VISUAL_STYLE")) or "cinematic_realistic"
    realism = clean(config.get("REALISM")) or "high"
    quality = clean(config.get("QUALITY")) or "high"
    camera_style = clean(config.get("CAMERA_STYLE")) or "cinematic"
    lighting = clean(config.get("LIGHTING")) or "cinematic"
    mood = clean(source.get("mood") or config.get("MOOD")) or "dramatic"

    sections.append(f"Visual style: {style}. Realism: {realism}. Quality: {quality}.")
    sections.append(f"Camera language: {camera_style}.")
    sections.append(f"Lighting treatment: {clean(source.get('lighting_prompt') or source.get('lighting') or lighting)}.")
    sections.append(f"Emotional mood: {mood}.")

    for key, label in (
        ("action", "Visible physical action"),
        ("world_context", "Story-world location and continuity"),
        ("camera_prompt", "Camera direction"),
    ):
        value = clean(source.get(key))
        if value:
            sections.append(f"{label}: {value}")

    if as_bool(config.get("CHARACTER_CONSISTENCY"), True):
        context = character_context(source, bible)
        if context:
            sections.append(
                "Character identity references. Preserve these established "
                "identities, faces, hairstyles and clothing: " + context
            )

    if as_bool(config.get("WORLD_CONSISTENCY"), True):
        world = bible.get("world", {})
        if world:
            sections.append(
                "Preserve established world continuity: "
                + json.dumps(world, ensure_ascii=False)
            )

    return "\n\n".join(sections)


def image_dimensions(format_name):
    if format_name == "short":
        return 768, 1024
    return 1024, 576


def flatten_scenes(document, expected_parts, expected_scenes):
    if not isinstance(document, dict):
        raise RuntimeError("scenes.json root must be an object.")

    rows = document.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("scenes.json has no scenes array.")

    expected_total = expected_parts * expected_scenes
    if len(rows) != expected_total:
        raise RuntimeError(
            f"Expected {expected_total} scenes "
            f"({expected_parts} x {expected_scenes}), got {len(rows)}."
        )

    prepared = []
    seen = set()

    for index, original in enumerate(rows, start=1):
        if not isinstance(original, dict):
            raise RuntimeError(f"Scene row {index} is not an object.")

        try:
            part = int(original.get("part"))
            local = int(original.get("scene"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part/scene number in row {index}."
            ) from exc

        if not 1 <= part <= expected_parts:
            raise RuntimeError(f"Invalid part number: {part}")
        if not 1 <= local <= expected_scenes:
            raise RuntimeError(f"Invalid scene number: {part}/{local}")

        pair = (part, local)
        if pair in seen:
            raise RuntimeError(f"Duplicate scene: {part}/{local}")
        seen.add(pair)

        prepared.append((part, local, dict(original)))

    expected = {
        (part, scene)
        for part in range(1, expected_parts + 1)
        for scene in range(1, expected_scenes + 1)
    }
    missing = sorted(expected - seen)
    if missing:
        raise RuntimeError(f"Missing scenes: {missing[:20]}")

    return sorted(prepared, key=lambda item: (item[0], item[1]))


def build_jobs(config, scenes, bible, format_name):
    width, height = image_dimensions(format_name)
    jobs = []
    global_number = 0

    for part, local, source in scenes:
        global_number += 1

        prompt = build_prompt(config, source, bible)
        negative = build_negative_prompt(config, source)

        duration = safe_number(
            source.get("duration")
            or source.get("scene_duration")
            or source.get("duration_seconds"),
            default=5.0,
        )

        relative_image = (
            f"output/visuals/part_{part:02d}/"
            f"scene_{local:02d}.png"
        )

        scene_id = clean(
            source.get("scene_id")
            or source.get("id")
            or f"part_{part:02d}_scene_{local:02d}"
        )

        job = {
            "id": scene_id,
            "scene_id": scene_id,
            "part": part,
            "scene": local,
            "local_scene": local,
            "global_scene": global_number,
            "scene_index": global_number,
            "title": clean(source.get("title")),
            "narration": clean(source.get("narration")),
            "prompt": prompt,
            "image_prompt": prompt,
            "visual_prompt": prompt,
            "negative_prompt": negative,
            "image_size": f"{width}x{height}",
            "image_path": relative_image,
            "output_path": relative_image,
            "duration": duration,
            "scene_duration": duration,
            "source_scene_signature": digest(source),
        }

        jobs.append(job)

    return jobs

def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")

    format_name = get_format(config)
    parts_count = get_parts(config)
    scenes_per_part = get_scenes(config)

    source_document = read_json(SCENES_FILE, required=True)
    bible = load_character_bible()

    scenes = flatten_scenes(
        source_document,
        parts_count,
        scenes_per_part,
    )

    jobs = build_jobs(
        config,
        scenes,
        bible,
        format_name,
    )

    if not jobs:
        raise RuntimeError("No visual-generation jobs were created.")

    source_fingerprint = digest({
        "scenes": source_document,
        "character_bible": bible,
    })

    config_signature = digest({
        "format": format_name,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "image_size": jobs[0]["image_size"],
        "visual_style": config.get("VISUAL_STYLE"),
        "realism": config.get("REALISM"),
        "quality": config.get("QUALITY"),
        "camera_style": config.get("CAMERA_STYLE"),
        "lighting": config.get("LIGHTING"),
        "mood": config.get("MOOD"),
        "character_consistency": config.get("CHARACTER_CONSISTENCY"),
        "world_consistency": config.get("WORLD_CONSISTENCY"),
        "scene_continuity": config.get("SCENE_CONTINUITY"),
        "avoid_cartoon": config.get("AVOID_CARTOON_LOOK"),
        "avoid_neon": config.get("AVOID_NEON"),
        "avoid_glitch": config.get("AVOID_GLITCH_EFFECTS"),
    })

    document = {
        "version": VERSION,
        "status": "completed",
        "format": format_name,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(jobs),
        "image_size": jobs[0]["image_size"],
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "character_bible_status": bible.get("status", "unknown"),
        "jobs": jobs,
    }

    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Write the complete job file atomically. The image generator,
    # depth generator and Photo Motion renderer all consume this file.
    atomic_json(JOBS_FILE, document)

    progress = {
        "version": VERSION,
        "status": "completed",
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "total_scenes": len(jobs),
        "completed_jobs": len(jobs),
        "scene_order": [
            {
                "part": job["part"],
                "scene": job["local_scene"],
                "global_scene": job["global_scene"],
                "scene_id": job["scene_id"],
                "image_path": job["image_path"],
                "prompt_sha256": hashlib.sha256(
                    job["prompt"].encode("utf-8")
                ).hexdigest(),
            }
            for job in jobs
        ],
    }
    atomic_json(CHECKPOINT_FILE, progress)

    log("=" * 55)
    log("PHOTO MOTION VISUAL JOBS READY")
    log("Format:", format_name)
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total image jobs:", len(jobs))
    log("Image size:", jobs[0]["image_size"])
    log("Character Bible:", bible.get("status", "unknown"))
    log("Jobs file:", JOBS_FILE.relative_to(ROOT))
    log("Checkpoint:", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 55)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("VISUAL JOB GENERATION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(
            f"VISUAL JOB GENERATION FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)