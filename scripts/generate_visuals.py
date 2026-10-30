#!/usr/bin/env python3
"""Build a validated, resumable visual queue for Katha Lok AI."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    normalize_format,
    get_parts,
    get_scenes,
)

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

VERSION = "visual-queue-v6-provider-hash-compatible"
MIN_IMAGE_BYTES = 1000

# Must remain identical to free_image_generator.py.
QUALITY = (
    "Photorealistic live-action cinematic photography. Realistic Indian "
    "people, locations, clothing and architecture. Natural skin texture, "
    "believable anatomy, realistic eyes, natural lighting and shadows. "
    "Maintain recurring character identity, age, hairstyle and clothing. "
    "No cartoon, anime, illustration, CGI, text, logo or watermark."
)


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
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
    if value in {"true", "1", "yes", "y", "on", "enabled"}:
        return True
    if value in {"false", "0", "no", "n", "off", "disabled"}:
        return False
    return default


def get_image_size(format_name):
    if format_name == "short":
        return "720x1280"
    if format_name == "full":
        return "1280x720"
    raise RuntimeError(f"Unsupported format: {format_name}")


def scene_key(part, scene):
    return f"part_{part:02d}_scene_{scene:02d}"


def image_is_valid(path, expected_size=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        return False

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                return False
            if image.width < 64 or image.height < 64:
                return False
            if expected_size:
                width, height = map(int, expected_size.split("x", 1))
                if image.size != (width, height):
                    return False
        return True
    except Exception:
        return False


def read_image_rows():
    rows = {}

    for path in (GENERATION_MANIFEST, IMAGE_MANIFEST):
        document = read_json(path, {})
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
                number = int(row.get("global_scene", row.get("scene")))
            except (TypeError, ValueError):
                continue
            if number > 0:
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
        if isinstance(item, dict) and str(item.get("id", "")) in wanted
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
        scene.get("visual_prompt", scene.get("visual", scene.get("description", "")))
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


def make_config_signature(config, format_name, image_size):
    keys = (
        "VISUAL_STYLE", "REALISM", "CAMERA_STYLE", "LIGHTING",
        "MOOD", "QUALITY", "CHARACTER_CONSISTENCY",
        "WORLD_CONSISTENCY", "SCENE_CONTINUITY",
        "AVOID_CARTOON_LOOK", "AVOID_NEON", "AVOID_GLITCH_EFFECTS",
    )

    data = {
        "format": format_name,
        "image_size": image_size,
        "version": VERSION,
    }

    for key in keys:
        data[key.lower()] = config.get(key)

    return sha256_text(json.dumps(data, ensure_ascii=False, sort_keys=True))


def validate_scenes(scenes, parts_count, scenes_per_part):
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("scenes.json contains no scenes")

    expected_total = parts_count * scenes_per_part
    if len(scenes) != expected_total:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected_total}, found {len(scenes)}"
        )

    result = []
    seen = set()

    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Scene {index} is not an object")

        try:
            part = int(scene["part"])
            local = int(scene["scene"])
            global_number = int(scene.get("global_scene_number", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Scene {index} has invalid numbering") from exc

        expected_part = ((index - 1) // scenes_per_part) + 1
        expected_local = ((index - 1) % scenes_per_part) + 1

        if (part, local, global_number) != (expected_part, expected_local, index):
            raise RuntimeError(
                f"Scene mapping mismatch at row {index}: "
                f"expected {expected_part}/{expected_local}/{index}, "
                f"got {part}/{local}/{global_number}"
            )

        key = scene_key(part, local)
        if key in seen:
            raise RuntimeError(f"Duplicate scene identifier: {key}")
        if scene.get("status") != "completed":
            raise RuntimeError(f"Scene is not completed: {key}")
        if not str(scene.get("narration", "")).strip():
            raise RuntimeError(f"Scene narration is missing: {key}")
        if not str(scene.get("visual_prompt", "")).strip():
            raise RuntimeError(f"Scene visual prompt is missing: {key}")

        try:
            duration = float(scene.get("duration", 5))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid duration: {key}") from exc

        if not 0.5 <= duration <= 3600:
            raise RuntimeError(f"Scene duration out of range: {key}")

        seen.add(key)
        result.append({
            "part": part,
            "scene": local,
            "global_scene": index,
            "duration": duration,
            "source": scene,
        })

    return result


def provider_prompt_hash(prompt, negative_prompt):
    # Exact same prompt construction as free_image_generator.make_prompt().
    provider_prompt = (
        f"{prompt}\n\n"
        f"Quality requirements: {QUALITY}\n\n"
        f"Avoid: {negative_prompt}"
    )
    return sha256_text(provider_prompt)


def existing_image_is_reusable(path, old, prompt_hash, image_size):
    if not isinstance(old, dict):
        return False
    if not image_is_valid(path, image_size):
        return False

    old_prompt = old.get("prompt_sha256") or old.get("prompt_hash")
    if old_prompt != prompt_hash:
        return False

    expected_hash = old.get("image_sha256")
    if expected_hash:
        try:
            if sha256_file(path) != expected_hash:
                return False
        except OSError:
            return False

    return True


def make_job(
    item,
    config,
    character_bible,
    negative_prompt,
    image_size,
    signature,
    old_rows,
):
    part = item["part"]
    local = item["scene"]
    global_scene = item["global_scene"]
    source = item["source"]

    key = scene_key(part, local)
    output_path = VISUALS_DIR / f"part_{part:02d}" / f"scene_{local:02d}.png"

    prompt = build_visual_prompt(config, source, character_bible)

    # Hash is now aligned with free_image_generator.py.
    prompt_hash = provider_prompt_hash(prompt, negative_prompt)

    old = old_rows.get(global_scene, {})
    reusable = (
        cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
        and existing_image_is_reusable(
            output_path, old, prompt_hash, image_size
        )
    )

    image_hash = sha256_file(output_path) if reusable else None
    relative_path = output_path.relative_to(ROOT).as_posix()

    return {
        "id": key,
        "scene_id": key,
        "part": part,
        "scene": local,
        "local_scene": local,
        "global_scene": global_scene,
        "global_scene_number": global_scene,
        "scene_index": global_scene,
        "status": "completed" if reusable else "pending",
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "image_size": image_size,
        "format": normalize_format(config),
        "output_path": relative_path,
        "image_path": relative_path,
        "image": relative_path,
        "scene_duration": item["duration"],
        "duration": item["duration"],
        "prompt_hash": prompt_hash,
        "prompt_sha256": prompt_hash,
        "image_sha256": image_hash,
        "config_signature": signature,
        "source_scene": source,
    }


def verify_zip(zip_path, expected, format_name, image_size):
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
            raise RuntimeError("Worker ZIP missing: " + ", ".join(sorted(missing)))

        queue = json.loads(
            archive.read("visual_generation_queue.json").decode("utf-8")
        )
        jobs = queue.get("jobs")

        if not isinstance(jobs, list) or len(jobs) != expected:
            raise RuntimeError("Worker ZIP queue count mismatch")

        numbers = [int(row.get("global_scene", 0)) for row in jobs]
        if numbers != list(range(1, expected + 1)):
            raise RuntimeError("Worker ZIP global scene numbering invalid")

        if queue.get("format") != format_name:
            raise RuntimeError("Worker ZIP format mismatch")
        if queue.get("image_size") != image_size:
            raise RuntimeError("Worker ZIP image dimensions mismatch")

        for row in jobs:
            if row.get("image_size") != image_size:
                raise RuntimeError("Worker ZIP contains mixed image dimensions")


def build_worker_queue():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    format_name = normalize_format(config)
    parts = get_parts(config)
    scenes_per_part = get_scenes(config)
    expected = parts * scenes_per_part
    image_size = get_image_size(format_name)

    scenes_data = read_json(SCENES_FILE)
    if not isinstance(scenes_data, dict):
        raise RuntimeError("scenes.json must be a JSON object")
    if scenes_data.get("status") != "completed":
        raise RuntimeError("scenes.json is not marked completed")
    if scenes_data.get("format") != format_name:
        raise RuntimeError("scenes.json format does not match Input/topic.txt")
    if int(scenes_data.get("total_scenes", -1)) != expected:
        raise RuntimeError("scenes.json total_scenes does not match input")

    scenes = validate_scenes(
        scenes_data.get("scenes", []), parts, scenes_per_part
    )

    character_bible = {}
    if cfg_bool(config, "CHARACTER_BIBLE", True):
        if CHARACTER_BIBLE_FILE.is_file():
            character_bible = read_json(CHARACTER_BIBLE_FILE, {})
            if not isinstance(character_bible, dict):
                raise RuntimeError("character_bible.json is invalid")
            if character_bible.get("status") not in {"completed", "disabled"}:
                raise RuntimeError("Character Bible status is not completed")
        else:
            log("Character Bible file missing; continuing without character references.")

    old_rows = read_image_rows()
    signature = make_config_signature(config, format_name, image_size)
    negative_prompt = build_negative_prompt(config)

    jobs = [
        make_job(
            item,
            config,
            character_bible,
            negative_prompt,
            image_size,
            signature,
            old_rows,
        )
        for item in scenes
    ]

    completed = sum(row["status"] == "completed" for row in jobs)
    pending = len(jobs) - completed

    queue = {
        "queue_version": 5,
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
        "source_fingerprint": scenes_data.get("source_fingerprint"),
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
        "queue_version": 5,
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
        "source_fingerprint": scenes_data.get("source_fingerprint"),
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

        verify_zip(temporary_zip, expected, format_name, image_size)
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
        "source_fingerprint": scenes_data.get("source_fingerprint"),
        "jobs": jobs,
    }

    save_json(JOBS_FILE, visual_manifest)

    log("VISUAL QUEUE READY")
    log("Format:", format_name)
    log("Image dimensions:", image_size)
    log("Parts:", parts)
    log("Scenes per part:", scenes_per_part)
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