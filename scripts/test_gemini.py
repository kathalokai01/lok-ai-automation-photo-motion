
#!/usr/bin/env python3

"""
Katha Lok AI - Gemini API Validator

Features:
- Tests only the selected/configured Gemini models.
- Uses the existing fallback order.
- Moves to the next model only after a failure.
- Reuses successful validation when model and API key match.
- Records failure reasons and successful model.
- Avoids unnecessary paid API requests.
"""

import os
import json
import time
import hashlib
import urllib.request
import urllib.error

from pathlib import Path
from datetime import datetime, timezone

from gemini_http import http_error_body, retry_after_seconds


BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
)

MODEL_FILE = Path(
    "output/config/selected_model.json"
)

TIMEOUT = 60
MAX_OUTPUT_TOKENS = 12

API_KEY = (
    os.environ.get("GEMINI_API_KEY", "").strip()
    or os.environ.get("GOOGLE_API_KEY", "").strip()
)


def log(message):
    print(
        f"[GEMINI TEST] {message}",
        flush=True
    )


def normalize_model(value):
    value = str(value or "").strip()

    if value.startswith("models/"):
        value = value.split("/", 1)[1]

    if (
        not value
        or any(c in value for c in "\r\n\t ")
        or "/" in value
        or ":" in value
        or not value.lower().startswith("gemini-")
    ):
        return ""

    return value


def load_config():
    if not MODEL_FILE.is_file():
        raise SystemExit(
            f"ERROR: Model file missing: {MODEL_FILE}"
        )

    try:
        with MODEL_FILE.open(
            "r", encoding="utf-8"
        ) as file:
            data = json.load(file)

    except Exception as exc:
        raise SystemExit(
            f"ERROR: Cannot read model configuration: {exc}"
        )

    if not isinstance(data, dict):
        raise SystemExit(
            "ERROR: Model configuration must be a JSON object."
        )

    if data.get("status") != "selected":
        raise SystemExit(
            "ERROR: Gemini model is not selected."
        )

    return data


def key_fingerprint():
    return hashlib.sha256(
        API_KEY.encode("utf-8")
    ).hexdigest()


def get_fallback_order(config):
    result = []

    selected = normalize_model(
        config.get("model")
    )

    if selected:
        result.append(selected)

    configured = config.get(
        "fallback_order", []
    )

    if not isinstance(configured, list):
        configured = []

    for item in configured:
        model = normalize_model(item)

        if model and model not in result:
            result.append(model)

    env_models = os.environ.get(
        "GEMINI_MODEL_FALLBACKS", ""
    )

    for item in env_models.replace(
        ";", ","
    ).split(","):

        model = normalize_model(item)

        if model and model not in result:
            result.append(model)

    return result


def reusable_test(config, model):
    record = config.get("api_test")

    if not isinstance(record, dict):
        return False

    return (
        record.get("status") == "passed"
        and record.get("model") == model
        and record.get("key_fingerprint")
        == key_fingerprint()
    )


def save_config(config):
    MODEL_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = MODEL_FILE.with_suffix(
        ".json.tmp"
    )

    try:
        with temporary.open(
            "w", encoding="utf-8"
        ) as file:

            json.dump(
                config,
                file,
                ensure_ascii=False,
                indent=2
            )

            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temporary,
            MODEL_FILE
        )

    finally:
        if temporary.exists():
            temporary.unlink()


def test_model(model):
    """
    Perform exactly one minimal generateContent request.
    """

    url = (
        f"{BASE_URL}/models/"
        f"{model}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Reply with exactly GEMINI_OK"
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
        }
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(
            "utf-8"
        ),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": API_KEY,
        },
        method="POST"
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode("utf-8")
            result = json.loads(raw)
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= 3:
                raise
            delay = retry_after_seconds(exc, attempt, default_base=12, maximum=120)
            log(f"HTTP 429 rate limit for {model}; retrying in {delay}s.")
            time.sleep(delay)

    if not isinstance(result, dict):
        raise ValueError(
            "Gemini returned an invalid JSON response."
        )

    candidates = result.get(
        "candidates", []
    )

    if not candidates:
        raise ValueError(
            "No Gemini response candidates returned."
        )

    candidate = candidates[0]

    content = candidate.get(
        "content", {}
    )

    parts = content.get(
        "parts", []
    )

    response_text = " ".join(
        str(part.get("text", "")).strip()
        for part in parts
        if isinstance(part, dict)
    ).strip()

    if not response_text:
        raise ValueError(
            "Gemini returned an empty response."
        )

    return {
        "response": response_text,
        "finish_reason": candidate.get(
            "finishReason", ""
        )
    }


