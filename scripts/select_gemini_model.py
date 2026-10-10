
#!/usr/bin/env python3
"""Discover and test compatible Gemini models before story generation."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OUTPUT_FILE = Path("output/config/selected_model.json")
PREFERRED_MODEL = "gemini-3.5-flash-lite"
EXPECTED_RESPONSE = "GEMINI_OK"
MAX_RETRIES = 2
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}


def get_api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY secret is missing or empty.")
    return key


def api_request(url, api_key, method="GET", payload=None, timeout=60):
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Katha-Lok-AI-Model-Selector/1.0",
    }

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body)


def list_models(api_key):
    """Read all pages returned by the Gemini models endpoint."""
    models = []
    page_token = None
    seen_tokens = set()

    while True:
        params = {"pageSize": "100"}
        if page_token:
            params["pageToken"] = page_token

        url = BASE_URL + "/models?" + urllib.parse.urlencode(params)
        result = api_request(url, api_key, timeout=60)

        entries = result.get("models", [])
        if not isinstance(entries, list):
            raise RuntimeError("Gemini models response has an invalid models field.")

        models.extend(entries)

        next_token = str(result.get("nextPageToken", "")).strip()
        if not next_token:
            break

        if next_token in seen_tokens:
            raise RuntimeError("Gemini model listing returned a repeated page token.")

        seen_tokens.add(next_token)
        page_token = next_token

    return models


def compatible_models(entries):
    result = []
    seen = set()

    excluded_terms = (
        "embedding",
        "aqa",
        "image",
        "vision",
        "audio",
        "tts",
        "robotics",
    )

    for item in entries:
        if not isinstance(item, dict):
            continue

        resource = str(item.get("name", "")).strip()
        methods = item.get("supportedGenerationMethods", [])

        if not resource.startswith("models/"):
            continue
        if not isinstance(methods, list) or "generateContent" not in methods:
            continue

        model_id = resource.split("/", 1)[1].strip()
        lower = model_id.lower()

        if "gemini" not in lower:
            continue
        if any(term in lower for term in excluded_terms):
            continue
        if model_id in seen:
            continue

        seen.add(model_id)
        result.append({
            "name": resource,
            "model_id": model_id,
            "display_name": str(item.get("displayName", "")),
        })

    return result


def model_priority(item):
    model_id = item["model_id"].lower()

    # Prefer the configured model if the API still lists it.
    if model_id == PREFERRED_MODEL:
        return (0, model_id)

    # Prefer Lite Flash variants before larger Flash variants.
    if "lite" in model_id and "flash" in model_id:
        return (10, model_id)

    if "flash" in model_id:
        return (20, model_id)

    # Keep other API-listed Gemini generateContent models as fallbacks.
    return (30, model_id)


def response_text(result):
    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError("No response candidates returned.")

    candidate = candidates[0]
    reason = str(candidate.get("finishReason", "")).upper()

    if reason in {"MAX_TOKENS", "SAFETY", "RECITATION", "BLOCKLIST"}:
        raise RuntimeError(f"Model test did not complete normally: {reason}")

    content = candidate.get("content", {})
    parts = content.get("parts", [])

    texts = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]

    answer = "".join(texts).strip()
    if not answer:
        raise RuntimeError("Model returned no text.")

    return answer


def test_model(item, api_key):
    model_id = item["model_id"]
    encoded_model = urllib.parse.quote(model_id, safe="-._")
    url = f"{BASE_URL}/models/{encoded_model}:generateContent"

    payload = {
        "contents": [{
            "role": "user",
            "parts": [{
                "text": (
                    "This is a compatibility test. "
                    "Reply with exactly GEMINI_OK and no other text."
                )
            }],
        }],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 12,
        },
    }

    last_error = "Unknown model test failure"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = api_request(
                url,
                api_key,
                method="POST",
                payload=payload,
                timeout=60,
            )

            answer = response_text(result)
            if answer.strip() != EXPECTED_RESPONSE:
                return False, f"Unexpected test response: {answer[:200]}"

            return True, "Exact compatibility response verified"

        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""

            last_error = f"HTTP {exc.code}: {body[:400]}"

            if exc.code not in TRANSIENT_HTTP_CODES:
                return False, last_error

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < MAX_RETRIES:
            delay = 2 ** attempt
            print(f"Temporary failure for {model_id}; retrying in {delay}s.")
            time.sleep(delay)

    return False, last_error


def save_selection(item, tested):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "status": "selected",
        "model": item["model_id"],
        "model_resource": item["name"],
        "display_name": item["display_name"],
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "test": EXPECTED_RESPONSE,
        "tested_models": tested,
    }

    temp = OUTPUT_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(OUTPUT_FILE)

    return result


def main():
    print("===== GEMINI MODEL DISCOVERY =====")

    api_key = get_api_key()

    try:
        entries = list_models(api_key)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Could not list Gemini models: HTTP {exc.code} {exc.reason}"
        ) from exc

    models = compatible_models(entries)
    if not models:
        raise RuntimeError(
            "No compatible Gemini generateContent models were listed by the API."
        )

    models.sort(key=model_priority)

    print("API-listed compatible models:", len(models))
    for index, item in enumerate(models, start=1):
        print(f"{index}. {item['model_id']}")

    tested = []

    for item in models:
        model_id = item["model_id"]
        print(f"\nTesting model: {model_id}")

        success, detail = test_model(item, api_key)
        tested.append({
            "model": model_id,
            "success": success,
            "detail": detail[:500],
            "tested_at": datetime.now(timezone.utc).isoformat(),
        })

        if success:
            selected = save_selection(item, tested)
            print("\n===== GEMINI MODEL SELECTED =====")
            print("Selected model:", selected["model"])
            print("Saved to:", OUTPUT_FILE)
            print("=================================")
            return

        print("FAILED:", model_id)
        print("Reason:", detail)
        print("Trying the next compatible model...")

    # Do not leave an old successful selection available after all tests fail.
    OUTPUT_FILE.unlink(missing_ok=True)
    raise RuntimeError(
        f"All {len(models)} compatible models failed the live API test. "
        "No model has been selected."
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Model selection cancelled.")
    except Exception as exc:
        raise SystemExit(f"MODEL SELECTION FAILED: {exc}")
