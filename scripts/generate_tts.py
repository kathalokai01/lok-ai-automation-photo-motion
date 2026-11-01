#!/usr/bin/env python3

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
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

SCRIPT_VERSION = 4
MIN_AUDIO_BYTES = 1000


# ============================================================
# CONFIGURATION
# ============================================================

def cfg_text(config, key, default=""):
    value = config.get(key, default)
    if value is None:
        return str(default)
    return str(value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()

    if value in {"true", "yes", "on", "1"}:
        return True

    if value in {"false", "no", "off", "0"}:
        return False

    return bool(default)


def cfg_int(config, key, default, minimum=0):
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default

    return max(minimum, value)


def get_failure_policy(config):
    value = cfg_text(
        config,
        "FAILURE_POLICY",
        "retry_then_checkpoint",
    ).lower()

    allowed = {
        "retry_then_checkpoint",
        "checkpoint",
        "retry",
        "skip",
        "stop",
    }

    if value not in allowed:
        raise RuntimeError(
            f"Unsupported FAILURE_POLICY: {value}"
        )

    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# JSON AND FILE HELPERS
# ============================================================

def load_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(
                f"Required file not found: {path}"
            )
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    except Exception as exc:
        raise RuntimeError(
            f"Could not read JSON {path}: {exc}"
        ) from exc


def save_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, path)

    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(payload).hexdigest()


def file_is_valid(path):
    return (
        path.is_file()
        and path.stat().st_size >= MIN_AUDIO_BYTES
    )


def remove_partial_file(path):
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# ============================================================
# COMMAND EXECUTION
# ============================================================

def command_exists(command):
    return shutil.which(command) is not None


def run_command(command, retries, retry_delay):
    last_error = "Unknown command failure"

    for attempt in range(1, retries + 1):
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=300,
            )

            if result.returncode == 0:
                return result

            last_error = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"Exit code {result.returncode}"
            )

        except subprocess.TimeoutExpired:
            last_error = "Command timed out after 300 seconds"

        except Exception as exc:
            last_error = str(exc)

        if attempt < retries:
            delay = retry_delay * attempt
            print(
                f"Command attempt {attempt}/{retries} failed; "
                f"retrying in {delay}s."
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Command failed after {retries} attempts: {last_error}"
    )


# ============================================================
# TTS ENGINE SELECTION
# ============================================================

def detect_tts_engine():
    configured = os.environ.get(
        "TTS_ENGINE",
        "",
    ).strip().lower()

    if configured:
        if configured in {"edge", "edge_tts"}:
            configured = "edge-tts"

        if configured not in {
            "edge-tts",
            "espeak",
            "espeak-ng",
        }:
            raise RuntimeError(
                f"Unsupported TTS_ENGINE={configured}. "
                "Supported engines in this script: "
                "edge-tts, espeak-ng, espeak."
            )

        if configured == "edge-tts":
            if not command_exists("edge-tts"):
                raise RuntimeError(
                    "TTS_ENGINE=edge-tts but edge-tts is not installed."
                )

        elif configured == "espeak-ng":
            if not command_exists("espeak-ng"):
                raise RuntimeError(
                    "TTS_ENGINE=espeak-ng but espeak-ng is not installed."
                )

        elif configured == "espeak":
            if not command_exists("espeak"):
                raise RuntimeError(
                    "TTS_ENGINE=espeak but espeak is not installed."
                )

        return configured

    if command_exists("edge-tts"):
        return "edge-tts"

    if command_exists("espeak-ng"):
        return "espeak-ng"

    if command_exists("espeak"):
        return "espeak"

    return ""


# ============================================================
# VOICE AND SPEED
# ============================================================

def resolve_voice(configured_voice):
    voice = str(configured_voice).strip()

    lowered = voice.lower()

    if lowered in {"female", "woman", "f"}:
        return "hi-IN-SwaraNeural"

    if lowered in {"male", "man", "m"}:
        return "hi-IN-MadhurNeural"

    if not voice:
        return "hi-IN-MadhurNeural"

    return voice


