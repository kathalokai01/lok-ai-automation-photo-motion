
#!/usr/bin/env python3
"""Resumable role-aware Hindi TTS with variable scenes and verified audio."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from gemini_http import http_status, http_error_body, retry_after_seconds
from voice_profiles import resolve_voice_profile
from input_config import (
    load_and_validate,
    get_selected_parts,
    get_scenes_range,
    get_format,
    get_tts_provider_order,
)

NARRATION_FILE = ROOT / "output/narration/narration.json"
OUTPUT_DIR = ROOT / "output/narration/audio"
MANIFEST_FILE = ROOT / "output/narration/audio_jobs.json"
CHECKPOINT_FILE = ROOT / "output/checkpoints/tts_progress.json"

VERSION = 15
MIN_AUDIO_BYTES = 1000
REQUEST_TIMEOUT = 180
MAX_TEXT_CHARS = 100000
MAX_AUDIO_SECONDS = 3600.0

PRIMARY_ORDER = ["fish_audio", "gemini_tts", "edge_tts"]
FALLBACK_ORDER = [
    "piper", "elevenlabs", "cartesia", "finevoice", "minimax"
]
ALLOWED_PROVIDERS = PRIMARY_ORDER + FALLBACK_ORDER

BILLABLE_PROVIDERS = {
    "fish_audio", "gemini_tts", "elevenlabs",
    "cartesia", "finevoice", "minimax",
}

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(*values: Any) -> None:
    print(*values, flush=True)


def text_value(value: Any, default: str = "") -> str:
    return default if value is None else str(value).strip()


def cfg_bool(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()

    if value in {"1", "true", "yes", "on", "enabled"}:
        return True

    if value in {"0", "false", "no", "off", "disabled"}:
        return False

    raise RuntimeError(f"{key} must be a valid boolean.")


def cfg_int(
    config: dict,
    key: str,
    default: int,
    minimum: int = 1,
) -> int:
    value = config.get(key, default)

    if isinstance(value, bool):
        raise RuntimeError(f"{key} must be an integer.")

    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be an integer.") from exc

    if result < minimum:
        raise RuntimeError(f"{key} must be at least {minimum}.")

    return result


def stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def load_json(path: Path, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON {path}.") from exc


def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def safe_relative_path(value: Any, label: str) -> Path:
    raw = text_value(value)

    if not raw:
        raise RuntimeError(f"{label}: path is missing.")

    path = Path(raw)

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"{label}: path escapes repository root."
        ) from exc

    return path


def run_command(
    command: list[str | Path],
    timeout: int = REQUEST_TIMEOUT,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(item) for item in command],
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=input_data is None,
        check=False,
        timeout=timeout,
    )

    if result.returncode:
        detail = result.stderr or result.stdout or ""

        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")

        raise RuntimeError(
            f"Local command failed with exit code "
            f"{result.returncode}: {str(detail)[-800:]}"
        )

    return result


class TTSProviderHTTPError(RuntimeError):
    """HTTP failure retaining status, response body and retry headers."""

    def __init__(self, status, reason, body="", headers=None):
        self.code = int(status)
        self.headers = headers
        self.response = SimpleNamespace(
            status_code=self.code,
            headers=headers,
            text=str(body or ""),
        )
        super().__init__(
            f"Provider HTTP {self.code}: {reason}: {str(body or '')[:1000]}"
        )


def request_bytes(
    url: str,
    headers: dict | None = None,
    payload: dict | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> tuple[bytes, str]:
    body = None
    request_headers = dict(headers or {})

    if payload is not None:
        body = json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8")

        request_headers.setdefault(
            "Content-Type", "application/json"
        )

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request, timeout=timeout
        ) as response:
            data = response.read()
            content_type = response.headers.get(
                "Content-Type", ""
            )

            if not data:
                raise RuntimeError(
                    "Provider returned an empty response."
                )

            return data, content_type

    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        error_body = http_error_body(exc, 1500)
        reason = (
            "authentication_or_permission" if status in {401, 403}
            else "payment_required" if status == 402
            else "rate_limit_or_quota" if status == 429
            else "model_unavailable" if status == 404
            else "provider_server_error" if status >= 500
            else "provider_request_rejected"
        )
        raise TTSProviderHTTPError(
            status, reason, error_body, exc.headers
        ) from None

    except urllib.error.URLError:
        raise RuntimeError(
            "Provider network connection failed."
        ) from None


def request_json(
    url: str,
    headers: dict,
    payload: dict,
) -> dict:
    data, _ = request_bytes(url, headers, payload)

    try:
        result = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "Provider returned invalid JSON."
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError(
            "Provider response is not a JSON object."
        )

    return result


def audio_duration(path: Path) -> float:
    if (
        not path.is_file()
        or path.stat().st_size < MIN_AUDIO_BYTES
    ):
        raise RuntimeError("Audio is missing or too small.")

    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        raise RuntimeError(
            "ffprobe is required for audio verification."
        )

    result = run_command(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        timeout=60,
    )

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "FFprobe returned an invalid duration."
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("Audio duration is invalid.")

    if duration > MAX_AUDIO_SECONDS:
        raise RuntimeError(
            "Audio exceeds the maximum duration."
        )

    return round(duration, 3)


def validate_audio(path: Path) -> dict:
    return {
        "duration": audio_duration(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def write_audio_bytes(path: Path, data: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        path.stem + ".partial" + path.suffix
    )

    try:
        temporary.write_bytes(data)
        info = validate_audio(temporary)
        os.replace(temporary, path)
        return info

    finally:
        temporary.unlink(missing_ok=True)


def pcm_to_mp3(
    pcm: bytes,
    output_path: Path,
    sample_rate: int = 24000,
) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path = output_path.with_suffix(".pcm.wav")

    try:
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)

        run_command(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y", "-v", "error",
                "-i", wav_path,
                "-codec:a", "libmp3lame",
                "-q:a", "3",
                output_path,
            ]
        )

    finally:
        wav_path.unlink(missing_ok=True)

    return validate_audio(output_path)


def normalize_speed(value: Any) -> str:
    value = str(value or "+0%").strip()

    if value.lower() in {"normal", "default", ""}:
        return "+0%"

    if not value.endswith("%"):
        value += "%"

    try:
        amount = int(value[:-1])
    except ValueError as exc:
        raise RuntimeError("Invalid TTS SPEED.") from exc

    if not -50 <= amount <= 50:
        raise RuntimeError(
            "TTS SPEED must be between -50% and +50%."
        )

    return f"{amount:+d}%"


def resolve_voice(provider: str, voice: str) -> str:
    voice = str(voice or "male").strip()

    if provider == "edge_tts":
        if voice.lower() in {"female", "woman", "f"}:
            return os.getenv(
                "EDGE_TTS_VOICE", "hi-IN-SwaraNeural"
            )

        if voice.lower() in {"male", "man", "m"}:
            return os.getenv(
                "EDGE_TTS_VOICE", "hi-IN-MadhurNeural"
            )

        return voice

    if provider == "gemini_tts":
        configured = os.getenv(
            "GEMINI_TTS_VOICE", ""
        ).strip()

        if configured:
            return configured

        return "Kore" if voice.lower() == "female" else "Puck"

    return voice


def provider_order(config: dict) -> list[str]:
    configured = get_tts_provider_order(config)
    normalized = []

    for item in configured:
        name = str(item).strip().lower()

        if name == "elevenlab":
            name = "elevenlabs"

        if (
            name in ALLOWED_PROVIDERS
            and name not in normalized
        ):
            normalized.append(name)

    if not normalized:
        raise RuntimeError(
            "Configured TTS provider order is empty."
        )

    return normalized


def billable_allowed(config: dict) -> bool:
    config_permission = cfg_bool(
        config, "ALLOW_BILLABLE_PROVIDERS", False
    )
    tts_permission = cfg_bool(
        config, "TTS_ALLOW_PAID", False
    )

    env_value = os.getenv("ALLOW_BILLABLE_PROVIDERS")

    if env_value is not None:
        normalized = env_value.strip().lower()

        if normalized not in {
            "true", "1", "yes", "on",
            "false", "0", "no", "off",
        }:
            raise RuntimeError(
                "Invalid ALLOW_BILLABLE_PROVIDERS value."
            )

        config_permission = (
            config_permission
            and normalized in {"true", "1", "yes", "on"}
        )

    return config_permission and tts_permission


def provider_available(
    provider: str,
    config: dict,
    allow_paid: bool,
) -> tuple[bool, str]:
    if provider in BILLABLE_PROVIDERS and not allow_paid:
        return False, "paid_provider_permission_disabled"

    if provider == "fish_audio":
        ok = bool(
            os.getenv("FISH_AUDIO_API_KEY")
            or os.getenv("FISH_API_KEY")
        )

    elif provider == "gemini_tts":
        ok = bool(
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )

    elif provider == "edge_tts":
        ok = bool(shutil.which("edge-tts"))

    elif provider == "piper":
        model = os.getenv("PIPER_MODEL") or str(
            config.get("PIPER_MODEL", "")
        )
        executable = (
            shutil.which("piper")
            or shutil.which("piper-tts")
        )
        model_path = (
            Path(model).expanduser() if model else None
        )
        ok = bool(
            executable
            and model_path
            and model_path.is_file()
        )

    elif provider == "elevenlabs":
        ok = bool(
            os.getenv("ELEVENLABS_API_KEY")
            and os.getenv("ELEVENLABS_VOICE_ID")
        )

    elif provider == "cartesia":
        ok = bool(
            os.getenv("CARTESIA_API_KEY")
            and os.getenv("CARTESIA_VOICE_ID")
        )

    elif provider == "finevoice":
        ok = bool(
            os.getenv("FINEVOICE_API_KEY")
            and os.getenv("FINEVOICE_TTS_URL")
            and os.getenv("FINEVOICE_VOICE_ID")
        )

    elif provider == "minimax":
        ok = bool(
            os.getenv("MINIMAX_API_KEY")
            and os.getenv("MINIMAX_TTS_VOICE_ID")
        )

    else:
        return False, "unsupported_provider"

    return (
        (True, "available")
        if ok
        else (False, "credentials_or_runtime_missing")
    )


def classify_provider_error(error: Exception) -> str:
    status = http_status(error)
    message = (str(error) + " " + http_error_body(error, 1500)).lower()

    if status in {401, 403} or "invalid api key" in message:
        return "authentication_or_permission"

    if status == 402 or any(term in message for term in (
        "payment required", "billing", "insufficient credit",
        "insufficient funds", "credit balance", "account suspended",
    )):
        return "payment_required"

    if status == 404:
        return "model_unavailable"

    if status == 429:
        rate_limited = any(term in message for term in (
            "rate limit", "too many requests", "requests per minute",
            "requests per second", "per minute", "per second",
        ))
        quota_exhausted = any(term in message for term in (
            "quota exceeded", "daily quota", "monthly quota",
            "resource_exhausted", "resource exhausted",
        ))
        return "rate_limit" if rate_limited or not quota_exhausted else "quota_or_billing"

    if "quota" in message or "billing" in message:
        return "quota_or_billing"

    return "transient_or_unknown"


def split_text(
    value: str,
    max_chars: int,
    min_chars: int,
) -> list[str]:
    source = str(value).strip()

    if not source:
        raise RuntimeError(
            "Cannot synthesize empty narration."
        )

    if len(source) > MAX_TEXT_CHARS:
        raise RuntimeError(
            "Narration exceeds maximum supported length."
        )

    max_chars = max(100, int(max_chars))
    min_chars = max(1, min(int(min_chars), max_chars))

    if len(source) <= max_chars:
        return [source]

    sentence_pattern = re.compile(
        r"""[.!?।॥]+["'”’)\]}»]*(?=\s|$)"""
    )

    sentences = []
    start = 0

    for match in sentence_pattern.finditer(source):
        end = match.end()
        part = source[start:end].strip()

        if part:
            sentences.append(part)

        start = end

    tail = source[start:].strip()

    if tail:
        sentences.append(tail)

    if not sentences:
        sentences = [source]

    units = []

    for sentence in sentences:
        if len(sentence) <= max_chars:
            units.append(sentence)
            continue

        start = 0

        for match in re.finditer(
            r"[,;:，；：](?:\s+|$)", sentence
        ):
            end = match.end()
            part = sentence[start:end].strip()

            if part:
                units.append(part)

            start = end

        tail = sentence[start:].strip()

        if tail:
            units.append(tail)

    chunks = []
    current = ""

    for unit in units:
        if len(unit) > max_chars:
            words = re.findall(r"\S+", unit)

            for word in words:
                if len(word) > max_chars:
                    raise RuntimeError(
                        "One narration token exceeds chunk limit."
                    )

                candidate = f"{current} {word}".strip()

                if current and len(candidate) > max_chars:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate

            continue

        candidate = f"{current} {unit}".strip()

        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = candidate

    if current:
        chunks.append(current)

    merged = []

    for chunk in chunks:
        if (
            merged
            and len(chunk) < min_chars
            and len(merged[-1]) + len(chunk) + 1 <= max_chars
        ):
            merged[-1] += " " + chunk
        else:
            merged.append(chunk)

    normalize = lambda item: re.sub(
        r"\s+", " ", item
    ).strip()

    if normalize(" ".join(merged)) != normalize(source):
        raise RuntimeError(
            "TTS chunking changed narration text."
        )

    return merged


