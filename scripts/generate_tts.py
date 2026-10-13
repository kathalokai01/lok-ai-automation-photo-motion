#!/usr/bin/env python3
"""Katha Lok AI: resumable scene-wise TTS with primary-first fallback."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from input_config import load_and_validate, get_parts, get_scenes
except ImportError:
    from scripts.input_config import load_and_validate, get_parts, get_scenes


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "narration"
NARRATION_FILE = OUT / "narration.json"
AUDIO_DIR = OUT / "audio"
CHUNK_DIR = AUDIO_DIR / "_chunks"
MANIFEST_FILE = OUT / "audio_jobs.json"
CONFIG_FILE = ROOT / "Input" / "topic.txt"

PROVIDERS = (
    "fish_audio", "gemini_tts", "edge_tts", "piper",
    "elevenlabs", "cartesia", "finevoice", "minimax",
)

POTENTIALLY_BILLABLE = {
    "fish_audio", "gemini_tts", "elevenlabs",
    "cartesia", "finevoice", "minimax",
}

PROVIDER_ENV_KEYS = (
    "FISH_TTS_MODEL", "FISH_TTS_URL", "FISH_REFERENCE_ID",
    "FISH_AUDIO_REFERENCE_ID", "GEMINI_TTS_MODEL", "GEMINI_TTS_VOICE",
    "ELEVENLABS_TTS_MODEL", "ELEVENLABS_VOICE_ID",
    "CARTESIA_TTS_MODEL", "CARTESIA_VOICE_ID", "CARTESIA_TTS_URL",
    "CARTESIA_API_VERSION", "FINEVOICE_VOICE_ID", "FINEVOICE_TTS_URL",
    "MINIMAX_TTS_MODEL", "MINIMAX_TTS_VOICE_ID", "MINIMAX_TTS_URL",
    "PIPER_MODEL_PATH", "PIPER_SPEAKER", "TTS_EDGE_VOICE",
    "TTS_EDGE_RATE",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def secret(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def truthy(value):
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


def paid_allowed(config):
    # Cloud/billable providers require explicit permission in BOTH places.
    return (
        truthy(config.get("TTS_ALLOW_PAID", False))
        and truthy(os.environ.get("TTS_ALLOW_PAID", "false"))
    )


def normalize_provider(value):
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
    original = str(value or "").strip().lower()
    normalized = original.replace("-", "_")
    return aliases.get(original, aliases.get(normalized, normalized))


def primary_provider_order(config):
    """Return exactly the three configured primary providers in order."""
    defaults = ("fish_audio", "gemini_tts", "edge_tts")
    primary = [
        normalize_provider(config.get(f"TTS_MAIN_{i}", defaults[i - 1]))
        for i in (1, 2, 3)
    ]

    if len(set(primary)) != 3:
        raise RuntimeError("Choose three different TTS_MAIN_1/2/3 providers.")

    invalid = [p for p in primary if p not in PROVIDERS]
    if invalid:
        raise RuntimeError(f"Unsupported primary TTS provider(s): {invalid}")

    return primary


def provider_groups(config):
    """
    Primary providers are attempted first.
    Fallback providers are attempted only if all eligible primary providers fail.
    """
    primary = primary_provider_order(config)

    if not truthy(config.get("TTS_FALLBACK_ENABLED", True)):
        return primary, []

    raw = config.get("TTS_FALLBACK_ORDER", ",".join(PROVIDERS))
    values = (
        raw if isinstance(raw, (list, tuple))
        else str(raw).replace(";", ",").split(",")
    )

    fallback = []
    for value in values:
        name = normalize_provider(value)
        if not name:
            continue
        if name not in PROVIDERS:
            raise RuntimeError(f"Unsupported fallback provider: {value}")
        if name not in primary and name not in fallback:
            fallback.append(name)

    # Append any provider not explicitly listed so fallback remains robust.
    for name in PROVIDERS:
        if name not in primary and name not in fallback:
            fallback.append(name)

    return primary, fallback


def provider_order(config):
    """Combined ordered list for manifests and cache fingerprints."""
    primary, fallback = provider_groups(config)
    return primary + fallback


def run(command, input_text=None, timeout=240):
    result = subprocess.run(
        [str(x) for x in command],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = result.stderr or result.stdout or "Command failed"
        raise RuntimeError(detail[-1500:])
    return result


def audio_duration(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError(f"Audio missing or empty: {path}")
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed.")

    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ], timeout=60)

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid audio duration: {path}") from exc

    if duration <= 0:
        raise RuntimeError(f"Invalid audio duration: {path}")
    return duration


def normalize_audio(source, destination):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed.")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".normalize.tmp.mp3")

    try:
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "24000",
            "-codec:a", "libmp3lame", "-b:a", "96k", str(temp),
        ], timeout=300)
        audio_duration(temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)

    return audio_duration(destination)


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
        raise RuntimeError(f"HTTP {exc.code}: {detail[:600]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Provider request failed: {exc}") from exc

    if len(data) < 100:
        raise RuntimeError("Provider returned an empty response.")
    return data


def write_bytes(data, destination):
    if not isinstance(data, bytes) or len(data) < 100:
        raise RuntimeError("Provider returned unusable audio bytes.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def decode_audio(value, destination, encoding="base64"):
    if not isinstance(value, str) or not value:
        raise RuntimeError("Provider response contains no audio.")
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    try:
        raw = (
            bytes.fromhex(value)
            if encoding == "hex"
            else base64.b64decode(value, validate=True)
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Provider audio decoding failed.") from exc
    write_bytes(raw, destination)


def edge_tts(text, destination, config):
    if not shutil.which("edge-tts"):
        raise RuntimeError("edge-tts is not installed.")

    voice = str(
        config.get("TTS_EDGE_VOICE", config.get("VOICE", "male"))
    ).strip()

    if voice.lower() in {"female", "woman", "f"}:
        voice = "hi-IN-SwaraNeural"
    elif voice.lower() in {"male", "man", "m"}:
        voice = "hi-IN-MadhurNeural"

    rate = str(config.get("TTS_EDGE_RATE", config.get("SPEED", "+0%")))
    temp = Path(destination).with_suffix(".edge.tmp.mp3")

    try:
        run([
            "edge-tts", "--voice", voice, "--rate", rate,
            "--text", text, "--write-media", str(temp),
        ], timeout=240)
        audio_duration(temp)
        shutil.copyfile(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def fish_audio(text, destination, config):
    key = secret("FISH_API_KEY", "FISH_AUDIO_API_KEY")
    if not key:
        raise RuntimeError("Fish Audio API key is missing.")

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
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Fish Audio HTTP {exc.code}: {detail[:500]}") from exc

    write_bytes(data, destination)


def gemini_tts(text, destination, config):
    key = secret("GEMINI_TTS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    model = secret("GEMINI_TTS_MODEL")
    if not key or not model:
        raise RuntimeError("Gemini TTS key or GEMINI_TTS_MODEL is missing.")

    voice = secret("GEMINI_TTS_VOICE") or "Kore"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="")
        + ":generateContent?key=" + urllib.parse.quote(key, safe="")
    )
    payload = {
        "contents": [{"parts": [{
            "text": (
                "Read this narration aloud in Hindi. Do not translate, "
                "summarize, or add words:\n" + text
            )
        }]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": voice}
            }},
        },
    }

    data = post_json(url, {}, payload)
    try:
        result = json.loads(data.decode("utf-8"))
        parts = result["candidates"][0]["content"]["parts"]
        encoded = next(
            item["inlineData"]["data"] for item in parts
            if isinstance(item, dict)
            and item.get("inlineData", {}).get("data")
        )
    except (
        UnicodeDecodeError, json.JSONDecodeError, KeyError,
        IndexError, StopIteration, TypeError,
    ) as exc:
        raise RuntimeError("Gemini returned no usable inline audio.") from exc

    decode_audio(encoded, destination)


def elevenlabs(text, destination, config):
    key = secret("ELEVENLABS_API_KEY")
    voice = secret("ELEVENLABS_VOICE_ID")
    if not key or not voice:
        raise RuntimeError("ElevenLabs key or voice ID is missing.")

    model = secret("ELEVENLABS_TTS_MODEL") or "eleven_multilingual_v2"
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
        raise RuntimeError("Cartesia key or voice ID is missing.")

    payload = {
        "model_id": secret("CARTESIA_TTS_MODEL") or "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice},
        "output_format": {
            "container": "mp3", "sample_rate": 24000, "encoding": "mp3"
        },
    }
    data = post_json(
        secret("CARTESIA_TTS_URL") or "https://api.cartesia.ai/tts/bytes",
        {
            "Authorization": f"Bearer {key}",
            "Cartesia-Version": (
                secret("CARTESIA_API_VERSION") or "2025-04-16"
            ),
        },
        payload,
    )
    write_bytes(data, destination)


def minimax(text, destination, config):
    key = secret("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("MiniMax API key is missing.")

    payload = {
        "model": secret("MINIMAX_TTS_MODEL") or "speech-2.8-turbo",
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": (
                secret("MINIMAX_TTS_VOICE_ID")
                or "English_expressive_narrator"
            ),
            "speed": 1, "vol": 1, "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000, "bitrate": 128000,
            "format": "mp3", "channel": 1,
        },
        "output_format": "hex",
    }
    data = post_json(
        secret("MINIMAX_TTS_URL") or "https://api.minimax.io/v1/t2a_v2",
        {"Authorization": f"Bearer {key}"},
        payload,
    )

    try:
        result = json.loads(data.decode("utf-8"))
        base_resp = result.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax rejected request: {base_resp}")
        decode_audio(result["data"]["audio"], destination, "hex")
    except (
        UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError,
    ) as exc:
        raise RuntimeError("MiniMax returned an unrecognized response.") from exc


def finevoice(text, destination, config):
    key = secret("FINEVOICE_API_KEY")
    voice = secret("FINEVOICE_VOICE_ID")
    if not key or not voice:
        raise RuntimeError("FineVoice key or voice ID is missing.")

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
        raise RuntimeError("FineVoice response has no recognized audio field.")
    decode_audio(encoded, destination)


def piper(text, destination, config):
    executable = shutil.which("piper")
    model = (
        secret("PIPER_MODEL_PATH")
        or str(config.get("PIPER_MODEL_PATH", "")).strip()
    )
    if not executable or not model or not Path(model).is_file():
        raise RuntimeError("Piper executable/model is unavailable.")

    command = [executable, "--model", model, "--output_file", str(destination)]
    speaker = secret("PIPER_SPEAKER")
    if speaker:
        command.extend(["--speaker", speaker])

    run(command, input_text=text, timeout=300)
    audio_duration(destination)


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


def split_text(text, limit):
    text = " ".join(str(text).split())
    chunks = []

    while len(text) > limit:
        boundary = max(
            text.rfind(mark, 0, limit)
            for mark in ("।", "!", "?", ".", " ")
        )
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1

        chunks.append(text[:boundary].strip())
        text = text[boundary:].strip()

    if text:
        chunks.append(text)

    return chunks


def scene_key(scene):
    try:
        part = int(scene["part"])
        number = int(scene.get("scene", scene.get("local_scene")))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid part/scene mapping: {scene}") from exc

    if part < 1 or number < 1:
        raise RuntimeError(f"Invalid part/scene mapping: {scene}")

    return part, number


def audio_path(part, scene):
    return AUDIO_DIR / f"part_{part:02d}" / f"scene_{scene:02d}.mp3"


def chunk_path(part, scene, index):
    return (
        CHUNK_DIR / f"part_{part:02d}" / f"scene_{scene:02d}"
        / f"chunk_{index:04d}.mp3"
    )


def load_manifest():
    if MANIFEST_FILE.is_file():
        try:
            data = read_json(MANIFEST_FILE)
            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                return data
        except Exception as exc:
            print(f"WARNING: Ignoring invalid TTS checkpoint: {exc}")

    return {"status": "in_progress", "jobs": []}


def save_checkpoint(manifest, jobs, status="in_progress", **extra):
    save_json(MANIFEST_FILE, {
        **manifest,
        **extra,
        "status": status,
        "jobs": jobs,
        "updated_at": now(),
    })


def build_settings(config, order, allow_paid, chunk_limit):
    values = {key: os.environ.get(key, "") for key in PROVIDER_ENV_KEYS}
    piper_model = values.get("PIPER_MODEL_PATH") or str(
        config.get("PIPER_MODEL_PATH", "")
    ).strip()

    values["PIPER_MODEL_SHA256"] = (
        file_sha(piper_model)
        if piper_model and Path(piper_model).is_file()
        else ""
    )

    return {
        "provider_order": order,
        "allow_paid": allow_paid,
        "voice": str(config.get("VOICE", "male")),
        "speed": str(config.get("SPEED", "+0%")),
        "chunk_limit": chunk_limit,
        "provider_settings": values,
        "tts_edge_voice": str(config.get("TTS_EDGE_VOICE", "")),
        "tts_edge_rate": str(config.get("TTS_EDGE_RATE", "")),
        "tts_fallback_enabled": truthy(
            config.get("TTS_FALLBACK_ENABLED", True)
        ),
        "piper_model_config": str(config.get("PIPER_MODEL_PATH", "")),
        "generator_sha256": file_sha(Path(__file__)),
    }


def generate_chunk(
    chunk_text, cached, config, primary, fallback, allow_paid,
    part, number, index, total_chunks,
):
    """
    Return (provider, chunk_record).
    Fallback is entered only after every eligible primary provider fails.
    """
    errors = []
    groups = (("PRIMARY", primary), ("FALLBACK", fallback))

    for group_name, providers in groups:
        if not providers:
            continue

        print(f"{group_name} provider group: {', '.join(providers)}")

        for provider in providers:
            if provider in POTENTIALLY_BILLABLE and not allow_paid:
                message = f"{provider}: billable/cloud TTS disabled"
                print(f"SKIP {message}")
                errors.append(message)
                continue

            raw = cached.with_suffix(".raw")
            raw.unlink(missing_ok=True)

            print(
                f"Part {part} Scene {number} "
                f"Chunk {index}/{total_chunks} -> {provider} [{group_name}]"
            )

            try:
                GENERATORS[provider](chunk_text, raw, config)

                if not raw.is_file() or raw.stat().st_size < 100:
                    raise RuntimeError("Provider created no usable audio.")

                normalize_audio(raw, cached)

                record = {
                    "status": "completed",
                    "provider": provider,
                    "provider_group": group_name.lower(),
                    "updated_at": now(),
                }
                print(f"SUCCESS {provider} [{group_name}]")
                return provider, record

            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                print(f"FAILED {provider}: {str(exc)[:220]}")

            finally:
                raw.unlink(missing_ok=True)

        # No primary provider succeeded; the loop naturally advances
        # to fallback. If fallback is empty, the function raises below.

    raise RuntimeError(
        f"No TTS provider succeeded for Part {part}, Scene {number}, "
        f"Chunk {index}. Errors: {' | '.join(errors[-12:])}"
    )


def main():
    config = load_and_validate(CONFIG_FILE)
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must both be positive.")

    allow_paid = paid_allowed(config)
    primary, fallback = provider_groups(config)
    order = primary + fallback

    chunk_limit = max(
        150, min(int(config.get("TTS_CHUNK_MAX_CHARS", 500)), 1500)
    )

    print("Primary TTS order:", " -> ".join(primary))
    print("Fallback TTS order:", " -> ".join(fallback) if fallback else "(disabled)")
    print("Billable cloud TTS allowed:", allow_paid)

    if not NARRATION_FILE.is_file():
        raise RuntimeError("Narration file is missing; generate narration first.")

    narration = read_json(NARRATION_FILE)
    if narration.get("status") != "completed":
        raise RuntimeError("Narration is not marked completed.")

    scene_list = narration.get("scenes")
    if not isinstance(scene_list, list):
        raise RuntimeError("narration.json must contain a scenes list.")

    expected = {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    }

    indexed = {}
    for item in scene_list:
        key = scene_key(item)

        if key in indexed:
            raise RuntimeError(f"Duplicate narration scene: {key}")

        text = str(item.get("text", item.get("narration", ""))).strip()
        if not text:
            raise RuntimeError(f"Empty narration text for scene {key}")

        indexed[key] = text

    if set(indexed) != expected:
        raise RuntimeError(
            f"Scene mapping mismatch. Missing={sorted(expected-set(indexed))[:12]}, "
            f"extra={sorted(set(indexed)-expected)[:12]}"
        )

    manifest = load_manifest()
    old_jobs = {}

    for job in manifest.get("jobs", []):
        if isinstance(job, dict):
            try:
                old_jobs[scene_key(job)] = job
            except RuntimeError:
                print("WARNING: Ignoring malformed old TTS checkpoint.")

    settings = build_settings(config, order, allow_paid, chunk_limit)
    settings_hash = sha(json.dumps(
        settings, sort_keys=True, ensure_ascii=False
    ))
    narration_hash = sha(json.dumps([
        {"part": p, "scene": s, "text_sha256": sha(text)}
        for (p, s), text in sorted(indexed.items())
    ], sort_keys=True, ensure_ascii=False))

    manifest.update({
        "format": str(config.get("FORMAT", "full")).lower(),
        "provider_order": order,
        "primary_provider_order": primary,
        "fallback_provider_order": fallback,
        "paid_cloud_allowed": allow_paid,
        "settings_sha256": settings_hash,
        "narration_sha256": narration_hash,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(expected),
        "audio_root": AUDIO_DIR.relative_to(ROOT).as_posix(),
    })

    # Keep previous checkpoints available until new work is checkpointed.
    new_jobs = []

    for part, number in sorted(expected):
        text = indexed[(part, number)]
        text_hash = sha(text)
        destination = audio_path(part, number)
        previous = old_jobs.get((part, number), {})
        relative_output = destination.relative_to(ROOT).as_posix()

        if (
            previous.get("status") == "completed"
            and previous.get("text_sha256") == text_hash
            and previous.get("settings_sha256") == settings_hash
            and previous.get("output") == relative_output
            and destination.is_file()
        ):
            try:
                duration = audio_duration(destination)
                job = {
                    **previous,
                    "duration": duration,
                    "status": "completed",
                }
                new_jobs.append(job)
                print(f"REUSE Part {part} Scene {number}")
                save_checkpoint(
                    manifest, new_jobs, completed_scenes=len(new_jobs)
                )
                continue
            except Exception as exc:
                print(f"WARNING: Cached audio invalid; rebuilding: {exc}")

        chunks = split_text(text, chunk_limit)
        old_chunks = previous.get("chunks", {})
        if not isinstance(old_chunks, dict):
            old_chunks = {}

        destination.parent.mkdir(parents=True, exist_ok=True)
        chunk_paths = []
        used_providers = []
        chunk_records = {}

        for index, chunk_text in enumerate(chunks, start=1):
            chunk_hash = sha(chunk_text)
            cached = chunk_path(part, number, index)
            old = old_chunks.get(str(index), {})

            if (
                old.get("status") == "completed"
                and old.get("text_sha256") == chunk_hash
                and old.get("settings_sha256") == settings_hash
                and cached.is_file()
            ):
                try:
                    audio_duration(cached)
                    chunk_paths.append(cached)
                    used_providers.append(old.get("provider", "cached"))
                    chunk_records[str(index)] = old
                    print(f"REUSE Part {part} Scene {number} Chunk {index}")
                    continue
                except Exception:
                    pass

            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.unlink(missing_ok=True)

            try:
                provider, record = generate_chunk(
                    chunk_text=chunk_text,
                    cached=cached,
                    config=config,
                    primary=primary,
                    fallback=fallback,
                    allow_paid=allow_paid,
                    part=part,
                    number=number,
                    index=index,
                    total_chunks=len(chunks),
                )
            except Exception as exc:
                incomplete = {
                    "part": part,
                    "scene": number,
                    "status": "incomplete",
                    "text_sha256": text_hash,
                    "settings_sha256": settings_hash,
                    "output": relative_output,
                    "chunks": chunk_records,
                    "error": str(exc)[:2000],
                }
                save_checkpoint(
                    manifest,
                    new_jobs + [incomplete],
                    status="incomplete",
                    failed_scene={
                        "part": part,
                        "scene": number,
                        "chunk": index,
                    },
                    completed_scenes=len(new_jobs),
                )
                raise RuntimeError(
                    f"TTS failed for Part {part}, Scene {number}, "
                    f"Chunk {index}. Successful chunks remain checkpointed."
                ) from exc

            record.update({
                "text_sha256": chunk_hash,
                "settings_sha256": settings_hash,
            })
            chunk_records[str(index)] = record
            chunk_paths.append(cached)
            used_providers.append(provider)

            # Save immediately after each successful chunk.
            in_progress = {
                "part": part,
                "scene": number,
                "status": "in_progress",
                "text_sha256": text_hash,
                "settings_sha256": settings_hash,
                "output": relative_output,
                "chunks": chunk_records,
            }
            save_checkpoint(
                manifest,
                new_jobs + [in_progress],
                completed_scenes=len(new_jobs),
                current_scene={"part": part, "scene": number},
            )

        concat_list = destination.with_suffix(".concat.txt")
        temp_final = destination.with_name(destination.stem + ".tmp.mp3")

        try:
            with concat_list.open("w", encoding="utf-8") as f:
                for path in chunk_paths:
                    escaped = str(path.resolve()).replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            run([
                "ffmpeg", "-y", "-v", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-vn", "-ac", "1", "-ar", "24000",
                "-codec:a", "libmp3lame", "-b:a", "96k",
                str(temp_final),
            ], timeout=300)

            duration = audio_duration(temp_final)
            os.replace(temp_final, destination)

        finally:
            concat_list.unlink(missing_ok=True)
            temp_final.unlink(missing_ok=True)

        job = {
            "part": part,
            "scene": number,
            "status": "completed",
            "text_sha256": text_hash,
            "settings_sha256": settings_hash,
            "output": relative_output,
            "duration": duration,
            "providers": used_providers,
            "chunks": chunk_records,
            "updated_at": now(),
        }
        new_jobs.append(job)
        save_checkpoint(
            manifest, new_jobs, completed_scenes=len(new_jobs)
        )
        print(f"DONE Part {part} Scene {number}: {duration:.2f}s")

    if len(new_jobs) != len(expected) or any(
        job.get("status") != "completed" for job in new_jobs
    ):
        save_checkpoint(
            manifest,
            new_jobs,
            status="incomplete",
            completed_scenes=len(new_jobs),
        )
        raise RuntimeError("Not all narration scenes have valid audio.")

    save_checkpoint(
        manifest,
        new_jobs,
        status="completed",
        completed_scenes=len(new_jobs),
        completed_at=now(),
    )
    print(f"TTS completed: {len(new_jobs)} scenes")
    print(f"Audio jobs manifest: {MANIFEST_FILE}")


if __name__ == "__main__":
    main()