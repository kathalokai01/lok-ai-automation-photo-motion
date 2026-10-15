
#!/usr/bin/env python3
"""Scene-wise resumable TTS with strict billing gates and validated audio."""

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
NARRATION_FILE = OUT / "narration.json"
AUDIO_DIR = OUT / "audio"
CHUNK_DIR = AUDIO_DIR / "_chunks"
MANIFEST_FILE = OUT / "audio_jobs.json"
CONFIG_FILE = ROOT / "Input" / "topic.txt"

VERSION = "generate-tts-v7-strict-billing-mime-resume"

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

POTENTIALLY_BILLABLE = {
    "fish_audio",
    "gemini_tts",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
}

ENV_SETTINGS = (
    "FISH_TTS_MODEL",
    "FISH_TTS_URL",
    "FISH_REFERENCE_ID",
    "FISH_AUDIO_REFERENCE_ID",
    "GEMINI_TTS_MODEL",
    "GEMINI_TTS_VOICE",
    "ELEVENLABS_TTS_MODEL",
    "ELEVENLABS_VOICE_ID",
    "CARTESIA_TTS_MODEL",
    "CARTESIA_VOICE_ID",
    "CARTESIA_TTS_URL",
    "CARTESIA_API_VERSION",
    "FINEVOICE_VOICE_ID",
    "FINEVOICE_TTS_URL",
    "MINIMAX_TTS_MODEL",
    "MINIMAX_TTS_VOICE_ID",
    "MINIMAX_TTS_URL",
    "PIPER_MODEL_PATH",
    "PIPER_SPEAKER",
    "TTS_EDGE_VOICE",
    "TTS_EDGE_RATE",
)

CACHE_VERSION = 7


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def truthy(value):
    if isinstance(value, bool):
        return value

    return str(value).strip().lower() in {
        "true", "yes", "1", "on", "enabled"
    }


def secret(*names):
    for name in names:
        value = os.environ.get(name, "").strip()

        if value:
            return value

    return ""


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def run(command, input_text=None, timeout=240):
    result = subprocess.run(
        [str(item) for item in command],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )

    if result.returncode:
        detail = result.stderr or result.stdout or "Command failed"
        raise RuntimeError(detail[-1600:])

    return result


def audio_info(path):
    path = Path(path)

    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"Audio missing or too small: {path}")

    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed")

    result = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json",
        str(path),
    ], timeout=60)

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            stream.get("codec_type") == "audio"
            for stream in data.get("streams", [])
        )

    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid audio metadata: {path}"
        ) from exc

    if duration <= 0 or not has_audio:
        raise RuntimeError(f"Invalid audio stream: {path}")

    return duration


def normalize_audio(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(
        destination.stem + ".normalize.tmp.mp3"
    )

    try:
        run([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(source),
            "-vn",
            "-ac", "1",
            "-ar", "24000",
            "-codec:a", "libmp3lame",
            "-b:a", "96k",
            str(temporary),
        ], timeout=300)

        audio_info(temporary)
        os.replace(temporary, destination)

    finally:
        temporary.unlink(missing_ok=True)

    return audio_info(destination)


def write_bytes(data, path):
    if not isinstance(data, bytes) or len(data) < 100:
        raise RuntimeError("Provider returned empty or invalid audio bytes")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def post_json(url, headers, payload, timeout=150):
    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request, timeout=timeout
        ) as response:
            data = response.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code}: {detail[:500]}"
        ) from exc

    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            f"Provider request failed: {exc}"
        ) from exc

    if len(data) < 100:
        raise RuntimeError("Provider returned an empty response")

    return data


def decode_audio(value, path, encoding="base64"):
    if not isinstance(value, str) or not value:
        raise RuntimeError("Provider response contains no audio")

    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]

    try:
        raw = (
            bytes.fromhex(value)
            if encoding == "hex"
            else base64.b64decode(value, validate=True)
        )

    except (ValueError, TypeError) as exc:
        raise RuntimeError("Audio decoding failed") from exc

    write_bytes(raw, path)