def synthesize_provider(
    provider: str,
    text: str,
    output_path: Path,
    config: dict,
    speed: str,
    voice: str,
    voice_profile: dict | None = None,
) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "edge_tts":
        temporary = output_path.with_name(
            output_path.stem + ".edge.partial.mp3"
        )

        try:
            run_command(
                [
                    shutil.which("edge-tts") or "edge-tts",
                    "--voice", resolve_voice(provider, voice),
                    "--rate", speed,
                    "--text", text,
                    "--write-media", temporary,
                ]
            )

            info = validate_audio(temporary)
            os.replace(temporary, output_path)
            return info

        finally:
            temporary.unlink(missing_ok=True)

    if provider == "piper":
        model = os.getenv("PIPER_MODEL") or str(
            config.get("PIPER_MODEL", "")
        )
        executable = (
            shutil.which("piper")
            or shutil.which("piper-tts")
        )

        if (
            not executable
            or not model
            or not Path(model).expanduser().is_file()
        ):
            raise RuntimeError(
                "Piper executable or model unavailable."
            )

        wav_path = output_path.with_suffix(".piper.wav")

        try:
            subprocess.run(
                [
                    executable,
                    "--model", str(Path(model).expanduser()),
                    "--output_file", str(wav_path),
                ],
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=REQUEST_TIMEOUT,
            )

            run_command(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-y", "-v", "error",
                    "-i", wav_path,
                    "-codec:a", "libmp3lame",
                    "-q:a", "3",
                    output_path,
                ]
            )

        finally:
            wav_path.unlink(missing_ok=True)

        return validate_audio(output_path)


    if provider == "fish_audio":
        key = (
            os.getenv("FISH_AUDIO_API_KEY")
            or os.getenv("FISH_API_KEY")
        )

        payload = {
            "text": text,
            "format": "mp3",
            "mp3_bitrate": 128,
            "latency": "normal",
            "normalize": True,
        }

        reference = os.getenv(
            "FISH_REFERENCE_ID", ""
        ).strip()

        if reference:
            payload["reference_id"] = reference

        model = os.getenv("FISH_TTS_MODEL", "s1")

        if model:
            payload["model"] = model

        data, _ = request_bytes(
            "https://api.fish.audio/v1/tts",
            {"Authorization": f"Bearer {key}"},
            payload,
        )

        return write_audio_bytes(output_path, data)

    if provider == "gemini_tts":
        key = (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )

        model = os.getenv(
            "GEMINI_TTS_MODEL",
            "gemini-2.5-flash-preview-tts",
        )

        payload = {
            "contents": [
                {"parts": [{"text": text}]}
            ],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": resolve_voice(
                                provider, voice
                            )
                        }
                    }
                }
            }
        }

        result = request_json(
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/"
            f"{model}:generateContent",
            {"x-goog-api-key": key},
            payload,
        )

        try:
            parts = result["candidates"][0]["content"]["parts"]
            item = next(
                part for part in parts
                if isinstance(part, dict)
                and (
                    isinstance(part.get("inlineData"), dict)
                    or isinstance(part.get("inline_data"), dict)
                )
            )
            inline = item.get("inlineData") or item.get("inline_data")
            audio_data = base64.b64decode(
                inline["data"],
                validate=True,
            )
            mime_type = str(
                inline.get("mimeType") or inline.get("mime_type") or ""
            ).lower()

        except Exception as exc:
            raise RuntimeError(
                "Gemini TTS returned no valid audio."
            ) from exc

        if "mpeg" in mime_type or "mp3" in mime_type:
            return write_audio_bytes(output_path, audio_data)

        if "wav" in mime_type:
            wav_path = output_path.with_name(
                output_path.stem + ".gemini.wav"
            )
            try:
                wav_path.write_bytes(audio_data)
                validate_audio(wav_path)
                run_command([
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-y", "-v", "error",
                    "-i", wav_path,
                    "-codec:a", "libmp3lame",
                    "-q:a", "3",
                    output_path,
                ])
                return validate_audio(output_path)
            finally:
                wav_path.unlink(missing_ok=True)

        return pcm_to_mp3(audio_data, output_path, 24000)

    if provider == "elevenlabs":
        key = os.getenv(
            "ELEVENLABS_API_KEY", ""
        ).strip()
        profile_id = str((voice_profile or {}).get("profile_id") or "").upper()
        mapped_voice_id = os.getenv(f"ELEVENLABS_VOICE_ID_{profile_id}", "").strip() if profile_id else ""
        voice_id = mapped_voice_id or os.getenv(
            "ELEVENLABS_VOICE_ID", ""
        ).strip()

        if not key or not voice_id:
            raise RuntimeError(
                "ElevenLabs API key and Voice ID are required."
            )

        payload = {
            "text": text,
            "model_id": os.getenv(
                "ELEVENLABS_MODEL_ID",
                "eleven_multilingual_v2",
            ),
            "voice_settings": {
                "stability": 0.48,
                "similarity_boost": 0.78,
                "style": max(0.0, min(1.0, float((voice_profile or {}).get("emotion_strength", 0.35)))),
                "use_speaker_boost": True,
            },
        }

        data, _ = request_bytes(
            f"https://api.elevenlabs.io/v1/text-to-speech/"
            f"{voice_id}?output_format=mp3_44100_128",
            {
                "xi-api-key": key,
                "Accept": "audio/mpeg",
            },
            payload,
        )

        return write_audio_bytes(output_path, data)

    if provider == "cartesia":
        payload = {
            "model_id": os.getenv(
                "CARTESIA_TTS_MODEL", "sonic-2"
            ),
            "transcript": text,
            "voice": {
                "mode": "id",
                "id": os.getenv("CARTESIA_VOICE_ID"),
            },
            "output_format": {
                "container": "mp3",
                "sample_rate": 44100,
                "bit_rate": 128000,
            },
            "language": "hi",
        }

        data, _ = request_bytes(
            "https://api.cartesia.ai/tts/bytes",
            {
                "X-API-Key": os.getenv("CARTESIA_API_KEY"),
                "Cartesia-Version": os.getenv(
                    "CARTESIA_API_VERSION",
                    "2025-04-16",
                ),
                "Accept": "audio/mpeg",
            },
            payload,
        )

        return write_audio_bytes(output_path, data)


    if provider == "minimax":
        payload = {
            "model": os.getenv(
                "MINIMAX_TTS_MODEL", "speech-02-hd"
            ),
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": os.getenv(
                    "MINIMAX_TTS_VOICE_ID"
                ),
                "speed": (
                    1.0
                    + int(speed.replace("%", "")) / 100.0
                ),
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }

        result = request_json(
            os.getenv(
                "MINIMAX_TTS_URL",
                "https://api.minimax.io/v1/t2a_v2",
            ),
            {
                "Authorization": (
                    f"Bearer {os.getenv('MINIMAX_API_KEY')}"
                )
            },
            payload,
        )

        try:
            audio = bytes.fromhex(
                result["data"]["audio"]
            )
        except Exception as exc:
            raise RuntimeError(
                "MiniMax returned no usable audio."
            ) from exc

        return write_audio_bytes(output_path, audio)

    if provider == "finevoice":
        payload = {
            "text": text,
            "voice_id": os.getenv(
                "FINEVOICE_VOICE_ID", ""
            ),
            "format": "mp3",
        }

        data, content_type = request_bytes(
            os.getenv("FINEVOICE_TTS_URL", ""),
            {
                "Authorization": (
                    f"Bearer {os.getenv('FINEVOICE_API_KEY')}"
                ),
                "Accept": "audio/mpeg,application/json",
            },
            payload,
        )

        if "json" in content_type.lower():
            try:
                result = json.loads(data.decode("utf-8"))
                encoded = (
                    result.get("audio")
                    or result.get("audio_base64")
                    or result.get("data")
                )

                if isinstance(encoded, dict):
                    encoded = (
                        encoded.get("audio")
                        or encoded.get("audio_base64")
                    )

                data = base64.b64decode(
                    encoded, validate=True
                )

            except Exception as exc:
                raise RuntimeError(
                    "FineVoice returned invalid audio JSON."
                ) from exc

        return write_audio_bytes(output_path, data)

    raise RuntimeError(
        f"Unsupported configured TTS provider: {provider}"
    )


