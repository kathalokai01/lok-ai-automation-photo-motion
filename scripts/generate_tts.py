#!/usr/bin/env python3
"""Role-aware, resumable, multi-provider TTS pipeline."""

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

from input_config import (
    load_input_config,
    get_parts,
    get_scenes,
    normalize_format,
    get_tts_provider_order,
)

ROOT = Path(".")
NARRATION_FILE = ROOT / "output/narration/narration.json"
OUTPUT_DIR = ROOT / "output/narration/audio"
MANIFEST_FILE = ROOT / "output/narration/audio_jobs.json"
CHECKPOINT_FILE = ROOT / "output/checkpoints/tts_progress.json"

VERSION = 9
MIN_AUDIO_BYTES = 1000
REQUEST_TIMEOUT = 180

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


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def log(*values):
    print(*values, flush=True)


def text_value(value, default=""):
    if value is None:
        return default
    return str(value).strip()


def cfg_text(config, key, default=""):
    return text_value(config.get(key), default)


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def cfg_int(config, key, default, minimum=0):
    try:
        return max(minimum, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def stable_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path, required=True):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc


def save_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def command_exists(name):
    return shutil.which(name) is not None


def run_command(command, timeout=REQUEST_TIMEOUT, input_data=None):
    result = subprocess.run(
        command,
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
            f"Command failed ({result.returncode}): {str(detail)[-1800:]}"
        )
    return result


def request_bytes(url, headers=None, payload=None, timeout=REQUEST_TIMEOUT):
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            if not data:
                raise RuntimeError("Provider returned an empty response.")
            return data, response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Provider HTTP {exc.code}: {detail[:1500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Provider network error: {exc}") from exc


def request_json(url, headers, payload):
    data, _ = request_bytes(url, headers, payload)
    try:
        result = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Provider returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Provider response is not a JSON object.")
    return result


def audio_duration(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"Audio missing or too small: {path}")
    if not command_exists("ffprobe"):
        raise RuntimeError("ffprobe is required to measure audio duration.")

    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ], timeout=60)

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid audio duration: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid measured audio duration: {path}")

    return round(duration, 3)


def validate_audio(path):
    path = Path(path)
    duration = audio_duration(path)
    return {
        "duration": duration,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def normalize_speed(value):
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


def resolve_voice(provider, voice):
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


def provider_order(config):
    order = get_tts_provider_order(config)
    if not order:
        raise RuntimeError("Configured TTS provider order is empty.")
    return list(dict.fromkeys(order))


def billable_allowed(config):
    allowed = cfg_bool(config, "ALLOW_BILLABLE_PROVIDERS", True)
    env_value = os.getenv("ALLOW_BILLABLE_PROVIDERS")
    if env_value is not None:
        allowed = allowed and env_value.strip().lower() in {
            "true", "1", "yes", "on"
        }
    return allowed and cfg_bool(config, "TTS_ALLOW_PAID", True)


def provider_available(provider, config, allow_paid):
    if provider in BILLABLE_PROVIDERS and not allow_paid:
        return False, "Billable provider permission is disabled."

    checks = {
        "fish_audio": bool(
            os.getenv("FISH_AUDIO_API_KEY") or os.getenv("FISH_API_KEY")
        ),
        "gemini_tts": bool(
            os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
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
            config, "PIPER_MODEL"
        )
        checks["piper"] = bool(
            model
            and Path(model).is_file()
            and (command_exists("piper") or command_exists("piper-tts"))
        )

    if provider not in checks:
        return False, f"Unsupported configured provider: {provider}"

    if not checks[provider]:
        return False, "Required API key, executable or model is unavailable."

    return True, "available"


def split_text(value, max_chars, min_chars):
    """Chunk text without losing content; prefer sentence boundaries."""
    source = str(value).strip()
    if not source:
        raise RuntimeError("Cannot synthesize empty narration.")

    max_chars = max(100, int(max_chars))
    min_chars = max(1, min(int(min_chars), max_chars))

    if len(source) <= max_chars:
        return [source]

    units = []
    for paragraph in source.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        start = 0
        for match in re.finditer(
            r"[.!?।॥]+(?:[\"'”’)\]]*)\s*",
            paragraph,
        ):
            unit = paragraph[start:match.end()].strip()
            if unit:
                units.append(unit)
            start = match.end()

        tail = paragraph[start:].strip()
        if tail:
            units.append(tail)

    if not units:
        units = [source]

    chunks = []
    current = ""

    for unit in units:
        while len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ""

            cut = unit.rfind(" ", 0, max_chars + 1)
            if cut < min_chars:
                cut = unit.rfind(",", 0, max_chars + 1)
            if cut < min_chars:
                cut = max_chars

            chunks.append(unit[:cut].strip())
            unit = unit[cut:].strip()

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

    if not merged or any(not chunk for chunk in merged):
        raise RuntimeError("TTS chunking produced an empty chunk.")

    return merged


def write_audio_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".partial" + path.suffix)

    try:
        temporary.write_bytes(data)
        if temporary.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError("Provider audio response is too small.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pcm_to_mp3(pcm, output_path, sample_rate=24000):
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
            "ffmpeg", "-y", "-v", "error",
            "-i", str(wav_path),
            "-codec:a", "libmp3lame",
            "-q:a", "3",
            str(output_path),
        ])
    finally:
        wav_path.unlink(missing_ok=True)

    validate_audio(output_path)


