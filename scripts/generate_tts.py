#!/usr/bin/env python3
"""Role-aware, resumable TTS with configured provider order and verified audio."""

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

VERSION = 10
MIN_AUDIO_BYTES = 1000
REQUEST_TIMEOUT = 180
MAX_TEXT_CHARS = 100000
MAX_AUDIO_SECONDS = 3600.0

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}

BILLABLE_PROVIDERS = {
    "fish_audio",
    "gemini_tts",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(*values: Any) -> None:
    print(*values, flush=True)


def text_value(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def cfg_text(config: dict, key: str, default: str = "") -> str:
    return text_value(config.get(key), default)


def cfg_bool(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
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
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


def save_json_atomic(path: Path, data: Any) -> None:
    path = Path(path)
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

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes repository root.")

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
            f"Command failed ({result.returncode}): "
            f"{str(detail)[-1800:]}"
        )

    return result


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


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
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
        request_headers.setdefault(
            "Content-Type",
            "application/json",
        )

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")

            if not data:
                raise RuntimeError("Provider returned an empty response.")

            return data, content_type

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Provider HTTP {exc.code}: {detail[:1500]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Provider network error: {exc}"
        ) from exc


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
    path = Path(path)

    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"Audio missing or too small: {path}")

    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        raise RuntimeError("ffprobe is required to measure audio duration.")

    result = run_command([
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ], timeout=60)

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid audio duration: {path}"
        ) from exc

    if (
        not math.isfinite(duration)
        or duration <= 0
        or duration > MAX_AUDIO_SECONDS
    ):
        raise RuntimeError(
            f"Audio duration outside allowed range: {duration}."
        )

    return round(duration, 3)


def validate_audio(path: Path) -> dict:
    path = Path(path)

    if not path.is_file():
        raise RuntimeError(f"Audio file not found: {path}")

    duration = audio_duration(path)

    return {
        "duration": duration,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def normalize_speed(value: Any) -> str:
    value = str(value or "+0%").strip()

    if value.lower() in {"normal", "default", ""}:
        return "+0%"

    if not value.endswith("%"):
        value += "%"

    try:
        amount = int(value[:-1])
    except ValueError as exc:
        raise RuntimeError(f"Invalid SPEED: {value}") from exc

    if not -50 <= amount <= 50:
        raise RuntimeError("SPEED must be between -50% and +50%.")

    return f"{amount:+d}%"


def resolve_voice(provider: str, voice: str) -> str:
    voice = str(voice or "male").strip()

    if provider == "edge_tts":
        if voice.lower() in {"female", "woman", "f"}:
            return os.getenv(
                "EDGE_TTS_VOICE",
                "hi-IN-SwaraNeural",
            )

        if voice.lower() in {"male", "man", "m"}:
            return os.getenv(
                "EDGE_TTS_VOICE",
                "hi-IN-MadhurNeural",
            )

        return voice

    if provider == "gemini_tts":
        configured = os.getenv("GEMINI_TTS_VOICE", "").strip()

        if configured:
            return configured

        return "Kore" if voice.lower() == "female" else "Puck"

    return voice


def provider_order(config: dict) -> list[str]:
    order = get_tts_provider_order(config)

    if not order:
        raise RuntimeError("Configured TTS provider order is empty.")

    # input_config returns the three primary providers first,
    # followed by the configured fallback providers in their given order.
    normalized = []

    for provider in order:
        value = str(provider).strip().lower()

        if value == "elevenlab":
            value = "elevenlabs"

        if value and value not in normalized:
            normalized.append(value)

    if len(normalized) < 3:
        raise RuntimeError(
            "All three configured primary TTS providers are required."
        )

    return normalized


def billable_allowed(config: dict) -> bool:
    global_allowed = cfg_bool(
        config,
        "ALLOW_BILLABLE_PROVIDERS",
        True,
    )

    env_value = os.getenv("ALLOW_BILLABLE_PROVIDERS")

    if env_value is not None:
        normalized = env_value.strip().lower()

        if normalized not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
            raise RuntimeError(
                "ALLOW_BILLABLE_PROVIDERS environment value is invalid."
            )

        global_allowed = global_allowed and normalized in {
            "true", "1", "yes", "on"
        }

    tts_allowed = cfg_bool(
        config,
        "TTS_ALLOW_PAID",
        True,
    )

    return global_allowed and tts_allowed


def provider_available(
    provider: str,
    config: dict,
    allow_paid: bool,
) -> tuple[bool, str]:
    if provider in BILLABLE_PROVIDERS and not allow_paid:
        return False, "Billable provider permission is disabled."

    checks = {
        "fish_audio": bool(
            os.getenv("FISH_AUDIO_API_KEY")
            or os.getenv("FISH_API_KEY")
        ),
        "gemini_tts": bool(
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        ),
        "edge_tts": command_exists("edge-tts"),
        "elevenlabs": bool(
            os.getenv("ELEVENLABS_API_KEY")
            and os.getenv("ELEVENLABS_VOICE_ID")
        ),
        "cartesia": bool(
            os.getenv("CARTESIA_API_KEY")
            and os.getenv("CARTESIA_VOICE_ID")
        ),
        "finevoice": bool(
            os.getenv("FINEVOICE_API_KEY")
            and os.getenv("FINEVOICE_TTS_URL")
        ),
        "minimax": bool(
            os.getenv("MINIMAX_API_KEY")
            and os.getenv("MINIMAX_TTS_VOICE_ID")
        ),
    }

    if provider == "piper":
        model = os.getenv("PIPER_MODEL") or cfg_text(
            config,
            "PIPER_MODEL",
        )
        executable = (
            command_exists("piper")
            or command_exists("piper-tts")
        )
        checks["piper"] = bool(
            model and Path(model).is_file() and executable
        )

    if provider not in checks:
        return False, f"Unsupported configured provider: {provider}"

    if not checks[provider]:
        return False, (
            "Required API key, executable, voice ID or model is unavailable."
        )

    return True, "available"


def split_text(
    value: str,
    max_chars: int,
    min_chars: int,
) -> list[str]:
    """Preserve all text and punctuation; split at sentences, clauses, then words."""
    source = str(value).strip()

    if not source:
        raise RuntimeError("Cannot synthesize empty narration.")

    if len(source) > MAX_TEXT_CHARS:
        raise RuntimeError("Narration exceeds maximum supported text length.")

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
        unit = source[start:end].strip()

        if unit:
            sentences.append(unit)

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

        # Long sentence: split at natural clause boundaries while retaining
        # the comma/semicolon/colon on the preceding clause.
        clause_start = 0

        for match in re.finditer(r"[,;:，；：](?:\s+|$)", sentence):
            clause_end = match.end()
            clause = sentence[clause_start:clause_end].strip()

            if clause:
                units.append(clause)

            clause_start = clause_end

        remainder = sentence[clause_start:].strip()

        if remainder:
            units.append(remainder)

    chunks = []
    current = ""

    for unit in units:
        if len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ""

            words = re.findall(r"\S+", unit)
            word_chunk = ""

            for word in words:
                if len(word) > max_chars:
                    raise RuntimeError(
                        "A single unbroken narration token exceeds "
                        "TTS_CHUNK_MAX_CHARS."
                    )

                candidate = f"{word_chunk} {word}".strip()

                if word_chunk and len(candidate) > max_chars:
                    chunks.append(word_chunk)
                    word_chunk = word
                else:
                    word_chunk = candidate

            if word_chunk:
                chunks.append(word_chunk)

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

    if not merged or any(not item for item in merged):
        raise RuntimeError("TTS chunking produced an empty chunk.")

    # Exact reconstruction check: only whitespace normalization is allowed.
    reconstructed = " ".join(merged)

    if re.sub(r"\s+", " ", reconstructed).strip() != re.sub(
        r"\s+", " ", source
    ).strip():
        raise RuntimeError("TTS chunking changed or omitted narration text.")

    return merged


def write_audio_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(
        path.stem + ".partial" + path.suffix
    )

    try:
        temporary.write_bytes(data)

        if temporary.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError(
                "Provider audio response is too small."
            )

        validate_audio(temporary)
        os.replace(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def pcm_to_mp3(
    pcm: bytes,
    output_path: Path,
    sample_rate: int = 24000,
) -> dict:
    output_path = Path(output_path)
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
            "-y", "-v", "error",
            "-i", wav_path,
            "-codec:a", "libmp3lame",
            "-q:a", "3",
            output_path,
        ])

    finally:
        wav_path.unlink(missing_ok=True)

    return validate_audio(output_path)