def extract_jobs(data: Any) -> list[dict]:
    if isinstance(data, list):
        raw_jobs = list(data)

    elif isinstance(data, dict):
        raw_jobs = None

        for field in ("jobs", "narrations"):
            candidate = data.get(field)

            if isinstance(candidate, list):
                raw_jobs = list(candidate)
                break

        if raw_jobs is None:
            main_rows = data.get("scenes")
            extra_rows = data.get("extra_scenes")

            if isinstance(main_rows, list):
                raw_jobs = list(main_rows)

                if isinstance(extra_rows, list):
                    raw_jobs.extend(extra_rows)

            else:
                candidate = data.get("parts")

                if isinstance(candidate, list):
                    raw_jobs = list(candidate)

        elif isinstance(data.get("extra_scenes"), list):
            existing_ids = {
                str(item.get("scene_id") or item.get("id") or "")
                for item in raw_jobs
                if isinstance(item, dict)
            }

            for item in data["extra_scenes"]:
                if not isinstance(item, dict):
                    continue

                item_id = str(
                    item.get("scene_id") or item.get("id") or ""
                )

                if not item_id or item_id not in existing_ids:
                    raw_jobs.append(item)
                    if item_id:
                        existing_ids.add(item_id)

        if not isinstance(raw_jobs, list):
            raise RuntimeError(
                "Narration JSON must contain scenes and extra_scenes, "
                "or a jobs/narrations list."
            )

    else:
        raise RuntimeError(
            "Narration JSON has an unsupported structure."
        )

    jobs = []

    def add_job(
        item: dict,
        parent: dict | None = None,
    ) -> None:
        parent = parent or {}
        merged = {**parent, **item}
        role_probe = text_value(
            merged.get("role")
            or merged.get("scene_role")
            or merged.get("scene_type")
            or merged.get("type")
            or "main"
        ).lower()
        role_aliases = {
            "opening": "hook", "intro": "hook", "hook_scene": "hook",
            "story": "main", "narration": "main", "body": "main",
            "cliffhanger": "suspense", "ending": "resolution",
            "final": "resolution", "final_resolution": "resolution",
        }
        role_probe = role_aliases.get(role_probe, role_probe)
        narration_probe = (
            merged.get("text")
            or merged.get("narration")
            or merged.get("script")
            or merged.get("content")
            or merged.get("voice_text")
            or ""
        )
        if role_probe in {"hook", "suspense", "resolution"}:
            if not isinstance(narration_probe, str) or len(narration_probe.strip().split()) < 8:
                raise RuntimeError(
                    f"{role_probe} must contain spoken narration of at least 8 words."
                )
            if merged.get("voiceover_enabled") is not True:
                raise RuntimeError(
                    f"{role_probe} voiceover must be enabled."
                )
            if merged.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(
                    f"{role_probe} narration source metadata is invalid."
                )

        narration = (
            merged.get("text")
            or merged.get("narration")
            or merged.get("script")
            or merged.get("content")
            or merged.get("voice_text")
        )

        if (
            not isinstance(narration, str)
            or not narration.strip()
        ):
            raise RuntimeError(
                "A narration record has empty or invalid text."
            )

        part_value = merged.get(
            "part", merged.get("part_number", 1)
        )

        try:
            part = int(part_value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Narration job has an invalid part number."
            ) from exc

        if part < 1:
            raise RuntimeError(
                "Narration part number must be positive."
            )


        role = text_value(
            merged.get("role")
            or merged.get("scene_role")
            or merged.get("scene_type")
            or merged.get("type")
            or "main"
        ).lower()

        aliases = {
            "opening": "hook",
            "intro": "hook",
            "hook_scene": "hook",
            "story": "main",
            "narration": "main",
            "body": "main",
            "cliffhanger": "suspense",
            "ending": "resolution",
            "final": "resolution",
            "final_resolution": "resolution",
        }

        role = aliases.get(role, role)

        if role not in ROLE_ORDER:
            raise RuntimeError(
                f"Unsupported narration role: {role!r}."
            )

        scene_value = merged.get("scene")

        if role == "main":
            if scene_value in (None, ""):
                scene_value = (
                    merged.get("global_scene")
                    or merged.get("scene_index")
                    or merged.get("scene_number")
                )

            try:
                scene = int(scene_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Main narration job has no valid scene number."
                ) from exc

            if scene < 1:
                raise RuntimeError(
                    "Main narration scene number must be positive."
                )

        else:
            if scene_value in (None, ""):
                scene = None
            else:
                try:
                    scene = int(scene_value)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"{role} narration has an invalid scene value."
                    ) from exc

                if scene < 0:
                    raise RuntimeError(
                        f"{role} narration scene value cannot be negative."
                    )

        scene_id = text_value(
            merged.get("scene_id")
            or merged.get("id")
            or merged.get("source_scene_id")
        )

        if not scene_id:
            if role == "main":
                scene_id = f"part_{part:02d}_scene_{scene:02d}"
            else:
                scene_id = f"part_{part:02d}_{role}"

        voice = text_value(
            merged.get("voice")
            or merged.get("voice_type")
            or merged.get("gender")
            or merged.get("speaker"),
            "male",
        )

        normalized_text = re.sub(r"\s+", " ", narration).strip()
        source_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        declared_hash = merged.get("text_sha256") or merged.get("narration_sha256")
        if declared_hash and declared_hash != source_hash:
            raise RuntimeError(
                f"{scene_id}: source narration text hash mismatch."
            )

        jobs.append({
            "part": part,
            "scene": scene,
            "scene_id": scene_id,
            "role": role,
            "text": normalized_text,
            "text_sha256": source_hash,
            "voice": voice,
            "voice_profile": merged.get("voice_profile") if isinstance(merged.get("voice_profile"), dict) else {},
            "emotion": str(merged.get("emotion") or "neutral"),
            "speaker_id": str(merged.get("speaker_id") or "narrator"),
            "source": merged,
        })

    for item in raw_jobs:
        if not isinstance(item, dict):
            raise RuntimeError(
                "Narration jobs must contain JSON objects."
            )

        nested = (
            item.get("scenes")
            or item.get("narration_jobs")
            or item.get("jobs")
        )

        if isinstance(nested, list):
            parent = {
                key: value
                for key, value in item.items()
                if key not in {
                    "scenes", "narration_jobs", "jobs"
                }
            }

            for child in nested:
                if not isinstance(child, dict):
                    raise RuntimeError(
                        "Nested narration record is invalid."
                    )
                add_job(child, parent)

        else:
            add_job(item)

    if not jobs:
        raise RuntimeError(
            "No usable narration jobs were found."
        )

    seen = set()

    for job in jobs:
        key = (job["part"], job["scene_id"])

        if key in seen:
            raise RuntimeError(
                f"Duplicate narration scene ID: {job['scene_id']}"
            )

        seen.add(key)

    jobs.sort(
        key=lambda job: (
            job["part"],
            ROLE_ORDER[job["role"]],
            job["scene"] or 0,
            job["scene_id"],
        )
    )

    return jobs


