#!/usr/bin/env python3
"""Zero-cost-only LLM provider client for the Katha Lok AI pipeline."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


VERSION = "llm-provider-client-v1"

SUPPORTED_PROVIDERS = (
    "groq",
    "openrouter",
    "cloudflare",
    "mistral",
)

DEFAULT_PROVIDER_ORDER = (
    "groq",
    "openrouter",
    "cloudflare",
    "mistral",
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
    "mistral": {
        "key": "MISTRAL_API_KEY",
        "model": "MISTRAL_MODEL",
        "url": "https://api.mistral.ai/v1/chat/completions",
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
    ):
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable

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


def _require_zero_cost_mode() -> None:
    if not _env_bool("LLM_ZERO_COST_ONLY", False):
        raise ProviderError(
            "configuration",
            "LLM_ZERO_COST_ONLY must be true. Paid API calls are disabled.",
        )


def get_provider_order() -> list[str]:
    """Return only explicitly supported non-Gemini free-tier providers."""

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

    if provider == "openrouter" and not model.endswith(":free"):
        raise ProviderError(
            provider,
            "Only OpenRouter model IDs ending in :free are allowed.",
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


def _require_provider_free_mode(provider: str) -> None:
    confirmation_variables = {
        "groq": "GROQ_FREE_TIER_CONFIRMED",
        "cloudflare": "CLOUDFLARE_FREE_TIER_CONFIRMED",
        "mistral": "MISTRAL_FREE_MODE_CONFIRMED",
    }

    variable = confirmation_variables.get(provider)

    if variable and not _env_bool(variable, False):
        raise ProviderError(
            provider,
            (
                f"Free-tier confirmation is required. "
                f"Set {variable}=true only after confirming "
                "the account is configured for free usage without paid billing."
            ),
        )


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
    Generate text using one explicitly selected free-tier provider.

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

    _require_zero_cost_mode()

    provider = _clean(provider).lower()

    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            provider or "unknown",
            "Provider is not supported by the free-tier client.",
        )

    _require_provider_free_mode(provider)

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

    # Apply the OpenRouter free-only rule even when the model is passed
    # directly by the calling script.
    if provider == "openrouter" and not selected_model.endswith(":free"):
        raise ProviderError(
            provider,
            "Paid OpenRouter model IDs are blocked. Use a :free model only.",
        )

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
            "max_tokens": int(max_tokens),
            "temperature": temperature,
            "stream": False,
        }

    data = _make_request(
        provider,
        url,
        api_key,
        payload,
        int(timeout),
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

    return {
        "provider": provider,
        "model": selected_model,
        "text": text,
        "usage": usage,
        "version": VERSION,
    }


def provider_configuration_status() -> list[dict]:
    """Report configured providers without revealing API credentials."""

    status = []

    for provider in get_provider_order():
        key_name = PROVIDER_ENV[provider]["key"]
        model_name = PROVIDER_ENV[provider]["model"]

        key_present = bool(_clean(os.environ.get(key_name)))
        model_present = bool(_clean(os.environ.get(model_name)))

        free_confirmed = (
            provider == "openrouter"
            or _env_bool(
                {
                    "groq": "GROQ_FREE_TIER_CONFIRMED",
                    "cloudflare": "CLOUDFLARE_FREE_TIER_CONFIRMED",
                    "mistral": "MISTRAL_FREE_MODE_CONFIRMED",
                }[provider],
                False,
            )
        )

        status.append({
            "provider": provider,
            "api_key_configured": key_present,
            "model_configured": model_present,
            "free_mode_confirmed": free_confirmed,
            "ready": key_present and model_present and free_confirmed,
        })

    return status