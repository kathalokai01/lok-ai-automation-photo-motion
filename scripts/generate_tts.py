
#!/usr/bin/env python3
"""Resumable scene-wise TTS with configurable providers and billing permission."""

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

VERSION = "generate-tts-v10-billing-gate-resume-safe"
MIN_AUDIO_BYTES = 100

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
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


def sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest(value):
    return sha_text(json.dumps(
        value, sort_keys=True, ensure_ascii=False
    ))


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def secret(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def run(command, input_text=None, timeout=240):
    result = subprocess.run(
        [str(x) for x in command],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            (result.stderr or result.stdout or "Command failed")[-1200:]
        )
    return result


def audio_info(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_AUDIO_BYTES:
        raise RuntimeError(f"Audio missing or too small: {path.name}")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    result = run([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ], timeout=60)

    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    has_audio = any(
        s.get("codec_type") == "audio"
        for s in data.get("streams", [])
    )
    if duration <= 0 or not has_audio:
        raise RuntimeError(f"Invalid audio stream: {path.name}")
    return duration


def normalize_audio(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.mp3")

    try:
        run([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", "24000",
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(temp),
        ], timeout=300)
        audio_info(temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)

    return audio_info(destination)


def write_bytes(data, path):
    if not isinstance(data, bytes) or len(data) < MIN_AUDIO_BYTES:
        raise RuntimeError("Provider returned unusable audio")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def post_json(url, headers, payload, timeout=150):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:400]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Request failed: {exc}") from exc

    if len(data) < MIN_AUDIO_BYTES:
        raise RuntimeError("Empty provider response")
    return data


def decode_audio(encoded, path, encoding="base64"):
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("Provider audio field is missing")
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        data = (
            bytes.fromhex(encoded)
            if encoding == "hex"
            else base64.b64decode(encoded, validate=True)
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Audio decoding failed") from exc
    write_bytes(data, path)


def wrap_pcm(raw, path, sample_rate=24000):
    if len(raw) < MIN_AUDIO_BYTES or len(raw) % 2:
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

    payload = {"text": text, "format": "mp3"}
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
        raise RuntimeError(f"Fish Audio HTTP {exc.code}: {detail[:350]}") from exc


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
                    "Read this narration aloud in Hindi. Do not translate, "
                    "summarize, or add words:\n" + text
                )
            }]
        }],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }

    response = post_json(url, {}, payload)
    try:
        data = json.loads(response.decode("utf-8"))
        inline = next(
            p["inlineData"]
            for p in data["candidates"][0]["content"]["parts"]
            if isinstance(p, dict)
            and p.get("inlineData", {}).get("data")
        )
        raw = base64.b64decode(inline["data"], validate=True)
        mime = str(inline.get("mimeType", "audio/L16;rate=24000")).lower()
    except (
        UnicodeDecodeError, json.JSONDecodeError, KeyError,
        IndexError, StopIteration, TypeError, ValueError,
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
    temp = path.with_name(path.stem + ".edge.tmp.mp3")
    try:
        run([
            "edge-tts", "--voice", voice, "--rate", rate,
            "--text", text, "--write-media", str(temp),
        ], timeout=240)
        audio_info(temp)
        shutil.copyfile(temp, path)
    finally:
        temp.unlink(missing_ok=True)


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
    audio_info(path)


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
    write_bytes(post_json(
        url, {"xi-api-key": key},
        {"text": text, "model_id": model},
    ), path)


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
            "container": "mp3", "sample_rate": 24000, "encoding": "mp3"
        },
    }
    write_bytes(post_json(
        secret("CARTESIA_TTS_URL") or "https://api.cartesia.ai/tts/bytes",
        {
            "Authorization": f"Bearer {key}",
            "Cartesia-Version": secret("CARTESIA_API_VERSION") or "2025-04-16",
        },
        payload,
    ), path)


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
            "voice_id": secret("MINIMAX_TTS_VOICE_ID")
            or "English_expressive_narrator",
            "speed": 1, "vol": 1, "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000, "bitrate": 128000,
            "format": "mp3", "channel": 1,
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
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
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
        "fish": "fish_audio", "fish audio": "fish_audio",
        "gemini": "gemini_tts", "gemini tts": "gemini_tts",
        "edge": "edge_tts", "edge tts": "edge_tts",
        "piper tts": "piper", "eleven labs": "elevenlabs",
        "fine voice": "finevoice", "mini max": "minimax",
    }
    return aliases.get(raw, raw)


