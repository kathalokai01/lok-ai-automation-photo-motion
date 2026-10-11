
#!/usr/bin/env python3
"""Reuse a saved Gemini model selection before doing fresh discovery."""

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
        "User-Agent": "Katha-Lok-AI-Model-Selector/1.1",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_saved_selection():
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
    if not model or model.startswith("models/") or "/" in model:
        return None

    # Reuse only selections produced by the current selector, which records
    # a successful exact-response test before marking the model selected.
    if data.get("test") != EXPECTED_RESPONSE:
        return None

    return data


def list_models(api_key):
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
            raise RuntimeError("Invalid Gemini models response.")

        models.extend(entries)
        next_token = str(result.get("nextPageToken", "")).strip()

        if not next_token:
            break
        if next_token in seen_tokens:
            raise RuntimeError("Repeated Gemini model-list page token.")

        seen_tokens.add(next_token)
        page_token = next_token

    return models


def compatible_models(entries):
    result = []
    seen = set()
    excluded = (
        "embedding", "aqa", "image", "vision",
        "audio", "tts", "robotics",
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

        if "gemini" not in lower or any(x in lower for x in excluded):
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
    if model_id == PREFERRED_MODEL:
        return (0, model_id)
    if "lite" in model_id and "flash" in model_id:
        return (10, model_id)
    if "flash" in model_id:
        return (20, model_id)
    return (30, model_id)


def response_text(result):
    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError("No response candidates returned.")

    candidate = candidates[0]
    reason = str(candidate.get("finishReason", "")).upper()
    if reason in {"MAX_TOKENS", "SAFETY", "RECITATION", "BLOCKLIST"}:
        raise RuntimeError(f"Model test did not complete: {reason}")

    parts = candidate.get("content", {}).get("parts", [])
    answer = "".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()

    if not answer:
        raise RuntimeError("Model returned no text.")
    return answer


def test_model(item, api_key):
    model_id = item["model_id"]
    encoded = urllib.parse.quote(model_id, safe="-._")
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
            "maxOutputTokens": 12,
        },
    }

    last_error = "Unknown model test failure"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = api_request(
                url, api_key, method="POST", payload=payload, timeout=60
            )
            answer = response_text(result)
            if answer == EXPECTED_RESPONSE:
                return True, "Exact compatibility response verified"
            return False, f"Unexpected test response: {answer[:200]}"

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {body[:400]}"
            if exc.code not in TRANSIENT_HTTP_CODES:
                return False, last_error
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < MAX_RETRIES:
            delay = 2 ** attempt
            print(f"Temporary failure; retrying in {delay}s.")
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
    print("===== GEMINI MODEL SELECTION =====")

    # A restored checkpoint may already contain a model that passed testing.
    # Reuse it unless an explicit rescan is requested.
    force_rescan = os.environ.get(
        "FORCE_GEMINI_MODEL_RESCAN", ""
    ).strip().lower() in {"1", "true", "yes"}

    if not force_rescan:
        saved = read_saved_selection()
        if saved:
            print("RESUME: Reusing previously tested model.")
            print("Selected model:", saved["model"])
            print("Fresh model discovery and selection tests skipped.")
            return

    api_key = get_api_key()

    try:
        entries = list_models(api_key)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Could not list Gemini models: HTTP {exc.code} {exc.reason}"
        ) from exc

    models = compatible_models(entries)
    if not models:
        raise RuntimeError("No compatible Gemini models were listed.")

    models.sort(key=model_priority)
    print("Compatible models listed:", len(models))

    tested = []
    for item in models:
        model_id = item["model_id"]
        print(f"Testing model: {model_id}")

        success, detail = test_model(item, api_key)
        tested.append({
            "model": model_id,
            "success": success,
            "detail": detail[:500],
            "tested_at": datetime.now(timezone.utc).isoformat(),
        })

        if success:
            saved = save_selection(item, tested)
            print("Selected model:", saved["model"])
            print("Saved to:", OUTPUT_FILE)
            return

        print("FAILED:", model_id, detail)
        print("Trying next compatible model.")

    OUTPUT_FILE.unlink(missing_ok=True)
    raise RuntimeError(
        f"All {len(models)} compatible models failed the live API test."
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Model selection cancelled.")
    except Exception as exc:
        raise SystemExit(f"MODEL SELECTION FAILED: {exc}")
