#!/usr/bin/env python3
"""No-API preflight validation for Katha Lok AI Photo Motion."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
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

VERSION = "photo-motion-preflight-v50"

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

REQUIRED_RUNTIME_COMMANDS = (
    "ffmpeg",
    "ffprobe",
    "fc-match",
)

TRUE_VALUES = {"true", "yes", "on", "1", "enabled"}
FALSE_VALUES = {"false", "no", "off", "0", "disabled"}

PROVIDER_TTS = {
    "fish_audio",
    "gemini_tts",
    "edge_tts",
    "piper",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
}

PROVIDER_IMAGE = {
    "huggingface",
    "pollinations",
    "gemini",
    "replicate",
}


def log(*values: Any) -> None:
    print(*values, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def relative(path: Path) -> str:
    resolved = path.resolve()

    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError(
            f"Path escapes repository root: {resolved}"
        )

    return resolved.relative_to(ROOT.resolve()).as_posix()


def parse_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in TRUE_VALUES:
        return True

    if text in FALSE_VALUES:
        return False

    raise ValueError(
        f"{key} must be true or false; found {value!r}"
    )


def read_input_values(path: Path) -> dict[str, Any]:
    """Read Input/topic.txt, including quoted multiline STORY_TEXT."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Input file is missing: {relative(path)}"
        )

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
                    trailing = remainder[
                        end + len(delimiter):
                    ].strip()

                    if trailing and not trailing.startswith("#"):
                        raise ValueError(
                            "Unexpected text after STORY_TEXT delimiter."
                        )

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

    temporary = REPORT_FILE.with_name(
        REPORT_FILE.name + ".tmp"
    )

    try:
        temporary.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(REPORT_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def check_python_syntax(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        return True, "Python syntax valid."
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, str(exc)


def check_module(module_name: str) -> tuple[bool, str]:
    try:
        available = (
            importlib.util.find_spec(module_name) is not None
        )
    except (
        ImportError,
        ModuleNotFoundError,
        ValueError,
        AttributeError,
    ):
        available = False

    if available:
        return True, "Python module is installed."

    return False, f"Python module is not installed: {module_name}"


def extract_workflow_commands(source: str) -> list[str]:
    """Extract direct Python script calls from workflow YAML."""
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

    errors = []
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

    for duplicate in sorted({
        command
        for command in actual_commands
        if actual_commands.count(command) > 1
    }):
        errors.append(
            f"Workflow invokes a script more than once: {duplicate}"
        )

    return not errors, errors


def inspect_obsolete_references(
    workflow_source: str,
    script_paths: list[Path],
) -> list[str]:
    problems = [
        (WORKFLOW_FILE, workflow_source),
    ]

    for path in script_paths:
        if not path.is_file():
            continue

        try:
            problems.append((
                path,
                path.read_text(encoding="utf-8"),
            ))
        except (OSError, UnicodeError):
            continue

    errors = []

    for path, source in problems:
        for line_number, line in enumerate(
            source.splitlines(),
            start=1,
        ):
            normalized = line.lower()

            if any(
                term in normalized
                for term in FORBIDDEN_TERMS
            ):
                errors.append(
                    f"{relative(path)}:{line_number}: "
                    "obsolete Wan2GP reference"
                )

            if re.search(r"\bi2v\b", normalized):
                errors.append(
                    f"{relative(path)}:{line_number}: "
                    "obsolete I2V reference"
                )

    return errors


def extract_workflow_env(
    source: str,
) -> dict[str, str]:
    """Read literal job-level env values without evaluating expressions."""
    result = {}
    in_env = False
    env_indent = None

    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if stripped == "env:":
            in_env = True
            env_indent = indent
            continue

        if in_env:
            if indent <= env_indent:
                in_env = False
                env_indent = None
                continue

            match = re.match(
                r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$",
                stripped,
            )

            if match:
                key, value = match.groups()
                result[key] = value.strip().strip("'\"")

    return result


def check_workflow_structure(
    source: str,
) -> tuple[bool, list[str], dict[str, str]]:
    errors = []

    if not source.strip():
        return False, ["Workflow file is empty."], {}

    if not re.search(
        r"(?m)^on:\s*(?:\n|$)",
        source,
    ):
        # GitHub YAML parsers may treat "on" as a boolean key;
        # this text-level check only verifies the authored trigger.
        if not re.search(
            r"(?m)^on\s*:\s*$",
            source,
        ):
            errors.append("Workflow trigger block 'on:' is missing.")

    if not re.search(
        r"(?m)^\s{2}workflow_dispatch\s*:\s*$",
        source,
    ):
        errors.append(
            "workflow_dispatch trigger is missing."
        )

    if not re.search(
        r"(?m)^\s*permissions\s*:",
        source,
    ):
        errors.append(
            "Explicit workflow permissions block is missing."
        )

    if not re.search(
        r"(?m)^\s*runs-on:\s*ubuntu-latest\s*$",
        source,
    ):
        errors.append(
            "Expected ubuntu-latest runner is not configured."
        )

    if not re.search(
        r"(?m)^\s*python-version:\s*[\"']?3\.11",
        source,
    ):
        errors.append(
            "Python 3.11 setup is not detected."
        )

    if not re.search(
        r"(?m)^\s*uses:\s*actions/checkout@v4\s*$",
        source,
    ):
        errors.append(
            "actions/checkout@v4 is missing."
        )

    if not re.search(
        r"(?m)^\s*uses:\s*actions/setup-python@v5\s*$",
        source,
    ):
        errors.append(
            "actions/setup-python@v5 is missing."
        )

    env = extract_workflow_env(source)

    return not errors, errors, env


def validate_provider_order(
    config: dict[str, Any],
    key: str,
    allowed: set[str],
) -> tuple[bool, list[str], str]:
    value = config.get(key, [])

    if not isinstance(value, (list, tuple)):
        return False, [], f"{key} is not a normalized provider list."

    order = [str(item).strip().lower() for item in value]

    if not order:
        return False, [], f"{key} is empty."

    if any(not item for item in order):
        return False, [], f"{key} contains an empty provider."

    if len(order) != len(set(order)):
        return False, order, f"{key} contains duplicate providers."

    invalid = [item for item in order if item not in allowed]

    if invalid:
        return (
            False,
            order,
            f"{key} contains unsupported providers: {invalid}",
        )

    return True, order, " -> ".join(order)


def validate_three_primary_tts(
    config: dict[str, Any],
) -> tuple[bool, str]:
    main = [
        str(config.get(f"TTS_MAIN_{index}", "")).strip().lower()
        for index in (1, 2, 3)
    ]

    if any(not value for value in main):
        return False, "All three primary TTS providers must be set."

    from input_config import normalize_provider

    normalized = [
        normalize_provider(value)
        for value in main
    ]

    if len(set(normalized)) != 3:
        return False, "The three primary TTS providers must be unique."

    invalid = [
        value for value in normalized
        if value not in PROVIDER_TTS
    ]

    if invalid:
        return False, f"Unsupported primary TTS providers: {invalid}"

    actual_order = config.get("TTS_PROVIDER_ORDER", [])

    if actual_order[:3] != normalized:
        return (
            False,
            "Normalized TTS fallback order does not preserve "
            "the three selected primary providers.",
        )

    return True, "Three primary TTS providers and their order are valid."


def validate_image_primary_order(
    config: dict[str, Any],
) -> tuple[bool, list[str], str]:
    return validate_provider_order(
        config,
        "IMAGE_PROVIDER_ORDER",
        PROVIDER_IMAGE,
    )

def validate_story_length_policy(
    values: dict[str, Any],
    format_value: str,
    record,
) -> dict | None:
    try:
        from story_length_policy import build_length_policy

        source_text = str(values.get("STORY_TEXT", "")).strip()
        source_words = len(source_text.split())

        policy = build_length_policy(
            format_type=format_value,
            story_length=values.get("STORY_LENGTH", "auto"),
            parts=values.get("PARTS", 1),
            scenes_per_part=values.get("SCENES", 10),
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
                f"FORMAT={policy['format']}; "
                f"parts={policy['parts_count']}; "
                f"configured_parts={policy['configured_parts_count']}; "
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
                str(warning),
                severity="warning",
            )

        return policy

    except Exception as exc:
        record(
            "story_length_policy",
            False,
            str(exc),
        )
        return None


def validate_saved_model(record) -> dict | None:
    path = OUTPUT_DIR / "config" / "selected_model.json"

    if not path.is_file():
        record(
            "saved_gemini_model",
            False,
            "No saved Gemini model selection exists yet.",
            severity="warning",
        )
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))

        if not isinstance(data, dict):
            raise ValueError("Selected model JSON must be an object.")

        model = data.get("model")

        if not isinstance(model, str) or not model.strip():
            raise ValueError("Selected model ID is missing.")

        status = data.get("status")

        if status is not None and status not in {
            "selected",
            "completed",
            "success",
            "ready",
        }:
            raise ValueError(
                f"Unexpected selected-model status: {status!r}"
            )

        record(
            "saved_gemini_model",
            True,
            (
                f"Saved model ID exists: {model.strip()}. "
                "Provider availability is not tested."
            ),
            severity="warning",
        )

        return {
            "model": model.strip(),
            "status": status,
            "sha256": sha256_file(path),
        }

    except Exception as exc:
        record(
            "saved_gemini_model",
            False,
            str(exc),
            severity="warning",
        )
        return None


