
#!/usr/bin/env python3

"""
Katha Lok AI - Gemini Model Selector

Goals:
- Reuse an already selected compatible model.
- Preserve explicitly configured fallback order.
- Never test every model with paid generation requests.
- Use model-list API only when selection cannot be reused.
- Do not silently invent additional fallback providers.
- Save selection atomically.
"""

import os
import json
import hashlib
import urllib.request
import urllib.error

from pathlib import Path
from datetime import datetime, timezone


BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
)

OUTPUT_FILE = Path(
    "output/config/selected_model.json"
)

PREFERRED_MODEL = "gemini-3.5-flash-lite"

API_KEY = os.environ.get(
    "GEMINI_API_KEY", ""
).strip()


def log(message):
    print(f"[GEMINI SELECTOR] {message}", flush=True)


def normalize_model(value):
    value = str(value or "").strip()

    if value.startswith("models/"):
        value = value.split("/", 1)[1]

    if not value:
        return ""

    if any(char in value for char in "\r\n\t "):
        return ""

    if "/" in value or ":" in value:
        return ""

    if not value.lower().startswith("gemini-"):
        return ""

    return value


def read_existing_selection():
    if not OUTPUT_FILE.is_file():
        return {}

    try:
        with OUTPUT_FILE.open(
            "r", encoding="utf-8"
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {}

        return data

    except (OSError, ValueError) as exc:
        log(f"Existing selection unreadable: {exc}")
        return {}


def configured_fallbacks():
    """
    Explicit fallback order:
    GEMINI_MODEL_FALLBACKS
    GEMINI_MODEL
    existing selected model
    preferred model

    Comma or semicolon separated.
    """

    values = []

    raw = os.environ.get(
        "GEMINI_MODEL_FALLBACKS", ""
    )

    for item in raw.replace(";", ",").split(","):
        model = normalize_model(item)

        if model and model not in values:
            values.append(model)

    primary = normalize_model(
        os.environ.get("GEMINI_MODEL", "")
    )

    if primary and primary not in values:
        values.insert(0, primary)

    existing = read_existing_selection()

    previous = normalize_model(
        existing.get("model")
    )

    if previous and previous not in values:
        values.append(previous)

    preferred = normalize_model(
        PREFERRED_MODEL
    )

    if preferred and preferred not in values:
        values.append(preferred)

    return values


def api_request(url, timeout=45):
    request = urllib.request.Request(
        url,
        headers={
            "x-goog-api-key": API_KEY,
            "Accept": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(
        request, timeout=timeout
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def discover_models():
    """
    One GET request only.
    No generateContent testing here.
    """

    log("Discovering compatible Gemini models.")

    result = api_request(
        f"{BASE_URL}/models"
    )

    available = {}

    for item in result.get("models", []):
        if not isinstance(item, dict):
            continue

        resource = str(
            item.get("name", "")
        ).strip()

        methods = item.get(
            "supportedGenerationMethods", []
        )

        if "generateContent" not in methods:
            continue

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
    selection_source,
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
        "selection_source": selection_source,
        "fallback_order": fallback_order,
        "tested_models": [],
        "generation_test_performed": False,
        "selector_version": 2,
    }

    temp_file = OUTPUT_FILE.with_suffix(
        ".json.tmp"
    )

    try:
        with temp_file.open(
            "w", encoding="utf-8"
        ) as file:
            json.dump(
                result,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temp_file,
            OUTPUT_FILE,
        )

    finally:
        if temp_file.exists():
            temp_file.unlink()

    return result


def can_reuse_selection(existing, fallbacks):
    if not isinstance(existing, dict):
        return False

    if existing.get("status") != "selected":
        return False

    if existing.get("selector_version") != 2:
        return False

    model = normalize_model(
        existing.get("model")
    )

    if not model:
        return False

    if model not in fallbacks:
        return False

    return True


def main():
    log("Starting model selection.")

    if not API_KEY:
        raise SystemExit(
            "ERROR: GEMINI_API_KEY is not set."
        )

    fallbacks = configured_fallbacks()

    if not fallbacks:
        raise SystemExit(
            "ERROR: No configured Gemini models."
        )

    existing = read_existing_selection()

    # --------------------------------------------------------
    # REUSE PREVIOUS SUCCESSFUL SELECTION
    # --------------------------------------------------------

    if can_reuse_selection(
        existing, fallbacks
    ):
        model = existing["model"]

        log(
            f"Reusing previously selected model: {model}"
        )

        existing["fallback_order"] = fallbacks
        existing["selection_source"] = (
            "reused_previous_selection"
        )
        existing["generation_test_performed"] = False

        temp_file = OUTPUT_FILE.with_suffix(
            ".json.tmp"
        )

        try:
            with temp_file.open(
                "w", encoding="utf-8"
            ) as file:
                json.dump(
                    existing,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())

            os.replace(temp_file, OUTPUT_FILE)

        finally:
            if temp_file.exists():
                temp_file.unlink()

        log("No model discovery or test request needed.")
        log(f"Selected model: {model}")
        return

    # --------------------------------------------------------
    # DISCOVERY: ONE GET REQUEST
    # --------------------------------------------------------

    try:
        available = discover_models()

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8", errors="replace"
        )

        raise SystemExit(
            f"ERROR: Gemini model discovery failed. "
            f"HTTP {exc.code}: {body[:700]}"
        )

    except Exception as exc:
        raise SystemExit(
            f"ERROR: Gemini model discovery failed: {exc}"
        )

    # --------------------------------------------------------
    # SELECT ONLY CONFIGURED MODELS
    # --------------------------------------------------------

    log("Checking configured fallback order.")

    for position, model in enumerate(
        fallbacks, start=1
    ):
        log(
            f"Priority {position}: {model}"
        )

        details = available.get(model)

        if not details:
            log(
                f"Unavailable or incompatible: {model}"
            )
            continue

        selected = save_selection(
            model=model,
            resource=details["resource"],
            display_name=details["display_name"],
            fallback_order=fallbacks,
            selection_source="configured_fallback_order",
        )

        log("GEMINI MODEL SELECTED")
        log(f"Model: {selected['model']}")
        log(f"Saved: {OUTPUT_FILE}")
        log(
            "Generation test: NOT performed by selector"
        )
        log(
            "The separate test_gemini.py step "
            "performs the actual API test."
        )

        return

    # --------------------------------------------------------
    # NO CONFIGURED MODEL AVAILABLE
    # --------------------------------------------------------

    raise SystemExit(
        "ERROR: None of the configured Gemini "
        "fallback models supports generateContent. "
        "No unconfigured model was substituted."
    )


if __name__ == "__main__":
    main()