def concat_chunks(paths, output_path):
    if not paths:
        raise RuntimeError("No TTS chunks available.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(paths) == 1:
        shutil.copy2(paths[0], output_path)
        validate_audio(output_path)
        return

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
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-codec:a", "libmp3lame",
            "-q:a", "3",
            str(output_path),
        ])
    finally:
        list_file.unlink(missing_ok=True)

    validate_audio(output_path)


def synthesize_provider(provider, text, output_path, config, speed, voice):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "edge_tts":
        temporary = output_path.with_name(
            output_path.stem + ".edge.partial.mp3"
        )
        try:
            run_command([
                "edge-tts",
                "--voice", resolve_voice(provider, voice),
                "--rate", speed,
                "--text", text,
                "--write-media", str(temporary),
            ])
            validate_audio(temporary)
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
        return validate_audio(output_path)

    if provider == "piper":
        model = os.getenv("PIPER_MODEL") or cfg_text(
            config, "PIPER_MODEL"
        )
        executable = shutil.which("piper") or shutil.which("piper-tts")
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
                "ffmpeg", "-y", "-v", "error",
                "-i", str(wav_path),
                "-codec:a", "libmp3lame",
                "-q:a", "3",
                str(output_path),
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
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        model = os.getenv(
            "GEMINI_TTS_MODEL",
            "gemini-2.5-flash-preview-tts",
        )
        voice_name = resolve_voice(provider, voice)

        payload = {
            "contents": [{"parts": [{"text": text}]}],
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
            item = next(
                part for part in parts
                if isinstance(part, dict)
                and isinstance(part.get("inlineData"), dict)
            )
            pcm = base64.b64decode(
                item["inlineData"]["data"],
                validate=True,
            )
        except Exception as exc:
            raise RuntimeError(
                "Gemini TTS returned no valid audio."
            ) from exc

        pcm_to_mp3(pcm, output_path, 24000)
        return validate_audio(output_path)

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
            raise RuntimeError("MiniMax returned no usable audio.") from exc

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
            result = json.loads(data.decode("utf-8"))
            encoded = (
                result.get("audio")
                or result.get("audio_base64")
                or result.get("data")
            )
            if not encoded:
                raise RuntimeError("FineVoice JSON has no audio field.")
            data = base64.b64decode(encoded, validate=True)

        write_audio_bytes(output_path, data)
        return validate_audio(output_path)

    raise RuntimeError(f"Unsupported configured provider: {provider}")


def safe_id(value):
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(value or "").strip(),
    )
    return value.strip("_")[:100] or "scene"