def wrap_pcm_as_wav(raw, path, sample_rate):
    """Wrap mono 16-bit PCM bytes in a WAV container."""
    if len(raw) < 100 or len(raw) % 2:
        raise RuntimeError("Gemini PCM payload is malformed")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(raw)


def edge_tts(text, destination, config):
    if not shutil.which("edge-tts"):
        raise RuntimeError("edge-tts is not installed")

    voice = str(
        config.get("TTS_EDGE_VOICE", config.get("VOICE", "male"))
    ).strip()

    if voice.lower() in {"female", "woman", "f"}:
        voice = "hi-IN-SwaraNeural"
    elif voice.lower() in {"male", "man", "m"}:
        voice = "hi-IN-MadhurNeural"

    rate = str(
        config.get("TTS_EDGE_RATE", config.get("SPEED", "+0%"))
    )

    temporary = Path(destination).with_name(
        Path(destination).stem + ".edge.tmp.mp3"
    )

    try:
        run([
            "edge-tts",
            "--voice", voice,
            "--rate", rate,
            "--text", text,
            "--write-media", str(temporary),
        ], timeout=240)

        audio_info(temporary)
        shutil.copyfile(temporary, destination)

    finally:
        temporary.unlink(missing_ok=True)


def fish_audio(text, destination, config):
    key = secret("FISH_API_KEY", "FISH_AUDIO_API_KEY")

    if not key:
        raise RuntimeError("Fish Audio API key is missing")

    payload = {"text": text, "format": "mp3"}
    reference = secret(
        "FISH_REFERENCE_ID", "FISH_AUDIO_REFERENCE_ID"
    )

    if reference:
        payload["reference_id"] = reference

    request = urllib.request.Request(
        secret("FISH_TTS_URL")
        or "https://api.fish.audio/v1/tts",
        data=json.dumps(
            payload, ensure_ascii=False
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
            request, timeout=150
        ) as response:
            data = response.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Fish Audio HTTP {exc.code}: {detail[:400]}"
        ) from exc

    write_bytes(data, destination)


def gemini_tts(text, destination, config):
    key = secret(
        "GEMINI_TTS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"
    )
    model = secret("GEMINI_TTS_MODEL")

    if not key or not model:
        raise RuntimeError(
            "Gemini TTS key or GEMINI_TTS_MODEL is missing"
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
                    "Read the following narration aloud in Hindi. "
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

    raw_response = post_json(url, {}, payload)

    try:
        data = json.loads(raw_response.decode("utf-8"))

        inline = next(
            part["inlineData"]
            for part in data["candidates"][0]["content"]["parts"]
            if isinstance(part, dict)
            and part.get("inlineData", {}).get("data")
        )

        encoded = inline["data"]
        mime = str(
            inline.get(
                "mimeType",
                "audio/L16;codec=pcm;rate=24000",
            )
        ).lower()

    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        StopIteration,
        TypeError,
    ) as exc:
        raise RuntimeError(
            "Gemini response contains no usable inline audio"
        ) from exc

    try:
        raw = base64.b64decode(encoded, validate=True)

    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "Gemini audio base64 is invalid"
        ) from exc

    if mime.startswith("audio/l16") or "pcm" in mime:
        match = re.search(r"rate\s*=\s*(\d+)", mime)
        sample_rate = int(match.group(1)) if match else 24000

        wrap_pcm_as_wav(raw, destination, sample_rate)

    else:
        write_bytes(raw, destination)


def elevenlabs(text, destination, config):
    key = secret("ELEVENLABS_API_KEY")
    voice = secret("ELEVENLABS_VOICE_ID")

    if not key or not voice:
        raise RuntimeError("ElevenLabs key or voice ID missing")

    model = (
        secret("ELEVENLABS_TTS_MODEL")
        or "eleven_multilingual_v2"
    )

    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + urllib.parse.quote(voice, safe="")
        + "?output_format=mp3_44100_128"
    )

    data = post_json(
        url,
        {"xi-api-key": key},
        {"text": text, "model_id": model},
    )

    write_bytes(data, destination)


