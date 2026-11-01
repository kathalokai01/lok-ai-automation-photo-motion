#!/usr/bin/env python3
"""Scene-wise TTS generation with configured provider order and recovery."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
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

SCRIPT_VERSION = 5
MIN_AUDIO_BYTES = 1000
REQUEST_TIMEOUT = 180
BILLABLE_PROVIDERS = {
    "fish_audio", "gemini_tts", "elevenlabs",
    "cartesia", "finevoice", "minimax",
}
FREE_PROVIDERS = {"edge_tts", "piper"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def log(*values):
    print(*values, flush=True)


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0"}:
        return False
    return default


def cfg_int(config, key, default, minimum=0):
    try:
        return max(minimum, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def stable_hash(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
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
            raise RuntimeError(f"Required file not found: {path}")
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
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
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def command_exists(name):
    return shutil.which(name) is not None


def run_command(command, timeout=REQUEST_TIMEOUT):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Command failed ({result.returncode}): {detail[-1800:]}"
        )
    return result


def request_bytes(url, headers, payload=None, timeout=REQUEST_TIMEOUT):
    body = None
    request_headers = dict(headers or {})

    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read()
            if not data:
                raise RuntimeError("Provider returned an empty response.")
            return data, content_type
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} from {url}: {detail[:1800]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc}") from exc


def request_json(url, headers, payload, timeout=REQUEST_TIMEOUT):
    data, _ = request_bytes(url, headers, payload, timeout)
    try:
        result = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Provider returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Provider JSON response is not an object.")
    return result


def get_audio_duration(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"Audio file missing or too small: {path}")
    if not command_exists("ffprobe"):
        raise RuntimeError("ffprobe is required for audio validation.")

    result = run_command([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ], timeout=60)

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid audio duration: {path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Audio duration is invalid: {path}")

    return round(duration, 3)


def validate_mp3(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"Missing or undersized audio: {path}")
    duration = get_audio_duration(path)
    return {
        "duration": duration,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def scene_key(scene):
    try:
        part = int(scene["part"])
        number = int(scene["scene"])
    except (KeyError, TypeError, ValueError):
        return None
    if part < 1 or number < 1:
        return None
    return part, number


def validate_narration(data, parts, scenes_per_part):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Narration manifest is not completed.")

    rows = data.get("scenes")
    if not isinstance(rows, list):
        raise RuntimeError("Narration manifest has no scenes list.")

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    mapped = {}

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}.")
        key = scene_key(row)
        if key is None or key not in expected:
            raise RuntimeError(f"Invalid narration scene mapping: {key}.")
        if key in mapped:
            raise RuntimeError(f"Duplicate narration scene: {key}.")
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Empty narration for scene {key}.")
        mapped[key] = dict(row)

    missing = sorted(expected - set(mapped))
    if missing:
        raise RuntimeError(f"Missing narration scenes: {missing[:30]}")

    return [
        mapped[(part, scene)]
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]


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


def resolve_edge_voice(value):
    voice = str(value or "female").strip()
    if voice.lower() in {"female", "woman", "f"}:
        return os.getenv("EDGE_TTS_VOICE", "hi-IN-SwaraNeural")
    if voice.lower() in {"male", "man", "m"}:
        return os.getenv("EDGE_TTS_VOICE", "hi-IN-MadhurNeural")
    return voice


def resolve_provider_order(config):
    order = get_tts_provider_order(config)
    if not order:
        raise RuntimeError("Configured TTS provider order is empty.")
    return order


def billable_allowed(config):
    global_allowed = cfg_bool(
        config, "ALLOW_BILLABLE_PROVIDERS", True
    )
    env_value = os.getenv("ALLOW_BILLABLE_PROVIDERS")
    if env_value is not None:
        global_allowed = global_allowed and (
            env_value.strip().lower() in {"true", "1", "yes", "on"}
        )

    return (
        global_allowed
        and cfg_bool(config, "TTS_ALLOW_PAID", True)
    )


def provider_available(provider, config, allow_billable):
    if provider in BILLABLE_PROVIDERS and not allow_billable:
        return False, "Billable providers disabled by configuration."

    if provider == "fish_audio":
        return bool(
            os.getenv("FISH_AUDIO_API_KEY") or os.getenv("FISH_API_KEY")
        ), "Missing FISH_AUDIO_API_KEY/FISH_API_KEY."

    if provider == "gemini_tts":
        return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")), \
            "Missing GEMINI_API_KEY/GOOGLE_API_KEY."

    if provider == "edge_tts":
        return command_exists("edge-tts"), "edge-tts executable not installed."

    if provider == "piper":
        model = os.getenv("PIPER_MODEL") or cfg_text(config, "PIPER_MODEL")
        return bool(
            model and Path(model).is_file()
            and (command_exists("piper") or command_exists("piper-tts"))
        ), "Piper executable or PIPER_MODEL file is missing."

    if provider == "elevenlabs":
        return bool(os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID")), \
            "Missing ElevenLabs API key or voice ID."

    if provider == "cartesia":
        return bool(os.getenv("CARTESIA_API_KEY") and os.getenv("CARTESIA_VOICE_ID")), \
            "Missing Cartesia API key or voice ID."

    if provider == "finevoice":
        return bool(
            os.getenv("FINEVOICE_API_KEY")
            and os.getenv("FINEVOICE_TTS_URL")
        ), "FineVoice requires FINEVOICE_API_KEY and FINEVOICE_TTS_URL."

    if provider == "minimax":
        return bool(
            os.getenv("MINIMAX_API_KEY")
            and os.getenv("MINIMAX_TTS_VOICE_ID")
        ), "Missing MiniMax API key or voice ID."

    return False, f"Unsupported provider: {provider}"


def split_text(text, max_chars, min_chars):
    """Split on sentence/paragraph boundaries without dropping source text."""
    text = str(text).strip()
    if not text:
        raise RuntimeError("Cannot synthesize empty narration.")

    max_chars = max(100, int(max_chars))
    min_chars = max(1, min(int(min_chars), max_chars))

    if len(text) <= max_chars:
        return [text]

    units = []
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]

    for paragraph in paragraphs or [text]:
        sentences = []
        start = 0
        for match in __import__("re").finditer(r"[.!?।॥]+(?:[\"'”’)]*)\s*", paragraph):
            end = match.end()
            sentence = paragraph[start:end].strip()
            if sentence:
                sentences.append(sentence)
            start = end
        tail = paragraph[start:].strip()
        if tail:
            sentences.append(tail)
        units.extend(sentences or [paragraph])

    chunks = []
    current = ""

    for unit in units:
        while len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            cut = unit.rfind(" ", 0, max_chars + 1)
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

    # Merge very short trailing chunks when possible.
    merged = []
    for chunk in chunks:
        if merged and len(chunk) < min_chars and len(merged[-1]) + 1 + len(chunk) <= max_chars:
            merged[-1] += " " + chunk
        else:
            merged.append(chunk)

    if "".join(merged).replace(" ", "") == "":
        raise RuntimeError("Narration chunking produced empty text.")

    return merged


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".partial" + path.suffix)
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError("Provider audio response is too small.")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def ffmpeg_convert_pcm(pcm_bytes, output_path, sample_rate=24000):
    """Convert Gemini's raw 16-bit PCM response to MP3."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path = output_path.with_name(output_path.stem + ".pcm.wav")
    try:
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm_bytes)
        run_command([
            "ffmpeg", "-y", "-v", "error", "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-q:a", "3",
            str(output_path),
        ])
    finally:
        wav_path.unlink(missing_ok=True)
    validate_mp3(output_path)


