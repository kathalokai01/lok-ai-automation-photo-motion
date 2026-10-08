#!/usr/bin/env python3

import json
import os
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

NARRATION_FILE = Path(
    "output/narration/narration.json"
)

OUTPUT_DIR = Path(
    "output/narration/audio"
)

MANIFEST_FILE = Path(
    "output/narration/audio_jobs.json"
)


# ============================================================
# INPUT CONFIG COMPATIBILITY HELPERS
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

    text = str(value).strip().lower()

    if text in {"true", "yes", "on", "1"}:
        return True

    if text in {"false", "no", "off", "0"}:
        return False

    return bool(default)


def get_failure_policy_value(
    config,
    default="retry_then_checkpoint",
):
    value = config.get(
        "FAILURE_POLICY",
        default,
    )

    if value is None:
        return default

    text = str(value).strip()

    if not text:
        return default

    return text


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    if not path.is_file():
        raise SystemExit(
            f"ERROR: Required file not found: {path}"
        )

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except Exception as exc:
        raise SystemExit(
            f"ERROR: Could not read {path}: {exc}"
        )


def load_optional_json(path):
    if not path.is_file():
        return None

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except Exception as exc:
        print(
            f"WARNING: Could not read {path}: {exc}"
        )
        return None


def save_json_atomic(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = Path(
        f"{path}.tmp"
    )

    with tmp.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")

    os.replace(
        tmp,
        path,
    )


# ============================================================
# COMMAND HELPERS
# ============================================================

def command_exists(command):
    try:
        result = subprocess.run(
            [
                "bash",
                "-lc",
                f"command -v {command}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        return result.returncode == 0

    except Exception:
        return False


def run_command(
    command,
    retries,
    retry_delay,
):
    last_error = None

    for attempt in range(
        1,
        retries + 1,
    ):
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                return result

            last_error = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit code {result.returncode}"
            )

        except Exception as exc:
            last_error = str(exc)

        if attempt < retries:
            print(
                f"Retry {attempt}/{retries - 1} "
                f"after failure..."
            )

            time.sleep(
                retry_delay * attempt
            )

    raise RuntimeError(
        f"Command failed after "
        f"{retries} attempts: "
        f"{last_error}"
    )


# ============================================================
# TTS ENGINE
# ============================================================

def detect_tts_engine():
    configured = os.environ.get(
        "TTS_ENGINE",
        "",
    ).strip().lower()

    if configured:
        return configured

    if command_exists("edge-tts"):
        return "edge-tts"

    if command_exists("espeak-ng"):
        return "espeak-ng"

    if command_exists("espeak"):
        return "espeak"

    return ""


def generate_with_edge_tts(
    text,
    output_file,
    voice,
    speed,
):
    if not command_exists("edge-tts"):
        raise RuntimeError(
            "edge-tts command not found."
        )

    command = [
        "edge-tts",
        "--voice",
        voice,
        "--rate",
        speed,
        "--text",
        text,
        "--write-media",
        str(output_file),
    ]

    run_command(
        command,
        retries=3,
        retry_delay=2,
    )


def generate_with_espeak(
    text,
    output_file,
    voice,
    speed,
):
    command_name = "espeak-ng"

    if not command_exists(
        command_name
    ):
        command_name = "espeak"

    if not command_exists(
        command_name
    ):
        raise RuntimeError(
            "Neither espeak-ng nor espeak "
            "is installed."
        )

    # Convert +0%, -10%, +20% etc. to an
    # approximate espeak speed.
    base_speed = 165

    try:
        numeric = int(
            str(speed)
            .replace("%", "")
            .replace("+", "")
            .strip()
        )

        if str(speed).strip().startswith("-"):
            numeric = -abs(
                int(
                    str(speed)
                    .replace("%", "")
                    .strip()
                )
            )

        elif str(speed).strip().startswith("+"):
            numeric = abs(numeric)

        speech_speed = int(
            base_speed
            * (
                1.0
                + numeric / 100.0
            )
        )

        speech_speed = max(
            80,
            min(
                300,
                speech_speed,
            ),
        )

    except Exception:
        speech_speed = base_speed

    command = [
        command_name,
        "-s",
        str(speech_speed),
        "-w",
        str(output_file),
        text,
    ]

    run_command(
        command,
        retries=3,
        retry_delay=2,
    )


def generate_audio(
    text,
    output_file,
    voice,
    speed,
    engine,
):
    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if engine == "edge-tts":
        generate_with_edge_tts(
            text,
            output_file,
            voice,
            speed,
        )

    elif engine in {
        "espeak",
        "espeak-ng",
    }:
        generate_with_espeak(
            text,
            output_file,
            voice,
            speed,
        )

    else:
        raise RuntimeError(
            "No supported TTS engine available. "
            "Install edge-tts or espeak-ng."
        )

    if not output_file.is_file():
        raise RuntimeError(
            f"TTS did not create output file: "
            f"{output_file}"
        )

    if output_file.stat().st_size <= 0:
        raise RuntimeError(
            f"TTS output file is empty: "
            f"{output_file}"
        )


# ============================================================
# VOICE RESOLUTION
# ============================================================

def resolve_voice(
    configured_voice,
):
    voice = str(
        configured_voice
    ).strip().lower()

    if voice in {
        "female",
        "woman",
        "f",
    }:
        return "hi-IN-SwaraNeural"

    if voice in {
        "male",
        "man",
        "m",
    }:
        return "hi-IN-MadhurNeural"

    # Allow direct provider voice names.
    return str(
        configured_voice
    ).strip()


# ============================================================
# SCENE HELPERS
# ============================================================

def scene_key(scene):
    try:
        return (
            int(scene.get("part")),
            int(scene.get("scene")),
        )
    except Exception:
        return None


def normalize_scenes(data):
    if not isinstance(
        data,
        dict,
    ):
        return []

    scenes = data.get(
        "scenes",
        [],
    )

    if not isinstance(
        scenes,
        list,
    ):
        return []

    result = []

    for scene in scenes:
        if not isinstance(
            scene,
            dict,
        ):
            continue

        result.append(
            dict(scene)
        )

    return result


# ============================================================
# MANIFEST
# ============================================================

def load_manifest():
    data = load_optional_json(
        MANIFEST_FILE
    )

    if not isinstance(
        data,
        dict,
    ):
        return {
            "status": "in_progress",
            "jobs": [],
        }

    if not isinstance(
        data.get("jobs"),
        list,
    ):
        data["jobs"] = []

    return data


def manifest_map(manifest):
    result = {}

    for job in manifest.get(
        "jobs",
        [],
    ):
        if not isinstance(
            job,
            dict,
        ):
            continue

        key = scene_key(
            job
        )

        if key is not None:
            result[key] = job

    return result


# ============================================================
# AUDIO DURATION
# ============================================================

def get_audio_duration(path):
    if not path.is_file():
        return 0.0

    if not command_exists(
        "ffprobe"
    ):
        return 0.0

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return 0.0

        return float(
            result.stdout.strip()
        )

    except Exception:
        return 0.0


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "======================================"
    )
    print(
        "          GENERATING TTS"
    )
    print(
        "======================================"
    )

    # --------------------------------------------------------
    # CENTRALIZED CONFIG
    # --------------------------------------------------------

    config = load_input_config()

    fmt = normalize_format(
        config
    )

    parts = get_parts(
        config
    )

    scenes_per_part = get_scenes(
        config
    )

    voice_config = cfg_text(
        config,
        "VOICE",
        "male",
    )

    speed = cfg_text(
        config,
        "SPEED",
        "+0%",
    )

    captions = cfg_text(
        config,
        "CAPTIONS",
        "hindi",
    )

    failure_policy = (
        get_failure_policy_value(
            config
        )
    )

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

    try:
        max_retries = int(
            config.get(
                "MAX_RETRIES",
                3,
            )
        )
    except Exception:
        max_retries = 3

    max_retries = max(
        1,
        max_retries,
    )

    # --------------------------------------------------------
    # TTS ENGINE
    # --------------------------------------------------------

    engine = detect_tts_engine()

    if not engine:
        raise SystemExit(
            "ERROR: No TTS engine found.\n"
            "Install edge-tts or espeak-ng."
        )

    provider_voice = resolve_voice(
        voice_config
    )

    print()
    print(
        "========== TTS CONFIG =========="
    )
    print(
        f"FORMAT             : {fmt}"
    )
    print(
        f"PARTS              : {parts}"
    )
    print(
        f"SCENES/PART        : {scenes_per_part}"
    )
    print(
        f"VOICE              : {voice_config}"
    )
    print(
        f"PROVIDER VOICE     : {provider_voice}"
    )
    print(
        f"SPEED              : {speed}"
    )
    print(
        f"CAPTIONS           : {captions}"
    )
    print(
        f"TTS ENGINE         : {engine}"
    )
    print(
        f"RESUME_ENABLED     : {resume_enabled}"
    )
    print(
        f"SKIP_COMPLETED     : {skip_completed}"
    )
    print(
        f"MAX_RETRIES        : {max_retries}"
    )
    print(
        f"FAILURE_POLICY     : {failure_policy}"
    )
    print(
        "================================"
    )

    # --------------------------------------------------------
    # NARRATION
    # --------------------------------------------------------

    narration = load_json(
        NARRATION_FILE
    )

    if narration.get(
        "status"
    ) != "completed":
        raise SystemExit(
            "ERROR: Narration is not completed."
        )

    scenes = normalize_scenes(
        narration
    )

    if not scenes:
        raise SystemExit(
            "ERROR: No narration scenes found."
        )

    expected_total = (
        parts
        * scenes_per_part
    )

    if len(scenes) != expected_total:
        raise SystemExit(
            "ERROR: Narration scene count mismatch.\n"
            f"Expected: {expected_total}\n"
            f"Found: {len(scenes)}"
        )

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest = load_manifest()

    manifest["status"] = (
        "in_progress"
    )

    manifest["format"] = fmt
    manifest["voice"] = voice_config
    manifest["provider_voice"] = (
        provider_voice
    )
    manifest["speed"] = speed
    manifest["tts_engine"] = engine
    manifest["total_parts"] = parts
    manifest["scenes_per_part"] = (
        scenes_per_part
    )
    manifest["total_scenes"] = (
        len(scenes)
    )
    manifest["failure_policy"] = (
        failure_policy
    )
    manifest["jobs"] = manifest.get(
        "jobs",
        [],
    )

    jobs = manifest_map(
        manifest
    )

    save_json_atomic(
        MANIFEST_FILE,
        manifest
    )

    # --------------------------------------------------------
    # GENERATE EACH SCENE
    # --------------------------------------------------------

    for index, scene in enumerate(
        scenes,
        start=1,
    ):

        part = scene.get(
            "part"
        )

        scene_number = scene.get(
            "scene"
        )

        key = scene_key(
            scene
        )

        if key is None:
            raise SystemExit(
                f"ERROR: Invalid scene "
                f"number at index {index}."
            )

        text = str(
            scene.get(
                "text",
                ""
            )
        ).strip()

        if not text:
            raise SystemExit(
                f"ERROR: Empty narration "
                f"for Part {part} "
                f"Scene {scene_number}."
            )

        part_dir = (
            OUTPUT_DIR
            / f"part_{int(part):02d}"
        )

        output_file = (
            part_dir
            / f"scene_{int(scene_number):02d}.mp3"
        )

        existing_job = jobs.get(
            key
        )

        # ----------------------------------------------------
        # RESUME
        # ----------------------------------------------------

        if (
            resume_enabled
            and skip_completed
            and existing_job
            and existing_job.get(
                "status"
            ) == "completed"
            and output_file.is_file()
            and output_file.stat().st_size > 0
        ):
            print(
                f"[{index}/{len(scenes)}] "
                f"Skipping completed "
                f"Part {part} Scene "
                f"{scene_number}"
            )
            continue

        print(
            f"[{index}/{len(scenes)}] "
            f"TTS Part {part} Scene "
            f"{scene_number}"
        )

        job = {
            "part": part,
            "scene": scene_number,
            "status": "in_progress",
            "output": str(
                output_file
            ),
            "voice": voice_config,
            "provider_voice": provider_voice,
            "speed": speed,
            "engine": engine,
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        jobs[key] = job

        manifest["jobs"] = sorted(
            jobs.values(),
            key=lambda item: (
                int(item.get("part", 0)),
                int(item.get("scene", 0)),
            ),
        )

        if save_checkpoint:
            save_json_atomic(
                MANIFEST_FILE,
                manifest
            )

        # ----------------------------------------------------
        # GENERATE
        # ----------------------------------------------------

        try:
            generate_audio(
                text=text,
                output_file=output_file,
                voice=provider_voice,
                speed=speed,
                engine=engine,
            )

            duration = (
                get_audio_duration(
                    output_file
                )
            )

            job["status"] = (
                "completed"
            )

            job["duration"] = (
                duration
            )

            job["size_bytes"] = (
                output_file.stat().st_size
            )

            job["completed_at"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            jobs[key] = job

            manifest["jobs"] = sorted(
                jobs.values(),
                key=lambda item: (
                    int(
                        item.get(
                            "part",
                            0
                        )
                    ),
                    int(
                        item.get(
                            "scene",
                            0
                        )
                    ),
                ),
            )

            if save_checkpoint:
                save_json_atomic(
                    MANIFEST_FILE,
                    manifest
                )

            print(
                f"Completed: "
                f"Part {part} Scene "
                f"{scene_number}"
            )

        except Exception as exc:

            job["status"] = (
                "failed"
            )

            job["error"] = str(
                exc
            )

            job["failed_at"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            jobs[key] = job

            manifest["jobs"] = sorted(
                jobs.values(),
                key=lambda item: (
                    int(
                        item.get(
                            "part",
                            0
                        )
                    ),
                    int(
                        item.get(
                            "scene",
                            0
                        )
                    ),
                ),
            )

            save_json_atomic(
                MANIFEST_FILE,
                manifest
            )

            print(
                f"FAILED: Part {part} "
                f"Scene {scene_number}: "
                f"{exc}"
            )

            # ------------------------------------------------
            # FAILURE POLICY
            # ------------------------------------------------

            policy = (
                failure_policy
                .strip()
                .lower()
            )

            if (
                policy
                in {
                    "checkpoint",
                    "retry_then_checkpoint",
                    "retry",
                }
            ):
                raise SystemExit(
                    "ERROR: TTS generation failed. "
                    "Checkpoint saved. "
                    "Resume is enabled for the "
                    "next workflow run."
                )

            raise

    # --------------------------------------------------------
    # FINAL VALIDATION
    # --------------------------------------------------------

    completed = []

    for scene in scenes:

        key = scene_key(
            scene
        )

        if key is None:
            continue

        job = jobs.get(
            key
        )

        if not job:
            continue

        if (
            job.get("status")
            != "completed"
        ):
            continue

        output_path = Path(
            job.get(
                "output",
                ""
            )
        )

        if (
            output_path.is_file()
            and output_path.stat().st_size > 0
        ):
            completed.append(
                key
            )

    if len(completed) != len(
        scenes
    ):
        missing = []

        for scene in scenes:
            key = scene_key(
                scene
            )

            if key not in completed:
                missing.append(
                    key
                )

        manifest["status"] = (
            "incomplete"
        )

        manifest["missing"] = (
            missing
        )

        save_json_atomic(
            MANIFEST_FILE,
            manifest
        )

        raise SystemExit(
            "ERROR: Not all TTS files "
            "were generated.\n"
            f"Missing: {missing}"
        )

    # --------------------------------------------------------
    # FINAL MANIFEST
    # --------------------------------------------------------

    manifest["status"] = (
        "completed"
    )

    manifest["completed_scenes"] = (
        len(completed)
    )

    manifest["missing"] = []

    manifest["completed_at"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    save_json_atomic(
        MANIFEST_FILE,
        manifest
    )

    print()
    print(
        "======================================"
    )
    print(
        "          TTS COMPLETED"
    )
    print(
        "======================================"
    )
    print(
        f"Total scenes : {len(scenes)}"
    )
    print(
        f"Completed    : {len(completed)}"
    )
    print(
        f"Engine       : {engine}"
    )
    print(
        f"Voice        : {provider_voice}"
    )
    print(
        f"Manifest     : {MANIFEST_FILE}"
    )
    print(
        "======================================"
    )


if __name__ == "__main__":
    main()
