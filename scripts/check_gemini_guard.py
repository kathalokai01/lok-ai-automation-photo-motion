#!/usr/bin/env python3
"""Static audit of Gemini billing guard integration; never calls APIs."""

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

VERSION = "gemini-guard-audit-v2"

NETWORK_FUNCTIONS = {
    "urlopen",
    "post",
    "request",
    "generate_content",
    "generatecontent",
    "generate",
    "predict",
}

GEMINI_MARKERS = (
    "generativelanguage.googleapis.com",
    "gemini",
    "generateContent",
    "generate_content",
)

GUARD_FUNCTION = "require_gemini_generation"


def atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def dotted_name(node: ast.AST) -> str:
    """Convert a callable expression into a dotted name."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return ""


def inspect_guard() -> dict[str, Any]:
    result: dict[str, Any] = {
        "file_exists": GUARD_FILE.is_file(),
        "syntax_valid": False,
        "has_guard_function": False,
        "has_explicit_opt_in": False,
        "has_key_check": False,
        "error": None,
    }

    if not GUARD_FILE.is_file():
        result["error"] = "Gemini guard file is missing."
        return result

    try:
        source = read_source(GUARD_FILE)
        tree = ast.parse(source, filename=str(GUARD_FILE))
        result["syntax_valid"] = True

        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        result["has_guard_function"] = (
            GUARD_FUNCTION in functions
        )
        result["has_explicit_opt_in"] = (
            "I_ACCEPT_POTENTIAL_GEMINI_CHARGES" in source
            and "ALLOW_GEMINI_GENERATION" in source
        )

        guard_node = functions.get(GUARD_FUNCTION)
        if guard_node:
            guard_source = ast.get_source_segment(source, guard_node) or ""
            result["has_key_check"] = (
                "api_key" in guard_source
                and "raise" in guard_source
            )

    except (OSError, UnicodeError, SyntaxError) as exc:
        result["error"] = str(exc)

    return result


def inspect_script(path: Path) -> dict[str, Any]:
    relative = path.relative_to(ROOT).as_posix()

    result: dict[str, Any] = {
        "file": relative,
        "syntax_valid": False,
        "imports_guard": False,
        "calls_guard": False,
        "network_call_sites": [],
        "gemini_markers": [],
        "status": "NOT_VERIFIED",
        "error": None,
    }

    try:
        source = read_source(path)
        tree = ast.parse(source, filename=str(path))
        result["syntax_valid"] = True

        imports_guard = False
        calls_guard = []
        network_calls = []
        gemini_markers = [
            marker for marker in GEMINI_MARKERS if marker in source
        ]

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "gemini_api_guard":
                    if any(
                        alias.name == GUARD_FUNCTION
                        for alias in node.names
                    ):
                        imports_guard = True

            elif isinstance(node, ast.Import):
                if any(
                    alias.name == "gemini_api_guard"
                    for alias in node.names
                ):
                    imports_guard = True

            elif isinstance(node, ast.Call):
                name = dotted_name(node.func)
                leaf = name.rsplit(".", 1)[-1].lower()

                if leaf == GUARD_FUNCTION.lower():
                    calls_guard.append(node.lineno)

                if leaf in NETWORK_FUNCTIONS:
                    network_calls.append({
                        "line": node.lineno,
                        "call": name[:160],
                    })

        result["imports_guard"] = imports_guard
        result["calls_guard"] = bool(calls_guard)
        result["guard_call_lines"] = sorted(set(calls_guard))
        result["network_call_sites"] = sorted(
            network_calls,
            key=lambda item: item["line"],
        )
        result["gemini_markers"] = gemini_markers

        if not gemini_markers:
            result["status"] = "NO_GEMINI_MARKER"
        elif not network_calls:
            result["status"] = "REVIEW_REQUIRED_NO_DETECTED_NETWORK_CALL"
        elif not imports_guard or not calls_guard:
            result["status"] = "POTENTIAL_UNGUARDED_GEMINI_PATH"
        else:
            # Presence is not proof that every request is guarded.
            result["status"] = "MANUAL_REVIEW_REQUIRED"

    except (OSError, UnicodeError, SyntaxError) as exc:
        result["status"] = "ERROR"
        result["error"] = str(exc)

    return result


def main() -> int:
    if not SCRIPTS.is_dir():
        print("ERROR: scripts/ directory is missing.")
        return 1

    guard = inspect_guard()
    files = sorted(SCRIPTS.glob("*.py"))
    script_reports = [inspect_script(path) for path in files]

    potential_paths = [
        item for item in script_reports
        if item["status"] in {
            "POTENTIAL_UNGUARDED_GEMINI_PATH",
            "MANUAL_REVIEW_REQUIRED",
            "REVIEW_REQUIRED_NO_DETECTED_NETWORK_CALL",
            "ERROR",
        }
    ]

    unguarded_paths = [
        item["file"] for item in script_reports
        if item["status"] == "POTENTIAL_UNGUARDED_GEMINI_PATH"
    ]

    syntax_errors = [
        item["file"] for item in script_reports
        if not item["syntax_valid"]
    ]

    guard_ready = all((
        guard["file_exists"],
        guard["syntax_valid"],
        guard["has_guard_function"],
        guard["has_explicit_opt_in"],
        guard["has_key_check"],
    ))

    report = {
        "version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "network_requests_made": 0,
        "generation_performed": False,
        "guard": guard,
        "scripts_scanned": len(script_reports),
        "scripts": script_reports,
        "summary": {
            "guard_definition_ready": guard_ready,
            "potential_gemini_paths": len(potential_paths),
            "potentially_unguarded_paths": unguarded_paths,
            "syntax_errors": syntax_errors,
            "fully_verified": False,
            "note": (
                "Static scanning cannot prove runtime protection. "
                "Review every potential Gemini request path, including "
                "indirect SDK calls, imported helpers, subprocesses, "
                "and dynamically constructed URLs. This audit sends "
                "no network requests."
            ),
        },
    }

    atomic_json_write(REPORT_FILE, report)

    print("===== GEMINI GUARD STATIC AUDIT =====")
    print("Network requests made: 0")
    print("Generation performed: False")
    print("Guard definition ready:", guard_ready)
    print("Python scripts scanned:", len(script_reports))
    print("Potential paths requiring review:", len(potential_paths))

    for item in script_reports:
        if item["status"] != "NO_GEMINI_MARKER":
            print(
                f"{item['status']}: {item['file']} "
                f"(guard_import={item['imports_guard']}, "
                f"guard_call={item['calls_guard']}, "
                f"network_sites={len(item['network_call_sites'])})"
            )

    print("Report:", REPORT_FILE.relative_to(ROOT))

    if not guard_ready or unguarded_paths or syntax_errors:
        print("AUDIT RESULT: FAIL — inspect the report before workflow run.")
        return 1

    if potential_paths:
        print(
            "AUDIT RESULT: REVIEW REQUIRED — static checks cannot certify "
            "billing protection."
        )
        return 1

    print(
        "AUDIT RESULT: no potential Gemini paths detected by these "
        "heuristics; runtime protection is still not proven."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Audit cancelled.", file=sys.stderr)
        raise SystemExit(130)