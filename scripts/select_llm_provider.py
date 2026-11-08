#!/usr/bin/env python3
"""Select configured zero-cost LLM fallbacks without making API calls."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from llm_provider_client import (
    DEFAULT_PROVIDER_ORDER,
    SUPPORTED_PROVIDERS,
    ProviderError,
    get_provider_api_key,
    get_provider_model,
    get_provider_order,
    _env_bool,
)


OUTPUT_FILE = Path("output/config/selected_llm_provider.json")
VERSION = "llm-provider-selector-v1"

# Only these providers are eligible by default.
# Cloudflare and Mistral require explicit addition to this allowlist.
DEFAULT_ZERO_COST_ALLOWLIST = ("groq", "openrouter")


def log(message: str) -> None:
    print(f"[LLM SELECTOR] {message}", flush=True)


def get_zero_cost_allowlist() -> list[str]:
    raw = os.environ.get("LLM_ZERO_COST_PROVIDERS", "").strip()

    values = (
        re.split(r"[,;\s]+", raw)
        if raw
        else list(DEFAULT_ZERO_COST_ALLOWLIST)
    )

    allowed = []

    for value in values:
        provider = value.strip().lower()

        if not provider:
            continue

        if provider not in SUPPORTED_PROVIDERS:
            raise ProviderError(
                "configuration",
                f"Unsupported provider in LLM_ZERO_COST_PROVIDERS: {provider}",
            )

        if provider not in allowed:
            allowed.append(provider)

    return allowed


def validate_provider(provider: str) -> tuple[bool, str, str]:
    """Validate local configuration only; never contact a provider."""

    try:
        model = get_provider_model(provider)
        get_provider_api_key(provider)

        if provider == "openrouter" and not model.endswith(":free"):
            raise ProviderError(
                provider,
                "Only OpenRouter :free models are permitted.",
            )

        if provider == "cloudflare":
            if not _env_bool("CLOUDFLARE_FREE_TIER_CONFIRMED", False):
                raise ProviderError(
                    provider,
                    "Cloudflare free-tier confirmation is missing.",
                )

        if provider == "mistral":
            if not _env_bool("MISTRAL_FREE_MODE_CONFIRMED", False):
                raise ProviderError(
                    provider,
                    "Mistral free-mode confirmation is missing.",
                )

        if provider == "groq":
            if not _env_bool("GROQ_FREE_TIER_CONFIRMED", False):
                raise ProviderError(
                    provider,
                    "GROQ_FREE_TIER_CONFIRMED is missing. Confirm the account's free-tier eligibility before enabling Groq.",
                )

        return True, model, "Credentials and local free-mode settings are present."

    except ProviderError as exc:
        return False, "", str(exc)

    except Exception as exc:
        return False, "", f"{type(exc).__name__}: {exc}"


def build_selection() -> dict:
    zero_cost_only = _env_bool("LLM_ZERO_COST_ONLY", False)

    if not zero_cost_only:
        return {
            "status": "disabled",
            "enabled": False,
            "reason": "LLM_ZERO_COST_ONLY is not true.",
            "provider_order": [],
            "configured_order": [],
            "providers": [],
        }

    allowed = get_zero_cost_allowlist()
    configured_order = get_provider_order()

    # Preserve the configured order; never reorder providers by preference.
    configured_order = [
        provider
        for provider in configured_order
        if provider in allowed
    ]

    provider_records = []
    ready_order = []

    for provider in configured_order:
        ready, model, detail = validate_provider(provider)

        provider_records.append({
            "provider": provider,
            "model": model or None,
            "ready": ready,
            "detail": detail,
        })

        if ready:
            ready_order.append(provider)

    return {
        "status": "ready" if ready_order else "no_provider_ready",
        "enabled": bool(ready_order),
        "reason": (
            "Gemini remains first. These providers are fallbacks only."
            if ready_order
            else "No permitted non-Gemini provider passed local configuration checks."
        ),
        "gemini_first": True,
        "zero_cost_only": True,
        "allowlist": allowed,
        "configured_order": configured_order,
        "provider_order": ready_order,
        "providers": provider_records,
    }

def atomic_save(data: dict) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(OUTPUT_FILE.parent),
            prefix=".selected-llm-provider-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)

            json.dump(
                data,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, OUTPUT_FILE)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def verify_saved_selection(expected: dict) -> dict:
    try:
        saved = json.loads(
            OUTPUT_FILE.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot verify saved provider selection: {exc}"
        ) from exc

    if not isinstance(saved, dict):
        raise RuntimeError("Saved provider selection is not a JSON object.")

    if saved.get("provider_order") != expected.get("provider_order"):
        raise RuntimeError("Saved provider order does not match validated order.")

    if saved.get("enabled") is not expected.get("enabled"):
        raise RuntimeError("Saved provider enabled flag is inconsistent.")

    if saved.get("zero_cost_only") != expected.get("zero_cost_only"):
        raise RuntimeError("Saved zero-cost mode flag is inconsistent.")

    return saved


def main() -> int:
    log("Checking non-Gemini provider configuration.")
    log("No provider API requests will be made.")

    try:
        selection = build_selection()

        selection.update({
            "version": VERSION,
            "created_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "api_calls_made": 0,
            "generation_performed": False,
        })

        atomic_save(selection)
        saved = verify_saved_selection(selection)

    except Exception as exc:
        log(f"SELECTION ERROR: {type(exc).__name__}: {exc}")
        return 1

    log("STATUS: " + str(saved.get("status")))
    log("GEMINI FIRST: " + str(saved.get("gemini_first", True)))
    log(
        "READY FALLBACKS: "
        + (
            ", ".join(saved.get("provider_order", []))
            or "none"
        )
    )
    log("OUTPUT: " + str(OUTPUT_FILE))
    log("API CALLS: 0")

    # No ready fallback is not a pipeline error here.
    # Gemini generation remains responsible for its existing fallback order.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())