def verify_part_count(
    jobs: list[dict],
    config: dict,
) -> int:
    expected = get_selected_parts(config)
    actual = len({job["part"] for job in jobs})

    if expected is not None and actual != int(expected):
        raise RuntimeError(
            f"Narration PARTS mismatch: "
            f"expected {expected}, found {actual}."
        )

    part_numbers = sorted({
        job["part"] for job in jobs
    })

    if part_numbers != list(range(1, actual + 1)):
        raise RuntimeError(
            f"Narration part numbering is not continuous: "
            f"{part_numbers}"
        )

    return actual


def validate_scene_counts(
    narration_data: dict,
    parts_count: int,
    scenes_min: int,
    scenes_max: int,
) -> list[int]:
    raw_counts = narration_data.get("actual_scene_counts")

    if (
        not isinstance(raw_counts, list)
        or len(raw_counts) != parts_count
    ):
        raise RuntimeError(
            "Narration actual_scene_counts is missing or "
            "does not match the Part count."
        )

    actual_counts = []

    for part, value in enumerate(raw_counts, 1):
        if isinstance(value, bool):
            raise RuntimeError(
                f"Part {part} has an invalid actual Scene count."
            )

        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Part {part} has an invalid actual Scene count."
            ) from exc

        if not scenes_min <= count <= scenes_max:
            raise RuntimeError(
                f"Part {part} has {count} Main Scenes; "
                f"allowed range is {scenes_min}-{scenes_max}."
            )

        actual_counts.append(count)

    try:
        source_min = int(narration_data["scenes_min"])
        source_max = int(narration_data["scenes_max"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Narration SCENES range metadata is missing."
        ) from exc

    if (
        source_min != scenes_min
        or source_max != scenes_max
    ):
        raise RuntimeError(
            "Narration SCENES range does not match Input/topic.txt: "
            f"narration={source_min}-{source_max}, "
            f"input={scenes_min}-{scenes_max}."
        )

    return actual_counts


def validate_role_structure(
    jobs: list[dict],
    actual_parts: int,
    actual_counts: list[int],
) -> None:
    if len(actual_counts) != actual_parts:
        raise RuntimeError(
            "Actual Scene count list does not match Part count."
        )

    grouped: dict[int, list[dict]] = {}

    for job in jobs:
        grouped.setdefault(
            job["part"], []
        ).append(job)

    for part in range(1, actual_parts + 1):
        rows = grouped.get(part, [])

        if not rows:
            raise RuntimeError(
                f"Part {part} has no narration."
            )

        roles = [job["role"] for job in rows]
        expected_extra_roles = (
            ["hook", "suspense"] if part < actual_parts else ["hook", "resolution"]
        )
        actual_extra_roles = [role for role in roles if role != "main"]
        if actual_extra_roles != expected_extra_roles:
            raise RuntimeError(
                f"Part {part}: expected narrated extra roles {expected_extra_roles}, "
                f"found {actual_extra_roles}."
            )

        main_rows = [
            job for job in rows
            if job["role"] == "main"
        ]

        main_numbers = sorted(
            job["scene"] for job in main_rows
        )

        expected_numbers = list(
            range(1, actual_counts[part - 1] + 1)
        )

        if main_numbers != expected_numbers:
            raise RuntimeError(
                f"Part {part} Main Scene IDs are not continuous: "
                f"expected {expected_numbers}, found {main_numbers}."
            )

        expected_total = actual_counts[part - 1] + len(expected_extra_roles)

        if len(rows) != expected_total:
            raise RuntimeError(
                f"Part {part} narration record count mismatch: "
                f"expected {expected_total}, found {len(rows)}."
            )

    expected_ids = set()

    for job in jobs:
        expected_ids.add(job["scene_id"])

    if len(expected_ids) != len(jobs):
        raise RuntimeError(
            "Duplicate scene_id exists across narration roles."
        )


def job_output_path(
    part: int,
    scene_id: str,
    chunk_index: int,
) -> Path:
    safe_id = re.sub(
        r"[^\w.-]+", "-", scene_id, flags=re.UNICODE
    )
    safe_id = safe_id.strip(".-")[:100]

    if not safe_id:
        raise RuntimeError(
            "Scene ID cannot be converted to a safe filename."
        )

    return (
        OUTPUT_DIR
        / f"part-{part:02d}"
        / f"{safe_id}-chunk-{chunk_index:03d}.mp3"
    )


def valid_existing_audio(
    path: Path,
    expected_hash: str,
) -> dict | None:
    if not path.is_file():
        return None

    try:
        info = validate_audio(path)

        if info["sha256"] != expected_hash:
            return None

        return info

    except Exception:
        return None


def save_checkpoint(
    records: list[dict],
    input_hash: str,
    config_hash: str,
) -> None:
    CHECKPOINT_FILE.parent.mkdir(
        parents=True, exist_ok=True
    )

    completed = [
        {
            "job_id": record["job_id"],
            "output_path": record["output_path"],
            "sha256": record["sha256"],
            "duration": record["duration"],
            "provider": record["provider"],
        }
        for record in records
        if record.get("status") == "completed"
    ]

    save_json_atomic(
        CHECKPOINT_FILE,
        {
            "schema_version": VERSION,
            "updated_at": utc_now(),
            "input_sha256": input_hash,
            "config_sha256": config_hash,
            "completed_chunks": completed,
            "completed_count": len(completed),
        },
    )


def load_reusable_records(
    input_hash: str,
    config_hash: str,
) -> dict[str, dict]:
    data = load_json(
        CHECKPOINT_FILE, required=False
    )

    if not isinstance(data, dict):
        return {}

    if data.get("schema_version") != VERSION:
        return {}

    if data.get("input_sha256") != input_hash:
        return {}

    if data.get("config_sha256") != config_hash:
        return {}

    raw = data.get("completed_chunks")

    if not isinstance(raw, list):
        return {}

    reusable = {}

    for record in raw:
        if not isinstance(record, dict):
            continue

        job_id = text_value(record.get("job_id"))

        if not job_id:
            continue

        try:
            path = safe_relative_path(
                record.get("output_path"),
                "Checkpoint audio",
            )
        except Exception:
            continue

        if not path.is_file():
            continue

        try:
            info = validate_audio(path)
        except Exception:
            continue

        if info["sha256"] != record.get("sha256"):
            continue

        reusable[job_id] = {
            **record,
            "verified_audio": info,
        }

    return reusable


def synthesize_chunk_with_fallback(
    chunk: str,
    output_path: Path,
    providers: list[str],
    config: dict,
    speed: str,
    voice: str,
    allow_paid: bool,
    max_retries: int,
    blocked_paid_providers: set[str],
    voice_profile: dict | None = None,
) -> tuple[dict, str]:
    failures = []

    for provider in providers:
        if provider in blocked_paid_providers:
            failures.append({
                "provider": provider,
                "reason": (
                    "paid_provider_blocked_after_quota_or_payment_error"
                ),
            })
            continue

        available, reason = provider_available(
            provider, config, allow_paid
        )

        if not available:
            failures.append({
                "provider": provider,
                "reason": reason,
            })
            continue

        provider_output = output_path.with_name(
            output_path.stem + f".{provider}.mp3"
        )

        for attempt in range(1, max_retries + 1):
            try:
                info = synthesize_provider(
                    provider,
                    chunk,
                    provider_output,
                    config,
                    speed,
                    voice,
                    voice_profile,
                )

                verified = validate_audio(provider_output)

                if verified["sha256"] != info["sha256"]:
                    raise RuntimeError(
                        "Audio SHA256 changed after synthesis."
                    )

                os.replace(provider_output, output_path)
                final_info = validate_audio(output_path)

                if final_info["sha256"] != info["sha256"]:
                    raise RuntimeError(
                        "Final audio SHA256 mismatch."
                    )

                return final_info, provider

            except Exception as exc:
                classification = classify_provider_error(exc)

                failures.append({
                    "provider": provider,
                    "attempt": attempt,
                    "reason": classification,
                    "message": str(exc)[:300],
                })

                provider_output.unlink(missing_ok=True)

                if classification in {
                    "authentication_or_permission",
                    "payment_required",
                    "quota_or_billing",
                }:
                    if provider in BILLABLE_PROVIDERS:
                        blocked_paid_providers.add(provider)
                    break

                if classification == "model_unavailable":
                    break

                if classification == "rate_limit":
                    if attempt < max_retries:
                        delay = retry_after_seconds(
                            exc, attempt, default_base=12, maximum=120
                        )
                        log(
                            f"TTS provider {provider} rate-limited; "
                            f"retrying in {delay}s."
                        )
                        time.sleep(delay)
                        continue
                    break

                if attempt < max_retries:
                    time.sleep(min(2 ** (attempt - 1), 12))

    raise RuntimeError(
        "All configured TTS providers failed for a narration chunk. "
        + json.dumps(failures, ensure_ascii=False)
    )


def merge_scene_chunks(
    chunk_paths: list[Path],
    final_path: Path,
) -> dict:
    if not chunk_paths:
        raise RuntimeError(
            "No audio chunks are available to merge."
        )

    final_path.parent.mkdir(
        parents=True, exist_ok=True
    )

    temporary = final_path.with_name(
        final_path.stem + ".partial.mp3"
    )
    concat_file = final_path.with_name(
        final_path.stem + ".concat.txt"
    )

    try:
        with concat_file.open(
            "w", encoding="utf-8"
        ) as handle:
            for path in chunk_paths:
                if not path.is_file():
                    raise RuntimeError(
                        f"Audio chunk missing: {path.name}"
                    )

                escaped = str(
                    path.resolve()
                ).replace("'", "'\\''")

                handle.write(
                    f"file '{escaped}'\n"
                )

        run_command(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y", "-v", "error",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
                "-codec:a", "libmp3lame",
                "-q:a", "3",
                temporary,
            ]
        )

        validate_audio(temporary)
        os.replace(temporary, final_path)

        return validate_audio(final_path)

    finally:
        temporary.unlink(missing_ok=True)
        concat_file.unlink(missing_ok=True)


