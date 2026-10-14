#!/usr/bin/env python3
"""Select a configured Gemini model without accidental API requests."""

from __future__ import annotations

import json
import os
import re
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
TEST_OPT_IN = "I_ACCEPT_POTENTIAL_GEMINI_CHARGES"
TIMEOUT = 30
MAX_MODEL_PAGES = 5
VERSION = "safe-gemini-selector-v4"

MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


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
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )


def valid_model_name(value: str) -> bool:
    return bool(MODEL_PATTERN.fullmatch(value or ""))


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def atomic_save(data: dict[str, Any]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    if not saved or saved.get("model") != data.get("model"):
        raise RuntimeError("Model selection read-back validation failed.")

    if saved.get("verified") is not data.get("verified"):
        raise RuntimeError("Model verification status was not preserved.")


def save_selection(
    model: str,
    *,
    verified: bool,
    display_name: str = "",
    reason: str = "",
) -> dict[str, Any]:
    if not valid_model_name(model):
        raise RuntimeError("Refusing to save an invalid model identifier.")

    data = {
        "version": VERSION,
        # Downstream scripts use status == "selected".
        # Verification remains a separate, truthful field.
        "status": "selected",
        "model": model,
        "model_resource": f"models/{model}",
        "display_name": display_name,
        "selected_at": utc_now(),
        "verified": verified,
        "test": EXPECTED_RESPONSE if verified else None,
        "selection_policy": (
            "explicitly_authorized_live_test"
            if verified
            else "local_default_no_network"
        ),
        "reason": reason,
        "external_api_calls": None if verified else 0,
        "paid_services_used": None if verified else False,
    }

    atomic_save(data)
    return data


def read_verified_selection() -> dict[str, Any] | None:
    data = read_json(OUTPUT_FILE)
    if not data:
        return None

    model = str(data.get("model", "")).strip()
    if (
        data.get("status") == "selected"
        and data.get("verified") is True
        and data.get("test") == EXPECTED_RESPONSE
        and data.get("model_resource") == f"models/{model}"
        and valid_model_name(model)
    ):
        return data

    return None


def api_request(
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = (
        json.dumps(payload).encode("utf-8")
        if payload is not None
        else None
    )

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "Katha-Lok-AI-Model-Selector/6.0",
        },
        method="POST" if body is not None else "GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not print the request URL with credentials or retry.
        raise RuntimeError(
            f"Gemini HTTP error {exc.code}; automatic retries are disabled."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Gemini request failed ({type(exc).__name__}); "
            "automatic retries are disabled."
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError("Gemini returned an unexpected response.")
    return result


def list_compatible_models(api_key: str) -> list[dict[str, str]]:
    url = BASE_URL + "/models?pageSize=100"
    compatible: list[dict[str, str]] = []
    seen_models: set[str] = set()
    seen_tokens: set[str] = set()

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

            if not valid_model_name(model) or model in seen_models:
                continue
            if "gemini" not in lowered:
                continue
            if any(
                word in lowered
                for word in (
                    "embedding", "image", "vision", "audio",
                    "tts", "robotics", "aqa",
                )
            ):
                continue

            seen_models.add(model)
            compatible.append({
                "model": model,
                "display_name": str(item.get("displayName", "")),
            })

        token = str(result.get("nextPageToken", "")).strip()
        if not token:
            break
        if token in seen_tokens:
            raise RuntimeError("Gemini repeated a pagination token.")

        seen_tokens.add(token)
        url = BASE_URL + "/models?" + urllib.parse.urlencode({
            "pageSize": "100",
            "pageToken": token,
        })
    else:
        raise RuntimeError("Gemini model listing exceeded its page limit.")

    return compatible


def choose_model(models: list[dict[str, str]]) -> dict[str, str]:
    if not models:
        raise RuntimeError("No compatible Gemini text models were listed.")

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
    model = urllib.parse.quote(item["model"], safe="-._")
    result = api_request(
        f"{BASE_URL}/models/{model}:generateContent",
        api_key,
        {
            "contents": [{
                "role": "user",
                "parts": [{
                    "text": "Reply exactly GEMINI_OK and nothing else."
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
    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []

    answer = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
    ).strip()

    if answer != EXPECTED_RESPONSE:
        raise RuntimeError("Gemini compatibility test did not match.")


def main() -> int:
    log("===== SAFE GEMINI MODEL SELECTION =====")

    live_test_authorized = (
        env_true("ALLOW_GEMINI_MODEL_TEST")
        and os.getenv("ALLOW_GEMINI_GENERATION", "").strip()
        == TEST_OPT_IN
    )
    force_rescan = env_true("FORCE_GEMINI_MODEL_RESCAN")

    if not live_test_authorized:
        existing = read_json(OUTPUT_FILE)

        if existing and not force_rescan:
            model = str(existing.get("model", "")).strip()

            if (
                existing.get("status") == "selected"
                and valid_model_name(model)
                and existing.get("model_resource") == f"models/{model}"
            ):
                log("Reusing configured model:", model)
                log("Verified:", bool(existing.get("verified")))
                log("Network requests: 0")
                return 0

        saved = save_selection(
            PREFERRED_MODEL,
            verified=False,
            reason=(
                "Local default only; model availability is unverified. "
                "No network request was made. Live testing requires both "
                "ALLOW_GEMINI_MODEL_TEST=true and explicit generation opt-in."
            ),
        )
        log("Configured model:", saved["model"])
        log("Verified:", saved["verified"])
        log("Network requests: 0")
        log("Saved:", OUTPUT_FILE)
        return 0

    api_key = get_api_key()
    if not api_key:
        log("ERROR: Live test was authorized but no API key is configured.")
        return 1

    log("Live model listing and one generation test are authorized.")
    models = list_compatible_models(api_key)
    selected = choose_model(models)

    log("Testing candidate:", selected["model"])
    test_model(selected, api_key)

    saved = save_selection(
        selected["model"],
        verified=True,
        display_name=selected.get("display_name", ""),
        reason="Explicitly authorized live compatibility test succeeded.",
    )
    log("Verified model:", saved["model"])
    log("Saved:", OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Model selection cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("MODEL SELECTION FAILED:", str(exc))
        raise SystemExit(1)