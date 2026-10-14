#!/usr/bin/env python3
"""Strict opt-in guard for potentially billable Gemini generation."""

from __future__ import annotations

import json
import os
import re
from typing import Any


REQUIRED_OPT_IN = "I_ACCEPT_POTENTIAL_GEMINI_CHARGES"
OPT_IN_ENV = "ALLOW_GEMINI_GENERATION"

API_KEY_ENVIRONMENTS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)

STAGE_MAX_LENGTH = 100


class GeminiBillingBlocked(RuntimeError):
    """Raised when Gemini generation is not explicitly authorized."""


def _clean_stage(stage: str) -> str:
    """Create a short, safe stage label for diagnostic messages."""
    value = str(stage or "unspecified stage").strip()
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value[:STAGE_MAX_LENGTH] or "unspecified stage"


def _environment_api_key() -> str:
    """Return a configured API key without logging or exposing it."""
    for name in API_KEY_ENVIRONMENTS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def gemini_generation_allowed() -> bool:
    """
    Return True only for the exact explicit authorization value.

    Values such as true, yes, 1, or false never authorize generation.
    """
    return (
        os.environ.get(OPT_IN_ENV, "").strip()
        == REQUIRED_OPT_IN
    )


def require_gemini_generation(
    stage: str,
    *,
    api_key: str | None = None,
) -> str:
    """
    Require explicit authorization and a non-empty API key.

    Call immediately before a real Gemini generation request.
    Checkpoint reuse should happen before calling this function.
    """
    stage_name = _clean_stage(stage)

    if not gemini_generation_allowed():
        raise GeminiBillingBlocked(
            f"Gemini generation blocked at stage '{stage_name}'. "
            "No generation request was authorized. "
            f"To explicitly permit potentially billable generation, "
            f"set {OPT_IN_ENV}={REQUIRED_OPT_IN}. "
            "Check provider pricing and account limits first."
        )

    resolved_key = str(api_key or "").strip() or _environment_api_key()

    if not resolved_key:
        raise GeminiBillingBlocked(
            f"Gemini generation blocked at stage '{stage_name}': "
            "no API key is configured. Set GEMINI_API_KEY or "
            "GOOGLE_API_KEY after checking provider pricing."
        )

    return resolved_key


def billing_status() -> dict[str, Any]:
    """
    Return safe diagnostics without exposing API key values.
    """
    return {
        "generation_opt_in": gemini_generation_allowed(),
        "opt_in_environment_variable": OPT_IN_ENV,
        "api_key_configured": bool(_environment_api_key()),
        "automatic_authorization": False,
        "api_key_value_exposed": False,
        "network_request_performed": False,
    }


def main() -> int:
    """Print local diagnostics without making any network request."""
    print(
        json.dumps(
            billing_status(),
            ensure_ascii=False,
            indent=2,
        )
    )

    if not gemini_generation_allowed():
        print(
            "SAFE DEFAULT: Gemini generation is not authorized."
        )
        return 0

    print(
        "Explicit opt-in detected. This diagnostic did not make "
        "a network request and did not verify provider billing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())