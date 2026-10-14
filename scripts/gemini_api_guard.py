#!/usr/bin/env python3
"""Explicit opt-in guard for potentially billable Gemini generation calls."""

from __future__ import annotations

import os
from typing import Any


# A normal "false", "true", or "yes" is deliberately not sufficient.
# The user must explicitly acknowledge possible provider charges.
REQUIRED_OPT_IN = "I_ACCEPT_POTENTIAL_GEMINI_CHARGES"

OPT_IN_ENV = "ALLOW_GEMINI_GENERATION"


class GeminiBillingBlocked(RuntimeError):
    """Raised when Gemini generation has not been explicitly authorized."""


def gemini_generation_allowed() -> bool:
    """Return True only when the exact explicit opt-in is present."""
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
    Enforce explicit opt-in before a Gemini generation request.

    Call this immediately before the first network request, not merely
    when importing this module. Cached completed outputs should be checked
    and reused before calling this function.
    """
    stage_name = str(stage or "unspecified stage").strip()

    if not gemini_generation_allowed():
        raise GeminiBillingBlocked(
            f"Gemini generation blocked at stage '{stage_name}'. "
            f"No generation request was authorized. To explicitly allow "
            f"potentially billable Gemini usage, set "
            f"{OPT_IN_ENV}={REQUIRED_OPT_IN}. "
            f"Check your provider plan and pricing before opting in."
        )

    resolved_key = (
        str(api_key or "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )

    if not resolved_key:
        raise GeminiBillingBlocked(
            f"Gemini generation at stage '{stage_name}' cannot continue: "
            "GEMINI_API_KEY or GOOGLE_API_KEY is missing."
        )

    return resolved_key


def billing_status() -> dict[str, Any]:
    """Return safe diagnostic information without exposing any API key."""
    return {
        "generation_opt_in": gemini_generation_allowed(),
        "opt_in_environment_variable": OPT_IN_ENV,
        "api_key_configured": bool(
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        ),
        "automatic_authorization": False,
        "api_key_value_exposed": False,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(billing_status(), indent=2))

    if not gemini_generation_allowed():
        raise SystemExit(
            "SAFE DEFAULT: Gemini generation is not authorized."
        )