def validate_existing_output_paths(record) -> dict:
    """Detect unsafe symlinks and malformed output directory entries."""
    if not OUTPUT_DIR.exists():
        record(
            "output_directory",
            True,
            "Output directory does not exist yet; fresh run is possible.",
            severity="warning",
        )
        return {
            "exists": False,
            "files": 0,
            "bytes": 0,
        }

    if not OUTPUT_DIR.is_dir():
        record(
            "output_directory",
            False,
            "output exists but is not a directory.",
        )
        return {
            "exists": True,
            "files": 0,
            "bytes": 0,
        }

    file_count = 0
    total_bytes = 0
    errors = []

    for path in OUTPUT_DIR.rglob("*"):
        try:
            if path.is_symlink():
                target = path.resolve()

                if not target.is_relative_to(OUTPUT_DIR.resolve()):
                    errors.append(
                        f"Output symlink escapes output/: {relative(path)}"
                    )
                    continue

            if path.is_file():
                file_count += 1
                total_bytes += path.stat().st_size

        except OSError as exc:
            errors.append(f"{path}: {exc}")

    record(
        "output_directory",
        not errors,
        (
            f"Existing output inspected: {file_count} files, "
            f"{total_bytes} bytes."
            if not errors
            else " | ".join(errors[:10])
        ),
    )

    return {
        "exists": True,
        "files": file_count,
        "bytes": total_bytes,
        "errors": errors,
    }