def normalize_speed(speed):
    value = str(speed).strip()

    if not value:
        return "+0%"

    if value.lower() in {"normal", "default"}:
        return "+0%"

    if not value.endswith("%"):
        value += "%"

    numeric = value[:-1]

    if numeric.startswith(("+", "-")):
        numeric = numeric[1:]

    if not numeric.isdigit():
        raise ValueError(
            f"Invalid SPEED value: {speed}"
        )

    amount = int(numeric)

    if amount > 50:
        raise ValueError(
            "SPEED must be between -50% and +50%."
        )

    if value.startswith("-"):
        return f"-{amount}%"

    return f"+{amount}%"


# ============================================================
# ENGINE IMPLEMENTATIONS
# ============================================================

def generate_with_edge_tts(
    text,
    output_file,
    voice,
    speed,
    retries,
):
    if not command_exists("edge-tts"):
        raise RuntimeError("edge-tts executable is unavailable.")

    temporary = output_file.with_name(
        output_file.stem + ".partial.mp3"
    )

    remove_partial_file(temporary)

    command = [
        "edge-tts",
        "--voice",
        voice,
        "--rate",
        speed,
        "--text",
        text,
        "--write-media",
        str(temporary),
    ]

    try:
        run_command(
            command,
            retries=retries,
            retry_delay=2,
        )

        if not file_is_valid(temporary):
            raise RuntimeError(
                "Edge TTS produced a missing or undersized audio file."
            )

        os.replace(temporary, output_file)

    finally:
        remove_partial_file(temporary)


def speed_to_espeak(speed):
    normalized = normalize_speed(speed)

    amount = int(normalized[:-1])
    if normalized.startswith("-"):
        amount = -amount

    result = int(165 * (1 + amount / 100.0))

    return max(80, min(300, result))


def generate_with_espeak(
    text,
    output_file,
    voice,
    speed,
    engine,
    retries,
):
    executable = engine

    if not command_exists(executable):
        raise RuntimeError(
            f"{executable} executable is unavailable."
        )

    temporary_wav = output_file.with_suffix(".partial.wav")

    remove_partial_file(temporary_wav)

    command = [
        executable,
        "-s",
        str(speed_to_espeak(speed)),
        "-w",
        str(temporary_wav),
        text,
    ]

    # espeak voice identifiers are passed only when they
    # are valid engine voice names; Neural voice names are
    # not valid eSpeak voice identifiers.
    if voice and not voice.lower().endswith("neural"):
        command[1:1] = ["-v", voice]

    try:
        run_command(
            command,
            retries=retries,
            retry_delay=2,
        )

        if not file_is_valid(temporary_wav):
            raise RuntimeError(
                "eSpeak produced a missing or undersized WAV file."
            )

        if not command_exists("ffmpeg"):
            raise RuntimeError(
                "FFmpeg is required to convert eSpeak WAV to MP3."
            )

        temporary_mp3 = output_file.with_name(
            output_file.stem + ".partial.mp3"
        )

        remove_partial_file(temporary_mp3)

        try:
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(temporary_wav),
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "3",
                    str(temporary_mp3),
                ],
                retries=retries,
                retry_delay=2,
            )

            if not file_is_valid(temporary_mp3):
                raise RuntimeError(
                    "WAV-to-MP3 conversion produced invalid output."
                )

            os.replace(temporary_mp3, output_file)

        finally:
            remove_partial_file(temporary_mp3)

    finally:
        remove_partial_file(temporary_wav)


def generate_audio(
    text,
    output_file,
    voice,
    speed,
    engine,
    retries,
):
    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    remove_partial_file(output_file)

    if engine == "edge-tts":
        generate_with_edge_tts(
            text,
            output_file,
            voice,
            speed,
            retries,
        )

    elif engine in {"espeak", "espeak-ng"}:
        generate_with_espeak(
            text,
            output_file,
            voice,
            speed,
            engine,
            retries,
        )

    else:
        raise RuntimeError(
            f"Unsupported TTS engine: {engine}"
        )

    if not file_is_valid(output_file):
        raise RuntimeError(
            f"Generated audio failed validation: {output_file}"
        )

# ============================================================
# AUDIO VALIDATION
# ============================================================

