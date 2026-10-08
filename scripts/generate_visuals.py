#!/usr/bin/env python3

import json
import hashlib
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
# HELPERS
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

    text = str(value).strip().lower()

    if text in {"true", "1", "yes", "y", "on"}:
        return True

    if text in {"false", "0", "no", "n", "off"}:
        return False

    return default


def cfg_int(config, key, default=0):
    value = config.get(key, default)

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_json(path, default=None):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"WARNING: Failed to read {path}: {exc}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    tmp.replace(path)


def scene_key(part, scene):
    return f"part_{part:02d}/scene_{scene:02d}"


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
        scene.get("character_ids", []),
    )

    if not characters:
        return ""

    bible_characters = character_bible.get(
        "characters",
        [],
    )

    if not isinstance(bible_characters, list):
        return ""

    wanted = {str(x) for x in characters}

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
        separators=(",", ":"),
    )


# ============================================================
# VISUAL PROMPT
# ============================================================


def build_visual_prompt(
    config,
    scene,
    character_bible,
):
    visual_style = cfg_text(
        config,
        "VISUAL_STYLE",
        "cinematic_realistic",
    )

    realism = cfg_text(
        config,
        "REALISM",
        "high",
    )

    camera_style = cfg_text(
        config,
        "CAMERA_STYLE",
        "cinematic",
    )

    lighting = cfg_text(
        config,
        "LIGHTING",
        "cinematic",
    )

    mood = cfg_text(
        config,
        "MOOD",
        "dramatic",
    )

    quality = cfg_text(
        config,
        "QUALITY",
        "high",
    )

    scene_prompt = str(
        scene.get(
            "visual_prompt",
            scene.get(
                "visual",
                scene.get(
                    "description",
                    "",
                ),
            ),
        )
    ).strip()

    world_context = str(
        scene.get(
            "world_context",
            scene.get(
                "world",
                "",
            ),
        )
    ).strip()

    action = str(
        scene.get(
            "action",
            "",
        )
    ).strip()

    character_context = get_character_context(
        scene,
        character_bible,
    )

    prompt_parts = [
        "Photorealistic live-action cinematic scene.",
        "Real human beings.",
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
        "Maintain identity and clothing continuity.",
        "Maintain environment and location continuity.",
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
        "visual_style": cfg_text(
            config,
            "VISUAL_STYLE",
            "",
        ),
        "realism": cfg_text(
            config,
            "REALISM",
            "",
        ),
        "camera_style": cfg_text(
            config,
            "CAMERA_STYLE",
            "",
        ),
        "lighting": cfg_text(
            config,
            "LIGHTING",
            "",
        ),
        "mood": cfg_text(
            config,
            "MOOD",
            "",
        ),
        "quality": cfg_text(
            config,
            "QUALITY",
            "",
        ),
        "character_consistency": cfg_bool(
            config,
            "CHARACTER_CONSISTENCY",
            True,
        ),
        "world_consistency": cfg_bool(
            config,
            "WORLD_CONSISTENCY",
            True,
        ),
        "scene_continuity": cfg_bool(
            config,
            "SCENE_CONTINUITY",
            True,
        ),
        "avoid_cartoon": cfg_bool(
            config,
            "AVOID_CARTOON_LOOK",
            True,
        ),
        "avoid_neon": cfg_bool(
            config,
            "AVOID_NEON",
            True,
        ),
        "avoid_glitch": cfg_bool(
            config,
            "AVOID_GLITCH_EFFECTS",
            True,
        ),
    }

    raw = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# ============================================================
