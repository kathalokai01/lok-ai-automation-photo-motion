
#!/usr/bin/env python3
"""Strict no-API preflight for the Katha Lok AI Photo Motion pipeline."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INPUT_FILE = ROOT / "Input" / "topic.txt"
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "generate-photo-motion-video.yml"
OUT = ROOT / "output"
REPORT_FILE = OUT / "preflight_report.json"

VERSION = "photo-motion-preflight-v52-execution-order"

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
IMAGE_ALLOWED = {
    "huggingface", "pollinations", "gemini", "replicate",
}

PYTHON_MODULES = (
    "requests",
    "PIL",
    "cv2",
    "gradio_client",
    "huggingface_hub",
    "edge_tts",
)

RUNTIME_COMMANDS = ("ffmpeg", "ffprobe", "fc-match")

OBSOLETE_PATTERN = re.compile(r"\bi2v\b|wan2gp", re.IGNORECASE)

STEP_NAME_RE = re.compile(r"^\s{6}-\s+name:\s*(.*?)\s*$")
RUN_RE = re.compile(r"^\s{8}run:\s*(.*?)\s*$")
PYTHON_SCRIPT_RE = re.compile(
    r"(?<![\w./-])python(?:3(?:\.\d+)?)?"
    r"\s+(?:-u\s+)?scripts/([A-Za-z0-9_.-]+\.py)"
)


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
    return [
        str(item).strip().lower()
        for item in value
        if str(item).strip()
    ]


def strip_shell_comment(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("#"):
        return ""
    return line


def extract_workflow_commands(source: str) -> list[dict]:
    """
    Read actual Python script invocations from YAML run sections.

    Does not use REQUIRED_SCRIPTS order or arbitrary filename mentions.
    Only executable commands inside workflow run steps are considered.
    """
    commands = []
    current_step = None
    inside_run = False

    for line_number, raw_line in enumerate(source.splitlines(), 1):
        step_match = STEP_NAME_RE.match(raw_line)

        if step_match:
            current_step = step_match.group(1).strip()
            inside_run = False
            continue

        if re.match(r"^\s{6}-\s+", raw_line):
            current_step = None
            inside_run = False

        run_match = RUN_RE.match(raw_line)

        if run_match:
            inside_run = True
            inline_command = run_match.group(1).strip()

            if inline_command and inline_command not in {"|", ">", "|-", ">-"}:
                candidate = strip_shell_comment(inline_command)
                for match in PYTHON_SCRIPT_RE.finditer(candidate):
                    commands.append({
                        "script": match.group(1),
                        "line": line_number,
                        "step": current_step or "unnamed",
                    })
            continue

        if not inside_run:
            continue

        indentation = len(raw_line) - len(raw_line.lstrip())

        # A YAML key at the run property's indentation ends the block.
        if raw_line.strip() and indentation <= 8:
            inside_run = False
            continue

        candidate = strip_shell_comment(raw_line)

        for match in PYTHON_SCRIPT_RE.finditer(candidate):
            commands.append({
                "script": match.group(1),
                "line": line_number,
                "step": current_step or "unnamed",
            })

    return commands


def check_workflow_order(commands: list[dict]) -> tuple[bool, str]:
    """
    Verify the required scripts in the order they are actually invoked.
    Additional setup commands and unrelated commands are allowed.
    """
    cursor = 0
    matched = []

    for expected in WORKFLOW_ORDER:
        found = None

        for index in range(cursor, len(commands)):
            if commands[index]["script"] == expected:
                found = index
                break

        if found is None:
            observed = [
                item["script"] for item in commands
            ]
            return False, (
                f"Missing or out-of-order executable command: {expected}. "
                f"Commands detected: {' -> '.join(observed)}"
            )

        matched.append(commands[found])
        cursor = found + 1

    details = [
        f"{item['script']} (line {item['line']}, step: {item['step']})"
        for item in matched
    ]

    return True, "Verified actual command sequence: " + " -> ".join(
        item["script"] for item in matched
    )


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
    mode = None

    # 1. Input and configuration.
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

        config_billable = get_global_billable_permission(config)
        environment_billable = (
            os.environ.get("ALLOW_BILLABLE_PROVIDERS", "")
            .strip().lower() in {"1", "true", "yes", "on"}
        )

        effective_billable = (
            config_billable is True or environment_billable
        )

        record(
            "input_file",
            True,
            "Input/topic.txt exists and is readable.",
        )

        record(
            "format",
            mode in {"short", "full"},
            f"FORMAT={mode}",
        )

        record(
            "parts_and_scenes",
            isinstance(parts, int)
            and isinstance(scenes, int)
            and parts >= 1
            and scenes >= 1,
            f"PARTS={parts}; SCENES={scenes}; total={parts * scenes}",
        )

        record(
            "fps",
            isinstance(fps, (int, float)) and 1 <= fps <= 60,
            f"FPS={fps}",
        )

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
            effective_billable,
            (
                f"Config={config_billable}; "
                f"ALLOW_BILLABLE_PROVIDERS="
                f"{environment_billable}; effective={effective_billable}"
            ),
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

    # 2. Required files and syntax.
    required_paths = [SCRIPTS / name for name in REQUIRED_SCRIPTS]

    for path in required_paths:
        exists = path.is_file() and path.stat().st_size > 0

        record(
            "required_file:" + path.name,
            exists,
            "Present." if exists else "Missing or empty.",
        )

        if exists:
            valid, detail = check_python_file(path)
            record("syntax:" + path.name, valid, detail)

    # 3. Removed I2V/Wan2GP pipeline references in active files.
    scan_paths = [WORKFLOW_FILE] + required_paths
    obsolete = []

    for path in scan_paths:
        if not path.is_file():
            continue

        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            obsolete.append(
                f"{path.relative_to(ROOT)}: unreadable ({exc})"
            )
            continue

        for line_number, line in enumerate(source.splitlines(), 1):
            if OBSOLETE_PATTERN.search(line):
                obsolete.append(
                    f"{path.relative_to(ROOT)}:{line_number}"
                )

    record(
        "photo_motion_only",
        not obsolete,
        (
            "No removed I2V/Wan2GP references in active pipeline files."
            if not obsolete
            else "; ".join(obsolete[:30])
        ),
    )

    # 4. Workflow readability and structural checks.
    workflow = ""

    try:
        workflow = WORKFLOW_FILE.read_text(encoding="utf-8-sig")
        record(
            "workflow_file",
            bool(workflow.strip()),
            "Workflow is readable.",
        )
    except Exception as exc:
        record("workflow_file", False, str(exc))

    structure_checks = {
        "workflow_dispatch": "workflow_dispatch:" in workflow,
        "ubuntu_runner": "runs-on: ubuntu-latest" in workflow,
        "python_311": bool(
            re.search(
                r"python-version:\s*[\"']?3\.11[\"']?",
                workflow,
            )
        ),
        "billable_true": bool(
            re.search(
                r"ALLOW_BILLABLE_PROVIDERS:\s*[\"']?true[\"']?",
                workflow,
                re.IGNORECASE,
            )
        ),
    }

    for name, passed in structure_checks.items():
        record(
            "workflow_structure:" + name,
            passed,
            "Present." if passed else "Missing or incorrect.",
        )

    # 5. Parse executable commands from actual YAML run sections.
    commands = extract_workflow_commands(workflow)

    record(
        "workflow_commands_detected",
        bool(commands),
        (
            f"Detected {len(commands)} executable Python script commands."
            if commands
            else "No executable Python script commands detected."
        ),
    )

    ordered, order_detail = check_workflow_order(commands)
    record("workflow_step_order", ordered, order_detail)

    # The preflight must execute before generation begins.
    preflight_positions = [
        item for item in commands
        if item["script"] == "pipeline_preflight.py"
    ]

    first_generation = next(
        (
            item for item in commands
            if item["script"] == WORKFLOW_ORDER[0]
        ),
        None,
    )

    preflight_before_generation = (
        bool(preflight_positions)
        and first_generation is not None
        and preflight_positions[0]["line"] < first_generation["line"]
    )

    record(
        "preflight_workflow_integration",
        preflight_before_generation,
        (
            "Preflight is invoked before generation."
            if preflight_before_generation
            else "Preflight command is missing or occurs after generation."
        ),
    )

    # Ensure the story-length hard gate is not just a filename mention.
    validation_command = next(
        (
            item for item in commands
            if item["script"] == "validate_story_length.py"
        ),
        None,
    )

    verify_command = next(
        (
            item for item in commands
            if item["script"] == "verify_story.py"
        ),
        None,
    )

    story_gate_order = (
        validation_command is not None
        and verify_command is not None
        and validation_command["line"] < verify_command["line"]
    )

    record(
        "story_length_gate_order",
        story_gate_order,
        (
            "Story-length validation executes before verify_story.py."
            if story_gate_order
            else "Story-length validation is missing or out of order."
        ),
    )

    # 6. Dependencies installed by the workflow.
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

    for command in RUNTIME_COMMANDS:
        found = shutil.which(command) is not None

        record(
            "runtime_command:" + command,
            found,
            "Available." if found else "Missing from PATH.",
        )

    # 7. Save an auditable report.
    failed = [
        item for item in checks
        if not item["passed"]
    ]

    report = {
        "version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": mode,
        "api_calls_made": 0,
        "generation_performed": False,
        "workflow_commands_detected": commands,
        "workflow_order_expected": list(WORKFLOW_ORDER),
        "ready_to_run": not failed,
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "failures": failed,
        "checks": checks,
    }

    try:
        atomic_json(REPORT_FILE, report)

        saved = json.loads(
            REPORT_FILE.read_text(encoding="utf-8")
        )

        if saved.get("version") != VERSION:
            raise RuntimeError("Preflight report version mismatch.")

        if saved.get("ready_to_run") is not report["ready_to_run"]:
            raise RuntimeError("Preflight report status mismatch.")

        if saved.get("checks_total") != len(checks):
            raise RuntimeError("Preflight report check count mismatch.")

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

    log(
        "PREFLIGHT PASSED — local checks only; "
        "external providers are not tested."
    )
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