def get_audio_duration(path):
    if not file_is_valid(path):
        return 0.0

    if not command_exists("ffprobe"):
        raise RuntimeError(
            "ffprobe is required to validate generated audio duration."
        )

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe could not read audio: {path}"
        )

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Invalid audio duration: {path}"
        )

    if duration <= 0:
        raise RuntimeError(
            f"Audio duration is zero or negative: {path}"
        )

    return round(duration, 3)


# ============================================================
# SCENE VALIDATION
# ============================================================

def scene_key(scene):
    try:
        part = int(scene.get("part"))
        number = int(scene.get("scene"))

        if part < 1 or number < 1:
            return None

        return part, number

    except (TypeError, ValueError, AttributeError):
        return None


def normalize_scenes(data):
    if not isinstance(data, dict):
        raise RuntimeError(
            "Narration JSON must be an object."
        )

    scenes = data.get("scenes")

    if not isinstance(scenes, list):
        raise RuntimeError(
            "Narration JSON has no valid scenes list."
        )

    return scenes


def validate_narration_scenes(
    narration,
    parts,
    scenes_per_part,
):
    if narration.get("status") != "completed":
        raise RuntimeError(
            "Narration is not marked completed."
        )

    scenes = normalize_scenes(narration)

    expected = {
        (part, number)
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    }

    scene_map = {}

    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Narration scene {index} is not an object."
            )

        key = scene_key(scene)

        if key is None:
            raise RuntimeError(
                f"Invalid part/scene number at narration index {index}."
            )

        if key not in expected:
            raise RuntimeError(
                f"Unexpected narration scene: Part {key[0]} Scene {key[1]}"
            )

        if key in scene_map:
            raise RuntimeError(
                f"Duplicate narration scene: Part {key[0]} Scene {key[1]}"
            )

        text = scene.get("text")

        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(
                f"Empty narration: Part {key[0]} Scene {key[1]}"
            )

        scene_map[key] = dict(scene)

    missing = sorted(expected - set(scene_map))

    if missing:
        raise RuntimeError(
            f"Narration is missing configured scenes: {missing}"
        )

    return [
        scene_map[(part, number)]
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    ]


# ============================================================
# MANIFEST
# ============================================================

def load_manifest():
    data = load_json(
        MANIFEST_FILE,
        required=False,
    )

    if not isinstance(data, dict):
        return {
            "status": "in_progress",
            "jobs": [],
        }

    if not isinstance(data.get("jobs"), list):
        data["jobs"] = []

    return data


def manifest_map(manifest):
    result = {}

    for job in manifest.get("jobs", []):
        if not isinstance(job, dict):
            continue

        key = scene_key(job)

        if key is not None:
            result[key] = dict(job)

    return result


def ordered_jobs(jobs):
    return sorted(
        jobs.values(),
        key=lambda item: (
            int(item.get("part", 0)),
            int(item.get("scene", 0)),
        ),
    )


def save_manifest(
    manifest,
    jobs,
    status=None,
):
    if status:
        manifest["status"] = status

    manifest["jobs"] = ordered_jobs(jobs)
    manifest["updated_at"] = utc_now()

    save_json_atomic(
        MANIFEST_FILE,
        manifest,
    )


# ============================================================
# CACHE VALIDATION
# ============================================================

def scene_fingerprint(
    text,
    voice,
    speed,
    engine,
    provider_voice,
):
    return stable_hash({
        "script_version": SCRIPT_VERSION,
        "text": text,
        "voice": voice,
        "speed": speed,
        "engine": engine,
        "provider_voice": provider_voice,
    })


def reusable_audio(
    job,
    output_file,
    fingerprint,
):
    if not isinstance(job, dict):
        return False

    if job.get("status") != "completed":
        return False

    if job.get("fingerprint") != fingerprint:
        return False

    if not file_is_valid(output_file):
        return False

    try:
        duration = get_audio_duration(output_file)
    except Exception:
        return False

    return duration > 0


# ============================================================
# OUTPUT PATH
# ============================================================