def provider_groups(config):
    defaults = ("fish_audio", "gemini_tts", "edge_tts")
    primary = [
        normalize_provider(config.get(f"TTS_MAIN_{i}", defaults[i - 1]))
        for i in (1, 2, 3)
    ]
    if len(set(primary)) != 3 or any(p not in PROVIDERS for p in primary):
        raise RuntimeError(
            "TTS_MAIN_1/2/3 must be three different supported providers"
        )

    fallback = []
    if truthy(config.get("TTS_FALLBACK_ENABLED", True)):
        raw = config.get("TTS_FALLBACK_ORDER", ",".join(PROVIDERS))
        values = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;]", str(raw))
        for item in values:
            provider = normalize_provider(item)
            if not provider:
                continue
            if provider not in PROVIDERS:
                raise RuntimeError(f"Unsupported fallback provider: {item}")
            if provider not in primary and provider not in fallback:
                fallback.append(provider)

        for provider in PROVIDERS:
            if provider not in primary and provider not in fallback:
                fallback.append(provider)

    return primary, fallback


def paid_allowed(config):
    # Workflow-level explicit opt-in takes precedence over the Input default.
    # This resolves the previous conflict where Input/topic.txt defaulted
    # TTS_ALLOW_PAID=false while the workflow explicitly allowed billable APIs.
    workflow_opt_in = truthy(os.environ.get("ALLOW_BILLABLE_PROVIDERS", "false"))
    input_opt_in = truthy(config.get("TTS_ALLOW_PAID", False))
    env_opt_in = truthy(os.environ.get("TTS_ALLOW_PAID", "false"))
    return workflow_opt_in or (input_opt_in and env_opt_in)


def split_text(text, limit):
    text = " ".join(str(text).split())
    if limit < 100:
        raise RuntimeError("TTS chunk size must be at least 100 characters")

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
    return chunks


def scene_key(row):
    try:
        part = int(row["part"])
        scene = int(row.get("scene", row.get("local_scene")))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid narration scene mapping: {row}") from exc
    if part < 1 or scene < 1:
        raise RuntimeError(f"Invalid narration scene mapping: {row}")
    return part, scene


def rel(path):
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def final_audio_path(part, scene):
    return AUDIO_DIR / f"part_{part:02d}" / f"scene_{scene:02d}.mp3"


def chunk_audio_path(part, scene, index):
    return (
        CHUNK_DIR / f"part_{part:02d}" / f"scene_{scene:02d}"
        / f"chunk_{index:04d}.mp3"
    )


def load_checkpoint():
    try:
        data = read_json(MANIFEST_FILE)
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"jobs": []}


def generate_one_chunk(text, destination, config, primary, fallback, allow_paid):
    errors = []
    raw = Path(destination).with_suffix(".raw")
    raw.parent.mkdir(parents=True, exist_ok=True)

    for group, providers in (("primary", primary), ("fallback", fallback)):
        for provider in providers:
            if provider in BILLABLE and not allow_paid:
                errors.append(f"{provider}: billing permission is disabled")
                continue

            raw.unlink(missing_ok=True)
            try:
                GENERATORS[provider](text, raw, config)
                audio_info(raw)
                normalize_audio(raw, destination)
                return provider, group
            except Exception as exc:
                errors.append(f"{provider}: {str(exc)[:220]}")
            finally:
                raw.unlink(missing_ok=True)

    raise RuntimeError("No TTS provider succeeded: " + " | ".join(errors[-8:]))


