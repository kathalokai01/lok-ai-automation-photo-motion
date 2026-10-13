
#!/usr/bin/env python3
"""Katha Lok AI: resumable, scene-wise TTS with provider fallback."""

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

from input_config import load_and_validate, get_parts, get_scenes

ROOT = Path("output/narration")
NARRATION_FILE = ROOT / "narration.json"
AUDIO_DIR = ROOT / "audio"
MANIFEST_FILE = ROOT / "audio_jobs.json"
CHUNK_DIR = AUDIO_DIR / "_chunks"

PROVIDERS = [
    "fish_audio",
    "gemini_tts",
    "edge_tts",
    "piper",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
]

PAID_PROVIDERS = {
    "fish_audio",
    "gemini_tts",
    "elevenlabs",
    "cartesia",
    "finevoice",
    "minimax",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def read_json(path):
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return data


def secret(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def paid_allowed(config):
    # Explicit input opt-in AND workflow environment opt-in are required.
    input_allowed = str(config.get("TTS_ALLOW_PAID", False)).lower() in {
        "true", "yes", "1", "on"
    }
    env_allowed = os.environ.get("TTS_ALLOW_PAID", "").strip().lower() in {
        "true", "yes", "1", "on"
    }
    return input_allowed and env_allowed


def provider_order(config):
    main = [
        str(config.get("TTS_MAIN_1", "fish_audio")).strip().lower(),
        str(config.get("TTS_MAIN_2", "gemini_tts")).strip().lower(),
        str(config.get("TTS_MAIN_3", "edge_tts")).strip().lower(),
    ]

    if len(set(main)) != 3 or any(p not in PROVIDERS for p in main):
        raise RuntimeError(
            "TTS_MAIN_1, TTS_MAIN_2 and TTS_MAIN_3 must be three "
            "different supported provider names."
        )

    order = list(main)
    if str(config.get("TTS_FALLBACK_ENABLED", True)).lower() not in {
        "false", "no", "0", "off"
    }:
        raw = str(config.get(
            "TTS_FALLBACK_ORDER", ",".join(PROVIDERS)
        ))
        for item in raw.split(","):
            item = item.strip().lower()
            if item and item not in PROVIDERS:
                raise RuntimeError(f"Unsupported TTS provider: {item}")
            if item and item not in order:
                order.append(item)

        for item in PROVIDERS:
            if item not in order:
                order.append(item)

    return order


def run(command, *, input_text=None, timeout=240):
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            (result.stderr or result.stdout or "Command failed")[-1200:]
        )
    return result


def audio_duration(path):
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
    except ValueError as exc:
        raise RuntimeError(f"Invalid audio duration: {path}") from exc
    if duration <= 0:
        raise RuntimeError(f"Invalid audio duration: {path}")
    return duration


def normalize_audio(source, destination):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".tmp.mp3")
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


def request_bytes(url, headers, payload, timeout=150):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:600]}") from exc
    if len(data) < 100:
        raise RuntimeError("Provider returned an empty response.")
    return data, content_type


