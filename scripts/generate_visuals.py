#!/usr/bin/env python3

import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from input_config import (
    load_input_config,
    normalize_format,
)


BASE = Path("output")

SCENES_FILE = BASE / "scenes" / "scenes.json"
CHARACTER_BIBLE_FILE = BASE / "story" / "character_bible.json"

VISUALS_DIR = BASE / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"

WORKER_DIR = BASE / "visual_worker"
QUEUE_JSON = WORKER_DIR / "visual_generation_queue.json"
QUEUE_ZIP = BASE / "i2v" / "visual_generation_queue.zip"


# ============================================================
# CONFIG HELPERS
# ============================================================

def cfg_text(config, key, default=""):
    value = config.get(key, default)

    if value is None:
        return str(default)

    return str(value).strip()


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


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path, default=None):
    if not path.is_file():
        return default

    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)

    except Exception as exc:
        print(f"WARNING: Failed to read {path}: {exc}")
        return default


def save_json(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temp.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )
        file.write("\n")

    os.replace(
        temp,
        path
    )


def sha256_text(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b""
        ):
            digest.update(chunk)

    return digest.hexdigest()


def scene_key(part, scene):
    return f"part_{part:02d}/scene_{scene:02d}"


# ============================================================
# PROMPT SETTINGS
# ============================================================

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


# ============================================================
# CHARACTER CONTEXT
# ============================================================

def get_character_context(scene, character_bible):
    if not isinstance(character_bible, dict):
        return ""

    characters = scene.get(
        "characters",
        scene.get("character_ids", [])
    )

    if not isinstance(characters, list):
        return ""

    if not characters:
        return ""

    bible_characters = character_bible.get(
        "characters",
        []
    )

    if not isinstance(bible_characters, list):
        return ""

    wanted = {
        str(value)
        for value in characters
    }

    selected = []

    for character in bible_characters:
        if not isinstance(character, dict):
            continue

        char_id = str(
            character.get("id", "")
        )

        if char_id in wanted:
            selected.append(character)

    if not selected:
        return ""

    return json.dumps(
        selected,
        ensure_ascii=False,
        separators=(",", ":")
    )


# ============================================================
# VISUAL PROMPT
# ============================================================

def build_visual_prompt(
    config,
    scene,
    character_bible
):
    visual_style = cfg_text(
        config,
        "VISUAL_STYLE",
        "cinematic_realistic"
    )

    realism = cfg_text(
        config,
        "REALISM",
        "high"
    )

    camera_style = cfg_text(
        config,
        "CAMERA_STYLE",
        "cinematic"
    )

    lighting = cfg_text(
        config,
        "LIGHTING",
        "cinematic"
    )

    mood = cfg_text(
        config,
        "MOOD",
        "dramatic"
    )

    quality = cfg_text(
        config,
        "QUALITY",
        "high"
    )

    scene_prompt = str(
        scene.get(
            "visual_prompt",
            scene.get(
                "visual",
                scene.get("description", "")
            )
        )
    ).strip()

    world_context = str(
        scene.get(
            "world_context",
            scene.get("world", "")
        )
    ).strip()

    action = str(
        scene.get("action", "")
    ).strip()

    character_context = get_character_context(
        scene,
        character_bible
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
            "Preserve the same world, architecture, "
            "time period and environmental details."
        )

    if cfg_bool(config, "SCENE_CONTINUITY", True):
        prompt_parts.append(
            "Maintain visual continuity with adjacent scenes."
        )

    if scene_prompt:
        prompt_parts.append(
            f"Scene description: {scene_prompt}"
        )

    if action:
        prompt_parts.append(
            f"Scene action: {action}"
        )

    if world_context:
        prompt_parts.append(
            f"World context: {world_context}"
        )

    if character_context:
        prompt_parts.append(
            "Character Bible reference: "
            + character_context
        )

    return " ".join(prompt_parts)


# ============================================================
# IMAGE SIZE
# ============================================================

def get_image_size(format_name):
    if format_name == "short":
        return "768x1024"

    return "1024x576"


# ============================================================
# CONFIG SIGNATURE
# ============================================================

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
        "avoid_neon": cfg_bool(
            config, "AVOID_NEON", True
        ),
        "avoid_glitch": cfg_bool(
            config, "AVOID_GLITCH_EFFECTS", True
        ),
    }

    raw = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True
    )

    return sha256_text(raw)


# ============================================================
# VALIDATE SCENES
# ============================================================

def validate_scenes(scenes):
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError(
            "scenes.json contains no scenes."
        )

    result = []
    seen = set()

    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Scene {index} is not a JSON object."
            )

        try:
            part = int(scene.get("part"))
            scene_number = int(scene.get("scene"))
        except (TypeError, ValueError):
            raise RuntimeError(
                f"Scene {index} has invalid part/scene numbers."
            )

        if part < 1 or scene_number < 1:
            raise RuntimeError(
                f"Scene {index} has non-positive numbering."
            )

        key = scene_key(part, scene_number)

        if key in seen:
            raise RuntimeError(
                f"Duplicate scene identifier: {key}"
            )

        seen.add(key)

        result.append({
            "part": part,
            "scene": scene_number,
            "source": scene,
        })

    return sorted(
        result,
        key=lambda item: (
            item["part"],
            item["scene"]
        )
    )