def record_failure(model, error, failures):
    failures.append({
        "model": model,
        "error": str(error)[:1000],
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    })


def main():
    log("Starting Gemini API validation.")

    if not API_KEY:
        raise SystemExit(
            "ERROR: GEMINI_API_KEY or GOOGLE_API_KEY is not set."
        )

    config = load_config()

    fallback_order = get_fallback_order(
        config
    )

    if not fallback_order:
        raise SystemExit(
            "ERROR: No configured Gemini models."
        )

    log(
        f"Configured models: {len(fallback_order)}"
    )

    current_model = normalize_model(
        config.get("model")
    )

    # Reuse successful validation.
    if (
        current_model
        and reusable_test(config, current_model)
    ):
        log(
            f"Previously validated model reused: "
            f"{current_model}"
        )
        log("No additional API request required.")
        log("GEMINI API TEST: PASSED (CACHED)")
        return

    failures = []

    for index, model in enumerate(
        fallback_order,
        start=1
    ):

        log(
            f"Testing fallback {index}/"
            f"{len(fallback_order)}: {model}"
        )

        try:
            result = test_model(model)

            # Save successful model immediately.
            config["status"] = "selected"
            config["model"] = model
            config["model_resource"] = (
                f"models/{model}"
            )

            config["api_test"] = {
                "status": "passed",
                "model": model,
                "response": result["response"][:300],
                "finish_reason": result["finish_reason"],
                "key_fingerprint": key_fingerprint(),
                "tested_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "request_count": 1
            }

            config["failed_model_tests"] = failures

            config["fallback_order"] = (
                fallback_order
            )

            save_config(config)

            log("GEMINI API TEST: PASSED")
            log(f"Working model: {model}")
            log("Successful selection saved.")
            return

        except urllib.error.HTTPError as exc:

            try:
                body = http_error_body(exc, 700)
            except Exception:
                body = ""

            message = (
                f"HTTP {exc.code}: {body[:700]}"
            )

            log(f"FAILED: {model}: {message}")

            record_failure(
                model,
                message,
                failures
            )

            # Invalid credentials or permission:
            # other models cannot fix account access.
            if exc.code in (401, 403):
                config["api_test"] = {
                    "status": "failed",
                    "model": model,
                    "error": message,
                    "tested_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "key_fingerprint": key_fingerprint()
                }

                config["failed_model_tests"] = failures
                save_config(config)

                raise SystemExit(
                    "ERROR: Gemini API authentication/"
                    "permission denied. Check API key "
                    "and project access."
                )

            # Rate limits, model availability and
            # server errors: continue configured fallback.
            continue

        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
            OSError
        ) as exc:

            message = str(exc)

            log(
                f"FAILED: {model}: {message}"
            )

            record_failure(
                model,
                message,
                failures
            )

            continue

        except Exception as exc:

            message = (
                f"{type(exc).__name__}: {exc}"
            )

            log(
                f"FAILED: {model}: {message}"
            )

            record_failure(
                model,
                message,
                failures
            )

            continue

    # No model succeeded.
    config["api_test"] = {
        "status": "failed",
        "model": current_model,
        "tested_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "key_fingerprint": key_fingerprint(),
        "failure_count": len(failures)
    }

    config["failed_model_tests"] = failures

    save_config(config)

    log("ALL CONFIGURED MODELS FAILED.")

    for failure in failures:
        log(
            f"{failure['model']}: "
            f"{failure['error']}"
        )

    raise SystemExit(
        "ERROR: No configured Gemini fallback "
        "model passed the API test."
    )


if __name__ == "__main__":
    main()
