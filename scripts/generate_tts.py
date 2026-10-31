#!/usr/bin/env python3
"""Resumable scene-wise TTS with strict audio validation and checkpoints."""

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

VERSION = "generate-tts-v14-strict-audio-checkpoint"

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
    with Path(path).open(
        "r", encoding="utf-8-sig"
    ) as stream:
        return json.load(stream)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                indent=2,
            )
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
    """Always verify a real audio stream and measure its duration."""

    path = Path(path)

    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(
            f"Audio missing or too small: {path}"
        )

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
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
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
        raise RuntimeError(
            f"Invalid audio stream: {path.name}"
        )

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
        raise RuntimeError(
            "Provider returned unusable audio"
        )

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
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            data = response.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"HTTP {exc.code}: {detail[:500]}"
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:
        raise RuntimeError(
            f"Request failed: {exc}"
        ) from exc

    if len(data) < 100:
        raise RuntimeError("Empty provider response")

    return data


def decode_audio(encoded, path, encoding="base64"):
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError(
            "Provider audio field is missing"
        )

    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]

    try:
        if encoding == "hex":
            data = bytes.fromhex(encoded)
        else:
            data = base64.b64decode(
                encoded,
                validate=True,
            )

    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "Audio decoding failed"
        ) from exc

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
    key = secret(
        "FISH_API_KEY",
        "FISH_AUDIO_API_KEY",
    )

    if not key:
        raise RuntimeError("Fish Audio API key missing")

    payload = {
        "text": text,
        "format": "mp3",
    }

    reference = secret(
        "FISH_REFERENCE_ID",
        "FISH_AUDIO_REFERENCE_ID",
    )

    if reference:
        payload["reference_id"] = reference

    request = urllib.request.Request(
        secret("FISH_TTS_URL")
        or "https://api.fish.audio/v1/tts",
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "model": secret("FISH_TTS_MODEL") or "s2.1",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=150,
        ) as response:
            write_bytes(response.read(), path)

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"Fish Audio HTTP {exc.code}: {detail[:350]}"
        ) from exc


def gemini_tts(text, path, config):
    key = secret(
        "GEMINI_TTS_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    )

    model = secret("GEMINI_TTS_MODEL")

    if not key or not model:
        raise RuntimeError(
            "Gemini TTS key or model missing"
        )

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

        raw = base64.b64decode(
            inline["data"],
            validate=True,
        )

        mime = str(
            inline.get(
                "mimeType",
                "audio/L16;rate=24000",
            )
        ).lower()

    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        StopIteration,
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeError(
            "Gemini returned no valid audio"
        ) from exc

    if mime.startswith("audio/l16") or "pcm" in mime:
        match = re.search(
            r"rate\s*=\s*(\d+)",
            mime,
        )

        wrap_pcm(
            raw,
            path,
            int(match.group(1)) if match else 24000,
        )

    else:
        write_bytes(raw, path)


def edge_tts(text, path, config):
    if not shutil.which("edge-tts"):
        raise RuntimeError(
            "edge-tts is not installed"
        )

    voice = str(
        config.get(
            "TTS_EDGE_VOICE",
            config.get("VOICE", "male"),
        )
    ).strip()

    if voice.lower() in {"male", "man", "m"}:
        voice = "hi-IN-MadhurNeural"

    elif voice.lower() in {"female", "woman", "f"}:
        voice = "hi-IN-SwaraNeural"

    rate = str(
        config.get(
            "TTS_EDGE_RATE",
            config.get("SPEED", "+0%"),
        )
    )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(
        path.stem + ".edge.tmp.mp3"
    )

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

    if (
        not executable
        or not model
        or not Path(model).is_file()
    ):
        raise RuntimeError(
            "Piper executable or model unavailable"
        )

    command = [
        executable,
        "--model", model,
        "--output_file", str(path),
    ]

    speaker = secret("PIPER_SPEAKER")

    if speaker:
        command.extend([
            "--speaker",
            speaker,
        ])

    run(
        command,
        input_text=text,
        timeout=300,
    )

    audio_info(path, config)


def elevenlabs(text, path, config):
    key = secret("ELEVENLABS_API_KEY")
    voice = secret("ELEVENLABS_VOICE_ID")

    if not key or not voice:
        raise RuntimeError(
            "ElevenLabs key or voice ID missing"
        )

    model = (
        secret("ELEVENLABS_TTS_MODEL")
        or "eleven_multilingual_v2"
    )

    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + urllib.parse.quote(voice, safe="")
        + "?output_format=mp3_44100_128"
    )

    write_bytes(
        post_json(
            url,
            {"xi-api-key": key},
            {
                "text": text,
                "model_id": model,
            },
        ),
        path,
    )