# ============================================================
# EXISTING MANIFEST
# ============================================================

def load_existing_jobs():
    existing = load_json(
        JOBS_FILE,
        {}
    )

    if not isinstance(existing, dict):
        return {}, ""

    jobs = existing.get(
        "jobs",
        {}
    )

    if not isinstance(jobs, dict):
        jobs = {}

    signature = str(
        existing.get("config_signature", "")
    )

    return jobs, signature


# ============================================================
# CHECK COMPLETED IMAGE
# ============================================================

def image_is_valid(path):
    if not path.is_file():
        return False

    if path.stat().st_size <= 1000:
        return False

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        return True

    except ImportError:
        # If Pillow is unavailable, retain basic file checks.
        return True

    except Exception:
        return False


def can_reuse_image(
    output_path,
    old_job,
    old_signature,
    new_signature,
    prompt_hash
):
    if not image_is_valid(output_path):
        return False

    if old_signature != new_signature:
        return False

    if not isinstance(old_job, dict):
        return False

    if old_job.get("status") != "completed":
        return False

    old_prompt_hash = old_job.get(
        "prompt_hash"
    )

    # Older manifests did not store prompt_hash.
    # Do not trust them when validating a changed queue.
    if not old_prompt_hash:
        return False

    if old_prompt_hash != prompt_hash:
        return False

    return True


# ============================================================
# WORKER QUEUE
# ============================================================