def decode_audio(value, destination, encoding="base64"):
    if not isinstance(value, str) or not value:
        raise RuntimeError("Provider response did not contain audio.")
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    try:
        raw = bytes.fromhex(value) if encoding == "hex" else base64.b64decode(value)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Could not decode provider audio.") from exc
    if len(raw) < 100:
        raise RuntimeError("Decoded audio is empty.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)


def edge_tts(text, destination, config):
    if not shutil.which("edge-tts"):
        raise RuntimeError("edge-tts is not installed.")
    voice = str(config.get("VOICE", "male")).lower()
    if voice in {"female", "woman", "f"}:
        voice = "hi-IN-SwaraNeural"
    elif voice in {"male", "man", "m"}:
        voice = "hi-IN-MadhurNeural"
    temp = destination.with_suffix(".edge.mp3")
    try:
        run([
            "edge-tts", "--voice", voice,
            "--rate", str(config.get("SPEED", "+0%")),
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
        "https://api.fish.audio/v1/tts",
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
        raise RuntimeError(f"Fish Audio HTTP {exc.code}: {detail[:500]}") from exc
    if len(data) < 100:
        raise RuntimeError("Fish Audio returned empty audio.")
    destination.write_bytes(data)


def gemini_tts(text, destination, config):
    key = secret("GEMINI_TTS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Gemini API key is missing.")
    model = os.environ.get("GEMINI_TTS_MODEL", "").strip()
    if not model:
        raise RuntimeError(
            "Set GEMINI_TTS_MODEL to a TTS model available to your API key."
        )
    voice = os.environ.get("GEMINI_TTS_VOICE", "Kore")
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
                    "Do not translate or add words:\n" + text
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
    data, _ = request_bytes(url, {}, payload)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        audio = next(
            p["inlineData"]["data"] for p in parts
            if isinstance(p, dict) and p.get("inlineData", {}).get("data")
        )
    except (KeyError, IndexError, StopIteration, TypeError) as exc:
        raise RuntimeError(
            "Gemini response did not contain inline audio. "
            "Verify model/API support for audio generation."
        ) from exc
    decode_audio(audio, destination)


def elevenlabs(text, destination, config):
    key = secret("ELEVENLABS_API_KEY")
    voice_id = secret("ELEVENLABS_VOICE_ID")
    if not key or not voice_id:
        raise RuntimeError("ElevenLabs API key or voice ID is missing.")
    model = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2")
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + urllib.parse.quote(voice_id, safe="")
        + "?output_format=mp3_44100_128"
    )
    data, _ = request_bytes(
        url, {"xi-api-key": key},
        {"text": text, "model_id": model},
    )
    destination.write_bytes(data)


def cartesia(text, destination, config):
    key = secret("CARTESIA_API_KEY")
    voice_id = secret("CARTESIA_VOICE_ID")
    if not key or not voice_id:
        raise RuntimeError("Cartesia API key or voice ID is missing.")
    payload = {
        "model_id": os.environ.get("CARTESIA_TTS_MODEL", "sonic-2"),
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "mp3", "sample_rate": 24000, "encoding": "mp3"
        },
    }
    data, _ = request_bytes(
        "https://api.cartesia.ai/tts/bytes",
        {"Authorization": f"Bearer {key}", "Cartesia-Version": "2025-04-16"},
        payload,
    )
    destination.write_bytes(data)


def minimax(text, destination, config):
    key = secret("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("MiniMax API key is missing.")
    payload = {
        "model": os.environ.get("MINIMAX_TTS_MODEL", "speech-2.8-turbo"),
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": os.environ.get(
                "MINIMAX_TTS_VOICE_ID", "English_expressive_narrator"
            ),
            "speed": 1, "vol": 1, "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000, "bitrate": 128000,
            "format": "mp3", "channel": 1,
        },
        "output_format": "hex",
    }
    data, _ = request_bytes(
        os.environ.get("MINIMAX_TTS_URL", "https://api.minimax.io/v1/t2a_v2"),
        {"Authorization": f"Bearer {key}"}, payload,
    )
    try:
        if data.get("base_resp", {}).get("status_code", 0) != 0:
            raise RuntimeError(str(data["base_resp"]))
        decode_audio(data["data"]["audio"], destination, "hex")
    except (KeyError, TypeError) as exc:
        raise RuntimeError("MiniMax returned an unrecognized audio response.") from exc


def finevoice(text, destination, config):
    key = secret("FINEVOICE_API_KEY")
    voice_id = secret("FINEVOICE_VOICE_ID")
    if not key or not voice_id:
        raise RuntimeError("FineVoice API key or voice ID is missing.")
    data, _ = request_bytes(
        "https://api.finevoice.ai/v1/text_to_speech",
        {"Authorization": f"Bearer {key}"},
        {"text": text, "voice_id": voice_id, "format": "mp3"},
    )
    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        destination.write_bytes(data)
        return
    audio = result.get("audio") or result.get("audio_base64")
    if audio:
        decode_audio(audio, destination)
        return
    raise RuntimeError("FineVoice response has no recognized audio field.")