def cartesia(text, path, config):
    key = secret("CARTESIA_API_KEY")
    voice = secret("CARTESIA_VOICE_ID")

    if not key or not voice:
        raise RuntimeError(
            "Cartesia key or voice ID missing"
        )

    payload = {
        "model_id": (
            secret("CARTESIA_TTS_MODEL")
            or "sonic-2"
        ),
        "transcript": text,
        "voice": {
            "mode": "id",
            "id": voice,
        },
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
                    secret("CARTESIA_API_VERSION")
                    or "2025-04-16"
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
        raise RuntimeError(
            "FineVoice key or voice ID missing"
        )

    data = post_json(
        secret("FINEVOICE_TTS_URL")
        or "https://api.finevoice.ai/v1/text_to_speech",
        {
            "Authorization": f"Bearer {key}"
        },
        {
            "text": text,
            "voice_id": voice,
            "format": "mp3",
        },
    )

    try:
        result = json.loads(data.decode("utf-8"))

    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        write_bytes(data, path)
        return

    decode_audio(
        result.get("audio")
        or result.get("audio_base64"),
        path,
    )


def minimax(text, path, config):
    key = secret("MINIMAX_API_KEY")

    if not key:
        raise RuntimeError("MiniMax API key missing")

    payload = {
        "model": (
            secret("MINIMAX_TTS_MODEL")
            or "speech-2.8-turbo"
        ),
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
        secret("MINIMAX_TTS_URL")
        or "https://api.minimax.io/v1/t2a_v2",
        {
            "Authorization": f"Bearer {key}"
        },
        payload,
    )

    try:
        data = json.loads(response.decode("utf-8"))

        status = data.get(
            "base_resp", {}
        ).get("status_code", 0)

        if status != 0:
            raise RuntimeError(
                f"MiniMax rejected request: {status}"
            )

        decode_audio(
            data["data"]["audio"],
            path,
            "hex",
        )

    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise RuntimeError(
            "MiniMax returned invalid audio"
        ) from exc


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
    raw = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
    )

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
    defaults = (
        "fish_audio",
        "gemini_tts",
        "edge_tts",
    )

    primary = [
        normalize_provider(
            config.get(
                f"TTS_MAIN_{index}",
                defaults[index - 1],
            )
        )
        for index in (1, 2, 3)
    ]

    if len(set(primary)) != 3:
        raise RuntimeError(
            "TTS_MAIN_1/2/3 must be three different providers"
        )

    if any(provider not in PROVIDERS for provider in primary):
        raise RuntimeError(
            "Unsupported provider in TTS_MAIN_1/2/3"
        )

    fallback = []

    if truthy(
        config.get("TTS_FALLBACK_ENABLED", True)
    ):
        raw = config.get(
            "TTS_FALLBACK_ORDER",
            "",
        )

        values = (
            raw
            if isinstance(raw, (list, tuple))
            else re.split(r"[,;]", str(raw))
        )

        for item in values:
            provider = normalize_provider(item)

            if not provider:
                continue

            if provider not in PROVIDERS:
                raise RuntimeError(
                    f"Unsupported fallback provider: {item}"
                )

            if (
                provider not in primary
                and provider not in fallback
            ):
                fallback.append(provider)

    return primary, fallback


