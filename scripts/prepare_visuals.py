#!/usr/bin/env python3

import hashlib
import json
import os
import sys
from pathlib import Path

from input_config import (
    load_input_config,
    normalize_format,
    print_config_summary,
)

BASE = Path("output")
SCENES_FILE = BASE / "scenes" / "scenes.json"
CHARACTER_BIBLE_FILE = BASE / "story" / "character_bible.json"

VISUALS_DIR = BASE / "visuals"
JOBS_FILE = VISUALS_DIR / "visual_jobs.json"

MANIFEST_VERSION = 2
MIN_IMAGE_BYTES = 1000


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


def load_json(path, required=False):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        temporary.replace(path)

    finally:
        if temporary.exists():
            temporary.unlink()


def stable_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def scene_key(part, scene):
    return f"part_{part:02d}/scene_{scene:02d}"


def valid_visual(path):
    if not path.is_file():
        return False

    try:
        if path.stat().st_size <= MIN_IMAGE_BYTES:
            return False

        with path.open("rb") as handle:
            header = handle.read(16)

        is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = header.startswith(b"\xff\xd8\xff")
        is_webp = (
            len(header) >= 12
            and header[:4] == b"RIFF"
            and header[8:12] == b"WEBP"
        )

        return is_png or is_jpeg or is_webp

    except OSError:
        return False


