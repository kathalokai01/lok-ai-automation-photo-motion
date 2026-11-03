#!/usr/bin/env python3
"""Select configured Gemini models without changing their priority order."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OUTPUT_FILE = Path("output/config/selected_model.json")
SELECTOR_VERSION = 4

REQUEST_TIMEOUT = 45
MAX_MODEL_PAGES = 20


def log(message: str) -> None:
    print(f"[GEMINI SELECTOR] {message}", flush=True)


def normalize_model(value: Any) -> str:
    value = str(value or "").strip()

    if value.startswith("models/"):
        value = value.split("/", 1)[1]

    if not value:
        return ""

    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*",
        value,
    ):
        return ""

    if not value.lower().startswith("gemini-"):
        return ""

    return value


def read_existing_selection() -> dict:
    if not OUTPUT_FILE.is_file():
        return {}

    try:
        data = json.loads(
            OUTPUT_FILE.read_text(encoding="utf-8-sig")
        )

        return data if isinstance(data, dict) else {}

    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log(f"Previous selection could not be read: {exc}")
        return {}


def previous_selection_is_successful(data: dict) -> bool:
    if not isinstance(data, dict):
        return False

    if data.get("status") != "selected":
        return False

    selected_model = normalize_model(data.get("model"))

    if not selected_model:
        return False

    api_test = data.get("api_test")

    if isinstance(api_test, dict):
        if (
            api_test.get("status") == "passed"
            and normalize_model(api_test.get("model"))
            == selected_model
        ):
            return True

    tested_models = data.get("tested_models", [])

    if isinstance(tested_models, list):
        for item in tested_models:
            if not isinstance(item, dict):
                continue

            if (
                normalize_model(item.get("model"))
                == selected_model
                and item.get("success") is True
            ):
                return True

    return False


def configured_fallbacks(existing: dict) -> list[str]:
    """
    Preserve the current priority:
    1. GEMINI_MODEL, when explicitly configured.
    2. GEMINI_MODEL_FALLBACKS, in its exact declared order.
    3. Previously API-tested successful model, if not already listed.

    No model is invented or silently substituted.
    """

    result: list[str] = []

    primary = normalize_model(
        os.environ.get("GEMINI_MODEL", "")
    )

    if primary:
        result.append(primary)

    raw_fallbacks = os.environ.get(
        "GEMINI_MODEL_FALLBACKS",
        "",
    )

    for item in re.split(r"[,;]", raw_fallbacks):
        model = normalize_model(item)

        if model and model not in result:
            result.append(model)

    if previous_selection_is_successful(existing):
        previous = normalize_model(existing.get("model"))

        if previous and previous not in result:
            result.append(previous)

    return result


def api_request(url: str) -> dict:
    api_key = os.environ.get(
        "GEMINI_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    request = urllib.request.Request(
        url,
        headers={
            "x-goog-api-key": api_key,
            "Accept": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        raw = response.read().decode("utf-8")

    data = json.loads(raw)

    if not isinstance(data, dict):
        raise ValueError(
            "Gemini model discovery returned invalid JSON."
        )

    return data


def discover_models() -> dict[str, dict]:
    log("Checking available Gemini models.")

    available: dict[str, dict] = {}
    page_token = ""
    seen_tokens: set[str] = set()

    for page_number in range(1, MAX_MODEL_PAGES + 1):
        params = {}

        if page_token:
            params["pageToken"] = page_token

        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}/models"

        if query:
            url = f"{url}?{query}"

        data = api_request(url)
        models = data.get("models", [])

        if not isinstance(models, list):
            raise ValueError(
                "Gemini model discovery returned an invalid models list."
            )

        for item in models:
            if not isinstance(item, dict):
                continue

            methods = item.get(
                "supportedGenerationMethods",
                [],
            )

            if (
                not isinstance(methods, list)
                or "generateContent" not in methods
            ):
                continue

            resource = str(
                item.get("name", "")
            ).strip()

            model = normalize_model(resource)

            if not model:
                continue

            available[model] = {
                "resource": resource,
                "display_name": str(
                    item.get("displayName", "")
                ).strip(),
            }

        next_token = str(
            data.get("nextPageToken", "")
        ).strip()

        if not next_token:
            break

        if next_token in seen_tokens:
            raise RuntimeError(
                "Gemini model discovery returned a repeated page token."
            )

        seen_tokens.add(next_token)
        page_token = next_token

    else:
        raise RuntimeError(
            f"Gemini model discovery exceeded "
            f"{MAX_MODEL_PAGES} pages."
        )

    log(f"Compatible Gemini models discovered: {len(available)}")

    return available


def atomic_save(data: dict) -> None:
    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = OUTPUT_FILE.with_suffix(".json.tmp")

    try:
        with temporary.open(
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
                indent=2,
            )

            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, OUTPUT_FILE)

    finally:
        temporary.unlink(missing_ok=True)

def save_selection(
    model: str,
    details: dict,
    fallback_order: list[str],
    previous: dict,
) -> dict:
    previous_model = normalize_model(
        previous.get("model")
    )

    result = {
        "status": "selected",
        "model": model,
        "model_resource": details["resource"],
        "display_name": details["display_name"],
        "selected_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "selection_source": "explicit_configured_fallback",
        "fallback_order": fallback_order,
        "tested_models": (
            previous.get("tested_models", [])
            if isinstance(previous.get("tested_models"), list)
            else []
        ),
        "generation_test_performed": False,
        "selector_version": SELECTOR_VERSION,
    }

    # Keep API-test evidence only when it belongs to this same model.
    if previous_model == model:
        api_test = previous.get("api_test")

        if isinstance(api_test, dict):
            result["api_test"] = api_test

        failures = previous.get("failed_model_tests")

        if isinstance(failures, list):
            result["failed_model_tests"] = failures

    atomic_save(result)

    return result


def reuse_previous(
    existing: dict,
    fallbacks: list[str],
) -> bool:
    if not previous_selection_is_successful(existing):
        return False

    previous_model = normalize_model(
        existing.get("model")
    )

    # Reuse only when the previous successful model is still
    # the highest-priority configured candidate.
    if not fallbacks or previous_model != fallbacks[0]:
        return False

    existing["fallback_order"] = fallbacks
    existing["selector_version"] = SELECTOR_VERSION

    existing.setdefault(
        "selection_source",
        "previous_successful_selection",
    )

    atomic_save(existing)

    log(f"Reusing previously API-tested model: {previous_model}")
    log("No model discovery or generation test performed.")

    return True


def main() -> None:
    log("Starting strict Gemini model selection.")

    existing = read_existing_selection()
    fallbacks = configured_fallbacks(existing)

    if not fallbacks:
        raise SystemExit(
            "ERROR: No Gemini model is configured and no "
            "previously API-tested successful model is available. "
            "Set GEMINI_MODEL or GEMINI_MODEL_FALLBACKS."
        )

    log(
        "Configured priority order: "
        + " -> ".join(fallbacks)
    )

    if reuse_previous(existing, fallbacks):
        return

    try:
        available = discover_models()

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise SystemExit(
            f"Gemini model discovery failed: "
            f"HTTP {exc.code}: {body[:1000]}"
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
    ) as exc:
        raise SystemExit(
            f"Gemini model discovery failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    for position, model in enumerate(
        fallbacks,
        start=1,
    ):
        log(f"Priority {position}: {model}")

        details = available.get(model)

        if not details:
            log(
                f"Unavailable or incompatible: {model}"
            )
            continue

        selected = save_selection(
            model=model,
            details=details,
            fallback_order=fallbacks,
            previous=existing,
        )

        log("GEMINI MODEL SELECTED")
        log("Model: " + selected["model"])
        log("Generation test: not performed")
        return

    raise SystemExit(
        "ERROR: None of the configured Gemini models "
        "supports generateContent. No substitute model was added."
    )


if __name__ == "__main__":
    main()