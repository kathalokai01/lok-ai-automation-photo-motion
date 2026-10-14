#!/usr/bin/env python3
"""No-API preflight checks for the Katha Lok AI photo-motion pipeline."""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_DIR = ROOT / "output"
REPORT_FILE = OUTPUT_DIR / "preflight_report.json"

VERSION = "pipeline-preflight-v1"

REQUIRED_FILES = [
    "scripts/input_config.py",
    "scripts/select_gemini_model.py",
    "scripts/generate_story_plan.py",
    "scripts/generate_story.py",
    "scripts/generate_ai_story.py",
    "scripts/generate_character_bible.py",
    "scripts/generate_scenes.py",
    "scripts/generate_narration.py",
    "scripts/prepare_captions.py",
    "scripts/generate_tts.py",
    "scripts/prepare_captions.py",
    "scripts/generate_visuals.py",
    "scripts/free_image_generator.py",
    "scripts/generate_depth_maps.py",
    "scripts/depth_parallax.py",
    "scripts/photo_motion.py",
    "scripts/generate_sound_design.py",
    "scripts/render_video.py",
    "scripts/burn_captions.py",
    "scripts/generate_thumbnail.py",
    "scripts/validate_final_output.py",
    "scripts/package_output.py",
    "scripts/story_length_policy.py",
    "scripts/validate_story_length.py",
    "scripts/gemini_api_guard.py",
]

GENERATION_SCRIPTS = [
    "scripts/generate_story_plan.py",
    "scripts/generate_story.py",
    "scripts/generate_ai_story.py",
    "scripts/generate_character_bible.py",
    "scripts/generate_scenes.py",
]


def log(*items: Any) -> None:
    print(*items, flush=True)


def read_input_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"Input file is missing: {path}")

    values: dict[str, str] = {}

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.strip()

        # Remove a trailing comment only when it is outside quoted text.
        quote = ""
        escaped = False
        end = len(value)

        for index, char in enumerate(value):
            if escaped:
                escaped = False
                continue

            if char == "\\":
                escaped = True
                continue

            if quote:
                if char == quote:
                    quote = ""
                continue

            if char in {"'", '"'}:
                quote = char
            elif char == "#":
                end = index
                break

        value = value[:end].strip().strip("'\"")
        values[key] = value

    return values