def piper(text, destination, config):
    executable = shutil.which("piper")
    model = secret("PIPER_MODEL_PATH") or os.environ.get("PIPER_MODEL_PATH", "")
    if not executable or not model or not Path(model).is_file():
        raise RuntimeError("Piper executable/model is unavailable.")
    run(
        [executable, "--model", model, "--output_file", str(destination)],
        input_text=text, timeout=300,
    )


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
    text = " ".join(text.split())
    chunks = []
    while len(text) > limit:
        boundary = max(
            text.rfind("।", 0, limit),
            text.rfind("!", 0, limit),
            text.rfind("?", 0, limit),
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
        part, number = int(scene["part"]), int(scene["scene"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"Invalid part/scene mapping: {scene}")
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
            if isinstance(data.get("jobs"), list):
                return data
        except Exception as exc:
            print(f"WARNING: Existing audio manifest cannot be reused: {exc}")
    return {"status": "in_progress", "jobs": []}


def main():
    config = load_and_validate()
    parts = get_parts(config)
    scenes_per_part = get_scenes(config)
    allow_paid = paid_allowed(config)
    order = provider_order(config)
    chunk_limit = int(config.get("TTS_CHUNK_MAX_CHARS", 500))
    chunk_limit = max(150, min(chunk_limit, 1500))

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
        scene["text"] = text
        indexed[key] = scene

    if set(indexed) != expected:
        missing = sorted(expected - set(indexed))
        extra = sorted(set(indexed) - expected)
        raise RuntimeError(
            f"Scene mapping mismatch. Missing={missing[:12]}, extra={extra[:12]}"
        )

    manifest = load_manifest()
    old_jobs = {
        scene_key(job): job for job in manifest.get("jobs", [])
        if isinstance(job, dict) and "part" in job and "scene" in job
    }
    new_jobs = []

    settings = {
        "provider_order": order,
        "allow_paid": allow_paid,
        "voice": str(config.get("VOICE", "male")),
        "speed": str(config.get("SPEED", "+0%")),
        "chunk_limit": chunk_limit,
        "models": {
            key: os.environ.get(key, "")
            for key in (
                "FISH_TTS_MODEL", "GEMINI_TTS_MODEL", "GEMINI_TTS_VOICE",
                "ELEVENLABS_TTS_MODEL", "CARTESIA_TTS_MODEL",
                "MINIMAX_TTS_MODEL", "MINIMAX_TTS_VOICE_ID",
            )
        },
    }
    settings_hash = digest(json.dumps(settings, sort_keys=True))

    manifest.update({
        "status": "in_progress",
        "format": str(config.get("FORMAT", "full")),
        "provider_order": order,
        "paid_cloud_allowed": allow_paid,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(expected),
        "updated_at": now(),
    })
    save_json(MANIFEST_FILE, manifest)

    for key in sorted(expected):
        part, number = key
        text = indexed[key]["text"]
        text_hash = digest(text)
        destination = audio_path(part, number)
        previous = old_jobs.get(key, {})
        previous_path = Path(str(previous.get("output", "")))
        valid_existing = (
            previous.get("status") == "completed"
            and previous.get("text_sha256") == text_hash
            and previous.get("settings_sha256") == settings_hash
            and previous_path == destination
            and destination.is_file()
        )
        if valid_existing:
            try:
                duration = audio_duration(destination)
                print(f"REUSE Part {part} Scene {number}: {destination}")
                new_jobs.append({
                    **previous, "duration": duration, "status": "completed"
                })
                continue
            except Exception:
                pass

        chunks = split_text(text, chunk_limit)
        chunk_records = previous.get("chunks", {})
        if not isinstance(chunk_records, dict):
            chunk_records = {}
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_chunks = []
        used_providers = []
        failed = False

        for index, chunk_text in enumerate(chunks, start=1):
            chash = digest(chunk_text)
            cp = chunk_path(part, number, index)
            old = chunk_records.get(str(index), {})
            if (
                old.get("status") == "completed"
                and old.get("text_sha256") == chash
                and old.get("settings_sha256") == settings_hash
                and cp.is_file()
            ):
                try:
                    audio_duration(cp)
                    temp_chunks.append(cp)
                    used_providers.append(old.get("provider", "cached"))
                    continue
                except Exception:
                    pass

            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.unlink(missing_ok=True)
            errors = []
            succeeded = False

            for provider in order:
                if provider in PAID_PROVIDERS and not allow_paid:
                    print(f"SKIP {provider}: paid APIs disabled")
                    continue

                raw = cp.with_suffix(".raw")
                raw.unlink(missing_ok=True)
                print(
                    f"Part {part} Scene {number} Chunk {index}/{len(chunks)} "
                    f"-> {provider}"
                )
                try:
                    GENERATORS[provider](chunk_text, raw, config)
                    if not raw.is_file() or raw.stat().st_size < 100:
                        raise RuntimeError("Provider created no audio.")
                    normalize_audio(raw, cp)
                    record = {
                        "status": "completed",
                        "provider": provider,
                        "text_sha256": chash,
                        "settings_sha256": settings_hash,
                        "updated_at": now(),
                    }
                    chunk_records[str(index)] = record
                    save_json(MANIFEST_FILE, {
                        **manifest,
                        "status": "in_progress",
                        "jobs": new_jobs + [{
                            "part": part, "scene": number,
                            "status": "in_progress",
                            "text_sha256": text_hash,
                            "settings_sha256": settings_hash,
                            "output": str(destination),
                            "chunks": chunk_records,
                        }],
                    })
                    temp_chunks.append(cp)
                    used_providers.append(provider)
                    succeeded = True
                    break
                except Exception as exc:
                    errors.append(f"{provider}: {exc}")
                    print(f"FAILED {provider}: {str(exc)[:220]}")
                finally:
                    raw.unlink(missing_ok=True)

            if not succeeded:
                failed = True
                new_jobs.append({
                    "part": part, "scene": number,
                    "status": "incomplete",
                    "text_sha256": text_hash,
                    "settings_sha256": settings_hash,
                    "output": str(destination),
                    "chunks": chunk_records,
                    "errors": errors[-12:],
                })
                save_json(MANIFEST_FILE, {
                    **manifest, "status": "incomplete",
                    "jobs": new_jobs,
                    "failed_scene": {"part": part, "scene": number, "chunk": index},
                })
                raise RuntimeError(
                    f"TTS failed for Part {part}, Scene {number}, Chunk {index}. "
                    "Successful chunks remain checkpointed."
                )

        if failed:
            break

        concat_list = destination.with_suffix(".concat.txt")
        temp_final = destination.with_name(destination.stem + ".tmp.mp3")
        try:
            with concat_list.open("w", encoding="utf-8") as f:
                for cp in temp_chunks:
                    escaped = str(cp.resolve()).replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
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

        new_jobs.append({
            "part": part,
            "scene": number,
            "status": "completed",
            "text_sha256": text_hash,
            "settings_sha256": settings_hash,
            "output": str(destination),
            "duration": duration,
            "providers": used_providers,
            "chunks": chunk_records,
            "updated_at": now(),
        })
        save_json(MANIFEST_FILE, {
            **manifest, "status": "in_progress", "jobs": new_jobs,
            "completed_scenes": len(new_jobs), "updated_at": now(),
        })
        print(f"DONE Part {part} Scene {number}: {duration:.2f}s")

    if len(new_jobs) != len(expected) or any(
        job.get("status") != "completed" for job in new_jobs
    ):
        save_json(MANIFEST_FILE, {
            **manifest, "status": "incomplete", "jobs": new_jobs,
            "updated_at": now(),
        })
        raise RuntimeError("Not all narration scenes have valid audio.")

    save_json(MANIFEST_FILE, {
        **manifest,
        "status": "completed",
        "jobs": new_jobs,
        "completed_scenes": len(new_jobs),
        "completed_at": now(),
        "updated_at": now(),
    })
    print(f"TTS completed: {len(new_jobs)} scenes")
    print(f"Manifest: {MANIFEST_FILE}")


if __name__ == "__main__":
    main()