def cartesia(text, destination, config):
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

    headers = {
        "Authorization": f"Bearer {key}",
        "Cartesia-Version": (
            secret("CARTESIA_API_VERSION") or "2025-04-16"
        ),
    }

    data = post_json(
        secret("CARTESIA_TTS_URL")
        or "https://api.cartesia.ai/tts/bytes",
        headers,
        payload,
    )

    write_bytes(data, destination)


def finevoice(text, destination, config):
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
        write_bytes(data, destination)
        return

    encoded = result.get("audio") or result.get("audio_base64")

    if not encoded:
        raise RuntimeError("FineVoice response has no audio field")

    decode_audio(encoded, destination)


def minimax(text, destination, config):
    key = secret("MINIMAX_API_KEY")

    if not key:
        raise RuntimeError("MiniMax API key is missing")

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
        secret("MINIMAX_TTS_URL")
        or "https://api.minimax.io/v1/t2a_v2",
        {"Authorization": f"Bearer {key}"},
        payload,
    )

    try:
        data = json.loads(response.decode("utf-8"))

    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "MiniMax returned invalid JSON"
        ) from exc

    base_resp = data.get("base_resp", {})

    if base_resp.get("status_code", 0) != 0:
        raise RuntimeError(
            f"MiniMax rejected request: {base_resp}"
        )

    try:
        decode_audio(data["data"]["audio"], destination, "hex")

    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "MiniMax response has no valid audio"
        ) from exc


def piper(text, destination, config):
    executable = shutil.which("piper")
    model = (
        secret("PIPER_MODEL_PATH")
        or str(config.get("PIPER_MODEL_PATH", "")).strip()
    )

    if (
        not executable
        or not model
        or not Path(model).is_file()
    ):
        raise RuntimeError("Piper executable/model is unavailable")

    command = [
        executable,
        "--model", model,
        "--output_file", str(destination),
    ]

    speaker = secret("PIPER_SPEAKER")

    if speaker:
        command.extend(["--speaker", speaker])

    run(command, input_text=text, timeout=300)
    audio_info(destination)


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
    raw = str(value or "").strip().lower()

    aliases = {
        "fish": "fish_audio",
        "fish audio": "fish_audio",
        "fish-audio": "fish_audio",
        "gemini": "gemini_tts",
        "gemini tts": "gemini_tts",
        "edge": "edge_tts",
        "edge tts": "edge_tts",
        "piper tts": "piper",
        "eleven labs": "elevenlabs",
        "elevenlabs tts": "elevenlabs",
        "mini max": "minimax",
        "fine voice": "finevoice",
    }

    return aliases.get(
        raw,
        aliases.get(raw.replace("-", "_"), raw.replace("-", "_")),
    )


def provider_groups(config):
    defaults = ("fish_audio", "gemini_tts", "edge_tts")

    primary = [
        normalize_provider(
            config.get(f"TTS_MAIN_{index}", defaults[index - 1])
        )
        for index in (1, 2, 3)
    ]

    if (
        len(set(primary)) != 3
        or any(provider not in PROVIDERS for provider in primary)
    ):
        raise RuntimeError(
            "TTS_MAIN_1/2/3 must be three different supported providers"
        )

    if not truthy(config.get("TTS_FALLBACK_ENABLED", True)):
        return primary, []

    raw = config.get(
        "TTS_FALLBACK_ORDER",
        ",".join(PROVIDERS),
    )

    fallback = []

    values = (
        raw
        if isinstance(raw, (list, tuple))
        else str(raw).replace(";", ",").split(",")
    )

    for item in values:
        provider = normalize_provider(item)

        if provider and provider not in PROVIDERS:
            raise RuntimeError(
                f"Unsupported fallback provider: {item}"
            )

        if (
            provider
            and provider not in primary
            and provider not in fallback
        ):
            fallback.append(provider)

    fallback.extend(
        provider for provider in PROVIDERS
        if provider not in primary and provider not in fallback
    )

    return primary, fallback


def paid_allowed(config):
    # The workflow's ALLOW_BILLABLE_PROVIDERS=false is a hard stop.
    # Paid cloud providers require explicit opt-in in all three places.
    return (
        truthy(config.get("TTS_ALLOW_PAID", False))
        and truthy(os.environ.get("TTS_ALLOW_PAID", "false"))
        and truthy(os.environ.get("ALLOW_BILLABLE_PROVIDERS", "false"))
    )