def write_report(report: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_FILE.with_suffix(".json.tmp")

    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(REPORT_FILE)


def check_python_syntax(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        return True, "Python syntax is valid."
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, str(exc)


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
        checks.append({
            "name": name,
            "passed": passed,
            "message": message,
            "severity": severity,
        })

        if not passed:
            if severity == "error":
                errors.append(f"{name}: {message}")
            else:
                warnings.append(f"{name}: {message}")

        log(
            ("PASS" if passed else severity.upper()),
            name,
            "-",
            message,
        )

    log("===== KATHA LOK AI PREFLIGHT =====")
    log("This check makes no generation API requests.")

    try:
        values = read_input_values(INPUT_FILE)
        record("input_file", True, "Input/topic.txt was read.")
    except Exception as exc:
        record("input_file", False, str(exc))
        values = {}

    for relative in REQUIRED_FILES:
        path = ROOT / relative
        exists = path.is_file() and path.stat().st_size > 0
        record(
            f"required_file:{relative}",
            exists,
            "File exists and is non-empty."
            if exists
            else "File is missing or empty.",
        )

    syntax_checked = 0

    for relative in REQUIRED_FILES:
        if not relative.endswith(".py"):
            continue

        path = ROOT / relative
        if not path.is_file():
            continue

        passed, message = check_python_syntax(path)
        record(f"python_syntax:{relative}", passed, message)
        syntax_checked += 1

    format_value = values.get("FORMAT", "").strip().lower()
    if format_value in {"short", "shorts", "reel", "reels", "vertical"}:
        normalized_format = "short"
        format_valid = True
    elif format_value in {
        "full", "long", "longform", "long-form", "landscape", "full_video"
    }:
        normalized_format = "full"
        format_valid = True
    else:
        normalized_format = format_value or "(missing)"
        format_valid = False

    record(
        "format",
        format_valid,
        f"FORMAT resolves to {normalized_format!r}.",
    )

    if not values.get("TOPIC", "").strip() and not values.get("TITLE", "").strip():
        record(
            "topic_or_title",
            False,
            "Set TOPIC or TITLE in Input/topic.txt.",
        )
    else:
        record(
            "topic_or_title",
            True,
            "A topic or title is configured.",
        )

    for key in ("PARTS", "SCENES"):
        raw = values.get(key, "")
        try:
            number = int(raw)
            valid = 1 <= number <= 100
        except (TypeError, ValueError):
            number = None
            valid = False

        record(
            f"input_{key.lower()}",
            valid,
            f"{key}={number!r}; expected an integer from 1 to 100.",
        )

    caption_mode = values.get("CAPTIONS", "hindi").strip().lower()
    caption_aliases = {
        "hi": "hindi",
        "en": "english",
        "mixed": "hinglish",
    }
    caption_mode = caption_aliases.get(caption_mode, caption_mode)

    record(
        "caption_mode",
        caption_mode in {"hindi", "english", "hinglish"},
        f"CAPTIONS resolves to {caption_mode!r}.",
    )

    policy_file = SCRIPTS / "story_length_policy.py"
    validator_file = SCRIPTS / "validate_story_length.py"

    if policy_file.is_file() and validator_file.is_file():
        record(
            "story_length_tools",
            True,
            "Length policy and validator files are present.",
        )
    else:
        record(
            "story_length_tools",
            False,
            "Both story_length_policy.py and validate_story_length.py are required.",
        )

    guard_file = SCRIPTS / "gemini_api_guard.py"

    if guard_file.is_file():
        record(
            "gemini_guard_file",
            True,
            "The guard module exists; this does not prove generation scripts call it.",
        )

        for relative in GENERATION_SCRIPTS:
            path = ROOT / relative

            if not path.is_file():
                continue

            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                source = ""

            wired = (
                "require_gemini_generation" in source
                and "gemini_api_guard" in source
            )

            record(
                f"gemini_guard_wiring:{relative}",
                wired,
                (
                    "Guard import/call markers were found."
                    if wired
                    else "Guard is not visibly wired into this generation script yet."
                ),
                severity="warning",
            )
    else:
        record(
            "gemini_guard_file",
            False,
            "Gemini API guard file is missing.",
        )

    model_file = OUTPUT_DIR / "config" / "selected_model.json"

    if model_file.is_file():
        try:
            model_data = json.loads(model_file.read_text(encoding="utf-8"))
            valid_model = (
                isinstance(model_data, dict)
                and isinstance(model_data.get("model"), str)
                and bool(model_data["model"].strip())
            )
        except (OSError, json.JSONDecodeError):
            valid_model = False

        record(
            "saved_gemini_model",
            valid_model,
            (
                "A saved model ID exists; provider availability and billing are not verified."
                if valid_model
                else "Saved model configuration is invalid."
            ),
            severity="warning",
        )
    else:
        record(
            "saved_gemini_model",
            False,
            "No saved model yet; the selector may create an unverified local default.",
            severity="warning",
        )

    # The shared policy can be calculated without calling any provider.
    try:
        from story_length_policy import build_length_policy

        policy = build_length_policy(
            format_type=normalized_format,
            story_length=values.get("STORY_LENGTH", "auto"),
            parts=values.get("PARTS", 1),
            scenes_per_part=values.get("SCENES", 11),
            scene_duration=values.get("SCENE_DURATION", "auto"),
        )

        record(
            "story_length_policy",
            True,
            (
                f"Target {policy['target_duration_minutes']} minute(s), "
                f"{policy['target_total_words']} words, "
                f"{policy['total_scenes']} scenes."
            ),
        )
    except Exception as exc:
        policy = None
        record("story_length_policy", False, str(exc))

    report = {
        "report_version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "api_calls_made": 0,
        "generation_performed": False,
        "input_file": str(INPUT_FILE.relative_to(ROOT)),
        "python_files_syntax_checked": syntax_checked,
        "summary": {
            "checks_total": len(checks),
            "checks_failed": sum(not item["passed"] for item in checks),
            "errors": errors,
            "warnings": warnings,
            "ready_to_run": not errors,
        },
        "checks": checks,
        "calculated_story_length_policy": policy,
    }

    write_report(report)

    log("----- PREFLIGHT SUMMARY -----")
    log("Checks:", len(checks))
    log("Errors:", len(errors))
    log("Warnings:", len(warnings))
    log("Report:", REPORT_FILE)

    if warnings:
        log("Warnings must be reviewed before spending API credits.")

    if errors:
        log("Preflight failed. Do not start the video workflow yet.")
        return 1

    log("Preflight checks passed, but this is not an end-to-end video test.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Preflight cancelled.", file=sys.stderr)
        raise SystemExit(130)