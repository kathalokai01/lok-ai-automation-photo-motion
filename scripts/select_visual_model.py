#!/usr/bin/env python3
"""Select the visual model without surprise API calls.

Cloudflare model testing is disabled by default.
Set ALLOW_BILLABLE_PROVIDERS=true to explicitly permit a test request.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

from input_config import load_input_config, normalize_format


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = ROOT / "output" / "config" / "selected_visual_model.json"

MODEL = "alibaba/wan-2.6-image"
API_BASE = "https://api.cloudflare.com/client/v4/accounts"
TEST_TIMEOUT = 60
VERSION = "safe-visual-model-selector-v2"


def log(*items):
    print(*items, flush=True)


def env_true(name, default=False):
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1", "true", "yes", "y", "on"
    }


def cfg_int(config, key, default):
    try:
        value = int(config.get(key, default))
        return max(1, min(value, 3))
    except (TypeError, ValueError):
        return default


def read_existing_selection():
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_selection(
    *,
    status,
    model=MODEL,
    provider="cloudflare",
    reason="",
    tested=False,
):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": VERSION,
        "status": status,
        "model": model,
        "provider": provider,
        "selected_by": "select_visual_model.py",
        "timestamp": int(time.time()),
        "tested": tested,
        "reason": reason,
        "external_api_calls": 0,
        "paid_services_used": False,
    }

    temporary = OUTPUT_FILE.with_suffix(
        OUTPUT_FILE.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_FILE)

    return data


def test_cloudflare_model(account_id, api_token):
    """Send one opt-in model test request."""
    url = f"{API_BASE}/{account_id}/ai/run"

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL,
        "input": {
            "prompt": (
                "Photorealistic cinematic Indian environment, "
                "natural lighting, realistic live-action appearance. "
                "This is a single provider availability test."
            ),
            "size": "768x1024",
        },
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=TEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, f"Network request failed: {exc}"

    try:
        body = response.json()
    except ValueError:
        body = {"response_excerpt": response.text[:1000]}

    if not 200 <= response.status_code < 300:
        return False, (
            f"HTTP {response.status_code}: "
            + json.dumps(body, ensure_ascii=False)[:1500]
        )

    if not isinstance(body, dict) or body.get("success") is False:
        return False, (
            "Provider reported failure: "
            + json.dumps(body, ensure_ascii=False)[:1500]
        )

    result = body.get("result")
    if not isinstance(result, dict) or not result.get("image"):
        return False, (
            "No result.image returned; provider compatibility "
            "has not been confirmed."
        )

    return True, "Cloudflare returned an image result."


def main():
    log("=" * 58)
    log("SAFE VISUAL MODEL SELECTION")
    log("=" * 58)

    try:
        config = load_input_config()
    except Exception as exc:
        log("ERROR: Could not load Input/topic.txt:", exc)
        return 1

    log("FORMAT:", normalize_format(config))

    allow_billable = env_true(
        "ALLOW_BILLABLE_PROVIDERS",
        default=False,
    )

    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()

    # Safe default: never send a provider request unless explicitly allowed.
    if not allow_billable:
        existing = read_existing_selection()

        if (
            existing.get("status") == "selected"
            and existing.get("model")
            and existing.get("provider")
        ):
            log(
                "API TEST SKIPPED. Keeping the existing model selection:",
                existing["provider"],
                existing["model"],
            )
            log("No external API request was made.")
            return 0

        save_selection(
            status="skipped",
            model=None,
            provider=None,
            reason=(
                "Model testing is disabled by default. "
                "No provider was tested or selected. "
                "Configure a free provider separately, or explicitly "
                "enable a provider test after checking its pricing."
            ),
        )

        log("API TEST SKIPPED: ALLOW_BILLABLE_PROVIDERS is not true.")
        log("No API request was made.")
        log("No provider availability was claimed.")
        log("Selection record:", OUTPUT_FILE)
        return 0

    if not account_id or not api_token:
        save_selection(
            status="failed",
            reason=(
                "Opt-in was enabled, but Cloudflare credentials "
                "are missing. No API request was made."
            ),
        )
        log("ERROR: Cloudflare credentials are missing.")
        return 1

    attempts = cfg_int(config, "MAX_RETRIES", 1)
    attempts = min(attempts, 3)

    for attempt in range(1, attempts + 1):
        log(f"Opt-in provider test: attempt {attempt}/{attempts}")
        success, reason = test_cloudflare_model(
            account_id,
            api_token,
        )

        if success:
            save_selection(
                status="selected",
                reason=reason,
                tested=True,
            )
            log("Provider test succeeded.")
            log("Selected model:", MODEL)
            log("Selection record:", OUTPUT_FILE)
            return 0

        log("Provider test failed:", reason)

        if attempt < attempts:
            # Do not automatically retry a billable request.
            log(
                "Automatic retry stopped to avoid additional "
                "potentially billable requests."
            )
            break

    save_selection(
        status="failed",
        reason=(
            "The opt-in provider test did not confirm availability. "
            "See the workflow log for the failure reason."
        ),
        tested=True,
    )

    log("No successful model selection was confirmed.")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)