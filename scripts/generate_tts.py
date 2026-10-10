
#!/usr/bin/env python3

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from input_config import (
    load_input_config,
    get_parts,
    get_scenes,
    normalize_format,
)


# ============================================================
# PATHS
# ============================================================

NARRATION_FILE = Path("output/narration/narration.json")
OUTPUT_DIR = Path("output/narration/audio")
MANIFEST_FILE = Path("output/narration/audio_jobs.json")


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


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


def text_hash(text):
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    if not path.is_file():
        raise SystemExit(f"ERROR: Required file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: Could not read {path}: {exc}")

    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: Expected a JSON object in {path}.")

    return data


def load_optional_json(path):
    if not path.is_file():
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Could not read {path}: {exc}")
        return None


def save_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
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


# ============================================================
# COMMAND HELPERS
# ============================================================

def command_exists(command):
    return shutil.which(command) is not None


def run_command(command, retries=1, retry_delay=2):
    retries = max(1, int(retries))
    last_error = "Unknown command error"

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
                or f"exit code {result.returncode}"
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
# TTS ENGINE
# ============================================================

def detect_tts_engine():
    configured = os.environ.get("TTS_ENGINE", "").strip().lower()

    if configured:
        if configured == "edge-tts":
            if command_exists("edge-tts"):
                return "edge-tts"
            raise RuntimeError(
                "TTS_ENGINE=edge-tts was configured, "
                "but edge-tts is not installed."
            )

        if configured in {"espeak", "espeak-ng"}:
            if command_exists(configured):
                return configured
            raise RuntimeError(
                f"TTS_ENGINE={configured} was configured, "
                "but the command is not installed."
            )

        raise RuntimeError(
            f"Unsupported TTS_ENGINE value: {configured}"
        )

    if command_exists("edge-tts"):
        return "edge-tts"

    if command_exists("espeak-ng"):
        return "espeak-ng"

    if command_exists("espeak"):
        return "espeak"

    raise RuntimeError(
        "No supported TTS engine found. "
        "Install edge-tts or espeak-ng."
    )


def resolve_voice(configured_voice):
    voice = str(configured_voice).strip()
    normalized = voice.lower()

    if normalized in {"female", "woman", "f"}:
        return "hi-IN-SwaraNeural"

    if normalized in {"male", "man", "m"}:
        return "hi-IN-MadhurNeural"

    # Preserve a specific user-supplied provider voice.
    return voice


def espeak_speed(speed):
    raw = str(speed).strip().replace("%", "")

    try:
        percentage = int(raw)
    except ValueError:
        percentage = 0

    result = int(165 * (1 + percentage / 100))
    return max(80, min(300, result))


def generate_with_edge_tts(text, output_file, voice, speed):
    if not command_exists("edge-tts"):
        raise RuntimeError("edge-tts command not found.")

    command = [
        "edge-tts",
        "--voice", voice,
        "--rate", speed,
        "--text", text,
        "--write-media", str(output_file),
    ]

    run_command(command, retries=1)


def generate_with_espeak(text, output_file, speed, engine):
    if not command_exists(engine):
        raise RuntimeError(f"{engine} command not found.")

    command = [
        engine,
        "-s", str(espeak_speed(speed)),
        "-w", str(output_file),
        text,
    ]

    run_command(command, retries=1)


def validate_audio_file(path):
    if not path.is_file():
        raise RuntimeError(f"TTS did not create output file: {path}")

    if path.stat().st_size < 100:
        raise RuntimeError(f"TTS output is empty or too small: {path}")

    # FFprobe is installed by the workflow. Validate the audio container
    # when possible, instead of trusting file size alone.
    if command_exists("ffprobe"):
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
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
                f"FFprobe rejected generated audio: {path}"
            )

        try:
            duration = float(result.stdout.strip())
        except (TypeError, ValueError):
            raise RuntimeError(
                f"Could not read audio duration: {path}"
            )

        if duration <= 0:
            raise RuntimeError(f"Audio duration is invalid: {path}")

        return duration

    return 0.0


def generate_audio(text, output_file, voice, speed, engine):
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Generate to a temporary path so a failed attempt cannot leave
    # a partial file that is mistaken for a completed MP3.
    temporary = output_file.with_name(
        output_file.stem + ".tmp" + output_file.suffix
    )

    if temporary.exists():
        temporary.unlink()

    try:
        if engine == "edge-tts":
            generate_with_edge_tts(
                text,
                temporary,
                voice,
                speed,
            )

        elif engine in {"espeak", "espeak-ng"}:
            generate_with_espeak(
                text,
                temporary,
                speed,
                engine,
            )

        else:
            raise RuntimeError(f"Unsupported TTS engine: {engine}")

        duration = validate_audio_file(temporary)
        os.replace(temporary, output_file)
        return duration

    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


# ============================================================
# SCENE HELPERS
# ============================================================