def validate_scene_list(scenes, parts, scenes_per_part):
    if not isinstance(scenes, list):
        raise RuntimeError("scenes.json must contain a 'scenes' list.")

    expected = parts * scenes_per_part

    if len(scenes) != expected:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected}, got {len(scenes)}."
        )

    seen = set()
    normalized = []

    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Scene #{index} is not a JSON object.")

        default_part = ((index - 1) // scenes_per_part) + 1
        default_scene = ((index - 1) % scenes_per_part) + 1

        try:
            part = int(scene.get("part", default_part))
            scene_number = int(scene.get("scene", default_scene))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Scene #{index} has invalid part/scene numbers."
            ) from exc

        if not 1 <= part <= parts:
            raise RuntimeError(
                f"Scene #{index} has part {part}; allowed range is 1-{parts}."
            )

        if not 1 <= scene_number <= scenes_per_part:
            raise RuntimeError(
                f"Scene #{index} has scene {scene_number}; "
                f"allowed range is 1-{scenes_per_part}."
            )

        key = scene_key(part, scene_number)

        if key in seen:
            raise RuntimeError(f"Duplicate scene identifier: {key}")

        seen.add(key)

        visual_prompt = str(
            scene.get("visual_prompt")
            or scene.get("visual")
            or scene.get("description")
            or ""
        ).strip()

        if not visual_prompt:
            raise RuntimeError(
                f"{key} has no visual_prompt, visual, or description."
            )

        normalized_scene = dict(scene)
        normalized_scene["part"] = part
        normalized_scene["scene"] = scene_number
        normalized_scene["visual_prompt"] = visual_prompt

        normalized.append((index, key, normalized_scene))

    expected_keys = {
        scene_key(part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    missing = sorted(expected_keys - seen)

    if missing:
        raise RuntimeError(
            "Missing scene identifiers: " + ", ".join(missing[:20])
        )

    return sorted(normalized, key=lambda item: (item[2]["part"], item[2]["scene"]))


def main():
    print("=" * 60)
    print("          PREPARING VISUAL GENERATION")
    print("=" * 60)

    config = load_input_config()
    if not isinstance(config, dict):
        raise RuntimeError("Input configuration did not return an object.")

    print_config_summary(config)

    format_name = normalize_format(config)
    if format_name not in {"short", "full"}:
        raise RuntimeError(f"Unsupported output format: {format_name!r}")

    parts = cfg_int(config, "PARTS", 1)
    scenes_per_part = cfg_int(config, "SCENES", 1)

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must both be at least 1.")

    character_bible_enabled = cfg_bool(
        config,
        "CHARACTER_BIBLE",
        True,
    )
    resume_enabled = cfg_bool(config, "RESUME_ENABLED", True)
    skip_completed = cfg_bool(config, "SKIP_COMPLETED_SCENES", True)

    print()
    print(f"FORMAT                : {format_name}")
    print(f"PARTS                 : {parts}")
    print(f"SCENES PER PART       : {scenes_per_part}")
    print(f"CHARACTER_BIBLE       : {character_bible_enabled}")
    print(f"RESUME_ENABLED        : {resume_enabled}")
    print(f"SKIP_COMPLETED_SCENES : {skip_completed}")

    scenes_data = load_json(SCENES_FILE, required=True)

    if not isinstance(scenes_data, dict):
        raise RuntimeError("scenes.json must be a JSON object.")

    scenes = validate_scene_list(
        scenes_data.get("scenes"),
        parts,
        scenes_per_part,
    )

    expected_total = parts * scenes_per_part

    character_bible = {}
    if character_bible_enabled:
        character_bible = load_json(
            CHARACTER_BIBLE_FILE,
            required=True,
        )

        if not isinstance(character_bible, dict):
            raise RuntimeError("character_bible.json must be a JSON object.")

    old_manifest = {}
    if resume_enabled and JOBS_FILE.exists():
        old_manifest = load_json(JOBS_FILE)

        if not isinstance(old_manifest, dict):
            raise RuntimeError(
                f"Existing visual manifest is invalid: {JOBS_FILE}"
            )

    existing_jobs = old_manifest.get("jobs", {})
    if not isinstance(existing_jobs, dict):
        existing_jobs = {}

    VISUALS_DIR.mkdir(parents=True, exist_ok=True)

    character_signature = stable_hash(character_bible)
    jobs = {}
    completed_count = 0
    pending_count = 0

    visual_settings = {
        "format": format_name,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "visual_style": config.get("VISUAL_STYLE", "cinematic_realistic"),
        "realism": config.get("REALISM", "high"),
        "camera_style": config.get("CAMERA_STYLE", "cinematic"),
        "lighting": config.get("LIGHTING", "cinematic"),
        "mood": config.get("MOOD", "dramatic"),
        "quality": config.get("QUALITY", "high"),
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
        "character_bible_enabled": character_bible_enabled,
        "character_bible_signature": character_signature,
    }

    config_signature = stable_hash(visual_settings)

    for index, key, scene in scenes:
        part = scene["part"]
        scene_number = scene["scene"]

        visual_path = (
            VISUALS_DIR
            / f"part_{part:02d}"
            / f"scene_{scene_number:02d}.png"
        )

        old_job = existing_jobs.get(key, {})
        if not isinstance(old_job, dict):
            old_job = {}

        scene_signature = stable_hash({
            "scene": scene,
            "settings": visual_settings,
        })

        physical_exists = valid_visual(visual_path)

        if skip_completed and physical_exists:
            status = "completed"
            completed_count += 1
        else:
            status = "pending"
            pending_count += 1

        jobs[key] = {
            "part": part,
            "scene": scene_number,
            "scene_index": index,
            "status": status,
            "visual_path": str(visual_path),
            "fingerprint": scene_signature,
            "config_signature": config_signature,
            "file_exists": physical_exists,
            "previous_status": old_job.get("status"),
        }

        if physical_exists and not skip_completed:
            print(f"REGENERATE: {key} (resume skipping disabled)")
        elif status == "completed":
            print(f"REUSE     : {key}")
        else:
            print(f"PENDING   : {key}")

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "completed" if pending_count == 0 else "pending",
        "format": format_name,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "expected_total": expected_total,
        "completed": completed_count,
        "pending": pending_count,
        "resume_enabled": resume_enabled,
        "skip_completed_scenes": skip_completed,
        "character_bible_enabled": character_bible_enabled,
        "character_bible_path": (
            str(CHARACTER_BIBLE_FILE)
            if character_bible_enabled
            else None
        ),
        "character_bible_signature": character_signature,
        "config_signature": config_signature,
        "jobs": jobs,
    }

    save_json(JOBS_FILE, manifest)

    print()
    print("=" * 60)
    print("          VISUAL PREPARATION COMPLETE")
    print("=" * 60)
    print(f"Expected visuals : {expected_total}")
    print(f"Completed        : {completed_count}")
    print(f"Pending          : {pending_count}")
    print(f"Manifest         : {JOBS_FILE}")

    if pending_count:
        print(
            f"{pending_count} visual(s) are pending for "
            "generate_visuals.py."
        )
    else:
        print("All required visual image files are present and valid.")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)