def inspect_workflow_secrets(
    workflow_source: str,
    record,
) -> dict:
    """Inspect referenced secret names, never print secret values."""
    names = sorted(set(re.findall(
        r"\bsecrets\.([A-Z][A-Z0-9_]*)",
        workflow_source,
    )))

    expected = {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "FISH_API_KEY",
        "FISH_AUDIO_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
        "FINEVOICE_API_KEY",
        "MINIMAX_API_KEY",
        "HF_TOKEN",
        "POLLINATIONS_API_KEY",
        "REPLICATE_API_TOKEN",
    }

    found = sorted(expected.intersection(names))

    record(
        "provider_secret_references",
        True,
        (
            f"Workflow references {len(names)} secret names. "
            "Secret values and actual GitHub secret availability "
            "cannot be inspected by this local preflight."
        ),
        severity="warning",
    )

    return {
        "referenced_secret_names": names,
        "recognized_provider_secret_names": found,
        "secret_values_read": False,
    }


def inspect_workflow_python_calls(
    actual_commands: list[str],
    record,
) -> None:
    unknown = []

    for command in actual_commands:
        relative_path = command.removeprefix("scripts/")
        path = SCRIPTS / relative_path

        if not path.is_file():
            unknown.append(command)

    record(
        "workflow_python_targets",
        not unknown,
        (
            "All directly invoked Python scripts exist."
            if not unknown
            else f"Missing workflow Python targets: {unknown}"
        ),
    )


def validate_runtime_commands(record) -> dict:
    result = {}

    for command in REQUIRED_RUNTIME_COMMANDS:
        location = shutil.which(command)
        passed = bool(location)

        result[command] = location

        record(
            f"runtime_command:{command}",
            passed,
            (
                f"Available at {location}."
                if passed
                else f"{command} is not installed or not on PATH."
            ),
            severity="warning",
        )

    return result


