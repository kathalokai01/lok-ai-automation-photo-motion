#!/usr/bin/env python3
"""Resumable, role-aware Hindi TTS with ordered provider fallback."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_selected_parts,
    get_scenes,
    get_format,
    get_tts_provider_order,
)

NARRATION_FILE = ROOT / "output/narration/narration.json"
OUTPUT_DIR = ROOT / "output/narration/audio"
MANIFEST_FILE = ROOT / "output/narration/audio_jobs.json"
CHECKPOINT_FILE = ROOT / "output/checkpoints/tts_progress.json"

VERSION = 12
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
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
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
        raise RuntimeError(f"{label}: path escapes repository root.") from exc
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
        # Do not print command arguments or environment credentials.
        detail = result.stderr or result.stdout or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Local command failed with exit code {result.returncode}: "
            f"{str(detail)[-800:]}"
        )
    return result


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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
            if not data:
                raise RuntimeError("Provider returned an empty response.")
            return data, content_type
    except urllib.error.HTTPError as exc:
        # Provider response bodies can echo sensitive request data.
        # Only expose status code and a sanitized short classification.
        if exc.code in {401, 403}:
            reason = "authentication_or_permission"
        elif exc.code == 402:
            reason = "payment_required"
        elif exc.code == 429:
            reason = "quota_or_rate_limit"
        elif exc.code >= 500:
            reason = "provider_server_error"
        else:
            reason = "provider_request_rejected"
        raise RuntimeError(
            f"Provider HTTP {exc.code}: {reason}"
        ) from None
    except urllib.error.URLError:
        raise RuntimeError("Provider network connection failed.") from None


def request_json(url: str, headers: dict, payload: dict) -> dict:
    data, _ = request_bytes(url, headers, payload)
    try:
        result = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Provider returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Provider response is not a JSON object.")
    return result


def audio_duration(path: Path) -> float:
    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError("Audio is missing or too small.")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for audio verification.")

    result = run_command([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ], timeout=60)

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("FFprobe returned an invalid duration.") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("Audio duration is invalid.")
    if duration > MAX_AUDIO_SECONDS:
        raise RuntimeError("Audio exceeds the maximum duration.")
    return round(duration, 3)


def validate_audio(path: Path) -> dict:
    return {
        "duration": audio_duration(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def write_audio_bytes(path: Path, data: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".partial" + path.suffix)
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
        run_command([
            shutil.which("ffmpeg") or "ffmpeg",
            "-y", "-v", "error", "-i", wav_path,
            "-codec:a", "libmp3lame", "-q:a", "3",
            output_path,
        ])
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
        raise RuntimeError("TTS SPEED must be between -50% and +50%.")
    return f"{amount:+d}%"


def resolve_voice(provider: str, voice: str) -> str:
    voice = str(voice or "male").strip()
    if provider == "edge_tts":
        if voice.lower() in {"female", "woman", "f"}:
            return os.getenv("EDGE_TTS_VOICE", "hi-IN-SwaraNeural")
        if voice.lower() in {"male", "man", "m"}:
            return os.getenv("EDGE_TTS_VOICE", "hi-IN-MadhurNeural")
        return voice
    if provider == "gemini_tts":
        configured = os.getenv("GEMINI_TTS_VOICE", "").strip()
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
        if name in ALLOWED_PROVIDERS and name not in normalized:
            normalized.append(name)

    if not normalized:
        raise RuntimeError("Configured TTS provider order is empty.")

    # Preserve the exact configured order; never reorder providers here.
    return normalized


def billable_allowed(config: dict) -> bool:
    config_permission = cfg_bool(
        config, "ALLOW_BILLABLE_PROVIDERS", False
    )
    tts_permission = cfg_bool(config, "TTS_ALLOW_PAID", False)

    env_value = os.getenv("ALLOW_BILLABLE_PROVIDERS")
    if env_value is not None:
        normalized = env_value.strip().lower()
        if normalized not in {
            "true", "1", "yes", "on",
            "false", "0", "no", "off",
        }:
            raise RuntimeError("Invalid ALLOW_BILLABLE_PROVIDERS value.")
        config_permission = config_permission and normalized in {
            "true", "1", "yes", "on",
        }

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
        model_path = Path(model).expanduser() if model else None
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

    return (True, "available") if ok else (False, "credentials_or_runtime_missing")


def classify_provider_error(error: Exception) -> str:
    message = str(error).lower()
    if "http 401" in message or "http 403" in message:
        return "authentication_or_permission"
    if "http 402" in message:
        return "payment_required"
    if "http 429" in message:
        return "quota_or_rate_limit"
    if "quota" in message or "billing" in message:
        return "quota_or_billing"
    if "payment" in message or "insufficient credit" in message:
        return "payment_required"
    return "transient_or_unknown"


def should_retry_same_provider(error: Exception) -> bool:
    return classify_provider_error(error) not in {
        "authentication_or_permission",
        "payment_required",
        "quota_or_rate_limit",
        "quota_or_billing",
    }


def split_text(
    value: str,
    max_chars: int,
    min_chars: int,
) -> list[str]:
    source = str(value).strip()
    if not source:
        raise RuntimeError("Cannot synthesize empty narration.")
    if len(source) > MAX_TEXT_CHARS:
        raise RuntimeError("Narration exceeds maximum supported length.")

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
        for match in re.finditer(r"[,;:，；：](?:\s+|$)", sentence):
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
                    raise RuntimeError("One narration token exceeds chunk limit.")
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

    normalize = lambda item: re.sub(r"\s+", " ", item).strip()
    if normalize(" ".join(merged)) != normalize(source):
        raise RuntimeError("TTS chunking changed narration text.")
    return merged


def synthesize_provider(
    provider: str,
    text: str,
    output_path: Path,
    config: dict,
    speed: str,
    voice: str,
) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "edge_tts":
        temporary = output_path.with_name(output_path.stem + ".edge.partial.mp3")
        try:
            run_command([
                shutil.which("edge-tts") or "edge-tts",
                "--voice", resolve_voice(provider, voice),
                "--rate", speed,
                "--text", text,
                "--write-media", temporary,
            ])
            info = validate_audio(temporary)
            os.replace(temporary, output_path)
            return info
        finally:
            temporary.unlink(missing_ok=True)

    if provider == "piper":
        model = os.getenv("PIPER_MODEL") or str(config.get("PIPER_MODEL", ""))
        executable = shutil.which("piper") or shutil.which("piper-tts")
        if not executable or not model or not Path(model).expanduser().is_file():
            raise RuntimeError("Piper executable or model unavailable.")
        wav_path = output_path.with_suffix(".piper.wav")
        try:
            subprocess.run(
                [executable, "--model", str(Path(model).expanduser()),
                 "--output_file", str(wav_path)],
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=REQUEST_TIMEOUT,
            )
            run_command([
                shutil.which("ffmpeg") or "ffmpeg",
                "-y", "-v", "error", "-i", wav_path,
                "-codec:a", "libmp3lame", "-q:a", "3",
                output_path,
            ])
        finally:
            wav_path.unlink(missing_ok=True)
        return validate_audio(output_path)

    if provider == "fish_audio":
        key = os.getenv("FISH_AUDIO_API_KEY") or os.getenv("FISH_API_KEY")
        payload = {
            "text": text,
            "format": "mp3",
            "mp3_bitrate": 128,
            "latency": "normal",
            "normalize": True,
        }
        reference = os.getenv("FISH_REFERENCE_ID", "").strip()
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
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        model = os.getenv(
            "GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"
        )
        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": resolve_voice(provider, voice)
                        }
                    }
                }
            }
        }
        result = request_json(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={key}",
            {},
            payload,
        )
        try:
            parts = result["candidates"][0]["content"]["parts"]
            item = next(
                part for part in parts
                if isinstance(part.get("inlineData"), dict)
            )
            pcm = base64.b64decode(item["inlineData"]["data"], validate=True)
        except Exception as exc:
            raise RuntimeError("Gemini TTS returned no valid audio.") from exc
        return pcm_to_mp3(pcm, output_path, 24000)

    if provider == "elevenlabs":
        key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
        if not key or not voice_id:
            raise RuntimeError("ElevenLabs API key and Voice ID are required.")
        payload = {
            "text": text,
            "model_id": os.getenv(
                "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"
            ),
            "voice_settings": {
                "stability": 0.48,
                "similarity_boost": 0.78,
                "style": 0.15,
                "use_speaker_boost": True,
            },
        }
        data, _ = request_bytes(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            "?output_format=mp3_44100_128",
            {"xi-api-key": key, "Accept": "audio/mpeg"},
            payload,
        )
        return write_audio_bytes(output_path, data)

    if provider == "cartesia":
        payload = {
            "model_id": os.getenv("CARTESIA_TTS_MODEL", "sonic-2"),
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
                    "CARTESIA_API_VERSION", "2025-04-16"
                ),
                "Accept": "audio/mpeg",
            },
            payload,
        )
        return write_audio_bytes(output_path, data)

    if provider == "minimax":
        payload = {
            "model": os.getenv("MINIMAX_TTS_MODEL", "speech-02-hd"),
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": os.getenv("MINIMAX_TTS_VOICE_ID"),
                "speed": 1.0 + int(speed.replace("%", "")) / 100.0,
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
            os.getenv("MINIMAX_TTS_URL", "https://api.minimax.io/v1/t2a_v2"),
            {"Authorization": f"Bearer {os.getenv('MINIMAX_API_KEY')}"},
            payload,
        )
        try:
            audio = bytes.fromhex(result["data"]["audio"])
        except Exception as exc:
            raise RuntimeError("MiniMax returned no usable audio.") from exc
        return write_audio_bytes(output_path, audio)

    if provider == "finevoice":
        payload = {
            "text": text,
            "voice_id": os.getenv("FINEVOICE_VOICE_ID", ""),
            "format": "mp3",
        }
        data, content_type = request_bytes(
            os.getenv("FINEVOICE_TTS_URL", ""),
            {
                "Authorization": f"Bearer {os.getenv('FINEVOICE_API_KEY')}",
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
                    encoded = encoded.get("audio") or encoded.get("audio_base64")
                data = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise RuntimeError("FineVoice returned invalid audio JSON.") from exc
        return write_audio_bytes(output_path, data)

    raise RuntimeError("Unsupported configured TTS provider.")

def extract_jobs(data: Any) -> list[dict]:
    if isinstance(data, list):
        raw_jobs = data
    elif isinstance(data, dict):
        raw_jobs = (
            data.get("jobs")
            or data.get("narrations")
            or data.get("parts")
            or data.get("scenes")
        )
        if not isinstance(raw_jobs, list):
            raise RuntimeError(
                "Narration JSON must contain a jobs, narrations, parts or scenes list."
            )
    else:
        raise RuntimeError("Narration JSON has an unsupported structure.")

    jobs = []

    def add_job(item: dict, parent: dict | None = None) -> None:
        parent = parent or {}
        merged = {**parent, **item}

        narration = (
            merged.get("narration")
            or merged.get("text")
            or merged.get("script")
            or merged.get("content")
            or merged.get("voice_text")
        )

        if not isinstance(narration, str) or not narration.strip():
            return

        part = merged.get("part", merged.get("part_number", 1))
        try:
            part = int(part)
        except (TypeError, ValueError):
            raise RuntimeError("Narration job has an invalid part number.")

        if part < 1:
            raise RuntimeError("Narration part number must be positive.")

        scene = merged.get(
            "global_scene",
            merged.get("scene_index", merged.get("scene_number", 1)),
        )
        try:
            scene = int(scene)
        except (TypeError, ValueError):
            raise RuntimeError("Narration job has an invalid scene number.")

        if scene < 1:
            raise RuntimeError("Narration scene number must be positive.")

        role = text_value(
            merged.get("role")
            or merged.get("scene_role")
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
            role = "main"

        scene_id = text_value(
            merged.get("scene_id")
            or merged.get("id")
            or merged.get("source_scene_id")
        )

        if not scene_id:
            scene_id = f"part-{part:02d}-scene-{scene:03d}-{role}"

        voice = text_value(
            merged.get("voice")
            or merged.get("voice_type")
            or merged.get("gender")
            or merged.get("speaker"),
            "male",
        )

        jobs.append({
            "part": part,
            "scene": scene,
            "scene_id": scene_id,
            "role": role,
            "text": re.sub(r"\s+", " ", narration).strip(),
            "voice": voice,
            "source": merged,
        })

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue

        nested = (
            item.get("scenes")
            or item.get("narration_jobs")
            or item.get("jobs")
        )

        if isinstance(nested, list):
            parent = {
                key: value
                for key, value in item.items()
                if key not in {"scenes", "narration_jobs", "jobs"}
            }
            for child in nested:
                if isinstance(child, dict):
                    add_job(child, parent)
        else:
            add_job(item)

    if not jobs:
        raise RuntimeError("No usable narration jobs were found.")

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
            job["scene"],
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
            f"Narration PARTS mismatch: expected {expected}, found {actual}."
        )

    part_numbers = sorted({job["part"] for job in jobs})
    if part_numbers != list(range(1, actual + 1)):
        raise RuntimeError(
            f"Narration part numbering is not continuous: {part_numbers}"
        )

    return actual


def validate_role_structure(
    jobs: list[dict],
    actual_parts: int,
) -> None:
    grouped: dict[int, list[dict]] = {}
    for job in jobs:
        grouped.setdefault(job["part"], []).append(job)

    for part in range(1, actual_parts + 1):
        roles = [job["role"] for job in grouped.get(part, [])]

        if not roles:
            raise RuntimeError(f"Part {part} has no narration.")

        if roles.count("hook") != 1:
            raise RuntimeError(
                f"Part {part} must contain exactly one Hook narration."
            )

        if "main" not in roles:
            raise RuntimeError(f"Part {part} has no Main narration.")

        if part < actual_parts and roles.count("suspense") != 1:
            raise RuntimeError(
                f"Non-final Part {part} must contain exactly one Suspense narration."
            )

        if part == actual_parts and "suspense" in roles:
            raise RuntimeError(
                "Final Part must not contain a cliffhanger Suspense narration."
            )

        if part == actual_parts and roles.count("resolution") != 1:
            raise RuntimeError(
                "Final Part must contain exactly one Resolution narration."
            )


def job_output_path(
    part: int,
    scene_id: str,
    chunk_index: int,
) -> Path:
    safe_id = re.sub(r"[^\w.-]+", "-", scene_id, flags=re.UNICODE)
    safe_id = safe_id.strip(".-")[:100]
    if not safe_id:
        raise RuntimeError("Scene ID cannot be converted to a safe filename.")

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
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

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

    save_json_atomic(CHECKPOINT_FILE, {
        "schema_version": VERSION,
        "updated_at": utc_now(),
        "input_sha256": input_hash,
        "config_sha256": config_hash,
        "completed_chunks": completed,
        "completed_count": len(completed),
    })


def load_reusable_records(
    input_hash: str,
    config_hash: str,
) -> dict[str, dict]:
    data = load_json(CHECKPOINT_FILE, required=False)
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
) -> tuple[dict, str]:
    failures = []

    for provider in providers:
        if provider in blocked_paid_providers:
            failures.append({
                "provider": provider,
                "reason": "paid_provider_blocked_after_quota_or_payment_error",
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
                )

                # Verify again before moving into the final checkpoint path.
                verified = validate_audio(provider_output)
                if verified["sha256"] != info["sha256"]:
                    raise RuntimeError("Audio SHA256 changed after synthesis.")

                os.replace(provider_output, output_path)
                final_info = validate_audio(output_path)

                if final_info["sha256"] != info["sha256"]:
                    raise RuntimeError("Final audio SHA256 mismatch.")

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
                    "quota_or_rate_limit",
                    "quota_or_billing",
                }:
                    if provider in BILLABLE_PROVIDERS:
                        blocked_paid_providers.add(provider)
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
        raise RuntimeError("No audio chunks are available to merge.")

    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_path.with_name(final_path.stem + ".partial.mp3")
    concat_file = final_path.with_name(final_path.stem + ".concat.txt")

    try:
        with concat_file.open("w", encoding="utf-8") as handle:
            for path in chunk_paths:
                if not path.is_file():
                    raise RuntimeError(f"Audio chunk missing: {path.name}")
                escaped = str(path.resolve()).replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")

        run_command([
            shutil.which("ffmpeg") or "ffmpeg",
            "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-codec:a", "libmp3lame",
            "-q:a", "3",
            temporary,
        ])

        info = validate_audio(temporary)
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
            item["scene"],
            item["scene_id"],
        ),
    )

    paths = [
        safe_relative_path(record["audio_path"], "Scene audio")
        for record in records
    ]

    output = OUTPUT_DIR / f"part-{part:02d}" / f"part-{part:02d}-narration.mp3"
    info = merge_scene_chunks(paths, output)

    return {
        "part": part,
        "audio_path": str(output.relative_to(ROOT)),
        "duration": info["duration"],
        "size_bytes": info["size_bytes"],
        "sha256": info["sha256"],
        "scene_count": len(records),
        "roles": [record["role"] for record in records],
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    config = load_and_validate(ROOT / "Input/topic.txt")
    mode = get_format(config)
    actual_scenes = get_scenes(config)
    actual_parts = get_selected_parts(config)

    if mode not in {"short", "full"}:
        raise RuntimeError("Invalid video FORMAT in input configuration.")

    if actual_scenes < 1:
        raise RuntimeError("SCENES must be positive.")

    narration_data = load_json(NARRATION_FILE)
    jobs = extract_jobs(narration_data)
    parts_count = verify_part_count(jobs, config)
    validate_role_structure(jobs, parts_count)

    providers = provider_order(config)
    allow_paid = billable_allowed(config)
    max_retries = cfg_int(config, "TTS_MAX_RETRIES", 2)
    max_chars = cfg_int(config, "TTS_MAX_CHARS_PER_CHUNK", 1800, 100)
    min_chars = cfg_int(config, "TTS_MIN_CHARS_PER_CHUNK", 250)
    speed = normalize_speed(config.get("TTS_SPEED", "+0%"))

    source_hash = sha256_file(NARRATION_FILE)
    config_hash = stable_hash({
        "format": mode,
        "parts": actual_parts,
        "scenes": actual_scenes,
        "providers": providers,
        "allow_paid": allow_paid,
        "speed": speed,
        "max_chars": max_chars,
        "min_chars": min_chars,
        "voice": config.get("VOICE", config.get("TTS_VOICE", "male")),
    })

    reusable = load_reusable_records(source_hash, config_hash)
    blocked_paid_providers: set[str] = set()

    manifest = {
        "schema_version": VERSION,
        "status": "running",
        "format": mode,
        "parts": parts_count,
        "configured_provider_order": providers,
        "billable_providers_allowed": allow_paid,
        "source_sha256": source_hash,
        "config_sha256": config_hash,
        "started_at": utc_now(),
        "completed_at": None,
        "jobs": [],
        "parts": [],
        "summary": {
            "total_jobs": len(jobs),
            "completed_jobs": 0,
            "reused_chunks": 0,
            "generated_chunks": 0,
            "failed_jobs": 0,
        },
    }

    scene_records = []
    checkpoint_records = []

    for job_index, job in enumerate(jobs, start=1):
        log(
            f"TTS scene {job_index}/{len(jobs)} "
            f"| Part {job['part']} "
            f"| {job['role']} "
            f"| {job['scene_id']}"
        )

        chunks = split_text(job["text"], max_chars, min_chars)
        voice = resolve_voice(
            "edge_tts",
            job["voice"],
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
            "voice": voice,
            "speed": speed,
            "chunk_count": len(chunks),
        })

        scene_chunk_paths = []
        chunk_records = []
        providers_used = []

        for chunk_index, chunk in enumerate(chunks, start=1):
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

            if reusable_record:
                info = valid_existing_audio(
                    output_path,
                    reusable_record["sha256"],
                )
                if info:
                    provider_used = reusable_record.get("provider")
                    manifest["summary"]["reused_chunks"] += 1

            if info is None:
                info, provider_used = synthesize_chunk_with_fallback(
                    chunk,
                    output_path,
                    providers,
                    config,
                    speed,
                    job["voice"],
                    allow_paid,
                    max_retries,
                    blocked_paid_providers,
                )
                manifest["summary"]["generated_chunks"] += 1

            scene_chunk_paths.append(output_path)
            providers_used.append(provider_used)

            record = {
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
                "output_path": str(output_path.relative_to(ROOT)),
                "provider": provider_used,
                "duration": info["duration"],
                "size_bytes": info["size_bytes"],
                "sha256": info["sha256"],
                "status": "completed",
            }

            chunk_records.append(record)
            checkpoint_records.append(record)
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

        scene_record = {
            "part": job["part"],
            "scene": job["scene"],
            "scene_id": job["scene_id"],
            "role": job["role"],
            "audio_path": str(scene_output.relative_to(ROOT)),
            "duration": scene_info["duration"],
            "size_bytes": scene_info["size_bytes"],
            "sha256": scene_info["sha256"],
            "chunk_count": len(chunks),
            "providers": list(dict.fromkeys(providers_used)),
            "status": "completed",
            "chunks": chunk_records,
        }

        scene_records.append(scene_record)
        manifest["jobs"].append(scene_record)
        manifest["summary"]["completed_jobs"] += 1

        save_json_atomic(MANIFEST_FILE, manifest)

    part_records = []
    for part in range(1, parts_count + 1):
        current = [
            record for record in scene_records
            if record["part"] == part
        ]
        part_records.append(combine_part_audio(current, part))

    manifest["parts"] = part_records
    manifest["status"] = "completed"
    manifest["completed_at"] = utc_now()
    manifest["summary"]["total_duration"] = round(
        sum(item["duration"] for item in part_records),
        3,
    )
    manifest["summary"]["completed_jobs"] = len(scene_records)

    save_json_atomic(MANIFEST_FILE, manifest)

    log("TTS GENERATION COMPLETED")
    log("FORMAT:", mode)
    log("PARTS:", parts_count)
    log("SCENES:", len(scene_records))
    log("REUSED CHUNKS:", manifest["summary"]["reused_chunks"])
    log("GENERATED CHUNKS:", manifest["summary"]["generated_chunks"])
    log("MANIFEST:", MANIFEST_FILE.relative_to(ROOT))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("TTS interrupted. Completed chunks remain checkpointed.")
        raise SystemExit(130)
    except Exception as exc:
        log("TTS FAILED:", str(exc))
        try:
            current = load_json(MANIFEST_FILE, required=False)
            if isinstance(current, dict):
                current["status"] = "failed"
                current["completed_at"] = utc_now()
                current["failure"] = str(exc)[:1000]
                save_json_atomic(MANIFEST_FILE, current)
        except Exception:
            pass
        raise SystemExit(1)