def concat_mp3_chunks(chunk_paths, output_path):
    if not chunk_paths:
        raise RuntimeError("No TTS audio chunks were produced.")
    if len(chunk_paths) == 1:
        shutil.copy2(chunk_paths[0], output_path)
        validate_mp3(output_path)
        return

    if not command_exists("ffmpeg"):
        raise RuntimeError("FFmpeg is required to join TTS audio chunks.")

    list_file = Path(output_path).with_suffix(".concat.txt")
    try:
        lines = []
        for path in chunk_paths:
            escaped = str(Path(path).resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        run_command([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-codec:a", "libmp3lame", "-q:a", "3",
            str(output_path),
        ])
    finally:
        list_file.unlink(missing_ok=True)
    validate_mp3(output_path)


def synthesize_provider(provider, text, output_path, config, speed, voice):
    """Generate one MP3 chunk using exactly the selected provider."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "edge_tts":
        if not command_exists("edge-tts"):
            raise RuntimeError("edge-tts executable is not installed.")
        temporary = output_path.with_name(output_path.stem + ".edge.partial.mp3")
        try:
            run_command([
                "edge-tts",
                "--voice", resolve_edge_voice(voice),
                "--rate", speed,
                "--text", text,
                "--write-media", str(temporary),
            ])
            validate_mp3(temporary)
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
        return validate_mp3(output_path)

    if provider == "piper":
        model = os.getenv("PIPER_MODEL") or cfg_text(config, "PIPER_MODEL")
        executable = shutil.which("piper") or shutil.which("piper-tts")
        if not model or not Path(model).is_file() or not executable:
            raise RuntimeError("Piper executable/model is not configured.")
        wav_path = output_path.with_suffix(".piper.wav")
        try:
            subprocess.run(
                [executable, "--model", model, "--output_file", str(wav_path)],
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=REQUEST_TIMEOUT,
            )
            if not wav_path.is_file() or wav_path.stat().st_size < MIN_AUDIO_BYTES:
                raise RuntimeError("Piper returned invalid WAV audio.")
            run_command([
                "ffmpeg", "-y", "-v", "error", "-i", str(wav_path),
                "-codec:a", "libmp3lame", "-q:a", "3",
                str(output_path),
            ])
        finally:
            wav_path.unlink(missing_ok=True)
        return validate_mp3(output_path)

    if provider == "fish_audio":
        api_key = os.getenv("FISH_AUDIO_API_KEY") or os.getenv("FISH_API_KEY")
        reference_id = os.getenv("FISH_REFERENCE_ID", "").strip()
        model = os.getenv("FISH_TTS_MODEL", "s1")
        payload = {
            "text": text,
            "format": "mp3",
            "mp3_bitrate": 128,
            "latency": "normal",
            "normalize": True,
        }
        if reference_id:
            payload["reference_id"] = reference_id
        if model:
            payload["model"] = model
        data, _ = request_bytes(
            "https://api.fish.audio/v1/tts",
            {"Authorization": f"Bearer {api_key}"},
            payload,
        )
        write_bytes(output_path, data)
        return validate_mp3(output_path)

    if provider == "gemini_tts":
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        model = os.getenv(
            "GEMINI_TTS_MODEL",
            "gemini-2.5-flash-preview-tts",
        )
        voice_name = os.getenv("GEMINI_TTS_VOICE", "").strip()
        if not voice_name:
            voice_name = "Kore" if str(voice).lower() == "female" else "Puck"

        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice_name}
                    }
                },
            },
        }
        result = request_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            {},
            payload,
        )
        try:
            parts = result["candidates"][0]["content"]["parts"]
            audio_part = next(
                item for item in parts
                if isinstance(item, dict) and isinstance(item.get("inlineData"), dict)
            )
            encoded = audio_part["inlineData"]["data"]
            pcm = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise RuntimeError(
                f"Gemini TTS returned no usable audio: {str(result)[:1200]}"
            ) from exc
        ffmpeg_convert_pcm(pcm, output_path, sample_rate=24000)
        return validate_mp3(output_path)

    if provider == "elevenlabs":
        api_key = os.getenv("ELEVENLABS_API_KEY")
        voice_id = os.getenv("ELEVENLABS_VOICE_ID")
        model = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
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
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
            {"xi-api-key": api_key, "Accept": "audio/mpeg"},
            payload,
        )
        write_bytes(output_path, data)
        return validate_mp3(output_path)

    if provider == "cartesia":
        payload = {
            "model_id": os.getenv("CARTESIA_TTS_MODEL", "sonic-2"),
            "transcript": text,
            "voice": {"mode": "id", "id": os.getenv("CARTESIA_VOICE_ID")},
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
                "Cartesia-Version": os.getenv("CARTESIA_API_VERSION", "2025-04-16"),
                "Accept": "audio/mpeg",
            },
            payload,
        )
        write_bytes(output_path, data)
        return validate_mp3(output_path)

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
            audio_hex = result["data"]["audio"]
            audio = bytes.fromhex(audio_hex)
        except Exception as exc:
            raise RuntimeError(
                f"MiniMax TTS returned no audio: {str(result)[:1200]}"
            ) from exc
        write_bytes(output_path, audio)
        return validate_mp3(output_path)

    if provider == "finevoice":
        endpoint = os.getenv("FINEVOICE_TTS_URL", "").strip()
        if not endpoint:
            raise RuntimeError(
                "FINEVOICE_TTS_URL is not configured; provider endpoint "
                "cannot safely be inferred from an API key."
            )
        payload = {
            "text": text,
            "voice_id": os.getenv("FINEVOICE_VOICE_ID", ""),
            "format": "mp3",
        }
        result, content_type = request_bytes(
            endpoint,
            {
                "Authorization": f"Bearer {os.getenv('FINEVOICE_API_KEY')}",
                "Accept": "audio/mpeg,application/json",
            },
            payload,
        )
        if "json" in content_type.lower():
            try:
                parsed = json.loads(result.decode("utf-8"))
                encoded = (
                    parsed.get("audio")
                    or parsed.get("audio_base64")
                    or parsed.get("data")
                )
                result = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise RuntimeError("FineVoice JSON response has no valid audio data.") from exc
        write_bytes(output_path, result)
        return validate_mp3(output_path)

    raise RuntimeError(f"Unknown TTS provider: {provider}")


def scene_output_path(part, scene):
    return OUTPUT_DIR / f"part_{part:02d}" / f"scene_{scene:02d}.mp3"


def chunk_output_path(part, scene, chunk_index):
    folder = OUTPUT_DIR / f"part_{part:02d}" / "chunks"
    return folder / f"scene_{scene:02d}_chunk_{chunk_index:03d}.mp3"


def scene_fingerprint(text, settings, provider_order):
    return stable_hash({
        "script_version": SCRIPT_VERSION,
        "text": text,
        "settings": settings,
        "provider_order": provider_order,
    })


def load_manifest():
    data = load_json(MANIFEST_FILE, required=False)
    if not isinstance(data, dict):
        return {"status": "in_progress", "jobs": []}
    if not isinstance(data.get("jobs"), list):
        data["jobs"] = []
    return data


def manifest_map(manifest):
    mapped = {}
    for row in manifest.get("jobs", []):
        if isinstance(row, dict):
            key = scene_key(row)
            if key:
                mapped[key] = dict(row)
    return mapped


def save_manifest(manifest, jobs, status="in_progress"):
    manifest["status"] = status
    manifest["jobs"] = sorted(
        jobs.values(),
        key=lambda row: (int(row["part"]), int(row["scene"])),
    )
    manifest["completed_count"] = sum(
        1 for row in manifest["jobs"] if row.get("status") == "completed"
    )
    manifest["updated_at"] = utc_now()
    save_json_atomic(MANIFEST_FILE, manifest)


def reusable_audio(job, output_file, fingerprint):
    if not isinstance(job, dict) or job.get("status") != "completed":
        return False
    if job.get("fingerprint") != fingerprint:
        return False
    try:
        actual = validate_mp3(output_file)
    except Exception:
        return False
    return (
        job.get("sha256") == actual["sha256"]
        and abs(float(job.get("duration", 0)) - actual["duration"]) < 0.15
    )


def main():
    log("=" * 64)
    log("KATHA LOK AI - MULTI-PROVIDER TTS")
    log("=" * 64)

    config = load_input_config()
    fmt = normalize_format(config)
    parts = get_parts(config)
    scenes_per_part = get_scenes(config)

    narration = load_json(NARRATION_FILE)
    scenes = validate_narration(narration, parts, scenes_per_part)
    expected_total = parts * scenes_per_part

    provider_order = resolve_provider_order(config)
    allow_paid = billable_allowed(config)

    voice = cfg_text(config, "VOICE", "female")
    speed = normalize_speed(cfg_text(config, "SPEED", "+0%"))
    retries = cfg_int(config, "MAX_RETRIES", 3, minimum=1)
    max_attempts = cfg_int(config, "TTS_MAX_PROVIDER_ATTEMPTS", 1, minimum=1)
    max_attempts = min(max_attempts, 3)

    max_chars = cfg_int(config, "TTS_CHUNK_MAX_CHARS", 500, minimum=100)
    min_chars = cfg_int(config, "TTS_CHUNK_MIN_CHARS", 150, minimum=1)
    chunk_checkpoint = cfg_bool(config, "TTS_CHUNK_CHECKPOINT", True)
    validate_enabled = cfg_bool(config, "TTS_VALIDATE_AUDIO", True)

    resume_enabled = cfg_bool(config, "RESUME_ENABLED", True)
    skip_completed = cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
    save_each_scene = cfg_bool(
        config, "SAVE_CHECKPOINT_AFTER_EACH_SCENE", True
    )
    failure_policy = cfg_text(
        config, "FAILURE_POLICY", "retry_then_checkpoint"
    ).lower()

    if failure_policy not in {"retry_then_checkpoint", "checkpoint", "retry", "skip", "stop"}:
        raise RuntimeError(f"Unsupported FAILURE_POLICY: {failure_policy}")

    if min_chars > max_chars:
        raise RuntimeError("TTS_CHUNK_MIN_CHARS cannot exceed TTS_CHUNK_MAX_CHARS.")

    available = []
    unavailable = []
    for provider in provider_order:
        ok, reason = provider_available(provider, config, allow_paid)
        if ok:
            available.append(provider)
        else:
            unavailable.append((provider, reason))

    log(f"FORMAT             : {fmt}")
    log(f"PARTS              : {parts}")
    log(f"SCENES PER PART    : {scenes_per_part}")
    log(f"TOTAL SCENES       : {expected_total}")
    log(f"PROVIDER ORDER     : {' -> '.join(provider_order)}")
    log(f"AVAILABLE PROVIDERS: {', '.join(available) or 'none'}")
    log(f"BILLABLE ALLOWED   : {allow_paid}")
    log(f"VOICE              : {voice}")
    log(f"SPEED              : {speed}")

    for provider, reason in unavailable:
        log(f"PROVIDER SKIP      : {provider}: {reason}")

    if not available:
        raise RuntimeError("No configured TTS provider is available.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()

    generation_signature = stable_hash({
        "script_version": SCRIPT_VERSION,
        "format": fmt,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "voice": voice,
        "speed": speed,
        "provider_order": provider_order,
        "allow_paid": allow_paid,
        "narration_signature": narration.get(
            "source_signature", stable_hash(scenes)
        ),
    })

    if manifest.get("generation_signature") != generation_signature:
        log("TTS settings or narration changed; old jobs will not be reused.")
        jobs = {}
    else:
        jobs = manifest_map(manifest)

    manifest.update({
        "script_version": SCRIPT_VERSION,
        "status": "in_progress",
        "format": fmt,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "provider_order": provider_order,
        "available_providers": available,
        "voice": voice,
        "speed": speed,
        "generation_signature": generation_signature,
        "narration_signature": narration.get("source_signature"),
        "started_at": manifest.get("started_at") or utc_now(),
    })
    save_manifest(manifest, jobs, "in_progress")

    failed = []
    reused_count = 0
    generated_count = 0

    for scene_index, scene in enumerate(scenes, 1):
        part, number = scene_key(scene)
        text = scene["text"].strip()
        output_file = scene_output_path(part, number)

        settings = {
            "voice": voice,
            "speed": speed,
            "format": fmt,
            "audience": scene.get("audience", "adult"),
            "max_chars": max_chars,
            "min_chars": min_chars,
        }
        fingerprint = scene_fingerprint(text, settings, provider_order)
        old_job = jobs.get((part, number))

        if resume_enabled and skip_completed and reusable_audio(
            old_job, output_file, fingerprint
        ):
            log(f"[{scene_index}/{expected_total}] REUSED Part {part} Scene {number}")
            reused_count += 1
            continue

        chunks = split_text(text, max_chars, min_chars)
        scene_errors = []
        successful_job = None

        for provider in available:
            if successful_job:
                break

            log(
                f"[{scene_index}/{expected_total}] "
                f"Part {part} Scene {number}: trying {provider}"
            )

            provider_success = False
            for provider_attempt in range(1, max_attempts + 1):
                chunk_paths = []
                try:
                    for chunk_index, chunk_text in enumerate(chunks, 1):
                        chunk_path = chunk_output_path(part, number, chunk_index)
                        chunk_path.parent.mkdir(parents=True, exist_ok=True)

                        chunk_fingerprint = stable_hash({
                            "scene_fingerprint": fingerprint,
                            "provider": provider,
                            "chunk_index": chunk_index,
                            "chunk_text": chunk_text,
                        })

                        # Reuse only a validated chunk belonging to this
                        # exact text/provider/version fingerprint.
                        chunk_meta_path = chunk_path.with_suffix(".json")
                        chunk_meta = load_json(chunk_meta_path, required=False)
                        chunk_valid = False

                        if (
                            chunk_checkpoint
                            and isinstance(chunk_meta, dict)
                            and chunk_meta.get("fingerprint") == chunk_fingerprint
                            and chunk_meta.get("status") == "completed"
                        ):
                            try:
                                chunk_info = validate_mp3(chunk_path)
                                chunk_valid = (
                                    chunk_meta.get("sha256") == chunk_info["sha256"]
                                )
                            except Exception:
                                chunk_valid = False

                        if not chunk_valid:
                            chunk_path.unlink(missing_ok=True)
                            info = synthesize_provider(
                                provider, chunk_text, chunk_path,
                                config, speed, voice,
                            )
                            save_json_atomic(chunk_meta_path, {
                                "status": "completed",
                                "provider": provider,
                                "fingerprint": chunk_fingerprint,
                                **info,
                                "completed_at": utc_now(),
                            })

                        chunk_paths.append(chunk_path)

                    temporary_output = output_file.with_name(
                        output_file.stem + f".{provider}.partial.mp3"
                    )
                    temporary_output.unlink(missing_ok=True)
                    concat_mp3_chunks(chunk_paths, temporary_output)
                    audio_info = validate_mp3(temporary_output)
                    os.replace(temporary_output, output_file)

                    successful_job = {
                        "part": part,
                        "scene": number,
                        "status": "completed",
                        "output": output_file.as_posix(),
                        "provider": provider,
                        "voice": voice,
                        "speed": speed,
                        "fingerprint": fingerprint,
                        "chunk_count": len(chunks),
                        "chunk_files": [path.as_posix() for path in chunk_paths],
                        "duration": audio_info["duration"],
                        "size_bytes": audio_info["size_bytes"],
                        "sha256": audio_info["sha256"],
                        "completed_at": utc_now(),
                        "error": None,
                    }
                    provider_success = True
                    break

                except Exception as exc:
                    scene_errors.append({
                        "provider": provider,
                        "attempt": provider_attempt,
                        "error": str(exc)[:1500],
                    })
                    log(f"  {provider} failed: {exc}")

                    # Discard a mixed/partial final file. Completed chunks
                    # stay cached but are reused only for the same provider.
                    output_file.unlink(missing_ok=True)
                    if provider_attempt < max_attempts:
                        time.sleep(min(2 * provider_attempt, 8))

            if not provider_success:
                log(f"  Moving to next configured provider.")

        if successful_job is None:
            failed.append((part, number, scene_errors))
            jobs[(part, number)] = {
                "part": part,
                "scene": number,
                "status": "failed",
                "output": output_file.as_posix(),
                "fingerprint": fingerprint,
                "provider_errors": scene_errors,
                "failed_at": utc_now(),
            }
            save_manifest(manifest, jobs, "incomplete")

            if failure_policy != "skip":
                raise RuntimeError(
                    f"TTS failed for Part {part} Scene {number}; "
                    "failure checkpoint saved. Successful scenes remain reusable."
                )
            continue

        jobs[(part, number)] = successful_job
        generated_count += 1

        if save_each_scene:
            save_manifest(manifest, jobs, "in_progress")

        log(
            f"SUCCESS Part {part} Scene {number} "
            f"| provider={successful_job['provider']} "
            f"| duration={successful_job['duration']:.2f}s"
        )

    # Validate every output file and its manifest checksum again.
    missing = []
    for scene in scenes:
        part, number = scene_key(scene)
        job = jobs.get((part, number))
        output_file = scene_output_path(part, number)

        if not reusable_audio(job, output_file, job.get("fingerprint") if job else ""):
            missing.append((part, number))

    if missing:
        manifest["missing_scenes"] = missing
        save_manifest(manifest, jobs, "incomplete")
        raise RuntimeError(
            f"TTS incomplete: {len(missing)}/{expected_total} scenes missing: {missing}"
        )

    manifest.update({
        "status": "completed",
        "completed_count": expected_total,
        "missing_scenes": [],
        "failed_scenes": [],
        "completed_at": utc_now(),
    })
    save_manifest(manifest, jobs, "completed")

    log("=" * 64)
    log("TTS COMPLETE")
    log(f"Total scenes       : {expected_total}")
    log(f"Newly generated    : {generated_count}")
    log(f"Reused scenes      : {reused_count}")
    log(f"Provider order     : {' -> '.join(provider_order)}")
    log(f"Manifest           : {MANIFEST_FILE}")
    log("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted; completed scene checkpoints are retained.")
        sys.exit(130)
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)