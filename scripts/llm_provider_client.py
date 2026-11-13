#!/usr/bin/env python3
"""Multi-provider LLM client with ordered fallback for the Katha Lok AI pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from production_events import record_provider_attempt


VERSION = "llm-provider-client-v4-provider-attempt-audit"

SUPPORTED_PROVIDERS = (
    "openai",
    "groq",
    "openrouter",
    "cloudflare",
)

DEFAULT_PROVIDER_ORDER = (
    "openai",
    "groq",
    "openrouter",
    "cloudflare",
)

PROVIDER_ENV = {
    "groq": {
        "key": "GROQ_API_KEY",
        "model": "GROQ_MODEL",
        "url": "https://api.groq.com/openai/v1/chat/completions",
    },
    "openrouter": {
        "key": "OPENROUTER_API_KEY",
        "model": "OPENROUTER_MODEL",
        "url": "https://openrouter.ai/api/v1/chat/completions",
    },
    "cloudflare": {
        "key": "CLOUDFLARE_API_TOKEN",
        "model": "CLOUDFLARE_MODEL",
        "url": "",
    },
    "openai": {
        "key": "OPENAI_API_KEY",
        "model": "OPENAI_MODEL",
        "url": "https://api.openai.com/v1/chat/completions",
    },
}

RETRYABLE_STATUS_CODES = {
    408, 409, 425, 429, 500, 502, 503, 504,
}

MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_TIMEOUT = 180


class ProviderError(RuntimeError):
    """A provider request failed without exposing credentials."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        headers: Any = None,
    ):
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.headers = headers

        prefix = f"[{provider}]"
        if status_code is not None:
            prefix += f" HTTP {status_code}"

        super().__init__(f"{prefix} {message}")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1", "true", "yes", "on", "enabled",
    }



def get_provider_order() -> list[str]:
    """Return all configured supported non-Gemini providers in priority order."""

    raw = os.environ.get("LLM_PROVIDER_ORDER", "").strip()

    values = (
        re.split(r"[,;\s]+", raw)
        if raw
        else list(DEFAULT_PROVIDER_ORDER)
    )

    order: list[str] = []

    for value in values:
        provider = value.strip().lower()

        if not provider:
            continue

        if provider == "gemini":
            continue

        if provider not in SUPPORTED_PROVIDERS:
            raise ProviderError(
                "configuration",
                f"Unsupported provider in LLM_PROVIDER_ORDER: {provider}",
            )

        if provider not in order:
            order.append(provider)

    return order


def get_provider_model(provider: str) -> str:
    provider = _clean(provider).lower()

    if provider not in PROVIDER_ENV:
        raise ProviderError(
            provider or "unknown",
            "Provider is not supported.",
        )

    model_variable = PROVIDER_ENV[provider]["model"]
    model = _clean(os.environ.get(model_variable))

    if not model:
        raise ProviderError(
            provider,
            f"Model ID is not configured. Set {model_variable}.",
        )

    if len(model) > 200 or any(char.isspace() for char in model):
        raise ProviderError(
            provider,
            "Configured model ID is invalid.",
        )

    if provider == "cloudflare":
        if not model.startswith("@cf/") or len(model) <= 4:
            raise ProviderError(
                provider,
                "CLOUDFLARE_MODEL must use a valid @cf/ model namespace.",
            )
        if any(char.isspace() for char in model) or ".." in model:
            raise ProviderError(
                provider,
                "CLOUDFLARE_MODEL contains an invalid model path.",
            )

    return model


def get_provider_api_key(provider: str) -> str:
    provider = _clean(provider).lower()

    if provider not in PROVIDER_ENV:
        raise ProviderError(
            provider or "unknown",
            "Provider is not supported.",
        )

    variable = PROVIDER_ENV[provider]["key"]
    key = _clean(os.environ.get(variable))

    if not key:
        raise ProviderError(
            provider,
            f"API key is missing. Configure GitHub Secret {variable}.",
        )

    return key


def _cloudflare_url(model: str) -> str:
    account_id = _clean(os.environ.get("CLOUDFLARE_ACCOUNT_ID"))

    if not account_id:
        raise ProviderError(
            "cloudflare",
            "CLOUDFLARE_ACCOUNT_ID is missing.",
        )

    if not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
        raise ProviderError(
            "cloudflare",
            "CLOUDFLARE_ACCOUNT_ID is invalid.",
        )

    if not model.startswith("@cf/"):
        raise ProviderError(
            "cloudflare",
            "Cloudflare model must use its @cf/ model namespace.",
        )

    encoded_model = urllib.parse.quote(model, safe="@/-._")
    return (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/ai/run/{encoded_model}"
    )


