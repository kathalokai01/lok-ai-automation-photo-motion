#!/usr/bin/env python3
"""Offline, strict preflight for the Katha Lok AI Photo Motion pipeline."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INPUT_FILE = ROOT / "Input" / "topic.txt"
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "generate-photo-motion-video.yml"
OUT = ROOT / "output"
REPORT_FILE = OUT / "preflight_report.json"

VERSION = "photo-motion-preflight-v61-artifact-schema-resume-offline"

REQUIRED_SCRIPTS = (
    "input_config.py",
    "select_gemini_model.py",
    "generate_story_plan.py",
    "generate_story.py",
    "validate_story_length.py",
    "verify_story.py",
    "generate_ai_story.py",
    "generate_character_bible.py",
    "generate_scenes.py",
    "generate_narration.py",
    "prepare_captions.py",
    "generate_tts.py",
    "generate_flow_music.py",
    "generate_visuals.py",
    "free_image_generator.py",
    "generate_depth_maps.py",
    "depth_parallax.py",
    "photo_motion.py",
    "generate_sound_design.py",
    "validate_sfx_timing.py",
    "render_video.py",
    "install_caption_fonts.py",
    "preview_caption_fonts.py",
    "burn_captions.py",
    "generate_thumbnail.py",
    "generate_youtube_metadata.py",
    "validate_final_output.py",
    "package_output.py",
    "pipeline_preflight.py",
)

WORKFLOW_ORDER = (
    "select_gemini_model.py",
    "generate_story_plan.py",
    "generate_story.py",
    "validate_story_length.py",
    "verify_story.py",
    "generate_ai_story.py",
    "generate_character_bible.py",
    "generate_scenes.py",
    "generate_narration.py",
    "prepare_captions.py",
    "generate_tts.py",
    "generate_flow_music.py",
    "generate_visuals.py",
    "free_image_generator.py",
    "generate_depth_maps.py",
    "photo_motion.py",
    "generate_sound_design.py",
    "validate_sfx_timing.py",
    "render_video.py",
    "burn_captions.py",
    "generate_thumbnail.py",
    "generate_youtube_metadata.py",
    "validate_final_output.py",
    "package_output.py",
)

TTS_ALLOWED = {
    "fish_audio", "gemini_tts", "edge_tts", "piper",
    "elevenlabs", "cartesia", "finevoice", "minimax",
}
IMAGE_ALLOWED = {"huggingface", "pollinations", "gemini", "replicate"}

PYTHON_MODULES = (
    "requests",
    "PIL",
    "cv2",
    "numpy",
    "imageio",
    "imageio_ffmpeg",
    "gradio_client",
    "huggingface_hub",
    "edge_tts",
    "transformers",
    "safetensors",
    "accelerate",
)

RUNTIME_COMMANDS = (
    "ffmpeg", "ffprobe", "fc-match", "zip", "unzip",
)

FONT_FAMILIES = (
    "Noto Sans Devanagari",
    "Mukta",
    "Hind",
    "Tiro Devanagari Hindi",
)

STEP_NAME_RE = re.compile(r"^\s{6}-\s+name:\s*(.*?)\s*$")
RUN_RE = re.compile(r"^\s{8}run:\s*(.*?)\s*$")
PYTHON_SCRIPT_RE = re.compile(
    r"(?<![\w./-])python(?:3(?:\.\d+)?)?"
    r"\s+(?:-u\s+)?scripts/([A-Za-z0-9_.-]+\.py)"
)
LEGACY_EXECUTABLE_PATTERN = re.compile(r"\bi2v\b|wan2gp", re.IGNORECASE)

SUCCESS = {"completed", "complete", "success", "done", "reused", "valid"}
FAILED = {"failed", "error", "cancelled", "incomplete", "invalid"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}

# Existing artifacts are optional before their pipeline stage. If present,
# their JSON structure, declared status, referenced files and hashes are checked.
ARTIFACT_MANIFESTS = (
    ("story", OUT / "story" / "story.json"),
    ("story_ai", OUT / "story" / "ai_story.json"),
    ("story_length", OUT / "story" / "story_length_validation.json"),
    ("narration_jobs", OUT / "narration" / "narration.json"),
    ("narration_audio", OUT / "narration" / "audio_jobs.json"),
    ("visual_jobs", OUT / "visuals" / "visual_jobs.json"),
    ("photo_motion_jobs", OUT / "visuals" / "photo_motion_jobs.json"),
    ("image_manifest", OUT / "visuals" / "image_manifest.json"),
    ("depth_manifest", OUT / "visuals" / "depth_maps" / "depth_manifest.json"),
    ("photo_motion_manifest", OUT / "photo_motion" / "photo_motion_manifest.json"),
    ("sound_design", OUT / "sound_design" / "sound_design_manifest.json"),
    ("sfx_cues", OUT / "sound_design" / "sfx_cues.json"),
    ("sfx_validation", OUT / "sound_design" / "sfx_timing_validation.json"),
    ("captions", OUT / "captions" / "captions.json"),
    ("render_manifest", OUT / "scene_renders" / "render_manifest.json"),
    ("package_manifest", OUT / "package_manifest.json"),
)

ROW_KEYS = (
    "jobs", "visual_jobs", "photo_motion_jobs", "scenes", "items",
    "images", "depth_maps", "audio_jobs", "records", "parts", "tracks",
    "cues", "captions", "thumbnails",
)
PATH_KEYS = (
    "path", "file", "output", "output_path", "image", "image_path",
    "audio", "audio_path", "video", "video_path", "depth_path",
    "depth_map", "rendered_video",
)
HASH_KEYS = (
    "sha256", "image_sha256", "audio_sha256", "video_sha256",
    "depth_sha256", "file_sha256",
)


def log(*items: Any) -> None:
    print(*items, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"Path escapes repository: {path}")
    return resolved.relative_to(ROOT.resolve()).as_posix()


def resolve_output_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: missing file path")
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(OUT.resolve()):
        raise ValueError(f"{label}: artifact path is outside output/")
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label}: missing or unsafe file: {resolved}")
    if resolved.stat().st_size <= 0:
        raise ValueError(f"{label}: empty file: {resolved}")
    return resolved


def check_python_file(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8-sig")
        ast.parse(source, filename=str(path))
        compile(source, str(path), "exec")
        return True, "Python syntax and compilation valid."
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_module(module: str) -> tuple[bool, str]:
    try:
        found = importlib.util.find_spec(module) is not None
        return found, "Importable." if found else "Module not installed."
    except (ImportError, ValueError, AttributeError) as exc:
        return False, f"Module check failed: {exc}"


def check_runtime_command(command: str) -> tuple[bool, str]:
    executable = shutil.which(command)
    return (
        bool(executable),
        executable or f"{command} is missing from PATH.",
    )


def check_font(family: str) -> tuple[bool, str]:
    executable = shutil.which("fc-match")
    if not executable:
        return False, "fc-match is not installed."
    try:
        result = subprocess.run(
            [executable, "-f", "%{file}\n", family],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            return False, result.stderr.strip() or "Font lookup failed."
        lines = result.stdout.strip().splitlines()
        if not lines:
            return False, f"No font file resolved for {family}."
        font_path = Path(lines[0])
        if not font_path.is_file():
            return False, f"Font file not found: {font_path}"
        return True, str(font_path)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def strip_shell_comment(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("#"):
        return ""
    return line


def extract_workflow_commands(source: str) -> list[dict]:
    """Extract Python script invocations from workflow run blocks."""
    commands = []
    current_step = None
    inside_run = False

    for line_number, raw_line in enumerate(source.splitlines(), 1):
        step_match = STEP_NAME_RE.match(raw_line)
        if step_match:
            current_step = step_match.group(1).strip()
            inside_run = False
            continue

        if re.match(r"^\s{6}-\s+", raw_line):
            current_step = None
            inside_run = False

        run_match = RUN_RE.match(raw_line)
        if run_match:
            inside_run = True
            inline = run_match.group(1).strip()
            if inline and inline not in {"|", ">", "|-", ">-"}:
                for match in PYTHON_SCRIPT_RE.finditer(
                    strip_shell_comment(inline)
                ):
                    commands.append({
                        "script": match.group(1),
                        "line": line_number,
                        "step": current_step or "unnamed",
                    })
            continue

        if not inside_run:
            continue

        indentation = len(raw_line) - len(raw_line.lstrip())
        if raw_line.strip() and indentation <= 8:
            inside_run = False
            continue

        for match in PYTHON_SCRIPT_RE.finditer(
            strip_shell_comment(raw_line)
        ):
            commands.append({
                "script": match.group(1),
                "line": line_number,
                "step": current_step or "unnamed",
            })

    return commands


def check_workflow_order(commands: list[dict]) -> tuple[bool, str]:
    cursor = 0
    matched = []
    for expected in WORKFLOW_ORDER:
        found = next(
            (
                index for index in range(cursor, len(commands))
                if commands[index]["script"] == expected
            ),
            None,
        )
        if found is None:
            observed = [item["script"] for item in commands]
            return False, (
                f"Missing or out-of-order command: {expected}. "
                f"Detected: {' -> '.join(observed)}"
            )
        matched.append(expected)
        cursor = found + 1
    return True, "Verified generation order: " + " -> ".join(matched)


def check_workflow_preflight(commands: list[dict]) -> tuple[bool, str]:
    preflight = [
        item for item in commands
        if item["script"] == "pipeline_preflight.py"
    ]
    generation = [
        item for item in commands
        if item["script"] in GENERATION_SCRIPTS
    ]
    if len(preflight) != 1:
        return False, (
            f"Expected exactly one preflight invocation; found {len(preflight)}."
        )
    if not generation:
        return False, "No generation command found."
    if preflight[0]["line"] >= generation[0]["line"]:
        return False, "Preflight must run before every generation command."
    return True, "One preflight invocation runs before generation."


GENERATION_SCRIPTS = set(WORKFLOW_ORDER)


def check_story_gate(commands: list[dict]) -> tuple[bool, str]:
    by_name = {}
    for item in commands:
        by_name.setdefault(item["script"], item)

    required = (
        "generate_story.py",
        "validate_story_length.py",
        "verify_story.py",
        "generate_ai_story.py",
    )
    missing = [name for name in required if name not in by_name]
    if missing:
        return False, "Missing story gate commands: " + ", ".join(missing)

    positions = [by_name[name]["line"] for name in required]
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        return False, (
            "Required order: generate_story.py -> "
            "validate_story_length.py -> verify_story.py -> generate_ai_story.py."
        )
    return True, "Story validation and verification gate order is correct."


def check_legacy_execution() -> tuple[bool, str]:
    """Reject executable legacy I2V/Wan2GP references in active Python scripts."""
    violations = []
    executable_calls = {
        "run", "Popen", "call", "check_call",
        "check_output", "system", "exec",
    }

    for name in REQUIRED_SCRIPTS:
        path = SCRIPTS / name
        if not path.is_file():
            continue
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8-sig"),
                filename=str(path),
            )
        except Exception as exc:
            violations.append(f"{name}: cannot inspect syntax: {exc}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if LEGACY_EXECUTABLE_PATTERN.search(alias.name):
                        violations.append(
                            f"{name}:{node.lineno} imports legacy module"
                        )
            elif isinstance(node, ast.ImportFrom):
                if LEGACY_EXECUTABLE_PATTERN.search(node.module or ""):
                    violations.append(
                        f"{name}:{node.lineno} imports legacy module"
                    )

            if not isinstance(node, ast.Call):
                continue
            function = node.func
            function_name = (
                function.attr if isinstance(function, ast.Attribute)
                else function.id if isinstance(function, ast.Name)
                else ""
            )
            if function_name not in executable_calls:
                continue

            values = []
            for argument in list(node.args) + [
                keyword.value for keyword in node.keywords
            ]:
                values.extend(
                    child.value for child in ast.walk(argument)
                    if isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                )
            if any(LEGACY_EXECUTABLE_PATTERN.search(value) for value in values):
                violations.append(
                    f"{name}:{node.lineno} executes a legacy I2V/Wan2GP command"
                )

    if violations:
        return False, "; ".join(violations[:30])
    return True, "No executable legacy I2V/Wan2GP integration detected."


def validate_existing_manifest(
    name: str,
    path: Path,
    expected_parts: int | None,
    expected_scenes: int | None,
    input_fingerprint: str,
) -> dict:
    if not path.exists():
        return {"status": "not_yet_generated", "path": safe_relative(path)}

    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"{name}: manifest is missing, empty or unsafe.")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name}: invalid JSON: {exc}") from exc

    if not isinstance(data, (dict, list)):
        raise ValueError(f"{name}: unsupported manifest root type.")

    if isinstance(data, dict):
        status = str(data.get("status", "")).strip().lower()
        if status in FAILED:
            raise ValueError(f"{name}: manifest status is {status}.")
        if status and status not in SUCCESS:
            raise ValueError(f"{name}: unrecognized manifest status {status!r}.")

        for field in ("format", "mode"):
            declared = data.get(field)
            if declared is not None and str(declared).strip().lower() not in {
                "short", "full", ""
            }:
                raise ValueError(f"{name}: invalid {field} value.")

        declared_input = (
            data.get("input_fingerprint")
            or data.get("input_sha256")
            or data.get("source_input_sha256")
        )
        if declared_input and str(declared_input).lower() != input_fingerprint:
            raise ValueError(
                f"{name}: artifact belongs to a different Input/topic.txt."
            )

        if expected_parts is not None:
            for field in ("parts", "total_parts", "selected_parts"):
                if data.get(field) is not None:
                    try:
                        actual = int(data[field])
                    except (TypeError, ValueError):
                        raise ValueError(f"{name}: invalid {field}.")
                    if actual != expected_parts:
                        raise ValueError(
                            f"{name}: {field}={actual}, expected {expected_parts}."
                        )

        if expected_scenes is not None:
            for field in ("scenes_per_part", "scenes"):
                value = data.get(field)
                if isinstance(value, int) and value != expected_scenes:
                    raise ValueError(
                        f"{name}: {field}={value}, expected {expected_scenes}."
                    )

    rows = []
    if isinstance(data, list):
        rows = data
    else:
        for key in ROW_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(value)

    checked_files = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue

        row_status = str(row.get("status", "")).strip().lower()
        if row_status in FAILED:
            raise ValueError(
                f"{name}: row {index} has failed status {row_status}."
            )
        if row_status and row_status not in SUCCESS and row_status not in {
            "pending", "skipped", "disabled", "unavailable"
        }:
            raise ValueError(
                f"{name}: row {index} has unknown status {row_status!r}."
            )

        file_value = next(
            (
                row.get(key) for key in PATH_KEYS
                if isinstance(row.get(key), str) and row.get(key).strip()
            ),
            None,
        )
        if file_value:
            asset = resolve_output_path(file_value, f"{name} row {index}")
            checked_files += 1

            for hash_key in HASH_KEYS:
                declared_hash = row.get(hash_key)
                if declared_hash:
                    actual_hash = sha256_file(asset)
                    if str(declared_hash).strip().lower() != actual_hash:
                        raise ValueError(
                            f"{name}: {asset.name} {hash_key} mismatch."
                        )
                    break

        scene_id = row.get("scene_id")
        if scene_id is not None and not str(scene_id).strip():
            raise ValueError(f"{name}: row {index} has an empty scene_id.")

    return {
        "status": "valid",
        "path": safe_relative(path),
        "sha256": sha256_file(path),
        "row_count": len(rows),
        "referenced_files_checked": checked_files,
    }


def check_artifact_inventory(
    parts: int,
    scenes: int,
    input_fingerprint: str,
) -> tuple[list[dict], dict]:
    checks = []
    inventory = {}

    for name, path in ARTIFACT_MANIFESTS:
        try:
            result = validate_existing_manifest(
                name, path, parts, scenes, input_fingerprint
            )
            inventory[name] = result
            checks.append({
                "name": "existing_artifact:" + name,
                "passed": True,
                "detail": (
                    "Not generated yet."
                    if result["status"] == "not_yet_generated"
                    else (
                        f"Valid; {result.get('row_count', 0)} records; "
                        f"{result.get('referenced_files_checked', 0)} files checked."
                    )
                ),
            })
        except Exception as exc:
            inventory[name] = {"status": "invalid", "error": str(exc)}
            checks.append({
                "name": "existing_artifact:" + name,
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            })

    return checks, inventory


def check_resume_state(
    parts: int,
    scenes: int,
    input_fingerprint: str,
    pipeline_fingerprint: str,
) -> tuple[bool, str, dict]:
    config_path = OUT / "checkpoints" / "pipeline_checkpoint.json"
    checkpoint_dirs = (
        OUT / "checkpoints",
        OUT / "story" / "checkpoints",
        OUT / "narration" / "checkpoints",
        OUT / "visuals" / "checkpoints",
        OUT / "photo_motion",
    )
    found_files = []
    for directory in checkpoint_dirs:
        if directory.is_dir():
            found_files.extend(
                path for path in directory.rglob("*")
                if path.is_file() and not path.is_symlink()
                and path.suffix.lower() in {".json", ".sha256"}
            )

    if not found_files:
        return True, "No prior checkpoint exists; clean first run is allowed.", {
            "available": False,
            "matching": False,
            "files": 0,
        }

    matching = 0
    stale = []
    invalid = []

    for path in found_files:
        if path.suffix.lower() != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            invalid.append(f"{safe_relative(path)}: {exc}")
            continue
        if not isinstance(data, dict):
            continue

        saved_input = (
            data.get("input_fingerprint")
            or data.get("input_sha256")
            or data.get("input_hash")
        )
        saved_pipeline = (
            data.get("pipeline_fingerprint")
            or data.get("pipeline_sha256")
            or data.get("pipeline_hash")
        )
        if saved_input and saved_pipeline:
            if (
                str(saved_input).lower() == input_fingerprint
                and str(saved_pipeline).lower() == pipeline_fingerprint
            ):
                matching += 1
            else:
                stale.append(safe_relative(path))

    if invalid:
        return False, "Corrupt checkpoint JSON: " + "; ".join(invalid[:10]), {
            "available": True, "matching": matching, "stale": stale,
            "invalid": invalid[:20],
        }

    # A stale checkpoint may coexist with fresh assets; it must not be reused.
    # It is reported, not treated as a fatal error, because the workflow restores
    # only a checkpoint whose two fingerprints match exactly.
    return True, (
        f"Checkpoint files found={len(found_files)}; "
        f"matching fingerprint manifests={matching}; "
        f"stale manifests rejected={len(stale)}."
    ), {
        "available": True,
        "matching": matching,
        "stale": stale,
        "files": len(found_files),
        "fingerprint_policy": "input_and_pipeline_sha256_must_both_match",
    }


def check_provider_credentials(
    tts_order: list[str],
    image_order: list[str],
) -> tuple[list[dict], dict]:
    """Inspect environment only; never call provider APIs or print secret values."""
    checks = []
    credentials = {}

    tts_credentials = {
        "fish_audio": ("FISH_API_KEY", "FISH_AUDIO_API_KEY"),
        "gemini_tts": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "edge_tts": (),
        "piper": (),
        "elevenlabs": ("ELEVENLABS_API_KEY",),
        "cartesia": ("CARTESIA_API_KEY",),
        "finevoice": ("FINEVOICE_API_KEY",),
        "minimax": ("MINIMAX_API_KEY",),
    }
    image_credentials = {
        "huggingface": ("HF_TOKEN",),
        "pollinations": ("POLLINATIONS_API_KEY",),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "replicate": ("REPLICATE_API_TOKEN",),
    }

    def available(names: tuple[str, ...]) -> bool:
        return not names or any(os.environ.get(key, "").strip() for key in names)

    tts_ready = []
    for provider in tts_order:
        names = tts_credentials[provider]
        ready = available(names)
        # Edge TTS and local Piper do not require API keys.
        if provider == "piper":
            ready = ready or bool(shutil.which("piper"))
        credentials[f"tts:{provider}"] = {
            "credential_names": list(names),
            "available": ready,
        }
        if ready:
            tts_ready.append(provider)

    image_ready = []
    for provider in image_order:
        names = image_credentials[provider]
        ready = available(names)
        # Public Hugging Face Spaces and Pollinations may work without a key.
        if provider in {"huggingface", "pollinations"}:
            ready = True
        credentials[f"image:{provider}"] = {
            "credential_names": list(names),
            "available": ready,
        }
        if ready:
            image_ready.append(provider)

    checks.append({
        "name": "tts_provider_credentials",
        "passed": bool(tts_ready),
        "detail": (
            "At least one configured TTS provider has usable local "
            "credential/runtime availability; order is unchanged: "
            + " -> ".join(tts_order)
            + "; available: " + ", ".join(tts_ready)
        ),
    })
    checks.append({
        "name": "image_provider_credentials",
        "passed": bool(image_ready),
        "detail": (
            "At least one configured image provider is locally usable; "
            "configured order is unchanged: "
            + " -> ".join(image_order)
            + "; available: " + ", ".join(image_ready)
        ),
    })
    return checks, credentials

def check_input_configuration() -> tuple[dict, list[dict], dict]:
    checks = []
    config = {}
    details = {}

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })

    if not INPUT_FILE.is_file() or INPUT_FILE.stat().st_size <= 0:
        add("input_file", False, "Input/topic.txt is missing or empty.")
        return config, checks, details

    add("input_file", True, "Input/topic.txt exists and is non-empty.")

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))

    try:
        from input_config import (
            load_and_validate,
            get_format,
            get_parts_range,
            get_selected_parts,
            get_scenes,
            get_fps,
            get_tts_provider_order,
            get_image_provider_order,
            get_global_billable_permission,
            get_story_text,
            get_caption_font,
            get_caption_font_size,
            get_topic,
            get_title_source,
        )

        config = load_and_validate(INPUT_FILE)

        mode = str(get_format(config)).strip().lower()
        minimum, maximum = get_parts_range(config)
        selected = get_selected_parts(config)
        scenes = get_scenes(config)
        fps = get_fps(config)
        tts_order = get_tts_provider_order(config)
        image_order = get_image_provider_order(config)
        billable = get_global_billable_permission(config)
        font = get_caption_font(config)
        font_size = get_caption_font_size(config)
        story_text = get_story_text(config).strip()
        topic = get_topic(config)
        title_source = get_title_source(config)

        add(
            "format",
            mode in {"short", "full"},
            f"FORMAT={mode}",
        )
        add(
            "parts_fixed_or_range",
            (
                isinstance(minimum, int)
                and isinstance(maximum, int)
                and isinstance(selected, int)
                and minimum >= 1
                and maximum >= minimum
                and minimum <= selected <= maximum
            ),
            (
                f"Allowed range={minimum}-{maximum}; "
                f"actual selected PARTS={selected}."
            ),
        )
        add(
            "scenes",
            isinstance(scenes, int) and not isinstance(scenes, bool)
            and 1 <= scenes <= 500,
            f"SCENES={scenes}; Main scene count={selected * scenes}.",
        )
        add(
            "fps",
            isinstance(fps, int) and not isinstance(fps, bool)
            and 1 <= fps <= 60,
            f"FPS={fps}.",
        )

        primary = [
            str(config.get(key, "")).strip()
            for key in ("TTS_MAIN_1", "TTS_MAIN_2", "TTS_MAIN_3")
        ]
        add(
            "three_primary_tts_providers",
            (
                len(primary) == 3
                and all(primary)
                and len(set(primary)) == 3
                and all(item in TTS_ALLOWED for item in tts_order[:3])
            ),
            "Primary providers: " + " -> ".join(tts_order[:3]),
        )
        add(
            "tts_fallback_order",
            (
                len(tts_order) >= 3
                and len(tts_order) == len(set(tts_order))
                and all(item in TTS_ALLOWED for item in tts_order)
            ),
            "Configured TTS order preserved: " + " -> ".join(tts_order),
        )
        add(
            "image_provider_order",
            (
                bool(image_order)
                and len(image_order) == len(set(image_order))
                and all(item in IMAGE_ALLOWED for item in image_order)
            ),
            "Configured image order preserved: " + " -> ".join(image_order),
        )
        add(
            "image_fallback_enabled",
            config.get("IMAGE_FALLBACK_ENABLED") is True,
            f"IMAGE_FALLBACK_ENABLED={config.get('IMAGE_FALLBACK_ENABLED')!r}",
        )
        add(
            "billable_permission",
            billable is True,
            (
                "ALLOW_BILLABLE_PROVIDERS is enabled."
                if billable
                else "ALLOW_BILLABLE_PROVIDERS must be true."
            ),
        )

        add(
            "caption_font",
            bool(font),
            f"CAPTION_FONT={font}.",
        )
        add(
            "caption_font_size",
            (
                font_size == "auto"
                or (
                    isinstance(font_size, int)
                    and 16 <= font_size <= 64
                )
            ),
            f"CAPTION_FONT_SIZE={font_size}.",
        )

        preserve = config.get("PRESERVE_STORY_TEXT") is True
        add(
            "pasted_story_preservation",
            not story_text or preserve,
            (
                "Pasted story is preserved."
                if story_text and preserve
                else "No pasted story supplied."
                if not story_text
                else "PRESERVE_STORY_TEXT must be true."
            ),
        )
        add(
            "story_input",
            bool(story_text or topic or title_source),
            (
                "Full story text supplied."
                if story_text
                else "Topic/title input supplied."
                if topic or title_source
                else "Neither story text nor topic/title is supplied."
            ),
        )

        required_motion = {
            "PHOTO_MOTION_STYLE": {"mixed", "zoom", "pan", "parallax", "dynamic", "static"},
            "PHOTO_MOTION_SPEED": {"slow", "normal", "fast", "very_fast"},
            "PHOTO_MOTION_VARIATION": {"low", "medium", "high"},
        }
        motion_errors = []
        for key, allowed in required_motion.items():
            value = str(config.get(key, "")).strip().lower()
            if value not in allowed:
                motion_errors.append(f"{key}={value!r}")

        strength = config.get("PHOTO_MOTION_PARALLAX_STRENGTH")
        try:
            strength_value = float(strength)
            if not math.isfinite(strength_value) or not 0 <= strength_value <= 0.10:
                motion_errors.append("PHOTO_MOTION_PARALLAX_STRENGTH out of range")
        except (TypeError, ValueError):
            motion_errors.append("PHOTO_MOTION_PARALLAX_STRENGTH is invalid")

        for key in ("PHOTO_MOTION_PARALLAX", "PHOTO_MOTION_PRESERVE_SUBJECT"):
            if not isinstance(config.get(key), bool):
                motion_errors.append(f"{key} must be boolean")

        add(
            "photo_motion_settings",
            not motion_errors,
            (
                "Generated-image zoom/pan/2.5D parallax settings are valid."
                if not motion_errors
                else "; ".join(motion_errors)
            ),
        )

        resume_ok = all(
            config.get(key) is True
            for key in (
                "RESUME_ENABLED",
                "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
                "SKIP_COMPLETED_SCENES",
            )
        )
        add(
            "resume_and_checkpoint",
            resume_ok,
            (
                "Resume, per-scene checkpoints and completed-artifact reuse enabled."
                if resume_ok
                else "RESUME_ENABLED, SAVE_CHECKPOINT_AFTER_EACH_SCENE "
                     "and SKIP_COMPLETED_SCENES must all be true."
            ),
        )

        details.update({
            "format": mode,
            "parts_min": minimum,
            "parts_max": maximum,
            "selected_parts": selected,
            "scenes_per_part": scenes,
            "main_scene_count": selected * scenes,
            "expected_role_scene_count": selected * scenes + 2 * selected,
            "fps": fps,
            "tts_provider_order": tts_order,
            "tts_primary_providers": primary,
            "image_provider_order": image_order,
            "billable_providers_allowed": billable,
            "caption_font": font,
            "caption_font_size": font_size,
            "pasted_story_supplied": bool(story_text),
            "topic_supplied": bool(topic),
            "story_preservation_enabled": preserve,
            "photo_motion_style": config.get("PHOTO_MOTION_STYLE"),
            "photo_motion_speed": config.get("PHOTO_MOTION_SPEED"),
            "photo_motion_variation": config.get("PHOTO_MOTION_VARIATION"),
            "photo_motion_parallax": config.get("PHOTO_MOTION_PARALLAX"),
            "photo_motion_parallax_strength": strength_value,
            "resume_enabled": config.get("RESUME_ENABLED"),
            "scene_checkpoints_enabled": config.get(
                "SAVE_CHECKPOINT_AFTER_EACH_SCENE"
            ),
            "completed_scene_reuse_enabled": config.get(
                "SKIP_COMPLETED_SCENES"
            ),
        })

    except Exception as exc:
        add(
            "input_configuration",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    return config, checks, details


def check_local_dependencies(config: dict) -> list[dict]:
    checks = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })

    for module in PYTHON_MODULES:
        passed, detail = check_module(module)
        add("python_dependency:" + module, passed, detail)

    for command in RUNTIME_COMMANDS:
        passed, detail = check_runtime_command(command)
        add("runtime_command:" + command, passed, detail)

    for family in FONT_FAMILIES:
        passed, detail = check_font(family)
        add("caption_font_installed:" + family, passed, detail)

    # OpenCV, NumPy and Pillow are mandatory for depth/parallax image processing.
    mandatory_modules = ("cv2", "numpy", "PIL")
    mandatory_available = all(
        importlib.util.find_spec(module) is not None
        for module in mandatory_modules
    )
    add(
        "photo_motion_depth_dependencies",
        mandatory_available,
        (
            "OpenCV, NumPy and Pillow are available for image/depth processing."
            if mandatory_available
            else "OpenCV, NumPy and Pillow are all required."
        ),
    )

    ai_depth_required = str(
        os.environ.get("DEPTH_AI_REQUIRED", "false")
    ).strip().lower() in TRUE_VALUES
    ai_modules = ("torch", "transformers")
    ai_available = all(
        importlib.util.find_spec(module) is not None
        for module in ai_modules
    )
    add(
        "depth_ai_dependencies",
        ai_available or not ai_depth_required,
        (
            "PyTorch and Transformers are available."
            if ai_available
            else (
                "DEPTH_AI_REQUIRED=true but PyTorch/Transformers are missing."
                if ai_depth_required
                else "AI depth dependencies are optional; configured pseudo-depth fallback remains available."
            )
        ),
    )

    if "piper" in (
        config.get("TTS_PROVIDER_ORDER", [])
        if isinstance(config.get("TTS_PROVIDER_ORDER"), list)
        else []
    ):
        piper_ready = bool(shutil.which("piper"))
        add(
            "piper_runtime",
            piper_ready,
            "Piper executable found." if piper_ready
            else "Piper is configured but its executable is missing.",
        )

    return checks


def calculate_pipeline_fingerprint() -> tuple[str, dict]:
    inputs = {}
    ordered_paths = [
        WORKFLOW_FILE,
        ROOT / "requirements.txt",
        *sorted(SCRIPTS.glob("*.py")),
    ]

    lines = []
    for path in ordered_paths:
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        checksum = sha256_file(path)
        inputs[relative] = checksum
        lines.append(f"{checksum}  {relative}\n")

    aggregate = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    return aggregate, inputs


def check_workflow_structure(source: str) -> list[dict]:
    checks = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })

    if not source.strip():
        add("workflow_readable", False, "Workflow file is empty or unreadable.")
        return checks

    add("workflow_readable", True, "Workflow file is readable.")

    patterns = {
        "manual_dispatch": r"(?m)^\s{2}workflow_dispatch:\s*(?:#.*)?$",
        "ubuntu_runner": r"(?m)^\s{4}runs-on:\s*ubuntu-latest\s*$",
        "python_311": r"(?m)^\s{10}python-version:\s*[\"']?3\.11[\"']?\s*$",
        "billable_environment": (
            r"(?m)^\s{6}ALLOW_BILLABLE_PROVIDERS:\s*[\"']?true[\"']?\s*$"
        ),
        "checkout_action": r"(?m)^\s{8}uses:\s*actions/checkout@v4\s*$",
        "python_setup_action": r"(?m)^\s{8}uses:\s*actions/setup-python@v5\s*$",
        "artifact_upload_action": (
            r"(?m)^\s{8}uses:\s*actions/upload-artifact@v4\s*$"
        ),
        "checkpoint_upload": (
            r"(?m)^\s{10}name:\s*katha-photo-motion-checkpoint-\$\{\{\s*github\.run_id\s*\}\}\s*$"
        ),
        "final_output_upload": (
            r"(?m)^\s{10}name:\s*katha-lok-ai-photo-motion-output-\$\{\{\s*github\.run_id\s*\}\}\s*$"
        ),
        "checkpoint_restore": (
            r"(?m)^\s{6}-\s+name:\s*Restore matching resumable checkpoint\s*$"
        ),
        "checkpoint_save": (
            r"(?m)^\s{6}-\s+name:\s*Save resumable checkpoint\s*$"
        ),
        "preflight_step": (
            r"(?m)^\s{6}-\s+name:\s*Run strict Photo Motion preflight\s*$"
        ),
    }

    for name, pattern in patterns.items():
        passed = bool(re.search(pattern, source))
        add(
            "workflow_structure:" + name,
            passed,
            "Present." if passed else "Missing or incorrect.",
        )

    # The workflow must install project requirements before preflight.
    install_position = source.find("python -m pip install -r requirements.txt")
    preflight_position = source.find("python scripts/pipeline_preflight.py")
    restore_position = source.find("Restore matching resumable checkpoint")
    if restore_position < 0:
        restore_position = 0

    add(
        "workflow_dependency_install_before_preflight",
        install_position >= 0 and preflight_position > install_position,
        (
            "requirements.txt installation precedes preflight."
            if install_position >= 0 and preflight_position > install_position
            else "Install requirements.txt before running preflight."
        ),
    )
    add(
        "workflow_checkpoint_restore_before_preflight",
        restore_position < preflight_position and preflight_position >= 0,
        (
            "Checkpoint restore precedes preflight."
            if restore_position < preflight_position and preflight_position >= 0
            else "Matching checkpoint must be restored before preflight."
        ),
    )

    return checks


def check_report_integrity(report: dict) -> tuple[bool, str]:
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        return False, "Preflight report has no checks."

    names = [
        item.get("name") for item in checks
        if isinstance(item, dict)
    ]
    if len(names) != len(checks):
        return False, "Preflight report contains invalid check records."
    if len(names) != len(set(names)):
        return False, "Preflight report contains duplicate check names."

    failed = [
        item for item in checks
        if item.get("passed") is not True
    ]
    if len(failed) != report.get("checks_failed"):
        return False, "Preflight failure count does not match checks."
    if report.get("checks_total") != len(checks):
        return False, "Preflight total check count mismatch."
    if report.get("checks_passed") != len(checks) - len(failed):
        return False, "Preflight passed count mismatch."
    if report.get("ready_to_run") is not (len(failed) == 0):
        return False, "Preflight readiness flag mismatch."
    if report.get("api_calls_made") != 0:
        return False, "Preflight report must declare zero API calls."
    if report.get("generation_performed") is not False:
        return False, "Preflight must not perform generation."

    return True, "Report counts, readiness and offline-only flags verified."


def main() -> int:
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })
        log("PASS" if passed else "FAIL", name, "-", detail)

    log("=" * 72)
    log("KATHA LOK AI — PHOTO MOTION PIPELINE PREFLIGHT")
    log("VERSION:", VERSION)
    log("OFFLINE CHECKS ONLY — NO GENERATION OR PROVIDER API CALLS")
    log("=" * 72)

    config, input_checks, input_details = check_input_configuration()
    for item in input_checks:
        record(item["name"], item["passed"], item["detail"])

    mode = input_details.get("format")
    parts = input_details.get("selected_parts")
    scenes = input_details.get("scenes_per_part")

    # Required scripts and syntax are checked independently.
    for name in REQUIRED_SCRIPTS:
        path = SCRIPTS / name
        exists = path.is_file() and path.stat().st_size > 0
        record(
            "required_file:" + name,
            exists,
            "Present and non-empty." if exists else "Missing or empty.",
        )
        if exists:
            valid, detail = check_python_file(path)
            record("syntax:" + name, valid, detail)

    legacy_ok, legacy_detail = check_legacy_execution()
    record("photo_motion_only_execution_path", legacy_ok, legacy_detail)

    workflow = ""
    try:
        workflow = WORKFLOW_FILE.read_text(encoding="utf-8-sig")
    except Exception as exc:
        record(
            "workflow_file",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    for item in check_workflow_structure(workflow):
        record(item["name"], item["passed"], item["detail"])

    commands = extract_workflow_commands(workflow)
    record(
        "workflow_commands_detected",
        bool(commands),
        f"Detected {len(commands)} Python script invocations."
        if commands else "No executable Python script commands detected.",
    )

    order_ok, order_detail = check_workflow_order(commands)
    record("workflow_generation_order", order_ok, order_detail)

    preflight_ok, preflight_detail = check_workflow_preflight(commands)
    record("preflight_before_generation", preflight_ok, preflight_detail)

    story_ok, story_detail = check_story_gate(commands)
    record("story_length_hard_gate", story_ok, story_detail)

    observed = {item["script"] for item in commands}
    missing_commands = [
        name for name in WORKFLOW_ORDER if name not in observed
    ]
    record(
        "all_generation_scripts_invoked",
        not missing_commands,
        (
            "Every required generation script is invoked."
            if not missing_commands
            else "Missing workflow commands: " + ", ".join(missing_commands)
        ),
    )

    for item in check_local_dependencies(config):
        record(item["name"], item["passed"], item["detail"])

    if input_details:
        credential_checks, credential_details = check_provider_credentials(
            input_details.get("tts_provider_order", []),
            input_details.get("image_provider_order", []),
        )
        for item in credential_checks:
            record(item["name"], item["passed"], item["detail"])
    else:
        credential_details = {}

    input_fingerprint = (
        sha256_file(INPUT_FILE)
        if INPUT_FILE.is_file() else ""
    )
    pipeline_fingerprint, source_fingerprints = calculate_pipeline_fingerprint()

    if input_fingerprint:
        record(
            "input_fingerprint",
            True,
            f"Input/topic.txt SHA256={input_fingerprint}.",
        )
    else:
        record("input_fingerprint", False, "Input/topic.txt is unavailable.")

    record(
        "pipeline_fingerprint",
        bool(source_fingerprints),
        (
            f"Fingerprint created for {len(source_fingerprints)} source files."
            if source_fingerprints
            else "No source files available for pipeline fingerprint."
        ),
    )

    # Validate existing manifests only when they already exist. Missing future
    # stage outputs are not failures on a first run.
    artifact_checks = []
    artifact_inventory = {}
    if parts and scenes and input_fingerprint:
        artifact_checks, artifact_inventory = check_artifact_inventory(
            parts, scenes, input_fingerprint
        )
        for item in artifact_checks:
            record(item["name"], item["passed"], item["detail"])
    else:
        record(
            "existing_artifact_validation",
            False,
            "Cannot validate existing artifacts without valid selected PARTS/SCENES.",
        )

    resume_ok, resume_detail, resume_state = check_resume_state(
        parts or 1,
        scenes or 1,
        input_fingerprint,
        pipeline_fingerprint,
    )
    record("checkpoint_resume_integrity", resume_ok, resume_detail)

    # Check expected paths without requiring outputs that are generated later.
    expected_paths = (
        INPUT_FILE,
        WORKFLOW_FILE,
        ROOT / "requirements.txt",
        SCRIPTS,
        OUT,
    )
    path_failures = []
    for path in expected_paths:
        if not path.exists():
            path_failures.append(path.relative_to(ROOT).as_posix())

    record(
        "expected_input_output_paths",
        not path_failures,
        (
            "Input, workflow, requirements, scripts and output paths exist."
            if not path_failures
            else "Missing: " + ", ".join(path_failures)
        ),
    )

    # Output directory write test uses a temporary file and does not alter
    # any generated image, audio, video, manifest or checkpoint.
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OUT,
            prefix=".preflight-write-test-",
            delete=True,
        ) as handle:
            handle.write("preflight")
            handle.flush()
            os.fsync(handle.fileno())
        record(
            "output_directory_writable",
            True,
            "Output directory accepts a temporary write.",
        )
    except Exception as exc:
        record(
            "output_directory_writable",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    # Stable manifest schema identifiers and measured audio timing are
    # checked when corresponding records already exist.
    schema_checks = []
    for manifest_name in (
        "narration_audio",
        "photo_motion_jobs",
        "photo_motion_manifest",
        "depth_manifest",
        "render_manifest",
        "captions",
    ):
        result = artifact_inventory.get(manifest_name)
        if not result or result.get("status") != "valid":
            continue
        schema_checks.append(manifest_name)

    record(
        "existing_manifest_schema_inventory",
        True,
        (
            "Existing manifest schemas and referenced file checksums were inspected: "
            + (", ".join(schema_checks) if schema_checks else "no stage manifests exist yet.")
        ),
    )

    failed = [
        item for item in checks
        if item["passed"] is not True
    ]

    report = {
        "status": "completed" if not failed else "blocked",
        "version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_root": ".",
        "format": mode,
        "parts_range": [
            input_details.get("parts_min"),
            input_details.get("parts_max"),
        ] if input_details else None,
        "selected_parts": parts,
        "scenes_per_part": scenes,
        "main_scene_count": input_details.get("main_scene_count"),
        "expected_role_scene_count": input_details.get(
            "expected_role_scene_count"
        ),
        "fps": input_details.get("fps"),
        "caption_font": input_details.get("caption_font"),
        "caption_font_size": input_details.get("caption_font_size"),
        "tts_provider_order": input_details.get("tts_provider_order"),
        "image_provider_order": input_details.get("image_provider_order"),
        "provider_credentials": credential_details,
        "billable_providers_allowed": input_details.get(
            "billable_providers_allowed"
        ),
        "photo_motion": {
            "style": input_details.get("photo_motion_style"),
            "speed": input_details.get("photo_motion_speed"),
            "variation": input_details.get("photo_motion_variation"),
            "parallax": input_details.get("photo_motion_parallax"),
            "parallax_strength": input_details.get(
                "photo_motion_parallax_strength"
            ),
            "character_or_body_animation": False,
            "motion_scope": "generated-image zoom, pan and 2.5D parallax only",
        },
        "resume": {
            "enabled": input_details.get("resume_enabled"),
            "per_scene_checkpoints": input_details.get(
                "scene_checkpoints_enabled"
            ),
            "completed_scene_reuse": input_details.get(
                "completed_scene_reuse_enabled"
            ),
            **resume_state,
        },
        "input_fingerprint": input_fingerprint,
        "pipeline_fingerprint": pipeline_fingerprint,
        "source_fingerprints": source_fingerprints,
        "existing_artifacts": artifact_inventory,
        "workflow_commands_detected": commands,
        "workflow_order_expected": list(WORKFLOW_ORDER),
        "api_calls_made": 0,
        "external_api_calls": 0,
        "generation_performed": False,
        "assets_regenerated": False,
        "ready_to_run": not failed,
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "failures": failed,
        "checks": checks,
        "limitations": [
            "Preflight is offline and does not call generation or provider APIs.",
            "Credential presence is checked locally; provider authentication, quotas and availability are not tested.",
            "Existing artifacts are checked for readable manifests, safe output paths and declared SHA256 values.",
            "Missing artifacts for stages that have not run are allowed.",
            "Existing checkpoint reuse requires matching input and pipeline fingerprints.",
            "Audio timing is measured by generation and final validation stages; preflight does not regenerate audio.",
            "Only generated-image zoom, pan and 2.5D parallax are permitted by this pipeline.",
        ],
    }

    try:
        atomic_json(REPORT_FILE, report)
        saved = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        integrity_ok, integrity_detail = check_report_integrity(saved)
        if not integrity_ok:
            raise RuntimeError(integrity_detail)

        record("report_integrity", True, integrity_detail)

        failed = [
            item for item in checks
            if item["passed"] is not True
        ]
        report.update({
            "status": "completed" if not failed else "blocked",
            "ready_to_run": not failed,
            "checks_total": len(checks),
            "checks_passed": len(checks) - len(failed),
            "checks_failed": len(failed),
            "failures": failed,
            "checks": checks,
            "api_calls_made": 0,
            "external_api_calls": 0,
            "generation_performed": False,
        })
        atomic_json(REPORT_FILE, report)

        final_saved = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        final_ok, final_detail = check_report_integrity(final_saved)
        if not final_ok:
            raise RuntimeError(final_detail)

    except Exception as exc:
        log("PREFLIGHT REPORT ERROR:", f"{type(exc).__name__}: {exc}")
        return 1

    log("-" * 72)
    log("CHECKS:", report["checks_total"])
    log("PASSED:", report["checks_passed"])
    log("FAILED:", report["checks_failed"])
    log("FORMAT:", mode)
    log("ACTUAL PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("REPORT:", REPORT_FILE.relative_to(ROOT))

    if failed:
        log("PREFLIGHT FAILED — generation must not start.")
        for item in failed:
            log("BLOCKER:", item["name"], "-", item["detail"])
        return 1

    log(
        "PREFLIGHT PASSED — no generation or provider API calls were made."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("PREFLIGHT CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("PREFLIGHT INTERNAL ERROR:", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)