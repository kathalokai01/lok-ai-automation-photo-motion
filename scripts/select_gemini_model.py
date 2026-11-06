#!/usr/bin/env python3
"""Prepare Gemini model priority without blocking on model discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OUTPUT_FILE = Path("output/config/selected_model.json")
SELECTOR_VERSION = 6

DEFAULT_PRIMARY_MODEL = "gemini-3.8-flash"

DEFAULT_FALLBACK_MODELS = (
    "gemini-4-argon",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)


def log(message: str) -> None:
    print(f"[GEMINI SELECTOR] {message}", flush=True)


def normalize_model(value: Any) -> str:
    model = str(value or "").strip()

    if model.startswith("models/"):
        model = model.split("/", 1)[1]

    if not model:
        return ""

    if not re.fullmatch(
        r"gemini-[A-Za-z0-9][A-Za-z0-9._-]*",
        model,
        flags=re.IGNORECASE,
    ):
        return ""

    return model


def read_existing_selection() -> dict:
    if not OUTPUT_FILE.is_file():
        return {}

    try:
        data = json.loads(
            OUTPUT_FILE.read_text(encoding="utf-8-sig")
        )
        return data if isinstance(data, dict) else {}

    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log(f"Previous selection ignored: {exc}")
        return {}


def previous_selection_is_successful(data: dict) -> bool:
    if not isinstance(data, dict):
        return False

    model = normalize_model(data.get("model"))
    if not model:
        return False

    api_test = data.get("api_test")

    if isinstance(api_test, dict):
        if (
            api_test.get("status") == "passed"
            and normalize_model(api_test.get("model")) == model
        ):
            return True

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

    return False


def parse_model_list(raw: str, setting_name: str) -> list[str]:
    result = []

    for item in re.split(r"[,;]", str(raw or "")):
        item = item.strip()

        if not item:
            continue

        model = normalize_model(item)

        if not model:
            raise ValueError(
                f"Invalid Gemini model in {setting_name}: {item!r}"
            )

        if model not in result:
            result.append(model)

    return result


def configured_fallbacks(existing: dict) -> list[str]:
    """
    Preserve configured priority.

    GEMINI_MODEL:
        Primary model.

    GEMINI_MODEL_FALLBACKS:
        Explicit fallback order.

    If no fallback variable is supplied, retain the repository's
    existing default fallback order.

    A previously successful model may be appended last, but it
    never moves ahead of the configured models.
    """

    raw_primary = os.environ.get("GEMINI_MODEL", "").strip()
    raw_fallbacks = os.environ.get(
        "GEMINI_MODEL_FALLBACKS", ""
    ).strip()

    primary = normalize_model(raw_primary)

    if raw_primary and not primary:
        raise ValueError(
            f"Invalid GEMINI_MODEL value: {raw_primary!r}"
        )

    configured = []

    if primary:
        configured.append(primary)

    explicit_fallbacks = parse_model_list(
        raw_fallbacks,
        "GEMINI_MODEL_FALLBACKS",
    )

    if explicit_fallbacks:
        for model in explicit_fallbacks:
            if model not in configured:
                configured.append(model)

    elif primary:
        for model in DEFAULT_FALLBACK_MODELS:
            if model not in configured:
                configured.append(model)

    elif not raw_fallbacks:
        configured.append(DEFAULT_PRIMARY_MODEL)

        for model in DEFAULT_FALLBACK_MODELS:
            if model not in configured:
                configured.append(model)

    if not configured:
        raise RuntimeError(
            "No Gemini model candidates are configured."
        )

    if previous_selection_is_successful(existing):
        previous = normalize_model(existing.get("model"))

        if previous and previous not in configured:
            configured.append(previous)
            log(
                "Previously successful model retained at the "
                "end of the fallback order."
            )

    return configured


def selection_signature(models: list[str]) -> str:
    payload = "\n".join(models).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_save(data: dict) -> None:
    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(OUTPUT_FILE.parent),
            prefix=".selected-model-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)

            json.dump(
                data,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, OUTPUT_FILE)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

def build_selection(
    models: list[str],
    previous: dict,
) -> dict:
    selected = models[0]

    previous_tests = previous.get("tested_models", [])
    if not isinstance(previous_tests, list):
        previous_tests = []

    previous_failures = previous.get("failed_model_tests", [])
    if not isinstance(previous_failures, list):
        previous_failures = []

    result = {
        "status": "selected",
        "model": selected,
        "model_resource": f"models/{selected}",
        "display_name": selected,
        "selected_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "selection_source": "configured_priority_order",
        "configured_primary_model": selected,
        "fallback_order": models,
        "fallback_signature": selection_signature(models),
        "tested_models": previous_tests,
        "failed_model_tests": previous_failures,
        "generation_test_performed": False,
        "selector_version": SELECTOR_VERSION,
    }

    previous_model = normalize_model(previous.get("model"))

    if previous_model == selected:
        previous_api_test = previous.get("api_test")

        if isinstance(previous_api_test, dict):
            if (
                previous_api_test.get("status") == "passed"
                and normalize_model(
                    previous_api_test.get("model")
                ) == selected
            ):
                result["api_test"] = previous_api_test

    return result


def main() -> None:
    log("Preparing configured Gemini model order.")

    previous = read_existing_selection()
    models = configured_fallbacks(previous)

    log("Configured priority order:")
    for index, model in enumerate(models, start=1):
        log(f"{index}. {model}")

    result = build_selection(models, previous)

    atomic_save(result)

    saved = json.loads(
        OUTPUT_FILE.read_text(encoding="utf-8")
    )

    if saved.get("status") != "selected":
        raise RuntimeError("Gemini selection status was not saved.")

    if saved.get("model") != models[0]:
        raise RuntimeError(
            "Saved primary model does not match configured priority."
        )

    if saved.get("fallback_order") != models:
        raise RuntimeError(
            "Saved fallback order does not match configured order."
        )

    if saved.get("fallback_signature") != selection_signature(models):
        raise RuntimeError("Saved fallback signature is invalid.")

    log("GEMINI MODEL PRIORITY SAVED")
    log("Selected primary: " + models[0])
    log("Fallback count: " + str(len(models) - 1))
    log("Generation fallback: handled by generate_story.py")
    log("No model-discovery API request was made.")


if __name__ == "__main__":
    main()