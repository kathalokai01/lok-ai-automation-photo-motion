#!/usr/bin/env python3
"""No-API validation for the Katha Lok AI Photo Motion pipeline."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INPUT_FILE = ROOT / "Input" / "topic.txt"
WORKFLOW_FILE = (
    ROOT / ".github" / "workflows"
    / "generate-photo-motion-video.yml"
)
OUTPUT_DIR = ROOT / "output"
REPORT_FILE = OUTPUT_DIR / "preflight_report.json"

VERSION = "photo-motion-preflight-v2"

# Scripts required by the current Photo Motion workflow.
REQUIRED_SCRIPTS = [
    "input_config.py",
    "select_gemini_model.py",
    "generate_story_plan.py",
    "generate_story.py",
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
]

# These are the actual pipeline entry points expected in workflow order.
EXPECTED_WORKFLOW_STEPS = [
    "select_gemini_model.py",
    "generate_story_plan.py",
    "generate_story.py",
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
]

FORBIDDEN_TERMS = (
    "wan2gp",
    "wan2gp/",
    "wan2gp\\",
)

REQUIRED_PYTHON_MODULES = (
    "requests",
    "PIL",
    "cv2",
    "gradio_client",
    "huggingface_hub",
    "edge_tts",
)

OPTIONAL_PYTHON_MODULES = (
    "torch",
    "transformers",
    "google",
    "google.generativeai",
)

TRUE_VALUES = {"true", "yes", "on", "1", "enabled"}
FALSE_VALUES = {"false", "no", "off", "0", "disabled"}


def log(*values: Any) -> None:
    print(*values, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def parse_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in TRUE_VALUES:
        return True

    if text in FALSE_VALUES:
        return False

    raise ValueError(f"{key} must be true or false.")


def read_input_values(path: Path) -> dict[str, Any]:
    """Read the configuration while preserving multiline STORY_TEXT."""
    if not path.is_file():
        raise FileNotFoundError(f"Input file is missing: {path}")

    lines = path.read_text(
        encoding="utf-8-sig"
    ).splitlines()

    values: dict[str, Any] = {}
    index = 0

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if not stripped or stripped.startswith("#"):
            index += 1
            continue

        match = re.match(
            r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$",
            raw,
        )

        if not match:
            index += 1
            continue

        key = match.group(1).upper()
        value = match.group(2).strip()

        if key == "STORY_TEXT":
            delimiter = None

            if value.startswith('"""'):
                delimiter = '"""'
            elif value.startswith("'''"):
                delimiter = "'''"

            if delimiter:
                remainder = value[len(delimiter):]
                end = remainder.find(delimiter)

                if end >= 0:
                    values[key] = remainder[:end]
                    index += 1
                    continue

                collected = [remainder] if remainder else []
                index += 1
                closed = False

                while index < len(lines):
                    current = lines[index]
                    end = current.find(delimiter)

                    if end >= 0:
                        if current[:end]:
                            collected.append(current[:end])

                        trailing = current[
                            end + len(delimiter):
                        ].strip()

                        if trailing and not trailing.startswith("#"):
                            raise ValueError(
                                "Unexpected text after STORY_TEXT closing delimiter."
                            )

                        closed = True
                        index += 1
                        break

                    collected.append(current)
                    index += 1

                if not closed:
                    raise ValueError(
                        "STORY_TEXT multiline block is not closed."
                    )

                values[key] = "\n".join(collected)
                continue

        values[key] = value.strip().strip("'\"")
        index += 1

    return values