def build_worker_queue():
    print("=" * 70)
    print("       BUILDING FREE VISUAL WORKER QUEUE")
    print("=" * 70)

    config = load_input_config()

    format_name = normalize_format(config)

    parts = cfg_int(
        config,
        "PARTS",
        1
    )

    scenes_per_part = cfg_int(
        config,
        "SCENES",
        1
    )

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "PARTS and SCENES must both be positive."
        )

    image_size = get_image_size(
        format_name
    )

    print(f"FORMAT          : {format_name}")
    print(f"IMAGE SIZE      : {image_size}")
    print(f"PARTS           : {parts}")
    print(f"SCENES/PART     : {scenes_per_part}")

    # --------------------------------------------------------
    # LOAD SCENES
    # --------------------------------------------------------

    scenes_data = load_json(
        SCENES_FILE
    )

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

    # --------------------------------------------------------
    # CHARACTER BIBLE
    # --------------------------------------------------------

    character_bible = {}

    if cfg_bool(config, "CHARACTER_BIBLE", True):
        if not CHARACTER_BIBLE_FILE.is_file():
            raise RuntimeError(
                "Character Bible is enabled but missing: "
                f"{CHARACTER_BIBLE_FILE}"
            )

        character_bible = load_json(
            CHARACTER_BIBLE_FILE,
            {}
        )

        if not isinstance(character_bible, dict):
            raise RuntimeError(
                "character_bible.json is invalid."
            )

    # --------------------------------------------------------
    # PREVIOUS JOBS
    # --------------------------------------------------------

    existing_jobs, old_signature = load_existing_jobs()

    signature = make_config_signature(
        config,
        format_name
    )

    negative_prompt = build_negative_prompt(
        config
    )

    jobs = []
    completed = 0
    pending = 0

    # --------------------------------------------------------
    # BUILD JOBS
    # --------------------------------------------------------

    for index, item in enumerate(scenes, start=1):
        part = item["part"]
        scene_number = item["scene"]
        scene = item["source"]

        key = scene_key(
            part,
            scene_number
        )

        output_path = (
            VISUALS_DIR
            / f"part_{part:02d}"
            / f"scene_{scene_number:02d}.png"
        )

        prompt = build_visual_prompt(
            config,
            scene,
            character_bible
        )

        prompt_hash = sha256_text(
            prompt + "\n" + negative_prompt
        )

        old_job = existing_jobs.get(
            key,
            {}
        )

        reusable = (
            cfg_bool(
                config,
                "SKIP_COMPLETED_SCENES",
                True
            )
            and can_reuse_image(
                output_path,
                old_job,
                old_signature,
                signature,
                prompt_hash
            )
        )

        status = (
            "completed"
            if reusable
            else "pending"
        )

        if reusable:
            completed += 1
        else:
            pending += 1

        jobs.append({
            "id": key,
            "part": part,
            "scene": scene_number,
            "status": status,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "image_size": image_size,
            "format": format_name,
            "output_path": str(output_path),
            "scene_duration": scene.get("duration"),
            "scene_index": index,
            "prompt_hash": prompt_hash,
            "config_signature": signature,
            "source_scene": scene,
        })

    # --------------------------------------------------------
    # QUEUE DOCUMENT
    # --------------------------------------------------------

    queue = {
        "queue_version": 1,
        "status": (
            "completed"
            if pending == 0
            else "pending"
        ),
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
            if character_bible
            else None
        ),
        "jobs": jobs,
    }

    # --------------------------------------------------------
    # PREPARE WORKER DIRECTORY
    # --------------------------------------------------------

    WORKER_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    save_json(
        QUEUE_JSON,
        queue
    )

    worker_scenes = (
        WORKER_DIR / "scenes.json"
    )

    shutil.copy2(
        SCENES_FILE,
        worker_scenes
    )

    if character_bible:
        shutil.copy2(
            CHARACTER_BIBLE_FILE,
            WORKER_DIR / "character_bible.json"
        )

    worker_config = (
        WORKER_DIR / "worker_config.json"
    )

    save_json(
        worker_config,
        {
            "queue_version": 1,
            "format": format_name,
            "image_size": image_size,
            "generation_mode": "text_to_image",
            "visual_style": cfg_text(
                config,
                "VISUAL_STYLE",
                "cinematic_realistic"
            ),
            "realism": cfg_text(
                config,
                "REALISM",
                "high"
            ),
            "camera_style": cfg_text(
                config,
                "CAMERA_STYLE",
                "cinematic"
            ),
            "lighting": cfg_text(
                config,
                "LIGHTING",
                "cinematic"
            ),
            "mood": cfg_text(
                config,
                "MOOD",
                "dramatic"
            ),
            "negative_prompt": negative_prompt,
            "config_signature": signature,
        }
    )

    # --------------------------------------------------------
    # CREATE ZIP ATOMICALLY
    # --------------------------------------------------------

    QUEUE_ZIP.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary_zip = QUEUE_ZIP.with_suffix(
        ".zip.tmp"
    )

    if temporary_zip.exists():
        temporary_zip.unlink()

    try:
        with zipfile.ZipFile(
            temporary_zip,
            "w",
            compression=zipfile.ZIP_DEFLATED
        ) as archive:

            archive.write(
                QUEUE_JSON,
                "visual_generation_queue.json"
            )

            archive.write(
                worker_scenes,
                "scenes.json"
            )

            if character_bible:
                archive.write(
                    WORKER_DIR / "character_bible.json",
                    "character_bible.json"
                )

            archive.write(
                worker_config,
                "worker_config.json"
            )

        with zipfile.ZipFile(
            temporary_zip,
            "r"
        ) as archive:

            names = set(
                archive.namelist()
            )

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

            # Verify the queue can be read back.
            archived_queue = json.loads(
                archive.read(
                    "visual_generation_queue.json"
                ).decode("utf-8")
            )

            if not isinstance(
                archived_queue.get("jobs"),
                list
            ):
                raise RuntimeError(
                    "Worker ZIP queue jobs must be a list."
                )

            if len(archived_queue["jobs"]) != expected:
                raise RuntimeError(
                    "Worker ZIP scene count mismatch."
                )

        os.replace(
            temporary_zip,
            QUEUE_ZIP
        )

    finally:
        if temporary_zip.exists():
            temporary_zip.unlink()

    # --------------------------------------------------------
    # LOCAL VISUAL MANIFEST
    # --------------------------------------------------------

    visual_manifest = {
        "version": 2,
        "status": (
            "completed"
            if pending == 0
            else "pending"
        ),
        "format": format_name,
        "image_size": image_size,
        "expected_total": expected,
        "completed": completed,
        "pending": pending,
        "generation_mode": "free_gpu_worker",
        "queue_json": str(QUEUE_JSON),
        "queue_zip": str(QUEUE_ZIP),
        "config_signature": signature,
        "jobs": {
            job["id"]: {
                "part": job["part"],
                "scene": job["scene"],
                "status": job["status"],
                "visual_path": job["output_path"],
                "prompt_hash": job["prompt_hash"],
                "config_signature": signature,
            }
            for job in jobs
        },
    }

    save_json(
        JOBS_FILE,
        visual_manifest
    )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("          FREE VISUAL QUEUE READY")
    print("=" * 70)

    print(f"Expected visuals : {expected}")
    print(f"Reusable images  : {completed}")
    print(f"Pending visuals  : {pending}")
    print(f"Config signature : {signature[:16]}")

    print()
    print(f"Queue JSON       : {QUEUE_JSON}")
    print(f"Queue ZIP        : {QUEUE_ZIP}")
    print(f"Local manifest   : {JOBS_FILE}")

    print()
    print("No placeholder images were created.")
    print("No image-generation API was called.")
    print("Pending jobs require the configured GPU worker.")

    print("=" * 70)

    return 0


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        sys.exit(
            build_worker_queue()
        )

    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)

    except Exception as exc:
        print()
        print(f"ERROR: {exc}")
        sys.exit(1)