def _extract_openai_text(data: dict, provider: str) -> str:
    choices = data.get("choices")

    if not isinstance(choices, list) or not choices:
        raise ProviderError(
            provider,
            "API response contains no choices.",
        )

    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError(provider, "Invalid response choice.")

    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderError(provider, "Response message is missing.")

    content = message.get("content")

    if isinstance(content, str):
        result = content.strip()

    elif isinstance(content, list):
        chunks = []

        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])

        result = "\n".join(chunks).strip()

    else:
        result = ""

    if not result:
        raise ProviderError(
            provider,
            "Model returned empty text.",
        )

    return result

def _decode_json_response(
    raw: bytes,
    provider: str,
) -> dict:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError(
            provider,
            "Response exceeded the maximum permitted size.",
        )

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            provider,
            "API returned invalid JSON.",
        ) from exc

    if not isinstance(data, dict):
        raise ProviderError(
            provider,
            "API response root is not a JSON object.",
        )

    return data



def _make_request(
    provider: str,
    url: str,
    api_key: str,
    payload: dict,
    timeout: int,
) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "KathaLokAI-PhotoMotion/1.0",
    }

    if provider == "openrouter":
        headers["HTTP-Referer"] = (
            "https://github.com/kathalokai01/lok-ai-automation-photo-motion"
        )
        headers["X-Title"] = "Katha Lok AI Photo Motion"

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)

        return _decode_json_response(raw, provider)

    except urllib.error.HTTPError as exc:
        status = int(exc.code)

        try:
            error_data = _decode_json_response(
                exc.read(MAX_RESPONSE_BYTES + 1),
                provider,
            )
            error_message = (
                error_data.get("error", {}).get("message")
                if isinstance(error_data.get("error"), dict)
                else error_data.get("message")
            )
        except Exception:
            error_message = ""

        # Never include request headers, API keys or raw response bodies.
        message = _clean(error_message)[:500] or "Provider request failed."

        raise ProviderError(
            provider,
            message,
            status_code=status,
            retryable=status in RETRYABLE_STATUS_CODES,
            headers=exc.headers,
        ) from None

    except urllib.error.URLError as exc:
        reason = _clean(getattr(exc, "reason", ""))

        raise ProviderError(
            provider,
            f"Network or connection failure: {reason or 'unknown error'}.",
            retryable=True,
        ) from None

    except TimeoutError:
        raise ProviderError(
            provider,
            "Request timed out.",
            retryable=True,
        ) from None



def selected_story_provider_candidates(selection: dict) -> list[str]:
    """Read the validated Story provider order without changing global provider priority."""
    if not isinstance(selection, dict):
        return []
    if selection.get("enabled") is not True or selection.get("status") != "ready":
        return []
    order = selection.get("provider_order")
    records = selection.get("providers")
    if not isinstance(order, list) or not isinstance(records, list):
        raise ProviderError("configuration", "Invalid selected Story provider schema.")
    ready = {}
    for item in records:
        if not isinstance(item, dict):
            raise ProviderError("configuration", "Invalid Story provider record.")
        name = _clean(item.get("provider")).lower()
        if name not in SUPPORTED_PROVIDERS:
            raise ProviderError("configuration", "Unsupported Story provider record.")
        if item.get("ready") is True:
            if name in ready:
                raise ProviderError("configuration", "Duplicate ready Story provider.")
            model = _clean(item.get("model"))
            if not model or len(model) > 200 or any(ch.isspace() for ch in model):
                raise ProviderError("configuration", "Invalid selected Story model.")
            if name == "cloudflare" and not model.startswith("@cf/"):
                raise ProviderError("configuration", "Invalid Cloudflare Story model namespace.")
            ready[name] = model
    result = []
    for value in order:
        name = _clean(value).lower()
        if name not in SUPPORTED_PROVIDERS:
            raise ProviderError("configuration", "Invalid Story provider order entry.")
        if name in ready and name not in [item.split("|", 1)[0] for item in result]:
            result.append(name + "|" + ready[name])
    stored_candidates = selection.get("story_candidates")
    stored_signature = selection.get("story_order_signature")
    if stored_candidates is not None or stored_signature is not None:
        signature = hashlib.sha256(
            "\n".join(result).encode("utf-8")
        ).hexdigest()
        if stored_candidates != result or stored_signature != signature:
            raise ProviderError(
                "configuration",
                "Saved Story provider order signature does not match ready providers.",
            )
    return result


