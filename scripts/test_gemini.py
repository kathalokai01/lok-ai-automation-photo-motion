#!/usr/bin/env python3
"""Validate the selected Gemini model with an exact-response test."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT_SECONDS = 60


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    if not MODEL_FILE.is_file():
        raise RuntimeError(
            f"Selected model file not found: {MODEL_FILE}"
        )

    try:
        config = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot read selected model configuration: {exc}"
        ) from exc

    if not isinstance(config, dict):
        raise RuntimeError("Selected model configuration must be an object.")

    if str(config.get("status", "")).strip().lower() != "selected":
        raise RuntimeError(
            "Selected model configuration status is not 'selected'."
        )

    model = str(config.get("model", "")).strip()

    if not model or model.startswith("models/") or "/" in model:
        raise RuntimeError(
            f"Invalid model identifier in selected model file: {model!r}"
        )

    encoded_model = urllib.parse.quote(model, safe="-._")
    url = f"{BASE_URL}/models/{encoded_model}:generateContent"

    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": "Reply with exactly: GEMINI_OK"}],
        }],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 20,
        },
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    print("=" * 55)
    print("KATHA LOK AI - GEMINI CONNECTION TEST")
    print(f"Selected model: {model}")
    print("Checking exact response...")
    print("=" * 55)

    try:
        with urllib.request.urlopen(
            request,
            timeout=TIMEOUT_SECONDS,
        ) as response:
            result = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini API returned HTTP {exc.code}: {body[:1000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not connect to Gemini API: {exc.reason}"
        ) from exc

    candidates = result.get("candidates", [])

    if not candidates or not isinstance(candidates[0], dict):
        raise RuntimeError("Gemini returned no valid candidates.")

    candidate = candidates[0]
    finish_reason = str(candidate.get("finishReason", "")).upper()

    if finish_reason in {
        "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT",
        "MAX_TOKENS",
    }:
        raise RuntimeError(
            f"Gemini test response was blocked or incomplete: {finish_reason}"
        )

    parts = candidate.get("content", {}).get("parts", [])
    response_text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
    ).strip()

    if response_text != "GEMINI_OK":
        raise RuntimeError(
            "Gemini did not return the exact expected text. "
            f"Received: {response_text[:300]!r}"
        )

    print(f"Model response: {response_text}")
    print("Gemini API test: PASSED")
    print("Exact response validation: PASSED")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)