def write_report(report: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    temporary = REPORT_FILE.with_suffix(".json.tmp")
    content = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    temporary.write_text(content, encoding="utf-8")
    temporary.replace(REPORT_FILE)


def check_python_syntax(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        return True, "Python syntax valid."
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, str(exc)


def check_module(module_name: str) -> tuple[bool, str]:
    try:
        available = importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        available = False

    if available:
        return True, "Python module is installed."

    return False, f"Python module is not installed: {module_name}"


def extract_workflow_commands(source: str) -> list[str]:
    """Find scripts invoked through python in the workflow."""
    pattern = re.compile(
        r"(?<![\w./-])python(?:3)?\s+"
        r"(scripts/[A-Za-z0-9_.-]+\.py)",
        re.IGNORECASE,
    )

    return [
        match.group(1).replace("\\", "/")
        for match in pattern.finditer(source)
    ]


def check_workflow_order(
    actual_commands: list[str],
) -> tuple[bool, list[str]]:
    expected = [
        f"scripts/{name}"
        for name in EXPECTED_WORKFLOW_STEPS
    ]

    errors: list[str] = []
    cursor = 0

    for expected_path in expected:
        try:
            position = actual_commands.index(
                expected_path,
                cursor,
            )
        except ValueError:
            errors.append(
                f"Workflow is missing or misorders: {expected_path}"
            )
            continue

        cursor = position + 1

    duplicates = sorted({
        command for command in actual_commands
        if actual_commands.count(command) > 1
    })

    for duplicate in duplicates:
        errors.append(
            f"Workflow invokes the same script more than once: {duplicate}"
        )

    return not errors, errors


def inspect_obsolete_references(
    workflow_source: str,
    script_paths: list[Path],
) -> list[str]:
    """Check active workflow and pipeline scripts for obsolete I2V references."""
    problems: list[str] = []

    sources = [
        (WORKFLOW_FILE, workflow_source),
    ]

    for path in script_paths:
        try:
            sources.append((
                path,
                path.read_text(encoding="utf-8"),
            ))
        except (OSError, UnicodeError):
            continue

    for path, source in sources:
        for line_number, line in enumerate(
            source.splitlines(),
            start=1,
        ):
            normalized = line.lower()

            if any(term in normalized for term in FORBIDDEN_TERMS):
                problems.append(
                    f"{path.relative_to(ROOT)}:{line_number}: "
                    "obsolete Wan2GP reference"
                )

            if re.search(r"\bi2v\b", normalized):
                problems.append(
                    f"{path.relative_to(ROOT)}:{line_number}: "
                    "obsolete I2V reference"
                )

    return problems

def main() -> int:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []

    def record(
        name: str,
        passed: bool,
        message: str,
        severity: str = "error",
    ) -> None:
        item = {
            "name": name,
            "passed": bool(passed),
            "message": str(message),
            "severity": severity,
        }

        checks.append(item)

        if not passed:
            if severity == "error":
                errors.append(f"{name}: {message}")
            else:
                warnings.append(f"{name}: {message}")

        log(
            "PASS" if passed else severity.upper(),
            name,
            "-",
            message,
        )

    log("=" * 58)
    log("KATHA LOK AI — PHOTO MOTION PREFLIGHT")
    log("No story, image, audio or video generation API calls.")
    log("=" * 58)

    # --------------------------------------------------------
    # INPUT FILE
    # --------------------------------------------------------

    try:
        values = read_input_values(INPUT_FILE)
        record(
            "input_file",
            True,
            "Input/topic.txt exists and was read.",
        )
    except Exception as exc:
        values = {}
        record("input_file", False, str(exc))

    input_hash = None

    if INPUT_FILE.is_file():
        try:
            input_hash = sha256_file(INPUT_FILE)
        except OSError as exc:
            record("input_hash", False, str(exc))

    # --------------------------------------------------------
    # CONFIGURATION VALIDATION
    # --------------------------------------------------------

    format_value = str(
        values.get("FORMAT", "")
    ).strip().lower()

    format_valid = format_value in {"short", "full"}

    record(
        "format",
        format_valid,
        (
            f"FORMAT={format_value!r}; only short or full is accepted."
        ),
    )

    story_text = str(values.get("STORY_TEXT", "")).strip()
    topic = str(values.get("TOPIC", "")).strip()
    title = str(values.get("TITLE", "")).strip()

    source_present = bool(story_text or topic or title)

    record(
        "story_source",
        source_present,
        (
            "TOPIC, TITLE or STORY_TEXT is available."
            if source_present
            else "Provide TOPIC, TITLE or STORY_TEXT."
        ),
    )

    if story_text:
        record(
            "story_text_multiline",
            True,
            f"Complete supplied story read: {len(story_text.split())} words.",
        )

    for key in ("PARTS", "SCENES"):
        raw = values.get(key, "")

        try:
            number = int(str(raw).strip())
            valid = 1 <= number <= 500
        except (TypeError, ValueError):
            number = None
            valid = False

        record(
            f"input_{key.lower()}",
            valid,
            f"{key}={number!r}; expected an integer from 1 to 500.",
        )

    try:
        from input_config import load_and_validate

        validated_config = load_and_validate(INPUT_FILE)

        record(
            "input_config_validation",
            True,
            "scripts/input_config.py accepted Input/topic.txt.",
        )

        format_value = validated_config["FORMAT"]

        for key in (
            "TTS_FALLBACK_ENABLED",
            "TTS_ALLOW_PAID",
            "ALLOW_BILLABLE_PROVIDERS",
            "IMAGE_FALLBACK_ENABLED",
            "IMAGE_ALLOW_BILLABLE",
        ):
            value = parse_bool(
                validated_config.get(key, False),
                key,
            )

            record(
                f"config_{key.lower()}",
                True,
                f"{key}={value}.",
                severity="warning" if not value else "error",
            )

        tts_order = validated_config.get(
            "TTS_PROVIDER_ORDER",
            [],
        )

        record(
            "tts_provider_order",
            bool(tts_order),
            (
                "Configured TTS fallback order: "
                + " -> ".join(tts_order)
                if tts_order
                else "No TTS provider is configured."
            ),
        )

        image_order = validated_config.get(
            "IMAGE_PROVIDER_ORDER",
            [],
        )

        record(
            "image_provider_order",
            bool(image_order),
            (
                "Configured image fallback order: "
                + " -> ".join(image_order)
                if image_order
                else "No image provider is configured."
            ),
        )

    except Exception as exc:
        validated_config = {}
        record(
            "input_config_validation",
            False,
            f"Input configuration validation failed: {exc}",
        )

    # --------------------------------------------------------
    # REQUIRED FILES AND PYTHON SYNTAX
    # --------------------------------------------------------

    required_paths = [
        SCRIPTS / name
        for name in REQUIRED_SCRIPTS
    ]

    for path in required_paths:
        exists = path.is_file() and path.stat().st_size > 0

        record(
            f"required_script:{path.name}",
            exists,
            (
                "Script exists and is non-empty."
                if exists
                else f"Missing or empty script: {path.relative_to(ROOT)}"
            ),
        )

    syntax_count = 0

    for path in required_paths:
        if not path.is_file():
            continue

        passed, message = check_python_syntax(path)

        record(
            f"python_syntax:{path.name}",
            passed,
            message,
        )

        syntax_count += 1

    # --------------------------------------------------------
    # WORKFLOW FILE AND SCRIPT ALIGNMENT
    # --------------------------------------------------------

    if not WORKFLOW_FILE.is_file():
        workflow_source = ""
        record(
            "workflow_file",
            False,
            "Photo Motion workflow file is missing.",
        )
    else:
        try:
            workflow_source = WORKFLOW_FILE.read_text(
                encoding="utf-8"
            )

            record(
                "workflow_file",
                bool(workflow_source.strip()),
                "Photo Motion workflow file exists and is readable.",
            )
        except (OSError, UnicodeError) as exc:
            workflow_source = ""
            record("workflow_file", False, str(exc))

    actual_commands = extract_workflow_commands(
        workflow_source
    )

    workflow_order_valid, workflow_errors = (
        check_workflow_order(actual_commands)
    )

    record(
        "workflow_script_order",
        workflow_order_valid,
        (
            "All required pipeline scripts are invoked in the expected order."
            if workflow_order_valid
            else " | ".join(workflow_errors)
        ),
    )

    for name in REQUIRED_SCRIPTS:
        if name == "input_config.py":
            continue

        relative = f"scripts/{name}"

        if relative not in actual_commands:
            record(
                f"workflow_script:{name}",
                False,
                f"{relative} is not invoked by the workflow.",
                severity="warning",
            )

    # --------------------------------------------------------
    # OBSOLETE PIPELINE REFERENCES
    # --------------------------------------------------------

    obsolete = inspect_obsolete_references(
        workflow_source,
        required_paths,
    )

    record(
        "photo_motion_only",
        not obsolete,
        (
            "No obsolete I2V/Wan2GP references found in active pipeline files."
            if not obsolete
            else " | ".join(obsolete[:20])
        ),
    )

    # --------------------------------------------------------
    # REQUIRED PYTHON DEPENDENCIES
    # --------------------------------------------------------

    for module in REQUIRED_PYTHON_MODULES:
        passed, message = check_module(module)

        record(
            f"python_dependency:{module}",
            passed,
            message,
            severity="warning",
        )

    for module in OPTIONAL_PYTHON_MODULES:
        passed, message = check_module(module)

        record(
            f"optional_dependency:{module}",
            passed,
            message,
            severity="warning",
        )

    # --------------------------------------------------------
    # PHOTO MOTION RUNTIME DEPENDENCIES
    # --------------------------------------------------------

    import shutil

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")

    record(
        "ffmpeg",
        bool(ffmpeg_path),
        (
            f"FFmpeg available at {ffmpeg_path}."
            if ffmpeg_path
            else "FFmpeg is not installed or not on PATH."
        ),
        severity="warning",
    )

    record(
        "ffprobe",
        bool(ffprobe_path),
        (
            f"FFprobe available at {ffprobe_path}."
            if ffprobe_path
            else "FFprobe is not installed or not on PATH."
        ),
        severity="warning",
    )

    # --------------------------------------------------------
    # STORY LENGTH POLICY
    # --------------------------------------------------------

    policy = None

    try:
        from story_length_policy import build_length_policy

        source_words = len(story_text.split())

        policy = build_length_policy(
            format_type=format_value,
            story_length=values.get("STORY_LENGTH", "auto"),
            parts=values.get("PARTS", 1),
            scenes_per_part=values.get("SCENES", 13),
            scene_duration=values.get(
                "SCENE_DURATION",
                "auto",
            ),
            source_word_count=source_words,
        )

        record(
            "story_length_policy",
            True,
            (
                f"Format={policy['format']}; "
                f"parts={policy['parts_count']}; "
                f"scenes={policy['total_scenes']}; "
                f"target_words={policy['target_total_words']}."
            ),
        )

        for index, warning in enumerate(
            policy.get("warnings", []),
            start=1,
        ):
            record(
                f"story_length_warning_{index}",
                False,
                warning,
                severity="warning",
            )

    except Exception as exc:
        record(
            "story_length_policy",
            False,
            str(exc),
        )

    # --------------------------------------------------------
    # SAVED MODEL CONFIGURATION
    # --------------------------------------------------------

    selected_model_file = (
        OUTPUT_DIR / "config" / "selected_model.json"
    )

    if selected_model_file.is_file():
        try:
            model_data = json.loads(
                selected_model_file.read_text(
                    encoding="utf-8"
                )
            )

            valid_model = (
                isinstance(model_data, dict)
                and isinstance(model_data.get("model"), str)
                and bool(model_data["model"].strip())
            )

            record(
                "saved_gemini_model",
                valid_model,
                (
                    "Saved Gemini model ID is present; "
                    "provider availability is not tested."
                    if valid_model
                    else "Saved Gemini model JSON is invalid."
                ),
                severity="warning",
            )

        except (OSError, json.JSONDecodeError) as exc:
            record(
                "saved_gemini_model",
                False,
                str(exc),
                severity="warning",
            )
    else:
        record(
            "saved_gemini_model",
            False,
            "No previously selected Gemini model file exists.",
            severity="warning",
        )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    failed_checks = [
        item for item in checks
        if not item["passed"]
    ]

    report = {
        "report_version": VERSION,
        "created_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "repository_root": str(ROOT),
        "input_file": str(INPUT_FILE.relative_to(ROOT)),
        "input_sha256": input_hash,
        "workflow_file": str(
            WORKFLOW_FILE.relative_to(ROOT)
        ),
        "format": format_value or None,
        "api_calls_made": 0,
        "generation_performed": False,
        "python_files_syntax_checked": syntax_count,
        "workflow_python_commands": actual_commands,
        "calculated_story_length_policy": policy,
        "summary": {
            "checks_total": len(checks),
            "checks_passed": sum(
                item["passed"] for item in checks
            ),
            "checks_failed": len(failed_checks),
            "errors": errors,
            "warnings": warnings,
            "ready_to_run": not errors,
        },
        "checks": checks,
    }

    try:
        write_report(report)
        log("Report:", REPORT_FILE)
    except Exception as exc:
        log("REPORT WRITE FAILED:", exc)
        errors.append(f"report_write: {exc}")

    log("=" * 58)
    log("PREFLIGHT SUMMARY")
    log("Checks:", len(checks))
    log("Passed:", sum(item["passed"] for item in checks))
    log("Failed:", len(failed_checks))
    log("Errors:", len(errors))
    log("Warnings:", len(warnings))

    if errors:
        log("PREFLIGHT FAILED — workflow generation must not start.")
        return 1

    log(
        "PREFLIGHT PASSED — this does not prove that external "
        "providers or the complete video render will succeed."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Preflight cancelled.", file=sys.stderr)
        raise SystemExit(130)