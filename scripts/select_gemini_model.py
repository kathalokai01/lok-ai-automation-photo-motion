#!/usr/bin/env python3
"""Select only explicitly configured or previously successful Gemini models."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OUTPUT_FILE = Path("output/config/selected_model.json")
SELECTOR_VERSION = 3


def log(message):
    print(f"[GEMINI SELECTOR] {message}", flush=True)


def normalize_model(value):
    value = str(value or "").strip()

    if value.startswith("models/"):
        value = value.split("/", 1)[1]

    if not value:
        return ""

    if re.search(r"[\s/:\\]", value):
        return ""

    if not value.lower().startswith("gemini-"):
        return ""

    return value


def read_existing_selection():
    if not OUTPUT_FILE.is_file():
        return {}

    try:
        data = json.loads(
            OUTPUT_FILE.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log(f"Previous selection could not be read: {exc}")
        return {}


def previous_selection_is_successful(data):
    if not isinstance(data, dict):
        return False

    if data.get("status") != "selected":
        return False

    model = normalize_model(data.get("model"))
    if not model:
        return False

    tested = data.get("tested_models", [])

    if isinstance(tested, list):
        for item in tested:
            if not isinstance(item, dict):
                continue

            if (
                normalize_model(item.get("model")) == model
                and item.get("success") is True
            ):
                return True

    # Earlier selector versions may store a successful selection
    # without a tested_models list.
    return bool(
        data.get("selection_source")
        or data.get("generation_test_performed") is False
    )


def configured_fallbacks(existing):
    """
    Priority:
    1. GEMINI_MODEL_FALLBACKS
    2. GEMINI_MODEL
    3. Previously successful selected model

    No hardcoded or invented model is added.
    """

    result = []

    raw = os.environ.get(
        "GEMINI_MODEL_FALLBACKS", ""
    )

    for item in re.split(r"[,;]", raw):
        model = normalize_model(item)

        if model and model not in result:
            result.append(model)

    primary = normalize_model(
        os.environ.get("GEMINI_MODEL", "")
    )

    if primary and primary not in result:
        result.insert(0, primary)

    if previous_selection_is_successful(existing):
        previous = normalize_model(
            existing.get("model")
        )

        if previous and previous not in result:
            result.append(previous)

    return result


def api_request(url):
    api_key = os.environ.get(
        "GEMINI_API_KEY", ""
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
        timeout=45,
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def discover_models():
    log("Checking available Gemini model names.")

    data = api_request(f"{BASE_URL}/models")
    available = {}

    for item in data.get("models", []):
        if not isinstance(item, dict):
            continue

        methods = item.get(
            "supportedGenerationMethods", []
        )

        if "generateContent" not in methods:
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
            ),
        }

    return available


def save_selection(
    model,
    resource,
    display_name,
    fallback_order,
    source,
    previous=None,
):
    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    result = {
        "status": "selected",
        "model": model,
        "model_resource": resource,
        "display_name": display_name,
        "selected_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "selection_source": source,
        "fallback_order": fallback_order,
        "tested_models": (
            previous.get("tested_models", [])
            if isinstance(previous, dict)
            else []
        ),
        "generation_test_performed": False,
        "selector_version": SELECTOR_VERSION,
    }

    temporary = OUTPUT_FILE.with_suffix(".json.tmp")

    try:
        with temporary.open(
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                result,
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

    return result


def reuse_previous(existing, fallbacks):
    if not previous_selection_is_successful(existing):
        return False

    model = normalize_model(existing.get("model"))

    if model not in fallbacks:
        return False

    existing["fallback_order"] = fallbacks
    existing["selection_source"] = (
        existing.get("selection_source")
        or "previous_successful_selection"
    )
    existing["selector_version"] = SELECTOR_VERSION
    existing["generation_test_performed"] = False

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
                existing,
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

    log(f"Reusing previously successful model: {model}")
    log("No model discovery or generation test performed.")
    return True


def main():
    log("Starting strict Gemini model selection.")

    existing = read_existing_selection()
    fallbacks = configured_fallbacks(existing)

    if not fallbacks:
        raise SystemExit(
            "ERROR: No Gemini model is configured and "
            "no previously successful model is available. "
            "Set GEMINI_MODEL or GEMINI_MODEL_FALLBACKS."
        )

    log("Allowed fallback order: " + " -> ".join(fallbacks))

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
            f"HTTP {exc.code}: {body[:700]}"
        )

    except Exception as exc:
        raise SystemExit(
            f"Gemini model discovery failed: {exc}"
        )

    for position, model in enumerate(
        fallbacks,
        start=1,
    ):
        log(f"Priority {position}: {model}")

        details = available.get(model)

        if not details:
            log(f"Unavailable or incompatible: {model}")
            continue

        selected = save_selection(
            model=model,
            resource=details["resource"],
            display_name=details["display_name"],
            fallback_order=fallbacks,
            source="explicit_configured_fallback",
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