def generate_text(
    provider: str,
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.25,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    Generate text using one explicitly selected configured provider.

    Returns:
        {
            "provider": str,
            "model": str,
            "text": str,
            "usage": dict
        }

    This function makes one API attempt. The calling pipeline controls
    fallback order and decides whether to try the next provider.
    """

    provider = _clean(provider).lower()

    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            provider or "unknown",
            "Provider is not supported by the LLM client.",
        )

    prompt = str(prompt or "").strip()

    if not prompt:
        raise ProviderError(provider, "Prompt is empty.")

    if len(prompt) > 500_000:
        raise ProviderError(
            provider,
            "Prompt exceeds the maximum permitted size.",
        )

    if isinstance(max_tokens, bool) or not 1 <= int(max_tokens) <= 32768:
        raise ProviderError(
            provider,
            "max_tokens must be between 1 and 32768.",
        )

    if isinstance(timeout, bool) or not 10 <= int(timeout) <= 600:
        raise ProviderError(
            provider,
            "timeout must be between 10 and 600 seconds.",
        )

    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        raise ProviderError(provider, "Invalid temperature.") from None

    if not 0.0 <= temperature <= 2.0:
        raise ProviderError(
            provider,
            "temperature must be between 0 and 2.",
        )

    api_key = get_provider_api_key(provider)
    selected_model = _clean(model) or get_provider_model(provider)

    messages = [
        {
            "role": "user",
            "content": prompt,
        }
    ]

    if provider == "cloudflare":
        url = _cloudflare_url(selected_model)
        payload = {
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": temperature,
        }
    else:
        url = PROVIDER_ENV[provider]["url"]
        payload = {
            "model": selected_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if provider == "openai":
            # Newer OpenAI models reject the legacy max_tokens field.
            payload["max_completion_tokens"] = int(max_tokens)
        else:
            # Groq and OpenRouter retain the OpenAI-compatible max_tokens API.
            payload["max_tokens"] = int(max_tokens)

    attempt_id = (
        f"llm:{provider}:{selected_model}:"
        f"{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12]}:{time.time_ns()}"
    )
    billable_marked = provider in {"openai", "openrouter"}
    record_provider_attempt(
        provider, selected_model, "request_started", billable_marked,
        scene_id="llm_text_generation", attempt_id=attempt_id,
    )
    try:
        data = _make_request(
            provider,
            url,
            api_key,
            payload,
            int(timeout),
        )
    except Exception as exc:
        record_provider_attempt(
            provider, selected_model, "request_failed", billable_marked,
            scene_id="llm_text_generation", attempt_id=attempt_id,
            error=f"{type(exc).__name__}: {exc}",
            http_status=getattr(exc, "status_code", None),
        )
        raise
    record_provider_attempt(
        provider, selected_model, "response_received", billable_marked,
        scene_id="llm_text_generation", attempt_id=attempt_id,
    )

    if provider == "cloudflare":
        if data.get("success") is False:
            errors = data.get("errors", [])
            detail = "Cloudflare generation failed."

            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    detail = _clean(first.get("message")) or detail

            raise ProviderError(provider, detail[:500])

        result = data.get("result")
        if not isinstance(result, dict):
            raise ProviderError(
                provider,
                "Cloudflare response has no result object.",
            )

        content = result.get("response")

        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                provider,
                "Cloudflare returned empty text.",
            )

        text = content.strip()
        usage = result.get("usage", {})

    else:
        text = _extract_openai_text(data, provider)
        usage = data.get("usage", {})

    if not isinstance(usage, dict):
        usage = {}

    result = {
        "provider": provider,
        "model": selected_model,
        "text": text,
        "usage": usage,
        "version": VERSION,
    }
    record_provider_attempt(
        provider, selected_model, "accepted", billable_marked,
        scene_id="llm_text_generation", attempt_id=attempt_id,
    )
    return result


def provider_configuration_status() -> list[dict]:
    """Report configured providers without revealing API credentials."""

    status = []

    for provider in get_provider_order():
        key_name = PROVIDER_ENV[provider]["key"]
        model_name = PROVIDER_ENV[provider]["model"]

        key_present = bool(_clean(os.environ.get(key_name)))
        model_present = bool(_clean(os.environ.get(model_name)))

        status.append({
            "provider": provider,
            "api_key_configured": key_present,
            "model_configured": model_present,
            "ready": key_present and model_present,
        })

    return status
