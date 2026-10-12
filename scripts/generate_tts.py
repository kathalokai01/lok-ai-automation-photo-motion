#!/usr/bin/env python3
"""
Katha Lok AI — chunked TTS with validated checkpoints and provider fallback.

Output:
  output/narration/narration.json
  output/narration/audio/part_XX/scene_XX.mp3
  output/narration/audio_jobs.json

Paid/uncertain-cost cloud providers are blocked unless TTS_ALLOW_PAID=true.
Never add API keys to this source file.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from input_config import (
    load_input_config,
    get_parts,
    get_scenes,
    normalize_format,
)

NARRATION_FILE = Path("output/narration/narration.json")
OUTPUT_DIR = Path("output/narration/audio")
MANIFEST_FILE = Path("output/narration/audio_jobs.json")
CHUNK_DIR = OUTPUT_DIR / "_chunks"


# ------------------------------------------------------------
# GENERAL HELPERS
# ------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def positive_int(value, name, default=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise SystemExit(f"ERROR: {name} must be an integer.")
    if result < 1:
        raise SystemExit(f"ERROR: {name} must be greater than zero.")
    return result


def load_json(path):
    if not path.is_file():
        raise SystemExit(f"ERROR: Required file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: Could not read {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: Expected JSON object: {path}")
    return data


def load_manifest():
    if MANIFEST_FILE.is_file():
        try:
            with MANIFEST_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                data.setdefault("jobs", [])
                if isinstance(data["jobs"], list):
                    return data
        except (OSError, json.JSONDecodeError):
            print("WARNING: Existing manifest is unreadable; rebuilding it.")
    return {"status": "in_progress", "jobs": []}


def save_manifest(manifest, manifest_jobs=None):
    # Save the live scene map into the manifest JSON list on every checkpoint.
    if manifest_jobs is not None:
        manifest["jobs"] = sorted(
            manifest_jobs.values(),
            key=lambda job: (
                int(job.get("part", 0)),
                int(job.get("scene", 0)),
            ),
        )

    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = utc_now()
    temporary = MANIFEST_FILE.with_suffix(".json.tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary, MANIFEST_FILE)


def run_command(command, timeout=300, input_text=None):
    result = subprocess.run(
        command,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Command failed ({result.returncode}): {message[-1200:]}"
        )

    return result


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed.")
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed.")


# ------------------------------------------------------------
# AUDIO VALIDATION AND NORMALIZATION
# ------------------------------------------------------------

def audio_duration(path):
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"Audio file is missing or too small: {path}")

    result = run_command([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ], timeout=60)

    try:
        duration = float(result.stdout.strip())
    except (ValueError, TypeError):
        raise RuntimeError(f"Cannot read audio duration: {path}")

    if duration <= 0:
        raise RuntimeError(f"Invalid audio duration: {path}")

    return duration


def normalize_to_mp3(source, destination):
    require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp3")

    try:
        temporary.unlink(missing_ok=True)

        run_command([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", "24000",
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(temporary),
        ], timeout=300)

        audio_duration(temporary)
        os.replace(temporary, destination)
        return audio_duration(destination)

    finally:
        temporary.unlink(missing_ok=True)


def validate_mp3(path):
    return audio_duration(path)


# ------------------------------------------------------------
# TEXT CHUNKING
# ------------------------------------------------------------

def split_text(text, max_chars, min_chars):
    """Split narration at sentence and word boundaries."""
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        raise RuntimeError("Narration text is empty.")

    chunks = []
    remaining = text

    while len(remaining) > max_chars:
        window = remaining[:max_chars + 1]
        cut = -1

        for match in re.finditer(r"[।!?]\s+", window):
            if match.end() >= min_chars:
                cut = match.end()

        if cut < min_chars:
            for match in re.finditer(r"[,;:]\s+", window):
                if match.end() >= min_chars:
                    cut = match.end()

        if cut < min_chars:
            cut = window.rfind(" ", min_chars, max_chars + 1)

        if cut < 1:
            cut = max_chars

        piece = remaining[:cut].strip()

        if piece:
            chunks.append(piece)

        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


# ------------------------------------------------------------
# HTTP API HELPERS
# ------------------------------------------------------------

def http_request(url, headers, body=None, timeout=120):
    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {details[:700]}")

    except Exception as exc:
        raise RuntimeError(f"API request failed: {exc}")


def json_post(url, headers, payload, timeout=120):
    body, content_type = http_request(
        url,
        {**headers, "Content-Type": "application/json"},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )

    if "json" not in content_type.lower():
        try:
            content_type = body[:100].decode("utf-8", errors="ignore")
        except Exception:
            content_type = ""

    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(
            f"Provider did not return valid JSON: {content_type[:150]}"
        )


def save_binary_response(url, headers, payload, destination):
    body, _ = http_request(
        url,
        {**headers, "Content-Type": "application/json"},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )

    if len(body) < 100:
        raise RuntimeError("Provider returned an empty audio response.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)


def decode_audio_value(value, destination, encoding="base64"):
    if not isinstance(value, str) or not value:
        raise RuntimeError("Provider response contains no audio data.")

    if encoding == "hex":
        data = bytes.fromhex(value)
    else:
        if value.startswith("data:") and "," in value:
            value = value.split(",", 1)[1]
        data = base64.b64decode(value)

    if len(data) < 100:
        raise RuntimeError("Decoded audio is empty or too small.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


# ------------------------------------------------------------
# PROVIDER CONFIGURATION
# ------------------------------------------------------------

def secret(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def paid_allowed(config):
    # API key presence alone is never permission to spend.
    config_allowed = cfg_bool(config, "TTS_ALLOW_PAID", False)
    env_value = os.environ.get("TTS_ALLOW_PAID", "").strip().lower()

    if env_value in {"false", "no", "off", "0"}:
        return False

    if env_value in {"true", "yes", "on", "1"}:
        return config_allowed

    return config_allowed


def get_provider_order(config):
    main = [
        cfg_text(config, "TTS_MAIN_1", "fish_audio").lower(),
        cfg_text(config, "TTS_MAIN_2", "gemini_tts").lower(),
        cfg_text(config, "TTS_MAIN_3", "edge_tts").lower(),
    ]

    fallback_enabled = cfg_bool(config, "TTS_FALLBACK_ENABLED", True)
    fallbacks = []

    if fallback_enabled:
        fallbacks = [
            item.strip().lower()
            for item in cfg_text(
                config,
                "TTS_FALLBACK_ORDER",
                "fish_audio,gemini_tts,edge_tts,piper,"
                "elevenlabs,cartesia,finevoice,minimax",
            ).split(",")
            if item.strip()
        ]

    allowed_order = [
        "fish_audio",
        "gemini_tts",
        "edge_tts",
        "piper",
        "elevenlabs",
        "cartesia",
        "finevoice",
        "minimax",
    ]
    allowed = set(allowed_order)

    if any(provider not in allowed for provider in main):
        raise SystemExit(
            "ERROR: TTS_MAIN_1/2/3 must each name a supported provider: "
            + ", ".join(allowed_order)
        )

    if len(set(main)) != 3:
        raise SystemExit(
            "ERROR: TTS_MAIN_1, TTS_MAIN_2 and TTS_MAIN_3 must be different."
        )

    # Fallback OFF: only the three selected primary providers.
    # Fallback ON: primaries, configured fallbacks, then omitted providers.
    order_source = main + fallbacks

    if fallback_enabled:
        order_source += allowed_order

    order = []

    for provider in order_source:
        if provider in allowed and provider not in order:
            order.append(provider)

    return order


def provider_is_paid(provider):
    return provider in {
        "fish_audio",
        "gemini_tts",
        "elevenlabs",
        "cartesia",
        "finevoice",
        "minimax",
    }


# ------------------------------------------------------------
# INDIVIDUAL TTS PROVIDERS
# ------------------------------------------------------------

def generate_edge(text, raw_file, voice, speed):
    if not shutil.which("edge-tts"):
        raise RuntimeError("edge-tts is not installed.")

    temporary = raw_file.with_suffix(".edge.mp3")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)

    try:
        run_command([
            "edge-tts",
            "--voice", voice,
            "--rate", speed,
            "--text", text,
            "--write-media", str(temporary),
        ], timeout=180)

        validate_mp3(temporary)
        shutil.copyfile(temporary, raw_file)

    finally:
        temporary.unlink(missing_ok=True)


def generate_fish(text, raw_file, config):
    api_key = secret("FISH_API_KEY", "FISH_AUDIO_API_KEY")

    if not api_key:
        raise RuntimeError("Fish Audio API key is not configured.")

    model = os.environ.get("FISH_TTS_MODEL", "s2.1-pro-free").strip()
    reference_id = secret("FISH_REFERENCE_ID", "FISH_AUDIO_REFERENCE_ID")

    payload = {"text": text, "format": "mp3"}

    if reference_id:
        payload["reference_id"] = reference_id

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        "https://api.fish.audio/v1/tts",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "model": model,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            audio = response.read()

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Fish Audio HTTP {exc.code}: {details[:500]}")

    if len(audio) < 100:
        raise RuntimeError("Fish Audio returned empty audio.")

    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(audio)


def generate_gemini(text, raw_file, config):
    api_key = secret("GEMINI_API_KEY", "GOOGLE_API_KEY")

    if not api_key:
        raise RuntimeError("Gemini API key is not configured.")

    model = os.environ.get(
        "GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts"
    ).strip()

    voice = os.environ.get("GEMINI_TTS_VOICE", "Kore").strip()

    payload = {
        "model": model,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": (
                    "Read the following narration naturally in Hindi. "
                    "Do not translate, summarize, or add words:\n" + text
                ),
            }],
        }],
        "response_format": {"type": "audio"},
        "generation_config": {
            "speech_config": [{"voice": voice}],
        },
    }

    data = json_post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        {"x-goog-api-key": api_key},
        payload,
    )

    audio_value = None
    output_audio = data.get("output_audio")

    if isinstance(output_audio, dict):
        audio_value = output_audio.get("data")

    output = data.get("output", [])

    if not audio_value and isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue

            if item.get("type") in {"audio", "output_audio"}:
                audio_value = item.get("data") or item.get("audio")

                if audio_value:
                    break

            content = item.get("content")

            if isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") in {"audio", "output_audio"}
                    ):
                        audio_value = part.get("data") or part.get("audio")

                        if audio_value:
                            break

            if audio_value:
                break

    if not audio_value:
        raise RuntimeError(
            "Gemini TTS response has no recognized audio data: "
            + json.dumps(data, ensure_ascii=False)[:500]
        )

    decode_audio_value(audio_value, raw_file)


def generate_elevenlabs(text, raw_file, config):
    api_key = secret("ELEVENLABS_API_KEY")

    if not api_key:
        raise RuntimeError("ElevenLabs API key is not configured.")

    voice_id = secret("ELEVENLABS_VOICE_ID")

    if not voice_id:
        raise RuntimeError(
            "Set ELEVENLABS_VOICE_ID in GitHub Actions Secrets."
        )

    model = os.environ.get(
        "ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"
    )

    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + urllib.parse.quote(voice_id, safe="")
        + "?output_format=mp3_44100_128"
    )

    save_binary_response(
        url,
        {"xi-api-key": api_key},
        {"text": text, "model_id": model},
        raw_file,
    )


def generate_cartesia(text, raw_file, config):
    api_key = secret("CARTESIA_API_KEY")

    if not api_key:
        raise RuntimeError("Cartesia API key is not configured.")

    voice_id = secret("CARTESIA_VOICE_ID")

    if not voice_id:
        raise RuntimeError(
            "Set CARTESIA_VOICE_ID in GitHub Actions Secrets."
        )

    payload = {
        "model_id": os.environ.get("CARTESIA_TTS_MODEL", "sonic-2"),
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "mp3",
            "sample_rate": 24000,
            "encoding": "mp3",
        },
    }

    body, _ = http_request(
        "https://api.cartesia.ai/tts/bytes",
        {
            "Authorization": f"Bearer {api_key}",
            "Cartesia-Version": "2025-04-16",
            "Content-Type": "application/json",
        },
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )

    if len(body) < 100:
        raise RuntimeError("Cartesia returned empty audio.")

    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(body)


def generate_minimax(text, raw_file, config):
    api_key = secret("MINIMAX_API_KEY")

    if not api_key:
        raise RuntimeError("MiniMax API key is not configured.")

    voice_id = os.environ.get(
        "MINIMAX_TTS_VOICE_ID", "English_expressive_narrator"
    )
    model = os.environ.get("MINIMAX_TTS_MODEL", "speech-2.8-turbo")

    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": voice_id,
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

    data = json_post(
        os.environ.get(
            "MINIMAX_TTS_URL", "https://api.minimax.io/v1/t2a_v2"
        ),
        {"Authorization": f"Bearer {api_key}"},
        payload,
    )

    try:
        if data.get("base_resp", {}).get("status_code", 0) != 0:
            raise RuntimeError(str(data["base_resp"]))

        audio_hex = data["data"]["audio"]
        decode_audio_value(audio_hex, raw_file, encoding="hex")

    except (KeyError, TypeError):
        raise RuntimeError(
            "MiniMax response did not contain audio: "
            + json.dumps(data, ensure_ascii=False)[:500]
        )


def generate_finevoice(text, raw_file, config):
    api_key = secret("FINEVOICE_API_KEY")

    if not api_key:
        raise RuntimeError("FineVoice API key is not configured.")

    voice_id = secret("FINEVOICE_VOICE_ID")

    if not voice_id:
        raise RuntimeError(
            "Set FINEVOICE_VOICE_ID after confirming your FineVoice API voice."
        )

    data = json_post(
        "https://api.finevoice.ai/v1/text_to_speech",
        {"Authorization": f"Bearer {api_key}"},
        {
            "text": text,
            "voice_id": voice_id,
            "format": "mp3",
        },
    )

    audio_value = (
        data.get("audio")
        or data.get("audio_base64")
        or data.get("audio_data")
    )

    if audio_value:
        decode_audio_value(audio_value, raw_file)
        return

    audio_url = data.get("audio_url") or data.get("url")

    if audio_url and str(audio_url).startswith("https://"):
        with urllib.request.urlopen(audio_url, timeout=120) as response:
            raw_file.write_bytes(response.read())
        return

    raise RuntimeError(
        "FineVoice response format is not recognized. "
        "Check the current API docs for your account."
    )


def generate_piper(text, raw_file, config):
    executable = shutil.which("piper")
    model = secret("PIPER_MODEL_PATH") or os.environ.get(
        "PIPER_MODEL_PATH", ""
    ).strip()

    if not executable:
        raise RuntimeError("Piper executable is not installed.")

    if not model or not Path(model).is_file():
        raise RuntimeError(
            "Piper local model is unavailable; configure PIPER_MODEL_PATH."
        )

    raw_file.parent.mkdir(parents=True, exist_ok=True)

    run_command(
        [executable, "--model", model, "--output_file", str(raw_file)],
        timeout=300,
        input_text=text,
    )


PROVIDERS = {
    "fish_audio": generate_fish,
    "gemini_tts": generate_gemini,
    "edge_tts": generate_edge,
    "elevenlabs": generate_elevenlabs,
    "cartesia": generate_cartesia,
    "finevoice": generate_finevoice,
    "minimax": generate_minimax,
    "piper": generate_piper,
}


# ------------------------------------------------------------
# VOICE AND FILE PATHS
# ------------------------------------------------------------

def resolve_edge_voice(value):
    voice = str(value).strip()
    normalized = voice.lower()

    if normalized in {"female", "woman", "f"}:
        return "hi-IN-SwaraNeural"

    if normalized in {"male", "man", "m"}:
        return "hi-IN-MadhurNeural"

    return voice


def scene_key(scene):
    try:
        part = int(scene.get("part"))
        number = int(scene.get("scene"))
    except (TypeError, ValueError, AttributeError):
        return None

    if part < 1 or number < 1:
        return None

    return part, number


def scene_audio_path(part, number):
    return (
        OUTPUT_DIR
        / f"part_{part:02d}"
        / f"scene_{number:02d}.mp3"
    )


def chunk_audio_path(part, number, chunk_number):
    return (
        CHUNK_DIR
        / f"part_{part:02d}"
        / f"scene_{number:02d}"
        / f"chunk_{chunk_number:04d}.mp3"
    )


def normalize_scenes(data, expected_total):
    scenes = data.get("scenes")

    if not isinstance(scenes, list):
        raise SystemExit("ERROR: narration.json must contain a scenes list.")

    if len(scenes) != expected_total:
        raise SystemExit(
            f"ERROR: Expected {expected_total} scenes, found {len(scenes)}."
        )

    seen = set()

    for scene in scenes:
        key = scene_key(scene)

        if key is None or key in seen:
            raise SystemExit(f"ERROR: Invalid/duplicate scene: {scene}")

        seen.add(key)

        if not isinstance(scene.get("text"), str) or not scene["text"].strip():
            raise SystemExit(f"ERROR: Empty narration for scene {key}.")

    return scenes


# ------------------------------------------------------------
# CHECKPOINT HELPERS
# ------------------------------------------------------------

def make_manifest_map(manifest):
    jobs = {}

    for job in manifest.get("jobs", []):
        if not isinstance(job, dict):
            continue

        key = scene_key(job)

        if key:
            jobs[key] = job

    return jobs


def chunk_is_valid(record, path, expected_hash, expected_settings_hash):
    if not isinstance(record, dict):
        return False

    if record.get("status") != "completed":
        return False

    if record.get("text_sha256") != expected_hash:
        return False

    if record.get("settings_sha256") != expected_settings_hash:
        return False

    if not path.is_file():
        return False

    try:
        validate_mp3(path)
        return True
    except Exception:
        return False


def provider_attempt(provider, text, output_path, voice, speed, config):
    if provider not in PROVIDERS:
        raise RuntimeError(f"Unsupported provider: {provider}")

    if provider_is_paid(provider) and not paid_allowed(config):
        raise RuntimeError(
            f"{provider} skipped: paid/uncertain-cost APIs are disabled. "
            "Set TTS_ALLOW_PAID=true only after approving possible charges."
        )

    raw_file = output_path.with_name(output_path.stem + ".raw")
    raw_file.unlink(missing_ok=True)

    try:
        if provider == "edge_tts":
            PROVIDERS[provider](text, raw_file, voice, speed)
        else:
            PROVIDERS[provider](text, raw_file, config)

        if not raw_file.is_file() or raw_file.stat().st_size < 100:
            raise RuntimeError(f"{provider} produced no usable audio.")

        normalize_to_mp3(raw_file, output_path)
        return validate_mp3(output_path)

    finally:
        raw_file.unlink(missing_ok=True)


# ------------------------------------------------------------
# CHUNK GENERATION
# ------------------------------------------------------------

def generate_scene_chunks(
    scene, providers, manifest, manifest_jobs, config,
    max_chars, min_chars, voice, speed,
):
    part, number = scene_key(scene)
    text = re.sub(r"\s+", " ", scene["text"]).strip()
    chunks = split_text(text, max_chars, min_chars)
    scene_hash = sha256_text(text)

    settings_payload = {
        "provider_order": providers,
        "voice": voice,
        "speed": speed,
        "paid_allowed": paid_allowed(config),
        "max_chars": max_chars,
        "min_chars": min_chars,
        "fish_model": os.environ.get("FISH_TTS_MODEL", "s2.1-pro-free"),
        "gemini_model": os.environ.get(
            "GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts"
        ),
        "gemini_voice": os.environ.get("GEMINI_TTS_VOICE", "Kore"),
        "elevenlabs_model": os.environ.get(
            "ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"
        ),
        "cartesia_model": os.environ.get("CARTESIA_TTS_MODEL", "sonic-2"),
        "minimax_model": os.environ.get(
            "MINIMAX_TTS_MODEL", "speech-2.8-turbo"
        ),
        "minimax_voice": os.environ.get(
            "MINIMAX_TTS_VOICE_ID", "English_expressive_narrator"
        ),
        "finevoice_voice": secret("FINEVOICE_VOICE_ID"),
        "piper_model": secret("PIPER_MODEL_PATH"),
    }

    settings_hash = sha256_text(
        json.dumps(settings_payload, sort_keys=True)
    )

    key = (part, number)
    old_job = manifest_jobs.get(key, {})
    old_chunks = (
        old_job.get("chunks", {}) if isinstance(old_job, dict) else {}
    )

    if not isinstance(old_chunks, dict):
        old_chunks = {}

    scene_record = {
        "part": part,
        "scene": number,
        "status": "in_progress",
        "text_sha256": scene_hash,
        "output": str(scene_audio_path(part, number)),
        "chunk_count": len(chunks),
        "chunks": old_chunks,
        "started_or_resumed_at": utc_now(),
    }

    manifest_jobs[key] = scene_record
    save_manifest(manifest, manifest_jobs)

    chunk_files = []

    for chunk_index, chunk_text in enumerate(chunks, start=1):
        chunk_key = f"{chunk_index:04d}"
        chunk_hash = sha256_text(chunk_text)
        output_path = chunk_audio_path(part, number, chunk_index)
        previous = old_chunks.get(chunk_key, {})

        if chunk_is_valid(
            previous, output_path, chunk_hash, settings_hash
        ):
            print(
                f"  Reusing saved chunk {chunk_index}/{len(chunks)} "
                f"(provider={previous.get('provider', 'unknown')})"
            )
            chunk_files.append(output_path)
            continue

        output_path.unlink(missing_ok=True)
        succeeded = False
        errors = []

        for provider in providers:
            if provider_is_paid(provider) and not paid_allowed(config):
                print(f"  SKIP {provider}: paid APIs disabled.")
                continue

            print(
                f"  Chunk {chunk_index}/{len(chunks)} "
                f"trying provider: {provider}"
            )

            try:
                duration = provider_attempt(
                    provider,
                    chunk_text,
                    output_path,
                    voice,
                    speed,
                    config,
                )

                record = {
                    "status": "completed",
                    "provider": provider,
                    "text_sha256": chunk_hash,
                    "settings_sha256": settings_hash,
                    "duration": duration,
                    "size_bytes": output_path.stat().st_size,
                    "completed_at": utc_now(),
                }

                old_chunks[chunk_key] = record
                scene_record["chunks"] = old_chunks
                scene_record["status"] = "in_progress"
                manifest_jobs[key] = scene_record
                save_manifest(manifest, manifest_jobs)

                print(
                    f"  Saved chunk {chunk_index}: "
                    f"{output_path.stat().st_size} bytes"
                )

                chunk_files.append(output_path)
                succeeded = True
                break

            except Exception as exc:
                output_path.unlink(missing_ok=True)
                message = str(exc)
                errors.append(f"{provider}: {message}")

                old_chunks[chunk_key] = {
                    "status": "failed",
                    "provider": provider,
                    "text_sha256": chunk_hash,
                    "settings_sha256": settings_hash,
                    "error": message[:700],
                    "failed_at": utc_now(),
                }

                scene_record["chunks"] = old_chunks
                manifest_jobs[key] = scene_record
                save_manifest(manifest, manifest_jobs)

                print(f"  Provider failed: {message[:250]}")

        if not succeeded:
            scene_record["status"] = "incomplete"
            scene_record["failed_chunk"] = chunk_index
            scene_record["errors"] = errors[-10:]
            manifest_jobs[key] = scene_record
            manifest["status"] = "incomplete"
            manifest["failed_scene"] = {
                "part": part,
                "scene": number,
                "chunk": chunk_index,
            }
            save_manifest(manifest, manifest_jobs)

            raise SystemExit(
                f"ERROR: Part {part} Scene {number} Chunk {chunk_index} "
                "failed on all allowed providers. Saved chunks remain "
                "checkpointed. Fix credentials/settings and rerun."
            )

    final_path = scene_audio_path(part, number)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = final_path.with_suffix(".concat.txt")
    temporary = final_path.with_name(final_path.stem + ".tmp.mp3")

    try:
        for path in chunk_files:
            validate_mp3(path)

        with list_file.open("w", encoding="utf-8") as file:
            for path in chunk_files:
                escaped = str(path.resolve()).replace("'", "'\\''")
                file.write(f"file '{escaped}'\n")

        run_command([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-vn", "-ac", "1", "-ar", "24000",
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(temporary),
        ], timeout=300)

        duration = validate_mp3(temporary)
        os.replace(temporary, final_path)

        scene_record.update({
            "status": "completed",
            "duration": duration,
            "size_bytes": final_path.stat().st_size,
            "completed_at": utc_now(),
            "chunk_count": len(chunk_files),
        })

        scene_record.pop("failed_chunk", None)
        scene_record.pop("errors", None)
        manifest_jobs[key] = scene_record
        save_manifest(manifest, manifest_jobs)

        print(
            f"Completed Part {part} Scene {number}: "
            f"{final_path} ({duration:.2f}s)"
        )

    finally:
        list_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)

    return final_path


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    print("=" * 60)
    print("KATHA LOK AI — CHUNKED TTS")
    print("=" * 60)

    config = load_input_config()
    fmt = normalize_format(config)
    parts = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")
    expected_total = parts * scenes_per_part

    if not NARRATION_FILE.is_file():
        raise SystemExit("ERROR: Run narration generation first.")

    narration = load_json(NARRATION_FILE)

    if narration.get("status") != "completed":
        raise SystemExit("ERROR: Narration is not marked completed.")

    scenes = normalize_scenes(narration, expected_total)
    voice_setting = cfg_text(config, "VOICE", "male")
    edge_voice = resolve_edge_voice(voice_setting)
    speed = cfg_text(config, "SPEED", "+0%")

    max_chars = positive_int(
        config.get("TTS_CHUNK_MAX_CHARS", 700),
        "TTS_CHUNK_MAX_CHARS",
        default=700,
    )

    min_chars = positive_int(
        config.get("TTS_CHUNK_MIN_CHARS", 150),
        "TTS_CHUNK_MIN_CHARS",
        default=150,
    )

    if min_chars >= max_chars:
        raise SystemExit(
            "ERROR: TTS_CHUNK_MIN_CHARS must be smaller than "
            "TTS_CHUNK_MAX_CHARS."
        )

    providers = get_provider_order(config)

    if not providers:
        raise SystemExit("ERROR: No TTS providers configured.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    manifest_jobs = make_manifest_map(manifest)

    manifest.update({
        "status": "in_progress",
        "format": fmt,
        "voice": voice_setting,
        "edge_voice": edge_voice,
        "speed": speed,
        "provider_order": providers,
        "paid_cloud_allowed": paid_allowed(config),
        "chunk_max_chars": max_chars,
        "chunk_min_chars": min_chars,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(scenes),
        "started_or_resumed_at": utc_now(),
    })

    save_manifest(manifest, manifest_jobs)

    print(f"Parts/scenes : {parts} / {scenes_per_part}")
    print(f"Provider order: {' -> '.join(providers)}")
    print(f"Paid cloud APIs allowed: {paid_allowed(config)}")
    print(f"Chunk size: {min_chars}-{max_chars} characters")
    print(f"Voice: {voice_setting}")
    print(
        "Resume: reuse only validated chunks with matching text "
        "and TTS settings"
    )
    print("-" * 60)

    completed_scenes = 0

    for index, scene in enumerate(scenes, start=1):
        part, number = scene_key(scene)

        print(
            f"\n[{index}/{len(scenes)}] "
            f"Part {part} Scene {number}"
        )

        generate_scene_chunks(
            scene=scene,
            providers=providers,
            manifest=manifest,
            manifest_jobs=manifest_jobs,
            config=config,
            max_chars=max_chars,
            min_chars=min_chars,
            voice=edge_voice,
            speed=speed,
        )

        completed_scenes += 1

    missing = []

    for scene in scenes:
        part, number = scene_key(scene)
        path = scene_audio_path(part, number)

        try:
            validate_mp3(path)
        except Exception:
            missing.append((part, number))

    if missing:
        manifest["status"] = "incomplete"
        manifest["missing_scenes"] = missing
        save_manifest(manifest, manifest_jobs)
        raise SystemExit(f"ERROR: Final audio validation failed: {missing}")

    manifest["status"] = "completed"
    manifest["completed_scenes"] = completed_scenes
    manifest["completed_at"] = utc_now()
    manifest.pop("failed_scene", None)
    manifest.pop("missing_scenes", None)
    save_manifest(manifest, manifest_jobs)

    print("\n" + "=" * 60)
    print("TTS COMPLETED")
    print(f"Scenes: {completed_scenes}/{len(scenes)}")
    print(f"Manifest: {MANIFEST_FILE}")
    print(f"Scene audio: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()