def combine_part_audio(
    scene_records: list[dict],
    part: int,
) -> dict:
    records = sorted(
        scene_records,
        key=lambda item: (
            ROLE_ORDER[item["role"]],
            item["scene"] or 0,
            item["scene_id"],
        ),
    )

    paths = [
        safe_relative_path(
            record["audio_path"], "Scene audio"
        )
        for record in records
    ]

    output = (
        OUTPUT_DIR
        / f"part-{part:02d}"
        / f"part-{part:02d}-narration.mp3"
    )

    info = merge_scene_chunks(paths, output)

    return {
        "part": part,
        "audio_path": str(output.relative_to(ROOT)),
        "duration": info["duration"],
        "actual_audio_duration_seconds": info["duration"],
        "duration_source": "ffprobe_measured",
        "audio_duration_source": "ffprobe_measured",
        "size_bytes": info["size_bytes"],
        "sha256": info["sha256"],
        "scene_count": len(records),
        "roles": [record["role"] for record in records],
    }


def stable_source_signature(
    narration_data: dict,
    jobs: list[dict],
) -> str:
    records = [
        {
            "part": job["part"],
            "scene": job["scene"],
            "scene_id": job["scene_id"],
            "role": job["role"],
            "text": job["text"],
            "text_sha256": job["text_sha256"],
            "voiceover_enabled": True,
            "narration_source": (
                "locked_part_text" if job["role"] == "main"
                else "ai_generated_role_narration"
            ),
            "source_text_sha256": job["text_sha256"],
            "source_word_count": len(job["text"].split()),
            "voice": job["voice"],
        }
        for job in jobs
    ]

    return stable_hash({
        "narration_source_signature": (
            narration_data.get("source_signature")
        ),
        "records": records,
    })