def record_identity(row):
    part = int(row["part"])
    role = str(row.get("role", "main")).strip().lower()

    if role == "main":
        scene = int(row["scene"])
        scene_id = str(
            row.get("scene_id")
            or f"part_{part:02d}_scene_{scene:03d}"
        ).strip()
        return scene_id, part, role, scene

    if role not in {"hook", "suspense", "resolution"}:
        raise RuntimeError(f"Unsupported narration role: {role}")

    scene_id = str(
        row.get("scene_id") or f"part_{part:02d}_{role}"
    ).strip()

    if not scene_id:
        raise RuntimeError(f"Missing scene_id for Part {part} {role}.")

    return scene_id, part, role, None


def narration_text(row):
    value = row.get("text")
    if value is None:
        value = row.get("narration")
    value = str(value or "").strip()
    if not value:
        raise RuntimeError(
            f"Empty narration text for {row.get('scene_id', 'unknown')}."
        )
    return value


def output_path_for(row):
    scene_id, part, role, scene = record_identity(row)
    folder = OUTPUT_DIR / f"part_{part:02d}"

    if role == "main":
        return folder / f"scene_{scene:03d}.mp3"

    return folder / f"{role}_{safe_id(scene_id)}.mp3"


def chunk_path_for(row, chunk_index):
    scene_id, part, role, scene = record_identity(row)
    folder = OUTPUT_DIR / f"part_{part:02d}" / "chunks"

    stem = (
        f"scene_{scene:03d}"
        if role == "main"
        else f"{role}_{safe_id(scene_id)}"
    )

    return folder / f"{stem}_chunk_{chunk_index:03d}.mp3"


def scene_fingerprint(row, settings, order):
    return stable_hash({
        "version": VERSION,
        "scene_id": record_identity(row)[0],
        "part": row["part"],
        "role": row.get("role", "main"),
        "text": narration_text(row),
        "settings": settings,
        "provider_order": order,
    })


def load_manifest():
    data = load_json(MANIFEST_FILE, required=False)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("jobs"), list):
        data["jobs"] = []
    return data


def manifest_map(manifest):
    return {
        str(row["scene_id"]): dict(row)
        for row in manifest.get("jobs", [])
        if isinstance(row, dict) and row.get("scene_id")
    }


def save_manifest(manifest, jobs, status):
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
        "status": status,
        "jobs": ordered,
        "total_records": len(ordered),
        "completed_count": sum(
            row.get("status") == "completed" for row in ordered
        ),
        "failed_count": sum(
            row.get("status") == "failed" for row in ordered
        ),
        "updated_at": utc_now(),
    })

    save_json_atomic(MANIFEST_FILE, manifest)


def reusable_audio(job, path, fingerprint):
    if not isinstance(job, dict):
        return False
    if job.get("status") != "completed":
        return False
    if job.get("fingerprint") != fingerprint:
        return False

    try:
        actual = validate_audio(path)
        expected_duration = float(job.get("duration", 0))
    except Exception:
        return False

    return (
        job.get("sha256") == actual["sha256"]
        and expected_duration > 0
        and abs(expected_duration - actual["duration"]) < 0.15
    )


def update_narration_audio(narration, jobs):
    """Write actual ffprobe-measured audio information to narration records."""
    for field in ("scenes", "extra_scenes"):
        rows = narration.get(field, [])
        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue

            scene_id = str(row.get("scene_id") or "").strip()
            job = jobs.get(scene_id)

            if not job or job.get("status") != "completed":
                continue

            row.update({
                "audio": job["output"],
                "audio_path": job["output"],
                "audio_file": job["output"],
                "audio_sha256": job["sha256"],
                "audio_hash": job["sha256"],
                "audio_duration": job["duration"],
                "actual_audio_duration_seconds": job["duration"],
                "audio_duration_source": "ffprobe_measured",
                "tts_provider": job["provider"],
                "tts_model": job.get("model", job["provider"]),
                "provider": job["provider"],
                "model": job.get("model", job["provider"]),
                "chunks": job.get("chunk_files", []),
                "chunk_count": job.get("chunk_count", 0),
                "tts_fingerprint": job["fingerprint"],
                "fingerprint": job["fingerprint"],
                "status": "completed",
                "audio_completed_at": job.get("completed_at"),
            })

    expected = [
        row
        for field in ("scenes", "extra_scenes")
        for row in narration.get(field, [])
        if isinstance(row, dict)
    ]

    narration["tts_status"] = (
        "completed"
        if expected and all(
            jobs.get(str(row.get("scene_id")), {}).get("status")
            == "completed"
            for row in expected
        )
        else "in_progress"
    )

    narration["tts_updated_at"] = utc_now()
    save_json_atomic(NARRATION_FILE, narration)


