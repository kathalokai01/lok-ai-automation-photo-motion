#!/usr/bin/env python3
"""Strict, offline preflight for the Katha Lok AI Photo Motion pipeline."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INPUT_FILE = ROOT / "Input" / "topic.txt"
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "generate-photo-motion-video.yml"
OUT = ROOT / "output"
REPORT_FILE = OUT / "preflight_report.json"

VERSION = "photo-motion-preflight-v60-offline-integration-verified"

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
    "pipeline_preflight.py",
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
    "fish_audio",
    "gemini_tts",
    "edge_tts",
    "piper",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
}

IMAGE_ALLOWED = {
    "huggingface",
    "pollinations",
    "gemini",
    "replicate",
}

PYTHON_MODULES = (
    "requests",
    "PIL",
    "cv2",
    "gradio_client",
    "huggingface_hub",
    "edge_tts",
)

RUNTIME_COMMANDS = (
    "ffmpeg",
    "ffprobe",
    "fc-match",
    "zip",
    "unzip",
)

FONT_FAMILIES = (
    "Noto Sans Devanagari",
    "Mukta",
    "Hind",
)

STEP_NAME_RE = re.compile(r"^\s{6}-\s+name:\s*(.*?)\s*$")
RUN_RE = re.compile(r"^\s{8}run:\s*(.*?)\s*$")
PYTHON_SCRIPT_RE = re.compile(
    r"(?<![\w./-])python(?:3(?:\.\d+)?)?"
    r"\s+(?:-u\s+)?scripts/([A-Za-z0-9_.-]+\.py)"
)

LEGACY_EXECUTABLE_PATTERN = re.compile(
    r"\bi2v\b|wan2gp",
    re.IGNORECASE,
)

GENERATION_SCRIPTS = set(WORKFLOW_ORDER)

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def log(*items: Any) -> None:
    print(*items, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def check_python_file(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8-sig")
        ast.parse(source, filename=str(path))
        return True, "Python syntax valid."
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def normalize_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;]", value)

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
    """Extract executable Python script commands from YAML run blocks."""

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
            inline = run_match.group(1).strip()

            if inline and inline not in {"|", ">", "|-", ">-"}:
                candidate = strip_shell_comment(inline)

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


def check_workflow_order(
    commands: list[dict],
) -> tuple[bool, str]:
    """Require the complete generation sequence in actual command order."""

    cursor = 0
    matched = []

    for expected in WORKFLOW_ORDER:
        found = None

        for index in range(cursor, len(commands)):
            if commands[index]["script"] == expected:
                found = index
                break

        if found is None:
            observed = [item["script"] for item in commands]

            return False, (
                f"Missing or out-of-order command: {expected}. "
                f"Detected: {' -> '.join(observed)}"
            )

        matched.append(expected)
        cursor = found + 1

    return True, (
        "Verified generation order: "
        + " -> ".join(matched)
    )


def check_workflow_preflight(
    commands: list[dict],
) -> tuple[bool, str]:
    preflight = [
        item for item in commands
        if item["script"] == "pipeline_preflight.py"
    ]

    first_generation = next(
        (
            item for item in commands
            if item["script"] in GENERATION_SCRIPTS
        ),
        None,
    )

    if not preflight:
        return False, (
            "pipeline_preflight.py is not executed by the workflow."
        )

    if first_generation is None:
        return False, "No generation command found in workflow."

    if preflight[0]["line"] >= first_generation["line"]:
        return False, (
            "Preflight must execute before the first generation command."
        )

    if len(preflight) != 1:
        return False, (
            f"Expected exactly one preflight invocation; found {len(preflight)}."
        )

    return True, (
        "Exactly one preflight command executes before generation."
    )


def check_story_gate(
    commands: list[dict],
) -> tuple[bool, str]:
    validation = next(
        (
            item for item in commands
            if item["script"] == "validate_story_length.py"
        ),
        None,
    )

    verification = next(
        (
            item for item in commands
            if item["script"] == "verify_story.py"
        ),
        None,
    )

    story_generation = next(
        (
            item for item in commands
            if item["script"] == "generate_story.py"
        ),
        None,
    )

    if not validation or not verification or not story_generation:
        return False, "Story generation or validation command is missing."

    if not (
        story_generation["line"]
        < validation["line"]
        < verification["line"]
    ):
        return False, (
            "Required order: generate_story.py, "
            "validate_story_length.py, verify_story.py."
        )

    return True, (
        "Story length validation and verification execute "
        "after story generation and before AI story generation."
    )


def check_legacy_execution() -> tuple[bool, str]:
    """Reject actual legacy execution, not comments or ordinary text."""

    violations = []
    executable_calls = {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "system",
        "exec",
    }

    for name in REQUIRED_SCRIPTS:
        path = SCRIPTS / name

        if not path.is_file():
            continue

        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except Exception as exc:
            violations.append(
                f"{name}: cannot inspect Python syntax: {exc}"
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if LEGACY_EXECUTABLE_PATTERN.search(alias.name):
                        violations.append(
                            f"{name}:{node.lineno} imports {alias.name}"
                        )

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""

                if LEGACY_EXECUTABLE_PATTERN.search(module):
                    violations.append(
                        f"{name}:{node.lineno} imports {module}"
                    )

            if not isinstance(node, ast.Call):
                continue

            function = node.func

            function_name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else ""
            )

            if function_name not in executable_calls:
                continue

            string_values = []

            for argument in list(node.args) + [
                keyword.value for keyword in node.keywords
            ]:
                string_values.extend(
                    child.value
                    for child in ast.walk(argument)
                    if isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                )

            for value in string_values:
                if LEGACY_EXECUTABLE_PATTERN.search(value):
                    violations.append(
                        f"{name}:{node.lineno} legacy executable command"
                    )
                    break

    if violations:
        return False, "; ".join(violations[:30])

    return True, (
        "No executable legacy I2V/Wan2GP integration detected."
    )


def check_module(module: str) -> tuple[bool, str]:
    try:
        found = importlib.util.find_spec(module) is not None
        return (
            found,
            "Importable." if found else "Module not installed.",
        )
    except (ImportError, ValueError, AttributeError) as exc:
        return False, f"Module check failed: {exc}"


def check_runtime_command(command: str) -> tuple[bool, str]:
    executable = shutil.which(command)

    if not executable:
        return False, f"{command} is missing from PATH."

    return True, executable


def check_font(family: str) -> tuple[bool, str]:
    executable = shutil.which("fc-match")

    if not executable:
        return False, "fc-match is not installed."

    try:
        result = subprocess.run(
            [
                executable,
                "-f",
                "%{file}\n",
                family,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        if result.returncode != 0:
            return False, result.stderr.strip() or "Font lookup failed."

        font_path = Path(result.stdout.strip().splitlines()[0])

        if not font_path.is_file():
            return False, f"Font file not found: {font_path}"

        return True, str(font_path)

    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_input_configuration() -> tuple[dict, list[dict]]:
    checks = []
    config = {}

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })

    if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size == 0:
        add(
            "input_file",
            False,
            "Input/topic.txt is missing or empty.",
        )
        return config, checks

    add(
        "input_file",
        True,
        "Input/topic.txt exists and is non-empty.",
    )

    sys.path.insert(0, str(SCRIPTS))

    try:
        from input_config import (
            load_and_validate,
            get_format,
            get_parts,
            get_parts_range,
            get_scenes,
            get_fps,
            get_tts_provider_order,
            get_image_provider_order,
            get_global_billable_permission,
            get_story_text,
        )

        config = load_and_validate(INPUT_FILE)

        mode = get_format(config)
        minimum, maximum = get_parts_range(config)
        selected = get_parts(config)
        scenes = get_scenes(config)
        fps = get_fps(config)

        tts_order = get_tts_provider_order(config)
        image_order = get_image_provider_order(config)

        billable = get_global_billable_permission(config)

        add(
            "format",
            mode in {"short", "full"},
            f"FORMAT={mode}",
        )

        add(
            "parts_range_and_selection",
            (
                minimum >= 1
                and maximum >= minimum
                and minimum <= selected <= maximum
            ),
            (
                f"Range={minimum}-{maximum}; "
                f"selected={selected}; "
                f"configured scenes={selected * scenes}"
            ),
        )

        add(
            "scenes",
            isinstance(scenes, int) and 1 <= scenes <= 500,
            f"SCENES={scenes}",
        )

        add(
            "fps",
            isinstance(fps, int) and 1 <= fps <= 60,
            f"FPS={fps}",
        )

        add(
            "tts_provider_order",
            (
                len(tts_order) >= 3
                and len(tts_order) == len(set(tts_order))
                and all(item in TTS_ALLOWED for item in tts_order)
            ),
            " -> ".join(tts_order),
        )

        add(
            "image_provider_order",
            (
                bool(image_order)
                and len(image_order) == len(set(image_order))
                and all(item in IMAGE_ALLOWED for item in image_order)
            ),
            " -> ".join(image_order),
        )

        add(
            "billable_permission",
            billable is True,
            (
                "Configured permission is enabled."
                if billable
                else "Configured permission blocks billable providers."
            ),
        )

        story_text = get_story_text(config).strip()
        preserve = config.get("PRESERVE_STORY_TEXT") is True

        add(
            "pasted_story_preservation",
            not story_text or preserve,
            (
                "Supplied story preservation is enabled."
                if story_text and preserve
                else "No pasted story supplied."
                if not story_text
                else "PRESERVE_STORY_TEXT must be true."
            ),
        )

        photo_settings = (
            "PHOTO_MOTION_STYLE",
            "PHOTO_MOTION_SPEED",
            "PHOTO_MOTION_VARIATION",
            "PHOTO_MOTION_PARALLAX",
            "PHOTO_MOTION_PARALLAX_STRENGTH",
            "PHOTO_MOTION_PRESERVE_SUBJECT",
        )

        missing_settings = [
            key for key in photo_settings
            if key not in config
        ]

        add(
            "photo_motion_settings",
            not missing_settings,
            (
                "All photo-motion settings are present."
                if not missing_settings
                else "Missing: " + ", ".join(missing_settings)
            ),
        )

        add(
            "resume_and_checkpoint",
            (
                config.get("RESUME_ENABLED") is True
                and config.get("SAVE_CHECKPOINT_AFTER_EACH_SCENE") is True
                and config.get("SKIP_COMPLETED_SCENES") is True
            ),
            (
                "Resume, per-scene checkpoint and completed-scene reuse "
                "must all be enabled."
            ),
        )

    except Exception as exc:
        add(
            "input_configuration",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    return config, checks


def check_workflow_structure(source: str) -> list[dict]:
    checks = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })

    if not source.strip():
        add(
            "workflow_readable",
            False,
            "Workflow file is empty.",
        )
        return checks

    add(
        "workflow_readable",
        True,
        "Workflow file is readable.",
    )

    patterns = {
        "manual_dispatch": (
            r"(?m)^\s{2}workflow_dispatch:\s*(?:#.*)?$"
        ),
        "ubuntu_runner": (
            r"(?m)^\s{4}runs-on:\s*ubuntu-latest\s*$"
        ),
        "python_311": (
            r"(?m)^\s{10}python-version:\s*[\"']?3\.11[\"']?\s*$"
        ),
        "billable_environment": (
            r"(?m)^\s{6}ALLOW_BILLABLE_PROVIDERS:\s*[\"']?true[\"']?\s*$"
        ),
        "checkout_action": (
            r"(?m)^\s{8}uses:\s*actions/checkout@v4\s*$"
        ),
        "python_setup_action": (
            r"(?m)^\s{8}uses:\s*actions/setup-python@v5\s*$"
        ),
        "artifact_upload_action": (
            r"(?m)^\s{8}uses:\s*actions/upload-artifact@v4\s*$"
        ),
        "checkpoint_upload": (
            r"(?m)^\s{10}name:\s*katha-photo-motion-checkpoint-\$\{\{\s*github\.run_id\s*\}\}\s*$"
        ),
        "final_output_upload": (
            r"(?m)^\s{10}name:\s*katha-lok-ai-photo-motion-output-\$\{\{\s*github\.run_id\s*\}\}\s*$"
        ),
    }

    for name, pattern in patterns.items():
        passed = bool(re.search(pattern, source))

        add(
            "workflow_structure:" + name,
            passed,
            "Present." if passed else "Missing or incorrect.",
        )

    return checks


def check_report_integrity(report: dict) -> tuple[bool, str]:
    checks = report.get("checks")

    if not isinstance(checks, list) or not checks:
        return False, "Preflight report has no checks."

    names = [
        item.get("name")
        for item in checks
        if isinstance(item, dict)
    ]

    if len(names) != len(checks):
        return False, "Preflight report contains invalid check records."

    if len(names) != len(set(names)):
        return False, "Preflight report contains duplicate check names."

    failures = [
        item for item in checks
        if item.get("passed") is not True
    ]

    if len(failures) != report.get("checks_failed"):
        return False, "Preflight failure count does not match its checks."

    if report.get("checks_total") != len(checks):
        return False, "Preflight total check count mismatch."

    if report.get("checks_passed") != len(checks) - len(failures):
        return False, "Preflight passed check count mismatch."

    if report.get("ready_to_run") is not (len(failures) == 0):
        return False, "Preflight readiness flag mismatch."

    return True, "Report counts and readiness verified."


def main() -> int:
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })

        log(
            "PASS" if passed else "FAIL",
            name,
            "-",
            detail,
        )

    log("=" * 68)
    log("KATHA LOK AI — PHOTO MOTION PREFLIGHT")
    log("VERSION:", VERSION)
    log("OFFLINE VALIDATION — NO GENERATION API CALLS")
    log("=" * 68)

    config, input_checks = check_input_configuration()

    for item in input_checks:
        record(
            item["name"],
            item["passed"],
            item["detail"],
        )

    # Required source files and Python syntax.
    for name in REQUIRED_SCRIPTS:
        path = SCRIPTS / name

        exists = (
            path.is_file()
            and path.stat().st_size > 0
        )

        record(
            "required_file:" + name,
            exists,
            (
                "Present and non-empty."
                if exists
                else "Missing or empty."
            ),
        )

        if exists:
            valid, detail = check_python_file(path)

            record(
                "syntax:" + name,
                valid,
                detail,
            )

    # Confirm that the pipeline does not execute the removed legacy path.
    legacy_ok, legacy_detail = check_legacy_execution()

    record(
        "photo_motion_execution_path",
        legacy_ok,
        legacy_detail,
    )

    # Actual workflow file.
    workflow = ""

    try:
        workflow = WORKFLOW_FILE.read_text(
            encoding="utf-8-sig"
        )
    except Exception as exc:
        record(
            "workflow_file",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    for item in check_workflow_structure(workflow):
        record(
            item["name"],
            item["passed"],
            item["detail"],
        )

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

    order_ok, order_detail = check_workflow_order(commands)

    record(
        "workflow_generation_order",
        order_ok,
        order_detail,
    )

    preflight_ok, preflight_detail = check_workflow_preflight(
        commands
    )

    record(
        "preflight_before_generation",
        preflight_ok,
        preflight_detail,
    )

    story_gate_ok, story_gate_detail = check_story_gate(commands)

    record(
        "story_length_hard_gate",
        story_gate_ok,
        story_gate_detail,
    )

    # The workflow must invoke every required generation script.
    observed = {
        item["script"]
        for item in commands
    }

    missing_commands = [
        name for name in WORKFLOW_ORDER
        if name not in observed
    ]

    record(
        "all_generation_scripts_invoked",
        not missing_commands,
        (
            "All required generation commands are invoked."
            if not missing_commands
            else "Missing commands: " + ", ".join(missing_commands)
        ),
    )

    # Verify dependencies available in the current runtime.
    for module in PYTHON_MODULES:
        passed, detail = check_module(module)

        record(
            "python_dependency:" + module,
            passed,
            detail,
        )

    for command in RUNTIME_COMMANDS:
        passed, detail = check_runtime_command(command)

        record(
            "runtime_command:" + command,
            passed,
            detail,
        )

    for family in FONT_FAMILIES:
        passed, detail = check_font(family)

        record(
            "caption_font:" + family,
            passed,
            detail,
        )

    # Validate output directory is writable without modifying deliverables.
    try:
        OUT.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OUT,
            prefix=".preflight-write-test-",
            delete=True,
        ) as handle:
            handle.write("preflight")
            handle.flush()
            os.fsync(handle.fileno())

        record(
            "output_directory_writable",
            True,
            "Output directory is writable.",
        )

    except Exception as exc:
        record(
            "output_directory_writable",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    # Record source fingerprints for diagnosis; no network access.
    fingerprints = {}

    for path in (
        INPUT_FILE,
        WORKFLOW_FILE,
        ROOT / "requirements.txt",
    ):
        if path.is_file():
            fingerprints[path.relative_to(ROOT).as_posix()] = (
                sha256_file(path)
            )

    for path in sorted(SCRIPTS.glob("*.py")):
        if path.is_file():
            fingerprints[path.relative_to(ROOT).as_posix()] = (
                sha256_file(path)
            )

    failed = [
        item for item in checks
        if item["passed"] is not True
    ]

    report = {
        "version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_root": ".",
        "format": config.get("FORMAT") if config else None,
        "parts_range": (
            [
                config.get("PARTS_MIN"),
                config.get("PARTS_MAX"),
            ]
            if config else None
        ),
        "selected_parts": (
            config.get("PARTS_SELECTED")
            if config else None
        ),
        "scenes_per_part": (
            config.get("SCENES")
            if config else None
        ),
        "fps": config.get("FPS") if config else None,
        "tts_provider_order": (
            config.get("TTS_PROVIDER_ORDER")
            if config else None
        ),
        "image_provider_order": (
            config.get("IMAGE_PROVIDER_ORDER")
            if config else None
        ),
        "billable_providers_allowed": (
            config.get("ALLOW_BILLABLE_PROVIDERS")
            if config else None
        ),
        "api_calls_made": 0,
        "generation_performed": False,
        "workflow_commands_detected": commands,
        "workflow_order_expected": list(WORKFLOW_ORDER),
        "source_fingerprints": fingerprints,
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

        integrity_ok, integrity_detail = check_report_integrity(
            saved
        )

        if not integrity_ok:
            raise RuntimeError(integrity_detail)

        if saved.get("api_calls_made") != 0:
            raise RuntimeError("Preflight report API-call count mismatch.")

        if saved.get("generation_performed") is not False:
            raise RuntimeError("Preflight must not perform generation.")

        record(
            "report_integrity",
            True,
            integrity_detail,
        )

        # Update report with its own integrity check.
        failed = [
            item for item in checks
            if item["passed"] is not True
        ]

        report.update({
            "ready_to_run": not failed,
            "checks_total": len(checks),
            "checks_passed": len(checks) - len(failed),
            "checks_failed": len(failed),
            "failures": failed,
            "checks": checks,
        })

        atomic_json(REPORT_FILE, report)

        final_saved = json.loads(
            REPORT_FILE.read_text(encoding="utf-8")
        )

        final_ok, final_detail = check_report_integrity(
            final_saved
        )

        if not final_ok:
            raise RuntimeError(final_detail)

    except Exception as exc:
        log(
            "PREFLIGHT REPORT ERROR:",
            f"{type(exc).__name__}: {exc}",
        )
        return 1

    log("-" * 68)
    log("CHECKS:", report["checks_total"])
    log("PASSED:", report["checks_passed"])
    log("FAILED:", report["checks_failed"])
    log("REPORT:", REPORT_FILE.relative_to(ROOT))

    if failed:
        log("PREFLIGHT FAILED — generation must not start.")

        for item in failed:
            log(
                "BLOCKER:",
                item["name"],
                "-",
                item["detail"],
            )

        return 1

    log(
        "PREFLIGHT PASSED — local checks only. "
        "External provider credentials, quotas and availability "
        "are not tested."
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("PREFLIGHT CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log(
            "PREFLIGHT INTERNAL ERROR:",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)