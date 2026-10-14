
#!/usr/bin/env python3
"""Select one compatible Gemini text model with minimal API requests.

Reuses a previously verified selection unless an explicit rescan is requested.
A fresh selection sends only one generation test request.
No API keys are stored in the output file.
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
REQUEST_TIMEOUT = 45


def log(*items: object) -> None:
    print(*items, flush=True)


def get_api_key() -> str:
    key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
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
) -> dict[str, Any]:
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Katha-Lok-AI-Model-Selector/3.0",
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
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT
        ) as response:
            result = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini HTTP {exc.code}: {details[:500]}"
        ) from exc

    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            f"Gemini connection failed: {exc}. "
            "The request was not automatically retried."
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError("Gemini returned an unexpected response.")

    return result


def read_saved_selection() -> dict[str, Any] | None:
    if not OUTPUT_FILE.is_file():
        return None

    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("Saved model selection is unreadable; a fresh selection is needed.")
        return None

    if not isinstance(data, dict):
        return None

    model = str(data.get("model", "")).strip()
    resource = str(data.get("model_resource", "")).strip()

    if (
        data.get("status") != "selected"
        or data.get("test") != EXPECTED_RESPONSE
        or not model
        or "/" in model
        or ":" in model
        or any(char.isspace() for char in model)
        or resource != f"models/{model}"
    ):
        log("Saved model selection failed local validation.")
        return None

    return data


def list_compatible_models(api_key: str) -> list[dict[str, str]]:
    """List compatible models; do not generate-test every model."""
    url = BASE_URL + "/models?pageSize=100"
    entries: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()

    for _ in range(10):
        result = api_request(url, api_key)
        models = result.get("models", [])

        if not isinstance(models, list):
            raise RuntimeError("Invalid Gemini model-list response.")

        entries.extend(
            item for item in models if isinstance(item, dict)
        )

        token = str(result.get("nextPageToken", "")).strip()
        if not token:
            break

        if token in seen_tokens:
            raise RuntimeError("Gemini repeated a model-list page token.")

        seen_tokens.add(token)
        url = BASE_URL + "/models?" + urllib.parse.urlencode({
            "pageSize": "100",
            "pageToken": token,
        })
    else:
        raise RuntimeError("Gemini model listing exceeded the page limit.")

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

        model = resource.split("/", 1)[1].strip()
        lowered = model.lower()

        if "gemini" not in lowered:
            continue
        if any(term in lowered for term in excluded_terms):
            continue
        if model in seen_models:
            continue

        seen_models.add(model)
        compatible.append({
            "model": model,
            "model_resource": resource,
            "display_name": str(item.get("displayName", "")),
        })

    return compatible


def choose_one_model(
    models: list[dict[str, str]],
) -> dict[str, str]:
    if not models:
        raise RuntimeError(
            "No listed Gemini text model supports generateContent."
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
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
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
                "text": "Reply with exactly GEMINI_OK and no other text."
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
            f"{answer[:150]!r}. No other models were tested."
        )


def save_selection(item: dict[str, str]) -> None:
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

    saved = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    if (
        saved.get("status") != "selected"
        or saved.get("model") != item["model"]
        or saved.get("test") != EXPECTED_RESPONSE
    ):
        raise RuntimeError("Saved model selection failed read-back validation.")


def main() -> None:
    log("===== GEMINI MODEL SELECTION =====")

    force_rescan = os.environ.get(
        "FORCE_GEMINI_MODEL_RESCAN", ""
    ).strip().lower() in {"1", "true", "yes"}

    if not force_rescan:
        saved = read_saved_selection()
        if saved:
            log("Reusing previously verified Gemini model.")
            log("Selected model:", saved["model"])
            log("New generation test requests: 0")
            return

    api_key = get_api_key()

    log("Listing compatible text models.")
    models = list_compatible_models(api_key)
    selected = choose_one_model(models)

    log("Candidate selected:", selected["model"])
    log("Sending one minimal compatibility test.")

    # Preserve any older saved selection if this new test fails.
    test_selected_model(selected, api_key)
    save_selection(selected)

    log("Model verified and saved:", selected["model"])
    log("Saved to:", OUTPUT_FILE)
    log("===== SELECTION COMPLETE =====")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Gemini model selection cancelled.")
    except Exception as exc:
        raise SystemExit(f"GEMINI MODEL SELECTION FAILED: {exc}")
