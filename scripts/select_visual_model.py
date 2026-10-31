#!/usr/bin/env python3
"""Select and record configured visual providers without surprise API calls."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from input_config import (
    load_and_validate,
    get_image_provider_settings,
)

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_FILE = ROOT / "output" / "config" / "selected_visual_model.json"

VERSION = "configured-visual-provider-selector-v3"


def log(*items):
    print(*items, flush=True)


def save_selection(data):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_FILE.with_suffix(".json.tmp")

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_FILE)


def provider_model(provider, settings):
    if provider == "huggingface":
        return {
            "provider": provider,
            "model": settings["HF_IMAGE_SPACE"],
            "fallback_models": settings["FREE_IMAGE_FALLBACK_SPACES"],
        }

    if provider == "pollinations":
        models = settings["POLLINATIONS_FREE_IMAGE_MODELS"]
        return {
            "provider": provider,
            "model": models[0],
            "fallback_models": models[1:],
        }

    if provider == "gemini":
        return {
            "provider": provider,
            "model": settings["GEMINI_IMAGE_MODEL"],
            "fallback_models": [],
        }

    if provider == "replicate":
        return {
            "provider": provider,
            "model": settings["REPLICATE_IMAGE_MODEL"],
            "fallback_models": [],
        }

    raise ValueError(f"Unsupported visual provider: {provider}")


def main():
    log("=" * 60)
    log("KATHA LOK AI — CONFIGURED VISUAL PROVIDER SELECTION")
    log("=" * 60)

    if not INPUT_FILE.is_file():
        log("ERROR: Input/topic.txt is missing.")
        return 1

    try:
        config = load_and_validate(INPUT_FILE)
        settings = get_image_provider_settings(config)

        order = settings["IMAGE_PROVIDER_ORDER"]
        fallback_enabled = settings["IMAGE_FALLBACK_ENABLED"]
        allow_billable = settings["IMAGE_ALLOW_BILLABLE"]

        if not order:
            raise ValueError("No image provider is configured.")

        selected = provider_model(order[0], settings)

        fallback_records = []
        if fallback_enabled:
            for provider in order[1:]:
                fallback_records.append(
                    provider_model(provider, settings)
                )

        data = {
            "version": VERSION,
            "status": "selected",
            "format": config["FORMAT"],
            "provider": selected["provider"],
            "model": selected["model"],
            "fallback_models": selected["fallback_models"],
            "provider_order": order,
            "fallback_enabled": fallback_enabled,
            "allow_billable_providers": allow_billable,
            "fallback_providers": fallback_records,
            "selected_by": "Input/topic.txt",
            "tested": False,
            "availability_confirmed": False,
            "external_api_calls": 0,
            "paid_services_used": False,
            "timestamp": int(time.time()),
            "note": (
                "This file records configured provider priority only. "
                "Actual provider success is determined by the image "
                "generator during scene generation."
            ),
        }

        save_selection(data)

        log("FORMAT:", config["FORMAT"])
        log("Primary provider:", selected["provider"])
        log("Primary model:", selected["model"])
        log("Fallback enabled:", fallback_enabled)
        log("Billable providers allowed:", allow_billable)
        log("Provider order:", " -> ".join(order))

        for number, item in enumerate(fallback_records, 1):
            log(
                f"Fallback {number}:",
                item["provider"],
                item["model"],
            )

        log("External API calls: 0")
        log("Availability tested: no")
        log("Selection saved:", OUTPUT_FILE)
        log("VISUAL PROVIDER SELECTION: OK")

        return 0

    except Exception as exc:
        failure = {
            "version": VERSION,
            "status": "failed",
            "provider": None,
            "model": None,
            "tested": False,
            "availability_confirmed": False,
            "external_api_calls": 0,
            "timestamp": int(time.time()),
            "error": f"{type(exc).__name__}: {exc}",
        }

        try:
            save_selection(failure)
        except OSError as save_error:
            log("Could not save failure record:", save_error)

        log("VISUAL PROVIDER SELECTION: FAILED")
        log(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)