def scene_output_path(part, scene):
    return (
        OUTPUT_DIR
        / f"part_{part:02d}"
        / f"scene_{scene:02d}.mp3"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("             GENERATING HINDI TTS")
    print("=" * 60)

    # --------------------------------------------------------
    # CONFIG
    # --------------------------------------------------------

    config = load_input_config()

    fmt = normalize_format(config)
    parts = get_parts(config)
    scenes_per_part = get_scenes(config)

    voice_config = cfg_text(
        config,
        "VOICE",
        "male",
    )

    speed = normalize_speed(
        cfg_text(config, "SPEED", "+0%")
    )

    provider_voice = resolve_voice(
        voice_config
    )

    failure_policy = get_failure_policy(config)

    resume_enabled = cfg_bool(
        config,
        "RESUME_ENABLED",
        True,
    )

    skip_completed = cfg_bool(
        config,
        "SKIP_COMPLETED_SCENES",
        True,
    )

    save_checkpoint = cfg_bool(
        config,
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
        True,
    )

    max_retries = cfg_int(
        config,
        "MAX_RETRIES",
        3,
        minimum=1,
    )

    engine = detect_tts_engine()

    if not engine:
        raise RuntimeError(
            "No supported TTS engine is installed. "
            "Install edge-tts or eSpeak."
        )

    # --------------------------------------------------------
    # INPUT NARRATION
    # --------------------------------------------------------

    narration = load_json(NARRATION_FILE)

    scenes = validate_narration_scenes(
        narration,
        parts,
        scenes_per_part,
    )

    expected_total = parts * scenes_per_part

    # --------------------------------------------------------
    # GENERATION SIGNATURE
    # --------------------------------------------------------

    generation_signature = stable_hash({
        "script_version": SCRIPT_VERSION,
        "format": fmt,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "voice": voice_config,
        "provider_voice": provider_voice,
        "speed": speed,
        "engine": engine,
        "narration_signature": narration.get(
            "source_signature",
            stable_hash(scenes),
        ),
    })

    print(f"FORMAT             : {fmt}")
    print(f"PARTS              : {parts}")
    print(f"SCENES/PART        : {scenes_per_part}")
    print(f"TOTAL SCENES       : {expected_total}")
    print(f"VOICE              : {voice_config}")
    print(f"PROVIDER VOICE     : {provider_voice}")
    print(f"SPEED              : {speed}")
    print(f"TTS ENGINE         : {engine}")
    print(f"MAX RETRIES        : {max_retries}")
    print(f"RESUME             : {resume_enabled}")
    print(f"SKIP COMPLETED     : {skip_completed}")
    print(f"FAILURE POLICY     : {failure_policy}")

    # --------------------------------------------------------
    # RESTORE CHECKPOINT
    # --------------------------------------------------------

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest = load_manifest()

    previous_signature = manifest.get(
        "generation_signature"
    )

    if previous_signature != generation_signature:
        print(
            "TTS input/settings changed. "
            "Old completion records will not be reused."
        )
        jobs = {}
    else:
        jobs = manifest_map(manifest)

    manifest.update({
        "script_version": SCRIPT_VERSION,
        "format": fmt,
        "voice": voice_config,
        "provider_voice": provider_voice,
        "speed": speed,
        "tts_engine": engine,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "generation_signature": generation_signature,
        "failure_policy": failure_policy,
        "status": "in_progress",
    })

    save_manifest(
        manifest,
        jobs,
        status="in_progress",
    )

    # --------------------------------------------------------
    # PROCESS SCENES IN PART ORDER
    # --------------------------------------------------------

    failed_scenes = []
    skipped_scenes = []
    completed_scenes = []

    for index, scene in enumerate(scenes, start=1):
        part, number = scene_key(scene)
        text = scene["text"].strip()

        output_file = scene_output_path(
            part,
            number,
        )

        fingerprint = scene_fingerprint(
            text=text,
            voice=voice_config,
            speed=speed,
            engine=engine,
            provider_voice=provider_voice,
        )

        existing_job = jobs.get(
            (part, number)
        )

        # ----------------------------------------------------
        # RESUME ONLY VERIFIED MATCHING AUDIO
        # ----------------------------------------------------

        if (
            resume_enabled
            and skip_completed
            and reusable_audio(
                existing_job,
                output_file,
                fingerprint,
            )
        ):
            print(
                f"[{index}/{expected_total}] "
                f"REUSED Part {part} Scene {number}"
            )

            skipped_scenes.append((part, number))
            completed_scenes.append((part, number))
            continue

        # A file without a matching manifest/fingerprint
        # is not treated as a successful checkpoint.
        remove_partial_file(output_file)

        job = {
            "part": part,
            "scene": number,
            "status": "in_progress",
            "output": str(output_file),
            "voice": voice_config,
            "provider_voice": provider_voice,
            "speed": speed,
            "engine": engine,
            "fingerprint": fingerprint,
            "started_at": utc_now(),
        }

        jobs[(part, number)] = job

        save_manifest(
            manifest,
            jobs,
            status="in_progress",
        )

        print(
            f"[{index}/{expected_total}] "
            f"GENERATING Part {part} Scene {number}"
        )

        # ----------------------------------------------------
        # GENERATE AND VALIDATE
        # ----------------------------------------------------

        try:
            generate_audio(
                text=text,
                output_file=output_file,
                voice=provider_voice,
                speed=speed,
                engine=engine,
                retries=max_retries,
            )

            duration = get_audio_duration(
                output_file
            )

            if duration <= 0:
                raise RuntimeError(
                    "Generated audio has invalid duration."
                )

            job.update({
                "status": "completed",
                "duration": duration,
                "size_bytes": output_file.stat().st_size,
                "completed_at": utc_now(),
                "error": None,
            })

            jobs[(part, number)] = job
            completed_scenes.append((part, number))

            # Always save successful scene state when the
            # configured per-scene checkpoint option is true.
            if save_checkpoint:
                save_manifest(
                    manifest,
                    jobs,
                    status="in_progress",
                )

            print(
                f"SUCCESS Part {part} Scene {number} "
                f"| {duration:.2f}s"
            )

        except Exception as exc:
            remove_partial_file(output_file)

            job.update({
                "status": "failed",
                "error": str(exc),
                "failed_at": utc_now(),
            })

            jobs[(part, number)] = job
            failed_scenes.append((part, number, str(exc)))

            # Save failure state regardless of the
            # success-checkpoint setting.
            save_manifest(
                manifest,
                jobs,
                status="incomplete",
            )

            print(
                f"FAILED Part {part} Scene {number}: {exc}"
            )

            if failure_policy != "skip":
                raise RuntimeError(
                    "TTS stopped after a scene failure. "
                    "The failure checkpoint was saved; "
                    "successful matching scenes will be reused "
                    "on the next run."
                ) from exc

            print(
                "FAILURE_POLICY=skip; moving to the next scene."
            )

    # --------------------------------------------------------
    # FINAL VALIDATION
    # --------------------------------------------------------

    missing = []

    for scene in scenes:
        part, number = scene_key(scene)
        job = jobs.get((part, number))
        output_file = scene_output_path(part, number)

        if not reusable_audio(
            job,
            output_file,
            scene_fingerprint(
                text=scene["text"].strip(),
                voice=voice_config,
                speed=speed,
                engine=engine,
                provider_voice=provider_voice,
            ),
        ):
            missing.append((part, number))

    if missing:
        manifest["missing_scenes"] = missing

        save_manifest(
            manifest,
            jobs,
            status="incomplete",
        )

        raise RuntimeError(
            f"TTS incomplete: {len(missing)}/{expected_total} "
            f"scenes are missing or invalid: {missing}"
        )

    # --------------------------------------------------------
    # COMPLETED MANIFEST
    # --------------------------------------------------------

    manifest.update({
        "status": "completed",
        "completed_scenes": expected_total,
        "missing_scenes": [],
        "failed_scenes": [],
        "completed_at": utc_now(),
    })

    save_manifest(
        manifest,
        jobs,
        status="completed",
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("               TTS COMPLETE")
    print("=" * 60)
    print(f"Format            : {fmt}")
    print(f"Engine            : {engine}")
    print(f"Expected scenes   : {expected_total}")
    print(f"Generated/reused  : {len(completed_scenes)}")
    print(f"Reused checkpoints: {len(skipped_scenes)}")
    print(f"Failed scenes     : {len(failed_scenes)}")
    print(f"Audio directory   : {OUTPUT_DIR}")
    print(f"Manifest          : {MANIFEST_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)