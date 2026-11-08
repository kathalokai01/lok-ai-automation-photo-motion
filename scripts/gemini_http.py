#!/usr/bin/env python3
"""Shared HTTP handling for Gemini API model fallback and rate limits."""

from __future__ import annotations

import datetime
import email.utils
import json
import re
from typing import Any

RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
PERMANENT_MODEL_STATUSES = {400, 404}


def http_status(error: BaseException) -> int | None:
    value = getattr(error, "code", None)
    if value is None:
        value = getattr(error, "status_code", None)
    if value is None:
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def http_error_body(error: BaseException, limit: int = 3000) -> str:
    cached = getattr(error, "_gemini_error_body", None)
    if isinstance(cached, str):
        return cached[:limit]
    reader = getattr(error, "read", None)
    if callable(reader):
        try:
            body = reader(limit + 1)
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            body = str(body)
        except Exception:
            body = ""
    else:
        response = getattr(error, "response", None)
        if response is None:
            response = error
        body = getattr(response, "text", "") if response is not None else ""
        if not body and response is not None:
            raw = getattr(response, "content", b"")
            if isinstance(raw, bytes):
                body = raw.decode("utf-8", errors="replace")
            else:
                body = str(raw or "")
    try:
        setattr(error, "_gemini_error_body", body)
    except Exception:
        pass
    return body[:limit]


def _retry_delay_from_body(body: str) -> float | None:
    if not body:
        return None
    try:
        data: Any = json.loads(body)
    except (TypeError, ValueError):
        return None
    error = data.get("error") if isinstance(data, dict) else None
    details = error.get("details", []) if isinstance(error, dict) else []
    if not isinstance(details, list):
        return None
    for item in details:
        if not isinstance(item, dict):
            continue
        retry_delay = item.get("retryDelay")
        if isinstance(retry_delay, str):
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", retry_delay)
            if match:
                return float(match.group(1))
    return None


def retry_after_seconds(
    error: BaseException,
    attempt: int,
    *,
    default_base: float = 12.0,
    maximum: float = 120.0,
) -> int:
    """Honor Retry-After / Google RetryInfo, then use bounded backoff."""
    headers = getattr(error, "headers", None)
    if headers is None:
        response = getattr(error, "response", None)
        if response is None:
            response = error
        headers = getattr(response, "headers", None)
    retry_after = None
    if headers is not None:
        try:
            retry_after = headers.get("Retry-After")
        except Exception:
            retry_after = None

    delay: float | None = None
    if retry_after:
        try:
            delay = max(0.0, float(retry_after.strip()))
        except (TypeError, ValueError):
            try:
                parsed = email.utils.parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                delay = max(
                    0.0,
                    (parsed - datetime.datetime.now(datetime.timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                delay = None

    if delay is None:
        delay = _retry_delay_from_body(http_error_body(error))

    if delay is None:
        delay = default_base * (2 ** max(0, int(attempt) - 1))

    return int(min(maximum, max(1.0, delay)))


def is_retryable_http_status(status: int | None) -> bool:
    return status in RETRYABLE_HTTP_STATUSES


def is_permanent_model_status(status: int | None) -> bool:
    return status in PERMANENT_MODEL_STATUSES