def validate_narration_records(data, parts, scenes_per_part):
    if not isinstance(data, dict):
        raise RuntimeError("Narration manifest is not a JSON object.")

    if data.get("status") != "completed":
        raise RuntimeError("Narration text preparation is not completed.")

    main_rows = data.get("scenes")
    extra_rows = data.get("extra_scenes", [])

    if not isinstance(main_rows, list):
        raise RuntimeError("Narration manifest has no scenes list.")
    if not isinstance(extra_rows, list):
        raise RuntimeError("Narration extra_scenes must be a list.")

    expected_main = [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    main_map = {}

    for index, row in enumerate(main_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid main narration record {index}.")

        if str(row.get("role", "main")).lower() != "main":
            raise RuntimeError(
                f"Main narration record {index} has an invalid role."
            )

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid main scene mapping at record {index}."
            ) from exc

        if key not in expected_main or key in main_map:
            raise RuntimeError(f"Invalid or duplicate main scene: {key}.")

        narration_text(row)
        main_map[key] = dict(row)

    if set(main_map) != set(expected_main):
        missing = sorted(set(expected_main) - set(main_map))
        raise RuntimeError(
            f"Missing main narration scenes: {missing[:25]}"
        )

    extras = {}

    for index, row in enumerate(extra_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid extra narration record {index}.")

        role = str(row.get("role", "")).strip().lower()
        if role not in {"hook", "suspense", "resolution"}:
            raise RuntimeError(f"Invalid extra role: {role!r}")

        try:
            part = int(row["part"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid extra narration part at record {index}."
            ) from exc

        if not 1 <= part <= parts:
            raise RuntimeError(f"Extra narration part out of range: {part}.")

        scene_id = str(row.get("scene_id") or "").strip()
        if not scene_id:
            raise RuntimeError(
                f"Extra narration record {index} has no scene_id."
            )
        if scene_id in extras:
            raise RuntimeError(f"Duplicate extra scene_id: {scene_id}.")

        narration_text(row)

        if role == "suspense" and part == parts:
            raise RuntimeError("Final part cannot have Suspense narration.")
        if role == "resolution" and part != parts:
            raise RuntimeError(
                "Resolution narration is allowed only in the final part."
            )

        extras[scene_id] = dict(row)

    expected_roles = []
    for part in range(1, parts + 1):
        expected_roles.append((part, "hook"))
        if part < parts:
            expected_roles.append((part, "suspense"))
        else:
            expected_roles.append((part, "resolution"))

    actual_roles = [
        (int(row["part"]), row["role"])
        for row in extras.values()
    ]

    if sorted(actual_roles) != sorted(expected_roles):
        raise RuntimeError(
            "Hook/Suspense/Resolution records do not match "
            "the required per-part structure."
        )

    ordered_main = [main_map[key] for key in expected_main]

    ordered_extras = sorted(
        extras.values(),
        key=lambda row: (
            int(row["part"]),
            ROLE_ORDER[row["role"]],
            str(row["scene_id"]),
        ),
    )

    all_rows = ordered_main + ordered_extras
    all_ids = [record_identity(row)[0] for row in all_rows]

    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("Duplicate scene_id across narration records.")

    return ordered_main, ordered_extras


def synthesize_record(
    row,
    config,
    available,
    voice,
    speed,
    max_chars,
    min_chars,
    max_attempts,
    chunk_checkpoint,
    old_job,
    fingerprint,
):
    scene_id, part, role, scene = record_identity(row)
    output_file = output_path_for(row)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if reusable_audio(old_job, output_file, fingerprint):
        log(f"REUSED {scene_id}")
        return dict(old_job), True

    chunks = split_text(narration_text(row), max_chars, min_chars)
    errors = []

    for provider in available:
        log(f"{scene_id}: trying {provider}")

        for attempt in range(1, max_attempts + 1):
            chunk_paths = []

            try:
                for chunk_index, chunk_text in enumerate(chunks, 1):
                    chunk_path = chunk_path_for(row, chunk_index)
                    chunk_path.parent.mkdir(parents=True, exist_ok=True)
                    meta_path = chunk_path.with_suffix(".json")

                    chunk_fingerprint = stable_hash({
                        "version": VERSION,
                        "record_fingerprint": fingerprint,
                        "provider": provider,
                        "chunk_index": chunk_index,
                        "chunk_text": chunk_text,
                    })

                    meta = load_json(meta_path, required=False)
                    valid = False

                    if (
                        chunk_checkpoint
                        and isinstance(meta, dict)
                        and meta.get("status") == "completed"
                        and meta.get("fingerprint") == chunk_fingerprint
                    ):
                        try:
                            actual = validate_audio(chunk_path)
                            valid = meta.get("sha256") == actual["sha256"]
                        except Exception:
                            valid = False

                    if not valid:
                        chunk_path.unlink(missing_ok=True)
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
                            **info,
                            "completed_at": utc_now(),
                        })

                    chunk_paths.append(chunk_path)

                temporary = output_file.with_name(
                    output_file.stem + f".{provider}.partial.mp3"
                )
                temporary.unlink(missing_ok=True)

                concat_chunks(chunk_paths, temporary)
                audio_info = validate_audio(temporary)
                os.replace(temporary, output_file)

                return {
                    "scene_id": scene_id,
                    "part": part,
                    "scene": scene,
                    "role": role,
                    "status": "completed",
                    "output": output_file.as_posix(),
                    "audio_path": output_file.as_posix(),
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
                        path.as_posix() for path in chunk_paths
                    ],
                    "duration": audio_info["duration"],
                    "actual_audio_duration_seconds": audio_info["duration"],
                    "duration_source": "ffprobe_measured",
                    "size_bytes": audio_info["size_bytes"],
                    "sha256": audio_info["sha256"],
                    "provider_errors": errors,
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
                output_file.unlink(missing_ok=True)

                if attempt < max_attempts:
                    time.sleep(min(2 * attempt, 8))

        log(f"{provider} exhausted; trying next configured provider.")

    return {
        "scene_id": scene_id,
        "part": part,
        "scene": scene,
        "role": role,
        "status": "failed",
        "output": output_file.as_posix(),
        "fingerprint": fingerprint,
        "provider_errors": errors,
        "failed_at": utc_now(),
    }, False


def main():
    log("=" * 64)
    log("KATHA LOK AI - ROLE-AWARE MULTI-PROVIDER TTS")
    log("=" * 64)

    config = load_input_config()
    fmt = normalize_format(config)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("Invalid selected PARTS or SCENES configuration.")

    narration = load_json(NARRATION_FILE)
    main_rows, extra_rows = validate_narration_records(
        narration,
        parts,
        scenes_per_part,
    )

    records = main_rows + extra_rows
    order = provider_order(config)
    allow_paid = billable_allowed(config)

    voice = cfg_text(config, "VOICE", "male")
    speed = normalize_speed(cfg_text(config, "SPEED", "+0%"))

    max_attempts = cfg_int(
        config,
        "TTS_MAX_PROVIDER_ATTEMPTS",
        1,
        minimum=1,
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
    resume_enabled = cfg_bool(config, "RESUME_ENABLED", True)
    skip_completed = cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
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
        raise RuntimeError(f"Unsupported FAILURE_POLICY: {failure_policy}")

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

    log(f"FORMAT            : {fmt}")
    log(f"PARTS             : {parts}")
    log(f"SCENES/PART       : {scenes_per_part}")
    log(f"MAIN RECORDS      : {len(main_rows)}")
    log(f"EXTRA RECORDS     : {len(extra_rows)}")
    log(f"TOTAL TTS RECORDS : {len(records)}")
    log(f"PROVIDER ORDER    : {' -> '.join(order)}")
    log(f"AVAILABLE ORDER   : {' -> '.join(available)}")
    log(f"BILLABLE ALLOWED  : {allow_paid}")
    log(f"VOICE             : {voice}")
    log(f"SPEED             : {speed}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    old_jobs = manifest_map(manifest)

    generation_signature = stable_hash({
        "version": VERSION,
        "format": fmt,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "voice": voice,
        "speed": speed,
        "provider_order": order,
        "allow_paid": allow_paid,
        "max_chars": max_chars,
        "min_chars": min_chars,
        "narration_signature": narration.get(
            "source_signature",
            stable_hash(records),
        ),
    })

    if manifest.get("generation_signature") != generation_signature:
        log("TTS settings or narration changed; old record reuse disabled.")
        jobs = {}
    else:
        jobs = old_jobs

    manifest.update({
        "generation_signature": generation_signature,
        "narration_signature": narration.get("source_signature"),
        "format": fmt,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "main_record_count": len(main_rows),
        "extra_record_count": len(extra_rows),
        "provider_order": order,
        "available_providers": available,
        "voice": voice,
        "speed": speed,
        "started_at": manifest.get("started_at") or utc_now(),
    })

    save_manifest(manifest, jobs, "in_progress")

    generated = 0
    reused = 0
    failures = []

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
        }

        fingerprint = scene_fingerprint(row, settings, order)

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
        )

        jobs[scene_id] = job

        if was_reused:
            reused += 1
        elif job["status"] == "completed":
            generated += 1
        else:
            failures.append(scene_id)

        if save_each:
            save_manifest(
                manifest,
                jobs,
                "incomplete" if failures else "in_progress",
            )
            update_narration_audio(narration, jobs)

            save_json_atomic(CHECKPOINT_FILE, {
                "status": "incomplete" if failures else "in_progress",
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
                    "Previously completed records remain reusable."
                )
        else:
            log(
                f"[{index}/{len(records)}] COMPLETE {scene_id} "
                f"| role={role} | provider={job['provider']} "
                f"| measured={job['duration']:.3f}s"
            )

    missing = []

    for row in records:
        scene_id = record_identity(row)[0]
        job = jobs.get(scene_id)
        path = (
            Path(job["output"])
            if job and job.get("output")
            else None
        )

        if (
            not job
            or job.get("status") != "completed"
            or path is None
            or not reusable_audio(
                job,
                path,
                job.get("fingerprint", ""),
            )
        ):
            missing.append(scene_id)

    if missing:
        save_manifest(manifest, jobs, "incomplete")
        update_narration_audio(narration, jobs)
        raise RuntimeError(
            f"TTS incomplete: {len(missing)} records missing or invalid: "
            f"{missing[:30]}"
        )

    update_narration_audio(narration, jobs)

    manifest.update({
        "total_records": len(records),
        "main_records": len(main_rows),
        "extra_records": len(extra_rows),
        "completed_count": len(records),
        "failed_count": 0,
        "missing_records": [],
        "status": "completed",
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

    log("=" * 64)
    log("TTS COMPLETE")
    log(f"Main records      : {len(main_rows)}")
    log(f"Hook/extra records: {len(extra_rows)}")
    log(f"Total records     : {len(records)}")
    log(f"Newly generated   : {generated}")
    log(f"Reused records    : {reused}")
    log(f"Provider order    : {' -> '.join(order)}")
    log(f"Manifest          : {MANIFEST_FILE}")
    log("Audio durations   : ffprobe measured")
    log("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("TTS interrupted; completed chunk checkpoints are retained.")
        sys.exit(130)
    except Exception as exc:
        log(f"TTS FAILED: {exc}")
        sys.exit(1)