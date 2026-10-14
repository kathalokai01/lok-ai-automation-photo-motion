#!/usr/bin/env python3
"""Audit Gemini billing-guard wiring without making network requests."""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
GUARD_FILE = SCRIPTS / "gemini_api_guard.py"
REPORT_FILE = ROOT / "output" / "config" / "gemini_guard_audit.json"

VERSION = "gemini-guard-audit-v1"

GENERATION_SCRIPTS = (
    "generate_story_plan.py",
    "generate_story.py",
    "generate_ai_story.py",
    "generate_character_bible.py",
    "generate_scenes.py",
)


def read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def inspect_guard() -> dict[str, Any]:
    result: dict[str, Any] = {
        "file_exists": GUARD_FILE.is_file(),
        "syntax_valid": False,
        "has_guard_function": False,
        "has_explicit_opt_in": False,
        "error": None,
    }

    if not GUARD_FILE.is_file():
        result["error"] = "scripts/gemini_api_guard.py is missing."
        return result

    try:
        source = read_source(GUARD_FILE)
        tree = ast.parse(source, filename=str(GUARD_FILE))
        result["syntax_valid"] = True

        function_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        result["has_guard_function"] = (
            "require_gemini_generation" in function_names
        )

        result["has_explicit_opt_in"] = (
            "I_ACCEPT_POTENTIAL_GEMINI_CHARGES" in source
            and "ALLOW_GEMINI_GENERATION" in source
        )

    except (OSError, UnicodeError, SyntaxError) as exc:
        result["error"] = str(exc)

    return result


def inspect_generation_script(filename: str) -> dict[str, Any]:
    path = SCRIPTS / filename

    result: dict[str, Any] = {
        "file": f"scripts/{filename}",
        "exists": path.is_file(),
        "syntax_valid": False,
        "imports_guard": False,
        "calls_guard": False,
        "guard_before_network": False,
        "status": "NOT_VERIFIED",
        "error": None,
    }

    if not path.is_file():
        result["status"] = "MISSING"
        result["error"] = "Generation script does not exist."
        return result

    try:
        source = read_source(path)
        tree = ast.parse(source, filename=str(path))
        result["syntax_valid"] = True

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "gemini_api_guard":
                    result["imports_guard"] = any(
                        alias.name == "require_gemini_generation"
                        for alias in node.names
                    )

            elif isinstance(node, ast.Import):
                if any(
                    alias.name == "gemini_api_guard"
                    for alias in node.names
                ):
                    result["imports_guard"] = True

        result["calls_guard"] = (
            "require_gemini_generation" in source
            and result["imports_guard"]
        )

        # Conservative check: require an explicit guard call to appear
        # textually before common network-request markers. This is a
        # heuristic, not proof of execution order on every code path.
        guard_position = source.find("require_gemini_generation(")

        network_markers = (
            "urlopen(",
            ".urlopen(",
            "requests.post(",
            "requests.request(",
            "httpx.post(",
        )

        network_positions = [
            source.find(marker)
            for marker in network_markers
            if source.find(marker) >= 0
        ]

        result["guard_before_network"] = (
            result["calls_guard"]
            and guard_position >= 0
            and bool(network_positions)
            and guard_position < min(network_positions)
        )

        if result["guard_before_network"]:
            result["status"] = "HEURISTIC_PASS"
        elif result["calls_guard"]:
            result["status"] = "REVIEW_REQUIRED"
        else:
            result["status"] = "GUARD_NOT_WIRED"

    except (OSError, UnicodeError, SyntaxError) as exc:
        result["status"] = "ERROR"
        result["error"] = str(exc)

    return result


def main() -> int:
    log_lines: list[str] = []

    guard = inspect_guard()
    scripts = [
        inspect_generation_script(filename)
        for filename in GENERATION_SCRIPTS
    ]

    missing_or_unwired = [
        item["file"]
        for item in scripts
        if item["status"] in {
            "MISSING",
            "GUARD_NOT_WIRED",
            "ERROR",
        }
    ]

    review_required = [
        item["file"]
        for item in scripts
        if item["status"] == "REVIEW_REQUIRED"
    ]

    guard_ready = (
        guard["file_exists"]
        and guard["syntax_valid"]
        and guard["has_guard_function"]
        and guard["has_explicit_opt_in"]
    )

    report = {
        "version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "network_requests_made": 0,
        "generation_performed": False,
        "guard": guard,
        "generation_scripts": scripts,
        "summary": {
            "guard_definition_ready": guard_ready,
            "scripts_checked": len(scripts),
            "missing_or_unwired": missing_or_unwired,
            "review_required": review_required,
            "fully_verified": False,
            "note": (
                "Static source inspection cannot prove runtime protection. "
                "A HEURISTIC_PASS still requires manual review of every "
                "Gemini request path."
            ),
        },
    }

    atomic_json_write(REPORT_FILE, report)

    print("===== GEMINI GUARD AUDIT =====")
    print("Network requests: 0")
    print("Guard definition ready:", guard_ready)

    for item in scripts:
        print(
            f"{item['status']}: {item['file']} "
            f"(import={item['imports_guard']}, "
            f"call={item['calls_guard']}, "
            f"before_network={item['guard_before_network']})"
        )

    print("Report:", REPORT_FILE)

    if not guard_ready or missing_or_unwired or review_required:
        print("AUDIT RESULT: guard integration needs work.")
        return 1

    print(
        "AUDIT RESULT: static checks passed; "
        "runtime billing protection is not proven."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Audit cancelled.", file=sys.stderr)
        raise SystemExit(130)