def scene_key(scene):
    if not isinstance(scene, dict):
        return None

    try:
        part = int(scene.get("part"))
        number = int(scene.get("scene"))
    except (TypeError, ValueError):
        return None

    if part < 1 or number < 1:
        return None

    return part, number


def normalize_scenes(data):
    scenes = data.get("scenes", [])

    if not isinstance(scenes, list):
        raise SystemExit(
            "ERROR: narration.json 'scenes' must be a list."
        )

    if any(not isinstance(scene, dict) for scene in scenes):
        raise SystemExit(
            "ERROR: narration.json contains an invalid scene record."
        )

    return [dict(scene) for scene in scenes]


def validate_scenes(scenes, expected_total):
    if len(scenes) != expected_total:
        raise SystemExit(
            "ERROR: Narration scene count mismatch.\n"
            f"Expected: {expected_total}\n"
            f"Found: {len(scenes)}"
        )

    seen = set()

    for scene in scenes:
        key = scene_key(scene)

        if key is None:
            raise SystemExit(
                "ERROR: Invalid part/scene number in narration."
            )

        if key in seen:
            raise SystemExit(
                f"ERROR: Duplicate narration scene: {key}"
            )

        seen.add(key)

        text = scene.get("text")

        if not isinstance(text, str) or not text.strip():
            raise SystemExit(
                f"ERROR: Empty narration for Part {key[0]} "
                f"Scene {key[1]}."
            )

    return seen


def audio_path_for(part, scene_number):
    return (
        OUTPUT_DIR
        / f"part_{int(part):02d}"
        / f"scene_{int(scene_number):02d}.mp3"
    )


# ============================================================
# MANIFEST / CHECKPOINTS
# ============================================================

def load_manifest():
    data = load_optional_json(MANIFEST_FILE)

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
            result[key] = job

    return result


def ordered_jobs(jobs):
    return sorted(
        jobs.values(),
        key=lambda item: (
            int(item.get("part", 0)),
            int(item.get("scene", 0)),
        ),
    )


def save_manifest(manifest, jobs):
    manifest["jobs"] = ordered_jobs(jobs)
    manifest["updated_at"] = utc_now()
    save_json_atomic(MANIFEST_FILE, manifest)