def reusable_legacy_scene(
    record: dict,
    job: dict,
) -> dict | None:
    if not isinstance(record, dict):
        return None

    if record.get("status") != "completed":
        return None

    if (
        record.get("scene_id") != job["scene_id"]
        or record.get("part") != job["part"]
        or record.get("role") != job["role"]
        or re.sub(r"\s+", " ", str(record.get("text") or record.get("narration") or "")).strip()
        != re.sub(r"\s+", " ", str(job.get("text") or "")).strip()
        or text_value(record.get("voice")) != text_value(job.get("voice"))
    ):
        return None

    try:
        path = safe_relative_path(
            record.get("audio_path"),
            "Previous TTS scene audio",
        )
        info = validate_audio(path)
    except Exception:
        return None

    if info["sha256"] != record.get("sha256"):
        return None

    declared_duration = record.get("duration")

    try:
        if abs(
            float(declared_duration) - info["duration"]
        ) > 0.20:
            return None
    except (TypeError, ValueError):
        return None

    return {
        **record,
        "audio_path": str(path.relative_to(ROOT)),
        "duration": info["duration"],
        "actual_audio_duration_seconds": info["duration"],
        "duration_source": "ffprobe_measured",
        "audio_duration_source": "ffprobe_measured",
        "size_bytes": info["size_bytes"],
        "sha256": info["sha256"],
        "status": "completed",
    }


def write_back_narration_audio(
    narration_data: dict,
    scene_records: list[dict],
    source_hash: str,
) -> None:
    if not isinstance(narration_data, dict):
        raise RuntimeError(
            "Narration JSON cannot be updated: invalid root object."
        )

    by_id = {
        row["scene_id"]: row
        for row in scene_records
    }

    updated_ids = set()

    for field in ("scenes", "extra_scenes"):
        rows = narration_data.get(field)

        if not isinstance(rows, list):
            raise RuntimeError(
                f"Narration JSON is missing the {field} list."
            )

        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Narration {field} contains an invalid record."
                )

            scene_id = text_value(row.get("scene_id"))
            audio = by_id.get(scene_id)

            if audio is None:
                raise RuntimeError(
                    f"TTS audio is missing for narration ID: {scene_id}"
                )

            audio_path = audio["audio_path"]
            checksum = audio["sha256"]
            duration = audio["actual_audio_duration_seconds"]

            row.update({
                "audio": audio_path,
                "audio_path": audio_path,
                "audio_file": audio_path,
                "audio_sha256": checksum,
                "audio_hash": checksum,
                "audio_size_bytes": audio["size_bytes"],
                "audio_duration": duration,
                "actual_audio_duration_seconds": duration,
                "audio_duration_source": "ffprobe_measured",
                "duration_source": "ffprobe_measured",
                "tts_provider": audio["provider"],
                "tts_providers": audio["providers"],
                "provider": audio["provider"],
                "tts_fingerprint": audio["tts_fingerprint"],
                "fingerprint": audio["tts_fingerprint"],
                "chunk_count": audio["chunk_count"],
                "chunks": audio["chunks"],
                "audio_completed_at": utc_now(),
                "status": "completed",
            })

            updated_ids.add(scene_id)

    if updated_ids != set(by_id):
        missing = sorted(set(by_id) - updated_ids)

        raise RuntimeError(
            "TTS manifest and narration records do not match: "
            + ", ".join(missing[:20])
        )

    narration_data["tts_source_sha256"] = source_hash
    narration_data["tts_audio_manifest"] = (
        MANIFEST_FILE.relative_to(ROOT).as_posix()
    )

    save_json_atomic(NARRATION_FILE, narration_data)


