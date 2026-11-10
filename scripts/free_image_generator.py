#!/usr/bin/env python3
"""Verified, resumable image generation using configured provider order."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path

from gemini_http import retry_after_seconds
from visual_quality_validator import (
    VisionVerificationUnavailable,
    validate_scene_image,
)
from image_provider_adapters import (
    generate_ai_horde_image as adapter_generate_ai_horde,
    generate_ovhcloud_image as adapter_generate_ovhcloud,
)
from urllib.parse import quote, urlparse

import requests
from PIL import Image, ImageOps, ImageEnhance

try:
    from gradio_client import Client
except ImportError:
    Client = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
VISUAL_DIR = ROOT / "output" / "visuals"

JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
JOBS_MANIFEST = VISUAL_DIR / "visual_jobs_manifest.json"
IMAGE_MANIFEST = VISUAL_DIR / "image_manifest.json"
GENERATION_MANIFEST = VISUAL_DIR / "image_generation_manifest.json"
MODEL_PROGRESS_FILE = ROOT / "output" / "checkpoints" / "image_model_progression.json"
SELECTED_MODEL_FILE = ROOT / "output" / "config" / "selected_visual_model.json"

VERSION = "photo-motion-image-generator-v38-vision-retry-and-unverified-candidate-resume"
MIN_IMAGE_BYTES = 1024
MAX_IMAGE_BYTES = 80 * 1024 * 1024
MIN_DIMENSION = 256
MAX_DIMENSION = 4096
BASE_SEED = 20261009
ALLOWED_ROLES = {"hook", "main", "suspense", "resolution"}
ALLOWED_FORMATS = {"short", "full"}

TIMEOUT = max(30, min(900, int(os.getenv("IMAGE_PROVIDER_TIMEOUT", "180"))))
POLL_INTERVAL = 2

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "KathaLokAI-PhotoMotion/33.0",
})

REPLICATE_KEY = os.getenv("REPLICATE_API_TOKEN", "").strip()
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
CLOUDFLARE_IMAGE_API_TOKEN = os.getenv("CLOUDFLARE_IMAGE_API_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()

PROVIDER_ORDER = []
HF_SPACES = []
POLLINATIONS_MODELS = []
REPLICATE_MODEL = ""
CLOUDFLARE_IMAGE_MODEL = ""
CLOUDFLARE_IMAGE_FALLBACK_MODEL = ""
OVHCLOUD_IMAGE_MODEL = ""
AIHORDE_IMAGE_MODEL = "stable_diffusion_xl"
ALLOW_BILLABLE = False
IMAGE_FALLBACK_ENABLED = True
MAX_RETRIES = 1

BILLABLE_BLOCKED = False
MODEL_PROGRESS_STATE = {}
IMAGE_QUALITY_VISION_ENABLED = True
IMAGE_QUALITY_VISION_MODEL = "gemini-3.8-flash"
IMAGE_QUALITY_MIN_SCORE = 7
IMAGE_QUALITY_MAX_FAILURES_PER_MODEL = 2
IMAGE_QUALITY_SKIP_SCENES = 3
IMAGE_PROCESSING_PROFILE = "none"


class ImageQualityRejected(RuntimeError):
    """A generated image failed semantic or visual quality acceptance."""


PROVIDER_ALIASES = {
    "hf": "huggingface",
    "hf_space": "huggingface",
    "hugging_face": "huggingface",
    "huggingface": "huggingface",
    "pollinations": "pollinations",
    "replicate": "replicate",
    "cloudflare": "cloudflare",
    "cloudflare_workers_ai": "cloudflare",
}


def log(*values):
    print(*values, flush=True)


def clean(value):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def split_values(value):
    if isinstance(value, (list, tuple)):
        source = value
    else:
        source = re.split(r"[,;]", str(value or ""))

    result = []
    seen = set()

    for item in source:
        value = clean(item)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)

    return result


def sha256_text(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256_text(raw)


def read_json(path, default=None, required=False):
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required JSON file missing: {path}")
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_input_settings():
    sys.path.insert(0, str(SCRIPTS))
    from input_config import load_and_validate
    return load_and_validate(ROOT / "Input" / "topic.txt")


def apply_input_settings(config):
    global PROVIDER_ORDER, HF_SPACES, POLLINATIONS_MODELS
    global REPLICATE_MODEL, CLOUDFLARE_IMAGE_MODEL
    global CLOUDFLARE_IMAGE_FALLBACK_MODEL
    global OVHCLOUD_IMAGE_MODEL, AIHORDE_IMAGE_MODEL
    global ALLOW_BILLABLE, IMAGE_FALLBACK_ENABLED, MAX_RETRIES
    global IMAGE_QUALITY_VISION_ENABLED, IMAGE_QUALITY_VISION_MODEL
    global IMAGE_QUALITY_MIN_SCORE, IMAGE_QUALITY_MAX_FAILURES_PER_MODEL
    global IMAGE_QUALITY_SKIP_SCENES, IMAGE_PROCESSING_PROFILE

    from input_config import (
        get_image_provider_settings,
        get_max_retries,
    )

    from input_config import get_production_settings
    production_settings = get_production_settings(config)
    IMAGE_PROCESSING_PROFILE = production_settings["image_profile"]
    settings = get_image_provider_settings(config)

    # Keep the exact order defined by Input/topic.txt.
    PROVIDER_ORDER = list(settings["IMAGE_PROVIDER_ORDER"])
    HF_SPACES = list(settings["FREE_IMAGE_FALLBACK_SPACES"])

    if settings["HF_IMAGE_SPACE"]:
        HF_SPACES.insert(0, settings["HF_IMAGE_SPACE"])

    POLLINATIONS_MODELS = list(
        settings["POLLINATIONS_FREE_IMAGE_MODELS"]
    )
    REPLICATE_MODEL = clean(settings["REPLICATE_IMAGE_MODEL"])
    CLOUDFLARE_IMAGE_MODEL = clean(settings["CLOUDFLARE_IMAGE_MODEL"])
    CLOUDFLARE_IMAGE_FALLBACK_MODEL = clean(
        settings["CLOUDFLARE_IMAGE_FALLBACK_MODEL"]
    )
    OVHCLOUD_IMAGE_MODEL = clean(settings["OVHCLOUD_IMAGE_MODEL"])
    AIHORDE_IMAGE_MODEL = clean(settings["AIHORDE_IMAGE_MODEL"])

    # Both the input setting and workflow environment must allow billing.
    ALLOW_BILLABLE = bool(settings["IMAGE_ALLOW_BILLABLE"])

    IMAGE_FALLBACK_ENABLED = bool(
        settings["IMAGE_FALLBACK_ENABLED"]
    )
    MAX_RETRIES = max(1, min(5, int(get_max_retries(config))))
    IMAGE_QUALITY_VISION_ENABLED = bool(config.get("IMAGE_QUALITY_VISION_ENABLED", True))
    IMAGE_QUALITY_VISION_MODEL = clean(config.get("IMAGE_QUALITY_VISION_MODEL")) or "gemini-3.8-flash"
    IMAGE_QUALITY_MIN_SCORE = max(1, min(10, int(config.get("IMAGE_QUALITY_MIN_SCORE", 7))))
    IMAGE_QUALITY_MAX_FAILURES_PER_MODEL = max(1, min(10, int(config.get("IMAGE_QUALITY_MAX_FAILURES_PER_MODEL", 2))))
    IMAGE_QUALITY_SKIP_SCENES = max(1, min(50, int(config.get("IMAGE_QUALITY_SKIP_SCENES", 3))))

    supported = {"huggingface", "pollinations", "replicate", "cloudflare", "cloudflare_schnell", "cloudflare_klein", "ovhcloud", "aihorde"}

    if not PROVIDER_ORDER:
        raise RuntimeError("IMAGE_PROVIDER_ORDER is empty.")

    if len(PROVIDER_ORDER) != len(set(PROVIDER_ORDER)):
        raise RuntimeError("IMAGE_PROVIDER_ORDER contains duplicates.")

    for provider in PROVIDER_ORDER:
        if provider not in supported:
            raise RuntimeError(
                f"Unsupported configured image provider: {provider}"
            )

    if "huggingface" in PROVIDER_ORDER and not HF_SPACES:
        raise RuntimeError("No Hugging Face Space is configured.")

    if "pollinations" in PROVIDER_ORDER and not POLLINATIONS_MODELS:
        raise RuntimeError("Pollinations model list is empty.")

    if (
        "replicate" in PROVIDER_ORDER
        and not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            REPLICATE_MODEL,
        )
    ):
        raise RuntimeError(
            "REPLICATE_IMAGE_MODEL must use owner/model format."
        )
    if (
        "cloudflare" in PROVIDER_ORDER
        and not re.fullmatch(
            r"@cf/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            CLOUDFLARE_IMAGE_MODEL,
        )
    ):
        raise RuntimeError(
            "CLOUDFLARE_IMAGE_MODEL must use @cf/owner/model format."
        )

    log("Configured image provider order:", " -> ".join(PROVIDER_ORDER))
    log("Billable image providers allowed:", ALLOW_BILLABLE)
    log("Provider fallback enabled:", IMAGE_FALLBACK_ENABLED)
    log("Maximum complete fallback rounds:", MAX_RETRIES)
    log("Gemini vision quality validation:", IMAGE_QUALITY_VISION_ENABLED)
    log("Vision model:", IMAGE_QUALITY_VISION_MODEL)
    log("Minimum image quality score:", IMAGE_QUALITY_MIN_SCORE, "/ 10")


def safe_path(value, label):
    raw = clean(value)
    if not raw:
        raise RuntimeError(f"{label}: output path is missing.")

    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    root = ROOT.resolve()

    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"{label}: output path escapes repository.")

    return resolved


def parse_size(value):
    try:
        width, height = map(
            int,
            str(value).lower().strip().split("x", 1),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid image size: {value!r}") from exc

    if not (
        MIN_DIMENSION <= width <= MAX_DIMENSION
        and MIN_DIMENSION <= height <= MAX_DIMENSION
    ):
        raise RuntimeError(f"Invalid image dimensions: {width}x{height}")

    return width, height


def get_jobs():
    jobs_document = read_json(JOBS_FILE, required=True)
    jobs_manifest = read_json(JOBS_MANIFEST, {})

    if isinstance(jobs_document, list):
        jobs = jobs_document
    elif isinstance(jobs_document, dict):
        jobs = (
            jobs_document.get("jobs")
            or jobs_document.get("visual_jobs")
            or jobs_document.get("scenes")
        )
        if not isinstance(jobs, list):
            raise RuntimeError("visual_jobs.json has no valid jobs list.")
        jobs_manifest = {**jobs_manifest, **jobs_document}
    else:
        raise RuntimeError("visual_jobs.json root must be a list or object.")

    if not jobs:
        raise RuntimeError("visual_jobs.json contains no image jobs.")

    if not isinstance(jobs_manifest, dict) or not jobs_manifest:
        raise RuntimeError("visual_jobs_manifest.json is missing or invalid.")
    if jobs_manifest.get("status") != "completed":
        raise RuntimeError("Visual jobs manifest is not completed.")
    if clean(jobs_manifest.get("stage")) != "visual_jobs_prepared":
        raise RuntimeError("Visual jobs manifest has an unexpected stage.")
    if clean(jobs_manifest.get("format")).lower() not in ALLOWED_FORMATS:
        raise RuntimeError("Visual jobs manifest has an invalid SHORT/FULL format.")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", clean(jobs_manifest.get("source_fingerprint"))):
        raise RuntimeError("Visual jobs manifest source fingerprint is missing or invalid.")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", clean(jobs_manifest.get("config_signature"))):
        raise RuntimeError("Visual jobs manifest config signature is missing or invalid.")

    prepared = []
    seen_global = set()
    seen_slots = set()
    seen_ids = set()
    seen_paths = set()

    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Image job #{index} is not an object.")

        try:
            part = int(job["part"])
            global_number = int(
                job.get("global_scene", job.get("scene_index", index))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Image job #{index} has invalid numbering."
            ) from exc

        role = clean(job.get("role") or job.get("scene_type") or "main").lower()
        if role not in ALLOWED_ROLES:
            raise RuntimeError(f"Unsupported image role: {role!r}")

        scene_id = clean(
            job.get("scene_id") or job.get("id") or job.get("source_scene_id")
        )

        if not scene_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
            raise RuntimeError(f"Image job #{index} has an invalid scene ID.")
        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate image scene ID: {scene_id}")
        if part < 1 or global_number < 1 or global_number in seen_global:
            raise RuntimeError(f"{scene_id}: invalid or duplicate numbering.")

        source_scene_number = None
        if role == "main":
            raw_local = (
                job.get("source_scene_number")
                or job.get("source_local_scene")
                or job.get("scene")
            )
            try:
                source_scene_number = int(raw_local)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{scene_id}: main scene requires source scene number."
                ) from exc
            if source_scene_number < 1:
                raise RuntimeError(f"{scene_id}: invalid main scene number.")

        try:
            local_slot = int(job.get("local_scene", job.get("scene", index)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{scene_id}: invalid local image slot.") from exc

        if local_slot < 1 or (part, local_slot) in seen_slots:
            raise RuntimeError(f"{scene_id}: duplicate or invalid local slot.")

        size_value = clean(job.get("image_size") or jobs_manifest.get("image_size"))
        if not size_value:
            mode = clean(jobs_manifest.get("format")).lower()
            size_value = "720x1280" if mode == "short" else (
                "1280x720" if mode == "full" else ""
            )

        if not size_value:
            raise RuntimeError(f"{scene_id}: image size and format are missing.")

        size = parse_size(size_value)
        prompt = next((
            clean(job.get(key))
            for key in (
                "prompt", "image_prompt", "visual_prompt",
                "scene_prompt", "description",
            )
            if clean(job.get(key))
        ), "")
        narration = clean(job.get("narration"))
        story_anchor = clean(job.get("story_anchor"))
        if len(prompt) < 24:
            raise RuntimeError(f"{scene_id}: visual prompt is missing or too short.")
        if role == "main":
            if len(narration) < 8:
                raise RuntimeError(f"{scene_id}: Main narration is missing or too short.")
            if job.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: Main voiceover must be enabled.")
            if job.get("narration_source") != "locked_part_text":
                raise RuntimeError(f"{scene_id}: Main narration source is not locked.")
            if job.get("source_text_sha256") != sha256_text(narration):
                raise RuntimeError(f"{scene_id}: Main narration hash mismatch.")
            if job.get("source_word_count") != len(narration.split()):
                raise RuntimeError(f"{scene_id}: Main narration word count mismatch.")
        else:
            if len(narration.split()) < 8:
                raise RuntimeError(f"{scene_id}: extra-role narration is missing or too short.")
            if job.get("voiceover_enabled") is not True:
                raise RuntimeError(f"{scene_id}: extra-role voiceover must be enabled.")
            if job.get("narration_source") != "ai_generated_role_narration":
                raise RuntimeError(f"{scene_id}: extra-role narration source is invalid.")
            if job.get("source_text_sha256") != sha256_text(narration):
                raise RuntimeError(f"{scene_id}: extra-role narration hash mismatch.")
            if job.get("source_word_count") != len(narration.split()):
                raise RuntimeError(f"{scene_id}: extra-role narration word count mismatch.")
            if not story_anchor:
                raise RuntimeError(f"{scene_id}: extra-role story anchor is missing.")

        destination = safe_path(
            job.get("output_path") or job.get("image_path"),
            scene_id,
        )
        relative_path = destination.relative_to(ROOT).as_posix()

        if (
            relative_path in seen_paths
            or not relative_path.startswith("output/visuals/images/")
        ):
            raise RuntimeError(
                f"{scene_id}: duplicate or invalid image destination."
            )

        signatures = {
            key: clean(job.get(key))
            for key in (
                "source_scene_signature",
                "prompt_signature",
                "config_signature",
                "source_fingerprint",
            )
        }

        if not all(signatures.values()):
            raise RuntimeError(f"{scene_id}: required fingerprints are missing.")

        seen_global.add(global_number)
        seen_slots.add((part, local_slot))
        seen_ids.add(scene_id)
        seen_paths.add(relative_path)

        prepared.append({
            "scene": global_number,
            "global_scene": global_number,
            "part": part,
            "local_scene": local_slot,
            "source_scene_number": source_scene_number,
            "scene_id": scene_id,
            "role": role,
            "prompt": prompt,
            "narration": narration,
            "story_anchor": story_anchor,
            "voiceover_enabled": True,
            "narration_source": (
                "locked_part_text" if role == "main"
                else "ai_generated_role_narration"
            ),
            "visual_only": False,
            "source_text_sha256": sha256_text(narration),
            "source_word_count": len(narration.split()),
            "negative_prompt": clean(job.get("negative_prompt")),
            "size": size,
            "size_text": f"{size[0]}x{size[1]}",
            "destination": destination,
            "relative_path": relative_path,
            "duration": job.get("scene_duration", job.get("duration")),
            "source_job": job,
        })

    prepared.sort(key=lambda row: row["global_scene"])

    if [row["global_scene"] for row in prepared] != list(range(1, len(prepared) + 1)):
        raise RuntimeError("Global image job numbering must be continuous from 1.")

    expected_count = jobs_manifest.get("total_jobs")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count != len(prepared):
        raise RuntimeError("Job count differs from visual jobs manifest.")

    expected_fingerprint = clean(jobs_manifest.get("source_fingerprint"))
    expected_config = clean(jobs_manifest.get("config_signature"))
    format_name = clean(jobs_manifest.get("format")).lower()
    expected_size = "720x1280" if format_name == "short" else "1280x720"
    if clean(jobs_manifest.get("image_size")) != expected_size:
        raise RuntimeError(
            f"{format_name.upper()} manifest must declare image_size={expected_size}."
        )

    part_numbers = sorted({row["part"] for row in prepared})
    if not part_numbers or part_numbers != list(range(1, len(part_numbers) + 1)):
        raise RuntimeError("Image jobs must contain continuous Part numbers starting at 1.")

    raw_actual_counts = jobs_manifest.get("actual_scene_counts")
    if not isinstance(raw_actual_counts, dict):
        raise RuntimeError("Visual jobs manifest actual_scene_counts must be an object.")
    actual_counts = {}
    for part in part_numbers:
        value = raw_actual_counts.get(str(part), raw_actual_counts.get(part))
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RuntimeError(f"Part {part}: actual Scene count is missing or invalid.")
        actual_counts[part] = value

    scenes_min = jobs_manifest.get("scenes_min")
    scenes_max = jobs_manifest.get("scenes_max")
    if (
        isinstance(scenes_min, bool) or not isinstance(scenes_min, int)
        or isinstance(scenes_max, bool) or not isinstance(scenes_max, int)
        or scenes_min < 1 or scenes_max < scenes_min
    ):
        raise RuntimeError("Visual jobs manifest SCENES range is invalid.")
    if any(not scenes_min <= count <= scenes_max for count in actual_counts.values()):
        raise RuntimeError("Per-Part actual Scene count is outside the configured SCENES range.")

    role_totals = {role: 0 for role in ALLOWED_ROLES}
    part_role_totals = {
        part: {role: 0 for role in ALLOWED_ROLES}
        for part in part_numbers
    }
    main_seen = set()
    for row in prepared:
        source = row["source_job"]
        if clean(source.get("source_fingerprint")) != expected_fingerprint:
            raise RuntimeError(f"{row['scene_id']}: source fingerprint mismatch.")
        if clean(source.get("config_signature")) != expected_config:
            raise RuntimeError(f"{row['scene_id']}: config signature mismatch.")
        if row["size_text"] != expected_size:
            raise RuntimeError(f"{row['scene_id']}: image dimensions do not match {format_name.upper()}.")

        role_totals[row["role"]] += 1
        part_role_totals[row["part"]][row["role"]] += 1
        if row["role"] == "main":
            number = row["source_scene_number"]
            if number > actual_counts[row["part"]]:
                raise RuntimeError(f"{row['scene_id']}: Main Scene exceeds actual Part Scene count.")
            pair = (row["part"], number)
            if pair in main_seen:
                raise RuntimeError(f"{row['scene_id']}: duplicate Main Scene number.")
            main_seen.add(pair)

    expected_main = {
        (part, number)
        for part in part_numbers
        for number in range(1, actual_counts[part] + 1)
    }
    if main_seen != expected_main:
        raise RuntimeError("Image jobs do not cover every actual Main Scene exactly once.")

    for position, part in enumerate(part_numbers):
        counts = part_role_totals[part]
        if counts["hook"] != 1 or counts["main"] != actual_counts[part]:
            raise RuntimeError(f"Part {part}: expected one Hook and {actual_counts[part]} Main images.")
        if position < len(part_numbers) - 1:
            if counts["suspense"] != 1 or counts["resolution"] != 0:
                raise RuntimeError(f"Part {part}: expected one Suspense and no Resolution image.")
        elif counts["resolution"] != 1 or counts["suspense"] != 0:
            raise RuntimeError("Final Part must have one Resolution and no Suspense image.")

    if jobs_manifest.get("total_main_scenes") != len(expected_main):
        raise RuntimeError("Manifest total_main_scenes does not match actual per-Part Scene counts.")
    declared_roles = jobs_manifest.get("scene_role_counts")
    if not isinstance(declared_roles, dict):
        raise RuntimeError("Manifest scene_role_counts is missing.")
    for role, count in role_totals.items():
        if declared_roles.get(role) != count:
            raise RuntimeError(f"Manifest {role} image count does not match jobs.")

    return prepared, jobs_manifest


def make_prompt(item):
    source = item["source_job"]
    sections = [
        item["prompt"],
        (
            (
                "Locked Main Scene narration context: " + item["narration"]
                if item["role"] == "main"
                else f"Locked {item['role'].title()} narration context: " + item["narration"]
            )
        ),
        "Photorealistic live-action cinematic still.",
        (
            "Depict the exact story event. Preserve all explicit names, "
            "people, locations, objects, animals, vehicles, clothing, "
            "weather, time period, relationships and actions. Do not "
            "replace specific story details with generic imagery."
        ),
        (
            "Locked narration is context only and must not appear as visible text, "
            "subtitles, labels or typography."
        ),
        (
            "Create one still image for later zoom, pan and optional 2.5D "
            "parallax. Keep important faces and objects within safe margins. "
            "Do not depict body, face or character animation."
        ),
        (
            "Natural anatomy, realistic skin texture, believable materials, "
            "physically plausible lighting and coherent perspective. "
            "No cartoon, anime, illustration, CGI, logo or watermark."
        ),
    ]

    for key, label in (
        ("location", "Exact story location"),
        ("world_context", "Story-world continuity"),
        ("time_period", "Story time period"),
        ("action", "Required visible action"),
        ("objects", "Story-specific objects"),
        ("animals", "Story-specific animals"),
        ("vehicles", "Story-specific vehicles"),
        ("clothing", "Story-specific clothing"),
        ("required_story_entities", "Required story entities — names, places, objects, animals, vehicles and clothing"),
        ("camera_prompt", "Camera composition"),
        ("lighting_prompt", "Lighting"),
        ("mood", "Emotional mood"),
    ):
        value = source.get(key)
        if isinstance(value, list):
            value = ", ".join(clean(entry) for entry in value if clean(entry))
        value = clean(value)
        if value:
            sections.append(f"{label}: {value}")

    character_records = source.get("character_references")
    if isinstance(character_records, list) and character_records:
        sections.append(
            "CHARACTER IDENTITY LOCK: "
            + json.dumps(character_records, ensure_ascii=False, separators=(",", ":"))
        )
    else:
        names = source.get("character_names") or source.get("characters")
        if names:
            sections.append(
                "Required story characters: "
                + ", ".join(split_values(names))
                + ". Preserve established identity."
            )

    role_instructions = {
        "hook": "HOOK FRAME: depict the actual opening hook event without revealing final resolution.",
        "suspense": "SUSPENSE FRAME: depict actual unresolved tension without showing final resolution.",
        "resolution": "FINAL RESOLUTION FRAME: depict the actual story ending and consequence.",
    }
    if item["role"] in role_instructions:
        sections.append(role_instructions[item["role"]])

    return "\n\n".join(sections)


def prompt_hash(item):
    return sha256_text(make_prompt(item))


def build_negative_prompt(item):
    values = [
        "wrong story event", "unrelated characters", "unrelated props",
        "wrong location", "wrong time period", "distorted face",
        "unrealistic anatomy", "extra fingers", "missing fingers",
        "fused limbs", "duplicated people", "plastic skin", "waxy face",
        "floating objects", "captions", "subtitles", "unintended text",
        "logo", "watermark", "low resolution", "blurry main subject",
        "cartoon", "anime", "comic-book illustration", "2D drawing",
        "3D animated character", "body animation", "face animation",
        "character animation", "motion blur",
    ]
    if item["negative_prompt"]:
        values.append(item["negative_prompt"])
    return ", ".join(dict.fromkeys(values))


def job_signature(item):
    source = item["source_job"]
    return stable_hash({
        "version": VERSION,
        "scene_id": item["scene_id"],
        "part": item["part"],
        "global_scene": item["global_scene"],
        "role": item["role"],
        "prompt": make_prompt(item),
        "negative_prompt": build_negative_prompt(item),
        "size": item["size_text"],
        "source_scene_signature": source.get("source_scene_signature"),
        "prompt_signature": source.get("prompt_signature"),
        "config_signature": source.get("config_signature"),
        "source_fingerprint": source.get("source_fingerprint"),
    })

def negative_prompt_sha256(item):
    return sha256_text(build_negative_prompt(item))


def image_reuse_signature(item):
    source = item["source_job"]
    return stable_hash({
        "scene_id": item["scene_id"],
        "part": item["part"],
        "global_scene": item["global_scene"],
        "role": item["role"],
        "prompt_sha256": prompt_hash(item),
        "negative_prompt_sha256": negative_prompt_sha256(item),
        "size": item["size_text"],
        "config_signature": source.get("config_signature"),
        "image_path": item["relative_path"],
    })


def valid_image(path, size):
    try:
        path = Path(path)
        if (
            not path.is_file()
            or path.stat().st_size < MIN_IMAGE_BYTES
            or path.stat().st_size > MAX_IMAGE_BYTES
        ):
            return False

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            return (
                image.format in {"PNG", "JPEG", "WEBP"}
                and image.size == tuple(size)
                and image.width >= MIN_DIMENSION
                and image.height >= MIN_DIMENSION
            )
    except Exception:
        return False


def image_integrity(path, size):
    if not valid_image(path, size):
        raise RuntimeError(f"Image integrity check failed: {path}")

    with Image.open(path) as image:
        image_format = image.format
        width, height = image.size
        image.load()
        extrema = image.convert("RGB").getextrema()

    if all(low == high for low, high in extrema):
        raise RuntimeError(f"Image is a single flat colour: {path}")

    return {
        "format": image_format,
        "width": width,
        "height": height,
        "size_bytes": Path(path).stat().st_size,
        "sha256": sha256_file(path),
        "file_integrity": "verified",
        "semantic_story_match": "not_verified_without_vision_model",
    }


def save_image(image, destination, size):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.png")

    try:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image = ImageOps.pad(
            image,
            tuple(size),
            method=Image.Resampling.LANCZOS,
            color=(18, 18, 18),
            centering=(0.5, 0.5),
        )
        # Apply optional finishing before hashing and Gemini Vision validation.
        if IMAGE_PROCESSING_PROFILE != "none":
            profiles = {"natural": (1.04, 1.02, 0.15), "cinematic": (1.08, 1.06, 0.25), "soft": (1.02, 0.98, 0.0)}
            if IMAGE_PROCESSING_PROFILE not in profiles:
                raise RuntimeError(f"Unsupported IMAGE_PROCESSING_PROFILE: {IMAGE_PROCESSING_PROFILE}")
            contrast, color, sharpness = profiles[IMAGE_PROCESSING_PROFILE]
            image = ImageEnhance.Contrast(image).enhance(contrast)
            image = ImageEnhance.Color(image).enhance(color)
            if sharpness:
                image = ImageEnhance.Sharpness(image).enhance(1.0 + sharpness)
        image.save(temporary, "PNG", optimize=True)
        image_integrity(temporary, size)
        os.replace(temporary, destination)
        image_integrity(destination, size)
    finally:
        temporary.unlink(missing_ok=True)


def save_image_bytes(raw, destination, size):
    if not raw:
        raise RuntimeError("Provider returned empty image data.")
    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_image(image, destination, size)

def save_image_bytes_fit(raw, destination, size):
    """Crop square-provider output to the requested aspect ratio without bars."""
    if not raw:
        raise RuntimeError("Provider returned empty image data.")
    with Image.open(BytesIO(raw)) as image:
        image.load()
        fitted = ImageOps.fit(
            ImageOps.exif_transpose(image).convert("RGB"),
            tuple(size),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        save_image(fitted, destination, size)



def response_image(response, destination, size, provider):
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()

    if not content_type.startswith("image/"):
        raise RuntimeError(
            f"{provider} returned non-image content: {content_type or 'unknown'}"
        )
    if len(response.content) > MAX_IMAGE_BYTES:
        raise RuntimeError(f"{provider} image exceeds size limit.")

    save_image_bytes(response.content, destination, size)


def deterministic_seed(item):
    raw = f"{BASE_SEED}:{item['scene_id']}:{item['global_scene']}"
    # Pollinations validates seed as a signed 32-bit integer:
    # -1 <= seed <= 2147483647. Keep deterministic values in that range.
    digest_seed = int(
        hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8],
        16,
    )
    return digest_seed & 0x7FFFFFFF


def hf_parameter_name(parameter):
    if not isinstance(parameter, dict):
        return ""
    return clean(parameter.get("parameter_name") or parameter.get("name"))


def build_hf_arguments(parameters, prompt, size, seed, negative):
    width, height = size
    arguments = {}
    prompt_found = False

    for parameter in parameters:
        name = hf_parameter_name(parameter)
        if not name:
            continue

        normalized = re.sub(r"[^a-z0-9]", "", name.lower())
        required = (
            not bool(parameter.get("parameter_has_default", True))
            if "parameter_has_default" in parameter
            else bool(parameter.get("required", False))
        )

        if any(token in normalized for token in (
            "prompt", "description", "textprompt", "positiveprompt"
        )):
            arguments[name] = prompt
            prompt_found = True
        elif normalized in {"width", "imagewidth", "outputwidth"}:
            arguments[name] = width
        elif normalized in {"height", "imageheight", "outputheight"}:
            arguments[name] = height
        elif normalized in {"seed", "randomseed"}:
            arguments[name] = seed
        elif normalized in {"negativeprompt", "negative"}:
            arguments[name] = negative
        elif normalized in {"guidancescale", "guidance", "cfgscale"}:
            arguments[name] = 7
        elif normalized in {"steps", "numsteps", "inferencesteps"}:
            arguments[name] = 9
        elif normalized in {"randomize", "randomizeseed"}:
            arguments[name] = False
        elif required:
            raise RuntimeError(f"Unsupported required Hugging Face input: {name}")

    if not prompt_found:
        raise RuntimeError("Hugging Face endpoint has no supported prompt input.")

    return arguments


def extract_hf_image(result, destination, size, space):
    if isinstance(result, (list, tuple)):
        errors = []
        for value in result:
            try:
                extract_hf_image(value, destination, size, space)
                return
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError(
            f"{space}: no usable image in endpoint result: {'; '.join(errors[:3])}"
        )

    if isinstance(result, Image.Image):
        save_image(result, destination, size)
        return

    if isinstance(result, dict):
        result = (
            result.get("path")
            or result.get("filepath")
            or result.get("url")
            or result.get("image")
        )
        if isinstance(result, dict):
            result = result.get("path") or result.get("url")

    if isinstance(result, str):
        local = Path(result)
        if local.is_file():
            with Image.open(local) as image:
                image.load()
                save_image(image, destination, size)
            return

        parsed = urlparse(result)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            response = SESSION.get(result, timeout=TIMEOUT)
            response_image(response, destination, size, space)
            return

    raise RuntimeError(f"{space}: endpoint did not return a usable image.")


def generate_huggingface(prompt, destination, size, seed, space, negative):
    if Client is None:
        raise RuntimeError("gradio_client is not installed.")

    options = {"verbose": False}
    if HF_TOKEN:
        options["hf_token"] = HF_TOKEN

    client = Client(space, **options)
    api = client.view_api(return_format="dict")
    endpoints = api.get("named_endpoints", {})

    if not isinstance(endpoints, dict) or not endpoints:
        raise RuntimeError(f"{space}: no named Gradio endpoint found.")

    preferred = ("/generate_image", "/generate", "/predict", "/run")
    endpoint = next(
        (name for name in preferred if name in endpoints),
        next(iter(endpoints)),
    )

    parameters = endpoints[endpoint].get("parameters", [])
    if not isinstance(parameters, list):
        raise RuntimeError(f"{space}: invalid endpoint parameter schema.")

    arguments = build_hf_arguments(
        parameters, prompt, size, seed, negative
    )
    result = client.predict(api_name=endpoint, **arguments)
    extract_hf_image(result, destination, size, space)


def generate_pollinations(prompt, destination, size, seed, model):
    """
    Use Pollinations' OpenAI-compatible JSON endpoint.

    The legacy GET /image/{prompt} route puts the entire prompt in the URL
    path. Our story-specific prompts can be very long, causing HTTP 400
    responses before image generation starts. POST keeps the full prompt in
    the JSON body and preserves the configured model, dimensions and seed.
    """
    width, height = size
    headers = {"Content-Type": "application/json"}

    if POLLINATIONS_KEY:
        headers["Authorization"] = f"Bearer {POLLINATIONS_KEY}"

    response = SESSION.post(
        "https://gen.pollinations.ai/v1/images/generations",
        json={
            "model": model,
            "prompt": prompt,
            "size": f"{width}x{height}",
            "seed": seed,
            "n": 1,
            "quality": "medium",
            "response_format": "b64_json",
        },
        headers=headers,
        timeout=TIMEOUT,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            detail = clean(error.get("message") or error.get("code"))
        except (ValueError, AttributeError):
            detail = clean(response.text[:1000])
        raise requests.HTTPError(
            f"Pollinations ({model}) HTTP {response.status_code}: "
            f"{detail or 'image generation request failed'}",
            response=response,
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Pollinations ({model}) returned invalid JSON."
        ) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError(
            f"Pollinations ({model}) response has no image data."
        )

    image_data = data[0].get("b64_json")
    image_url = data[0].get("url")

    if image_data:
        try:
            image_bytes = base64.b64decode(image_data, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Pollinations ({model}) returned invalid base64 image data."
            ) from exc

        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise RuntimeError(
                f"Pollinations ({model}) returned an empty or oversized image."
            )
        save_image_bytes(image_bytes, destination, size)
        return

    if image_url:
        image_response = SESSION.get(
            image_url,
            headers=headers,
            timeout=TIMEOUT,
        )
        response_image(
            image_response, destination, size, f"Pollinations ({model})"
        )
        return

    raise RuntimeError(
        f"Pollinations ({model}) response contains neither b64_json nor url."
    )


def generate_replicate(prompt, destination, size, seed):
    if not ALLOW_BILLABLE:
        raise RuntimeError("Replicate image generation blocked by billing permission.")
    if not REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_TOKEN is not configured.")

    owner, separator, model = REPLICATE_MODEL.partition("/")
    if not separator or not owner or not model:
        raise RuntimeError("REPLICATE_IMAGE_MODEL must use owner/model format.")

    width, height = size
    aspect = "9:16" if height > width else "16:9"

    response = SESSION.post(
        f"https://api.replicate.com/v1/models/{owner}/{model}/predictions",
        headers={
            "Authorization": f"Bearer {REPLICATE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={
            "input": {
                "prompt": prompt,
                "aspect_ratio": aspect,
                "output_format": "png",
                "seed": seed,
            }
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    prediction = response.json()
    deadline = time.monotonic() + TIMEOUT

    while prediction.get("status") in {"starting", "processing"}:
        if time.monotonic() >= deadline:
            raise TimeoutError("Replicate prediction timed out.")

        time.sleep(POLL_INTERVAL)
        poll_url = (
            prediction.get("urls", {}).get("get")
            or "https://api.replicate.com/v1/predictions/" + clean(prediction.get("id"))
        )

        if not clean(prediction.get("id")) and not prediction.get("urls", {}).get("get"):
            raise RuntimeError("Replicate did not return a prediction ID.")

        check = SESSION.get(
            poll_url,
            headers={"Authorization": f"Bearer {REPLICATE_KEY}"},
            timeout=min(TIMEOUT, 60),
        )
        check.raise_for_status()
        prediction = check.json()

    if prediction.get("status") != "succeeded":
        raise RuntimeError(
            "Replicate prediction failed: "
            + clean(prediction.get("error") or prediction.get("status"))
        )

    output = prediction.get("output")
    if isinstance(output, str):
        output = [output]
    elif isinstance(output, dict):
        output = list(output.values())

    image_url = next((
        value for value in (output or [])
        if isinstance(value, str)
        and urlparse(value).scheme in {"https", "http"}
    ), None)

    if not image_url:
        raise RuntimeError("Replicate returned no image URL.")

    image_response = SESSION.get(image_url, timeout=TIMEOUT)
    response_image(image_response, destination, size, "Replicate")



def generate_cloudflare(prompt, destination, size, seed, model):
    """Generate with a selected Cloudflare Workers AI image model."""
    if not ALLOW_BILLABLE:
        raise RuntimeError("Cloudflare Workers AI blocked by billing permission.")
    if not CLOUDFLARE_IMAGE_API_TOKEN:
        raise RuntimeError("CLOUDFLARE_IMAGE_API_TOKEN is not configured.")
    if not CLOUDFLARE_ACCOUNT_ID:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID is not configured.")

    model = clean(model)
    if not model:
        raise RuntimeError("Cloudflare image model is empty.")

    cloudflare_prompt = clean(prompt)
    if len(cloudflare_prompt) > 2048:
        cloudflare_prompt = cloudflare_prompt[:2045].rsplit(" ", 1)[0].rstrip()
        cloudflare_prompt += "..."

    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        + quote(CLOUDFLARE_ACCOUNT_ID, safe="")
        + "/ai/run/"
        + quote(model, safe="/@.-")
    )

    if model.endswith("/flux-2-klein-4b"):
        width, height = parse_size(size)
        multipart_fields = [
            ("prompt", (None, cloudflare_prompt)),
            ("width", (None, str(width))),
            ("height", (None, str(height))),
            ("seed", (None, str(seed))),
        ]
        response = SESSION.post(
            endpoint,
            headers={"Authorization": f"Bearer {CLOUDFLARE_IMAGE_API_TOKEN}"},
            files=multipart_fields,
            timeout=TIMEOUT,
        )
    else:
        response = SESSION.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {CLOUDFLARE_IMAGE_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"prompt": cloudflare_prompt, "seed": seed, "steps": 8},
            timeout=TIMEOUT,
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = response.json()
            errors = payload.get("errors", []) if isinstance(payload, dict) else []
            if isinstance(errors, list):
                detail = "; ".join(
                    clean(row.get("message") or row.get("code"))
                    for row in errors if isinstance(row, dict)
                )
        except (ValueError, AttributeError):
            detail = clean(response.text[:1000])
        raise requests.HTTPError(
            f"Cloudflare Workers AI ({model}) HTTP {response.status_code}: "
            f"{detail or 'image generation failed'}",
            response=response,
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Cloudflare Workers AI returned invalid JSON.") from exc

    result = payload.get("result") if isinstance(payload, dict) else None
    image_data = result.get("image") if isinstance(result, dict) else None
    if not image_data:
        raise RuntimeError(
            f"Cloudflare Workers AI ({model}) response contains no result.image data."
        )
    try:
        image_bytes = base64.b64decode(image_data, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Cloudflare Workers AI ({model}) returned invalid base64 image data."
        ) from exc
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"Cloudflare Workers AI ({model}) returned an empty or oversized image."
        )
    save_image_bytes(image_bytes, destination, size)


def build_provider_attempts(prompt, destination, size, seed, negative):
    """Create attempts in the exact configured provider order."""
    attempts = []

    for provider in PROVIDER_ORDER:
        if provider == "huggingface":
            spaces = HF_SPACES if IMAGE_FALLBACK_ENABLED else HF_SPACES[:1]
            for space in spaces:
                attempts.append((
                    f"Hugging Face: {space}",
                    f"huggingface:{space}",
                    False,
                    lambda space=space: generate_huggingface(
                        prompt, destination, size, seed, space, negative
                    ),
                ))

        elif provider == "pollinations":
            models = (
                POLLINATIONS_MODELS
                if IMAGE_FALLBACK_ENABLED
                else POLLINATIONS_MODELS[:1]
            )
            for model in models:
                attempts.append((
                    f"Pollinations: {model}",
                    f"pollinations:{model}",
                    True,
                    lambda model=model: generate_pollinations(
                        prompt, destination, size, seed, model
                    ),
                ))

        elif provider == "replicate":
            if not ALLOW_BILLABLE:
                log("Skipping Replicate: billable permission disabled.")
            elif not REPLICATE_KEY:
                log("Skipping Replicate: REPLICATE_API_TOKEN missing.")
            else:
                attempts.append((
                    f"Replicate: {REPLICATE_MODEL}",
                    f"replicate:{REPLICATE_MODEL}",
                    True,
                    lambda: generate_replicate(
                        prompt, destination, size, seed
                    ),
                ))

        elif provider == "cloudflare_schnell":
            model = CLOUDFLARE_IMAGE_FALLBACK_MODEL
            if not ALLOW_BILLABLE:
                log("Skipping Cloudflare Schnell: billable permission disabled.")
            elif not model or not CLOUDFLARE_IMAGE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
                log("Skipping Cloudflare Schnell: model/token/account ID missing.")
            else:
                attempts.append((
                    f"Cloudflare Workers AI Schnell: {model}",
                    f"cloudflare:{model}",
                    True,
                    lambda model=model: generate_cloudflare(
                        prompt, destination, size, seed, model
                    ),
                ))

        elif provider == "cloudflare_klein":
            model = CLOUDFLARE_IMAGE_MODEL
            if not ALLOW_BILLABLE:
                log("Skipping Cloudflare Klein: billable permission disabled.")
            elif not model or not CLOUDFLARE_IMAGE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
                log("Skipping Cloudflare Klein: model/token/account ID missing.")
            else:
                attempts.append((
                    f"Cloudflare Workers AI Klein: {model}",
                    f"cloudflare:{model}",
                    True,
                    lambda model=model: generate_cloudflare(
                        prompt, destination, size, seed, model
                    ),
                ))

        elif provider == "ovhcloud":
            attempts.append((
                f"OVHcloud SDXL: {OVHCLOUD_IMAGE_MODEL}",
                f"ovhcloud:{OVHCLOUD_IMAGE_MODEL}",
                False,
                lambda: save_image_bytes_fit(
                    adapter_generate_ovhcloud(
                        prompt, size, seed, negative
                    ),
                    destination, size,
                ),
            ))

        elif provider == "aihorde":
            if os.getenv("AIHORDE_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
                log("Skipping AI Horde: explicitly disabled.")
            else:
                attempts.append((
                    f"AI Horde: {AIHORDE_IMAGE_MODEL}",
                    f"aihorde:{AIHORDE_IMAGE_MODEL}",
                    False,
                    lambda: save_image_bytes(
                        adapter_generate_ai_horde(
                            prompt, size, seed, negative
                        ),
                        destination, size,
                    ),
                ))

        elif provider == "cloudflare":
            if not ALLOW_BILLABLE:
                log("Skipping Cloudflare Workers AI: billable permission disabled.")
            elif not CLOUDFLARE_IMAGE_API_TOKEN:
                log("Skipping Cloudflare Workers AI: CLOUDFLARE_IMAGE_API_TOKEN missing.")
            elif not CLOUDFLARE_ACCOUNT_ID:
                log("Skipping Cloudflare Workers AI: CLOUDFLARE_ACCOUNT_ID missing.")
            else:
                cloudflare_models = [CLOUDFLARE_IMAGE_MODEL]
                if (
                    IMAGE_FALLBACK_ENABLED
                    and CLOUDFLARE_IMAGE_FALLBACK_MODEL
                    and CLOUDFLARE_IMAGE_FALLBACK_MODEL not in cloudflare_models
                ):
                    cloudflare_models.append(CLOUDFLARE_IMAGE_FALLBACK_MODEL)

                for model in cloudflare_models:
                    attempts.append((
                        f"Cloudflare Workers AI: {model}",
                        f"cloudflare:{model}",
                        True,
                        lambda model=model: generate_cloudflare(
                            prompt, destination, size, seed, model
                        ),
                    ))

        if not IMAGE_FALLBACK_ENABLED:
            break

    return attempts


def load_previous_records():
    records = {}

    for path in (IMAGE_MANIFEST, GENERATION_MANIFEST):
        document = read_json(path, {})
        rows = document.get("scenes", []) if isinstance(document, dict) else []

        if not isinstance(rows, list):
            continue

        for row in rows:
            if isinstance(row, dict):
                scene_id = clean(row.get("scene_id"))
                if scene_id:
                    records[scene_id] = row

    return records


def can_reuse_image(item, old):
    if not isinstance(old, dict):
        return False

    if clean(old.get("status")).lower() not in {
        "generated", "existing", "completed"
    }:
        return False

    if clean(old.get("scene_id")) != item["scene_id"]:
        return False

    saved_reuse_signature = clean(old.get("reuse_signature"))
    if saved_reuse_signature:
        # Reuse is tied to exact positive/negative prompts, scene identity,
        # output dimensions/path and image configuration—not generator version
        # or unrelated source metadata fingerprints.
        if saved_reuse_signature != image_reuse_signature(item):
            return False
    else:
        # Backward-compatible strict path for manifests not yet upgraded by
        # checkpoint_manager.py.
        if clean(old.get("job_signature")) != job_signature(item):
            return False
        source = item["source_job"]
        for field in (
            "prompt_signature",
            "source_scene_signature",
            "source_fingerprint",
            "config_signature",
        ):
            if clean(old.get(field)) != clean(source.get(field)):
                return False

    old_path = clean(
        old.get("image_path") or old.get("image") or old.get("file")
    )
    if old_path != item["relative_path"]:
        return False

    if (
        clean(old.get("image_size")) != item["size_text"]
        or int(old.get("width") or 0) != item["size"][0]
        or int(old.get("height") or 0) != item["size"][1]
    ):
        return False

    expected_hash = clean(old.get("image_sha256"))
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash):
        return False

    try:
        integrity = image_integrity(item["destination"], item["size"])
    except (OSError, RuntimeError, ValueError):
        return False

    quality = old.get("quality_verification")
    if not isinstance(quality, dict) or quality.get("status") != "passed":
        return False
    vision = quality.get("vision")
    if not isinstance(vision, dict) or vision.get("prompt_match") is not True or vision.get("character_consistency") is not True:
        return False

    return integrity["sha256"].casefold() == expected_hash.casefold()

def make_record(item, status, provider=None, attempts=None):
    integrity = None

    if status in {"generated", "existing"}:
        integrity = image_integrity(item["destination"], item["size"])

    source = item["source_job"]
    character_ids = (
        source.get("character_ids")
        or source.get("character_names")
        or source.get("characters")
        or []
    )
    if isinstance(character_ids, str):
        character_ids = [character_ids]
    elif not isinstance(character_ids, list):
        character_ids = []
    character_references = source.get("character_references") or []
    if not isinstance(character_references, list):
        character_references = []

    return {
        "scene": item["global_scene"],
        "global_scene": item["global_scene"],
        "global_scene_number": item["global_scene"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "source_scene_number": item["source_scene_number"],
        "scene_id": item["scene_id"],
        "role": item["role"],
        "is_extra_scene": item["role"] != "main",
        "file": item["relative_path"],
        "image": item["relative_path"],
        "image_path": item["relative_path"],
        "width": item["size"][0],
        "height": item["size"][1],
        "image_size": item["size_text"],
        "status": status,
        "provider": provider,
        "prompt_sha256": prompt_hash(item),
        "negative_prompt_sha256": negative_prompt_sha256(item),
        "reuse_signature": image_reuse_signature(item),
        "prompt_signature": source.get("prompt_signature"),
        "job_signature": job_signature(item),
        "source_scene_signature": source.get("source_scene_signature"),
        "source_fingerprint": source.get("source_fingerprint"),
        "config_signature": source.get("config_signature"),
        "image_sha256": integrity["sha256"] if integrity else None,
        "file_integrity": integrity["file_integrity"] if integrity else "pending",
        "semantic_story_match": (
            integrity["semantic_story_match"] if integrity else "not_verified"
        ),
        "quality_verification": None,
        "last_successful_model": provider,
        "character_ids": character_ids,
        "character_references": character_references,
        "duration": item["duration"],
        "attempts": list(attempts or []),
    }


def selected_model_notice():
    selection = read_json(SELECTED_MODEL_FILE, {})

    if not isinstance(selection, dict):
        return
    if clean(selection.get("status")).lower() != "selected":
        return

    provider = clean(selection.get("provider")).lower()
    model = clean(selection.get("model"))
    normalized = PROVIDER_ALIASES.get(provider.replace("-", "_"), provider)

    log("Visual selector record:", provider or "unknown", model or "unknown")

    if normalized and normalized not in PROVIDER_ORDER:
        log("Selector provider is not configured and will not be used:", provider)

    log("Actual generation follows Input IMAGE_PROVIDER_ORDER only.")


def _model_progress_signature(metadata):
    return stable_hash({
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "provider_order": PROVIDER_ORDER,
        "huggingface_spaces": HF_SPACES,
        "pollinations_models": POLLINATIONS_MODELS,
        "replicate_model": REPLICATE_MODEL,
        "cloudflare_primary": CLOUDFLARE_IMAGE_MODEL,
        "cloudflare_fallback": CLOUDFLARE_IMAGE_FALLBACK_MODEL,
        "ovhcloud_model": OVHCLOUD_IMAGE_MODEL,
        "aihorde_model": AIHORDE_IMAGE_MODEL,
    })


def load_model_progress(metadata):
    global MODEL_PROGRESS_STATE, BILLABLE_BLOCKED
    signature = _model_progress_signature(metadata)
    saved = read_json(MODEL_PROGRESS_FILE, {})
    if (
        not isinstance(saved, dict)
        or saved.get("progress_signature") != signature
        or saved.get("source_fingerprint") != metadata.get("source_fingerprint")
        or saved.get("config_signature") != metadata.get("config_signature")
    ):
        saved = {}

    MODEL_PROGRESS_STATE = {
        "version": 1,
        "progress_signature": signature,
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "provider_order": list(PROVIDER_ORDER),
        "cursor_key": clean(saved.get("cursor_key")),
        "last_successful_model": saved.get("last_successful_model"),
        "quality_failure_streaks": (
            saved.get("quality_failure_streaks")
            if isinstance(saved.get("quality_failure_streaks"), dict) else {}
        ),
        "skip_until_scene": (
            saved.get("skip_until_scene")
            if isinstance(saved.get("skip_until_scene"), dict) else {}
        ),
        # Authentication, model availability and billing blocks are run-local.
        # Re-evaluate them after a restart because credentials/quotas may have changed.
        "disabled_models": [],
        "billable_blocked": False,
        "billable_blocked_providers": [],
    }
    BILLABLE_BLOCKED = False
    save_model_progress(metadata)


def save_model_progress(metadata):
    if not isinstance(MODEL_PROGRESS_STATE, dict) or not MODEL_PROGRESS_STATE:
        return
    MODEL_PROGRESS_STATE["updated_at"] = int(time.time())
    MODEL_PROGRESS_STATE["source_fingerprint"] = metadata.get("source_fingerprint")
    MODEL_PROGRESS_STATE["config_signature"] = metadata.get("config_signature")
    save_json(MODEL_PROGRESS_FILE, MODEL_PROGRESS_STATE)


def build_policy():
    return {
        "provider_order": list(PROVIDER_ORDER),
        "fallback_enabled": IMAGE_FALLBACK_ENABLED,
        "billable_providers_allowed": ALLOW_BILLABLE,
        "billable_providers_blocked_after_quota_error": BILLABLE_BLOCKED,
        "billable_provider_groups_blocked": sorted(
            MODEL_PROGRESS_STATE.get("billable_blocked_providers", [])
        ),
        "max_complete_rounds": MAX_RETRIES,
        "persistent_model_cursor": True,
        "quality_validation_enabled": IMAGE_QUALITY_VISION_ENABLED,
        "quality_validation_model": IMAGE_QUALITY_VISION_MODEL,
        "quality_minimum_score": IMAGE_QUALITY_MIN_SCORE,
        "quality_failure_limit_per_model": IMAGE_QUALITY_MAX_FAILURES_PER_MODEL,
        "quality_skip_scenes": IMAGE_QUALITY_SKIP_SCENES,
        "model_cursor_key": MODEL_PROGRESS_STATE.get("cursor_key"),
        "huggingface_spaces": (
            list(HF_SPACES) if "huggingface" in PROVIDER_ORDER else []
        ),
        "pollinations_models": (
            list(POLLINATIONS_MODELS) if "pollinations" in PROVIDER_ORDER else []
        ),
        "replicate_model": (
            REPLICATE_MODEL if "replicate" in PROVIDER_ORDER else None
        ),
        "cloudflare_image_model": (
            CLOUDFLARE_IMAGE_MODEL if any(value.startswith("cloudflare") for value in PROVIDER_ORDER) else None
        ),
        "cloudflare_image_fallback_model": (
            CLOUDFLARE_IMAGE_FALLBACK_MODEL
            if any(value.startswith("cloudflare") for value in PROVIDER_ORDER) else None
        ),
        "ovhcloud_image_model": (
            OVHCLOUD_IMAGE_MODEL if "ovhcloud" in PROVIDER_ORDER else None
        ),
        "aihorde_image_model": (
            AIHORDE_IMAGE_MODEL if "aihorde" in PROVIDER_ORDER else None
        ),
        "provider_credentials_logged": False,
    }


def count_status(records):
    ready = sum(
        row.get("status") in {"existing", "generated"}
        for row in records
    )
    failed = sum(row.get("status") == "failed" for row in records)
    pending = sum(row.get("status") == "pending" for row in records)
    return ready, failed, pending


def build_image_manifest(records, metadata, policy):
    ready, failed, pending = count_status(records)
    total = len(records)

    return {
        "version": VERSION,
        "status": "completed" if ready == total and total > 0 else "in_progress",
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "format": metadata.get("format"),
        "image_size": metadata.get("image_size"),
        "actual_scene_counts": metadata.get("actual_scene_counts"),
        "scenes_min": metadata.get("scenes_min"),
        "scenes_max": metadata.get("scenes_max"),
        "total_scenes": total,
        "images_ready": ready,
        "images_required": total,
        "images_failed": failed,
        "images_pending": pending,
        "policy": policy,
        "model_progression": MODEL_PROGRESS_STATE,
        "scenes": [
            {
                key: row.get(key)
                for key in (
                    "scene", "global_scene", "global_scene_number",
                    "part", "local_scene", "source_scene_number",
                    "scene_id", "role", "is_extra_scene",
                    "image", "image_path", "image_sha256",
                    "width", "height", "image_size", "duration",
                    "status", "provider", "prompt_sha256",
                    "prompt_signature", "job_signature",
                    "negative_prompt_sha256", "reuse_signature",
                    "source_scene_signature", "source_fingerprint",
                    "config_signature", "file_integrity",
                    "semantic_story_match", "quality_verification",
                    "last_successful_model",
                    "character_ids", "character_references",
                )
            }
            for row in records
        ],
    }


def build_generation_manifest(records, metadata, policy, counters):
    ready, failed, pending = count_status(records)
    total = len(records)

    return {
        "version": VERSION,
        "status": "completed" if ready == total and total > 0 else "in_progress",
        "stage": "image_generation",
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "format": metadata.get("format"),
        "image_size": metadata.get("image_size"),
        "actual_scene_counts": metadata.get("actual_scene_counts"),
        "scenes_min": metadata.get("scenes_min"),
        "scenes_max": metadata.get("scenes_max"),
        "total_scenes": total,
        "completed_count": ready,
        "failed_count": failed,
        "pending_count": pending,
        "external_api_calls": counters["external_api_calls"],
        "billable_api_calls": counters["billable_api_calls"],
        "policy": policy,
        "summary": {
            "ready": ready,
            "failed": failed,
            "pending": pending,
            "total": total,
        },
        "scenes": records,
    }


def validate_manifest_consistency(records, metadata):
    if not records:
        raise RuntimeError("No image records exist.")

    ids = [clean(row.get("scene_id")) for row in records]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("Image manifest has missing or duplicate scene IDs.")

    for row in records:
        if row.get("status") in {"generated", "existing"}:
            if not re.fullmatch(r"[a-fA-F0-9]{64}", clean(row.get("image_sha256"))):
                raise RuntimeError(
                    f"{row['scene_id']}: ready image has no valid SHA256."
                )
            if row.get("file_integrity") != "verified":
                raise RuntimeError(
                    f"{row['scene_id']}: image integrity is unverified."
                )
            if not clean(row.get("image_path")):
                raise RuntimeError(f"{row['scene_id']}: image path is missing.")

    ready, failed, pending = count_status(records)
    if ready + failed + pending != len(records):
        raise RuntimeError("Image manifest status totals do not match job count.")

    expected_fingerprint = clean(metadata.get("source_fingerprint"))
    expected_config = clean(metadata.get("config_signature"))

    for row in records:
        if (
            expected_fingerprint
            and clean(row.get("source_fingerprint")) != expected_fingerprint
        ):
            raise RuntimeError(f"{row['scene_id']}: source fingerprint mismatch.")
        if (
            expected_config
            and clean(row.get("config_signature")) != expected_config
        ):
            raise RuntimeError(f"{row['scene_id']}: config signature mismatch.")

    if ready == len(records) and (failed or pending):
        raise RuntimeError("Completed image count conflicts with unfinished records.")


def save_checkpoints(records, metadata, policy, counters):
    validate_manifest_consistency(records, metadata)

    generation = build_generation_manifest(
        records, metadata, policy, counters
    )
    images = build_image_manifest(records, metadata, policy)

    save_json(GENERATION_MANIFEST, generation)
    save_json(IMAGE_MANIFEST, images)
    save_model_progress(metadata)


def billable_provider_group(provider_key):
    """Return the account/provider family that shares billing credentials."""
    value = clean(provider_key).casefold()
    return value.split(":", 1)[0] if ":" in value else value


def billable_provider_is_blocked(provider_key, billable, blocked_groups):
    """A billing failure blocks only that provider account, not other vendors."""
    if not billable:
        return False
    group = billable_provider_group(provider_key)
    return bool(group and group in set(blocked_groups or []))


def classify_provider_error(exc):
    """Return whether this failure is quota/billing/auth related."""
    status = None
    response = getattr(exc, "response", None)

    if response is not None:
        status = getattr(response, "status_code", None)

    response_body = ""
    if response is not None:
        response_body = clean(
            getattr(response, "text", "")
            or getattr(response, "content", "")
        )
    message = f"{clean(exc)} {response_body}".lower()

    quota_terms = (
        "quota exceeded", "quota limit", "daily quota", "monthly quota",
        "billing", "payment required", "insufficient credit",
        "insufficient funds", "resource_exhausted", "resource exhausted",
        "exceeded your", "credit balance", "account suspended",
    )

    auth_terms = (
        "unauthorized", "invalid api key", "api key not valid",
        "authentication", "permission denied", "forbidden",
    )

    explicit_quota = any(term in message for term in quota_terms)
    rate_limited = status == 429 and (
        any(term in message for term in (
            "rate limit", "too many requests", "requests per minute",
            "requests per second", "per minute", "per second",
        ))
        or not explicit_quota
    )
    quota_or_billing = status == 402 or (
        explicit_quota and not rate_limited
    ) or (status == 429 and not rate_limited)
    model_unavailable = status == 404
    authentication = status in {401, 403} or any(
        term in message for term in auth_terms
    )

    return {
        "stop_retry": bool(quota_or_billing or authentication or rate_limited or model_unavailable),
        "stop_billable": bool(quota_or_billing or authentication),
        "category": (
            "quota_or_billing" if quota_or_billing
            else "rate_limit" if rate_limited
            else "model_unavailable" if model_unavailable
            else "authentication" if authentication
            else "provider_error"
        ),
        "http_status": status,
    }


def find_character_reference_images(item, records):
    source = item.get("source_job") or {}
    raw_ids = (
        source.get("character_ids")
        or source.get("character_names")
        or source.get("characters")
        or []
    )
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    wanted = {
        clean(value).casefold()
        for value in raw_ids
        if clean(value)
    }
    if not wanted:
        return []

    references = []
    for row in reversed(records):
        if row.get("status") not in {"generated", "existing"}:
            continue
        if clean(row.get("scene_id")) == item["scene_id"]:
            continue
        row_ids = row.get("character_ids") or row.get("characters") or []
        if isinstance(row_ids, str):
            row_ids = [row_ids]
        available = {
            clean(value).casefold()
            for value in row_ids
            if clean(value)
        }
        if not wanted.intersection(available):
            continue
        try:
            candidate = safe_path(
                row.get("image_path") or row.get("image"),
                f"character reference {row.get('scene_id')}",
            )
            expected_hash = clean(row.get("image_sha256"))
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash)
                and sha256_file(candidate).casefold() == expected_hash.casefold()
            ):
                references.append(candidate)
        except (OSError, RuntimeError, ValueError):
            continue
        if len(references) >= 2:
            break
    return references


def run_provider_attempts(
    item,
    record,
    records,
    metadata,
    policy,
    counters,
):
    global BILLABLE_BLOCKED

    prompt = make_prompt(item)
    negative = build_negative_prompt(item)
    seed = deterministic_seed(item)
    destination = item["destination"]

    # A previous run may have generated the image but lost Gemini Vision availability.
    # Re-verify that exact file before spending another image-generation API call.
    candidate_hash = clean(record.get("unverified_candidate_sha256"))
    candidate_prompt_hash = clean(record.get("unverified_candidate_prompt_sha256"))
    candidate_provider = clean(record.get("unverified_candidate_provider"))
    candidate_provider_key = clean(record.get("unverified_candidate_provider_key")) or candidate_provider
    if (
        candidate_hash
        and candidate_prompt_hash == sha256_text(prompt)
        and destination.is_file()
        and not destination.is_symlink()
        and sha256_file(destination).casefold() == candidate_hash.casefold()
    ):
        log("REVERIFYING SAVED UNVERIFIED IMAGE:", item["scene_id"], "| provider:", candidate_provider)
        try:
            candidate_quality = validate_scene_image(
                destination, prompt, item["size"],
                enabled=IMAGE_QUALITY_VISION_ENABLED,
                minimum_score=IMAGE_QUALITY_MIN_SCORE,
                model=IMAGE_QUALITY_VISION_MODEL,
                timeout=min(TIMEOUT, 120),
                reference_image_paths=find_character_reference_images(item, records),
            )
            if candidate_quality.get("accepted"):
                integrity = image_integrity(destination, item["size"])
                record.update(make_record(
                    item, "generated", candidate_provider or "verified previous candidate",
                    record.get("attempts", []),
                ))
                record["quality_verification"] = candidate_quality
                record["semantic_story_match"] = "verified"
                record["last_successful_model"] = candidate_provider_key
                record["image_sha256"] = integrity["sha256"]
                record["file_integrity"] = "verified"
                record["unverified_candidate_sha256"] = None
                record["unverified_candidate_prompt_sha256"] = None
                record["unverified_candidate_provider"] = None
                record["unverified_candidate_provider_key"] = None
                MODEL_PROGRESS_STATE["cursor_key"] = candidate_provider_key or MODEL_PROGRESS_STATE.get("cursor_key")
                MODEL_PROGRESS_STATE["last_successful_model"] = candidate_provider_key or MODEL_PROGRESS_STATE.get("last_successful_model")
                save_checkpoints(records, metadata, policy, counters)
                log("SAVED IMAGE VERIFIED WITHOUT REGENERATION:", item["scene_id"])
                return
            log("Saved candidate failed vision quality; generating a replacement:", item["scene_id"])
            destination.unlink(missing_ok=True)
        except VisionVerificationUnavailable:
            record["status"] = "pending"
            save_checkpoints(records, metadata, policy, counters)
            raise
        except Exception as candidate_error:
            log("Saved candidate is invalid; replacing:", item["scene_id"], type(candidate_error).__name__)
            destination.unlink(missing_ok=True)
        record["unverified_candidate_sha256"] = None
        record["unverified_candidate_prompt_sha256"] = None
        record["unverified_candidate_provider"] = None
        record["unverified_candidate_provider_key"] = None

    attempts = build_provider_attempts(
        prompt, destination, item["size"], seed, negative
    )

    if not attempts:
        record["status"] = "failed"
        record["error"] = (
            "No configured image provider is usable. "
            "Check credentials, model lists and billing permission."
        )
        return

    attempt_count = len(attempts)
    provider_keys = [row[1] for row in attempts]
    cursor_key = clean(MODEL_PROGRESS_STATE.get("cursor_key"))
    cursor_index = provider_keys.index(cursor_key) if cursor_key in provider_keys else 0
    disabled_models = set(
        clean(value) for value in MODEL_PROGRESS_STATE.get("disabled_models", [])
    )
    quality_failed_this_scene = set()
    retry_not_before = {}
    last_error = "All configured image models failed generation or quality validation."
    succeeded = False

    for round_number in range(1, MAX_RETRIES + 1):
        if succeeded:
            break
        round_start_index = cursor_index
        log(
            "IMAGE FALLBACK ROUND:", round_number, "of", MAX_RETRIES,
            "| starting model:", attempts[round_start_index][0],
        )

        for offset in range(attempt_count):
            index = (round_start_index + offset) % attempt_count
            provider_name, provider_key, billable, generate = attempts[index]

            if provider_key in disabled_models:
                log("Skipping persistently unavailable model:", provider_name)
                continue
            if provider_key in quality_failed_this_scene:
                log("Skipping this model for this Scene after quality rejection:", provider_name)
                continue
            blocked_groups = MODEL_PROGRESS_STATE.get(
                "billable_blocked_providers", []
            )
            if billable_provider_is_blocked(
                provider_key, billable, blocked_groups
            ):
                log(
                    "Skipping provider after its own billing/authentication block:",
                    provider_name,
                    "| account group:",
                    billable_provider_group(provider_key),
                )
                continue

            skip_until = MODEL_PROGRESS_STATE.get("skip_until_scene", {}).get(provider_key)
            if isinstance(skip_until, int) and item["global_scene"] < skip_until:
                log(
                    "Temporarily skipping model after repeated quality failures:",
                    provider_name, "| until Scene", skip_until,
                )
                continue
            elif skip_until is not None:
                MODEL_PROGRESS_STATE["skip_until_scene"].pop(provider_key, None)

            not_before = retry_not_before.get(provider_key, 0)
            remaining_wait = not_before - time.time()
            if remaining_wait > 0:
                log(
                    "Waiting for provider cooldown:", provider_name,
                    "| seconds:", round(remaining_wait),
                )
                time.sleep(remaining_wait)

            cursor_index = index
            MODEL_PROGRESS_STATE["cursor_key"] = provider_key
            save_model_progress(metadata)

            try:
                log(
                    "Generating:", item["scene_id"],
                    "| Provider:", provider_name,
                    "| Round:", round_number, "of", MAX_RETRIES,
                    "| Cursor:", provider_key,
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                record["status"] = "pending"
                record["current_provider"] = provider_name
                record["current_round"] = round_number
                record["current_model_index"] = index
                save_checkpoints(records, metadata, build_policy(), counters)

                counters["external_api_calls"] += 1
                if billable:
                    counters["billable_api_calls"] += 1
                generate()

                quality = validate_scene_image(
                    destination,
                    prompt,
                    item["size"],
                    enabled=IMAGE_QUALITY_VISION_ENABLED,
                    minimum_score=IMAGE_QUALITY_MIN_SCORE,
                    model=IMAGE_QUALITY_VISION_MODEL,
                    timeout=min(TIMEOUT, 120),
                    reference_image_paths=find_character_reference_images(item, records),
                )
                if not quality.get("accepted"):
                    raise ImageQualityRejected(
                        json.dumps(quality, ensure_ascii=False)[:1200]
                    )

                integrity = image_integrity(destination, item["size"])
                success_attempts = list(record.get("attempts", []))
                success_attempts.append({
                    "provider": provider_name,
                    "provider_key": provider_key,
                    "billable": billable,
                    "attempt": len(success_attempts) + 1,
                    "round": round_number,
                    "status": "success",
                    "image_sha256": integrity["sha256"],
                    "quality_score": quality["vision"]["quality_score"],
                    "timestamp": int(time.time()),
                })
                record.update(
                    make_record(item, "generated", provider_name, success_attempts)
                )
                record["quality_verification"] = quality
                record["semantic_story_match"] = "verified"
                record["last_successful_model"] = provider_key
                record.pop("current_provider", None)
                record.pop("current_round", None)
                record.pop("current_model_index", None)
                record.pop("last_provider_error", None)
                record.pop("quality_verification_error", None)
                record.pop("error", None)

                MODEL_PROGRESS_STATE["cursor_key"] = provider_key
                MODEL_PROGRESS_STATE["last_successful_model"] = provider_key
                MODEL_PROGRESS_STATE["quality_failure_streaks"][provider_key] = 0
                MODEL_PROGRESS_STATE["skip_until_scene"].pop(provider_key, None)
                succeeded = True
                save_checkpoints(records, metadata, build_policy(), counters)
                log(
                    "IMAGE ACCEPTED:", item["scene_id"],
                    "| model:", provider_name,
                    "| vision score:", quality["vision"]["quality_score"],
                    "| next Scene starts with this model.",
                )
                break

            except VisionVerificationUnavailable as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                record["status"] = "pending"
                record["quality_verification_error"] = last_error[:1000]
                # Keep the generated file, hash and prompt signature. A later run can
                # re-verify it without paying for another image generation.
                if destination.is_file() and not destination.is_symlink():
                    try:
                        candidate_integrity = image_integrity(destination, item["size"])
                        record["unverified_candidate_sha256"] = candidate_integrity["sha256"]
                        record["unverified_candidate_prompt_sha256"] = sha256_text(prompt)
                        record["unverified_candidate_provider"] = provider_name
                        record["unverified_candidate_provider_key"] = provider_key
                        record["unverified_candidate_path"] = item["relative_path"]
                    except Exception:
                        destination.unlink(missing_ok=True)
                record.pop("current_provider", None)
                record.pop("current_round", None)
                record.pop("current_model_index", None)
                save_checkpoints(records, metadata, build_policy(), counters)
                raise

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, ImageQualityRejected):
                    category = "quality_failure"
                    classification = {
                        "category": category,
                        "stop_billable": False,
                        "http_status": None,
                    }
                    quality_failed_this_scene.add(provider_key)
                    streaks = MODEL_PROGRESS_STATE["quality_failure_streaks"]
                    streaks[provider_key] = int(streaks.get(provider_key, 0)) + 1
                    if streaks[provider_key] >= IMAGE_QUALITY_MAX_FAILURES_PER_MODEL:
                        MODEL_PROGRESS_STATE["skip_until_scene"][provider_key] = (
                            item["global_scene"] + IMAGE_QUALITY_SKIP_SCENES + 1
                        )
                else:
                    classification = classify_provider_error(exc)
                    category = classification["category"]

                if destination.is_file():
                    destination.unlink(missing_ok=True)

                record.setdefault("attempts", []).append({
                    "provider": provider_name,
                    "provider_key": provider_key,
                    "billable": billable,
                    "attempt": len(record.get("attempts", [])) + 1,
                    "round": round_number,
                    "status": "failed",
                    "error": last_error[:1000],
                    "error_category": category,
                    "http_status": classification.get("http_status"),
                    "timestamp": int(time.time()),
                })
                record["status"] = "pending"
                record["last_provider_error"] = last_error[:1000]
                log(
                    "Model failed:", provider_name,
                    "| category:", category,
                    "| round:", round_number,
                    "|", last_error[:350],
                )

                if billable and classification.get("stop_billable"):
                    group = billable_provider_group(provider_key)
                    blocked_groups = set(
                        MODEL_PROGRESS_STATE.get("billable_blocked_providers", [])
                    )
                    if group:
                        blocked_groups.add(group)
                    MODEL_PROGRESS_STATE["billable_blocked_providers"] = sorted(
                        blocked_groups
                    )
                    BILLABLE_BLOCKED = bool(blocked_groups)
                    MODEL_PROGRESS_STATE["billable_blocked"] = BILLABLE_BLOCKED
                    log(
                        "Billing/authentication block recorded for provider account:",
                        group,
                        "| other providers remain eligible.",
                    )
                if category in {
                    "model_unavailable", "authentication", "quota_or_billing"
                }:
                    disabled_models.add(provider_key)
                    MODEL_PROGRESS_STATE["disabled_models"] = sorted(disabled_models)
                if category == "rate_limit":
                    delay = retry_after_seconds(
                        exc, round_number, default_base=12, maximum=120
                    )
                    retry_not_before[provider_key] = time.time() + delay
                    log(
                        "Rate-limit cooldown recorded:", provider_name,
                        "| seconds:", delay,
                    )

                cursor_index = (index + 1) % attempt_count
                MODEL_PROGRESS_STATE["cursor_key"] = attempts[cursor_index][1]
                save_checkpoints(records, metadata, build_policy(), counters)

        if not succeeded:
            cursor_key = clean(MODEL_PROGRESS_STATE.get("cursor_key"))
            cursor_index = (
                provider_keys.index(cursor_key)
                if cursor_key in provider_keys else cursor_index
            )

    if not succeeded:
        record["status"] = "failed"
        record["error"] = last_error[:1200]
        record.pop("current_provider", None)
        record.pop("current_round", None)
        record.pop("current_model_index", None)
        save_checkpoints(records, metadata, build_policy(), counters)
        log(
            "IMAGE FAILED:", item["scene_id"],
            "| complete rounds exhausted:", MAX_RETRIES,
            "| next Scene cursor:", MODEL_PROGRESS_STATE.get("cursor_key"),
        )


def validate_output_format(format_name, metadata, items):
    if format_name not in ALLOWED_FORMATS:
        raise RuntimeError(
            f"Visual jobs manifest has invalid format: {format_name!r}"
        )

    expected_size = "720x1280" if format_name == "short" else "1280x720"

    if clean(metadata.get("image_size")) != expected_size:
        raise RuntimeError(
            f"{format_name.upper()} must use {expected_size}; "
            f"manifest has {metadata.get('image_size')!r}."
        )

    for item in items:
        if item["size_text"] != expected_size:
            raise RuntimeError(
                f"{item['scene_id']}: image size does not match {format_name.upper()}."
            )

    return expected_size


def main():
    log("=" * 64)
    log("KATHA LOK AI — PHOTO MOTION IMAGE GENERATION")
    log("Version:", VERSION)
    log("=" * 64)

    config = parse_input_settings()
    apply_input_settings(config)
    selected_model_notice()

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    items, metadata = get_jobs()
    format_name = clean(metadata.get("format")).lower()

    expected_size = validate_output_format(format_name, metadata, items)

    expected_fingerprint = clean(metadata.get("source_fingerprint"))
    expected_config = clean(metadata.get("config_signature"))

    if not expected_fingerprint or not expected_config:
        raise RuntimeError(
            "Visual jobs manifest is missing source/config fingerprints."
        )

    load_model_progress(metadata)
    previous = load_previous_records()
    records = []

    for item in items:
        old = previous.get(item["scene_id"], {})

        if can_reuse_image(item, old):
            record = make_record(
                item,
                "existing",
                old.get("provider") or "verified previous run",
                old.get("attempts", []),
            )
            record["quality_verification"] = old.get("quality_verification")
            record["semantic_story_match"] = "verified"
            record["last_successful_model"] = (
                old.get("last_successful_model") or old.get("provider")
            )
        else:
            record = make_record(item, "pending")
            previous_attempts = old.get("attempts", []) if isinstance(old, dict) else []
            if isinstance(previous_attempts, list) and previous_attempts:
                record["attempts"] = list(previous_attempts)
            # Carry only hash-bound unverified candidates across runs for re-verification.
            for candidate_key in (
                "unverified_candidate_sha256",
                "unverified_candidate_prompt_sha256",
                "unverified_candidate_provider",
                "unverified_candidate_provider_key",
                "unverified_candidate_path",
            ):
                candidate_value = old.get(candidate_key) if isinstance(old, dict) else None
                if candidate_value:
                    record[candidate_key] = candidate_value

        records.append(record)

    policy = build_policy()
    counters = {
        "external_api_calls": 0,
        "billable_api_calls": 0,
    }

    save_checkpoints(records, metadata, policy, counters)

    ready_before = sum(row["status"] == "existing" for row in records)
    invalid_existing_files = sum(
        bool(item["destination"].exists()) and row["status"] == "pending"
        for item, row in zip(items, records)
    )
    pending_items = [
        (item, record)
        for item, record in zip(items, records)
        if record["status"] == "pending"
    ]

    log("-" * 64)
    log("FORMAT:", format_name)
    log("IMAGE DIMENSIONS:", expected_size)
    log("TOTAL IMAGE JOBS:", len(items))
    log("VERIFIED REUSABLE IMAGES:", ready_before)
    log("EXISTING INVALID/MISMATCHED IMAGE FILES TO REPLACE:", invalid_existing_files)
    log("IMAGES REQUIRING GENERATION:", len(pending_items))
    log("CONFIGURED PROVIDER ORDER:", " -> ".join(PROVIDER_ORDER))
    log("BILLABLE PROVIDERS ALLOWED:", ALLOW_BILLABLE)
    log("FALLBACK ENABLED:", IMAGE_FALLBACK_ENABLED)
    log("PERSISTENT MODEL CURSOR:", MODEL_PROGRESS_STATE.get("cursor_key") or "first configured model")
    log("QUALITY VALIDATION:", "Gemini vision enabled" if IMAGE_QUALITY_VISION_ENABLED else "disabled")
    log("-" * 64)

    for item, record in pending_items:
        run_provider_attempts(
            item,
            record,
            records,
            metadata,
            policy,
            counters,
        )
        save_checkpoints(records, metadata, build_policy(), counters)

    save_checkpoints(records, metadata, build_policy(), counters)

    ready, failed, pending = count_status(records)
    failed_ids = [
        row["scene_id"] for row in records
        if row["status"] == "failed"
    ]

    log("=" * 64)
    log("IMAGE GENERATION SUMMARY")
    log("IMAGES READY:", f"{ready}/{len(records)}")
    log("FAILED:", failed)
    log("PENDING:", pending)
    log("VERIFIED REUSED:", ready_before)
    log("EXTERNAL PROVIDER CALLS:", counters["external_api_calls"])
    log("BILLABLE PROVIDER CALLS:", counters["billable_api_calls"])
    log("BILLABLE PROVIDERS BLOCKED:", BILLABLE_BLOCKED)
    log("IMAGE MANIFEST:", IMAGE_MANIFEST.relative_to(ROOT))
    log("GENERATION MANIFEST:", GENERATION_MANIFEST.relative_to(ROOT))
    log("=" * 64)

    if failed or pending or ready != len(records):
        raise RuntimeError(
            "Image generation is incomplete. Failed scene IDs: "
            + (", ".join(failed_ids) if failed_ids else "none")
            + ". Successful image checkpoints have been preserved."
        )

    log(
        "All image files passed format, dimensions, integrity and SHA256 checks."
    )
    log("All accepted images passed Gemini vision prompt, character and quality checks.")

    return 0




# _PHOTO_MOTION_EVENT_WRAPPED: preserve original stage behavior and record factual outcomes.
_photo_motion_original_main = main
def main():
    import time as _pm_time
    _pm_started = _pm_time.monotonic()
    try:
        try:
            from production_events import record_event as _pm_record
        except ImportError:
            from scripts.production_events import record_event as _pm_record
        _pm_record("free_image_generator", "running")
    except Exception:
        _pm_record = None
    try:
        _pm_result = _photo_motion_original_main()
        if _pm_record:
            _pm_record("free_image_generator", "completed" if _pm_result in (None, 0) else "failed", metadata={"return_code": _pm_result}, started_at=_pm_started)
        return _pm_result
    except BaseException as _pm_exc:
        if _pm_record:
            _pm_record("free_image_generator", "failed", error=f"{type(_pm_exc).__name__}: {_pm_exc}", started_at=_pm_started)
        raise

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("IMAGE GENERATION CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("IMAGE GENERATION FAILED:", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)