def split_text(text, limit):
    text = " ".join(str(text).split())
    chunks = []

    while len(text) > limit:
        boundaries = [
            text.rfind(mark, 0, limit)
            for mark in ("।", "!", "?", ".", " ")
        ]

        boundary = max(boundaries)

        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1

        chunks.append(text[:boundary].strip())
        text = text[boundary:].strip()

    if text:
        chunks.append(text)

    return chunks


def scene_key(row):
    try:
        part = int(row["part"])
        scene = int(row.get("scene", row.get("local_scene")))

    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid scene mapping: {row}"
        ) from exc

    if part < 1 or scene < 1:
        raise RuntimeError(f"Invalid scene mapping: {row}")

    return part, scene


def rel(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def audio_path(part, scene):
    return AUDIO_DIR / f"part_{part:02d}" / f"scene_{scene:02d}.mp3"


def chunk_path(part, scene, index):
    return (
        CHUNK_DIR
        / f"part_{part:02d}"
        / f"scene_{scene:02d}"
        / f"chunk_{index:04d}.mp3"
    )


def load_manifest():
    try:
        data = read_json(MANIFEST_FILE)

        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            return data

    except Exception as exc:
        print(f"WARNING: ignoring invalid TTS checkpoint: {exc}")

    return {"status": "in_progress", "jobs": []}


def checkpoint(manifest, jobs, status="in_progress", **extra):
    save_json(MANIFEST_FILE, {
        **manifest,
        **extra,
        "version": VERSION,
        "status": status,
        "jobs": jobs,
        "updated_at": now(),
    })


def generate_chunk(
    text,
    destination,
    config,
    primary,
    fallback,
    allow_paid,
    part,
    scene,
    index,
    total,
):
    errors = []

    for group, providers in (
        ("primary", primary),
        ("fallback", fallback),
    ):
        for provider in providers:
            if provider in POTENTIALLY_BILLABLE and not allow_paid:
                errors.append(
                    f"{provider}: blocked by strict free-only billing gate"
                )
                print("SKIP", provider, "(paid/cloud opt-in disabled)")
                continue

            raw = destination.with_suffix(".raw")
            raw.unlink(missing_ok=True)

            print(
                f"Part {part} Scene {scene} "
                f"Chunk {index}/{total}: {provider}"
            )

            try:
                GENERATORS[provider](text, raw, config)
                audio_info(raw)
                normalize_audio(raw, destination)

                return provider, {
                    "status": "completed",
                    "provider": provider,
                    "provider_group": group,
                    "updated_at": now(),
                }

            except Exception as exc:
                errors.append(f"{provider}: {str(exc)[:350]}")
                print("FAILED", provider, str(exc)[:220])

            finally:
                raw.unlink(missing_ok=True)

    raise RuntimeError(
        f"No TTS provider succeeded for part {part}, scene {scene}, "
        f"chunk {index}. " + " | ".join(errors[-10:])
    )


def main():
    config = load_and_validate(CONFIG_FILE)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    primary, fallback = provider_groups(config)
    allow_paid = paid_allowed(config)

    chunk_limit = max(
        150,
        min(int(config.get("TTS_CHUNK_MAX_CHARS", 500)), 1500),
    )

    print("TTS version:", VERSION)
    print("Primary:", " -> ".join(primary))
    print(
        "Fallback:",
        " -> ".join(fallback) if fallback else "disabled",
    )
    print("Potentially billable providers enabled:", allow_paid)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg and ffprobe are required")

    if not NARRATION_FILE.is_file():
        raise RuntimeError(f"Narration missing: {NARRATION_FILE}")

    narration = read_json(NARRATION_FILE)

    if (
        not isinstance(narration, dict)
        or narration.get("status") != "completed"
    ):
        raise RuntimeError("narration.json must have status=completed")

    rows = narration.get("scenes")

    if not isinstance(rows, list):
        raise RuntimeError("narration.json must contain a scenes list")

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }

    texts = {}

    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid narration scene row")

        key = scene_key(row)

        if key in texts:
            raise RuntimeError(f"Duplicate narration scene: {key}")

        text = str(row.get("text", row.get("narration", ""))).strip()

        if not text:
            raise RuntimeError(f"Empty narration text for scene {key}")

        texts[key] = text

    if set(texts) != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
            f"missing={sorted(expected - set(texts))[:12]}, "
            f"extra={sorted(set(texts) - expected)[:12]}"
        )

    manifest = load_manifest()
    old_jobs = {}

    for job in manifest.get("jobs", []):
        if isinstance(job, dict):
            try:
                old_jobs[scene_key(job)] = job
            except RuntimeError:
                pass

    settings_env = {
        key: os.environ.get(key, "")
        for key in ENV_SETTINGS
    }

    model_path = (
        settings_env.get("PIPER_MODEL_PATH")
        or str(config.get("PIPER_MODEL_PATH", "")).strip()
    )

    settings_env["PIPER_MODEL_SHA256"] = (
        file_sha(model_path)
        if model_path and Path(model_path).is_file()
        else ""
    )

    settings = {
        "cache_version": CACHE_VERSION,
        "primary": primary,
        "fallback": fallback,
        "paid_allowed": allow_paid,
        "voice": str(config.get("VOICE", "male")),
        "speed": str(config.get("SPEED", "+0%")),
        "chunk_limit": chunk_limit,
        "env": settings_env,
        "edge_voice": str(config.get("TTS_EDGE_VOICE", "")),
        "edge_rate": str(config.get("TTS_EDGE_RATE", "")),
        "piper_model_config": str(config.get("PIPER_MODEL_PATH", "")),
        "script_sha256": file_sha(__file__),
    }

    settings_hash = sha(json.dumps(
        settings, sort_keys=True, ensure_ascii=False
    ))

    narration_hash = sha(json.dumps([
        {
            "part": part,
            "scene": scene,
            "text_sha256": sha(text),
        }
        for (part, scene), text in sorted(texts.items())
    ], sort_keys=True, ensure_ascii=False))

    manifest.update({
        "version": VERSION,
        "cache_version": CACHE_VERSION,
        "format": str(config.get("FORMAT", "full")).lower(),
        "provider_order": primary + fallback,
        "primary_provider_order": primary,
        "fallback_provider_order": fallback,
        "paid_cloud_allowed": allow_paid,
        "settings_sha256": settings_hash,
        "narration_sha256": narration_hash,
        "total_parts": parts,
        "scenes_per_part": scenes,
        "total_scenes": len(expected),
        "audio_root": AUDIO_DIR.relative_to(ROOT).as_posix(),
    })

    new_jobs = []

    for part, scene in sorted(expected):
        text = texts[(part, scene)]
        text_hash = sha(text)
        destination = audio_path(part, scene)
        destination_rel = destination.relative_to(ROOT).as_posix()
        previous = old_jobs.get((part, scene), {})

        if (
            previous.get("status") == "completed"
            and previous.get("text_sha256") == text_hash
            and previous.get("settings_sha256") == settings_hash
            and previous.get("output") == destination_rel
            and destination.is_file()
        ):
            try:
                duration = audio_info(destination)
                job = {
                    **previous,
                    "duration": duration,
                    "status": "completed",
                }

                new_jobs.append(job)
                print(f"REUSE part {part} scene {scene}")

                checkpoint(
                    manifest, new_jobs,
                    completed_scenes=len(new_jobs),
                )
                continue

            except Exception as exc:
                print(
                    "WARNING: cached final audio invalid; rebuilding:",
                    exc,
                )

        chunks = split_text(text, chunk_limit)
        previous_chunks = previous.get("chunks", {})

        if not isinstance(previous_chunks, dict):
            previous_chunks = {}

        destination.parent.mkdir(parents=True, exist_ok=True)
        chunk_files = []
        used_providers = []
        chunk_records = {}

        for index, chunk in enumerate(chunks, start=1):
            cached = chunk_path(part, scene, index)
            chunk_hash = sha(chunk)
            old = previous_chunks.get(str(index), {})

            if (
                old.get("status") == "completed"
                and old.get("text_sha256") == chunk_hash
                and old.get("settings_sha256") == settings_hash
                and cached.is_file()
            ):
                try:
                    audio_info(cached)
                    chunk_files.append(cached)
                    used_providers.append(old.get("provider", "cached"))
                    chunk_records[str(index)] = old

                    print(
                        f"REUSE part {part} scene {scene} chunk {index}"
                    )
                    continue

                except Exception:
                    pass

            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.unlink(missing_ok=True)

            try:
                provider, record = generate_chunk(
                    chunk,
                    cached,
                    config,
                    primary,
                    fallback,
                    allow_paid,
                    part,
                    scene,
                    index,
                    len(chunks),
                )

            except Exception as exc:
                incomplete = {
                    "part": part,
                    "scene": scene,
                    "status": "incomplete",
                    "text_sha256": text_hash,
                    "settings_sha256": settings_hash,
                    "output": destination_rel,
                    "chunks": chunk_records,
                    "error": str(exc)[:1800],
                }

                checkpoint(
                    manifest,
                    new_jobs + [incomplete],
                    status="incomplete",
                    failed_scene={
                        "part": part,
                        "scene": scene,
                        "chunk": index,
                    },
                    completed_scenes=len(new_jobs),
                )

                raise RuntimeError(
                    f"TTS failed at part {part}, scene {scene}, "
                    f"chunk {index}; prior successful chunks remain "
                    "checkpointed"
                ) from exc

            record.update({
                "text_sha256": chunk_hash,
                "settings_sha256": settings_hash,
            })

            chunk_records[str(index)] = record
            chunk_files.append(cached)
            used_providers.append(provider)

            in_progress = {
                "part": part,
                "scene": scene,
                "status": "in_progress",
                "text_sha256": text_hash,
                "settings_sha256": settings_hash,
                "output": destination_rel,
                "chunks": chunk_records,
            }

            checkpoint(
                manifest,
                new_jobs + [in_progress],
                completed_scenes=len(new_jobs),
                current_scene={"part": part, "scene": scene},
            )

        concat = destination.with_suffix(".concat.txt")
        temporary = destination.with_name(
            destination.stem + ".tmp.mp3"
        )

        try:
            with concat.open("w", encoding="utf-8") as handle:
                for chunk_file in chunk_files:
                    escaped = str(chunk_file.resolve()).replace(
                        "'", "'\\''"
                    )
                    handle.write(f"file '{escaped}'\n")

            run([
                "ffmpeg", "-y", "-v", "error",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat),
                "-vn",
                "-ac", "1",
                "-ar", "24000",
                "-codec:a", "libmp3lame",
                "-b:a", "96k",
                str(temporary),
            ], timeout=300)

            duration = audio_info(temporary)
            os.replace(temporary, destination)

        finally:
            concat.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

        job = {
            "part": part,
            "scene": scene,
            "status": "completed",
            "text_sha256": text_hash,
            "settings_sha256": settings_hash,
            "output": destination_rel,
            "duration": duration,
            "providers": used_providers,
            "chunks": chunk_records,
            "updated_at": now(),
        }

        new_jobs.append(job)

        checkpoint(
            manifest,
            new_jobs,
            completed_scenes=len(new_jobs),
        )

        print(
            f"DONE part {part} scene {scene}: {duration:.2f}s"
        )

    if (
        len(new_jobs) != len(expected)
        or any(job.get("status") != "completed" for job in new_jobs)
    ):
        checkpoint(
            manifest,
            new_jobs,
            status="incomplete",
            completed_scenes=len(new_jobs),
        )
        raise RuntimeError("Not all narration scenes have valid audio")

    checkpoint(
        manifest,
        new_jobs,
        status="completed",
        completed_scenes=len(new_jobs),
        completed_at=now(),
    )

    print(
        f"TTS completed: {len(new_jobs)} scenes; "
        f"manifest: {MANIFEST_FILE}"
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("TTS cancelled", file=sys.stderr)
        sys.exit(130)

    except Exception as exc:
        print(f"TTS FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
