
#!/usr/bin/env python3
"""Strict offline preflight for the Katha Lok AI photo-motion pipeline."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
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

VERSION = "photo-motion-preflight-v67-flow-music-and-complete-asset-audit"

REQUIRED_SCRIPTS = (
    "input_config.py",
    "pipeline_preflight.py",
    "check_gemini_guard.py",
    "gemini_api_guard.py",
    "gemini_http.py",
    "select_visual_model.py",
    "test_gemini.py",
    "story_length_policy.py",
    "select_gemini_model.py",
    "llm_provider_client.py",
    "select_llm_provider.py",
    "test_llm_providers.py",
    "generate_story.py",
    "validate_story_length.py",
    "verify_story.py",
    "generate_story_plan.py",
    "generate_ai_story.py",
    "generate_character_bible.py",
    "generate_scenes.py",
    "generate_narration.py",
    "voice_profiles.py",
    "preview_voice.py",
    "music_provider_client.py",
    "prepare_captions.py",
    "generate_tts.py",
    "generate_flow_music.py",
    "generate_visuals.py",
    "free_image_generator.py",
    "prepare_visuals.py",
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
)

WORKFLOW_ORDER = (
    "select_gemini_model.py",
    "generate_story.py",
    "validate_story_length.py",
    "verify_story.py",
    "generate_story_plan.py",
    "generate_ai_story.py",
    "generate_character_bible.py",
    "generate_scenes.py",
    "generate_narration.py",
    "prepare_captions.py",
    "generate_tts.py",
    "generate_flow_music.py",
    "generate_visuals.py",
    "free_image_generator.py",
    "prepare_visuals.py",
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
    "requests", "PIL", "cv2", "numpy", "imageio",
    "imageio_ffmpeg", "gradio_client", "huggingface_hub",
    "edge_tts", "transformers", "safetensors", "accelerate",
    "librosa", "sentencepiece", "soundfile",
)

REQUIRED_DISTRIBUTIONS = (
    "requests", "Pillow", "numpy", "opencv-python-headless",
    "imageio", "imageio-ffmpeg", "edge-tts", "gradio_client",
    "huggingface_hub", "transformers", "safetensors", "accelerate",
    "librosa", "sentencepiece", "soundfile",
)

RUNTIME_COMMANDS = ("ffmpeg", "ffprobe", "fc-match", "zip", "unzip")

FONT_FAMILIES = (
    "Noto Sans Devanagari",
    "Mukta",
    "Hind",
    "Tiro Devanagari Hindi",
)

SUCCESS = {"completed", "complete", "success", "done", "reused", "valid"}
FAILED = {"failed", "error", "cancelled", "incomplete", "invalid"}
NONFINAL = {
    "pending",
    "skipped",
    "disabled",
    "unavailable",
    "ready_for_generation",
}

ROW_KEYS = (
    "jobs", "visual_jobs", "photo_motion_jobs", "scenes", "items",
    "images", "depth_maps", "audio_jobs", "records", "parts",
    "tracks", "cues", "captions", "thumbnails", "role_segments", "role_compositions",
)

PATH_KEYS = (
    "path", "file", "output", "output_path", "image", "image_path",
    "audio", "audio_path", "video", "video_path", "depth_path",
    "depth_map", "rendered_video", "source_path",
)

HASH_KEYS = (
    "sha256", "image_sha256", "audio_sha256", "video_sha256",
    "depth_sha256", "file_sha256", "source_sha256",
)

ARTIFACT_MANIFESTS = (
    ("story", OUT / "story" / "story.json"),
    ("story_ai", OUT / "story" / "ai_story.json"),
    ("story_length", OUT / "story" / "story_length_validation.json"),
    ("narration_jobs", OUT / "narration" / "narration.json"),
    ("narration_audio", OUT / "narration" / "audio_jobs.json"),
    ("flow_music", OUT / "music" / "flow_music_manifest.json"),
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

STEP_RE = re.compile(r"^\s{6}-\s+name:\s*(.*?)\s*$")
RUN_RE = re.compile(r"^\s{8}run:\s*(.*?)\s*$")
SCRIPT_RE = re.compile(
    r"(?<![\w./-])python(?:3(?:\.\d+)?)?"
    r"\s+(?:-u\s+)?scripts/([A-Za-z0-9_.-]+\.py)"
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
    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)

    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def safe_relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError(f"Path escapes repository: {path}")
    return resolved.relative_to(ROOT.resolve()).as_posix()


def resolve_output_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: missing artifact path.")

    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    resolved = candidate.resolve()

    if not resolved.is_relative_to(OUT.resolve()):
        raise ValueError(f"{label}: artifact is outside output/.")

    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label}: artifact missing or unsafe: {resolved}")

    if resolved.stat().st_size <= 0:
        raise ValueError(f"{label}: artifact is empty: {resolved}")

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
        available = importlib.util.find_spec(module) is not None
        return (
            available,
            "Importable." if available else "Module not installed.",
        )
    except (ImportError, ValueError, AttributeError) as exc:
        return False, f"Module check failed: {exc}"


def check_requirements_manifest() -> tuple[bool, str]:
    path = ROOT / "requirements.txt"
    if not path.is_file():
        return False, "requirements.txt is missing."
    declared = {}
    try:
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            match = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", line)
            if not match:
                return False, f"Invalid requirements entry: {line!r}"
            name = re.sub(r"[-_.]+", "-", match.group(1)).casefold()
            declared[name] = match.group(2).strip()
    except (OSError, UnicodeError) as exc:
        return False, f"Cannot read requirements.txt: {exc}"

    missing = []
    unpinned = []
    for distribution in REQUIRED_DISTRIBUTIONS:
        name = re.sub(r"[-_.]+", "-", distribution).casefold()
        specification = declared.get(name)
        if specification is None:
            missing.append(distribution)
        elif not specification.startswith("=="):
            unpinned.append(distribution)
    if missing or unpinned:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unpinned:
            details.append("not exactly pinned: " + ", ".join(unpinned))
        return False, "; ".join(details)
    return True, f"All {len(REQUIRED_DISTRIBUTIONS)} required distributions are exactly pinned."


def check_runtime_command(command: str) -> tuple[bool, str]:
    executable = shutil.which(command)
    return bool(executable), executable or f"{command} missing from PATH."


def check_font() -> tuple[bool, str]:
    manifest_paths = (
        OUT / "fonts" / "font_manifest.json",
        Path.home() / ".local" / "share" / "fonts" / "katha-caption" / "font_install_manifest.json",
    )
    manifest = None
    for manifest_path in manifest_paths:
        try:
            candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and candidate.get("status") == "completed":
                manifest = candidate
                break
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue

    if isinstance(manifest, dict):
        selected = manifest.get("selected_hindi_font")
        if not isinstance(selected, dict):
            return False, "Font manifest has no selected_hindi_font record."
        family = str(selected.get("family") or "").strip()
        path_value = str(selected.get("path") or "").strip()
        declared_hash = str(selected.get("sha256") or "").strip().lower()
        if family not in FONT_FAMILIES:
            return False, f"Selected font family is outside the verified fallback list: {family!r}."
        if not path_value:
            return False, "Selected font manifest path is missing."
        path = Path(path_value)
        if not path.is_file() or path.is_symlink():
            return False, f"Selected font file is missing or unsafe: {path}."
        if manifest.get("project_font_file") != path_value:
            return False, "Selected font path differs from the packaged font path."
        if not re.fullmatch(r"[a-f0-9]{64}", declared_hash):
            return False, "Selected font SHA-256 is missing or invalid."
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != declared_hash:
            return False, "Selected font file SHA-256 differs from its manifest."
        license_value = str(manifest.get("project_license_file") or "").strip()
        if not license_value:
            return False, "Selected font license path is missing."
        license_path = Path(license_value)
        if not license_path.is_file() or license_path.is_symlink():
            return False, "Selected font license file is missing or unsafe."
        license_text = license_path.read_text(encoding="utf-8")
        if (
            "SIL OPEN FONT LICENSE Version 1.1" not in license_text
            or not license_text.startswith("Copyright")
        ):
            return False, "Selected font copyright/OFL license text is invalid."
        scanner = shutil.which("fc-scan")
        if not scanner:
            return False, "fc-scan is not installed."
        scanned = subprocess.run(
            [scanner, "--format", "%{family}\n", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if scanned.returncode:
            return False, "fc-scan could not verify the selected font family."
        normalized = scanned.stdout.replace("\r", "\n").replace(",", "\n")
        actual = [item.strip().casefold() for item in normalized.splitlines() if item.strip()]
        expected = family.casefold()
        if not any(item == expected or item.startswith(expected + " ") for item in actual):
            return False, f"Font family mismatch: expected {family}, got {scanned.stdout!r}."
        fallback_note = "primary font" if family == FONT_FAMILIES[0] else f"verified fallback for {FONT_FAMILIES[0]}"
        return True, f"{family} verified at {path} ({fallback_note}; SHA-256 matched)."

    executable = shutil.which("fc-match")
    scanner = shutil.which("fc-scan")
    if not executable or not scanner:
        return False, "Font manifest is missing and fontconfig tools are unavailable."
    for family in FONT_FAMILIES:
        result = subprocess.run(
            [executable, "-f", "%{family}|%{file}", family],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode or "|" not in result.stdout:
            continue
        actual_family, raw_path = result.stdout.strip().split("|", 1)
        path = Path(raw_path)
        if not path.is_file():
            continue
        normalized = actual_family.replace("\r", "\n").replace(",", "\n")
        actual_names = [item.strip().casefold() for item in normalized.splitlines() if item.strip()]
        expected = family.casefold()
        if any(item == expected or item.startswith(expected + " ") for item in actual_names):
            return True, f"{family} verified by fontconfig at {path}; installer manifest is absent."
    return False, "No exact Devanagari font family is installed and no verified fallback manifest exists."



def extract_workflow_commands(source: str) -> list[dict]:
    commands = []
    current_step = None
    inside_run = False

    for line_number, raw_line in enumerate(
        source.splitlines(), 1
    ):
        step_match = STEP_RE.match(raw_line)

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

            if inline and inline not in {
                "|", ">", "|-", ">-"
            }:
                for match in SCRIPT_RE.finditer(
                    inline.split("#", 1)[0]
                ):
                    commands.append({
                        "script": match.group(1),
                        "line": line_number,
                        "step": current_step or "unnamed",
                    })

            continue

        if not inside_run:
            continue

        indentation = (
            len(raw_line) - len(raw_line.lstrip())
        )

        if raw_line.strip() and indentation <= 8:
            inside_run = False
            continue

        for match in SCRIPT_RE.finditer(
            raw_line.split("#", 1)[0]
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
                f"Detected order: {' -> '.join(observed)}"
            )

        matched.append(expected)
        cursor = found + 1

    return True, "Verified order: " + " -> ".join(matched)


def check_workflow_preflight(commands: list[dict]) -> tuple[bool, str]:
    preflight = [
        item for item in commands
        if item["script"] == "pipeline_preflight.py"
    ]
    generation = [
        item for item in commands
        if item["script"] in set(WORKFLOW_ORDER)
    ]

    if len(preflight) != 1:
        return False, (
            f"Expected exactly one preflight invocation; found {len(preflight)}."
        )

    if not generation:
        return False, "No generation commands detected."

    if preflight[0]["line"] >= min(item["line"] for item in generation):
        return False, "Preflight must run before generation."

    return True, "Exactly one preflight invocation precedes generation."


def check_story_gate(commands: list[dict]) -> tuple[bool, str]:
    positions = {}

    for item in commands:
        positions.setdefault(item["script"], item["line"])

    required = (
        "generate_story.py",
        "validate_story_length.py",
        "verify_story.py",
        "generate_story_plan.py",
    )

    missing = [name for name in required if name not in positions]
    if missing:
        return False, "Missing story gate commands: " + ", ".join(missing)

    ordered = [positions[name] for name in required]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        return False, (
            "Required order: generate_story.py -> "
            "validate_story_length.py -> verify_story.py -> "
            "generate_story_plan.py."
        )

    return True, "Story validation precedes final story-plan generation."


def check_prepare_visuals_order(commands: list[dict]) -> tuple[bool, str]:
    positions = {}

    for item in commands:
        positions.setdefault(item["script"], item["line"])

    required = (
        "generate_visuals.py",
        "free_image_generator.py",
        "prepare_visuals.py",
        "generate_depth_maps.py",
        "photo_motion.py",
    )

    missing = [name for name in required if name not in positions]
    if missing:
        return False, (
            "Missing visual preparation commands: " + ", ".join(missing)
        )

    ordered = [positions[name] for name in required]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        return False, (
            "Required order: generate_visuals.py -> free_image_generator.py "
            "-> prepare_visuals.py -> generate_depth_maps.py -> photo_motion.py."
        )

    return True, (
        "prepare_visuals.py runs after image generation and before depth maps "
        "and photo motion."
    )


def check_story_length_policy_integration() -> tuple[bool, str]:
    policy_path = SCRIPTS / "story_length_policy.py"
    story_path = SCRIPTS / "generate_story.py"

    if not policy_path.is_file() or not story_path.is_file():
        return False, "story_length_policy.py or generate_story.py is missing."

    try:
        tree = ast.parse(
            story_path.read_text(encoding="utf-8-sig"),
            filename=str(story_path),
        )
    except Exception as exc:
        return False, f"generate_story.py cannot be inspected: {exc}"

    references = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            references.extend(alias.name for alias in node.names)

        elif isinstance(node, ast.ImportFrom):
            references.append(node.module or "")

    integrated = any(
        name.rsplit(".", 1)[-1] == "story_length_policy"
        for name in references


    )

    if not integrated:
        return False, (
            "generate_story.py does not import story_length_policy.py."
        )

    return True, "generate_story.py imports the story length policy module."


def _manifest_rows(data: Any) -> list[dict]:
    rows = []

    if isinstance(data, list):
        rows.extend(item for item in data if isinstance(item, dict))

    elif isinstance(data, dict):
        for key in ROW_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(
                    item for item in value if isinstance(item, dict)
                )

    return rows


def _row_part_number(row: dict) -> int | None:
    for key in ("part", "part_number", "part_index"):
        value = row.get(key)
        if value is None:
            continue

        try:
            number = int(value)
        except (TypeError, ValueError):
            continue

        if number >= 1:
            return number

    return None


def _is_main_scene(row: dict) -> bool:
    role = str(
        row.get("role")
        or row.get("scene_type")
        or row.get("segment_type")
        or ""
    ).strip().lower()

    if role in {
        "hook", "part_hook", "suspense", "part_suspense",
        "resolution", "final_resolution",
    }:
        return False

    return True


def validate_actual_scene_counts(
    name: str,
    data: Any,
    minimum: int,
    maximum: int,
    expected_parts: int | None,
    mode: str,
) -> tuple[bool, str, dict]:
    rows = _manifest_rows(data)
    scene_rows = [
        row for row in rows
        if row.get("scene_id") is not None and _is_main_scene(row)
    ]

    if not scene_rows:
        return True, (
            f"{name}: no actual scene records yet; future-stage manifest "
            "is allowed before scene generation."
        ), {
            "available": False,
            "part_counts": {},
        }

    grouped: dict[int, set[str]] = {}

    for row in scene_rows:
        part = _row_part_number(row)

        if part is None:


            if mode == "full":
                part = 1
            else:
                return False, (
                    f"{name}: scene record has no valid Part number."
                ), {
                    "available": True,
                    "part_counts": {},
                }

        scene_id = str(row.get("scene_id", "")).strip()
        if not scene_id:
            return False, f"{name}: empty scene_id found.", {}

        grouped.setdefault(part, set()).add(scene_id)

    if mode == "full" and set(grouped) != {1}:
        return False, (
            f"{name}: FULL mode must contain scenes for exactly Part 1; "
            f"found Parts {sorted(grouped)}."
        ), {
            "available": True,
            "part_counts": {
                str(part): len(ids) for part, ids in grouped.items()
            },
        }

    if expected_parts is not None:
        expected = set(range(1, expected_parts + 1))
        actual = set(grouped)

        if actual != expected:
            return False, (
                f"{name}: actual Part IDs {sorted(actual)} do not match "
                f"expected Parts {sorted(expected)}."
            ), {
                "available": True,
                "part_counts": {
                    str(part): len(ids) for part, ids in grouped.items()
                },
            }

    counts = {
        str(part): len(ids)
        for part, ids in sorted(grouped.items())
    }

    invalid = {
        part: count
        for part, count in counts.items()
        if not minimum <= count <= maximum
    }

    if invalid:
        return False, (
            f"{name}: actual Scene counts outside configured range "
            f"{minimum}-{maximum}: {invalid}."
        ), {
            "available": True,
            "part_counts": counts,
        }

    return True, (
        f"{name}: actual Scene counts are within {minimum}-{maximum}: "
        f"{counts}."
    ), {
        "available": True,
        "part_counts": counts,
    }



def validate_existing_manifest(
    name: str,
    path: Path,
    expected_parts: int | None,
    scenes_min: int,
    scenes_max: int,
    input_fingerprint: str,
    expected_format: str,
) -> dict:
    if not path.exists():
        return {
            "status": "not_yet_generated",
            "path": safe_relative(path),
            "actual_scene_counts": {},
        }

    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size <= 0
    ):
        raise ValueError(
            f"{name}: manifest is missing, empty or unsafe."
        )

    try:
        data = json.loads(
            path.read_text(encoding="utf-8-sig")
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(
            f"{name}: invalid JSON: {exc}"
        ) from exc

    if not isinstance(data, (dict, list)):
        raise ValueError(
            f"{name}: unsupported manifest root type."
        )

    is_tts = (
        name == "narration_audio"
        and isinstance(data, dict)
    )

    status = ""
    raw_jobs = None
    raw_parts = None
    summary = {}
    total_jobs = None

    legacy_empty_tts_marker = False
    resumable_tts_manifest = False

    if isinstance(data, dict):
        status = str(
            data.get("status", "")
        ).strip().lower()

        raw_jobs = data.get("jobs")
        raw_parts = data.get("parts")

        if isinstance(data.get("summary"), dict):
            summary = data["summary"]

        total_jobs = summary.get("total_jobs")

        if is_tts and status == "failed":
            legacy_empty_tts_marker = (
                isinstance(raw_jobs, list)
                and len(raw_jobs) == 0
                and type(data.get("total")) is int
                and data["total"] > 0
                and type(data.get("completed")) is int
                and data["completed"] == 0
                and type(data.get("failed")) is int
                and data["failed"] == 0
                and isinstance(
                    data.get("requested_voice"), str
                )
                and bool(
                    data["requested_voice"].strip()
                )
                and isinstance(
                    data.get("configured_rate"), str
                )
                and bool(
                    data["configured_rate"].strip()
                )
                and not data.get("source_sha256")
                and not data.get("config_sha256")
                and not _manifest_rows(data)
            )

        valid_tts_hashes = (
            bool(
                str(
                    data.get("source_sha256", "")
                ).strip()
            )
            and bool(
                str(
                    data.get("config_sha256", "")
                ).strip()
            )
        )

        valid_tts_total = (
            type(total_jobs) is int
            and total_jobs > 0
        )

        resumable_tts_manifest = (
            is_tts
            and status in {"running", "failed"}
            and isinstance(raw_jobs, list)
            and valid_tts_hashes
            and valid_tts_total
            and len(raw_jobs) <= total_jobs
            and (
                status == "running"
                or (
                    data.get("retryable") is True
                    and isinstance(
                        data.get("failure"), str
                    )
                    and bool(data["failure"].strip())
                )
            )
        )

        retryable_narration_audio = (
            legacy_empty_tts_marker
            or resumable_tts_manifest
        )

        if status in FAILED and not retryable_narration_audio:
            raise ValueError(
                f"{name}: manifest status is {status}."
            )

        if (
            is_tts
            and status in {"running", "failed"}
            and not retryable_narration_audio
        ):
            raise ValueError(
                "narration_audio: failed/running manifest "
                "does not have a valid resumable identity."
            )

        known_status = SUCCESS | NONFINAL | FAILED

        if (
            status
            and status not in known_status
            and not (
                is_tts
                and resumable_tts_manifest
                and status == "running"
            )
        ):
            raise ValueError(
                f"{name}: unknown manifest status "
                f"{status!r}."
            )

        for field in ("format", "mode"):
            declared = data.get(field)

            if declared is None:
                continue

            normalized = str(declared).strip().lower()

            if (
                normalized
                and normalized != expected_format
            ):
                raise ValueError(
                    f"{name}: {field}={normalized}, "
                    f"expected {expected_format}."
                )

        declared_input = (
            data.get("input_fingerprint")
            or data.get("input_sha256")
            or data.get("source_input_sha256")
        )

        if (
            declared_input
            and str(declared_input).lower()
            != input_fingerprint
        ):
            raise ValueError(
                f"{name}: artifact belongs to "
                "a different input file."
            )

        for field in (
            "parts",
            "total_parts",
            "selected_parts",
        ):
            value = data.get(field)

            if value is None:
                continue

            # TTS v13 stores Part audio records in "parts".
            # This field is a list, not a Part-count integer.
            if field == "parts" and isinstance(value, list):
                if (
                    expected_parts is not None
                    and len(value) > expected_parts
                ):
                    raise ValueError(
                        f"{name}: too many Part records."
                    )

                if (
                    status in SUCCESS
                    and expected_parts is not None
                    and len(value) != expected_parts
                ):
                    raise ValueError(
                        f"{name}: Part record count "
                        f"{len(value)} does not match "
                        f"expected {expected_parts}."
                    )

                if (
                    expected_format == "full"
                    and (
                        len(value) > 1
                        or (
                            status in SUCCESS
                            and len(value) != 1
                        )
                    )
                ):
                    raise ValueError(
                        f"{name}: FULL mode requires "
                        "exactly one Part record."
                    )

                if (
                    status in SUCCESS
                    and not value
                ):
                    raise ValueError(
                        f"{name}: completed manifest "
                        "has no Part records."
                    )

                continue

            if isinstance(value, bool):
                raise ValueError(
                    f"{name}: invalid {field}."
                )

            try:
                actual = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{name}: invalid {field}."
                ) from exc

            if (
                expected_parts is not None
                and actual != expected_parts
            ):
                raise ValueError(
                    f"{name}: {field}={actual}, "
                    f"expected {expected_parts}."
                )

            if expected_format == "full" and actual != 1:
                raise ValueError(
                    f"{name}: FULL mode requires "
                    f"{field}=1; found {actual}."
                )

        if is_tts and status in SUCCESS:
            if not isinstance(raw_jobs, list) or not raw_jobs:
                raise ValueError(
                    "narration_audio: completed manifest "
                    "contains no completed jobs."
                )

            if not isinstance(raw_parts, list) or not raw_parts:
                raise ValueError(
                    "narration_audio: completed manifest "
                    "contains no Part audio records."
                )

            if (
                type(total_jobs) is not int
                or len(raw_jobs) != total_jobs
            ):
                raise ValueError(
                    "narration_audio: completed job count "
                    "does not match total_jobs."
                )

            completed_jobs = summary.get("completed_jobs")

            if (
                type(completed_jobs) is not int
                or completed_jobs != len(raw_jobs)
            ):
                raise ValueError(
                    "narration_audio: summary completed_jobs "
                    "does not match jobs."
                )

    else:
        retryable_narration_audio = False

    if not isinstance(data, dict):
        retryable_narration_audio = False

    # Validate the identity of every recorded TTS scene.
    if is_tts and isinstance(raw_jobs, list):
        seen_scene_ids = set()

        for index, row in enumerate(raw_jobs, 1):
            if not isinstance(row, dict):
                raise ValueError(
                    f"narration_audio: job {index} "
                    "is not an object."
                )

            scene_id = str(
                row.get("scene_id") or ""
            ).strip()

            if not scene_id:
                raise ValueError(
                    f"narration_audio: job {index} "
                    "has no scene_id."
                )

            if scene_id in seen_scene_ids:
                raise ValueError(
                    f"narration_audio: duplicate scene_id "
                    f"{scene_id!r}."
                )

            seen_scene_ids.add(scene_id)

            part_number = _row_part_number(row)

            if part_number is None:
                raise ValueError(
                    f"narration_audio: job {index} "
                    "has an invalid Part number."
                )

            if (
                expected_parts is not None
                and part_number > expected_parts
            ):
                raise ValueError(
                    f"narration_audio: job {index} "
                    "references an unexpected Part."
                )

            if (
                expected_format == "full"
                and part_number != 1
            ):
                raise ValueError(
                    "narration_audio: FULL mode "
                    "contains a non-Part-1 job."
                )

            role = str(
                row.get("role") or "main"
            ).strip().lower()

            if role not in {
                "main",
                "hook",
                "suspense",
                "resolution",
            }:
                raise ValueError(
                    f"narration_audio: unsupported "
                    f"job role {role!r}."
                )

            row_status = str(
                row.get("status", "")
            ).strip().lower()

            if (
                status in SUCCESS
                and row_status not in SUCCESS
            ):
                raise ValueError(
                    f"narration_audio: completed manifest "
                    f"contains unfinished job {scene_id}."
                )

            if (
                row_status
                and row_status not in SUCCESS | NONFINAL | FAILED
            ):
                raise ValueError(
                    f"narration_audio: unknown job status "
                    f"{row_status!r}."
                )

    # Validate Part audio records. During a retryable run,
    # missing Part merges are allowed because TTS recreates them.
    if is_tts and isinstance(raw_parts, list):
        seen_part_numbers = set()

        for index, row in enumerate(raw_parts, 1):
            if not isinstance(row, dict):
                raise ValueError(
                    f"narration_audio: Part record {index} "
                    "is not an object."
                )

            part_number = _row_part_number(row)

            if part_number is None:
                raise ValueError(
                    f"narration_audio: Part record {index} "
                    "has an invalid Part number."
                )

            if part_number in seen_part_numbers:
                raise ValueError(
                    f"narration_audio: duplicate Part "
                    f"record {part_number}."
                )

            seen_part_numbers.add(part_number)

            if (
                expected_parts is not None
                and part_number > expected_parts
            ):
                raise ValueError(
                    "narration_audio: Part audio record "
                    "exceeds expected Part count."
                )

            if (
                expected_format == "full"
                and part_number != 1
            ):
                raise ValueError(
                    "narration_audio: FULL mode contains "
                    "a non-Part-1 audio record."
                )

            if status in SUCCESS:
                has_path = any(
                    isinstance(row.get(key), str)
                    and row[key].strip()
                    for key in PATH_KEYS
                )

                has_hash = any(
                    isinstance(row.get(key), str)
                    and row[key].strip()
                    for key in HASH_KEYS
                )

                if not has_path or not has_hash:
                    raise ValueError(
                        f"narration_audio: completed "
                        f"Part {part_number} has no "
                        "verified audio path/hash."
                    )

        if status in SUCCESS:
            actual_part_ids = seen_part_numbers
            expected_part_ids = set(
                range(1, len(raw_parts) + 1)
            )

            if actual_part_ids != expected_part_ids:
                raise ValueError(
                    "narration_audio: Part audio IDs "
                    "are not continuous from Part 1."
                )

    if (
        is_tts
        and status in SUCCESS
        and expected_parts is not None
        and len(raw_parts) != expected_parts
    ):
        raise ValueError(
            "narration_audio: completed Part audio "
            "count does not match expected Parts."
        )

    rows = _manifest_rows(data)

    checked_files = 0
    retryable_failed_rows = 0
    retryable_unavailable_files = 0

    for index, row in enumerate(rows, 1):
        row_status = str(
            row.get("status", "")
        ).strip().lower()

        if row_status in FAILED:
            if retryable_narration_audio:
                retryable_failed_rows += 1
                continue

            raise ValueError(
                f"{name}: row {index} has failed status "
                f"{row_status}."
            )

        if (
            row_status
            and row_status not in SUCCESS | NONFINAL
        ):
            raise ValueError(
                f"{name}: row {index} has unknown status "
                f"{row_status!r}."
            )

        file_value = next(
            (
                row.get(key)
                for key in PATH_KEYS
                if isinstance(row.get(key), str)
                and row.get(key).strip()
            ),
            None,
        )

        if not file_value:
            if (
                retryable_narration_audio
                and row_status in SUCCESS
            ):
                retryable_unavailable_files += 1
                continue

            if (
                name == "narration_audio"
                and status in SUCCESS
            ):
                raise ValueError(
                    f"{name}: completed audio row {index} "
                    "has no audio path."
                )

            continue

        try:
            asset = resolve_output_path(
                file_value,
                f"{name} row {index}",
            )
        except Exception:
            if retryable_narration_audio:
                retryable_unavailable_files += 1
                continue
            raise

        checked_files += 1
        hash_mismatch = False

        for hash_key in HASH_KEYS:
            declared_hash = row.get(hash_key)

            if not declared_hash:
                continue

            actual_hash = sha256_file(asset)

            if (
                str(declared_hash).strip().lower()
                != actual_hash.lower()
            ):
                if retryable_narration_audio:
                    retryable_unavailable_files += 1
                    hash_mismatch = True
                    break

                raise ValueError(
                    f"{name}: {asset.name} "
                    f"{hash_key} mismatch."
                )

            break

        if hash_mismatch:
            continue

        if row.get("scene_id") is not None:
            if not str(row.get("scene_id")).strip():
                raise ValueError(
                    f"{name}: row {index} has empty scene_id."
                )

    scene_data = {
        "available": False,
        "part_counts": {},
    }

    if legacy_empty_tts_marker:
        scene_detail = (
            "Legacy empty TTS failure marker found; "
            "no completed jobs or audio assets are recorded. "
            "The current TTS stage may safely replace this marker."
        )

    elif resumable_tts_manifest:
        raw_counts = data.get("actual_scene_counts")

        if (
            not isinstance(raw_counts, list)
            or not raw_counts
        ):
            raise ValueError(
                "narration_audio: resumable TTS manifest "
                "has no actual_scene_counts."
            )

        if (
            expected_parts is not None
            and len(raw_counts) != expected_parts
        ):
            raise ValueError(
                "narration_audio: actual_scene_counts "
                "does not match expected Parts."
            )

        if (
            expected_format == "full"
            and len(raw_counts) != 1
        ):
            raise ValueError(
                "narration_audio: FULL mode must have "
                "exactly one scene-count entry."
            )

        normalized_counts = []

        for part_number, value in enumerate(
            raw_counts, 1
        ):
            if isinstance(value, bool):
                raise ValueError(
                    f"narration_audio: invalid scene count "
                    f"for Part {part_number}."
                )

            try:
                count = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"narration_audio: invalid scene count "
                    f"for Part {part_number}."
                ) from exc

            if not scenes_min <= count <= scenes_max:
                raise ValueError(
                    f"narration_audio: Part {part_number} "
                    f"has {count} Main Scenes; expected "
                    f"{scenes_min}-{scenes_max}."
                )

            normalized_counts.append(count)

        declared_main_total = data.get("total_main_scenes")

        if declared_main_total is not None:
            if (
                isinstance(declared_main_total, bool)
                or int(declared_main_total)
                != sum(normalized_counts)
            ):
                raise ValueError(
                    "narration_audio: total_main_scenes "
                    "does not match actual_scene_counts."
                )

        expected_job_total = (
            sum(normalized_counts)
            + (len(normalized_counts) * 2)
        )

        if total_jobs != expected_job_total:
            raise ValueError(
                "narration_audio: total_jobs does not "
                "match Main + Hook + Suspense/Resolution jobs."
            )

        declared_completed = summary.get("completed_jobs")

        if (
            declared_completed is not None
            and (
                type(declared_completed) is not int
                or declared_completed != len(raw_jobs)
            )
        ):
            raise ValueError(
                "narration_audio: completed_jobs does "
                "not match saved scene jobs."
            )

        scene_data = {
            "available": True,
            "part_counts": {
                str(index): count
                for index, count in enumerate(
                    normalized_counts, 1
                )
            },
        }

        scene_detail = (
            "TTS manifest is resumable. Planned scene "
            "counts are valid; completed audio records "
            "were checked where reusable."
        )

    else:
        scene_ok, scene_detail, scene_data = (
            validate_actual_scene_counts(
                name,
                data,
                scenes_min,
                scenes_max,
                expected_parts,
                expected_format,
            )
        )

        if not scene_ok:
            raise ValueError(scene_detail)

    result = {
        "status": (
            "valid_retryable"
            if retryable_narration_audio
            else "valid"
        ),
        "path": safe_relative(path),
        "sha256": sha256_file(path),
        "row_count": len(rows),
        "referenced_files_checked": checked_files,
        "actual_scene_counts": scene_data.get(
            "part_counts", {}
        ),
    }

    if retryable_narration_audio:
        result["retryable"] = True
        result["legacy_empty_placeholder"] = (
            legacy_empty_tts_marker
        )
        result["retryable_failed_rows"] = (
            retryable_failed_rows
        )
        result["retryable_unavailable_files"] = (
            retryable_unavailable_files
        )
        result["detail"] = scene_detail

    return result


def check_artifact_inventory(
    parts: int | None,
    scenes_min: int,
    scenes_max: int,
    input_fingerprint: str,
    mode: str,
) -> tuple[list[dict], dict]:
    checks = []
    inventory = {}

    for name, path in ARTIFACT_MANIFESTS:
        try:
            result = validate_existing_manifest(
                name,
                path,
                parts,
                scenes_min,
                scenes_max,
                input_fingerprint,
                mode,
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
                        f"{result.get('referenced_files_checked', 0)} files checked; "
                        f"actual Scene counts={result.get('actual_scene_counts', {})}."
                    )
                ),
            })

        except Exception as exc:
            inventory[name] = {
                "status": "invalid",
                "error": str(exc),
            }
            checks.append({
                "name": "existing_artifact:" + name,
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            })

    return checks, inventory


def check_resume_state(
    input_fingerprint: str,
    pipeline_fingerprint: str,
) -> tuple[bool, str, dict]:
    checkpoint_dirs = (
        OUT / "checkpoints",
        OUT / "story" / "checkpoints",
        OUT / "narration" / "checkpoints",
        OUT / "visuals" / "checkpoints",
        OUT / "photo_motion",
    )

    found = []

    for directory in checkpoint_dirs:
        if directory.is_dir():
            found.extend(
                path for path in directory.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() == ".json"
            )

    if not found:
        return True, "No prior checkpoint; clean first run is allowed.", {
            "available": False,
            "matching": 0,
            "stale": [],
            "files": 0,
        }

    matching = 0
    stale = []
    invalid = []

    for path in found:
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

        if not saved_input and not saved_pipeline:
            continue

        if (
            str(saved_input or "").lower() == input_fingerprint
            and str(saved_pipeline or "").lower() == pipeline_fingerprint
        ):
            matching += 1
        else:
            stale.append(safe_relative(path))

    if invalid:
        return False, "Corrupt checkpoint JSON: " + "; ".join(invalid[:10]), {
            "available": True,
            "matching": matching,
            "stale": stale,
            "invalid": invalid[:20],
            "files": len(found),
        }

    return True, (
        f"Checkpoint JSON files={len(found)}; matching={matching}; "
        f"stale manifests rejected={len(stale)}."
    ), {
        "available": True,
        "matching": matching,
        "stale": stale,
        "files": len(found),
        "fingerprint_policy": "input_and_pipeline_sha256_must_both_match",
    }


def check_provider_credentials(
    tts_order: list[str],
    image_order: list[str],
) -> tuple[list[dict], dict]:
    checks = []
    credentials = {}

    tts_credentials = {
        "fish_audio": ("FISH_API_KEY", "FISH_AUDIO_API_KEY"),
        "gemini_tts": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "edge_tts": (),
        "piper": (),
        "elevenlabs": ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"),
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

    def tts_available(provider: str) -> tuple[bool, list[str]]:
        names = tts_credentials[provider]

        if provider == "elevenlabs":
            present = [
                key for key in names
                if os.environ.get(key, "").strip()
            ]
            return len(present) == 2, present

        if provider == "piper":
            executable = shutil.which("piper")
            return bool(executable), [executable] if executable else []

        if provider == "edge_tts":
            return True, []

        present = [
            key for key in names
            if os.environ.get(key, "").strip()
        ]
        return bool(present), present

    tts_ready = []

    for provider in tts_order:
        if provider not in tts_credentials:
            continue

        ready, present = tts_available(provider)
        credentials[f"tts:{provider}"] = {
            "available": ready,
            "configured_fields": present,
        }

        if ready:
            tts_ready.append(provider)

    image_ready = []

    for provider in image_order:
        if provider not in image_credentials:
            continue

        names = image_credentials[provider]
        present = [
            key for key in names
            if os.environ.get(key, "").strip()
        ]

        ready = bool(present) or provider in {"huggingface", "pollinations"}

        credentials[f"image:{provider}"] = {
            "available": ready,
            "configured_fields": present,
        }

        if ready:
            image_ready.append(provider)

    checks.append({
        "name": "tts_provider_credentials",
        "passed": bool(tts_ready),
        "detail": (
            "Configured order preserved: "
            + " -> ".join(tts_order)
            + "; locally usable: "
            + (", ".join(tts_ready) or "none")
        ),
    })

    checks.append({
        "name": "image_provider_credentials",
        "passed": bool(image_ready),
        "detail": (
            "Configured order preserved: "
            + " -> ".join(image_order)
            + "; locally usable: "
            + (", ".join(image_ready) or "none")
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
            get_selected_parts_record,
            get_scenes_range,
            get_fps,
            get_tts_provider_order,
            get_music_provider_order,
            get_image_provider_order,
            get_global_billable_permission,
            get_story_text,
            get_caption_font,
            get_caption_font_size,
            get_topic,
            get_title_source,
        )

        config = load_and_validate(INPUT_FILE)

        mode = get_format(config)
        minimum_parts, maximum_parts = get_parts_range(config)
        selected = get_selected_parts(config, required=False)
        selection_record = get_selected_parts_record(config)
        scenes_min, scenes_max = get_scenes_range(config)

        fps = get_fps(config)
        tts_order = get_tts_provider_order(config)
        music_order = get_music_provider_order(config)
        image_order = get_image_provider_order(config)
        billable = get_global_billable_permission(config)
        font = get_caption_font(config)
        font_size = get_caption_font_size(config)
        story_text = get_story_text(config).strip()
        topic = get_topic(config)
        title_source = get_title_source(config)

        selection_valid = (
            isinstance(selected, int)
            and not isinstance(selected, bool)
            and minimum_parts <= selected <= maximum_parts
        )

        selection_pending = (
            mode == "short"
            and minimum_parts != maximum_parts
            and selected is None
        )

        add(
            "format",
            mode in {"short", "full"},
            f"FORMAT={mode}.",
        )

        add(
            "parts_range",
            (
                isinstance(minimum_parts, int)
                and isinstance(maximum_parts, int)
                and minimum_parts >= 1
                and maximum_parts >= minimum_parts
            ),
            f"Configured PARTS range={minimum_parts}-{maximum_parts}.",
        )

        if mode == "full":
            full_parts_ok = (
                minimum_parts == 1
                and maximum_parts == 1
                and selected == 1
            )
            add(
                "full_mode_single_part",
                full_parts_ok,
                (
                    "FULL mode is configured for exactly one Part."
                    if full_parts_ok
                    else (
                        "FULL mode must have PARTS=1, PARTS_MIN=1, "
                        "PARTS_MAX=1 and PARTS_SELECTED=1."
                    )
                ),
            )
        else:
            add(
                "actual_parts_selection",
                selection_valid or selection_pending,
                (
                    f"Persisted actual PARTS={selected}."
                    if selection_valid and selection_record
                    else (
                        "Actual PARTS selection is pending; story generation "
                        "must select and persist it."
                        if selection_pending
                        else "Actual PARTS selection is invalid or inconsistent."
                    )
                ),
            )

            add(
                "parts_selection_record",
                (
                    selection_pending
                    or (
                        selection_valid
                        and isinstance(selection_record, dict)
                        and selection_record.get("selected_parts") == selected
                        and selection_record.get("format") == mode
                    )
                ),
                (
                    "No actual selection is required before story generation."
                    if selection_pending
                    else (
                        "Persisted selection record matches current input."
                        if selection_valid and selection_record
                        else "Selection record missing or inconsistent."
                    )
                ),
            )

        scenes_valid = (
            isinstance(scenes_min, int)
            and not isinstance(scenes_min, bool)
            and isinstance(scenes_max, int)
            and not isinstance(scenes_max, bool)
            and 1 <= scenes_min <= scenes_max <= 500
        )

        add(
            "scene_range",
            scenes_valid,
            (
                f"SCENES inclusive range={scenes_min}-{scenes_max}."
                if scenes_valid
                else "SCENES must be a valid positive inclusive Min-Max range."
            ),
        )

        if mode == "full":
            add(
                "full_mode_scene_range",
                scenes_valid,
                (
                    "FULL mode uses the configured Scene range within its "
                    "single complete-story Part."
                    if scenes_valid
                    else "FULL mode Scene range is invalid."
                ),


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
                and all(item in TTS_ALLOWED for item in primary)
                and tts_order[:3] == primary
            ),
            "Primary TTS order: " + " -> ".join(primary),
        )

        add(
            "tts_fallback_order",
            (
                len(tts_order) >= 1
                and len(tts_order) == len(set(tts_order))
                and all(item in TTS_ALLOWED for item in tts_order)
            ),
            "Effective TTS order: " + " -> ".join(tts_order),
        )

        add(
            "music_provider_order",
            (
                bool(music_order)
                and len(music_order) == len(set(music_order))
                and all(item in {"lyria", "eleven_music", "stable_audio", "local"} for item in music_order)
            ),
            "Configured music order: " + " -> ".join(music_order),
        )

        add(
            "image_provider_order",
            (
                bool(image_order)
                and len(image_order) == len(set(image_order))
                and all(item in IMAGE_ALLOWED for item in image_order)
            ),
            "Configured image order: " + " -> ".join(image_order),
        )

        fallback_enabled = config.get("IMAGE_FALLBACK_ENABLED") is True

        add(
            "image_fallback_enabled",
            fallback_enabled,
            f"IMAGE_FALLBACK_ENABLED={fallback_enabled}.",
        )

        add(
            "billable_permission",
            billable is True,
            (
                "ALLOW_BILLABLE_PROVIDERS=true."
                if billable
                else "ALLOW_BILLABLE_PROVIDERS must be true."
            ),
        )

        add("caption_font", bool(font), f"CAPTION_FONT={font}.")

        add(
            "caption_font_size",
            (
                font_size == "auto"
                or (
                    isinstance(font_size, int)
                    and not isinstance(font_size, bool)
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
                "Pasted story preservation enabled."
                if story_text and preserve
                else (
                    "No pasted story supplied."
                    if not story_text
                    else "PRESERVE_STORY_TEXT must be true."
                )
            ),
        )

        add(
            "story_input",
            bool(story_text or topic or title_source),
            (
                "Complete story text supplied."
                if story_text
                else (
                    "Topic/title supplied."
                    if topic or title_source
                    else "STORY_TEXT, TOPIC or TITLE is required."
                )
            ),
        )

        motion_allowed = {
            "PHOTO_MOTION_STYLE": {
                "mixed", "zoom", "pan", "parallax", "dynamic", "static"
            },
            "PHOTO_MOTION_SPEED": {
                "slow", "normal", "fast", "very_fast"
            },
            "PHOTO_MOTION_VARIATION": {
                "low", "medium", "high"
            },
        }

        motion_errors = []

        for key, allowed in motion_allowed.items():
            value = str(config.get(key, "")).strip().lower()
            if value not in allowed:
                motion_errors.append(f"{key}={value!r}")

        try:
            strength = float(
                config.get("PHOTO_MOTION_PARALLAX_STRENGTH")
            )
            if not 0 <= strength <= 0.10:
                motion_errors.append(
                    "PHOTO_MOTION_PARALLAX_STRENGTH outside 0-0.055"
                )
        except (TypeError, ValueError):
            strength = None
            motion_errors.append(
                "PHOTO_MOTION_PARALLAX_STRENGTH is invalid"
            )

        for key in (
            "PHOTO_MOTION_PARALLAX",
            "PHOTO_MOTION_PRESERVE_SUBJECT",
        ):
            if not isinstance(config.get(key), bool):
                motion_errors.append(f"{key} must be boolean")

        add(
            "photo_motion_settings",
            not motion_errors,
            (
                "Generated-image zoom, pan and 2.5D parallax settings valid."
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
                else (
                    "RESUME_ENABLED, SAVE_CHECKPOINT_AFTER_EACH_SCENE "
                    "and SKIP_COMPLETED_SCENES must all be true."
                )
            ),
        )

        details.update({
            "format": mode,
            "parts_min": minimum_parts,
            "parts_max": maximum_parts,
            "selected_parts": selected,
            "selection_pending": selection_pending,
            "selection_record": selection_record,
            "scenes_min": scenes_min,
            "scenes_max": scenes_max,
            "scenes_range": [scenes_min, scenes_max],
            "fps": fps,
            "tts_provider_order": tts_order,
            "music_provider_order": music_order,
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
            "photo_motion_parallax_strength": strength,
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

    passed, detail = check_requirements_manifest()
    add("requirements_manifest_pinned_and_complete", passed, detail)

    for command in RUNTIME_COMMANDS:
        passed, detail = check_runtime_command(command)
        add("runtime_command:" + command, passed, detail)

    passed, detail = check_font()
    add("caption_font_installed_and_fallback_verified", passed, detail)

    mandatory_modules = ("cv2", "numpy", "PIL")
    mandatory_available = all(
        importlib.util.find_spec(module) is not None
        for module in mandatory_modules
    )

    add(
        "photo_motion_depth_dependencies",
        mandatory_available,
        (
            "OpenCV, NumPy and Pillow are available."
            if mandatory_available
            else "OpenCV, NumPy and Pillow are required."
        ),
    )

    ai_required = os.environ.get(
        "DEPTH_AI_REQUIRED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}

    ai_available = all(
        importlib.util.find_spec(module) is not None
        for module in ("torch", "transformers")
    )

    add(
        "depth_ai_dependencies",
        ai_available or not ai_required,
        (
            "PyTorch and Transformers are available."
            if ai_available
            else (
                "DEPTH_AI_REQUIRED=true but PyTorch/Transformers are missing."
                if ai_required
                else "AI depth dependencies are optional; fallback depth is permitted."
            )
        ),
    )

    tts_order = config.get("TTS_PROVIDER_ORDER", [])

    if "piper" in tts_order:
        piper_ready = bool(shutil.which("piper"))
        other_provider_exists = any(
            provider != "piper" for provider in tts_order
        )

        add(
            "piper_runtime",
            piper_ready or other_provider_exists,
            (
                "Piper executable found."
                if piper_ready
                else (
                    "Piper unavailable; configured alternative TTS providers remain."
                    if other_provider_exists
                    else "Piper is the only configured TTS provider but executable is missing."
                )
            ),
        )

    return checks


def calculate_pipeline_fingerprint() -> tuple[str, dict]:
    fingerprints = {}

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
        fingerprints[relative] = checksum
        lines.append(f"{checksum}  {relative}\n")

    aggregate = hashlib.sha256(
        "".join(lines).encode("utf-8")
    ).hexdigest()

    return aggregate, fingerprints


def check_workflow_structure(source: str) -> list[dict]:
    checks = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": str(detail),
        })

    if not source.strip():
        add(
            "workflow_readable",
            False,
            "Workflow file is empty or unreadable.",
        )
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
        "cpu_torch_install": r"(?s)python -m pip install torch\s*\\\s*--index-url https://download\.pytorch\.org/whl/cpu",
        "pip_dependency_check": r"(?m)^\s*python -m pip check\s*$",
        "caption_font_manifest": r"(?m)^\s*test -s output/fonts/font_manifest\.json\s*$",
        "artifact_upload_action": r"(?m)^\s{8}uses:\s*actions/upload-artifact@v4\s*$",
        "checkpoint_upload": (
            r"(?m)^\s{10}name:\s*"
            r"katha-photo-motion-checkpoint-\$\{\{\s*github\.run_id\s*\}\}\s*$"
        ),
        "final_output_upload": (
            r"(?m)^\s{10}name:\s*"
            r"katha-lok-ai-photo-motion-output-\$\{\{\s*github\.run_id\s*\}\}\s*$"
        ),
        "checkpoint_restore": (
            r"(?m)^\s{6}-\s+name:\s*"
            r"Restore matching resumable checkpoint\s*$"
        ),
        "checkpoint_save": (
            r"(?m)^\s{6}-\s+name:\s*"
            r"Save resumable checkpoint\s*$"
        ),
        "preflight_step": (
            r"(?m)^\s{6}-\s+name:\s*"
            r"Run strict Photo Motion preflight\s*$"
        ),
    }

    for name, pattern in patterns.items():
        passed = bool(re.search(pattern, source))
        add(
            "workflow_structure:" + name,
            passed,
            "Present." if passed else "Missing or incorrect.",
        )

    install_position = source.find(
        "python -m pip install -r requirements.txt"
    )
    preflight_position = source.find(
        "python scripts/pipeline_preflight.py"
    )
    restore_position = source.find(
        "Restore matching resumable checkpoint"
    )

    add(
        "workflow_dependency_install_before_preflight",
        install_position >= 0 and preflight_position > install_position,
        (
            "requirements.txt installation precedes preflight."
            if install_position >= 0 and preflight_position > install_position
            else "Install requirements.txt before preflight."
        ),
    )

    add(
        "workflow_checkpoint_restore_before_preflight",
        (
            restore_position >= 0
            and preflight_position >= 0
            and restore_position < preflight_position
        ),
        (
            "Checkpoint restore precedes preflight."
            if restore_position >= 0
            and preflight_position >= 0
            and restore_position < preflight_position
            else "Matching checkpoint must be restored before preflight."
        ),
    )

    return checks


def check_report_integrity(report: dict) -> tuple[bool, str]:
    checks = report.get("checks")

    if not isinstance(checks, list) or not checks:
        return False, "Preflight report has no checks."

    names = [
        item.get("name")
        for item in checks
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
        return False, "Preflight failure count mismatch."

    if report.get("checks_total") != len(checks):
        return False, "Preflight total check count mismatch."

    if report.get("checks_passed") != len(checks) - len(failed):
        return False, "Preflight passed count mismatch."

    if report.get("ready_to_run") is not (len(failed) == 0):
        return False, "Preflight readiness flag mismatch."

    if report.get("api_calls_made") != 0:
        return False, "Preflight must declare zero API calls."

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
    scenes_min = input_details.get("scenes_min")
    scenes_max = input_details.get("scenes_max")

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

    policy_ok, policy_detail = check_story_length_policy_integration()
    record(
        "story_length_policy_integration",
        policy_ok,
        policy_detail,
    )

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

    required_block = re.search(
        r"(?ms)^\s*required_files=\(\s*(.*?)^\s*\)",
        workflow,
    )
    workflow_required = (
        set(re.findall(r"scripts/([A-Za-z0-9_.-]+\.py)", required_block.group(1)))
        if required_block
        else set()
    )
    expected_required = set(REQUIRED_SCRIPTS)
    missing_from_workflow = sorted(expected_required - workflow_required)
    extra_in_workflow = sorted(workflow_required - expected_required)
    required_scripts_match = bool(required_block) and not missing_from_workflow and not extra_in_workflow
    record(
        "workflow_required_script_inventory",
        required_scripts_match,
        (
            f"Workflow and preflight both require {len(expected_required)} identical scripts."
            if required_scripts_match
            else f"Missing from workflow: {missing_from_workflow}; extra in workflow: {extra_in_workflow}."
        ),
    )

    installer_text = (SCRIPTS / "install_caption_fonts.py").read_text(encoding="utf-8")
    package_text = (SCRIPTS / "package_output.py").read_text(encoding="utf-8")
    validator_text = (SCRIPTS / "validate_final_output.py").read_text(encoding="utf-8")
    font_contract = (
        'PROJECT_MANIFEST = ROOT / "output" / "fonts" / "font_manifest.json"' in installer_text
        and "atomic_json(PROJECT_MANIFEST, manifest_data)" in installer_text
        and "test -s output/fonts/font_manifest.json" in workflow
        and 'OUT / "fonts" / "font_manifest.json"' in package_text
    )
    record(
        "caption_font_install_workflow_package_contract",
        font_contract,
        "Installer, workflow and package use output/fonts/font_manifest.json."
        if font_contract
        else "Caption font manifest paths do not agree across installer/workflow/package.",
    )

    package_contract = all(
        marker in package_text
        for marker in ("scene_image_members", "scene_audio_members", "depth_map_members", "selected_package_files")
    ) and all(
        marker in validator_text
        for marker in ("final_srt_sha256", "caption_timeline", "source_photo_manifest_sha256")
    )
    record(
        "final_media_and_package_contract",
        package_contract,
        "Package inventories scene images, narration audio and depth maps; validator binds captions and source manifests."
        if package_contract
        else "Final validator and package media contracts are incomplete.",
    )

    commands = extract_workflow_commands(workflow)

    record(
        "workflow_commands_detected",
        bool(commands),
        (
            f"Detected {len(commands)} Python script invocations."
            if commands
            else "No executable Python script commands detected."
        ),
    )

    order_ok, order_detail = check_workflow_order(commands)
    record("workflow_generation_order", order_ok, order_detail)

    preflight_ok, preflight_detail = check_workflow_preflight(commands)
    record(
        "preflight_before_generation",
        preflight_ok,
        preflight_detail,
    )

    story_ok, story_detail = check_story_gate(commands)
    record("story_length_hard_gate", story_ok, story_detail)

    visuals_ok, visuals_detail = check_prepare_visuals_order(commands)
    record(
        "prepare_visuals_execution_order",
        visuals_ok,
        visuals_detail,
    )

    observed = {item["script"] for item in commands}
    missing_commands = [
        name for name in WORKFLOW_ORDER
        if name not in observed
    ]

    record(
        "all_generation_scripts_invoked",
        not missing_commands,
        (
            "Every required generation script is invoked."
            if not missing_commands
            else "Missing workflow commands: "
            + ", ".join(missing_commands)
        ),
    )

    for item in check_local_dependencies(config):
        record(item["name"], item["passed"], item["detail"])

    credential_details = {}

    if input_details:
        credential_checks, credential_details = check_provider_credentials(
            input_details.get("tts_provider_order", []),
            input_details.get("image_provider_order", []),
        )

        for item in credential_checks:
            record(item["name"], item["passed"], item["detail"])

    else:
        record(
            "provider_credentials",
            False,
            "Provider configuration could not be loaded.",
        )

    input_fingerprint = (
        sha256_file(INPUT_FILE)
        if INPUT_FILE.is_file()
        else ""
    )

    pipeline_fingerprint, source_fingerprints = (
        calculate_pipeline_fingerprint()
    )

    record(
        "input_fingerprint",
        bool(input_fingerprint),
        (
            f"Input/topic.txt SHA256={input_fingerprint}."
            if input_fingerprint
            else "Input/topic.txt is unavailable."
        ),
    )

    record(
        "pipeline_fingerprint",
        bool(source_fingerprints),
        (
            f"Fingerprint created for {len(source_fingerprints)} source files."
            if source_fingerprints
            else "No source files available for pipeline fingerprint."


        ),
    )

    artifact_inventory = {}

    if (
        scenes_min is not None
        and scenes_max is not None
        and input_fingerprint
        and mode in {"short", "full"}
    ):
        artifact_checks, artifact_inventory = check_artifact_inventory(
            parts if isinstance(parts, int) else None,
            scenes_min,
            scenes_max,
            input_fingerprint,
            mode,
        )

        for item in artifact_checks:
            record(item["name"], item["passed"], item["detail"])

    else:
        record(
            "existing_artifact_validation",
            False,
            (
                "Cannot validate existing artifacts: valid FORMAT, "
                "SCENES range and input fingerprint are required."
            ),
        )

    resume_ok, resume_detail, resume_state = check_resume_state(
        input_fingerprint,
        pipeline_fingerprint,
    )

    record(
        "checkpoint_resume_integrity",
        resume_ok,
        resume_detail,
    )

    expected_paths = (
        INPUT_FILE,
        WORKFLOW_FILE,
        ROOT / "requirements.txt",
        SCRIPTS,
        OUT,
    )

    path_failures = [
        path.relative_to(ROOT).as_posix()
        for path in expected_paths
        if not path.exists()
    ]

    record(
        "expected_input_output_paths",
        not path_failures,
        (
            "Input, workflow, requirements, scripts and output paths exist."
            if not path_failures
            else "Missing: " + ", ".join(path_failures)
        ),
    )

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

    schema_checks = [
        name for name in (
            "narration_audio",
            "photo_motion_jobs",
            "photo_motion_manifest",
            "depth_manifest",
            "render_manifest",
            "captions",
        )
        if (
            name in artifact_inventory
            and artifact_inventory[name].get("status") == "valid"
        )
    ]

    record(
        "existing_manifest_schema_inventory",
        True,
        (
            "Existing manifest records inspected: "
            + (
                ", ".join(schema_checks)
                if schema_checks
                else "no stage manifests exist yet."
            )
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
        "parts_range": (
            [
                input_details.get("parts_min"),
                input_details.get("parts_max"),
            ]
            if input_details else None
        ),
        "selected_parts": parts,
        "parts_selection_pending": input_details.get(
            "selection_pending"
        ),
        "parts_selection_record": input_details.get(
            "selection_record"
        ),
        "scenes_range": (
            [scenes_min, scenes_max]
            if scenes_min is not None and scenes_max is not None
            else None
        ),
        "selected_parts_scene_counts": {
            name: data.get("actual_scene_counts", {})
            for name, data in artifact_inventory.items()
            if data.get("actual_scene_counts")
        },
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
            "motion_scope": (
                "Generated-image zoom, pan and 2.5D parallax only."
            ),
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
            "Preflight is offline and does not call generation/provider APIs.",
            "Credential presence is checked locally; authentication, quotas and provider availability are not tested.",
            "Piper is not mandatory when another configured TTS provider remains available.",
            "An unresolved SHORT PARTS range is allowed before story generation.",
            "FULL mode is restricted to exactly one Part.",
            "SCENES is validated as an inclusive per-Part minimum-maximum range.",
            "Actual scene counts are checked from available scene records; missing future-stage artifacts are allowed.",
            "Existing artifacts are checked for readable manifests, safe output paths and declared SHA256 values.",
            "Checkpoint reuse requires matching input and pipeline fingerprints.",
            "Audio duration is measured by generation/final-validation stages, not by preflight.",
            "Only generated-image zoom, pan and 2.5D parallax are permitted.",
        ],
    }

    try:
        atomic_json(REPORT_FILE, report)

        saved = json.loads(
            REPORT_FILE.read_text(encoding="utf-8")
        )

        integrity_ok, integrity_detail = check_report_integrity(saved)

        if not integrity_ok:
            raise RuntimeError(integrity_detail)

        record(
            "report_integrity",
            True,
            integrity_detail,
        )

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

        final_saved = json.loads(
            REPORT_FILE.read_text(encoding="utf-8")
        )

        final_ok, final_detail = check_report_integrity(final_saved)

        if not final_ok:
            raise RuntimeError(final_detail)

    except Exception as exc:
        log(
            "PREFLIGHT REPORT ERROR:",
            f"{type(exc).__name__}: {exc}",
        )
        return 1

    log("-" * 72)
    log("CHECKS:", report["checks_total"])
    log("PASSED:", report["checks_passed"])
    log("FAILED:", report["checks_failed"])
    log("FORMAT:", mode)
    log(
        "PARTS RANGE:",
        input_details.get("parts_min"),
        "-",
        input_details.get("parts_max"),
    )
    log(
        "ACTUAL PARTS:",
        parts if parts is not None else "PENDING",
    )
    log(
        "SCENES RANGE:",
        scenes_min,
        "-",
        scenes_max,
    )
    log("REPORT:", REPORT_FILE.relative_to(ROOT))

    if failed:
        log("PREFLIGHT FAILED — generation must not start.")

        for item in failed:
            log(
                "BLOCKER:",
                item["name"],
                "-",
                item["detail"],
            )

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
        log(
            "PREFLIGHT INTERNAL ERROR:",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)
