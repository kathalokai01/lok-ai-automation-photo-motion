#!/usr/bin/env python3
"""Resumable scene-wise TTS with dynamic SHORT parts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_parts, get_scenes

OUT = ROOT / "output" / "narration"
AUDIO_DIR = OUT / "audio"
CHUNK_DIR = AUDIO_DIR / "_chunks"
NARRATION_FILE = OUT / "narration.json"
MANIFEST_FILE = OUT / "audio_jobs.json"

VERSION = "generate-tts-v15-dynamic-short-parts"

PROVIDERS = (
    "fish_audio",
    "gemini_tts",
    "edge_tts",
    "piper",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
)

BILLABLE = {
    "fish_audio",
    "gemini_tts",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


def sha_text(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def digest(value):
    return sha_text(
        value if isinstance(value, str) else canonical_json(value)
    )


def file_digest(path):
    hasher = hashlib.sha256()

    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)

    return hasher.hexdigest()


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def secret(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def run(command, input_text=None, timeout=240):
    result = subprocess.run(
        [str(item) for item in command],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode:
        message = result.stderr or result.stdout or "Command failed"
        raise RuntimeError(message[-1500:])

    return result


def audio_info(path, config=None):
    """Verify a real audio stream and measure its duration."""

    path = Path(path)

    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"Audio missing or too small: {path}")

    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    result = run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of", "json",
            str(path),
        ],
        timeout=60,
    )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams", [])

    except (
        ValueError, TypeError, KeyError, json.JSONDecodeError
    ) as exc:
        raise RuntimeError(
            f"Cannot read audio metadata: {path.name}"
        ) from exc

    has_audio = any(
        item.get("codec_type") == "audio"
        for item in streams
        if isinstance(item, dict)
    )

    if not has_audio or duration <= 0:
        raise RuntimeError(f"Invalid audio stream: {path.name}")

    return duration


def normalize_audio(source, destination, config):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(
        destination.stem + ".tmp.mp3"
    )

    try:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(source),
                "-vn",
                "-ac", "1",
                "-ar", "24000",
                "-codec:a", "libmp3lame",
                "-b:a", "96k",
                str(temporary),
            ],
            timeout=300,
        )

        audio_info(temporary, config)
        os.replace(temporary, destination)

    finally:
        temporary.unlink(missing_ok=True)

    return audio_info(destination, config)


def write_bytes(data, path):
    if not isinstance(data, bytes) or len(data) < 100:
        raise RuntimeError("Provider returned unusable audio")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def post_json(url, headers, payload, timeout=150):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code}: {detail[:500]}"
        ) from exc

    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Request failed: {exc}") from exc

    if len(data) < 100:
        raise RuntimeError("Empty provider response")

    return data


def decode_audio(encoded, path, encoding="base64"):
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("Provider audio field is missing")

    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]

    try:
        if encoding == "hex":
            data = bytes.fromhex(encoded)
        else:
            data = base64.b64decode(encoded, validate=True)

    except (ValueError, TypeError) as exc:
        raise RuntimeError("Audio decoding failed") from exc

    write_bytes(data, path)


def wrap_pcm(raw, path, sample_rate=24000):
    if len(raw) < 100 or len(raw) % 2:
        raise RuntimeError("Invalid PCM audio")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(raw)


def fish_audio(text, path, config):
    key = secret("FISH_API_KEY", "FISH_AUDIO_API_KEY")

    if not key:
        raise RuntimeError("Fish Audio API key missing")

    payload = {
        "text": text,
        "format": "mp3",
    }

    reference = secret("FISH_REFERENCE_ID", "FISH_AUDIO_REFERENCE_ID")

    if reference:
        payload["reference_id"] = reference

    request = urllib.request.Request(
        secret("FISH_TTS_URL") or "https://api.fish.audio/v1/tts",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "model": secret("FISH_TTS_MODEL") or "s2.1",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            write_bytes(response.read(), path)

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Fish Audio HTTP {exc.code}: {detail[:350]}"
        ) from exc


def gemini_tts(text, path, config):
    key = secret("GEMINI_TTS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    model = secret("GEMINI_TTS_MODEL")

    if not key or not model:
        raise RuntimeError("Gemini TTS key or model missing")

    voice = secret("GEMINI_TTS_VOICE") or "Kore"

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="")
        + ":generateContent?key="
        + urllib.parse.quote(key, safe="")
    )

    payload = {
        "contents": [{
            "parts": [{
                "text": (
                    "Read this narration aloud in Hindi. "
                    "Do not translate, summarize, or add words:\n"
                    + text
                )
            }]
        }],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice
                    }
                }
            },
        },
    }

    response = post_json(url, {}, payload)

    try:
        data = json.loads(response.decode("utf-8"))

        inline = next(
            item["inlineData"]
            for item in data["candidates"][0]["content"]["parts"]
            if isinstance(item, dict)
            and item.get("inlineData", {}).get("data")
        )

        raw = base64.b64decode(inline["data"], validate=True)
        mime = str(
            inline.get("mimeType", "audio/L16;rate=24000")
        ).lower()

    except (
        UnicodeDecodeError, json.JSONDecodeError, KeyError,
        IndexError, StopIteration, TypeError, ValueError
    ) as exc:
        raise RuntimeError("Gemini returned no valid audio") from exc

    if mime.startswith("audio/l16") or "pcm" in mime:
        match = re.search(r"rate\s*=\s*(\d+)", mime)
        wrap_pcm(raw, path, int(match.group(1)) if match else 24000)

    else:
        write_bytes(raw, path)


def edge_tts(text, path, config):
    if not shutil.which("edge-tts"):
        raise RuntimeError("edge-tts is not installed")

    voice = str(
        config.get("TTS_EDGE_VOICE", config.get("VOICE", "male"))
    ).strip()

    if voice.lower() in {"male", "man", "m"}:
        voice = "hi-IN-MadhurNeural"
    elif voice.lower() in {"female", "woman", "f"}:
        voice = "hi-IN-SwaraNeural"

    rate = str(config.get("TTS_EDGE_RATE", config.get("SPEED", "+0%")))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".edge.tmp.mp3")

    try:
        run(
            [
                "edge-tts",
                "--voice", voice,
                "--rate", rate,
                "--text", text,
                "--write-media", str(temporary),
            ],
            timeout=240,
        )

        audio_info(temporary, config)
        shutil.copyfile(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def piper(text, path, config):
    executable = shutil.which("piper")
    model = secret("PIPER_MODEL_PATH") or str(
        config.get("PIPER_MODEL_PATH", "")
    ).strip()

    if not executable or not model or not Path(model).is_file():
        raise RuntimeError("Piper executable or model unavailable")

    command = [executable, "--model", model, "--output_file", str(path)]
    speaker = secret("PIPER_SPEAKER")

    if speaker:
        command.extend(["--speaker", speaker])

    run(command, input_text=text, timeout=300)
    audio_info(path, config)


def elevenlabs(text, path, config):
    key = secret("ELEVENLABS_API_KEY")
    voice = secret("ELEVENLABS_VOICE_ID")

    if not key or not voice:
        raise RuntimeError("ElevenLabs key or voice ID missing")

    model = secret("ELEVENLABS_TTS_MODEL") or "eleven_multilingual_v2"

    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + urllib.parse.quote(voice, safe="")
        + "?output_format=mp3_44100_128"
    )

    write_bytes(
        post_json(
            url,
            {"xi-api-key": key},
            {"text": text, "model_id": model},
        ),
        path,
    )


def cartesia(text, path, config):
    key = secret("CARTESIA_API_KEY")
    voice = secret("CARTESIA_VOICE_ID")

    if not key or not voice:
        raise RuntimeError("Cartesia key or voice ID missing")

    payload = {
        "model_id": secret("CARTESIA_TTS_MODEL") or "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice},
        "output_format": {
            "container": "mp3",
            "sample_rate": 24000,
            "encoding": "mp3",
        },
    }

    write_bytes(
        post_json(
            secret("CARTESIA_TTS_URL")
            or "https://api.cartesia.ai/tts/bytes",
            {
                "Authorization": f"Bearer {key}",
                "Cartesia-Version": (
                    secret("CARTESIA_API_VERSION") or "2025-04-16"
                ),
            },
            payload,
        ),
        path,
    )


def finevoice(text, path, config):
    key = secret("FINEVOICE_API_KEY")
    voice = secret("FINEVOICE_VOICE_ID")

    if not key or not voice:
        raise RuntimeError("FineVoice key or voice ID missing")

    data = post_json(
        secret("FINEVOICE_TTS_URL")
        or "https://api.finevoice.ai/v1/text_to_speech",
        {"Authorization": f"Bearer {key}"},
        {"text": text, "voice_id": voice, "format": "mp3"},
    )

    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        write_bytes(data, path)
        return

    decode_audio(result.get("audio") or result.get("audio_base64"), path)


def minimax(text, path, config):
    key = secret("MINIMAX_API_KEY")

    if not key:
        raise RuntimeError("MiniMax API key missing")

    payload = {
        "model": secret("MINIMAX_TTS_MODEL") or "speech-2.8-turbo",
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": (
                secret("MINIMAX_TTS_VOICE_ID")
                or "English_expressive_narrator"
            ),
            "speed": 1,
            "vol": 1,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
        "output_format": "hex",
    }

    response = post_json(
        secret("MINIMAX_TTS_URL") or "https://api.minimax.io/v1/t2a_v2",
        {"Authorization": f"Bearer {key}"},
        payload,
    )

    try:
        data = json.loads(response.decode("utf-8"))
        status = data.get("base_resp", {}).get("status_code", 0)

        if status != 0:
            raise RuntimeError(f"MiniMax rejected request: {status}")

        decode_audio(data["data"]["audio"], path, "hex")

    except (
        UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError
    ) as exc:
        raise RuntimeError("MiniMax returned invalid audio") from exc


GENERATORS = {
    "fish_audio": fish_audio,
    "gemini_tts": gemini_tts,
    "edge_tts": edge_tts,
    "piper": piper,
    "elevenlabs": elevenlabs,
    "cartesia": cartesia,
    "finevoice": finevoice,
    "minimax": minimax,
}


def normalize_provider(value):
    raw = str(value or "").strip().lower().replace("-", "_")

    aliases = {
        "fish": "fish_audio",
        "fish audio": "fish_audio",
        "gemini": "gemini_tts",
        "gemini tts": "gemini_tts",
        "edge": "edge_tts",
        "edge tts": "edge_tts",
        "piper tts": "piper",
        "eleven labs": "elevenlabs",
        "fine voice": "finevoice",
        "mini max": "minimax",
    }

    return aliases.get(raw, raw)


def provider_groups(config):
    defaults = ("fish_audio", "gemini_tts", "edge_tts")

    primary = [
        normalize_provider(
            config.get(f"TTS_MAIN_{index}", defaults[index - 1])
        )
        for index in (1, 2, 3)
    ]

    if len(set(primary)) != 3:
        raise RuntimeError("TTS_MAIN_1/2/3 must be three different providers")

    if any(provider not in PROVIDERS for provider in primary):
        raise RuntimeError("Unsupported provider in TTS_MAIN_1/2/3")

    fallback = []

    if truthy(config.get("TTS_FALLBACK_ENABLED", True)):
        raw = config.get("TTS_FALLBACK_ORDER", "")
        values = (
            raw if isinstance(raw, (list, tuple))
            else re.split(r"[,;]", str(raw))
        )

        for item in values:
            provider = normalize_provider(item)

            if not provider:
                continue

            if provider not in PROVIDERS:
                raise RuntimeError(f"Unsupported fallback provider: {item}")

            if provider not in primary and provider not in fallback:
                fallback.append(provider)

    return primary, fallback


def paid_allowed(config):
    """Billable providers are permitted unless explicitly disabled."""

    value = os.environ.get("ALLOW_BILLABLE_PROVIDERS")

    if value is None:
        value = os.environ.get("TTS_ALLOW_PAID")

    if value is None:
        value = config.get("TTS_ALLOW_PAID", True)

    return truthy(value)


def provider_settings_fingerprint(config):
    environment = {}

    prefixes = (
        "FISH_",
        "GEMINI_TTS_",
        "ELEVENLABS_",
        "CARTESIA_",
        "FINEVOICE_",
        "MINIMAX_",
        "PIPER_",
        "TTS_",
    )

    for key, value in os.environ.items():
        if key.startswith(prefixes):
            environment[key] = value

    config_values = {
        key: value
        for key, value in config.items()
        if key.startswith(("TTS_", "PIPER_"))
    }

    return digest({
        "environment": environment,
        "input_config": config_values,
    })


def split_text(text, limit, minimum):
    text = " ".join(str(text).split())

    if limit < 100:
        raise RuntimeError("TTS_CHUNK_MAX_CHARS must be at least 100")

    if minimum < 1 or minimum > limit:
        raise RuntimeError(
            "TTS_CHUNK_MIN_CHARS must be between 1 and TTS_CHUNK_MAX_CHARS"
        )

    chunks = []

    while len(text) > limit:
        candidates = [
            text.rfind(mark, 0, limit)
            for mark in ("।", "!", "?", ".", " ")
        ]

        boundary = max(candidates)

        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1

        chunks.append(text[:boundary].strip())
        text = text[boundary:].strip()

    if text:
        chunks.append(text)