def main():
    config = load_and_validate()
    if not isinstance(config, dict):
        raise RuntimeError("Input configuration must be a dictionary")

    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    narration = read_json(NARRATION_FILE)
    if not isinstance(narration, dict) or not isinstance(narration.get("scenes"), list):
        raise RuntimeError("narration.json must contain a scenes list")

    rows = {}
    for row in narration["scenes"]:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid narration scene row")
        key = scene_key(row)
        if key in rows:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        text = str(row.get("narration", row.get("text", ""))).strip()
        if not text:
            raise RuntimeError(f"Empty narration text for scene {key}")
        if str(row.get("status", "completed")).lower() not in {
            "completed", "complete", "ready"
        }:
            raise RuntimeError(f"Narration scene is not completed: {key}")
        rows[key] = text

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }
    if set(rows) != expected:
        raise RuntimeError(
            f"Narration mapping mismatch: missing={sorted(expected-set(rows))}; "
            f"extra={sorted(set(rows)-expected)}"
        )

    primary, fallback = provider_groups(config)
    allow_paid = paid_allowed(config)

    max_chars = int(config.get("TTS_CHUNK_MAX_CHARS", 500))
    settings_hash = digest({
        "version": VERSION,
        "primary": primary,
        "fallback": fallback,
        "paid_allowed": allow_paid,
        "voice": config.get("VOICE", ""),
        "speed": config.get("SPEED", ""),
        "chunk_max_chars": max_chars,
        "provider_settings": {
            key: value for key, value in os.environ.items()
            if key.endswith(("_TTS_MODEL", "_TTS_VOICE", "_VOICE_ID"))
        },
    })

    checkpoint = load_checkpoint()
    old_jobs = {}
    for job in checkpoint.get("jobs", []):
        if isinstance(job, dict):
            try:
                old_jobs[(int(job["part"]), int(job["scene"]))] = job
            except (KeyError, TypeError, ValueError):
                continue

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)

    for part, scene in sorted(expected):
        text = rows[(part, scene)]
        text_hash = sha_text(text)
        destination = final_audio_path(part, scene)
        destination.parent.mkdir(parents=True, exist_ok=True)
        chunks = split_text(text, max_chars)

        signature = digest({
            "text_sha256": text_hash,
            "settings_sha256": settings_hash,
            "chunk_count": len(chunks),
        })
        previous = old_jobs.get((part, scene), {})

        if (
            previous.get("status") == "completed"
            and previous.get("signature") == signature
            and destination.is_file()
        ):
            try:
                duration = audio_info(destination)
                if previous.get("sha256") == file_digest(destination):
                    old_jobs[(part, scene)] = {
                        **previous, "duration": round(duration, 3)
                    }
                    print(f"RESUME scene {part}/{scene}: cached audio")
                    continue
            except Exception:
                pass

        chunk_records = []
        provider_history = []
        chunk_paths = []
        previous_chunks = previous.get("chunks", [])

        for index, chunk in enumerate(chunks, start=1):
            chunk_path = chunk_audio_path(part, scene, index)
            chunk_path.parent.mkdir(parents=True, exist_ok=True)
            chunk_signature = digest({
                "chunk_sha256": sha_text(chunk),
                "settings_sha256": settings_hash,
                "index": index,
            })
            old_chunk = (
                previous_chunks[index - 1]
                if isinstance(previous_chunks, list)
                and len(previous_chunks) >= index
                else {}
            )

            if (
                isinstance(old_chunk, dict)
                and old_chunk.get("status") == "completed"
                and old_chunk.get("signature") == chunk_signature
                and chunk_path.is_file()
            ):
                try:
                    duration = audio_info(chunk_path)
                    if old_chunk.get("sha256") == file_digest(chunk_path):
                        chunk_records.append({
                            **old_chunk, "duration": round(duration, 3)
                        })
                        chunk_paths.append(chunk_path)
                        provider_history.append(old_chunk.get("provider", "unknown"))
                        print(f"RESUME chunk {part}/{scene}/{index}")
                        continue
                except Exception:
                    pass

            temp_chunk = chunk_path.with_suffix(".generating.mp3")
            temp_chunk.unlink(missing_ok=True)

            provider, group = generate_one_chunk(
                chunk, temp_chunk, config, primary, fallback, allow_paid
            )
            duration = audio_info(temp_chunk)
            os.replace(temp_chunk, chunk_path)

            record = {
                "index": index,
                "status": "completed",
                "signature": chunk_signature,
                "sha256": file_digest(chunk_path),
                "output": rel(chunk_path),
                "duration": round(duration, 3),
                "provider": provider,
                "provider_group": group,
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
                "chunks": chunk_records,
                "providers": provider_history,
                "updated_at": now(),
            }
            checkpoint.update({
                "version": VERSION,
                "status": "in_progress",
                "expected_scenes": len(expected),
                "jobs": [
                    old_jobs.get(
                        key, {"part": key[0], "scene": key[1], "status": "pending"}
                    )
                    for key in sorted(expected)
                ],
                "updated_at": now(),
            })
            save_json(MANIFEST_FILE, checkpoint)

        if not chunk_paths:
            raise RuntimeError(f"No audio chunks generated for {part}/{scene}")

        temp_final = destination.with_suffix(".tmp.mp3")
        try:
            if len(chunk_paths) == 1:
                shutil.copyfile(chunk_paths[0], temp_final)
            else:
                concat_file = destination.with_suffix(".concat.txt")
                try:
                    concat_file.write_text(
                        "".join(
                            "file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n"
                            for p in chunk_paths
                        ),
                        encoding="utf-8",
                    )
                    run([
                        "ffmpeg", "-y", "-v", "error",
                        "-f", "concat", "-safe", "0",
                        "-i", str(concat_file),
                        "-vn", "-ac", "1", "-ar", "24000",
                        "-codec:a", "libmp3lame", "-b:a", "96k",
                        str(temp_final),
                    ], timeout=300)
                finally:
                    concat_file.unlink(missing_ok=True)

            audio_info(temp_final)
            os.replace(temp_final, destination)
        finally:
            temp_final.unlink(missing_ok=True)

        duration = audio_info(destination)
        old_jobs[(part, scene)] = {
            "part": part,
            "scene": scene,
            "status": "completed",
            "signature": signature,
            "text_sha256": text_hash,
            "settings_sha256": settings_hash,
            "output": rel(destination),
            "sha256": file_digest(destination),
            "duration": round(duration, 3),
            "providers": provider_history,
            "chunks": chunk_records,
            "updated_at": now(),
        }

        checkpoint.update({
            "version": VERSION,
            "status": "in_progress",
            "expected_scenes": len(expected),
            "jobs": [
                old_jobs.get(
                    key, {"part": key[0], "scene": key[1], "status": "pending"}
                )
                for key in sorted(expected)
            ],
            "updated_at": now(),
        })
        save_json(MANIFEST_FILE, checkpoint)
        print(f"COMPLETED scene {part}/{scene}: {duration:.2f}s")

    final_jobs = [old_jobs[key] for key in sorted(expected)]
    if len(final_jobs) != len(expected) or any(
        job.get("status") != "completed" for job in final_jobs
    ):
        raise RuntimeError("Not all expected narration scenes completed")

    save_json(MANIFEST_FILE, {
        "version": VERSION,
        "status": "completed",
        "expected_scenes": len(expected),
        "jobs": final_jobs,
        "primary_providers": primary,
        "fallback_providers": fallback,
        "paid_providers_allowed": allow_paid,
        "updated_at": now(),
    })

    print("TTS COMPLETE")
    print("Scenes:", len(final_jobs))
    print("Primary providers:", " -> ".join(primary))
    print("Fallback providers:", " -> ".join(fallback))
    print("Billable provider permission:", allow_paid)
    print("Manifest:", MANIFEST_FILE.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("TTS FAILED:", str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