def main() -> int:
    OUTPUT_DIR.mkdir(
        parents=True, exist_ok=True
    )
    MANIFEST_FILE.parent.mkdir(
        parents=True, exist_ok=True
    )
    CHECKPOINT_FILE.parent.mkdir(
        parents=True, exist_ok=True
    )

    config = load_and_validate(
        ROOT / "Input" / "topic.txt"
    )

    mode = get_format(config)
    scenes_min, scenes_max = get_scenes_range(config)
    selected_parts = get_selected_parts(config)

    if mode not in {"short", "full"}:
        raise RuntimeError(
            "Invalid video FORMAT in input configuration."
        )

    narration_data = load_json(NARRATION_FILE)

    if not isinstance(narration_data, dict):
        raise RuntimeError(
            "Narration JSON root must be an object."
        )

    jobs = extract_jobs(narration_data)
    parts_count = verify_part_count(jobs, config)

    if selected_parts is not None and parts_count != int(selected_parts):
        raise RuntimeError(
            "Narration Part count does not match selected PARTS."
        )

    actual_counts = validate_scene_counts(
        narration_data,
        parts_count,
        scenes_min,
        scenes_max,
    )

    validate_role_structure(
        jobs,
        parts_count,
        actual_counts,
    )

    providers = provider_order(config)
    allow_paid = billable_allowed(config)

    max_retries = cfg_int(
        config,
        "TTS_MAX_PROVIDER_ATTEMPTS",
        config.get("TTS_MAX_RETRIES", 2),
    )

    max_chars = cfg_int(
        config,
        "TTS_MAX_CHARS_PER_CHUNK",
        config.get("TTS_CHUNK_MAX_CHARS", 1800),
        100,
    )

    min_chars = cfg_int(
        config,
        "TTS_MIN_CHARS_PER_CHUNK",
        config.get("TTS_CHUNK_MIN_CHARS", 250),
    )

    speed = normalize_speed(
        config.get(
            "TTS_SPEED",
            config.get("SPEED", "+0%"),
        )
    )

    voice_setting = config.get(
        "VOICE", config.get("TTS_VOICE", "male")
    )

    source_hash = stable_source_signature(
        narration_data, jobs
    )

    config_hash = stable_hash({
        "format": mode,
        "parts": parts_count,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": actual_counts,
        "providers": providers,
        "allow_paid": allow_paid,
        "speed": speed,
        "max_chars": max_chars,
        "min_chars": min_chars,
        "max_retries": max_retries,
        "voice": voice_setting,
        "voice_profiles": [
            job.get("voice_profile", {}) for job in jobs
        ],
        "emotion_tags": [job.get("emotion", "neutral") for job in jobs],
    })

    reusable = load_reusable_records(
        source_hash, config_hash
    )

    previous_manifest = load_json(
        MANIFEST_FILE, required=False
    )

    previous_jobs = {}


    if (
        isinstance(previous_manifest, dict)
        and previous_manifest.get("status") in {
            "completed", "failed", "running"
        }
        and previous_manifest.get("config_sha256") == config_hash
        and isinstance(previous_manifest.get("jobs"), list)
    ):
        previous_jobs = {
            row.get("scene_id"): row
            for row in previous_manifest["jobs"]
            if (
                isinstance(row, dict)
                and row.get("status") == "completed"
                and row.get("scene_id")
            )
        }

    blocked_paid_providers: set[str] = set()

    manifest = {
        "schema_version": VERSION,
        "status": "running",
        "retryable": False,
        "format": mode,
        "parts": parts_count,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": actual_counts,
        "total_main_scenes": sum(actual_counts),
        "total_extra_scenes": parts_count * 2,
        "visual_only_extra_scenes": 0,
        "configured_provider_order": providers,
        "billable_providers_allowed": allow_paid,
        "source_sha256": source_hash,
        "narration_source_signature": narration_data.get(
            "source_signature"
        ),
        "config_sha256": config_hash,
        "duration_source": "ffprobe_measured",
        "started_at": utc_now(),
        "completed_at": None,
        "jobs": [],
        "parts": [],
        "summary": {
            "total_jobs": len(jobs),
            "completed_jobs": 0,
            "reused_chunks": 0,
            "generated_chunks": 0,
            "reused_scene_audio": 0,
            "generated_scene_audio": 0,
            "failed_jobs": 0,
        },
    }

    # Save the current run identity before provider calls.
    # A failure can then be retried without losing completed jobs.
    save_json_atomic(MANIFEST_FILE, manifest)

    scene_records = []
    checkpoint_records = []

    for job_index, job in enumerate(jobs, start=1):
        log(
            f"TTS {job_index}/{len(jobs)}"
            f" | Part {job['part']}"
            f" | {job['role']}"
            f" | {job['scene_id']}"
        )

        scene_fingerprint = stable_hash({
            "version": VERSION,
            "source_sha256": source_hash,
            "config_sha256": config_hash,
            "part": job["part"],
            "scene": job["scene"],
            "scene_id": job["scene_id"],
            "role": job["role"],
            "text": job["text"],
            "voice": job["voice"],
            "speed": speed,
        })

        old_record = previous_jobs.get(job["scene_id"])

        reused_scene = reusable_legacy_scene(
            old_record, job
        )


        if reused_scene is not None:
            reused_scene.update({
                "part": job["part"],
                "scene": job["scene"],
                "scene_id": job["scene_id"],
                "role": job["role"],
                "text": job["text"],
                "voice": job["voice"],
                "tts_fingerprint": scene_fingerprint,
                "fingerprint": scene_fingerprint,
                "provider": (
                    reused_scene.get("provider")
                    or (
                        reused_scene.get("providers") or ["unknown"]
                    )[0]
                ),
                "providers": reused_scene.get(
                    "providers",
                    [reused_scene.get("provider", "unknown")],
                ),
                "chunks": reused_scene.get("chunks", []),
                "chunk_count": int(
                    reused_scene.get("chunk_count", 0)
                ),
            })

            scene_records.append(reused_scene)
            manifest["jobs"].append(reused_scene)
            manifest["summary"]["completed_jobs"] += 1
            manifest["summary"]["reused_scene_audio"] += 1

            save_json_atomic(MANIFEST_FILE, manifest)
            continue

        chunks = split_text(
            job["text"], max_chars, min_chars
        )

        scene_chunk_paths = []
        chunk_records = []
        providers_used = []

        for chunk_index, chunk in enumerate(
            chunks, start=1
        ):
            chunk_fingerprint = stable_hash({
                "scene_fingerprint": scene_fingerprint,
                "chunk_index": chunk_index,
                "chunk_text": chunk,
            })

            output_path = job_output_path(
                job["part"],
                job["scene_id"],
                chunk_index,
            )

            job_id = (
                f"{job['part']}:{job['scene_id']}:"
                f"{chunk_index}:{chunk_fingerprint[:16]}"
            )

            reusable_record = reusable.get(job_id)
            info = None
            provider_used = None

            chunk_reused = False

            if reusable_record:
                info = valid_existing_audio(
                    output_path,
                    reusable_record["sha256"],
                )

                if info:
                    provider_used = reusable_record.get(
                        "provider"
                    )
                    chunk_reused = True
                    manifest["summary"]["reused_chunks"] += 1
                    checkpoint_records.append({
                        "job_id": job_id,
                        "part": job["part"],
                        "scene": job["scene"],
                        "scene_id": job["scene_id"],
                        "role": job["role"],
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                        "text_sha256": hashlib.sha256(
                            chunk.encode("utf-8")
                        ).hexdigest(),
                        "output_path": str(
                            output_path.relative_to(ROOT)
                        ),
                        "provider": provider_used,
                        "duration": info["duration"],
                        "duration_source": "ffprobe_measured",
                        "size_bytes": info["size_bytes"],
                        "sha256": info["sha256"],
                        "status": "completed",
                        "reused": True,
                    })

            if info is None:
                info, provider_used = (
                    synthesize_chunk_with_fallback(
                        chunk,
                        output_path,
                        providers,
                        config,
                        speed,
                        job["voice"],
                        allow_paid,
                        max_retries,
                        blocked_paid_providers,
                        job.get("voice_profile") or {},
                    )
                )
                manifest["summary"]["generated_chunks"] += 1



            scene_chunk_paths.append(output_path)
            providers_used.append(provider_used)

            chunk_record = {
                "job_id": job_id,
                "part": job["part"],
                "scene": job["scene"],
                "scene_id": job["scene_id"],
                "role": job["role"],
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
                "text_sha256": hashlib.sha256(
                    chunk.encode("utf-8")
                ).hexdigest(),
                "output_path": str(
                    output_path.relative_to(ROOT)
                ),
                "provider": provider_used,
                "duration": info["duration"],
                "duration_source": "ffprobe_measured",
                "size_bytes": info["size_bytes"],
                "sha256": info["sha256"],
                "status": "completed",
                "reused": chunk_reused,
            }

            chunk_records.append(chunk_record)
            checkpoint_records.append(chunk_record)

            save_checkpoint(
                checkpoint_records,
                source_hash,
                config_hash,
            )

        scene_output = (
            OUTPUT_DIR
            / f"part-{job['part']:02d}"
            / f"{job['scene_id']}.mp3"
        )

        scene_info = merge_scene_chunks(
            scene_chunk_paths,
            scene_output,
        )

        unique_providers = list(
            dict.fromkeys(providers_used)
        )

        provider_label = (
            unique_providers[0]
            if len(unique_providers) == 1
            else "mixed"
        )

        scene_record = {
            "part": job["part"],
            "scene": job["scene"],
            "scene_id": job["scene_id"],
            "role": job["role"],
            "text": job["text"],
            "voice": job["voice"],
            "audio_path": str(
                scene_output.relative_to(ROOT)
            ),
            "audio_file": str(
                scene_output.relative_to(ROOT)
            ),
            "duration": scene_info["duration"],
            "actual_audio_duration_seconds": (
                scene_info["duration"]
            ),
            "audio_duration": scene_info["duration"],
            "duration_source": "ffprobe_measured",
            "audio_duration_source": "ffprobe_measured",
            "size_bytes": scene_info["size_bytes"],
            "sha256": scene_info["sha256"],
            "audio_sha256": scene_info["sha256"],
            "chunk_count": len(chunks),
            "providers": unique_providers,
            "provider": provider_label,
            "tts_provider": provider_label,
            "tts_fingerprint": scene_fingerprint,
            "fingerprint": scene_fingerprint,
            "status": "completed",
            "chunks": chunk_records,
        }

        scene_records.append(scene_record)
        manifest["jobs"].append(scene_record)
        manifest["summary"]["completed_jobs"] += 1
        manifest["summary"]["generated_scene_audio"] += 1

        save_json_atomic(MANIFEST_FILE, manifest)




    expected_ids = {
        job["scene_id"] for job in jobs
    }
    completed_ids = {
        record["scene_id"] for record in scene_records
    }

    if completed_ids != expected_ids:
        missing = sorted(expected_ids - completed_ids)

        raise RuntimeError(
            "TTS did not complete every narration role: "
            + ", ".join(missing[:20])
        )

    part_records = []

    for part in range(1, parts_count + 1):
        current = [
            record for record in scene_records
            if record["part"] == part
        ]

        part_records.append(
            combine_part_audio(current, part)
        )

    manifest["parts"] = part_records
    manifest["status"] = "completed"
    manifest["retryable"] = False
    manifest["completed_at"] = utc_now()
    manifest.pop("failure", None)
    manifest.pop("failure_type", None)

    manifest["summary"]["total_duration"] = round(
        sum(
            item["actual_audio_duration_seconds"]
            for item in part_records
        ),
        3,
    )

    manifest["summary"]["completed_jobs"] = len(
        scene_records
    )

    manifest["summary"]["actual_scene_counts"] = (
        actual_counts
    )

    save_json_atomic(MANIFEST_FILE, manifest)

    write_back_narration_audio(
        narration_data,
        scene_records,
        source_hash,
    )

    saved_narration = load_json(NARRATION_FILE)

    if not isinstance(saved_narration, dict):
        raise RuntimeError(
            "Narration audio write-back verification failed."
        )

    for record in scene_records:
        matching = None

        for field in ("scenes", "extra_scenes"):
            for row in saved_narration.get(field, []):
                if (
                    isinstance(row, dict)
                    and row.get("scene_id") == record["scene_id"]
                ):
                    matching = row
                    break

            if matching is not None:
                break

        if matching is None:
            raise RuntimeError(
                f"Narration write-back missing: {record['scene_id']}"
            )


        if (
            matching.get("audio_duration_source")
            != "ffprobe_measured"
            or matching.get("audio_sha256")
            != record["sha256"]
            or matching.get("audio_path")
            != record["audio_path"]
        ):
            raise RuntimeError(
                f"Narration write-back verification failed: "
                f"{record['scene_id']}"
            )

    log("TTS GENERATION COMPLETED")
    log("FORMAT:", mode)
    log("PARTS:", parts_count)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", actual_counts)
    log("TOTAL AUDIO RECORDS:", len(scene_records))
    log(
        "REUSED SCENE AUDIO:",
        manifest["summary"]["reused_scene_audio"],
    )
    log(
        "GENERATED SCENE AUDIO:",
        manifest["summary"]["generated_scene_audio"],
    )
    log(
        "REUSED CHUNKS:",
        manifest["summary"]["reused_chunks"],
    )
    log(
        "GENERATED CHUNKS:",
        manifest["summary"]["generated_chunks"],
    )
    log("DURATION SOURCE: ffprobe_measured")
    log("MANIFEST:", MANIFEST_FILE.relative_to(ROOT))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except KeyboardInterrupt:
        log(
            "TTS interrupted. Completed chunks remain checkpointed."
        )
        raise SystemExit(130)

    except Exception as exc:
        log("TTS FAILED:", str(exc))

        try:
            current = load_json(
                MANIFEST_FILE, required=False
            )

            if isinstance(current, dict):
                current["status"] = "failed"
                current["retryable"] = True
                current["completed_at"] = utc_now()
                current["failure"] = str(exc)[:1000]
                current["failure_type"] = type(exc).__name__
                current["retry_hint"] = (
                    "Rerun generate_tts.py with the same narration "
                    "and configuration. Verified completed scene audio "
                    "and checkpoint chunks will be reused."
                )

                save_json_atomic(
                    MANIFEST_FILE, current
                )

        except Exception as manifest_exc:
            log(
                "FAILED TO SAVE RETRYABLE TTS MANIFEST:",
                str(manifest_exc),
            )

        raise SystemExit(1)

