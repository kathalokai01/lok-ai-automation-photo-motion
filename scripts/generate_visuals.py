#!/usr/bin/env python3
"""Build a resumable, schema-compatible visual generation queue."""

import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from input_config import load_input_config, normalize_format


ROOT = Path.cwd()
BASE = ROOT / "output"

SCENES_FILE = BASE / "scenes" / "scenes.json"
CHARACTER_BIBLE_FILE = BASE / "story" / "character_bible.json"

VISUALS_DIR = BASE / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"

WORKER_DIR = BASE / "visual_worker"
QUEUE_JSON = WORKER_DIR / "visual_generation_queue.json"
QUEUE_ZIP = BASE / "i2v" / "visual_generation_queue.zip"


def log(*args):
    print(*args, flush=True)


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    if value is None:
        return default

    value = str(value).strip().lower()

    if value in {"true", "1", "yes", "y", "on"}:
        return True

    if value in {"false", "0", "no", "n", "off"}:
        return False

    return default


def cfg_int(config, key, default=0):
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def load_json(path, default=None):
    path = Path(path)

    if not path.is_file():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def save_json(path, data):
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


def sha256_text(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def scene_key(part, scene):
    return f"part_{part:02d}/scene_{scene:02d}"


def get_image_size(format_name):
    if format_name == "short":
        return "768x1024"

    if format_name == "full":
        return "1024x576"

    raise RuntimeError(f"Unsupported format: {format_name}")


def build_negative_prompt(config):
    negative = []

    if cfg_bool(config, "AVOID_CARTOON_LOOK", True):
        negative.extend([
            "cartoon",
            "comic",
            "anime",
            "manga",
            "illustration",
            "drawing",
            "painting",
            "2D art",
        ])

    if cfg_bool(config, "AVOID_NEON", True):
        negative.append("neon colors")

    if cfg_bool(config, "AVOID_GLITCH_EFFECTS", True):
        negative.extend([
            "glitch",
            "digital distortion",
            "flicker",
        ])

    negative.extend([
        "CGI look",
        "3D render",
        "game graphics",
        "plastic skin",
        "artificial face",
        "deformed anatomy",
        "extra fingers",
        "extra limbs",
        "duplicate person",
        "watermark",
        "logo",
        "text",
        "low quality",
        "blurry face",
    ])

    return ", ".join(negative)


def get_character_context(scene, character_bible):
    if not isinstance(character_bible, dict):
        return ""

    characters = scene.get(
        "characters",
        scene.get("character_ids", []),
    )

    if not isinstance(characters, list) or not characters:
        return ""

    bible_characters = character_bible.get("characters", [])

    if not isinstance(bible_characters, list):
        return ""

    wanted = {str(value) for value in characters}

    selected = [
        character
        for character in bible_characters
        if isinstance(character, dict)
        and str(character.get("id", "")) in wanted
    ]

    if not selected:
        return ""

    return json.dumps(
        selected,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_visual_prompt(config, scene, character_bible):
    visual_style = cfg_text(
        config, "VISUAL_STYLE", "cinematic_realistic"
    )
    realism = cfg_text(config, "REALISM", "high")
    camera_style = cfg_text(config, "CAMERA_STYLE", "cinematic")
    lighting = cfg_text(config, "LIGHTING", "cinematic")
    mood = cfg_text(config, "MOOD", "dramatic")
    quality = cfg_text(config, "QUALITY", "high")

    scene_prompt = str(
        scene.get(
            "visual_prompt",
            scene.get("visual", scene.get("description", "")),
        )
    ).strip()

    world_context = str(
        scene.get("world_context", scene.get("world", ""))
    ).strip()

    action = str(scene.get("action", "")).strip()

    character_context = get_character_context(
        scene, character_bible
    )

    prompt_parts = [
        "Photorealistic live-action cinematic scene.",
        "Real human beings, not animated characters.",
        "Real-world physical environment.",
        f"Visual style: {visual_style}.",
        f"Realism: {realism}.",
        f"Camera style: {camera_style}.",
        f"Lighting: {lighting}.",
        f"Mood: {mood}.",
        f"Quality: {quality}.",
        "Natural human anatomy.",
        "Natural skin texture.",
        "Physically believable lighting.",
        "Realistic shadows.",
        "Maintain character identity and clothing continuity.",
        "Maintain environment and location continuity.",
        "Cinematic composition and natural camera perspective.",
    ]

    if cfg_bool(config, "CHARACTER_CONSISTENCY", True):
        prompt_parts.append(
            "Preserve the exact appearance of recurring characters."
        )

    if cfg_bool(config, "WORLD_CONSISTENCY", True):
        prompt_parts.append(
            "Preserve the same world, architecture, time period "
            "and environmental details."
        )

    if cfg_bool(config, "SCENE_CONTINUITY", True):
        prompt_parts.append(
            "Maintain visual continuity with adjacent scenes."
        )

    if scene_prompt:
        prompt_parts.append(f"Scene description: {scene_prompt}")

    if action:
        prompt_parts.append(f"Scene action: {action}")

    if world_context:
        prompt_parts.append(f"World context: {world_context}")

    if character_context:
        prompt_parts.append(
            "Character Bible reference: " + character_context
        )

    return " ".join(prompt_parts)


def make_config_signature(config, format_name):
    relevant = {
        "format": format_name,
        "visual_style": cfg_text(config, "VISUAL_STYLE", ""),
        "realism": cfg_text(config, "REALISM", ""),
        "camera_style": cfg_text(config, "CAMERA_STYLE", ""),
        "lighting": cfg_text(config, "LIGHTING", ""),
        "mood": cfg_text(config, "MOOD", ""),
        "quality": cfg_text(config, "QUALITY", ""),
        "character_consistency": cfg_bool(
            config, "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": cfg_bool(
            config, "WORLD_CONSISTENCY", True
        ),
        "scene_continuity": cfg_bool(
            config, "SCENE_CONTINUITY", True
        ),
        "avoid_cartoon": cfg_bool(
            config, "AVOID_CARTOON_LOOK", True
        ),
        "avoid_neon": cfg_bool(config, "AVOID_NEON", True),
        "avoid_glitch": cfg_bool(
            config, "AVOID_GLITCH_EFFECTS", True
        ),
    }

    raw = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
    )

    return sha256_text(raw)


def validate_scenes(scenes):
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("scenes.json contains no scenes.")

    result = []
    seen = set()

    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Scene {index} is not a JSON object."
            )

        try:
            part = int(scene.get("part"))
            local_scene = int(scene.get("scene"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Scene {index} has invalid part/scene numbers."
            ) from exc

        if part < 1 or local_scene < 1:
            raise RuntimeError(
                f"Scene {index} has non-positive numbering."
            )

        key = scene_key(part, local_scene)

        if key in seen:
            raise RuntimeError(
                f"Duplicate scene identifier: {key}"
            )

        seen.add(key)

        result.append({
            "part": part,
            "scene": local_scene,
            "source": scene,
        })

    return sorted(
        result,
        key=lambda item: (item["part"], item["scene"]),
    )


def image_is_valid(path):
    path = Path(path)

    if not path.is_file() or path.stat().st_size <= 1000:
        return False

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        return True

    except ImportError:
        return True

    except Exception:
        return False


def load_existing_jobs():
    document = load_json(JOBS_FILE, {})

    if isinstance(document, list):
        rows = document
        metadata = {}
    elif isinstance(document, dict):
        rows = (
            document.get("jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
            or []
        )
        metadata = document
    else:
        rows = []
        metadata = {}

    existing = {}

    if isinstance(rows, dict):
        for key, row in rows.items():
            if isinstance(row, dict):
                existing[str(key)] = row

    elif isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            key = row.get("id") or row.get("scene_id")

            if not key:
                try:
                    key = scene_key(
                        int(row["part"]),
                        int(row.get("local_scene", row["scene"])),
                    )
                except (KeyError, TypeError, ValueError):
                    continue

            existing[str(key)] = row

    signature = str(metadata.get("config_signature", ""))

    return existing, signature


def can_reuse_image(
    output_path,
    old_job,
    old_signature,
    new_signature,
    prompt_hash,
):
    if not image_is_valid(output_path):
        return False

    if old_signature != new_signature:
        return False

    if not isinstance(old_job, dict):
        return False

    if old_job.get("status") != "completed":
        return False

    old_hash = old_job.get("prompt_hash")

    return bool(old_hash and old_hash == prompt_hash)


def make_job(
    item,
    index,
    config,
    character_bible,
    negative_prompt,
    image_size,
    signature,
    existing_jobs,
    old_signature,
):
    part = item["part"]
    local_scene = item["scene"]
    scene = item["source"]

    key = scene_key(part, local_scene)
    output_path = (
        VISUALS_DIR
        / f"part_{part:02d}"
        / f"scene_{local_scene:02d}.png"
    )

    prompt = build_visual_prompt(
        config, scene, character_bible
    )

    prompt_hash = sha256_text(
        prompt + "\n" + negative_prompt
    )

    old_job = existing_jobs.get(key, {})

    reusable = (
        cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
        and can_reuse_image(
            output_path,
            old_job,
            old_signature,
            signature,
            prompt_hash,
        )
    )

    status = "completed" if reusable else "pending"

    # Global scene numbering is independent of local scene numbering.
    global_scene = index

    return {
        "id": key,
        "scene_id": key,
        "part": part,
        "scene": local_scene,
        "local_scene": local_scene,
        "global_scene": global_scene,
        "scene_index": global_scene,
        "status": status,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "image_size": image_size,
        "format": normalize_format(config),
        "output_path": str(output_path),
        "image_path": str(output_path),
        "scene_duration": scene.get("duration"),
        "duration": scene.get("duration"),
        "prompt_hash": prompt_hash,
        "config_signature": signature,
        "source_scene": scene,
    }


def verify_zip(zip_path, expected):
    with zipfile.ZipFile(zip_path, "r") as archive:
        names = set(archive.namelist())

        required = {
            "visual_generation_queue.json",
            "scenes.json",
            "worker_config.json",
        }

        missing = required - names

        if missing:
            raise RuntimeError(
                "Worker ZIP missing required files: "
                + ", ".join(sorted(missing))
            )

        corrupt = archive.testzip()

        if corrupt:
            raise RuntimeError(
                f"Corrupt ZIP entry: {corrupt}"
            )

        archived_queue = json.loads(
            archive.read(
                "visual_generation_queue.json"
            ).decode("utf-8")
        )

        jobs = archived_queue.get("jobs")

        if not isinstance(jobs, list):
            raise RuntimeError(
                "Worker ZIP queue jobs must be a list."
            )

        if len(jobs) != expected:
            raise RuntimeError(
                "Worker ZIP scene count mismatch."
            )

        numbers = [
            int(job.get("global_scene", 0))
            for job in jobs
        ]

        if numbers != list(range(1, expected + 1)):
            raise RuntimeError(
                "Worker ZIP global scene numbers are not continuous."
            )


def build_worker_queue():
    log("=" * 70)
    log("BUILDING SCHEMA-COMPATIBLE VISUAL WORKER QUEUE")
    log("=" * 70)

    config = load_input_config()
    format_name = normalize_format(config)

    parts = cfg_int(config, "PARTS", 1)
    scenes_per_part = cfg_int(config, "SCENES", 1)

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "PARTS and SCENES must both be positive."
        )

    image_size = get_image_size(format_name)

    log("FORMAT:", format_name)
    log("IMAGE SIZE:", image_size)
    log("PARTS:", parts)
    log("SCENES/PART:", scenes_per_part)

    scenes_data = load_json(SCENES_FILE)

    if not isinstance(scenes_data, dict):
        raise RuntimeError(
            "scenes.json must contain a JSON object."
        )

    if scenes_data.get("status") != "completed":
        raise RuntimeError(
            "scenes.json is not marked completed."
        )

    scenes = validate_scenes(
        scenes_data.get("scenes", [])
    )

    expected = parts * scenes_per_part

    if len(scenes) != expected:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected}, "
            f"but scenes.json contains {len(scenes)}."
        )

    character_bible = {}

    if cfg_bool(config, "CHARACTER_BIBLE", True):
        if not CHARACTER_BIBLE_FILE.is_file():
            raise RuntimeError(
                "Character Bible is enabled but missing: "
                f"{CHARACTER_BIBLE_FILE}"
            )

        character_bible = load_json(
            CHARACTER_BIBLE_FILE, {}
        )

        if not isinstance(character_bible, dict):
            raise RuntimeError(
                "character_bible.json is invalid."
            )

    existing_jobs, old_signature = load_existing_jobs()

    signature = make_config_signature(
        config, format_name
    )

    negative_prompt = build_negative_prompt(config)

    jobs = []

    for index, item in enumerate(scenes, start=1):
        jobs.append(
            make_job(
                item=item,
                index=index,
                config=config,
                character_bible=character_bible,
                negative_prompt=negative_prompt,
                image_size=image_size,
                signature=signature,
                existing_jobs=existing_jobs,
                old_signature=old_signature,
            )
        )

    completed = sum(
        job["status"] == "completed" for job in jobs
    )
    pending = len(jobs) - completed

    queue = {
        "queue_version": 2,
        "status": "completed" if pending == 0 else "pending",
        "worker_type": "free_gpu_visual_worker",
        "generation_mode": "text_to_image",
        "format": format_name,
        "image_size": image_size,
        "expected_total": expected,
        "completed": completed,
        "pending": pending,
        "config_signature": signature,
        "character_bible_path": (
            str(CHARACTER_BIBLE_FILE)
            if character_bible else None
        ),
        "jobs": jobs,
    }

    WORKER_DIR.mkdir(parents=True, exist_ok=True)

    save_json(QUEUE_JSON, queue)

    worker_scenes = WORKER_DIR / "scenes.json"
    shutil.copy2(SCENES_FILE, worker_scenes)

    if character_bible:
        shutil.copy2(
            CHARACTER_BIBLE_FILE,
            WORKER_DIR / "character_bible.json",
        )

    worker_config = WORKER_DIR / "worker_config.json"

    save_json(
        worker_config,
        {
            "queue_version": 2,
            "format": format_name,
            "image_size": image_size,
            "generation_mode": "text_to_image",
            "visual_style": cfg_text(
                config, "VISUAL_STYLE", "cinematic_realistic"
            ),
            "realism": cfg_text(config, "REALISM", "high"),
            "camera_style": cfg_text(
                config, "CAMERA_STYLE", "cinematic"
            ),
            "lighting": cfg_text(
                config, "LIGHTING", "cinematic"
            ),
            "mood": cfg_text(config, "MOOD", "dramatic"),
            "negative_prompt": negative_prompt,
            "config_signature": signature,
        },
    )

    QUEUE_ZIP.parent.mkdir(parents=True, exist_ok=True)

    temporary_zip = QUEUE_ZIP.with_suffix(".zip.tmp")
    temporary_zip.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(
            temporary_zip,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.write(
                QUEUE_JSON,
                "visual_generation_queue.json",
            )
            archive.write(
                worker_scenes,
                "scenes.json",
            )

            if character_bible:
                archive.write(
                    WORKER_DIR / "character_bible.json",
                    "character_bible.json",
                )

            archive.write(
                worker_config,
                "worker_config.json",
            )

        verify_zip(temporary_zip, expected)
        temporary_zip.replace(QUEUE_ZIP)

    finally:
        temporary_zip.unlink(missing_ok=True)

    # IMPORTANT:
    # jobs is now a LIST, not a dictionary.
    # This matches free_image_generator.py, generate_depth_maps.py
    # and photo_motion.py input readers.
    visual_manifest = {
        "version": 3,
        "status": "completed" if pending == 0 else "pending",
        "format": format_name,
        "image_size": image_size,
        "expected_total": expected,
        "completed": completed,
        "pending": pending,
        "generation_mode": "free_gpu_worker",
        "queue_json": str(QUEUE_JSON),
        "queue_zip": str(QUEUE_ZIP),
        "config_signature": signature,
        "jobs": jobs,
    }

    save_json(JOBS_FILE, visual_manifest)

    log("=" * 70)
    log("VISUAL QUEUE READY")
    log("Expected visuals:", expected)
    log("Reusable images:", completed)
    log("Pending visuals:", pending)
    log("Global scene numbering: 1..", expected)
    log("Jobs schema: LIST")
    log("Queue JSON:", QUEUE_JSON)
    log("Queue ZIP:", QUEUE_ZIP)
    log("Visual manifest:", JOBS_FILE)
    log("=" * 70)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(build_worker_queue())

    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)

    except Exception as exc:
        log("ERROR:", exc)
        sys.exit(1)