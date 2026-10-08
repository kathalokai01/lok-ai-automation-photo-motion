#!/usr/bin/env python3

import json
import os
import sys
import time
from pathlib import Path

import requests

from input_config import (
    load_input_config,
    normalize_format,
)


OUTPUT_FILE = Path(
    "output/config/selected_visual_model.json"
)

MODEL = "alibaba/wan-2.6-image"

API_BASE = (
    "https://api.cloudflare.com/client/v4/accounts"
)

TEST_TIMEOUT = 120


# ============================================================
# INPUT CONFIG COMPATIBILITY HELPERS
# ============================================================

def cfg_bool(config, key, default=False):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    if value is None:
        return default

    text = str(value).strip().lower()

    if text in {"true", "1", "yes", "y", "on"}:
        return True

    if text in {"false", "0", "no", "n", "off"}:
        return False

    return default


def cfg_int(config, key, default=0):
    value = config.get(key, default)

    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================
# BASIC HELPERS
# ============================================================

def fail(message):
    print(f"ERROR: {message}")
    sys.exit(1)


def get_credentials():

    account_id = os.getenv(
        "CLOUDFLARE_ACCOUNT_ID",
        ""
    ).strip()

    api_token = os.getenv(
        "CLOUDFLARE_API_TOKEN",
        ""
    ).strip()

    if not account_id:
        fail(
            "CLOUDFLARE_ACCOUNT_ID is not set."
        )

    if not api_token:
        fail(
            "CLOUDFLARE_API_TOKEN is not set."
        )

    return account_id, api_token


# ============================================================
# CLOUDFLARE MODEL TEST
# ============================================================

def test_model(
    account_id,
    api_token,
):

    # IMPORTANT:
    # Cloudflare's current REST API uses:
    #
    # POST /accounts/{account_id}/ai/run
    #
    # with:
    #
    # {
    #   "model": "...",
    #   "input": {...}
    # }
    #
    # Do NOT append /@model to the URL.

    url = (
        f"{API_BASE}/"
        f"{account_id}"
        f"/ai/run"
    )

    headers = {
        "Authorization": (
            f"Bearer {api_token}"
        ),
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL,
        "input": {
            "prompt": (
                "A photorealistic cinematic "
                "live-action environment test "
                "frame, natural realistic lighting, "
                "real-world appearance"
            ),
            "size": "768x1024",
        },
    }

    print()
    print(
        f"Testing visual model: {MODEL}"
    )
    print(
        f"Endpoint: {url}"
    )

    try:

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=TEST_TIMEOUT,
        )

    except requests.RequestException as exc:

        print(
            f"Model test request failed: {exc}"
        )

        return False, None

    print(
        f"HTTP status: "
        f"{response.status_code}"
    )

    try:

        data = response.json()

    except Exception:

        data = {
            "raw": response.text[:3000]
        }

    if response.status_code in (
        200,
        201,
        202,
    ):

        if (
            isinstance(data, dict)
            and data.get("success") is False
        ):

            print(
                "Cloudflare returned "
                "success=false."
            )

            print(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=2,
                )[:3000]
            )

            return False, data

        # Wan 2.6 returns the generated image
        # inside result.image.
        result = (
            data.get("result", {})
            if isinstance(data, dict)
            else {}
        )

        image = (
            result.get("image")
            if isinstance(result, dict)
            else None
        )

        if image:

            print(
                "Cloudflare visual model "
                "test succeeded."
            )

            print(
                "Generated image result received."
            )

            return True, data

        print(
            "Cloudflare request succeeded, "
            "but no result.image was returned."
        )

        print(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            )[:3000]
        )

        return False, data

    if response.status_code in (
        401,
        403,
    ):

        print(
            "Cloudflare authentication/"
            "permission error."
        )

    elif response.status_code == 400:

        print(
            "Cloudflare rejected the model "
            "request."
        )

    elif response.status_code == 404:

        print(
            "Cloudflare AI endpoint was "
            "not found."
        )

    elif response.status_code == 429:

        print(
            "Cloudflare rate limit received."
        )

    elif response.status_code >= 500:

        print(
            "Cloudflare server-side error."
        )

    print(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )[:3000]
    )

    return False, data


# ============================================================
# SAVE MODEL SELECTION
# ============================================================

def save_selection(
    model,
    status,
):

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    data = {
        "status": status,
        "model": model,
        "provider": "cloudflare",
        "selected_by": (
            "select_visual_model.py"
        ),
        "timestamp": int(time.time()),
    }

    tmp = OUTPUT_FILE.with_suffix(
        OUTPUT_FILE.suffix + ".tmp"
    )

    with tmp.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    tmp.replace(
        OUTPUT_FILE
    )

    return data


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=" * 60
    )

    print(
        "        SELECTING VISUAL GENERATION MODEL"
    )

    print(
        "=" * 60
    )

    try:

        config = load_input_config()

    except Exception as exc:

        fail(
            "Failed to load Input "
            f"configuration: {exc}"
        )

    format_name = normalize_format(
        config
    )

    max_retries = cfg_int(
        config,
        "MAX_RETRIES",
        3,
    )

    resume_enabled = cfg_bool(
        config,
        "RESUME_ENABLED",
        True,
    )

    print(
        f"FORMAT        : {format_name}"
    )

    print(
        f"MAX_RETRIES   : {max_retries}"
    )

    print(
        f"RESUME        : {resume_enabled}"
    )

    print(
        f"MODEL         : {MODEL}"
    )

    account_id, api_token = (
        get_credentials()
    )

    # ========================================================
    # TEST EXACT MODEL
    # ========================================================

    success = False
    result = None

    attempts = max(
        1,
        max_retries,
    )

    for attempt in range(
        1,
        attempts + 1,
    ):

        print()

        print(
            f"Model test attempt "
            f"{attempt}/{attempts}"
        )

        success, result = test_model(
            account_id,
            api_token,
        )

        if success:
            break

        if attempt < attempts:

            wait_seconds = min(
                10 * attempt,
                60,
            )

            print(
                f"Retrying in "
                f"{wait_seconds} seconds..."
            )

            time.sleep(
                wait_seconds
            )

    # ========================================================
    # FAILURE
    # ========================================================

    if not success:

        save_selection(
            MODEL,
            "failed",
        )

        fail(
            f"Visual model test failed: "
            f"{MODEL}"
        )

    # ========================================================
    # SAVE SELECTED MODEL
    # ========================================================

    selection = save_selection(
        MODEL,
        "selected",
    )

    print()

    print(
        "=" * 60
    )

    print(
        "        VISUAL MODEL SELECTED"
    )

    print(
        "=" * 60
    )

    print(
        f"Selected model : "
        f"{selection['model']}"
    )

    print(
        f"Provider       : "
        f"{selection['provider']}"
    )

    print(
        f"Saved to       : "
        f"{OUTPUT_FILE}"
    )

    print(
        "=" * 60
    )

    return 0


if __name__ == "__main__":

    sys.exit(
        main()
    )