# BUILD WORKER QUEUE
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
        1,
    )

    scenes_per_part = cfg_int(
        config,
        "SCENES",
        1,
    )

    image_size = get_image_size(
        format_name
    )

    print(f"FORMAT          : {format_name}")
    print(f"IMAGE SIZE      : {image_size}")
    print(f"PARTS           : {parts}")
    print(f"SCENES/PART     : {scenes_per_part}")

    # --------------------------------------------------------
    # SCENES
    # --------------------------------------------------------

    if not SCENES_FILE.exists():
        raise RuntimeError(
            f"Missing scenes file: {SCENES_FILE}"
        )

    scenes_data = load_json(
        SCENES_FILE,
        {},
    )

    if not isinstance(scenes_data, dict):
        raise RuntimeError(
            "scenes.json is not a JSON object."
        )

    scenes = scenes_data.get(
        "scenes",
        [],
    )

    if not isinstance(scenes, list):
        raise RuntimeError(
            "scenes.json 'scenes' must be a list."
        )

    expected = parts * scenes_per_part

    if len(scenes) != expected:
        raise RuntimeError(
            f"Scene count mismatch: "
            f"expected {expected}, got {len(scenes)}"
        )

    # --------------------------------------------------------
    # CHARACTER BIBLE
    # --------------------------------------------------------

    character_bible = {}

    if cfg_bool(
        config,
        "CHARACTER_BIBLE",
        True,
    ):
        if not CHARACTER_BIBLE_FILE.exists():
            raise RuntimeError(
                "Character Bible is enabled but missing: "
                f"{CHARACTER_BIBLE_FILE}"
            )

        character_bible = load_json(
            CHARACTER_BIBLE_FILE,
            {},
        )

        if not isinstance(
            character_bible,
            dict,
        ):
            raise RuntimeError(
                "character_bible.json is invalid."
            )

    # --------------------------------------------------------
    # EXISTING MANIFEST
    # --------------------------------------------------------

    existing = load_json(
        JOBS_FILE,
        {},
    )

    existing_jobs = {}

    if isinstance(existing, dict):
        old_jobs = existing.get(
            "jobs",
            {},
        )

        if isinstance(old_jobs, dict):
            existing_jobs = old_jobs

    # --------------------------------------------------------
    # BUILD JOBS
    # --------------------------------------------------------

    negative_prompt = build_negative_prompt(
        config
    )

    signature = make_config_signature(
        config,
        format_name,
    )

    jobs = []

    completed = 0
    pending = 0

    for index, scene in enumerate(
        scenes,
        start=1,
    ):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Scene {index} is invalid."
            )

        part = int(
            scene.get(
                "part",
                ((index - 1) // scenes_per_part) + 1,
            )
        )

        scene_number = int(
            scene.get(
                "scene",
                ((index - 1) % scenes_per_part) + 1,
            )
        )

        key = scene_key(
            part,
            scene_number,
        )

        output_path = (
            VISUALS_DIR
            / f"part_{part:02d}"
            / f"scene_{scene_number:02d}.png"
        )

        prompt = build_visual_prompt(
            config,
            scene,
            character_bible,
        )

        old_job = existing_jobs.get(
            key,
            {},
        )

        physical_exists = (
            output_path.exists()
            and output_path.is_file()
            and output_path.stat().st_size > 1000
        )

        if (
            cfg_bool(
                config,
                "SKIP_COMPLETED_SCENES",
                True,
            )
            and physical_exists
        ):
            status = "completed"
            completed += 1
        else:
            status = "pending"
            pending += 1

        jobs.append(
            {
                "id": key,
                "part": part,
                "scene": scene_number,
                "status": status,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "image_size": image_size,
                "format": format_name,
                "output_path": str(
                    output_path
                ),
                "scene_duration": scene.get(
                    "duration",
                    None,
                ),
                "scene_index": index,
                "source_scene": scene,
                "previous_job": old_job,
            }
        )

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

    save_json(
        QUEUE_JSON,
        queue,
    )

    # --------------------------------------------------------
    # COPY SOURCE FILES
    # --------------------------------------------------------

    WORKER_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    worker_scenes = (
        WORKER_DIR / "scenes.json"
    )

    shutil.copy2(
        SCENES_FILE,
        worker_scenes,
    )

    if CHARACTER_BIBLE_FILE.exists():
        shutil.copy2(
            CHARACTER_BIBLE_FILE,
            WORKER_DIR / "character_bible.json",
        )

    worker_config = (
        WORKER_DIR / "worker_config.json"
    )

    save_json(
        worker_config,
        {
            "format": format_name,
            "image_size": image_size,
            "generation_mode": "text_to_image",
            "visual_style": cfg_text(
                config,
                "VISUAL_STYLE",
                "cinematic_realistic",
            ),
            "realism": cfg_text(
                config,
                "REALISM",
                "high",
            ),
            "negative_prompt": negative_prompt,
        },
    )

    # --------------------------------------------------------
    # CREATE ZIP
    # --------------------------------------------------------

    QUEUE_ZIP.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with zipfile.ZipFile(
        QUEUE_ZIP,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as zf:

        zf.write(
            QUEUE_JSON,
            "visual_generation_queue.json",
        )

        zf.write(
            worker_scenes,
            "scenes.json",
        )

        if (
            WORKER_DIR
            / "character_bible.json"
        ).exists():
            zf.write(
                WORKER_DIR
                / "character_bible.json",
                "character_bible.json",
            )

        zf.write(
            worker_config,
            "worker_config.json",
        )

    # --------------------------------------------------------
    # VALIDATE ZIP
    # --------------------------------------------------------

    with zipfile.ZipFile(
        QUEUE_ZIP,
        "r",
    ) as zf:

        names = set(
            zf.namelist()
        )

        required = {
            "visual_generation_queue.json",
            "scenes.json",
            "worker_config.json",
        }

        missing = required - names

        if missing:
            raise RuntimeError(
                "Worker queue ZIP missing: "
                + ", ".join(
                    sorted(missing)
                )
            )

        bad = zf.testzip()

        if bad:
            raise RuntimeError(
                f"Corrupt ZIP entry: {bad}"
            )

    # --------------------------------------------------------
    # MANIFEST FOR LOCAL PIPELINE
    # --------------------------------------------------------

    visual_manifest = {
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
            }
            for job in jobs
        },
    }

    save_json(
        JOBS_FILE,
        visual_manifest,
    )

    # --------------------------------------------------------
    # FINAL REPORT
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("          FREE VISUAL QUEUE READY")
    print("=" * 70)

    print(f"Expected visuals : {expected}")
    print(f"Completed        : {completed}")
    print(f"Pending          : {pending}")

    print()
    print(
        "Cloudflare visual API: DISABLED"
    )

    print(
        "Generation mode     : FREE GPU WORKER"
    )

    print(
        f"Queue JSON          : {QUEUE_JSON}"
    )

    print(
        f"Queue ZIP           : {QUEUE_ZIP}"
    )

    print()
    print(
        "No fake placeholder images were created."
    )

    print(
        "Actual image generation must be performed "
        "by the free GPU worker."
    )

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