
#!/usr/bin/env python3
"""Select an available Gemini text model without running a paid generation test."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = ROOT / "output" / "config" / "selected_model.json"

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED_MODEL = "gemini-3.5-flash-lite"
TIMEOUT = 30
MAX_MODEL_PAGES = 5
VERSION = "safe-gemini-selector-v5-list-available-models"
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def log(*items: object) -> None:
    print(*items, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_model_name(value: str) -> bool:
    return bool(MODEL_PATTERN.fullmatch(value or ""))


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def get_api_key() -> str:
    return (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )


def atomic_save(data: dict[str, Any]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_FILE.with_name(OUTPUT_FILE.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, OUTPUT_FILE)
    finally:
        temporary.unlink(missing_ok=True)

    saved = read_json(OUTPUT_FILE)
    if not saved or saved.get("model") != data.get("model"):
        raise RuntimeError("Model selection read-back validation failed.")


def save_selection(
    model: str,
    *,
    verified: bool,
    reason: str,
    display_name: str = "",
    models_listed: int = 0,
) -> None:
    if not valid_model_name(model):
        raise RuntimeError("Refusing to save an invalid model identifier.")

    atomic_save({
        "version": VERSION,
        "status": "selected",
        "model": model,
        "model_resource": f"models/{model}",
        "display_name": display_name,
        "selected_at": utc_now(),
        "verified": verified,
        "verification_method": (
            "models_api_list_only" if verified else "local_fallback"
        ),
        "reason": reason,
        "compatible_models_listed": models_listed,
        "generation_test_performed": False,
        "external_generation_calls": 0,
        "paid_generation_test_performed": False,
    })


def api_get(url: str, api_key: str) -> dict[str, Any]:
    separator = "&" if "?" in url else "?"
    request_url = (
        url + separator
        + urllib.parse.urlencode({"key": api_key})
    )
    request = urllib.request.Request(
        request_url,
        headers={"Accept": "application/json"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(
            f"Gemini model-list HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Gemini model-list request failed ({type(exc).__name__})."
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError("Gemini returned an invalid model-list response.")

    return result


def list_compatible_models(api_key: str) -> list[dict[str, str]]:
    url = BASE_URL + "/models?pageSize=100"
    compatible: list[dict[str, str]] = []
    seen_models: set[str] = set()
    seen_tokens: set[str] = set()
    page_count = 0

    while url and page_count < MAX_MODEL_PAGES:
        result = api_get(url, api_key)
        page_count += 1

        entries = result.get("models", [])
        if not isinstance(entries, list):
            raise RuntimeError("Gemini model-list response has invalid models.")

        for item in entries:
            if not isinstance(item, dict):
                continue

            resource = str(item.get("name", "")).strip()
            methods = item.get("supportedGenerationMethods", [])

            if not resource.startswith("models/"):
                continue
            if not isinstance(methods, list) or "generateContent" not in methods:
                continue

            model = resource.split("/", 1)[1]
            lowered = model.lower()

            if not valid_model_name(model) or model in seen_models:
                continue
            if "gemini" not in lowered:
                continue
            if any(term in lowered for term in (
                "embedding", "image", "vision", "audio", "tts",
                "robotics", "aqa",
            )):
                continue

            seen_models.add(model)
            compatible.append({
                "model": model,
                "display_name": str(item.get("displayName", "")),
            })

        token = str(result.get("nextPageToken", "")).strip()
        if not token:
            url = ""
            break
        if token in seen_tokens:
            raise RuntimeError("Gemini repeated a pagination token.")

        seen_tokens.add(token)
        url = BASE_URL + "/models?" + urllib.parse.urlencode({
            "pageSize": "100",
            "pageToken": token,
        })

    if url:
        raise RuntimeError(
            f"Gemini model listing exceeded {MAX_MODEL_PAGES} pages."
        )

    return compatible


def choose_model(
    models: list[dict[str, str]],
    existing_model: str = "",
) -> dict[str, str]:
    if not models:
        raise RuntimeError(
            "The API key returned no compatible Gemini text models."
        )

    by_name = {item["model"]: item for item in models}

    # Preserve the current model if it is still available.
    if existing_model in by_name:
        return by_name[existing_model]

    # Use the preferred model only when the API lists it.
    if PREFERRED_MODEL in by_name:
        return by_name[PREFERRED_MODEL]

    def priority(item: dict[str, str]) -> tuple[int, str]:
        name = item["model"].lower()

        # Prefer stable names over experimental/preview variants.
        experimental = any(
            token in name
            for token in ("preview", "experimental", "exp-", "latest")
        )
        stability = 1 if experimental else 0

        if "flash" in name and "lite" in name:
            return stability, "0-" + name
        if "flash" in name:
            return stability, "1-" + name
        if "pro" in name:
            return stability, "2-" + name
        return stability, "3-" + name

    return sorted(models, key=priority)[0]


def main() -> int:
    log("===== GEMINI TEXT MODEL SELECTION =====")

    existing = read_json(OUTPUT_FILE) or {}
    existing_model = str(existing.get("model", "")).strip()
    api_key = get_api_key()

    if not api_key:
        # Avoid network calls when no key is configured.
        if (
            existing.get("status") == "selected"
            and valid_model_name(existing_model)
            and existing.get("model_resource") == f"models/{existing_model}"
        ):
            log("No API key: retaining existing configured model.")
            log("Model:", existing_model)
            log("Availability verified: no")
            log("Network requests: 0")
            return 0

        save_selection(
            PREFERRED_MODEL,
            verified=False,
            reason=(
                "No Gemini API key available. Local default only; model "
                "availability is unverified. No generation request made."
            ),
        )
        log("No API key; saved unverified local default:", PREFERRED_MODEL)
        log("Network requests: 0")
        return 0

    log("Checking available Gemini text models.")
    log("This step only lists models; it does not generate text.")

    models = list_compatible_models(api_key)
    selected = choose_model(models, existing_model)

    save_selection(
        selected["model"],
        verified=True,
        display_name=selected.get("display_name", ""),
        reason=(
            "Model was returned by the Gemini API and supports generateContent. "
            "No generation test was performed."
        ),
        models_listed=len(models),
    )

    log("Selected model:", selected["model"])
    log("Compatible text models listed:", len(models))
    log("Available in model list: yes")
    log("Generation test performed: no")
    log("Paid generation test performed: no")
    log("Saved:", OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Model selection cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("MODEL SELECTION FAILED:", str(exc))
        raise SystemExit(1)
