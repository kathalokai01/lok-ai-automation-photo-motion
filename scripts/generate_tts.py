
#!/usr/bin/env python3
"""Katha Lok AI: resumable scene-wise TTS with ordered provider fallback."""

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


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "output" / "narration"
NARRATION_FILE = OUTPUT_ROOT / "narration.json"
AUDIO_DIR = OUTPUT_ROOT / "audio"
MANIFEST_FILE = OUTPUT_ROOT / "audio_jobs.json"
CHUNK_DIR = AUDIO_DIR / "_chunks"
CONFIG_FILE = REPO_ROOT / "Input" / "topic.txt"

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

# These providers can incur charges. They require explicit opt-in in
# both Input/topic.txt and the workflow environment.
POTENTIALLY_BILLABLE = {
    "fish_audio",
    "gemini_tts",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
}

PROVIDER_ENV_KEYS = (
    "FISH_TTS_MODEL",
    "FISH_REFERENCE_ID",
    "FISH_AUDIO_REFERENCE_ID",
    "GEMINI_TTS_MODEL",
    "GEMINI_TTS_VOICE",
    "ELEVENLABS_TTS_MODEL",
    "ELEVENLABS_VOICE_ID",
    "CARTESIA_TTS_MODEL",
    "CARTESIA_VOICE_ID",
    "MINIMAX_TTS_MODEL",
    "MINIMAX_TTS_VOICE_ID",
    "MINIMAX_TTS_URL",
    "FINEVOICE_VOICE_ID",
    "FINEVOICE_TTS_URL",
    "PIPER_MODEL_PATH",
    "PIPER_SPEAKER",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, path)


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def secret(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def truthy(value):
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


def paid_allowed(config):
    """Fail closed unless both configuration layers explicitly opt in."""
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


def provider_order(config):
    """Selected three providers first, then configured fallbacks."""
    defaults = ("fish_audio", "gemini_tts", "edge_tts")

    main = [
        normalize_provider(
            config.get(f"TTS_MAIN_{index}", defaults[index - 1])
        )
        for index in (1, 2, 3)
    ]

    if len(set(main)) != 3:
        raise RuntimeError(
            "TTS_MAIN_1, TTS_MAIN_2 and TTS_MAIN_3 "
            "must select three different providers."
        )

    unsupported = [name for name in main if name not in PROVIDERS]
    if unsupported:
        raise RuntimeError(
            f"Unsupported primary TTS provider(s): {unsupported}"
        )

    order = list(main)

    if truthy(config.get("TTS_FALLBACK_ENABLED", True)):
        raw = config.get("TTS_FALLBACK_ORDER", ",".join(PROVIDERS))

        if isinstance(raw, (list, tuple)):
            fallback = list(raw)
        else:
            fallback = str(raw).replace(";", ",").split(",")

        for value in fallback:
            name = normalize_provider(value)
            if not name:
                continue
            if name not in PROVIDERS:
                raise RuntimeError(
                    f"Unsupported fallback provider: {value}"
                )
            if name not in order:
                order.append(name)

        # Ensure available supported providers are not accidentally omitted.
        for name in PROVIDERS:
            if name not in order:
                order.append(name)

    return order


def run(command, *, input_text=None, timeout=240):
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

    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp3")

    try:
        run([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", "24000",
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(temporary),
        ], timeout=300)

        audio_duration(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    return audio_duration(destination)


def request_bytes(url, headers, payload, timeout=150):
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
            f"HTTP {exc.code}: {detail[:600]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Provider request failed: {exc}") from exc

    if len(data) < 100:
        raise RuntimeError("Provider returned an empty response.")

    return data


def write_audio_bytes(data, destination):
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
        if encoding == "hex":
            raw = bytes.fromhex(value)
        else:
            raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Provider audio decoding failed.") from exc

    write_audio_bytes(raw, destination)


def edge_tts(text, destination, config):
    if not shutil.which("edge-tts"):
        raise RuntimeError("edge-tts is not installed.")

    voice = str(config.get("TTS_EDGE_VOICE", config.get("VOICE", "male"))).strip()
    voice_lower = voice.lower()

    if voice_lower in {"female", "woman", "f"}:
        voice = "hi-IN-SwaraNeural"
    elif voice_lower in {"male", "man", "m"}:
        voice = "hi-IN-MadhurNeural"

    rate = str(config.get("TTS_EDGE_RATE", config.get("SPEED", "+0%")))
    temporary = Path(destination).with_suffix(".edge.mp3")

    try:
        run([
            "edge-tts",
            "--voice", voice,
            "--rate", rate,
            "--text", text,
            "--write-media", str(temporary),
        ], timeout=240)

        audio_duration(temporary)
        shutil.copyfile(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def fish_audio(text, destination, config):
    key = secret("FISH_API_KEY", "FISH_AUDIO_API_KEY")
    if not key:
        raise RuntimeError("Fish Audio API key is missing.")

    payload = {
        "text": text,
        "format": "mp3",
    }

    reference = secret("FISH_REFERENCE_ID", "FISH_AUDIO_REFERENCE_ID")
    if reference:
        payload["reference_id"] = reference

    request = urllib.request.Request(
        os.environ.get("FISH_TTS_URL", "https://api.fish.audio/v1/tts"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "model": os.environ.get("FISH_TTS_MODEL", "s2.1"),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Fish Audio HTTP {exc.code}: {detail[:500]}"
        ) from exc

    write_audio_bytes(data, destination)


def gemini_tts(text, destination, config):
    key = secret("GEMINI_TTS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    model = secret("GEMINI_TTS_MODEL")

    if not key:
        raise RuntimeError("Gemini TTS API key is missing.")
    if not model:
        raise RuntimeError("GEMINI_TTS_MODEL is not configured.")

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
                    "Do not translate, summarize, or add words:\n" + text
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

    data = request_bytes(url, {}, payload)

    try:
        result = json.loads(data.decode("utf-8"))
        parts = result["candidates"][0]["content"]["parts"]
        audio = next(
            item["inlineData"]["data"]
            for item in parts
            if isinstance(item, dict)
            and item.get("inlineData", {}).get("data")
        )
    except (
        UnicodeDecodeError, json.JSONDecodeError, KeyError,
        IndexError, StopIteration, TypeError,
    ) as exc:
        raise RuntimeError(
            "Gemini response contained no usable inline audio."
        ) from exc

    decode_audio(audio, destination)


def elevenlabs(text, destination, config):
    key = secret("ELEVENLABS_API_KEY")
    voice_id = secret("ELEVENLABS_VOICE_ID")

    if not key or not voice_id:
        raise RuntimeError("ElevenLabs key or voice ID is missing.")

    model = secret("ELEVENLABS_TTS_MODEL") or "eleven_multilingual_v2"
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + urllib.parse.quote(voice_id, safe="")
        + "?output_format=mp3_44100_128"
    )

    data = request_bytes(
        url,
        {"xi-api-key": key},
        {"text": text, "model_id": model},
    )
    write_audio_bytes(data, destination)


def cartesia(text, destination, config):
    key = secret("CARTESIA_API_KEY")
    voice_id = secret("CARTESIA_VOICE_ID")

    if not key or not voice_id:
        raise RuntimeError("Cartesia key or voice ID is missing.")

    payload = {
        "model_id": secret("CARTESIA_TTS_MODEL") or "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "mp3",
            "sample_rate": 24000,
            "encoding": "mp3",
        },
    }

    data = request_bytes(
        os.environ.get(
            "CARTESIA_TTS_URL", "https://api.cartesia.ai/tts/bytes"
        ),
        {
            "Authorization": f"Bearer {key}",
            "Cartesia-Version": os.environ.get(
                "CARTESIA_API_VERSION", "2025-04-16"
            ),
        },
        payload,
    )
    write_audio_bytes(data, destination)


def minimax(text, destination, config):
    key = secret("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("MiniMax API key is missing.")

    payload = {
        "model": secret("MINIMAX_TTS_MODEL") or "speech-2.8-turbo",
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": secret("MINIMAX_TTS_VOICE_ID")
            or "English_expressive_narrator",
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

    data = request_bytes(
        os.environ.get(
            "MINIMAX_TTS_URL", "https://api.minimax.io/v1/t2a_v2"
        ),
        {"Authorization": f"Bearer {key}"},
        payload,
    )

    try:
        result = json.loads(data.decode("utf-8"))
        status = result.get("base_resp", {}).get("status_code", 0)
        if status != 0:
            raise RuntimeError(
                f"MiniMax rejected the request: {result.get('base_resp')}"
            )
        decode_audio(result["data"]["audio"], destination, "hex")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("MiniMax returned an unrecognized response.") from exc


def finevoice(text, destination, config):
    key = secret("FINEVOICE_API_KEY")
    voice_id = secret("FINEVOICE_VOICE_ID")

    if not key or not voice_id:
        raise RuntimeError("FineVoice key or voice ID is missing.")

    url = secret("FINEVOICE_TTS_URL") or "https://api.finevoice.ai/v1/text_to_speech"
    data = request_bytes(
        url,
        {"Authorization": f"Bearer {key}"},
        {"text": text, "voice_id": voice_id, "format": "mp3"},
    )

    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        write_audio_bytes(data, destination)
        return

    audio = result.get("audio") or result.get("audio_base64")
    if not audio:
        raise RuntimeError("FineVoice response has no recognized audio field.")

    decode_audio(audio, destination)


def piper(text, destination, config):
    executable = shutil.which("piper")
    model = secret("PIPER_MODEL_PATH")

    if not model:
        model = str(config.get("PIPER_MODEL_PATH", "")).strip()

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
            text.rfind("।", 0, limit),
            text.rfind("!", 0, limit),
            text.rfind("?", 0, limit),
            text.rfind(".", 0, limit),
            text.rfind(" ", 0, limit),
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


def chunk_path(part, scene, number):
    return (
        CHUNK_DIR / f"part_{part:02d}" / f"scene_{scene:02d}"
        / f"chunk_{number:04d}.mp3"
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
    payload = {
        **manifest,
        **extra,
        "status": status,
        "jobs": jobs,
        "updated_at": now(),
    }
    save_json(MANIFEST_FILE, payload)


def build_settings(config, order, allow_paid, chunk_limit):
    values = {
        key: os.environ.get(key, "")
        for key in PROVIDER_ENV_KEYS
    }

    # Never save secret API keys into output manifests or logs.
    piper_model = values.get("PIPER_MODEL_PATH") or str(
        config.get("PIPER_MODEL_PATH", "")
    ).strip()

    if piper_model and Path(piper_model).is_file():
        values["PIPER_MODEL_SHA256"] = file_digest(piper_model)
    else:
        values["PIPER_MODEL_SHA256"] = ""

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
    }


def main():
    config = load_and_validate(CONFIG_FILE)

    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must both be positive.")

    allow_paid = paid_allowed(config)
    order = provider_order(config)
    chunk_limit = max(
        150,
        min(int(config.get("TTS_CHUNK_MAX_CHARS", 500)), 1500),
    )

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
    for scene in scene_list:
        key = scene_key(scene)
        if key in indexed:
            raise RuntimeError(f"Duplicate narration scene: {key}")

        text = str(scene.get("text", scene.get("narration", ""))).strip()
        if not text:
            raise RuntimeError(f"Empty narration text for scene {key}")

        indexed[key] = text

    if set(indexed) != expected:
        missing = sorted(expected - set(indexed))
        extra = sorted(set(indexed) - expected)
        raise RuntimeError(
            f"Scene mapping mismatch. Missing={missing[:12]}, "
            f"extra={extra[:12]}"
        )

    manifest = load_manifest()
    old_jobs = {}

    for job in manifest.get("jobs", []):
        if not isinstance(job, dict):
            continue
        try:
            old_jobs[scene_key(job)] = job
        except RuntimeError:
            print("WARNING: Ignoring malformed old TTS job checkpoint.")

    settings = build_settings(config, order, allow_paid, chunk_limit)
    settings_hash = digest(json.dumps(settings, sort_keys=True))
    narration_hash = digest(json.dumps(
        [
            {
                "part": part,
                "scene": scene,
                "text_sha256": digest(text),
            }
            for (part, scene), text in sorted(indexed.items())
        ],
        sort_keys=True,
        ensure_ascii=False,
    ))

    manifest.update({
        "format": str(config.get("FORMAT", "full")).lower(),
        "provider_order": order,
        "paid_cloud_allowed": allow_paid,
        "settings_sha256": settings_hash,
        "narration_sha256": narration_hash,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(expected),
        "audio_root": AUDIO_DIR.relative_to(REPO_ROOT).as_posix(),
    })

    save_checkpoint(manifest, [])

    new_jobs = []

    for part, number in sorted(expected):
        text = indexed[(part, number)]
        text_hash = digest(text)
        destination = audio_path(part, number)
        previous = old_jobs.get((part, number), {})

        previous_output = str(previous.get("output", ""))
        expected_output = destination.relative_to(REPO_ROOT).as_posix()

        # Reuse a complete scene only when its text, settings and output agree.
        if (
            previous.get("status") == "completed"
            and previous.get("text_sha256") == text_hash
            and previous.get("settings_sha256") == settings_hash
            and previous_output == expected_output
            and destination.is_file()
        ):
            try:
                duration = audio_duration(destination)
                print(f"REUSE Part {part} Scene {number}: {destination}")
                reused = {
                    **previous,
                    "duration": duration,
                    "status": "completed",
                }
                new_jobs.append(reused)
                save_checkpoint(
                    manifest,
                    new_jobs,
                    completed_scenes=len(new_jobs),
                )
                continue
            except Exception as exc:
                print(f"WARNING: Rebuilding invalid cached audio: {exc}")

        chunks = split_text(text, chunk_limit)
        old_chunks = previous.get("chunks", {})
        if not isinstance(old_chunks, dict):
            old_chunks = {}

        destination.parent.mkdir(parents=True, exist_ok=True)
        chunk_paths = []
        used_providers = []
        chunk_records = {}

        for index, chunk_text in enumerate(chunks, start=1):
            chunk_hash = digest(chunk_text)
            cached_path = chunk_path(part, number, index)
            old = old_chunks.get(str(index), {})

            if (
                old.get("status") == "completed"
                and old.get("text_sha256") == chunk_hash
                and old.get("settings_sha256") == settings_hash
                and cached_path.is_file()
            ):
                try:
                    audio_duration(cached_path)
                    chunk_paths.append(cached_path)
                    used_providers.append(old.get("provider", "cached"))
                    chunk_records[str(index)] = old
                    print(
                        f"REUSE Part {part} Scene {number} "
                        f"Chunk {index}/{len(chunks)}"
                    )
                    continue
                except Exception:
                    pass

            cached_path.parent.mkdir(parents=True, exist_ok=True)
            cached_path.unlink(missing_ok=True)
            errors = []
            succeeded = False

            for provider in order:
                if provider in POTENTIALLY_BILLABLE and not allow_paid:
                    print(f"SKIP {provider}: paid/cloud providers disabled")
                    continue

                raw = cached_path.with_suffix(".raw")
                raw.unlink(missing_ok=True)

                print(
                    f"Part {part} Scene {number} "
                    f"Chunk {index}/{len(chunks)} -> {provider}"
                )

                try:
                    GENERATORS[provider](chunk_text, raw, config)

                    if not raw.is_file() or raw.stat().st_size < 100:
                        raise RuntimeError("Provider created no usable audio.")

                    normalize_audio(raw, cached_path)

                    record = {
                        "status": "completed",
                        "provider": provider,
                        "text_sha256": chunk_hash,
                        "settings_sha256": settings_hash,
                        "updated_at": now(),
                    }

                    chunk_records[str(index)] = record
                    chunk_paths.append(cached_path)
                    used_providers.append(provider)
                    succeeded = True

                    # Checkpoint immediately after each successful chunk.
                    in_progress = {
                        "part": part,
                        "scene": number,
                        "status": "in_progress",
                        "text_sha256": text_hash,
                        "settings_sha256": settings_hash,
                        "output": expected_output,
                        "chunks": chunk_records,
                    }

                    save_checkpoint(
                        manifest,
                        new_jobs + [in_progress],
                        completed_scenes=len(new_jobs),
                        current_scene={"part": part, "scene": number},
                    )
                    break

                except Exception as exc:
                    errors.append(f"{provider}: {exc}")
                    print(f"FAILED {provider}: {str(exc)[:220]}")

                finally:
                    raw.unlink(missing_ok=True)

            if not succeeded:
                incomplete = {
                    "part": part,
                    "scene": number,
                    "status": "incomplete",
                    "text_sha256": text_hash,
                    "settings_sha256": settings_hash,
                    "output": expected_output,
                    "chunks": chunk_records,
                    "errors": errors[-12:],
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
                )

        concat_list = destination.with_suffix(".concat.txt")
        temp_final = destination.with_name(destination.stem + ".tmp.mp3")

        try:
            with concat_list.open("w", encoding="utf-8") as handle:
                for path in chunk_paths:
                    escaped = str(path.resolve()).replace("'", "'\\''")
                    handle.write(f"file '{escaped}'\n")

            run([
                "ffmpeg", "-y", "-v", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
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
            "output": expected_output,
            "duration": duration,
            "providers": used_providers,
            "chunks": chunk_records,
            "updated_at": now(),
        }

        new_jobs.append(job)
        save_checkpoint(
            manifest,
            new_jobs,
            completed_scenes=len(new_jobs),
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
