#!/usr/bin/env python3
"""Optional OVHcloud AI Endpoints and AI Horde image adapters.

Both adapters return raw image bytes. Provider-specific polling and HTTP
validation live here; image normalization and semantic validation remain in
free_image_generator.py.
"""
from __future__ import annotations

import base64
import os
import time
from urllib.parse import urlparse

import requests

SESSION = requests.Session()
OVH_IMAGE_URL = "https://stable-diffusion-xl.endpoints.kepler.ai.cloud.ovh.net/api/text2image"
OVH_DEFAULT_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
HORDE_BASE_URL = "https://aihorde.net/api/v2"
MAX_IMAGE_BYTES = 80 * 1024 * 1024


def _json(response, label):
    response.raise_for_status()
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned an invalid response")
    return value


def _image_from_payload(payload, label):
    rows = payload.get("data")
    if isinstance(rows, list) and rows:
        row = rows[0]
        if isinstance(row, dict):
            encoded = row.get("b64_json")
            if encoded:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError(f"{label} returned invalid base64") from exc
                if raw and len(raw) <= MAX_IMAGE_BYTES:
                    return raw
            url = row.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise RuntimeError(f"{label} returned an unsafe image URL")
                image = SESSION.get(url, timeout=(20, 120))
                image.raise_for_status()
                if image.content and len(image.content) <= MAX_IMAGE_BYTES:
                    return image.content
    raise RuntimeError(f"{label} response contains no usable image")


def generate_ovhcloud_image(prompt, size, seed, negative_prompt=""):
    """Call OVHcloud's documented SDXL raw-image endpoint (anonymous or keyed)."""
    api_key = os.getenv("OVHCLOUD_AI_ENDPOINTS_API_KEY", "").strip()
    headers = {
        "Accept": "application/octet-stream",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = SESSION.post(
        OVH_IMAGE_URL,
        headers=headers,
        json={
            "prompt": prompt[:12000],
            "negative_prompt": negative_prompt[:4000],
        },
        timeout=(30, 240),
    )
    response.raise_for_status()
    content_type = str(response.headers.get("content-type", "")).lower()
    if (
        content_type.startswith("image/")
        or "octet-stream" in content_type
    ):
        raw = response.content
        if raw and len(raw) <= MAX_IMAGE_BYTES:
            return raw
        raise RuntimeError("OVHcloud returned an empty or oversized image")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"OVHcloud returned unexpected content type: {content_type or 'unknown'}"
        ) from exc
    errors = payload.get("errors") if isinstance(payload, dict) else None
    detail = "; ".join(str(item)[:200] for item in errors) if isinstance(errors, list) else "image generation failed"
    raise RuntimeError(f"OVHcloud SDXL request failed: {detail}")


def generate_ai_horde_image(prompt, size, seed, negative_prompt=""):
    """Submit, poll and retrieve one volunteer-worker AI Horde image."""
    width, height = size
    scale = min(1.0, 1024.0 / max(width, height))
    width = max(512, int((width * scale) // 64) * 64)
    height = max(512, int((height * scale) // 64) * 64)
    width = min(width, 1024)
    height = min(height, 1024)
    api_key = os.getenv("AIHORDE_API_KEY", "0000000000").strip()
    model = os.getenv("AIHORDE_IMAGE_MODEL", "stable_diffusion_xl").strip()
    client_agent = os.getenv(
        "AIHORDE_CLIENT_AGENT", "katha-lok-photo-motion:1.0:github"
    ).strip()
    headers = {"apikey": api_key, "Client-Agent": client_agent}
    full_prompt = prompt[:10000]
    if negative_prompt:
        full_prompt += "###" + negative_prompt[:3000]
    submitted = SESSION.post(
        HORDE_BASE_URL + "/generate/async",
        headers=headers,
        json={
            "prompt": full_prompt,
            "params": {
                "width": width,
                "height": height,
                "steps": 20,
                "cfg_scale": 7,
                "seed": str(int(seed)),
                "n": 1,
                "sampler_name": "k_euler",
            },
            "models": [model] if model else [],
            "nsfw": False,
            "censor_nsfw": True,
            "trusted_workers": True,
            "slow_workers": False,
            "dry_run": False,
        },
        timeout=(20, 60),
    )
    request_data = _json(submitted, "AI Horde submission")
    request_id = request_data.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("AI Horde did not return a generation ID")

    deadline = time.monotonic() + 540
    while time.monotonic() < deadline:
        status_response = SESSION.get(
            HORDE_BASE_URL + "/generate/check/" + request_id,
            headers=headers,
            timeout=(15, 45),
        )
        status = _json(status_response, "AI Horde status")
        if status.get("faulted") is True:
            raise RuntimeError("AI Horde generation was faulted")
        if int(status.get("finished", 0) or 0) >= 1:
            result_response = SESSION.get(
                HORDE_BASE_URL + "/generate/status/" + request_id,
                headers=headers,
                timeout=(15, 60),
            )
            result = _json(result_response, "AI Horde result")
            generations = result.get("generations")
            if not isinstance(generations, list) or not generations:
                raise RuntimeError("AI Horde completed without an image")
            image_url = generations[0].get("img") if isinstance(generations[0], dict) else None
            if not isinstance(image_url, str) or not image_url.startswith("https://"):
                raise RuntimeError("AI Horde returned an invalid image URL")
            parsed = urlparse(image_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise RuntimeError("AI Horde returned an unsafe image URL")
            image = SESSION.get(image_url, timeout=(20, 120))
            image.raise_for_status()
            if not image.content or len(image.content) > MAX_IMAGE_BYTES:
                raise RuntimeError("AI Horde image is empty or oversized")
            return image.content
        if status.get("done") is True and int(status.get("finished", 0) or 0) < 1:
            raise RuntimeError("AI Horde finished without a generated image")
        time.sleep(3)
    raise TimeoutError("AI Horde image generation exceeded the 9-minute wait limit")