def job_is_reusable(job, output_file, current_hash, engine, voice, speed):
    if not isinstance(job, dict):
        return False

    if job.get("status") != "completed":
        return False

    if job.get("text_sha256") != current_hash:
        return False

    if job.get("engine") != engine:
        return False

    if job.get("provider_voice") != voice:
        return False

    if job.get("speed") != speed:
        return False

    if not output_file.is_file():
        return False

    if output_file.stat().st_size < 100:
        return False

    try:
        validate_audio_file(output_file)
    except Exception as exc:
        print(f"Existing audio failed validation: {exc}")
        return False

    return True


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 54)
    print("             GENERATING TTS")
    print("=" * 54)

    config = load_input_config()

    fmt = normalize_format(config)
    parts = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    voice_config = cfg_text(config, "VOICE", "male")
    provider_voice = resolve_voice(voice_config)
    speed = cfg_text(config, "SPEED", "+0%")
    captions = cfg_text(config, "CAPTIONS", "hindi")

    resume_enabled = cfg_bool(config, "RESUME_ENABLED", True)
    skip_completed = cfg_bool(config, "SKIP_COMPLETED_SCENES", True)
    save_checkpoint = cfg_bool(
        config,
        "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
        True,
    )

    max_retries = positive_int(
        config.get("MAX_RETRIES", 3),
        "MAX_RETRIES",
        default=3,
    )

    failure_policy = cfg_text(
        config,
        "FAILURE_POLICY",
        "retry_then_checkpoint",
    ).lower()

    engine = detect_tts_engine()
    expected_total = parts * scenes_per_part

    print()
    print("========== TTS CONFIG ==========")
    print(f"FORMAT          : {fmt}")
    print(f"PARTS           : {parts}")
    print(f"SCENES/PART     : {scenes_per_part}")
    print(f"VOICE           : {voice_config}")
    print(f"PROVIDER VOICE  : {provider_voice}")
    print(f"SPEED           : {speed}")
    print(f"CAPTIONS        : {captions}")
    print(f"TTS ENGINE      : {engine}")
    print(f"RESUME_ENABLED  : {resume_enabled}")
    print(f"SKIP_COMPLETED  : {skip_completed}")
    print(f"MAX_RETRIES     : {max_retries}")
    print(f"FAILURE_POLICY  : {failure_policy}")
    print("================================")

    narration = load_json(NARRATION_FILE)

    if narration.get("status") != "completed":
        raise SystemExit(
            "ERROR: Narration is not completed. "
            "Run scripts/generate_narration.py first."
        )

    scenes = normalize_scenes(narration)
    expected_keys = validate_scenes(scenes, expected_total)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    jobs = manifest_map(manifest)

    manifest.update({
        "status": "in_progress",
        "format": fmt,
        "voice": voice_config,
        "provider_voice": provider_voice,
        "speed": speed,
        "tts_engine": engine,
        "total_parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(scenes),
        "failure_policy": failure_policy,
        "started_or_resumed_at": utc_now(),
    })

    save_manifest(manifest, jobs)

    completed_count = 0

    for index, scene in enumerate(scenes, start=1):
        key = scene_key(scene)
        part, scene_number = key
        text = scene["text"].strip()
        current_hash = text_hash(text)

        output_file = audio_path_for(part, scene_number)
        previous_job = jobs.get(key)

        if (
            resume_enabled
            and skip_completed
            and job_is_reusable(
                previous_job,
                output_file,
                current_hash,
                engine,
                provider_voice,
                speed,
            )
        ):
            completed_count += 1
            print(
                f"[{index}/{len(scenes)}] "
                f"Verified existing audio: Part {part} "
                f"Scene {scene_number}"
            )
            continue

        print(
            f"\n[{index}/{len(scenes)}] "
            f"Generating Part {part} Scene {scene_number}"
        )

        # A changed narration or TTS setting invalidates the old job.
        job = {
            "part": part,
            "scene": scene_number,
            "status": "in_progress",
            "output": str(output_file),
            "voice": voice_config,
            "provider_voice": provider_voice,
            "speed": speed,
            "engine": engine,
            "text_sha256": current_hash,
            "attempts": 0,
            "started_at": utc_now(),
        }

        jobs[key] = job

        if save_checkpoint:
            save_manifest(manifest, jobs)

        last_error = None
        attempts = max_retries

        for attempt in range(1, attempts + 1):
            job["attempts"] = attempt
            job["last_attempt_at"] = utc_now()

            try:
                duration = generate_audio(
                    text=text,
                    output_file=output_file,
                    voice=provider_voice,
                    speed=speed,
                    engine=engine,
                )

                job.update({
                    "status": "completed",
                    "duration": duration,
                    "size_bytes": output_file.stat().st_size,
                    "completed_at": utc_now(),
                    "error": None,
                })

                jobs[key] = job
                save_manifest(manifest, jobs)
                completed_count += 1

                print(
                    f"Completed Part {part} Scene {scene_number} "
                    f"({output_file.stat().st_size} bytes)"
                )
                break

            except Exception as exc:
                last_error = str(exc)
                job["error"] = last_error
                job["status"] = "retrying" if attempt < attempts else "failed"
                job["last_failed_at"] = utc_now()

                jobs[key] = job
                save_manifest(manifest, jobs)

                print(
                    f"Attempt {attempt}/{attempts} failed for "
                    f"Part {part} Scene {scene_number}: {last_error}"
                )

                if attempt < attempts:
                    time.sleep(min(2 * attempt, 10))

        if job.get("status") != "completed":
            manifest["status"] = "incomplete"
            manifest["failed_scene"] = {
                "part": part,
                "scene": scene_number,
                "error": last_error,
            }
            save_manifest(manifest, jobs)

            # Keep the workflow fail-fast after retries. The manifest
            # remains available for a later resume-enabled run.
            raise SystemExit(
                "ERROR: TTS failed after retries. "
                f"Checkpoint saved for Part {part} Scene {scene_number}. "
                "Fix the TTS issue and rerun the workflow."
            )

        if not save_checkpoint:
            save_manifest(manifest, jobs)

    # --------------------------------------------------------
    # FINAL VALIDATION
    # --------------------------------------------------------

    missing = []

    for scene in scenes:
        key = scene_key(scene)
        part, scene_number = key
        job = jobs.get(key)
        output_file = audio_path_for(part, scene_number)

        if not job_is_reusable(
            job,
            output_file,
            text_hash(scene["text"].strip()),
            engine,
            provider_voice,
            speed,
        ):
            missing.append(key)

    if missing:
        manifest["status"] = "incomplete"
        manifest["missing"] = missing
        save_manifest(manifest, jobs)

        raise SystemExit(
            f"ERROR: TTS output validation failed. Missing/invalid: {missing}"
        )

    manifest["status"] = "completed"
    manifest["completed_scenes"] = len(expected_keys)
    manifest["missing"] = []
    manifest["completed_at"] = utc_now()
    manifest.pop("failed_scene", None)

    save_manifest(manifest, jobs)

    print()
    print("=" * 54)
    print("               TTS COMPLETED")
    print("=" * 54)
    print(f"Total scenes : {len(scenes)}")
    print(f"Verified     : {len(expected_keys)}")
    print(f"Engine       : {engine}")
    print(f"Voice        : {provider_voice}")
    print(f"Manifest     : {MANIFEST_FILE}")
    print("=" * 54)


if __name__ == "__main__":
    main()
