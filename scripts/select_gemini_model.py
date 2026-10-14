#!/usr/bin/env python3
"""Billing-conscious Gemini text model selection.

Default:
- Reuse an existing verified selection.
- If none exists, save a configured but UNVERIFIED model.
- Make no network requests unless ALLOW_GEMINI_MODEL_TEST=true.

A model configured locally is not proof that the provider currently
supports it or that the account has access to it.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = ROOT / "output" / "config" / "selected_model.json"

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED_MODEL = "gemini-3.5-flash-lite"
EXPECTED_RESPONSE = "GEMINI_OK"
REQUEST_TIMEOUT = 30
MAX_MODEL_PAGES = 5
VERSION = "safe-gemini-selector-v2"


def log(*items: object) -> None:
    print(*items, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {
        "1", "true", "yes", "y", "on"
    }


def get_api_key() -> str:
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def valid_model_name(model: str) -> bool:
    return bool(
        model
        and "/" not in model
        and ":" not in model
        and not any(char.isspace() for char in model)
    )


def read_verified_selection() -> dict[str, Any] | None:
    data = read_json(OUTPUT_FILE)
    if not data:
        return None

    model = str(data.get("model", "")).strip()

    if (
        data.get("status") == "selected"
        and data.get("test") == EXPECTED_RESPONSE
        and data.get("verified") is True
        and valid_model_name(model)
        and data.get("model_resource") == f"models/{model}"
    ):
        return data

    return None


def save_selection(
    model: str,
    *,
    verified: bool,
    display_name: str = "",
    reason: str = "",
) -> dict[str, Any]:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "version": VERSION,
        "status": "selected" if verified else "configured_unverified",
        "model": model,
        "model_resource": f"models/{model}",
        "display_name": display_name,
        "selected_at": utc_now(),
        "verified": verified,
        "test": EXPECTED_RESPONSE if verified else None,
        "selection_policy": (
            "single_model_live_test"
            if verified
            else "local_default_no_network"
        ),
        "reason": reason,
        "external_api_calls": None if verified else 0,
        "paid_services_used": None if verified else False,
    }

    temporary = OUTPUT_FILE.with_name(OUTPUT_FILE.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, OUTPUT_FILE)
    finally:
        temporary.unlink(missing_ok=True)

    saved = read_json(OUTPUT_FILE)

    if not saved or saved.get("model") != model:
        raise RuntimeError("Model selection failed read-back validation.")

    if saved.get("verified") is not verified:
        raise RuntimeError("Model verification status was not saved correctly.")

    return saved


def api_request(
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    method = "GET"

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        method = "POST"

    request = urllib.request.Request(
        url=url,
        data=body,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "Katha-Lok-AI-Model-Selector/4.0",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini HTTP {exc.code}: {details[:500]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Gemini request failed; automatic retries are disabled: {exc}"
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError("Gemini returned an unexpected response.")

    return result


def list_compatible_models(api_key: str) -> list[dict[str, str]]:
    url = BASE_URL + "/models?pageSize=100"
    compatible: list[dict[str, str]] = []
    seen_tokens: set[str] = set()
    seen_models: set[str] = set()

    for _ in range(MAX_MODEL_PAGES):
        result = api_request(url, api_key)
        entries = result.get("models", [])

        if not isinstance(entries, list):
            raise RuntimeError("Invalid Gemini model-list response.")

        for item in entries:
            if not isinstance(item, dict):
                continue

            resource = str(item.get("name", "")).strip()
            methods = item.get("supportedGenerationMethods", [])

            if not resource.startswith("models/"):
                continue
            if not isinstance(methods, list):
                continue
            if "generateContent" not in methods:
                continue

            model = resource.split("/", 1)[1]
            lowered = model.lower()

            if not valid_model_name(model):
                continue
            if "gemini" not in lowered:
                continue
            if any(
                word in lowered
                for word in (
                    "embedding", "image", "vision",
                    "audio", "tts", "robotics", "aqa",
                )
            ):
                continue
            if model in seen_models:
                continue

            seen_models.add(model)
            compatible.append({
                "model": model,
                "model_resource": resource,
                "display_name": str(item.get("displayName", "")),
            })

        token = str(result.get("nextPageToken", "")).strip()

        if not token:
            break
        if token in seen_tokens:
            raise RuntimeError("Gemini repeated a page token.")

        seen_tokens.add(token)
        url = BASE_URL + "/models?" + urllib.parse.urlencode({
            "pageSize": "100",
            "pageToken": token,
        })
    else:
        raise RuntimeError("Gemini model listing exceeded the page limit.")

    return compatible


def choose_model(
    models: list[dict[str, str]],
) -> dict[str, str]:
    if not models:
        raise RuntimeError(
            "No compatible Gemini text models were returned."
        )

    for item in models:
        if item["model"] == PREFERRED_MODEL:
            return item

    def priority(item: dict[str, str]) -> tuple[int, str]:
        name = item["model"].lower()

        if "flash" in name and "lite" in name:
            return (0, name)
        if "flash" in name:
            return (1, name)
        if "pro" in name:
            return (2, name)
        return (3, name)

    return sorted(models, key=priority)[0]


def test_model(item: dict[str, str], api_key: str) -> None:
    model = item["model"]
    encoded = urllib.parse.quote(model, safe="-._")

    result = api_request(
        f"{BASE_URL}/models/{encoded}:generateContent",
        api_key,
        payload={
            "contents": [{
                "role": "user",
                "parts": [{
                    "text": (
                        "Reply with exactly GEMINI_OK and no other text."
                    )
                }],
            }],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 12,
            },
        },
    )

    candidates = result.get("candidates", [])

    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("Gemini test returned no candidates.")

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise RuntimeError("Gemini returned an invalid candidate.")

    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []

    answer = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()

    if answer != EXPECTED_RESPONSE:
        raise RuntimeError(
            f"Gemini test did not return the expected response: "
            f"{answer[:100]!r}"
        )


def main() -> int:
    log("===== SAFE GEMINI MODEL SELECTION =====")

    force_test = env_true("ALLOW_GEMINI_MODEL_TEST")
    force_rescan = env_true("FORCE_GEMINI_MODEL_RESCAN")

    if not force_test:
        saved = read_verified_selection()

        if saved and not force_rescan:
            log("Reusing previously verified model:", saved["model"])
            log("New API requests: 0")
            return 0

        existing = read_json(OUTPUT_FILE)

        if (
            existing
            and existing.get("status") == "configured_unverified"
            and existing.get("model") == PREFERRED_MODEL
            and not force_rescan
        ):
            log("Reusing locally configured model:", PREFERRED_MODEL)
            log("WARNING: This model has not been verified by a live test.")
            log("Network requests: 0")
            return 0

        saved = save_selection(
            PREFERRED_MODEL,
            verified=False,
            reason=(
                "Local default only. No live availability test was made. "
                "Set ALLOW_GEMINI_MODEL_TEST=true only after checking "
                "current account access, pricing and usage limits."
            ),
        )

        log("Configured default model:", saved["model"])
        log("Verified:", saved["verified"])
        log("No Gemini API request was made.")
        log("Saved to:", OUTPUT_FILE)
        return 0

    api_key = get_api_key()

    if not api_key:
        log(
            "ERROR: Set GEMINI_API_KEY or GOOGLE_API_KEY to run "
            "the explicitly requested test."
        )
        return 1

    # One model-listing sequence and one minimal generation test.
    # There are no automatic retries.
    models = list_compatible_models(api_key)
    selected = choose_model(models)

    log("Testing one candidate:", selected["model"])
    test_model(selected, api_key)

    saved = save_selection(
        selected["model"],
        verified=True,
        display_name=selected.get("display_name", ""),
        reason="One explicit live compatibility test succeeded.",
    )

    log("Verified model:", saved["model"])
    log("Saved to:", OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Gemini model selection cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("GEMINI MODEL SELECTION FAILED:", exc)
        raise SystemExit(1)