def concat_chunks(
    paths: list[Path],
    output_path: Path,
) -> dict:
    if not paths:
        raise RuntimeError("No TTS chunks available.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(paths) == 1:
        shutil.copy2(paths[0], output_path)
        return validate_audio(output_path)

    list_file = output_path.with_suffix(".concat.txt")

    try:
        lines = []

        for path in paths:
            escaped = str(Path(path).resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")

        list_file.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

        run_command([
            shutil.which("ffmpeg") or "ffmpeg",
            "-y", "-v", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-codec:a", "libmp3lame",
            "-q:a", "3",
            output_path,
        ])

    finally:
        list_file.unlink(missing_ok=True)

    return validate_audio(output_path)


def synthesize_provider(
    provider: str,
    text: str,
    output_path: Path,
    config: dict,
    speed: str,
    voice: str,
) -> dict:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "edge_tts":
        temporary = output_path.with_name(
            output_path.stem + ".edge.partial.mp3"
        )

        try:
            run_command([
                shutil.which("edge-tts") or "edge-tts",
                "--voice", resolve_voice(provider, voice),
                "--rate", speed,
                "--text", text,
                "--write-media", temporary,
            ])

            validate_audio(temporary)
            os.replace(temporary, output_path)

        finally:
            temporary.unlink(missing_ok=True)

        return validate_audio(output_path)

    if provider == "piper":
        model = os.getenv("PIPER_MODEL") or cfg_text(
            config,
            "PIPER_MODEL",
        )
        executable = (
            shutil.which("piper")
            or shutil.which("piper-tts")
        )

        if not model or not executable:
            raise RuntimeError("Piper executable or model is missing.")

        wav_path = output_path.with_suffix(".piper.wav")

        try:
            subprocess.run(
                [
                    executable,
                    "--model", model,
                    "--output_file", str(wav_path),
                ],
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=REQUEST_TIMEOUT,
            )

            run_command([
                shutil.which("ffmpeg") or "ffmpeg",
                "-y", "-v", "error",
                "-i", wav_path,
                "-codec:a", "libmp3lame",
                "-q:a", "3",
                output_path,
            ])

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

        reference = os.getenv("FISH_REFERENCE_ID", "").strip()
        model = os.getenv("FISH_TTS_MODEL", "s1")

        if reference:
            payload["reference_id"] = reference

        if model:
            payload["model"] = model

        data, _ = request_bytes(
            "https://api.fish.audio/v1/tts",
            {"Authorization": f"Bearer {key}"},
            payload,
        )

        write_audio_bytes(output_path, data)
        return validate_audio(output_path)

    if provider == "gemini_tts":
        key = (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )

        model = os.getenv(
            "GEMINI_TTS_MODEL",
            "gemini-2.5-flash-preview-tts",
        )

        voice_name = resolve_voice(provider, voice)

        payload = {
            "contents": [{
                "parts": [{"text": text}]
            }],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": voice_name
                        }
                    }
                },
            },
        }

        result = request_json(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={key}",
            {},
            payload,
        )

        try:
            parts = result["candidates"][0]["content"]["parts"]
            audio_item = next(
                item for item in parts
                if isinstance(item, dict)
                and isinstance(item.get("inlineData"), dict)
            )

            pcm = base64.b64decode(
                audio_item["inlineData"]["data"],
                validate=True,
            )

        except Exception as exc:
            raise RuntimeError(
                "Gemini TTS returned no valid audio."
            ) from exc

        return pcm_to_mp3(pcm, output_path, 24000)

    if provider == "elevenlabs":
        key = os.getenv("ELEVENLABS_API_KEY")
        voice_id = os.getenv("ELEVENLABS_VOICE_ID")
        model = os.getenv(
            "ELEVENLABS_MODEL_ID",
            "eleven_multilingual_v2",
        )

        payload = {
            "text": text,
            "model_id": model,
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
            {
                "xi-api-key": key,
                "Accept": "audio/mpeg",
            },
            payload,
        )

        write_audio_bytes(output_path, data)
        return validate_audio(output_path)

    if provider == "cartesia":
        payload = {
            "model_id": os.getenv(
                "CARTESIA_TTS_MODEL",
                "sonic-2",
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

        write_audio_bytes(output_path, data)
        return validate_audio(output_path)

    if provider == "minimax":
        payload = {
            "model": os.getenv(
                "MINIMAX_TTS_MODEL",
                "speech-02-hd",
            ),
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
            audio = bytes.fromhex(result["data"]["audio"])
        except Exception as exc:
            raise RuntimeError(
                "MiniMax returned no usable audio."
            ) from exc

        write_audio_bytes(output_path, audio)
        return validate_audio(output_path)

    if provider == "finevoice":
        payload = {
            "text": text,
            "voice_id": os.getenv("FINEVOICE_VOICE_ID", ""),
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

                if not encoded:
                    raise ValueError("Missing audio field.")

                data = base64.b64decode(
                    encoded,
                    validate=True,
                )

            except Exception as exc:
                raise RuntimeError(
                    "FineVoice JSON response has no valid audio."
                ) from exc

        write_audio_bytes(output_path, data)
        return validate_audio(output_path)

    raise RuntimeError(
        f"Unsupported configured TTS provider: {provider}"
    )

def safe_id(value: Any) -> str:
    result = re.sub(
        r"[^\w-]+",
        "_",
        str(value or "").strip(),
        flags=re.UNICODE,
    )
    return result.strip("_")[:100] or "scene"


def story_slug(title: str) -> str:
    slug = safe_id(title).strip("_-")

    if not slug:
        raise RuntimeError(
            "Story title cannot produce a valid output filename."
        )

    return slug[:90]


def record_identity(row: dict) -> tuple[str, int, str, int | None]:
    if not isinstance(row, dict):
        raise RuntimeError("Narration record must be an object.")

    try:
        part = int(row["part"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Narration record has invalid part.") from exc

    if part < 1:
        raise RuntimeError("Narration part must be positive.")

    role = str(row.get("role", "main")).strip().lower()

    if role == "main":
        try:
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Part {part}: main scene number is invalid."
            ) from exc

        if scene < 1:
            raise RuntimeError("Main scene number must be positive.")

        expected = f"part_{part:02d}_scene_{scene:02d}"
        actual = str(row.get("scene_id") or "").strip()

        if actual != expected:
            raise RuntimeError(
                f"Main scene ID mismatch: expected {expected}, "
                f"received {actual!r}."
            )

        return expected, part, role, scene

    if role not in {"hook", "suspense", "resolution"}:
        raise RuntimeError(f"Unsupported narration role: {role!r}.")

    expected = f"part_{part:02d}_{role}"
    actual = str(row.get("scene_id") or "").strip()

    if actual != expected:
        raise RuntimeError(
            f"Extra scene ID mismatch: expected {expected}, "
            f"received {actual!r}."
        )

    return expected, part, role, None


def narration_text(row: dict) -> str:
    value = row.get("text")

    if value is None:
        value = row.get("narration")

    value = str(value or "").strip()

    if not value:
        raise RuntimeError(
            f"Empty narration text for {row.get('scene_id', 'unknown')}."
        )

    if len(value) > MAX_TEXT_CHARS:
        raise RuntimeError(
            f"Narration exceeds maximum length for {row.get('scene_id')}."
        )

    return value


def output_path_for(row: dict, slug: str) -> Path:
    scene_id, part, role, scene = record_identity(row)
    folder = OUTPUT_DIR / slug / f"part_{part:02d}"

    if role == "main":
        filename = f"{slug}_part_{part:02d}_scene_{scene:02d}.mp3"
    else:
        filename = f"{slug}_part_{part:02d}_{role}.mp3"

    return folder / filename


def chunk_path_for(
    row: dict,
    slug: str,
    chunk_index: int,
) -> Path:
    scene_id, part, role, scene = record_identity(row)
    folder = (
        OUTPUT_DIR
        / slug
        / f"part_{part:02d}"
        / "chunks"
    )

    if role == "main":
        stem = f"{slug}_part_{part:02d}_scene_{scene:02d}"
    else:
        stem = f"{slug}_part_{part:02d}_{role}"

    return folder / f"{stem}_chunk_{chunk_index:03d}.mp3"


def scene_fingerprint(
    row: dict,
    settings: dict,
    order: list[str],
) -> str:
    scene_id, part, role, scene = record_identity(row)

    return stable_hash({
        "version": VERSION,
        "scene_id": scene_id,
        "part": part,
        "scene": scene,
        "role": role,
        "text": narration_text(row),
        "settings": settings,
        "provider_order": order,
    })


def validate_narration_records(
    data: Any,
    parts: int,
    scenes_per_part: int,
) -> tuple[list[dict], list[dict]]:
    if not isinstance(data, dict):
        raise RuntimeError(
            "Narration manifest must be a JSON object."
        )

    if data.get("status") != "completed":
        raise RuntimeError(
            "Narration text preparation is not completed."
        )

    if data.get("total_parts") != parts:
        raise RuntimeError(
            "Narration PARTS do not match the selected PARTS."
        )

    if data.get("scenes_per_part") != scenes_per_part:
        raise RuntimeError(
            "Narration SCENES do not match configured SCENES."
        )

    main_rows = data.get("scenes")
    extra_rows = data.get("extra_scenes")

    if not isinstance(main_rows, list) or not isinstance(extra_rows, list):
        raise RuntimeError(
            "Narration manifest must contain scenes and extra_scenes lists."
        )

    expected_main = [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    main_map: dict[tuple[int, int], dict] = {}

    for index, row in enumerate(main_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Invalid main narration record {index}."
            )

        scene_id, part, role, scene = record_identity(row)

        if role != "main":
            raise RuntimeError(
                f"{scene_id}: main narration list contains another role."
            )

        if not 1 <= part <= parts or not 1 <= scene <= scenes_per_part:
            raise RuntimeError(
                f"{scene_id}: record is outside selected PARTS/SCENES."
            )

        key = (part, scene)

        if key in main_map:
            raise RuntimeError(
                f"Duplicate main narration mapping: {key}."
            )

        narration_text(row)
        main_map[key] = dict(row)

    if set(main_map) != set(expected_main):
        missing = sorted(set(expected_main) - set(main_map))
        extra = sorted(set(main_map) - set(expected_main))

        raise RuntimeError(
            f"Main narration mapping mismatch. "
            f"Missing={missing[:20]}, unexpected={extra[:20]}."
        )

    extra_map: dict[str, dict] = {}

    for index, row in enumerate(extra_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Invalid extra narration record {index}."
            )

        scene_id, part, role, scene = record_identity(row)

        if role == "main":
            raise RuntimeError(
                f"{scene_id}: main role cannot be an extra record."
            )

        if not 1 <= part <= parts:
            raise RuntimeError(
                f"{scene_id}: extra narration part is out of range."
            )

        if scene_id in extra_map:
            raise RuntimeError(
                f"Duplicate extra narration ID: {scene_id}."
            )

        narration_text(row)
        extra_map[scene_id] = dict(row)

    expected_roles = []

    for part in range(1, parts + 1):
        expected_roles.append((part, "hook"))

        if part < parts:
            expected_roles.append((part, "suspense"))
        else:
            expected_roles.append((part, "resolution"))

    actual_roles = sorted(
        (
            int(row["part"]),
            str(row["role"]).lower(),
        )
        for row in extra_map.values()
    )

    if actual_roles != sorted(expected_roles):
        raise RuntimeError(
            "Hook/Suspense/Resolution records do not match "
            "the required per-part structure."
        )

    ordered_main = [
        main_map[key] for key in expected_main
    ]

    ordered_extras = []

    for part in range(1, parts + 1):
        ordered_extras.append(
            extra_map[f"part_{part:02d}_hook"]
        )

        if part < parts:
            ordered_extras.append(
                extra_map[f"part_{part:02d}_suspense"]
            )
        else:
            ordered_extras.append(
                extra_map[f"part_{part:02d}_resolution"]
            )

    all_rows = ordered_main + ordered_extras
    all_ids = [record_identity(row)[0] for row in all_rows]

    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError(
            "Duplicate scene_id across main and extra narration."
        )

    return ordered_main, ordered_extras


def load_manifest() -> dict:
    data = load_json(MANIFEST_FILE, required=False)

    if data is None:
        return {"jobs": []}

    if not isinstance(data, dict):
        raise RuntimeError("Existing TTS manifest must be an object.")

    if not isinstance(data.get("jobs", []), list):
        raise RuntimeError("Existing TTS manifest jobs must be a list.")

    return data


def manifest_map(manifest: dict) -> dict[str, dict]:
    result = {}

    for row in manifest.get("jobs", []):
        if not isinstance(row, dict):
            continue

        scene_id = str(row.get("scene_id") or "").strip()

        if not scene_id:
            continue

        if scene_id in result:
            raise RuntimeError(
                f"Duplicate scene ID in previous TTS manifest: {scene_id}."
            )

        result[scene_id] = dict(row)

    return result


def save_manifest(
    manifest: dict,
    jobs: dict[str, dict],
    status: str,
) -> None:
    ordered = sorted(
        jobs.values(),
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER.get(str(row.get("role")), 9),
            int(row.get("scene") or 0),
            str(row["scene_id"]),
        ),
    )

    manifest.update({
        "script_version": VERSION,
        "stage": "tts",
        "status": status,
        "jobs": ordered,
        "total_records": len(ordered),
        "completed_count": sum(
            row.get("status") == "completed"
            for row in ordered
        ),
        "failed_count": sum(
            row.get("status") == "failed"
            for row in ordered
        ),
        "updated_at": utc_now(),
    })

    save_json_atomic(MANIFEST_FILE, manifest)


def reusable_audio(
    job: dict | None,
    expected_path: Path,
    fingerprint: str,
) -> bool:
    if not isinstance(job, dict):
        return False

    if job.get("status") != "completed":
        return False

    if job.get("fingerprint") != fingerprint:
        return False

    try:
        stored_path = safe_relative_path(
            job.get("output"),
            "cached TTS output",
        )

        if stored_path != expected_path.resolve():
            return False

        actual = validate_audio(stored_path)
        expected_duration = float(job.get("duration", 0))

        if not math.isfinite(expected_duration) or expected_duration <= 0:
            return False

        if job.get("sha256") != actual["sha256"]:
            return False

        if abs(expected_duration - actual["duration"]) >= 0.15:
            return False

        if job.get("duration_source") != "ffprobe_measured":
            return False

        if job.get("size_bytes") != actual["size_bytes"]:
            return False

        if not job.get("provider"):
            return False

        if not isinstance(job.get("chunk_files"), list):
            return False

        return True

    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def valid_chunk_cache(
    chunk_path: Path,
    meta_path: Path,
    chunk_fingerprint: str,
    provider: str,
) -> bool:
    meta = load_json(meta_path, required=False)

    if not isinstance(meta, dict):
        return False

    if meta.get("status") != "completed":
        return False

    if meta.get("fingerprint") != chunk_fingerprint:
        return False

    if meta.get("provider") != provider:
        return False

    try:
        actual = validate_audio(chunk_path)

        return (
            meta.get("sha256") == actual["sha256"]
            and meta.get("size_bytes") == actual["size_bytes"]
            and abs(
                float(meta.get("duration", 0)) - actual["duration"]
            ) < 0.15
        )

    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def update_narration_audio(
    narration: dict,
    jobs: dict[str, dict],
) -> None:
    """Attach verified, measured TTS results to every narration role."""
    for field in ("scenes", "extra_scenes"):
        rows = narration.get(field, [])

        if not isinstance(rows, list):
            raise RuntimeError(
                f"Narration field {field} is not a list."
            )

        for row in rows:
            if not isinstance(row, dict):
                continue

            scene_id = str(row.get("scene_id") or "").strip()
            job = jobs.get(scene_id)

            if not job or job.get("status") != "completed":
                row["tts_status"] = "pending"
                continue

            output = safe_relative_path(
                job.get("output"),
                f"{scene_id} audio output",
            )

            actual = validate_audio(output)

            if actual["sha256"] != job.get("sha256"):
                raise RuntimeError(
                    f"{scene_id}: audio SHA verification failed."
                )

            duration = actual["duration"]

            row.update({
                "audio": output.relative_to(ROOT).as_posix(),
                "audio_path": output.relative_to(ROOT).as_posix(),
                "audio_file": output.relative_to(ROOT).as_posix(),
                "audio_sha256": actual["sha256"],
                "audio_hash": actual["sha256"],
                "audio_duration": duration,
                "actual_audio_duration_seconds": duration,
                "audio_duration_source": "ffprobe_measured",
                "tts_provider": job["provider"],
                "tts_model": job.get("model", job["provider"]),
                "provider": job["provider"],
                "model": job.get("model", job["provider"]),
                "chunks": job.get("chunk_files", []),
                "chunk_count": job.get("chunk_count", 0),
                "tts_fingerprint": job["fingerprint"],
                "fingerprint": job["fingerprint"],
                "audio_completed_at": job.get("completed_at"),
                "tts_status": "completed",
                "status": "completed",
            })

    all_rows = [
        row
        for field in ("scenes", "extra_scenes")
        for row in narration.get(field, [])
        if isinstance(row, dict)
    ]

    narration["tts_status"] = (
        "completed"
        if all_rows and all(
            jobs.get(str(row.get("scene_id")), {}).get("status")
            == "completed"
            for row in all_rows
        )
        else "in_progress"
    )

    narration["tts_updated_at"] = utc_now()
    save_json_atomic(NARRATION_FILE, narration)


def synthesize_record(
    row: dict,
    config: dict,
    available: list[str],
    voice: str,
    speed: str,
    max_chars: int,
    min_chars: int,
    max_attempts: int,
    chunk_checkpoint: bool,
    old_job: dict | None,
    fingerprint: str,
    slug: str,
) -> tuple[dict, bool]:
    scene_id, part, role, scene = record_identity(row)
    output_file = output_path_for(row, slug)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if reusable_audio(old_job, output_file, fingerprint):
        log(f"REUSED {scene_id}")

        result = dict(old_job)
        result["cache_reused"] = True
        return result, True

    chunks = split_text(
        narration_text(row),
        max_chars,
        min_chars,
    )

    errors = []

    for provider in available:
        log(f"{scene_id}: trying {provider}")

        for attempt in range(1, max_attempts + 1):
            chunk_paths = []

            try:
                for chunk_index, chunk_text in enumerate(chunks, 1):
                    chunk_path = chunk_path_for(
                        row,
                        slug,
                        chunk_index,
                    )

                    chunk_path.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    meta_path = chunk_path.with_suffix(".json")

                    chunk_fingerprint = stable_hash({
                        "version": VERSION,
                        "record_fingerprint": fingerprint,
                        "provider": provider,
                        "chunk_index": chunk_index,
                        "chunk_text": chunk_text,
                    })

                    valid = (
                        chunk_checkpoint
                        and valid_chunk_cache(
                            chunk_path,
                            meta_path,
                            chunk_fingerprint,
                            provider,
                        )
                    )

                    if not valid:
                        chunk_path.unlink(missing_ok=True)
                        meta_path.unlink(missing_ok=True)

                        info = synthesize_provider(
                            provider,
                            chunk_text,
                            chunk_path,
                            config,
                            speed,
                            voice,
                        )

                        save_json_atomic(meta_path, {
                            "status": "completed",
                            "provider": provider,
                            "fingerprint": chunk_fingerprint,
                            "duration": info["duration"],
                            "size_bytes": info["size_bytes"],
                            "sha256": info["sha256"],
                            "completed_at": utc_now(),
                        })

                    chunk_paths.append(chunk_path)

                temporary = output_file.with_name(
                    output_file.stem
                    + f".{provider}.partial.mp3"
                )

                temporary.unlink(missing_ok=True)

                audio_info = concat_chunks(
                    chunk_paths,
                    temporary,
                )

                os.replace(temporary, output_file)

                final_info = validate_audio(output_file)

                if final_info["sha256"] != audio_info["sha256"]:
                    raise RuntimeError(
                        "Final audio changed during atomic replacement."
                    )

                return {
                    "scene_id": scene_id,
                    "part": part,
                    "scene": scene,
                    "role": role,
                    "status": "completed",
                    "output": output_file.relative_to(ROOT).as_posix(),
                    "audio_path": output_file.relative_to(ROOT).as_posix(),
                    "provider": provider,
                    "model": os.getenv(
                        f"{provider.upper()}_TTS_MODEL",
                        provider,
                    ),
                    "voice": voice,
                    "speed": speed,
                    "fingerprint": fingerprint,
                    "chunk_count": len(chunks),
                    "chunk_files": [
                        path.relative_to(ROOT).as_posix()
                        for path in chunk_paths
                    ],
                    "duration": final_info["duration"],
                    "actual_audio_duration_seconds": final_info["duration"],
                    "duration_source": "ffprobe_measured",
                    "size_bytes": final_info["size_bytes"],
                    "sha256": final_info["sha256"],
                    "provider_errors": errors,
                    "cache_reused": False,
                    "completed_at": utc_now(),
                }, False

            except Exception as exc:
                errors.append({
                    "provider": provider,
                    "attempt": attempt,
                    "error": str(exc)[:1500],
                    "at": utc_now(),
                })

                log(
                    f"{provider} attempt {attempt} failed: {exc}"
                )

                temporary = output_file.with_name(
                    output_file.stem
                    + f".{provider}.partial.mp3"
                )
                temporary.unlink(missing_ok=True)
                output_file.unlink(missing_ok=True)

                if attempt < max_attempts:
                    time.sleep(min(2 * attempt, 8))

        log(
            f"{provider} exhausted; moving to next configured provider."
        )

    return {
        "scene_id": scene_id,
        "part": part,
        "scene": scene,
        "role": role,
        "status": "failed",
        "output": output_file.relative_to(ROOT).as_posix(),
        "fingerprint": fingerprint,
        "provider_errors": errors,
        "failed_at": utc_now(),
    }, False


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — ROLE-AWARE MULTI-PROVIDER TTS")
    log("=" * 64)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    fmt = str(get_format(config)).strip().lower()

    if fmt not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {fmt!r}.")

    parts = int(get_selected_parts(config, required=True))
    scenes_per_part = int(get_scenes(config))

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "Invalid selected PARTS or SCENES configuration."
        )

    narration = load_json(NARRATION_FILE)

    if not isinstance(narration, dict):
        raise RuntimeError("Narration file must contain a JSON object.")

    main_rows, extra_rows = validate_narration_records(
        narration,
        parts,
        scenes_per_part,
    )

    records = main_rows + extra_rows

    title = str(
        narration.get("title")
        or narration.get("topic")
        or config.get("TITLE")
        or config.get("TOPIC")
        or ""
    ).strip()

    if not title:
        raise RuntimeError(
            "Story title is missing; TTS filenames cannot be story-based."
        )

    slug = story_slug(title)

    order = provider_order(config)
    allow_paid = billable_allowed(config)

    voice = cfg_text(config, "VOICE", "male").lower()
    speed = normalize_speed(cfg_text(config, "SPEED", "+0%"))

    max_attempts = cfg_int(
        config,
        "TTS_MAX_PROVIDER_ATTEMPTS",
        1,
        minimum=1,
    )

    if max_attempts > 3:
        raise RuntimeError(
            "TTS_MAX_PROVIDER_ATTEMPTS cannot exceed 3."
        )

    max_chars = cfg_int(
        config,
        "TTS_CHUNK_MAX_CHARS",
        500,
        minimum=100,
    )

    min_chars = cfg_int(
        config,
        "TTS_CHUNK_MIN_CHARS",
        150,
        minimum=1,
    )

    if min_chars > max_chars:
        raise RuntimeError(
            "TTS_CHUNK_MIN_CHARS cannot exceed TTS_CHUNK_MAX_CHARS."
        )

    chunk_checkpoint = cfg_bool(
        config,
        "TTS_CHUNK_CHECKPOINT",
        True,
    )

    resume_enabled = cfg_bool(
        config,
        "RESUME_ENABLED",
        True,
    )

    skip_completed = cfg_bool(
        config,
        "SKIP_COMPLETED_SCENES",
        True,
    )

    save_each = cfg_bool(
        config,
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
        True,
    )

    failure_policy = cfg_text(
        config,
        "FAILURE_POLICY",
        "retry_then_checkpoint",
    ).lower()

    if failure_policy not in {
        "retry_then_checkpoint",
        "checkpoint",
        "retry",
        "skip",
        "stop",
    }:
        raise RuntimeError(
            f"Unsupported FAILURE_POLICY: {failure_policy}"
        )

    available = []

    for provider in order:
        ok, reason = provider_available(
            provider,
            config,
            allow_paid,
        )

        if ok:
            available.append(provider)
        else:
            log(f"PROVIDER SKIP: {provider}: {reason}")

    if not available:
        raise RuntimeError(
            "None of the configured TTS providers is available."
        )

    log("FORMAT             :", fmt)
    log("STORY TITLE        :", title)
    log("OUTPUT SLUG        :", slug)
    log("PARTS              :", parts)
    log("SCENES/PART        :", scenes_per_part)
    log("MAIN RECORDS       :", len(main_rows))
    log("EXTRA RECORDS      :", len(extra_rows))
    log("TOTAL TTS RECORDS  :", len(records))
    log("CONFIGURED ORDER   :", " -> ".join(order))
    log("AVAILABLE ORDER    :", " -> ".join(available))
    log("BILLABLE ALLOWED   :", allow_paid)
    log("VOICE              :", voice)
    log("SPEED              :", speed)
    log("RESUME             :", resume_enabled)
    log("CHUNK CHECKPOINT   :", chunk_checkpoint)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    old_jobs = manifest_map(manifest)

    generation_signature = stable_hash({
        "version": VERSION,
        "title": title,
        "slug": slug,
        "format": fmt,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "voice": voice,
        "speed": speed,
        "provider_order": order,
        "allow_paid": allow_paid,
        "max_chars": max_chars,
        "min_chars": min_chars,
        "narration_signature": narration.get("source_signature"),
        "scene_fingerprints": [
            stable_hash({
                "scene_id": record_identity(row)[0],
                "text": narration_text(row),
            })
            for row in records
        ],
    })

    if manifest.get("generation_signature") != generation_signature:
        log(
            "TTS settings or narration changed; "
            "previous complete-record reuse disabled."
        )
        jobs: dict[str, dict] = {}
    else:
        jobs = old_jobs

    manifest.update({
        "generation_signature": generation_signature,
        "narration_signature": narration.get("source_signature"),
        "title": title,
        "story_slug": slug,
        "format": fmt,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "main_record_count": len(main_rows),
        "extra_record_count": len(extra_rows),
        "provider_order": order,
        "available_providers": available,
        "billable_allowed": allow_paid,
        "voice": voice,
        "speed": speed,
        "output_directory": OUTPUT_DIR.relative_to(ROOT).as_posix(),
        "started_at": manifest.get("started_at") or utc_now(),
    })

    save_manifest(manifest, jobs, "in_progress")

    save_json_atomic(CHECKPOINT_FILE, {
        "status": "in_progress",
        "script_version": VERSION,
        "generation_signature": generation_signature,
        "completed_records": sum(
            item.get("status") == "completed"
            for item in jobs.values()
        ),
        "expected_records": len(records),
        "updated_at": utc_now(),
    })

    generated = 0
    reused = 0
    failures = []

    try:
        for index, row in enumerate(records, 1):
            scene_id, part, role, scene = record_identity(row)

            settings = {
                "format": fmt,
                "voice": voice,
                "speed": speed,
                "audience": row.get("audience", "adult"),
                "max_chars": max_chars,
                "min_chars": min_chars,
                "role": role,
                "title": title,
                "slug": slug,
            }

            fingerprint = scene_fingerprint(
                row,
                settings,
                order,
            )

            old_job = (
                jobs.get(scene_id)
                if resume_enabled and skip_completed
                else None
            )

            job, was_reused = synthesize_record(
                row=row,
                config=config,
                available=available,
                voice=voice,
                speed=speed,
                max_chars=max_chars,
                min_chars=min_chars,
                max_attempts=max_attempts,
                chunk_checkpoint=chunk_checkpoint,
                old_job=old_job,
                fingerprint=fingerprint,
                slug=slug,
            )

            jobs[scene_id] = job

            if was_reused:
                reused += 1
            elif job["status"] == "completed":
                generated += 1
            else:
                failures.append(scene_id)

            save_manifest(
                manifest,
                jobs,
                "incomplete" if failures else "in_progress",
            )

            if save_each or job["status"] == "failed":
                update_narration_audio(narration, jobs)

                save_json_atomic(CHECKPOINT_FILE, {
                    "status": (
                        "incomplete" if failures else "in_progress"
                    ),
                    "script_version": VERSION,
                    "generation_signature": generation_signature,
                    "completed_records": sum(
                        item.get("status") == "completed"
                        for item in jobs.values()
                    ),
                    "expected_records": len(records),
                    "last_scene_id": scene_id,
                    "updated_at": utc_now(),
                })

            if job["status"] == "failed":
                log(f"FAILED {scene_id} ({role})")

                if failure_policy != "skip":
                    raise RuntimeError(
                        f"TTS failed for {scene_id}; checkpoint saved. "
                        "Previously verified audio remains reusable."
                    )

            else:
                log(
                    f"[{index}/{len(records)}] COMPLETE {scene_id} "
                    f"| role={role} "
                    f"| provider={job['provider']} "
                    f"| measured={job['duration']:.3f}s"
                )

        missing = []

        for row in records:
            scene_id = record_identity(row)[0]
            job = jobs.get(scene_id)
            expected_path = output_path_for(row, slug)

            if not reusable_audio(
                job,
                expected_path,
                job.get("fingerprint", "") if job else "",
            ):
                missing.append(scene_id)

        if missing:
            save_manifest(manifest, jobs, "incomplete")
            update_narration_audio(narration, jobs)

            raise RuntimeError(
                f"TTS incomplete: {len(missing)} records missing "
                f"or invalid: {missing[:30]}"
            )

        update_narration_audio(narration, jobs)

        # Verify every narration record now points to its actual audio.
        for row in records:
            scene_id = record_identity(row)[0]
            job = jobs[scene_id]
            audio_path = safe_relative_path(
                job["output"],
                f"{scene_id} final audio",
            )
            info = validate_audio(audio_path)

            if (
                info["sha256"] != job["sha256"]
                or info["duration"] != job["duration"]
            ):
                raise RuntimeError(
                    f"{scene_id}: final audio verification failed."
                )

        manifest.update({
            "total_records": len(records),
            "main_records": len(main_rows),
            "extra_records": len(extra_rows),
            "completed_count": len(records),
            "failed_count": 0,
            "missing_records": [],
            "status": "completed",
            "generated_count": generated,
            "reused_count": reused,
            "completed_at": utc_now(),
        })

        save_manifest(manifest, jobs, "completed")

        save_json_atomic(CHECKPOINT_FILE, {
            "status": "completed",
            "script_version": VERSION,
            "generation_signature": generation_signature,
            "completed_records": len(records),
            "expected_records": len(records),
            "updated_at": utc_now(),
        })

        saved_manifest = load_json(MANIFEST_FILE)

        if (
            not isinstance(saved_manifest, dict)
            or saved_manifest.get("status") != "completed"
            or saved_manifest.get("completed_count") != len(records)
            or len(saved_manifest.get("jobs", [])) != len(records)
        ):
            raise RuntimeError(
                "Final TTS manifest read-after-write validation failed."
            )

        saved_narration = load_json(NARRATION_FILE)

        if saved_narration.get("tts_status") != "completed":
            raise RuntimeError(
                "Narration file did not record completed TTS status."
            )

    except Exception as exc:
        save_manifest(manifest, jobs, "incomplete")

        update_narration_audio(narration, jobs)

        save_json_atomic(CHECKPOINT_FILE, {
            "status": "incomplete",
            "script_version": VERSION,
            "generation_signature": generation_signature,
            "completed_records": sum(
                item.get("status") == "completed"
                for item in jobs.values()
            ),
            "expected_records": len(records),
            "last_error": str(exc)[:2000],
            "updated_at": utc_now(),
        })

        raise

    log("=" * 64)
    log("TTS COMPLETE")
    log("Story             :", title)
    log("Format            :", fmt)
    log("Parts             :", parts)
    log("Main records      :", len(main_rows))
    log("Hook/extra records:", len(extra_rows))
    log("Total records     :", len(records))
    log("Newly generated   :", generated)
    log("Reused records    :", reused)
    log("Provider order    :", " -> ".join(order))
    log("Audio durations   : ffprobe measured")
    log("Manifest          :", MANIFEST_FILE.relative_to(ROOT))
    log("Checkpoint        :", CHECKPOINT_FILE.relative_to(ROOT))
    log("=" * 64)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("TTS INTERRUPTED; verified checkpoints retained.")
        raise SystemExit(130)
    except Exception as exc:
        log("TTS FAILED:", exc)
        raise SystemExit(1)