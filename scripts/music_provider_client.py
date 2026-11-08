#!/usr/bin/env python3
"""Optional paid music provider adapters; never called without explicit permission."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

VERSION = "music-provider-client-v1"
ELEVEN_URL = "https://api.elevenlabs.io/v1/music"
STABILITY_URL = "https://api.stability.ai/v2beta/audio/stable-audio/text-to-audio"
TIMEOUT = (30, 900)
MIN_AUDIO_BYTES = 10_000

class MusicProviderError(RuntimeError):
    pass

def _enabled() -> bool:
    global_permission = os.getenv("ALLOW_BILLABLE_PROVIDERS", "").strip().lower() in {"1", "true", "yes", "on"}
    music_permission = os.getenv("MUSIC_ALLOW_PAID", "").strip().lower() in {"1", "true", "yes", "on"}
    return global_permission and music_permission

def _save_response(response: requests.Response, destination: str | Path) -> Path:
    if not response.ok:
        body = (response.text or "")[:1200]
        raise MusicProviderError(f"HTTP {response.status_code}: {body}")
    content_type = response.headers.get("Content-Type", "").lower()
    data = response.content
    if not data or len(data) < MIN_AUDIO_BYTES:
        raise MusicProviderError("Provider returned empty or undersized audio.")
    if "json" in content_type:
        raise MusicProviderError("Provider returned JSON instead of audio bytes.")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path

def generate_music(provider: str, prompt: str, duration_seconds: float, destination: str | Path) -> Path:
    """Generate one instrumental music bed through a supported provider adapter."""
    if not _enabled():
        raise MusicProviderError("Paid music providers are disabled; set both ALLOW_BILLABLE_PROVIDERS and MUSIC_ALLOW_PAID.")
    provider = str(provider or "").strip().lower()
    prompt = str(prompt or "").strip()
    if not prompt:
        raise MusicProviderError("Music prompt is empty.")
    duration = max(3.0, min(600.0, float(duration_seconds)))
    if provider == "eleven_music":
        key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not key:
            raise MusicProviderError("ELEVENLABS_API_KEY is missing.")
        payload = {
            "prompt": prompt[:4100],
            "music_length_ms": int(duration * 1000),
            "model_id": os.getenv("ELEVEN_MUSIC_MODEL", "music_v2_5"),
            "force_instrumental": True,
        }
        response = requests.post(
            ELEVEN_URL,
            headers={"xi-api-key": key, "Accept": "audio/mpeg"},
            params={"output_format": "mp3_48000_192"},
            json=payload,
            timeout=TIMEOUT,
        )
        return _save_response(response, destination)
    if provider == "stable_audio":
        key = os.getenv("STABILITY_API_KEY", "").strip()
        if not key:
            raise MusicProviderError("STABILITY_API_KEY is missing.")
        response = requests.post(
            STABILITY_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"none": ""},
            data={
                "model": os.getenv("STABLE_AUDIO_MODEL", "stable-audio-3"),
                "prompt": prompt[:2000],
                "duration": int(round(duration)),
            },
            timeout=TIMEOUT,
        )
        return _save_response(response, destination)
    raise MusicProviderError(f"Unsupported music provider: {provider}")