def validate_dependencies(record) -> dict:
    results = {}

    for module in REQUIRED_PYTHON_MODULES:
        passed, message = check_module(module)
        results[module] = passed

        record(
            f"python_dependency:{module}",
            passed,
            message,
            severity="warning",
        )

    for module in OPTIONAL_PYTHON_MODULES:
        passed, message = check_module(module)
        results[module] = passed

        record(
            f"optional_dependency:{module}",
            passed,
            message,
            severity="warning",
        )

    return results


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

    log("=" * 65)
    log("KATHA LOK AI — PHOTO MOTION PREFLIGHT")
    log("NO GENERATION API CALLS")
    log("VERSION:", VERSION)
    log("=" * 65)

    values: dict[str, Any] = {}
    validated_config: dict[str, Any] = {}
    input_hash = None
    workflow_source = ""
    actual_commands: list[str] = []
    policy = None
    model_info = None

    # --------------------------------------------------------
    # 1. INPUT FILE
    # --------------------------------------------------------

    try:
        values = read_input_values(INPUT_FILE)

        input_hash = sha256_file(INPUT_FILE)

        record(
            "input_file",
            True,
            (
                f"{relative(INPUT_FILE)} exists and is readable; "
                f"SHA256={input_hash}."
            ),
        )

    except Exception as exc:
        record(
            "input_file",
            False,
            str(exc),
        )

    # --------------------------------------------------------
    # 2. CANONICAL CONFIGURATION VALIDATION
    # --------------------------------------------------------

    try:
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

        validated_config = load_and_validate(INPUT_FILE)

        format_value = get_format(validated_config)
        configured_parts = get_parts(validated_config)
        scenes = get_scenes(validated_config)
        fps = get_fps(validated_config)

        record(
            "input_config_validation",
            True,
            (
                "input_config.load_and_validate accepted "
                "Input/topic.txt."
            ),
        )

        record(
            "format",
            format_value in {"short", "full"},
            f"FORMAT={format_value}.",
        )

        record(
            "part_scene_settings",
            configured_parts >= 1 and scenes >= 1,
            (
                f"PARTS={configured_parts}; "
                f"SCENES={scenes}; "
                f"configured scene count="
                f"{configured_parts * scenes}."
            ),
        )

        record(
            "fps",
            1 <= fps <= 60,
            f"FPS={fps}; supported range is 1–60.",
        )

        story_text = str(
            validated_config.get("STORY_TEXT", "")
        ).strip()

        topic = str(
            validated_config.get("TOPIC", "")
        ).strip()

        title = str(
            validated_config.get("TITLE", "")
        ).strip()

        source_present = bool(story_text or topic or title)

        record(
            "story_source",
            source_present,
            (
                "TOPIC, TITLE or STORY_TEXT is available."
                if source_present
                else "Provide TOPIC or STORY_TEXT."
            ),
        )

        record(
            "story_text_integrity",
            (
                not story_text
                or validated_config.get(
                    "PRESERVE_STORY_TEXT",
                    True,
                )
            ),
            (
                f"Supplied story contains {len(story_text.split())} words; "
                "PRESERVE_STORY_TEXT is enabled."
                if story_text
                else "No pasted story; topic/title generation is allowed."
            ),
        )

        record(
            "caption_settings",
            (
                validated_config.get("CAPTIONS")
                in {"hindi", "english", "hinglish"}
                and validated_config.get("CAPTION_FONT_SIZE") is not None
            ),
            (
                f"CAPTIONS={validated_config.get('CAPTIONS')}; "
                f"FONT={validated_config.get('CAPTION_FONT')}; "
                f"SIZE={validated_config.get('CAPTION_FONT_SIZE')}."
            ),
        )

        record(
            "resume_settings",
            (
                validated_config.get("RESUME_ENABLED") is True
                and validated_config.get(
                    "SAVE_CHECKPOINT_AFTER_EACH_SCENE"
                ) is True
                and validated_config.get(
                    "SKIP_COMPLETED_SCENES"
                ) is True
            ),
            (
                "Resume, per-scene checkpoints and completed-scene "
                "reuse are enabled."
                if (
                    validated_config.get("RESUME_ENABLED") is True
                    and validated_config.get(
                        "SAVE_CHECKPOINT_AFTER_EACH_SCENE"
                    ) is True
                    and validated_config.get(
                        "SKIP_COMPLETED_SCENES"
                    ) is True
                )
                else "One or more resume/checkpoint protections are disabled."
            ),
            severity="warning",
        )

        # ----------------------------------------------------
        # 3. EXACT CONFIGURED PROVIDER ORDER
        # ----------------------------------------------------

        tts_order = get_tts_provider_order(validated_config)

        tts_valid, tts_message = validate_three_primary_tts(
            validated_config
        )

        record(
            "tts_primary_provider_order",
            tts_valid,
            tts_message,
        )

        record(
            "tts_fallback_order",
            bool(tts_order)
            and len(tts_order) == len(set(tts_order))
            and all(item in PROVIDER_TTS for item in tts_order),
            (
                "Configured TTS order: "
                + " -> ".join(tts_order)
            ),
        )

        image_order = get_image_provider_order(
            validated_config
        )

        image_valid, image_order, image_message = (
            validate_image_primary_order(validated_config)
        )

        record(
            "image_fallback_order",
            image_valid,
            (
                image_message
                + "; order="
                + " -> ".join(image_order)
            ),
        )

        fallback_tts = parse_bool(
            validated_config.get(
                "TTS_FALLBACK_ENABLED",
                True,
            ),
            "TTS_FALLBACK_ENABLED",
        )

        fallback_image = parse_bool(
            validated_config.get(
                "IMAGE_FALLBACK_ENABLED",
                True,
            ),
            "IMAGE_FALLBACK_ENABLED",
        )

        record(
            "fallback_configuration",
            fallback_tts and fallback_image,
            (
                f"TTS_FALLBACK_ENABLED={fallback_tts}; "
                f"IMAGE_FALLBACK_ENABLED={fallback_image}."
            ),
            severity="warning",
        )

        global_billable = get_global_billable_permission(
            validated_config
        )

        tts_billable = bool(
            validated_config.get("TTS_BILLABLE_ALLOWED", False)
        )

        image_billable = bool(
            validated_config.get("IMAGE_ALLOW_BILLABLE", False)
        )

        record(
            "billable_provider_permission",
            global_billable and tts_billable and image_billable,
            (
                f"GLOBAL={global_billable}; "
                f"TTS={tts_billable}; "
                f"IMAGE={image_billable}."
            ),
            severity="warning",
        )

        policy = validate_story_length_policy(
            values,
            format_value,
            record,
        )

    except Exception as exc:
        format_value = str(
            values.get("FORMAT", "")
        ).strip().lower()

        configured_parts = None
        scenes = None
        fps = None

        record(
            "input_config_validation",
            False,
            f"Canonical configuration validation failed: {exc}",
        )

    # --------------------------------------------------------
    # 4. REQUIRED SCRIPTS AND PYTHON SYNTAX
    # --------------------------------------------------------

    all_required = list(REQUIRED_SCRIPTS)

    if "pipeline_preflight.py" not in all_required:
        all_required.append("pipeline_preflight.py")

    required_paths = [
        SCRIPTS / name
        for name in all_required
    ]

    missing_scripts = []

    for path in required_paths:
        exists = (
            path.is_file()
            and path.stat().st_size > 0
        )

        if not exists:
            missing_scripts.append(relative(path))

        record(
            f"required_script:{path.name}",
            exists,
            (
                "Script exists and is non-empty."
                if exists
                else f"Missing or empty script: {relative(path)}"
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
    # 5. WORKFLOW STRUCTURE AND ORDER
    # --------------------------------------------------------

    if not WORKFLOW_FILE.is_file():
        record(
            "workflow_file",
            False,
            "Photo Motion GitHub Actions workflow is missing.",
        )

    else:
        try:
            workflow_source = WORKFLOW_FILE.read_text(
                encoding="utf-8-sig"
            )

            record(
                "workflow_file",
                bool(workflow_source.strip()),
                "Workflow file exists and is readable.",
            )

        except (OSError, UnicodeError) as exc:
            record(
                "workflow_file",
                False,
                str(exc),
            )

    structure_valid, structure_errors, workflow_env = (
        check_workflow_structure(workflow_source)
    )

    record(
        "workflow_structure",
        structure_valid,
        (
            "workflow_dispatch, permissions, Ubuntu runner "
            "and Python setup are present."
            if structure_valid
            else " | ".join(structure_errors)
        ),
    )

    actual_commands = extract_workflow_commands(
        workflow_source
    )

    order_valid, order_errors = check_workflow_order(
        actual_commands
    )

    record(
        "workflow_script_order",
        order_valid,
        (
            "All generation, validation and packaging entry points "
            "are invoked in the required order."
            if order_valid
            else " | ".join(order_errors)
        ),
    )

    inspect_workflow_python_calls(
        actual_commands,
        record,
    )

    # Helper scripts may be imported or executed inside another
    # script. They are not required to appear as direct workflow calls.
    helper_scripts = {
        "input_config.py",
        "depth_parallax.py",
        "install_caption_fonts.py",
        "preview_caption_fonts.py",
        "pipeline_preflight.py",
    }

    missing_workflow_calls = [
        name
        for name in EXPECTED_WORKFLOW_STEPS
        if f"scripts/{name}" not in actual_commands
    ]

    record(
        "workflow_entrypoints",
        not missing_workflow_calls,
        (
            "Every required pipeline entry point is called."
            if not missing_workflow_calls
            else "Missing direct workflow calls: "
            + ", ".join(missing_workflow_calls)
        ),
    )

    preflight_invoked = (
        "scripts/pipeline_preflight.py" in actual_commands
    )

    record(
        "preflight_workflow_integration",
        preflight_invoked,
        (
            "Workflow invokes this no-API preflight."
            if preflight_invoked
            else "Workflow does not yet invoke pipeline_preflight.py."
        ),
        severity="warning",
    )

    # --------------------------------------------------------
    # 6. BILLABLE WORKFLOW SWITCH
    # --------------------------------------------------------

    workflow_billable = workflow_env.get(
        "ALLOW_BILLABLE_PROVIDERS"
    )

    try:
        workflow_billable_value = (
            parse_bool(
                workflow_billable,
                "Workflow ALLOW_BILLABLE_PROVIDERS",
            )
            if workflow_billable is not None
            else False
        )

        workflow_billable_valid = workflow_billable_value

    except ValueError:
        workflow_billable_value = False
        workflow_billable_valid = False

    record(
        "workflow_billable_permission",
        workflow_billable_valid,
        (
            "Workflow ALLOW_BILLABLE_PROVIDERS=true."
            if workflow_billable_valid
            else (
                "Workflow ALLOW_BILLABLE_PROVIDERS is missing, "
                "false or invalid."
            )
        ),
        severity="warning",
    )

    # --------------------------------------------------------
    # 7. PHOTO MOTION ONLY
    # --------------------------------------------------------

    obsolete = inspect_obsolete_references(
        workflow_source,
        required_paths,
    )

    record(
        "photo_motion_only",
        not obsolete,
        (
            "No obsolete I2V/Wan2GP references found in "
            "the active workflow and required scripts."
            if not obsolete
            else " | ".join(obsolete[:20])
        ),
    )

    # --------------------------------------------------------
    # 8. DEPENDENCIES AND RUNTIME
    # --------------------------------------------------------

    dependency_results = validate_dependencies(record)
    runtime_results = validate_runtime_commands(record)

    # --------------------------------------------------------
    # 9. WORKFLOW SECRET REFERENCES
    # --------------------------------------------------------

    secret_report = inspect_workflow_secrets(
        workflow_source,
        record,
    )

    # --------------------------------------------------------
    # 10. SAVED MODEL AND EXISTING OUTPUT
    # --------------------------------------------------------

    model_info = validate_saved_model(record)

    output_info = validate_existing_output_paths(record)

    # --------------------------------------------------------
    # 11. FINAL REPORT
    # --------------------------------------------------------

    failed_checks = [
        item
        for item in checks
        if not item["passed"]
    ]

    error_checks = [
        item
        for item in failed_checks
        if item["severity"] == "error"
    ]

    warning_checks = [
        item
        for item in failed_checks
        if item["severity"] != "error"
    ]

    report = {
        "report_version": VERSION,
        "created_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "repository_root": str(ROOT),
        "input_file": relative(INPUT_FILE),
        "input_sha256": input_hash,
        "workflow_file": relative(WORKFLOW_FILE),
        "format": format_value or None,
        "configured_parts": configured_parts,
        "scenes_per_part": scenes,
        "fps": fps,
        "api_calls_made": 0,
        "generation_performed": False,
        "preflight_only": True,
        "python_files_syntax_checked": syntax_count,
        "required_scripts": all_required,
        "missing_scripts": missing_scripts,
        "workflow_python_commands": actual_commands,
        "workflow_preflight_invoked": preflight_invoked,
        "workflow_billable_permission": workflow_billable_value,
        "provider_configuration": {
            "tts_order": (
                validated_config.get(
                    "TTS_PROVIDER_ORDER",
                    [],
                )
                if validated_config
                else []
            ),
            "image_order": (
                validated_config.get(
                    "IMAGE_PROVIDER_ORDER",
                    [],
                )
                if validated_config
                else []
            ),
            "tts_billable_allowed": (
                validated_config.get(
                    "TTS_BILLABLE_ALLOWED",
                    False,
                )
                if validated_config
                else False
            ),
            "image_billable_allowed": (
                validated_config.get(
                    "IMAGE_ALLOW_BILLABLE",
                    False,
                )
                if validated_config
                else False
            ),
        },
        "workflow_secrets": secret_report,
        "dependencies": dependency_results,
        "runtime_commands": runtime_results,
        "saved_model": model_info,
        "existing_output": output_info,
        "calculated_story_length_policy": policy,
        "summary": {
            "checks_total": len(checks),
            "checks_passed": sum(
                item["passed"] for item in checks
            ),
            "checks_failed": len(failed_checks),
            "errors": [
                item["message"]
                for item in error_checks
            ],
            "warnings": [
                item["message"]
                for item in warning_checks
            ],
            "ready_to_run": not error_checks,
            "ready_with_warnings": (
                not error_checks and bool(warning_checks)
            ),
        },
        "checks": checks,
    }

    try:
        write_report(report)

        # Confirm the persisted report can be read and is complete.
        saved = json.loads(
            REPORT_FILE.read_text(encoding="utf-8")
        )

        if saved.get("report_version") != VERSION:
            raise ValueError(
                "Persisted report version mismatch."
            )

        if saved.get("api_calls_made") != 0:
            raise ValueError(
                "Preflight report incorrectly records API calls."
            )

        if len(saved.get("checks", [])) != len(checks):
            raise ValueError(
                "Persisted report check count mismatch."
            )

        if (
            saved.get("summary", {}).get("ready_to_run")
            is not report["summary"]["ready_to_run"]
        ):
            raise ValueError(
                "Persisted report readiness mismatch."
            )

        log("REPORT:", relative(REPORT_FILE))

    except Exception as exc:
        log("REPORT WRITE/VERIFY FAILED:", exc)
        return 1

    # --------------------------------------------------------
    # 12. SUMMARY
    # --------------------------------------------------------

    log("=" * 65)
    log("PREFLIGHT SUMMARY")
    log("Checks:", len(checks))
    log("Passed:", len(checks) - len(failed_checks))
    log("Failed:", len(failed_checks))
    log("Errors:", len(error_checks))
    log("Warnings:", len(warning_checks))
    log("API calls:", 0)
    log("Generation performed:", False)

    if error_checks:
        log(
            "PREFLIGHT FAILED — generation must not start."
        )
        log("=" * 65)
        return 1

    log(
        "PREFLIGHT PASSED — this checks local configuration, "
        "workflow structure and available files only."
    )

    if warning_checks:
        log(
            "PREFLIGHT PASSED WITH WARNINGS — review "
            "output/preflight_report.json."
        )

    log(
        "External API access, secret validity, provider quotas "
        "and complete video rendering are not verified."
    )
    log("=" * 65)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("PREFLIGHT CANCELLED", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        log("PREFLIGHT INTERNAL ERROR:", str(exc))
        raise SystemExit(1)