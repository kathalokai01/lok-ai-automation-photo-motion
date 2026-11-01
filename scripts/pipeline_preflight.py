#!/usr/bin/env python3
"""Strict no-API preflight for the Katha Lok AI Photo Motion pipeline."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INPUT_FILE = ROOT / "Input" / "topic.txt"
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "generate-photo-motion-video.yml"
OUT = ROOT / "output"
REPORT_FILE = OUT / "preflight_report.json"

VERSION = "photo-motion-preflight-v51-strict"

REQUIRED_SCRIPTS = (
    "input_config.py",
    "select_gemini_model.py",
    "generate_story_plan.py",
    "generate_story.py",
    "validate_story_length.py",
    "verify_story.py",
    "generate_ai_story.py",
    "generate_character_bible.py",
    "generate_scenes.py",
    "generate_narration.py",
    "prepare_captions.py",
    "generate_tts.py",
    "generate_flow_music.py",
    "generate_visuals.py",
    "free_image_generator.py",
    "generate_depth_maps.py",
    "depth_parallax.py",
    "photo_motion.py",
    "generate_sound_design.py",
    "validate_sfx_timing.py",
    "render_video.py",
    "install_caption_fonts.py",
    "preview_caption_fonts.py",
    "burn_captions.py",
    "generate_thumbnail.py",
    "generate_youtube_metadata.py",
    "validate_final_output.py",
    "package_output.py",
)

WORKFLOW_ORDER = (
    "select_gemini_model.py",
    "generate_story_plan.py",
    "generate_story.py",
    "validate_story_length.py",
    "verify_story.py",
    "generate_ai_story.py",
    "generate_character_bible.py",
    "generate_scenes.py",
    "generate_narration.py",
    "prepare_captions.py",
    "generate_tts.py",
    "generate_flow_music.py",
    "generate_visuals.py",
    "free_image_generator.py",
    "generate_depth_maps.py",
    "photo_motion.py",
    "generate_sound_design.py",
    "validate_sfx_timing.py",
    "render_video.py",
    "burn_captions.py",
    "generate_thumbnail.py",
    "generate_youtube_metadata.py",
    "validate_final_output.py",
    "package_output.py",
)

TTS_ALLOWED = {
    "fish_audio", "gemini_tts", "edge_tts", "piper",
    "elevenlabs", "cartesia", "finevoice", "minimax",
}
IMAGE_ALLOWED = {"huggingface", "pollinations", "gemini", "replicate"}

PYTHON_MODULES = (
    "requests", "PIL", "cv2", "gradio_client",
    "huggingface_hub", "edge_tts",
)
RUNTIME_COMMANDS = ("ffmpeg", "ffprobe", "fc-match")

OBSOLETE_PATTERN = re.compile(r"\bi2v\b|wan2gp", re.IGNORECASE)


def log(*items):
    print(*items, flush=True)


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def check_python_file(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8-sig")
        ast.parse(source, filename=str(path))
        return True, "Python syntax valid."
    except Exception as exc:
        return False, str(exc)


def normalize_list(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def main() -> int:
    checks = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })
        log("PASS" if passed else "FAIL", name, "-", detail)

    log("=" * 64)
    log("KATHA LOK AI — PHOTO MOTION PREFLIGHT")
    log("VERSION:", VERSION)
    log("NO GENERATION API CALLS")
    log("=" * 64)

    config = {}

    # 1. Input and canonical configuration.
    try:
        if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size == 0:
            raise RuntimeError("Input/topic.txt is missing or empty.")

        sys.path.insert(0, str(SCRIPTS))
        from input_config import (
            load_and_validate,
            get_format,
            get_parts,
            get_scenes,
            get_fps,
            get_tts_provider_order,
            get_image_provider_order,
            get_global_billable_permission,
        )

        config = load_and_validate(INPUT_FILE)
        mode = get_format(config)
        parts = get_parts(config)
        scenes = get_scenes(config)
        fps = get_fps(config)
        tts_order = get_tts_provider_order(config)
        image_order = get_image_provider_order(config)
        billable = get_global_billable_permission(config)

        record("input_file", True, "Input/topic.txt exists and is readable.")
        record("format", mode in {"short", "full"}, f"FORMAT={mode}")
        record(
            "parts_and_scenes",
            parts >= 1 and scenes >= 1,
            f"PARTS={parts}; SCENES={scenes}; total={parts * scenes}",
        )
        record("fps", 1 <= fps <= 60, f"FPS={fps}")
        record(
            "tts_provider_order",
            len(tts_order) >= 3
            and len(tts_order) == len(set(tts_order))
            and all(item in TTS_ALLOWED for item in tts_order),
            " -> ".join(tts_order),
        )
        record(
            "image_provider_order",
            bool(image_order)
            and len(image_order) == len(set(image_order))
            and all(item in IMAGE_ALLOWED for item in image_order),
            " -> ".join(image_order),
        )
        record(
            "billable_permission",
            billable is True,
            f"ALLOW_BILLABLE_PROVIDERS={billable}",
        )

        story_text = str(config.get("STORY_TEXT", "")).strip()
        preserve = config.get("PRESERVE_STORY_TEXT", True)
        record(
            "pasted_story_preservation",
            not story_text or preserve is True,
            (
                "Supplied story preservation is enabled."
                if story_text and preserve is True
                else "No pasted story supplied."
                if not story_text
                else "PRESERVE_STORY_TEXT must be true."
            ),
        )

    except Exception as exc:
        record("input_configuration", False, str(exc))
        mode = None

    # 2. Required scripts and Python syntax.
    required_paths = [SCRIPTS / name for name in REQUIRED_SCRIPTS]
    missing = []

    for path in required_paths:
        exists = path.is_file() and path.stat().st_size > 0
        if not exists:
            missing.append(path.name)
        record(
            "required_file:" + path.name,
            exists,
            "Present." if exists else "Missing or empty.",
        )

        if exists:
            valid, detail = check_python_file(path)
            record("syntax:" + path.name, valid, detail)

    # 3. Active Photo Motion code must not refer to removed pipelines.
    # This checker deliberately excludes its own source so its search
    # expression cannot report itself as an obsolete dependency.
    scan_paths = [WORKFLOW_FILE] + [
        path for path in required_paths
        if path.resolve() != Path(__file__).resolve()
    ]

    obsolete = []
    for path in scan_paths:
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            obsolete.append(f"{path}: unreadable source")
            continue

        for line_number, line in enumerate(source.splitlines(), 1):
            if OBSOLETE_PATTERN.search(line):
                obsolete.append(f"{path.relative_to(ROOT)}:{line_number}")

    record(
        "photo_motion_only",
        not obsolete,
        (
            "No removed I2V/Wan2GP references in active pipeline files."
            if not obsolete
            else "; ".join(obsolete[:20])
        ),
    )

    # 4. Workflow structure and exact command order.
    workflow = ""
    try:
        workflow = WORKFLOW_FILE.read_text(encoding="utf-8-sig")
        record("workflow_file", bool(workflow.strip()), "Workflow is readable.")
    except Exception as exc:
        record("workflow_file", False, str(exc))

    structure_ok = (
        "workflow_dispatch:" in workflow
        and "runs-on: ubuntu-latest" in workflow
        and 'python-version: "3.11"' in workflow
        and 'ALLOW_BILLABLE_PROVIDERS: "true"' in workflow
    )
    record(
        "workflow_structure",
        structure_ok,
        "Manual dispatch, Ubuntu, Python 3.11 and billable=true checked.",
    )

    positions = []
    for filename in WORKFLOW_ORDER:
        matches = [
            match.start()
            for match in re.finditer(
                re.escape("scripts/" + filename),
                workflow,
            )
        ]
        positions.append(matches[0] if matches else -1)

    all_present = all(position >= 0 for position in positions)
    ordered = (
        all_present
        and positions == sorted(positions)
        and len(positions) == len(set(positions))
    )

    record(
        "workflow_step_order",
        ordered,
        (
            "All required generation, story-length validation, final "
            "validation and packaging steps appear in order."
            if ordered
            else "Missing or incorrectly ordered workflow commands: "
            + ", ".join(
                filename
                for filename, position in zip(WORKFLOW_ORDER, positions)
                if position < 0
            )
        ),
    )

    preflight_command = "python scripts/pipeline_preflight.py" in workflow
    record(
        "preflight_workflow_integration",
        preflight_command,
        (
            "Workflow invokes pipeline preflight."
            if preflight_command
            else "Workflow does not invoke pipeline preflight."
        ),
    )

    # 5. Dependencies are checked after the workflow installs them.
    for module in PYTHON_MODULES:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        record(
            "python_dependency:" + module,
            found,
            "Importable." if found else "Not installed or not importable.",
        )

    import shutil
    for command in RUNTIME_COMMANDS:
        found = shutil.which(command) is not None
        record(
            "runtime_command:" + command,
            found,
            "Available." if found else "Missing from PATH.",
        )

    failed = [item for item in checks if not item["passed"]]
    report = {
        "version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": mode,
        "api_calls_made": 0,
        "generation_performed": False,
        "ready_to_run": not failed,
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "failures": failed,
        "checks": checks,
    }

    try:
        atomic_json(REPORT_FILE, report)
        saved = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        if saved.get("version") != VERSION:
            raise RuntimeError("Preflight report version mismatch.")
        if saved.get("ready_to_run") is not report["ready_to_run"]:
            raise RuntimeError("Preflight report status mismatch.")
    except Exception as exc:
        log("PREFLIGHT REPORT ERROR:", exc)
        return 1

    log("-" * 64)
    log("CHECKS:", len(checks))
    log("PASSED:", report["checks_passed"])
    log("FAILED:", report["checks_failed"])
    log("REPORT:", REPORT_FILE.relative_to(ROOT))

    if failed:
        log("PREFLIGHT FAILED — generation must not start.")
        return 1

    log("PREFLIGHT PASSED — local checks only; external providers are not tested.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("PREFLIGHT CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("PREFLIGHT INTERNAL ERROR:", exc)
        raise SystemExit(1)

