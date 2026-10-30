
#!/usr/bin/env python3
"""Build a resumable visual queue compatible with image and depth workers."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "output"

SCENES_FILE = BASE / "scenes" / "scenes.json"
CHARACTER_BIBLE_FILE = BASE / "story" / "character_bible.json"

VISUALS_DIR = BASE / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"
IMAGE_MANIFEST = VISUALS_DIR / "image_manifest.json"
GENERATION_MANIFEST = VISUALS_DIR / "image_generation_manifest.json"

WORKER_DIR = BASE / "visual_worker"
QUEUE_JSON = WORKER_DIR / "visual_generation_queue.json"
QUEUE_ZIP = BASE / "i2v" / "visual_generation_queue.zip"

MIN_IMAGE_BYTES = 1000
VERSION = "visual-queue-v4-image-manifest-resume"


def log(*args):
    print(*args, flush=True)


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
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing path for {label}")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label} escapes project directory")
    return path


def normalize_format(config):
    from input_config import normalize_format as normalize
    value = str(normalize(config)).strip().lower()
    if value not in {"short", "full"}:
        raise RuntimeError(f"Unsupported video format: {value}")
    return value


def get_image_size(format_name):
    return "768x1024" if format_name == "short" else "1024x576"


def scene_key(part, scene):
    return f"part_{part:02d}/scene_{scene:02d}"


def image_is_valid(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        return False
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return (
                image.format in {"PNG", "JPEG", "WEBP"}
                and image.width >= 64
                and image.height >= 64
            )
    except Exception:
        return False


def read_image_rows():
    rows = {}
    for path in (GENERATION_MANIFEST, IMAGE_MANIFEST):
        document = load_json(path, {})
        if isinstance(document, dict):
            source = document.get("scenes", [])
        elif isinstance(document, list):
            source = document
        else:
            source = []

        if not isinstance(source, list):
            continue

        for row in source:
            if not isinstance(row, dict):
                continue
            try:
                number = int(
                    row.get("global_scene", row.get("scene"))
                )
            except (TypeError, ValueError):
                continue
            if number > 0:
                # Generation manifest is read first; image manifest
                # supplies compatibility fields when available.
                rows.setdefault(number, {}).update(row)

    return rows


def build_negative_prompt(config):
    negative = []

    if cfg_bool(config, "AVOID_CARTOON_LOOK", True):
        negative.extend([
            "cartoon", "comic", "anime", "manga",
            "illustration", "drawing", "painting", "2D art",
        ])

    if cfg_bool(config, "AVOID_NEON", True):
        negative.append("neon colors")

    if cfg_bool(config, "AVOID_GLITCH_EFFECTS", True):
        negative.extend(["glitch", "digital distortion", "flicker"])

    negative.extend([
        "CGI look", "3D render", "game graphics", "plastic skin",
        "artificial face", "deformed anatomy", "extra fingers",
        "extra limbs", "duplicate person", "watermark", "logo",
        "text", "low quality", "blurry face",
    ])
    return ", ".join(negative)


def get_character_context(scene, character_bible):
    if not isinstance(character_bible, dict):
        return ""

    characters = scene.get("characters", scene.get("character_ids", []))
    if not isinstance(characters, list):
        return ""

    wanted = {str(value) for value in characters}
    source = character_bible.get("characters", [])
    if not isinstance(source, list):
        return ""

    selected = [
        item for item in source
        if isinstance(item, dict)
        and str(item.get("id", "")) in wanted
    ]
    return json.dumps(selected, ensure_ascii=False) if selected else ""


def build_visual_prompt(config, scene, character_bible):
    values = [
        ("Visual style", cfg_text(config, "VISUAL_STYLE", "cinematic_realistic")),
        ("Realism", cfg_text(config, "REALISM", "high")),
        ("Camera style", cfg_text(config, "CAMERA_STYLE", "cinematic")),
        ("Lighting", cfg_text(config, "LIGHTING", "cinematic")),
        ("Mood", cfg_text(config, "MOOD", "dramatic")),
        ("Quality", cfg_text(config, "QUALITY", "high")),
    ]

    prompt = [
        "Photorealistic live-action cinematic photography.",
        "Real human beings, real-world locations and physical environments.",
        "Natural skin texture, realistic anatomy, eyes, lighting and shadows.",
        "Maintain recurring character identity, age, hairstyle and clothing.",
        "Maintain location, architecture, time period and scene continuity.",
        "Cinematic composition and natural camera perspective.",
    ]

    prompt.extend(f"{key}: {value}." for key, value in values)

    scene_prompt = str(
        scene.get(
            "visual_prompt",
            scene.get("visual", scene.get("description", "")),
        )
    ).strip()
    action = str(scene.get("action", "")).strip()
    world = str(scene.get("world_context", scene.get("world", ""))).strip()
    character_context = get_character_context(scene, character_bible)

    if scene_prompt:
        prompt.append("Scene description: " + scene_prompt)
    if action:
        prompt.append("Scene action: " + action)
    if world:
        prompt.append("World context: " + world)
    if character_context:
        prompt.append("Character Bible reference: " + character_context)

    if cfg_bool(config, "CHARACTER_CONSISTENCY", True):
        prompt.append("Preserve exact recurring character appearance.")
    if cfg_bool(config, "WORLD_CONSISTENCY", True):
        prompt.append("Preserve world and environmental continuity.")
    if cfg_bool(config, "SCENE_CONTINUITY", True):
        prompt.append("Maintain continuity with adjacent scenes.")

    return " ".join(prompt)


def make_config_signature(config, format_name):
    keys = (
        "VISUAL_STYLE", "REALISM", "CAMERA_STYLE", "LIGHTING",
        "MOOD", "QUALITY", "CHARACTER_CONSISTENCY",
        "WORLD_CONSISTENCY", "SCENE_CONTINUITY",
        "AVOID_CARTOON_LOOK", "AVOID_NEON", "AVOID_GLITCH_EFFECTS",
    )
    data = {"format": format_name}
    for key in keys:
        data[key.lower()] = config.get(key)
    return sha256_text(json.dumps(data, ensure_ascii=False, sort_keys=True))


def validate_scenes(scenes):
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("scenes.json contains no scenes")

    result = []
    seen = set()

    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Scene {index} is not an object")
        try:
            part = int(scene["part"])
            local = int(scene["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Scene {index} has invalid part/scene numbers"
            ) from exc

        if part < 1 or local < 1:
            raise RuntimeError("Part and scene numbers must be positive")

        key = scene_key(part, local)
        if key in seen:
            raise RuntimeError(f"Duplicate scene identifier: {key}")
        seen.add(key)
        result.append({"part": part, "scene": local, "source": scene})

    result.sort(key=lambda item: (item["part"], item["scene"]))
    return result


def existing_image_is_reusable(path, old, prompt_hash, signature):
    if not image_is_valid(path) or not isinstance(old, dict):
        return False

    old_prompt = old.get("prompt_sha256") or old.get("prompt_hash")
    if old_prompt != prompt_hash:
        return False

    old_signature = old.get("config_signature")
    if old_signature and old_signature != signature:
        return False

    expected_hash = old.get("image_sha256")
    if expected_hash:
        try:
            if sha256_file(path) != expected_hash:
                return False
        except OSError:
            return False

    return True


def make_job(item, index, config, character_bible, negative_prompt,
             image_size, signature, old_rows):
    part = item["part"]
    local = item["scene"]
    source = item["source"]
    global_scene = index

    key = scene_key(part, local)
    output_path = (
        VISUALS_DIR / f"part_{part:02d}" / f"scene_{local:02d}.png"
    )
    prompt = build_visual_prompt(config, source, character_bible)
    prompt_hash = sha256_text(prompt + "\n" + negative_prompt)

    old = old_rows.get(global_scene, {})
    reusable = (
        cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
        and existing_image_is_reusable(
            output_path, old, prompt_hash, signature
        )
    )

    image_hash = sha256_file(output_path) if reusable else None
    status = "completed" if reusable else "pending"

    return {
        "id": key,
        "scene_id": key,
        "part": part,
        "scene": local,
        "local_scene": local,
        "global_scene": global_scene,
        "global_scene_number": global_scene,
        "scene_index": global_scene,
        "status": status,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "image_size": image_size,
        "format": normalize_format(config),
        "output_path": output_path.relative_to(ROOT).as_posix(),
        "image_path": output_path.relative_to(ROOT).as_posix(),
        "image": output_path.relative_to(ROOT).as_posix(),
        "scene_duration": source.get("duration"),
        "duration": source.get("duration"),
        "prompt_hash": prompt_hash,
        "prompt_sha256": prompt_hash,
        "image_sha256": image_hash,
        "config_signature": signature,
        "source_scene": source,
    }


def verify_zip(zip_path, expected):
    with zipfile.ZipFile(zip_path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"Corrupt ZIP entry: {bad}")

        names = set(archive.namelist())
        required = {
            "visual_generation_queue.json",
            "scenes.json",
            "worker_config.json",
        }
        missing = required - names
        if missing:
            raise RuntimeError(
                "Worker ZIP missing: " + ", ".join(sorted(missing))
            )

        queue = json.loads(
            archive.read("visual_generation_queue.json").decode("utf-8")
        )
        jobs = queue.get("jobs")
        if not isinstance(jobs, list) or len(jobs) != expected:
            raise RuntimeError("Worker ZIP queue count mismatch")

        numbers = [int(row.get("global_scene", 0)) for row in jobs]
        if numbers != list(range(1, expected + 1)):
            raise RuntimeError("Worker ZIP global scene numbering invalid")


def build_worker_queue():
    from input_config import load_input_config

    config = load_input_config()
    format_name = normalize_format(config)

    parts = cfg_int(config, "PARTS", 1)
    scenes_per_part = cfg_int(config, "SCENES", 1)
    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    expected = parts * scenes_per_part
    image_size = get_image_size(format_name)

    scenes_data = load_json(SCENES_FILE)
    if not isinstance(scenes_data, dict):
        raise RuntimeError("scenes.json must be a JSON object")
    if scenes_data.get("status") != "completed":
        raise RuntimeError("scenes.json is not marked completed")

    scenes = validate_scenes(scenes_data.get("scenes", []))
    if len(scenes) != expected:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected}, found {len(scenes)}"
        )

    character_bible = {}
    if cfg_bool(config, "CHARACTER_BIBLE", True):
        if not CHARACTER_BIBLE_FILE.is_file():
            raise RuntimeError("Character Bible is enabled but missing")
        character_bible = load_json(CHARACTER_BIBLE_FILE, {})
        if not isinstance(character_bible, dict):
            raise RuntimeError("character_bible.json is invalid")

    old_rows = read_image_rows()
    signature = make_config_signature(config, format_name)
    negative_prompt = build_negative_prompt(config)

    jobs = [
        make_job(
            item, index, config, character_bible,
            negative_prompt, image_size, signature, old_rows,
        )
        for index, item in enumerate(scenes, 1)
    ]

    completed = sum(row["status"] == "completed" for row in jobs)
    pending = len(jobs) - completed

    queue = {
        "queue_version": 3,
        "generator_version": VERSION,
        "status": "completed" if pending == 0 else "pending",
        "worker_type": "free_gpu_visual_worker",
        "generation_mode": "text_to_image",
        "format": format_name,
        "image_size": image_size,
        "expected_total": expected,
        "completed": completed,
        "pending": pending,
        "config_signature": signature,
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
    save_json(worker_config, {
        "queue_version": 3,
        "format": format_name,
        "image_size": image_size,
        "generation_mode": "text_to_image",
        "visual_style": cfg_text(config, "VISUAL_STYLE", "cinematic_realistic"),
        "realism": cfg_text(config, "REALISM", "high"),
        "camera_style": cfg_text(config, "CAMERA_STYLE", "cinematic"),
        "lighting": cfg_text(config, "LIGHTING", "cinematic"),
        "mood": cfg_text(config, "MOOD", "dramatic"),
        "negative_prompt": negative_prompt,
        "config_signature": signature,
    })

    QUEUE_ZIP.parent.mkdir(parents=True, exist_ok=True)
    temporary_zip = QUEUE_ZIP.with_suffix(".zip.tmp")
    temporary_zip.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(
            temporary_zip, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.write(QUEUE_JSON, "visual_generation_queue.json")
            archive.write(worker_scenes, "scenes.json")
            if character_bible:
                archive.write(
                    WORKER_DIR / "character_bible.json",
                    "character_bible.json",
                )
            archive.write(worker_config, "worker_config.json")

        verify_zip(temporary_zip, expected)
        temporary_zip.replace(QUEUE_ZIP)
    finally:
        temporary_zip.unlink(missing_ok=True)

    visual_manifest = {
        "version": VERSION,
        "status": "completed" if pending == 0 else "pending",
        "format": format_name,
        "image_size": image_size,
        "expected_total": expected,
        "completed": completed,
        "pending": pending,
        "generation_mode": "free_gpu_worker",
        "queue_json": QUEUE_JSON.relative_to(ROOT).as_posix(),
        "queue_zip": QUEUE_ZIP.relative_to(ROOT).as_posix(),
        "config_signature": signature,
        "jobs": jobs,
    }
    save_json(JOBS_FILE, visual_manifest)

    log("VISUAL QUEUE READY")
    log("Format:", format_name)
    log("Expected images:", expected)
    log("Verified reusable images:", completed)
    log("Pending images:", pending)
    log("Queue ZIP:", QUEUE_ZIP.relative_to(ROOT))
    log("Manifest:", JOBS_FILE.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(build_worker_queue())
    except KeyboardInterrupt:
        log("Interrupted")
        sys.exit(130)
    except Exception as exc:
        log("VISUAL QUEUE FAILED:", exc)
        sys.exit(1)