def paid_allowed(config):
    return (
        truthy(
            os.environ.get(
                "ALLOW_BILLABLE_PROVIDERS",
                "false",
            )
        )
        or (
            truthy(
                config.get("TTS_ALLOW_PAID", False)
            )
            and truthy(
                os.environ.get("TTS_ALLOW_PAID", "false")
            )
        )
    )


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
        raise RuntimeError(
            "TTS_CHUNK_MAX_CHARS must be at least 100"
        )

    if minimum < 1 or minimum > limit:
        raise RuntimeError(
            "TTS_CHUNK_MIN_CHARS must be between "
            "1 and TTS_CHUNK_MAX_CHARS"
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

        chunks.append(
            text[:boundary].strip()
        )

        text = text[boundary:].strip()

    if text:
        chunks.append(text)

    if (
        len(chunks) > 1
        and len(chunks[-1]) < minimum
    ):
        combined = (
            chunks[-2] + " " + chunks[-1]
        )

        if len(combined) <= limit:
            chunks[-2] = combined
            chunks.pop()

    index = 0

    while index < len(chunks) - 1:
        if len(chunks[index]) < minimum:
            combined = (
                chunks[index] + " " + chunks[index + 1]
            )

            if len(combined) <= limit:
                chunks[index] = combined
                chunks.pop(index + 1)
                continue

        index += 1

    return chunks


def scene_key(row):
    try:
        part = int(row["part"])
        scene = int(
            row.get("scene", row.get("local_scene"))
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeError(
            f"Invalid narration scene mapping: {row}"
        ) from exc

    if part < 1 or scene < 1:
        raise RuntimeError(
            f"Invalid narration scene mapping: {row}"
        )

    return part, scene


def rel(path):
    return (
        Path(path)
        .resolve()
        .relative_to(ROOT.resolve())
        .as_posix()
    )


def final_audio_path(part, scene):
    return (
        AUDIO_DIR
        / f"part_{part:02d}"
        / f"scene_{scene:02d}.mp3"
    )


def chunk_audio_path(part, scene, index):
    return (
        CHUNK_DIR
        / f"part_{part:02d}"
        / f"scene_{scene:02d}"
        / f"chunk_{index:04d}.mp3"
    )


def load_checkpoint():
    try:
        data = read_json(MANIFEST_FILE)

        if (
            isinstance(data, dict)
            and isinstance(data.get("jobs"), list)
        ):
            return data

    except (
        OSError,
        ValueError,
        TypeError,
    ):
        pass

    return {"jobs": []}


def save_checkpoint(checkpoint, old_jobs, expected, status):
    checkpoint.update({
        "version": VERSION,
        "status": status,
        "expected_scenes": len(expected),
        "jobs": [
            old_jobs.get(
                key,
                {
                    "part": key[0],
                    "scene": key[1],
                    "status": "pending",
                },
            )
            for key in sorted(expected)
        ],
        "updated_at": now(),
    })

    save_json(MANIFEST_FILE, checkpoint)


def generate_one_chunk(
    text,
    destination,
    config,
    primary,
    fallback,
    allow_paid,
    attempts,
):
    errors = []

    raw = Path(destination).with_suffix(".raw")
    raw.parent.mkdir(parents=True, exist_ok=True)

    provider_groups_to_try = (
        ("primary", primary),
        ("fallback", fallback),
    )

    for group, providers in provider_groups_to_try:
        for provider in providers:

            if provider in BILLABLE and not allow_paid:
                errors.append(
                    f"{provider}: billable permission disabled"
                )
                continue

            for attempt in range(1, attempts + 1):
                raw.unlink(missing_ok=True)

                try:
                    GENERATORS[provider](
                        text,
                        raw,
                        config,
                    )

                    audio_info(raw, config)

                    duration = normalize_audio(
                        raw,
                        destination,
                        config,
                    )

                    if duration <= 0:
                        raise RuntimeError(
                            "Normalized audio duration is invalid"
                        )

                    return (
                        provider,
                        group,
                        attempt,
                        duration,
                    )

                except Exception as exc:
                    errors.append(
                        f"{provider} attempt "
                        f"{attempt}/{attempts}: "
                        f"{str(exc)[:220]}"
                    )

                    raw.unlink(missing_ok=True)

            print(
                f"PROVIDER FAILED: {provider}; "
                "trying next configured provider",
                flush=True,
            )

    raise RuntimeError(
        "No TTS provider succeeded: "
        + " | ".join(errors[-15:])
    )


def validate_narration(config):
    if not NARRATION_FILE.is_file():
        raise RuntimeError(
            "narration.json is missing"
        )

    narration = read_json(NARRATION_FILE)

    if (
        not isinstance(narration, dict)
        or not isinstance(narration.get("scenes"), list)
    ):
        raise RuntimeError(
            "narration.json must contain a scenes list"
        )

    narration_status = str(
        narration.get("status", "completed")
    ).lower()

    if narration_status not in {
        "completed",
        "complete",
        "ready",
    }:
        raise RuntimeError(
            f"Narration manifest status is invalid: "
            f"{narration_status}"
        )

    rows = {}

    for row in narration["scenes"]:
        if not isinstance(row, dict):
            raise RuntimeError(
                "Invalid narration scene row"
            )

        key = scene_key(row)

        if key in rows:
            raise RuntimeError(
                f"Duplicate narration scene: {key}"
            )

        value = str(
            row.get(
                "narration",
                row.get("text", ""),
            )
        ).strip()

        if not value:
            raise RuntimeError(
                f"Empty narration text for scene {key}"
            )

        status = str(
            row.get("status", "completed")
        ).lower()

        if status not in {
            "completed",
            "complete",
            "ready",
        }:
            raise RuntimeError(
                f"Narration scene is not completed: {key}"
            )

        rows[key] = value

    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    if set(rows) != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
            f"missing={sorted(expected - set(rows))}; "
            f"extra={sorted(set(rows) - expected)}"
        )

    return rows, expected


def validate_chunk_record(record, index, text, settings_hash):
    if not isinstance(record, dict):
        return False

    if record.get("status") != "completed":
        return False

    if record.get("index") != index:
        return False

    if record.get("text") != text:
        return False

    if record.get("text_sha256") != sha_text(text):
        return False

    expected_signature = digest({
        "chunk_sha256": sha_text(text),
        "text": text,
        "settings_sha256": settings_hash,
        "index": index,
    })

    if record.get("signature") != expected_signature:
        return False

    output = Path(str(record.get("output", "")))

    if not output.is_absolute():
        output = ROOT / output

    if not output.is_file():
        return False

    try:
        if record.get("sha256") != file_digest(output):
            return False

        duration = audio_info(output)

        if duration <= 0:
            return False

    except Exception:
        return False

    return True


def validate_final_job(job, part, scene, signature, destination):
    if not isinstance(job, dict):
        return False

    if job.get("status") != "completed":
        return False

    if job.get("part") != part or job.get("scene") != scene:
        return False

    if job.get("signature") != signature:
        return False

    if not destination.is_file():
        return False

    try:
        if job.get("sha256") != file_digest(destination):
            return False

        duration = audio_info(destination)

        if duration <= 0:
            return False

    except Exception:
        return False

    chunks = job.get("chunks")

    if not isinstance(chunks, list) or not chunks:
        return False

    for index, record in enumerate(chunks, 1):
        if not isinstance(record, dict):
            return False

        output = Path(str(record.get("output", "")))

        if not output.is_absolute():
            output = ROOT / output

        if not output.is_file():
            return False

        try:
            if record.get("sha256") != file_digest(output):
                return False

            audio_info(output)

        except Exception:
            return False

    return True


def main():
    config = load_and_validate(
        ROOT / "Input" / "topic.txt"
    )

    if not isinstance(config, dict):
        raise RuntimeError(
            "Input configuration must be a dictionary"
        )

    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(
                f"Required command not installed: {command}"
            )

    rows, expected = validate_narration(config)

    primary, fallback = provider_groups(config)
    allow_paid = paid_allowed(config)

    max_chars = int(
        config.get("TTS_CHUNK_MAX_CHARS", 500)
    )

    min_chars = int(
        config.get("TTS_CHUNK_MIN_CHARS", 150)
    )

    attempts = int(
        config.get("TTS_MAX_PROVIDER_ATTEMPTS", 1)
    )

    checkpoint_chunks = truthy(
        config.get("TTS_CHUNK_CHECKPOINT", True)
    )

    if not 100 <= min_chars <= max_chars:
        raise RuntimeError(
            "TTS_CHUNK_MIN_CHARS must be >=100 "
            "and <= TTS_CHUNK_MAX_CHARS"
        )

    if not 100 <= max_chars <= 5000:
        raise RuntimeError(
            "TTS_CHUNK_MAX_CHARS must be between 100 and 5000"
        )

    if not 1 <= attempts <= 3:
        raise RuntimeError(
            "TTS_MAX_PROVIDER_ATTEMPTS must be between 1 and 3"
        )

    settings_hash = digest({
        "version": VERSION,
        "primary": primary,
        "fallback": fallback,
        "paid_allowed": allow_paid,
        "voice": config.get("VOICE", ""),
        "speed": config.get("SPEED", ""),
        "chunk_min_chars": min_chars,
        "chunk_max_chars": max_chars,
        "chunk_checkpoint": checkpoint_chunks,
        "attempts": attempts,
        "provider_settings": provider_settings_fingerprint(
            config
        ),
    })

    checkpoint = load_checkpoint()
    old_jobs = {}

    for job in checkpoint.get("jobs", []):
        if not isinstance(job, dict):
            continue

        try:
            key = (
                int(job["part"]),
                int(job["scene"]),
            )
            old_jobs[key] = job

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue

    AUDIO_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    CHUNK_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("TTS VERSION:", VERSION)
    print("EXPECTED SCENES:", len(expected))
    print("PRIMARY:", " -> ".join(primary))
    print("FALLBACK:", " -> ".join(fallback))
    print("BILLABLE ALLOWED:", allow_paid)
    print("CHUNK CHECKPOINT:", checkpoint_chunks)
    print("AUDIO VALIDATION: always enabled")

    for part, scene in sorted(expected):
        narration_text = rows[(part, scene)]
        text_hash = sha_text(narration_text)

        destination = final_audio_path(
            part,
            scene,
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        chunks = split_text(
            narration_text,
            max_chars,
            min_chars,
        )

        if not chunks:
            raise RuntimeError(
                f"No TTS chunks created for {part}/{scene}"
            )

        signature = digest({
            "text_sha256": text_hash,
            "settings_sha256": settings_hash,
            "chunk_count": len(chunks),
        })

        previous = old_jobs.get(
            (part, scene),
            {},
        )

        if validate_final_job(
            previous,
            part,
            scene,
            signature,
            destination,
        ):
            print(
                f"RESUME scene {part}/{scene}: "
                "verified cached audio",
                flush=True,
            )
            continue

        previous_chunks = previous.get(
            "chunks",
            [],
        )

        chunk_records = []
        chunk_paths = []
        provider_history = []

        for index, chunk in enumerate(chunks, 1):
            chunk_path = chunk_audio_path(
                part,
                scene,
                index,
            )

            chunk_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            old_chunk = (
                previous_chunks[index - 1]
                if (
                    isinstance(previous_chunks, list)
                    and len(previous_chunks) >= index
                )
                else None
            )

            if validate_chunk_record(
                old_chunk,
                index,
                chunk,
                settings_hash,
            ):
                record = dict(old_chunk)

                record["output"] = rel(chunk_path)

                chunk_records.append(record)
                chunk_paths.append(chunk_path)

                provider_history.append(
                    record.get("provider", "unknown")
                )

                print(
                    f"RESUME chunk {part}/{scene}/{index}",
                    flush=True,
                )

                continue

            temporary = chunk_path.with_name(
                chunk_path.stem + ".generating.mp3"
            )

            temporary.unlink(missing_ok=True)

            try:
                (
                    provider,
                    group,
                    used_attempt,
                    duration,
                ) = generate_one_chunk(
                    chunk,
                    temporary,
                    config,
                    primary,
                    fallback,
                    allow_paid,
                    attempts,
                )

                audio_info(temporary)

                os.replace(
                    temporary,
                    chunk_path,
                )

            finally:
                temporary.unlink(missing_ok=True)

            record = {
                "index": index,
                "status": "completed",
                "text": chunk,
                "text_sha256": sha_text(chunk),
                "signature": digest({
                    "chunk_sha256": sha_text(chunk),
                    "text": chunk,
                    "settings_sha256": settings_hash,
                    "index": index,
                }),
                "sha256": file_digest(chunk_path),
                "output": rel(chunk_path),
                "duration": round(duration, 4),
                "provider": provider,
                "provider_group": group,
                "provider_attempt": used_attempt,
            }

            chunk_records.append(record)
            chunk_paths.append(chunk_path)
            provider_history.append(provider)

            old_jobs[(part, scene)] = {
                "part": part,
                "scene": scene,
                "status": "in_progress",
                "signature": signature,
                "text_sha256": text_hash,
                "settings_sha256": settings_hash,
                "output": rel(destination),
                "chunks": list(chunk_records),
                "providers": list(provider_history),
                "updated_at": now(),
            }

            if checkpoint_chunks:
                save_checkpoint(
                    checkpoint,
                    old_jobs,
                    expected,
                    "in_progress",
                )

            print(
                f"CHUNK COMPLETED {part}/{scene}/{index}: "
                f"{provider}",
                flush=True,
            )

        if len(chunk_paths) != len(chunks):
            raise RuntimeError(
                f"Chunk count mismatch for {part}/{scene}"
            )

        temporary_final = destination.with_name(
            destination.stem + ".tmp.mp3"
        )

        temporary_final.unlink(missing_ok=True)

        try:
            if len(chunk_paths) == 1:
                shutil.copyfile(
                    chunk_paths[0],
                    temporary_final,
                )

            else:
                concat_file = destination.with_suffix(
                    ".concat.txt"
                )

                try:
                    lines = []

                    for path in chunk_paths:
                        resolved = path.resolve().as_posix()

                        if "\n" in resolved or "\r" in resolved:
                            raise RuntimeError(
                                "Invalid newline in audio path"
                            )

                        lines.append(
                            "file '"
                            + resolved.replace("'", "'\\''")
                            + "'"
                        )

                    concat_file.write_text(
                        "\n".join(lines) + "\n",
                        encoding="utf-8",
                    )

                    run(
                        [
                            "ffmpeg",
                            "-hide_banner",
                            "-loglevel", "error",
                            "-y",
                            "-f", "concat",
                            "-safe", "0",
                            "-i", str(concat_file),
                            "-vn",
                            "-ac", "1",
                            "-ar", "24000",
                            "-codec:a", "libmp3lame",
                            "-b:a", "96k",
                            str(temporary_final),
                        ],
                        timeout=600,
                    )

                finally:
                    concat_file.unlink(missing_ok=True)

            final_duration = audio_info(
                temporary_final
            )

            if final_duration <= 0:
                raise RuntimeError(
                    "Final narration duration is invalid"
                )

            os.replace(
                temporary_final,
                destination,
            )

        finally:
            temporary_final.unlink(missing_ok=True)

        final_duration = audio_info(destination)

        final_job = {
            "part": part,
            "scene": scene,
            "status": "completed",
            "signature": signature,
            "text_sha256": text_hash,
            "settings_sha256": settings_hash,
            "output": rel(destination),
            "sha256": file_digest(destination),
            "duration": round(final_duration, 4),
            "providers": provider_history,
            "chunks": chunk_records,
            "updated_at": now(),
        }

        old_jobs[(part, scene)] = final_job

        save_checkpoint(
            checkpoint,
            old_jobs,
            expected,
            "in_progress",
        )

        print(
            f"COMPLETED scene {part}/{scene}: "
            f"{final_duration:.2f}s",
            flush=True,
        )

    final_jobs = [
        old_jobs.get(key)
        for key in sorted(expected)
    ]

    if (
        len(final_jobs) != len(expected)
        or any(
            not isinstance(job, dict)
            or job.get("status") != "completed"
            for job in final_jobs
        )
    ):
        raise RuntimeError(
            "Not all expected narration scenes completed"
        )

    for job in final_jobs:
        part = int(job["part"])
        scene = int(job["scene"])
        destination = final_audio_path(part, scene)

        if not validate_final_job(
            job,
            part,
            scene,
            job.get("signature"),
            destination,
        ):
            raise RuntimeError(
                f"Final audio verification failed: {part}/{scene}"
            )

        chunks = job.get("chunks", [])

        if not isinstance(chunks, list) or not chunks:
            raise RuntimeError(
                f"Missing chunk records: {part}/{scene}"
            )

        expected_text_chunks = split_text(
            rows[(part, scene)],
            max_chars,
            min_chars,
        )

        if len(chunks) != len(expected_text_chunks):
            raise RuntimeError(
                f"Chunk count mismatch: {part}/{scene}"
            )

        for index, (record, expected_text) in enumerate(
            zip(chunks, expected_text_chunks),
            1,
        ):
            if not isinstance(record, dict):
                raise RuntimeError(
                    "Invalid TTS chunk record"
                )

            if record.get("index") != index:
                raise RuntimeError(
                    "TTS chunk order is not continuous"
                )

            if record.get("text") != expected_text:
                raise RuntimeError(
                    "TTS chunk text does not match narration"
                )

            if record.get("text_sha256") != sha_text(
                expected_text
            ):
                raise RuntimeError(
                    "TTS chunk text hash mismatch"
                )

    save_json(
        MANIFEST_FILE,
        {
            "version": VERSION,
            "status": "completed",
            "expected_scenes": len(final_jobs),
            "jobs": final_jobs,
            "primary_providers": primary,
            "fallback_providers": fallback,
            "paid_providers_allowed": allow_paid,
            "provider_attempt_limit": attempts,
            "chunk_min_chars": min_chars,
            "chunk_max_chars": max_chars,
            "chunk_checkpoint_enabled": checkpoint_chunks,
            "audio_validation_enabled": True,
            "provider_settings_sha256": (
                provider_settings_fingerprint(config)
            ),
            "updated_at": now(),
        },
    )

    print("TTS COMPLETE")
    print("Scenes:", len(final_jobs))
    print("Primary providers:", " -> ".join(primary))
    print("Fallback providers:", " -> ".join(fallback))
    print("Billable provider permission:", allow_paid)
    print("Provider attempts:", attempts)
    print("Chunk checkpoint:", checkpoint_chunks)
    print("Audio validation: enabled")
    print(
        "Manifest:",
        MANIFEST_FILE.relative_to(ROOT),
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "TTS INTERRUPTED",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(130)

    except Exception as exc:
        print(
            "TTS FAILED:",
            str(exc),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)