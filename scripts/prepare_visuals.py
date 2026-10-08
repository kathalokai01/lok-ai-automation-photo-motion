#!/usr/bin/env python3

import json
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


# ---------------------------------------------------------
# LOCAL CONFIG COMPATIBILITY HELPERS
# ---------------------------------------------------------

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

    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path, default=None):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"WARNING: Failed to read {path}: {e}")
        return default


def save_json(path: Path, data):
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


def valid_visual(path: Path) -> bool:
    if not path.exists():
        return False

    if not path.is_file():
        return False

    try:
        return path.stat().st_size > 1000
    except Exception:
        return False


def scene_key(part: int, scene: int) -> str:
    return f"part_{part:02d}/scene_{scene:02d}"


def main():
    print("=" * 60)
    print("          PREPARING VISUAL GENERATION")
    print("=" * 60)

    # ---------------------------------------------------------
    # INPUT CONFIG
    # ---------------------------------------------------------

    try:
        config = load_input_config()
    except Exception as e:
        print(f"ERROR: Failed to load Input configuration: {e}")
        return 1

    print_config_summary(config)

    format_name = normalize_format(config)

    parts = cfg_int(config, "PARTS", 1)
    scenes_per_part = cfg_int(config, "SCENES", 1)

    character_bible_enabled = cfg_bool(
        config,
        "CHARACTER_BIBLE",
        True,
    )

    resume_enabled = cfg_bool(
        config,
        "RESUME_ENABLED",
        True,
    )

    skip_completed = cfg_bool(
        config,
        "SKIP_COMPLETED_SCENES",
        True,
    )

    save_checkpoint = cfg_bool(
        config,
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
        True,
    )

    print()
    print(f"FORMAT                : {format_name}")
    print(f"PARTS                 : {parts}")
    print(f"SCENES PER PART       : {scenes_per_part}")
    print(f"CHARACTER_BIBLE       : {character_bible_enabled}")
    print(f"RESUME_ENABLED        : {resume_enabled}")
    print(f"SKIP_COMPLETED_SCENES : {skip_completed}")
    print(f"SAVE_CHECKPOINT       : {save_checkpoint}")

    # ---------------------------------------------------------
    # REQUIRED SCENES
    # ---------------------------------------------------------

    if not SCENES_FILE.exists():
        print()
        print(f"ERROR: Missing scenes file: {SCENES_FILE}")
        return 1

    scenes_data = load_json(SCENES_FILE)

    if not isinstance(scenes_data, dict):
        print("ERROR: scenes.json is not a JSON object.")
        return 1

    scenes = scenes_data.get("scenes", [])

    if not isinstance(scenes, list):
        print("ERROR: scenes.json 'scenes' must be a list.")
        return 1

    expected_total = parts * scenes_per_part

    print()
    print(f"Expected scenes : {expected_total}")
    print(f"Loaded scenes   : {len(scenes)}")

    if len(scenes) != expected_total:
        print(
            "ERROR: Scene count mismatch. "
            f"Expected {expected_total}, got {len(scenes)}."
        )
        return 1

    # ---------------------------------------------------------
    # CHARACTER BIBLE
    # ---------------------------------------------------------

    character_bible = {}

    if character_bible_enabled:
        if not CHARACTER_BIBLE_FILE.exists():
            print()
            print(
                "ERROR: CHARACTER_BIBLE=true but Character Bible "
                f"is missing:\n{CHARACTER_BIBLE_FILE}"
            )
            return 1

        character_bible = load_json(
            CHARACTER_BIBLE_FILE,
            {},
        )

        if not isinstance(character_bible, dict):
            print(
                "ERROR: character_bible.json is not a valid JSON object."
            )
            return 1

        print()
        print(
            "Character Bible loaded from:"
            f" {CHARACTER_BIBLE_FILE}"
        )

    else:
        print()
        print("Character Bible disabled by Input.")

    # ---------------------------------------------------------
    # EXISTING JOB MANIFEST
    # ---------------------------------------------------------

    existing_jobs = {}

    if resume_enabled and JOBS_FILE.exists():
        old_manifest = load_json(
            JOBS_FILE,
            {},
        )

        if isinstance(old_manifest, dict):
            old_jobs = old_manifest.get("jobs", {})

            if isinstance(old_jobs, dict):
                existing_jobs = old_jobs

    VISUALS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------------------------------------------------------
    # BUILD JOBS
    # ---------------------------------------------------------

    jobs = {}

    completed_count = 0
    pending_count = 0

    for index, scene in enumerate(scenes, start=1):

        if not isinstance(scene, dict):
            print(
                f"ERROR: Scene {index} is not a JSON object."
            )
            return 1

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

        visual_path = (
            VISUALS_DIR
            / f"part_{part:02d}"
            / f"scene_{scene_number:02d}.png"
        )

        old_job = existing_jobs.get(
            key,
            {},
        )

        # -----------------------------------------------------
        # PHYSICAL FILE IS THE SOURCE OF TRUTH
        # -----------------------------------------------------

        physical_exists = valid_visual(
            visual_path
        )

        old_status = (
            old_job.get("status")
            if isinstance(old_job, dict)
            else None
        )

        if (
            skip_completed
            and physical_exists
            and old_status == "completed"
        ):
            status = "completed"
            completed_count += 1

        elif (
            skip_completed
            and physical_exists
            and not old_job
        ):
            # Existing valid image without an old manifest entry.
            # Treat it as completed instead of regenerating it.
            status = "completed"
            completed_count += 1

        else:
            status = "pending"
            pending_count += 1

        jobs[key] = {
            "part": part,
            "scene": scene_number,
            "status": status,
            "visual_path": str(
                visual_path
            ),
            "scene_index": index,
        }

    # ---------------------------------------------------------
    # MANIFEST
    # ---------------------------------------------------------

    manifest = {
        "status": (
            "completed"
            if pending_count == 0
            else "pending"
        ),
        "format": format_name,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "expected_total": expected_total,
        "completed": completed_count,
        "pending": pending_count,
        "character_bible_enabled": character_bible_enabled,
        "character_bible_path": (
            str(CHARACTER_BIBLE_FILE)
            if character_bible_enabled
            else None
        ),
        "jobs": jobs,
    }

    save_json(
        JOBS_FILE,
        manifest,
    )

    # ---------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------

    print()
    print("=" * 60)
    print("          VISUAL PREPARATION COMPLETE")
    print("=" * 60)

    print(f"Expected visuals : {expected_total}")
    print(f"Completed        : {completed_count}")
    print(f"Pending          : {pending_count}")
    print(f"Manifest         : {JOBS_FILE}")

    if pending_count:
        print()
        print(
            "Visual generation is pending for "
            f"{pending_count} scene(s)."
        )
        print(
            "generate_visuals.py will generate only "
            "the missing visuals."
        )
    else:
        print()
        print("All visual files are already available.")

    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
