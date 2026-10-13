
#!/usr/bin/env python3
"""Select one compatible Gemini text model with minimal API testing.

A saved, previously verified selection is reused.
On a fresh selection, only one model receives a live test request.
No API keys are stored in this file.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OUTPUT_FILE = Path("output/config/selected_model.json")
PREFERRED_MODEL = "gemini-3.5-flash-lite"
EXPECTED_RESPONSE = "GEMINI_OK"
TEST_MAX_OUTPUT_TOKENS = 12


def get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not key:
        key = os.environ.get("GOOGLE_API_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY or GOOGLE_API_KEY secret is required."
        )

    return key


def api_request(
    url: str,
    api_key: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 45,
) -> dict[str, Any]:
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Katha-Lok-AI-Model-Selector/2.0",
    }

    body = None

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini HTTP {exc.code}: {details[:500]}"
        ) from exc

    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Gemini connection failed: {exc}") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Gemini returned an unexpected response.")

    return result


def read_saved_selection() -> dict[str, Any] | None:
    if not OUTPUT_FILE.is_file():
        return None

    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    if data.get("status") != "selected":
        return None

    model = str(data.get("model", "")).strip()

    if not model or "/" in model:
        return None

    if data.get("test") != EXPECTED_RESPONSE:
        return None

    if not str(data.get("model_resource", "")).startswith("models/"):
        return None

    return data


def list_compatible_models(api_key: str) -> list[dict[str, str]]:
    """List models, without sending generation requests to each model."""
    url = BASE_URL + "/models?pageSize=100"
    entries: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    page_count = 0

    while url:
        page_count += 1

        if page_count > 10:
            raise RuntimeError("Gemini model listing exceeded page limit.")

        result = api_request(url, api_key)
        models = result.get("models", [])

        if not isinstance(models, list):
            raise RuntimeError("Invalid Gemini model-list response.")

        entries.extend(
            item for item in models if isinstance(item, dict)
        )

        next_token = str(result.get("nextPageToken", "")).strip()

        if not next_token:
            break

        if next_token in seen_tokens:
            raise RuntimeError("Gemini repeated a model-list page token.")

        seen_tokens.add(next_token)

        url = BASE_URL + "/models?" + urllib.parse.urlencode({
            "pageSize": "100",
            "pageToken": next_token,
        })

    excluded_terms = (
        "embedding",
        "image",
        "vision",
        "audio",
        "tts",
        "robotics",
        "aqa",
    )

    compatible: list[dict[str, str]] = []
    seen_models: set[str] = set()

    for item in entries:
        resource = str(item.get("name", "")).strip()
        methods = item.get("supportedGenerationMethods", [])

        if not resource.startswith("models/"):
            continue

        if not isinstance(methods, list):
            continue

        if "generateContent" not in methods:
            continue

        model_id = resource.split("/", 1)[1].strip()
        lowered = model_id.lower()

        if "gemini" not in lowered:
            continue

        if any(term in lowered for term in excluded_terms):
            continue

        if model_id in seen_models:
            continue

        seen_models.add(model_id)

        compatible.append({
            "model": model_id,
            "model_resource": resource,
            "display_name": str(item.get("displayName", "")),
        })

    return compatible


def choose_one_model(
    models: list[dict[str, str]],
) -> dict[str, str]:
    """Choose one candidate; do not test every available model."""
    if not models:
        raise RuntimeError(
            "No Gemini text model supporting generateContent was listed."
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


def response_text(result: dict[str, Any]) -> str:
    candidates = result.get("candidates", [])

    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("Gemini test returned no candidates.")

    candidate = candidates[0]

    if not isinstance(candidate, dict):
        raise RuntimeError("Gemini returned an invalid candidate.")

    finish_reason = str(candidate.get("finishReason", "")).upper()

    if finish_reason in {
        "MAX_TOKENS",
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
    }:
        raise RuntimeError(
            f"Gemini test did not finish normally: {finish_reason}"
        )

    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []

    answer = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()

    if not answer:
        raise RuntimeError("Gemini test returned no text.")

    return answer


def test_selected_model(
    item: dict[str, str],
    api_key: str,
) -> None:
    model = item["model"]
    encoded = urllib.parse.quote(model, safe="-._")
    url = f"{BASE_URL}/models/{encoded}:generateContent"

    payload = {
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
            "maxOutputTokens": TEST_MAX_OUTPUT_TOKENS,
        },
    }

    result = api_request(
        url=url,
        api_key=api_key,
        method="POST",
        payload=payload,
    )

    answer = response_text(result)

    if answer != EXPECTED_RESPONSE:
        raise RuntimeError(
            f"Selected model returned an unexpected test response: "
            f"{answer[:150]!r}. No additional models were tested."
        )


def save_selection(
    item: dict[str, str],
) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "status": "selected",
        "model": item["model"],
        "model_resource": item["model_resource"],
        "display_name": item.get("display_name", ""),
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "test": EXPECTED_RESPONSE,
        "selection_policy": "single_model_live_test",
    }

    temporary = OUTPUT_FILE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    temporary.replace(OUTPUT_FILE)


def main() -> None:
    print("===== GEMINI MODEL SELECTION =====")

    force_rescan = os.environ.get(
        "FORCE_GEMINI_MODEL_RESCAN", ""
    ).strip().lower() in {"1", "true", "yes"}

    if not force_rescan:
        saved = read_saved_selection()

        if saved:
            print("Reusing previously verified Gemini model.")
            print("Selected model:", saved["model"])
            print("No fresh generation test was sent.")
            return

    api_key = get_api_key()

    print("Listing compatible text models.")
    models = list_compatible_models(api_key)
    selected = choose_one_model(models)

    print("Candidate selected:", selected["model"])
    print("Sending one minimal compatibility test.")

    try:
        test_selected_model(selected, api_key)
    except Exception as exc:
        OUTPUT_FILE.unlink(missing_ok=True)
        raise RuntimeError(
            "The single selected Gemini model failed its compatibility "
            "test. No other models were tested, to avoid extra requests. "
            f"Details: {exc}"
        ) from exc

    save_selection(selected)

    print("Model verified and saved:", selected["model"])
    print("Saved to:", OUTPUT_FILE)
    print("===== SELECTION COMPLETE =====")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Gemini model selection cancelled.")
    except Exception as exc:
        raise SystemExit(f"GEMINI MODEL SELECTION FAILED: {exc}")
