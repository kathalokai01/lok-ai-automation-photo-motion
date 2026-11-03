#!/usr/bin/env python3
"""Configured-provider image generation with strict validation and resume."""

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
from urllib.parse import quote, urlparse

import requests
from PIL import Image, ImageOps

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
INPUT_FILE = ROOT / "Input" / "topic.txt"
SELECTED_MODEL_FILE = ROOT / "output" / "config" / "selected_visual_model.json"

VERSION = "photo-motion-image-generator-v26-configured-order-verified-resume"
MIN_IMAGE_BYTES = 1024
MIN_DIMENSION = 256
MAX_DIMENSION = 4096
BASE_SEED = 20261009
ALLOWED_ROLES = {"hook", "main", "suspense", "resolution"}
ALLOWED_FORMATS = {"short", "full"}

TIMEOUT = max(30, min(900, int(os.getenv("IMAGE_PROVIDER_TIMEOUT", "180"))))
POLL_INTERVAL = 2

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "KathaLokAI-PhotoMotion/26.0",
})

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
REPLICATE_KEY = os.getenv("REPLICATE_API_TOKEN", "").strip()
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()

PROVIDER_ORDER = []
HF_SPACES = []
POLLINATIONS_MODELS = []
GEMINI_MODEL = ""
REPLICATE_MODEL = ""
ALLOW_BILLABLE = False
IMAGE_FALLBACK_ENABLED = True
MAX_RETRIES = 1

PROVIDER_ALIASES = {
    "hf": "huggingface",
    "hf_space": "huggingface",
    "hugging_face": "huggingface",
    "huggingface": "huggingface",
    "pollinations": "pollinations",
    "gemini": "gemini",
    "gemini_image": "gemini",
    "replicate": "replicate",
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
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def stable_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256_text(raw)


def sha256_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


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
    return load_and_validate(INPUT_FILE)


def apply_input_settings(config):
    global PROVIDER_ORDER, HF_SPACES, POLLINATIONS_MODELS
    global GEMINI_MODEL, REPLICATE_MODEL
    global ALLOW_BILLABLE, IMAGE_FALLBACK_ENABLED, MAX_RETRIES

    from input_config import (
        get_image_provider_settings,
        get_max_retries,
    )

    settings = get_image_provider_settings(config)

    # Preserve the exact configured order; never sort or promote a provider.
    PROVIDER_ORDER = list(settings["IMAGE_PROVIDER_ORDER"])
    HF_SPACES = list(settings["FREE_IMAGE_FALLBACK_SPACES"])
    if settings["HF_IMAGE_SPACE"]:
        HF_SPACES.insert(0, settings["HF_IMAGE_SPACE"])

    POLLINATIONS_MODELS = list(
        settings["POLLINATIONS_FREE_IMAGE_MODELS"]
    )
    GEMINI_MODEL = clean(settings["GEMINI_IMAGE_MODEL"])
    REPLICATE_MODEL = clean(settings["REPLICATE_IMAGE_MODEL"])

    ALLOW_BILLABLE = bool(settings["IMAGE_ALLOW_BILLABLE"])
    IMAGE_FALLBACK_ENABLED = bool(settings["IMAGE_FALLBACK_ENABLED"])
    MAX_RETRIES = max(1, get_max_retries(config))

    if not PROVIDER_ORDER:
        raise RuntimeError("IMAGE_PROVIDER_ORDER is empty.")

    for provider in PROVIDER_ORDER:
        if provider not in {
            "huggingface", "pollinations", "gemini", "replicate"
        }:
            raise RuntimeError(
                f"Unsupported configured image provider: {provider}"
            )

    if "huggingface" in PROVIDER_ORDER and not HF_SPACES:
        raise RuntimeError("Hugging Face is configured but no Space is configured.")
    if "pollinations" in PROVIDER_ORDER and not POLLINATIONS_MODELS:
        raise RuntimeError("Pollinations is configured but its model list is empty.")
    if "gemini" in PROVIDER_ORDER and not GEMINI_MODEL:
        raise RuntimeError("GEMINI_IMAGE_MODEL is empty.")
    if "replicate" in PROVIDER_ORDER and not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        REPLICATE_MODEL,
    ):
        raise RuntimeError("REPLICATE_IMAGE_MODEL must use owner/model format.")

    log("Configured image provider order:", " -> ".join(PROVIDER_ORDER))
    log("Billable image providers allowed:", ALLOW_BILLABLE)
    log("Provider fallback enabled:", IMAGE_FALLBACK_ENABLED)


def safe_path(value, label):
    raw = clean(value)
    if not raw:
        raise RuntimeError(f"{label}: output path is missing.")

    path = Path(raw)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (ROOT / path).resolve()

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
        raise RuntimeError(f"Invalid image dimensions: {width}x{height}.")
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

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("visual_jobs.json contains no image jobs.")

    if jobs_manifest and jobs_manifest.get("status") not in {
        "completed", "ready"
    }:
        raise RuntimeError("Visual jobs manifest is not completed.")

    prepared = []
    seen_global, seen_slots, seen_ids, seen_paths = set(), set(), set(), set()

    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Image job #{index} is not an object.")

        try:
            part = int(job["part"])
            global_number = int(
                job.get("global_scene", job.get("scene_index", index))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Image job #{index} has invalid numbering.") from exc

        role = clean(job.get("role") or job.get("scene_type") or "main").lower()
        if role not in ALLOWED_ROLES:
            raise RuntimeError(f"Unsupported role in image job #{index}: {role!r}.")

        scene_id = clean(
            job.get("scene_id") or job.get("id") or job.get("source_scene_id")
        )
        if not scene_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
            raise RuntimeError(f"Image job #{index} has an invalid scene ID.")
        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate image scene ID: {scene_id}.")

        if part < 1 or global_number < 1:
            raise RuntimeError(f"{scene_id}: part/global numbering must be positive.")
        if global_number in seen_global:
            raise RuntimeError(f"Duplicate global scene number: {global_number}.")

        if role == "main":
            raw_local = job.get("source_scene_number")
            if raw_local in (None, ""):
                raw_local = job.get("source_local_scene")
            if raw_local in (None, ""):
                raw_local = job.get("scene")
            try:
                source_scene_number = int(raw_local)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{scene_id}: main scene requires source scene number."
                ) from exc
            if source_scene_number < 1:
                raise RuntimeError(f"{scene_id}: invalid main scene number.")
        else:
            source_scene_number = None

        try:
            local_slot = int(job.get("local_scene", job.get("scene", index)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{scene_id}: invalid local image slot.") from exc
        if local_slot < 1 or (part, local_slot) in seen_slots:
            raise RuntimeError(f"{scene_id}: duplicate or invalid local image slot.")

        size_value = clean(job.get("image_size") or jobs_manifest.get("image_size"))
        if not size_value:
            format_name = clean(jobs_manifest.get("format")).lower()
            if format_name == "short":
                size_value = "720x1280"
            elif format_name == "full":
                size_value = "1280x720"
            else:
                raise RuntimeError(
                    f"{scene_id}: image dimensions and format are missing."
                )
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
        if len(prompt) < 24:
            raise RuntimeError(f"{scene_id}: visual prompt is missing or too short.")
        if len(narration) < 8:
            raise RuntimeError(f"{scene_id}: source narration is missing or too short.")

        destination = safe_path(
            job.get("output_path") or job.get("image_path"),
            scene_id,
        )
        relative_path = destination.relative_to(ROOT).as_posix()
        if relative_path in seen_paths:
            raise RuntimeError(f"Duplicate image destination: {relative_path}.")
        if not relative_path.startswith("output/visuals/images/"):
            raise RuntimeError(
                f"{scene_id}: image destination must be under output/visuals/images/."
            )

        source_signature = clean(job.get("source_scene_signature"))
        prompt_signature = clean(job.get("prompt_signature"))
        config_signature = clean(job.get("config_signature"))
        source_fingerprint = clean(job.get("source_fingerprint"))

        if not all((
            source_signature,
            prompt_signature,
            config_signature,
            source_fingerprint,
        )):
            raise RuntimeError(
                f"{scene_id}: missing source, prompt, config or story fingerprint."
            )

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
            "negative_prompt": clean(job.get("negative_prompt")),
            "size": size,
            "size_text": f"{size[0]}x{size[1]}",
            "destination": destination,
            "relative_path": relative_path,
            "duration": job.get("scene_duration", job.get("duration")),
            "source_job": job,
        })

    prepared.sort(key=lambda row: row["global_scene"])
    global_numbers = [row["global_scene"] for row in prepared]
    if global_numbers != list(range(1, len(prepared) + 1)):
        raise RuntimeError("Global image job numbering must be continuous from 1.")

    if jobs_manifest:
        expected_count = jobs_manifest.get("total_jobs")
        if expected_count is not None and int(expected_count) != len(prepared):
            raise RuntimeError("Job count differs from visual jobs manifest.")

        expected_fingerprint = clean(jobs_manifest.get("source_fingerprint"))
        if expected_fingerprint and any(
            row["source_job"].get("source_fingerprint") != expected_fingerprint
            for row in prepared
        ):
            raise RuntimeError("Job source fingerprints do not match manifest.")

    return prepared, jobs_manifest


def make_prompt(item):
    source = item["source_job"]
    sections = [
        item["prompt"],
        "Photographic quality: photorealistic live-action cinematic still.",
        (
            "Depict the exact story event in the visual prompt. Preserve "
            "all explicit names, people, locations, objects, animals, "
            "vehicles, clothing, weather, time period, relationships and "
            "actions. Do not replace specific story details with generic "
            "imagery, omit required story nouns or invent events."
        ),
        (
            "Use the narration only as story context. It must not appear "
            "as visible text, subtitles, labels or typography in the image."
        ),
        (
            "Compose a single still image for later zoom, pan and optional "
            "2.5D parallax. Keep faces, people and important objects within "
            "safe margins. Do not depict body, face or character animation."
        ),
        (
            "Use natural human anatomy, realistic skin texture, believable "
            "materials, physically plausible lighting and coherent "
            "perspective. No cartoon, anime, illustration, CGI, logo or watermark."
        ),
    ]

    for key, label in (
        ("location", "Exact story location"),
        ("world_context", "Story-world continuity"),
        ("time_period", "Story time period"),
        ("action", "Required visible action"),
        ("objects", "Story-specific objects"),
        ("clothing", "Story-specific clothing"),
        ("camera_prompt", "Camera composition"),
        ("lighting_prompt", "Lighting"),
        ("mood", "Emotional mood"),
    ):
        value = source.get(key)
        if isinstance(value, list):
            value = ", ".join(clean(item) for item in value if clean(item))
        value = clean(value)
        if value:
            sections.append(f"{label}: {value}")

    character_records = source.get("character_references")
    if isinstance(character_records, list) and character_records:
        sections.append(
            "CHARACTER IDENTITY LOCK — preserve the supplied identity "
            "and continuity. Do not swap, merge or redesign characters: "
            + json.dumps(
                character_records,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        character_names = source.get("character_names") or source.get("characters")
        if character_names:
            sections.append(
                "Required story characters: "
                + ", ".join(split_values(character_names))
                + ". Preserve their established identity."
            )

    if item["role"] == "hook":
        sections.append(
            "HOOK FRAME: depict the actual opening hook event; create "
            "curiosity without revealing the final resolution."
        )
    elif item["role"] == "suspense":
        sections.append(
            "SUSPENSE FRAME: depict the actual unresolved tension or "
            "danger without showing the final resolution."
        )
    elif item["role"] == "resolution":
        sections.append(
            "FINAL RESOLUTION FRAME: depict the actual story ending and "
            "its consequence. Do not invent an alternate ending."
        )

    return "\n\n".join(sections)


def prompt_hash(item):
    return sha256_text(make_prompt(item))


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


def build_negative_prompt(item):
    values = [
        "wrong story event", "unrelated characters", "unrelated props",
        "wrong location", "wrong time period", "distorted face",
        "unrealistic anatomy", "extra fingers", "missing fingers",
        "fused limbs", "duplicated people", "plastic skin", "waxy face",
        "floating objects", "captions", "subtitles", "unintended text",
        "logo", "watermark", "low resolution", "blurry main subject",
        "cartoon", "anime", "comic-book illustration", "2D drawing",
        "3D animated character",
    ]
    if item["negative_prompt"]:
        values.append(item["negative_prompt"])
    return ", ".join(dict.fromkeys(values))


def valid_image(path, size):
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
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
        raise RuntimeError(f"Image failed format/dimension/integrity check: {path}")
    with Image.open(path) as image:
        image_format = image.format
        width, height = image.size
        image.load()
        extrema = image.convert("RGB").getextrema()
    if all(low == high for low, high in extrema):
        raise RuntimeError(f"Generated image is a single flat colour: {path}")
    return {
        "format": image_format,
        "width": width,
        "height": height,
        "size_bytes": Path(path).stat().st_size,
        "sha256": sha256_file(path),
        "file_integrity": "verified",
        "semantic_story_match": "not_claimed_without_vision_verification",
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
        image.save(temporary, "PNG", optimize=True)
        image_integrity(temporary, size)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def save_image_bytes(raw, destination, size):
    if not raw:
        raise RuntimeError("Provider returned empty image data.")
    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_image(image, destination, size)


def response_image(response, destination, size, provider):
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not content_type.startswith("image/"):
        raise RuntimeError(
            f"{provider} returned non-image content ({content_type or 'unknown'})."
        )
    save_image_bytes(response.content, destination, size)


def deterministic_seed(item):
    raw = f"{BASE_SEED}:{item['scene_id']}:{item['global_scene']}"
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8], 16)


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
            raise RuntimeError(f"Unsupported required Hugging Face input: {name}.")

    if not prompt_found:
        raise RuntimeError("Hugging Face endpoint has no supported prompt input.")
    return arguments


def extract_hf_image(result, destination, size, space):
    if isinstance(result, (list, tuple)):
        if not result:
            raise RuntimeError(f"{space}: empty endpoint result.")
        for item in result:
            try:
                extract_hf_image(item, destination, size, space)
                return
            except Exception:
                continue
        raise RuntimeError(f"{space}: no usable image in endpoint result.")

    if isinstance(result, Image.Image):
        save_image(result, destination, size)
        return

    if isinstance(result, dict):
        candidate = (
            result.get("path")
            or result.get("filepath")
            or result.get("url")
            or result.get("image")
        )
        if isinstance(candidate, dict):
            candidate = candidate.get("path") or candidate.get("url")
        result = candidate

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

    arguments = build_hf_arguments(parameters, prompt, size, seed, negative)
    result = client.predict(api_name=endpoint, **arguments)
    extract_hf_image(result, destination, size, space)


def generate_pollinations(prompt, destination, size, seed, model):
    width, height = size
    url = "https://gen.pollinations.ai/image/" + quote(prompt, safe="")
    headers = {}
    if POLLINATIONS_KEY:
        headers["Authorization"] = f"Bearer {POLLINATIONS_KEY}"

    response = SESSION.get(
        url,
        params={
            "model": model,
            "width": width,
            "height": height,
            "seed": seed,
            "nologo": "true",
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    response_image(response, destination, size, f"Pollinations ({model})")


def generate_gemini(prompt, destination, size):
    if not ALLOW_BILLABLE:
        raise RuntimeError("Gemini image generation blocked by billable permission.")
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    width, height = size
    aspect = "9:16" if height > width else "16:9"
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + quote(GEMINI_MODEL, safe="")
        + ":generateContent"
    )
    response = SESSION.post(
        endpoint,
        headers={
            "x-goog-api-key": GEMINI_KEY,
            "Content-Type": "application/json",
        },
        json={
            "contents": [{
                "parts": [{
                    "text": (
                        prompt
                        + f"\nGenerate one image with {aspect} composition."
                    )
                }]
            }],
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"]
            },
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                raw = base64.b64decode(inline["data"], validate=True)
                save_image_bytes(raw, destination, size)
                return

    raise RuntimeError("Gemini response contains no image data.")


def generate_replicate(prompt, destination, size, seed):
    if not ALLOW_BILLABLE:
        raise RuntimeError("Replicate image generation blocked by billable permission.")
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
            or (
                "https://api.replicate.com/v1/predictions/"
                + clean(prediction.get("id"))
            )
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


def build_provider_attempts(prompt, destination, size, seed, negative):
    """Construct attempts in configured order without adding providers."""
    attempts = []

    for provider in PROVIDER_ORDER:
        if provider == "huggingface":
            models = HF_SPACES if IMAGE_FALLBACK_ENABLED else HF_SPACES[:1]
            for space in models:
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
                    False,
                    lambda model=model: generate_pollinations(
                        prompt, destination, size, seed, model
                    ),
                ))

        elif provider == "gemini":
            if not ALLOW_BILLABLE:
                log("Skipping configured Gemini provider: billable permission disabled.")
            elif not GEMINI_KEY:
                log("Skipping configured Gemini provider: GEMINI_API_KEY missing.")
            else:
                attempts.append((
                    f"Gemini Image: {GEMINI_MODEL}",
                    "gemini:" + GEMINI_MODEL,
                    True,
                    lambda: generate_gemini(prompt, destination, size),
                ))

        elif provider == "replicate":
            if not ALLOW_BILLABLE:
                log("Skipping configured Replicate provider: billable permission disabled.")
            elif not REPLICATE_KEY:
                log("Skipping configured Replicate provider: REPLICATE_API_TOKEN missing.")
            else:
                attempts.append((
                    f"Replicate: {REPLICATE_MODEL}",
                    "replicate:" + REPLICATE_MODEL,
                    True,
                    lambda: generate_replicate(
                        prompt, destination, size, seed
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
            if not isinstance(row, dict):
                continue
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

    if clean(old.get("job_signature")) != job_signature(item):
        return False

    if clean(old.get("prompt_sha256")) != prompt_hash(item):
        return False

    if clean(old.get("prompt_signature")) != clean(
        item["source_job"].get("prompt_signature")
    ):
        return False

    if clean(old.get("source_scene_signature")) != clean(
        item["source_job"].get("source_scene_signature")
    ):
        return False

    if clean(old.get("image_path") or old.get("image") or old.get("file")) != (
        item["relative_path"]
    ):
        return False

    expected_hash = clean(old.get("image_sha256"))
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash):
        return False

    try:
        integrity = image_integrity(item["destination"], item["size"])
    except (OSError, RuntimeError, ValueError):
        return False

    return integrity["sha256"].casefold() == expected_hash.casefold()


def make_record(item, status, provider=None, attempts=None):
    destination = item["destination"]
    source = item["source_job"]
    integrity = None

    if status in {"generated", "existing"}:
        integrity = image_integrity(destination, item["size"])

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
        log(
            "Selector provider is not configured; it will not be used:",
            provider,
        )
    log("Generation follows Input IMAGE_PROVIDER_ORDER only.")


def build_policy():
    return {
        "provider_order": list(PROVIDER_ORDER),
        "fallback_enabled": IMAGE_FALLBACK_ENABLED,
        "billable_providers_allowed": ALLOW_BILLABLE,
        "max_retries_per_provider": MAX_RETRIES,
        "huggingface_spaces": (
            list(HF_SPACES) if "huggingface" in PROVIDER_ORDER else []
        ),
        "pollinations_models": (
            list(POLLINATIONS_MODELS)
            if "pollinations" in PROVIDER_ORDER else []
        ),
        "gemini_model": (
            GEMINI_MODEL if "gemini" in PROVIDER_ORDER else None
        ),
        "replicate_model": (
            REPLICATE_MODEL if "replicate" in PROVIDER_ORDER else None
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
        "total_scenes": total,
        "images_ready": ready,
        "images_required": total,
        "images_failed": failed,
        "images_pending": pending,
        "policy": policy,
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
                    "source_scene_signature", "source_fingerprint",
                    "config_signature", "file_integrity",
                    "semantic_story_match",
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

    expected_ids = [
        clean(row.get("scene_id"))
        for row in records
    ]
    if len(expected_ids) != len(records):
        raise RuntimeError("Image record count is inconsistent.")

    for row in records:
        if row.get("status") in {"generated", "existing"}:
            if not re.fullmatch(
                r"[a-fA-F0-9]{64}",
                clean(row.get("image_sha256")),
            ):
                raise RuntimeError(
                    f"{row['scene_id']}: ready image has no valid SHA256."
                )
            if row.get("file_integrity") != "verified":
                raise RuntimeError(
                    f"{row['scene_id']}: ready image integrity is unverified."
                )

    ready, failed, pending = count_status(records)
    if ready + failed + pending != len(records):
        raise RuntimeError("Image manifest status totals do not match job count.")

    if ready == len(records):
        for row in records:
            if row.get("status") not in {"generated", "existing"}:
                raise RuntimeError("Cannot complete manifest with unfinished images.")

    expected_fingerprint = clean(metadata.get("source_fingerprint"))
    expected_config = clean(metadata.get("config_signature"))
    for row in records:
        if expected_fingerprint and clean(row.get("source_fingerprint")) != expected_fingerprint:
            raise RuntimeError(
                f"{row['scene_id']}: source fingerprint mismatch."
            )
        if expected_config and clean(row.get("config_signature")) != expected_config:
            raise RuntimeError(
                f"{row['scene_id']}: config signature mismatch."
            )


def save_checkpoints(records, metadata, policy, counters):
    validate_manifest_consistency(records, metadata)
    generation = build_generation_manifest(
        records,
        metadata,
        policy,
        counters,
    )
    images = build_image_manifest(records, metadata, policy)

    save_json(GENERATION_MANIFEST, generation)
    save_json(IMAGE_MANIFEST, images)


def run_provider_attempts(item, record, records, metadata, policy, counters):
    prompt = make_prompt(item)
    negative = build_negative_prompt(item)
    seed = deterministic_seed(item)
    destination = item["destination"]
    size = item["size"]

    attempts = build_provider_attempts(
        prompt,
        destination,
        size,
        seed,
        negative,
    )

    if not attempts:
        record["status"] = "failed"
        record["error"] = (
            "No configured image provider is currently usable. "
            "Check configured provider credentials and billable permission."
        )
        return

    last_error = "All configured provider attempts failed."
    succeeded = False

    for provider_name, provider_key, billable, generate in attempts:
        if succeeded:
            break

        for retry_number in range(1, MAX_RETRIES + 1):
            try:
                log(
                    "Generating:",
                    item["scene_id"],
                    "| Part:", item["part"],
                    "| Role:", item["role"],
                    "| Provider:", provider_name,
                    "| Attempt:", retry_number,
                )

                destination.parent.mkdir(parents=True, exist_ok=True)

                counters["external_api_calls"] += 1
                if billable:
                    counters["billable_api_calls"] += 1

                record["status"] = "pending"
                record["current_provider"] = provider_name
                record["current_attempt"] = retry_number
                save_checkpoints(records, metadata, policy, counters)

                generate()

                integrity = image_integrity(destination, size)
                record.update(make_record(
                    item,
                    "generated",
                    provider_name,
                    record.get("attempts", []),
                ))
                record.setdefault("attempts", []).append({
                    "provider": provider_name,
                    "provider_key": provider_key,
                    "billable": billable,
                    "attempt": retry_number,
                    "status": "success",
                    "image_sha256": integrity["sha256"],
                    "timestamp": int(time.time()),
                })

                succeeded = True
                save_checkpoints(records, metadata, policy, counters)
                log("IMAGE CHECKPOINT SAVED:", item["scene_id"])
                break

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                destination.unlink(missing_ok=True)

                record.setdefault("attempts", []).append({
                    "provider": provider_name,
                    "provider_key": provider_key,
                    "billable": billable,
                    "attempt": retry_number,
                    "status": "failed",
                    "error": last_error[:800],
                    "timestamp": int(time.time()),
                })

                record["status"] = "pending"
                record["last_provider_error"] = last_error[:800]
                log("Provider failed:", provider_name, last_error[:350])
                save_checkpoints(records, metadata, policy, counters)

            if not IMAGE_FALLBACK_ENABLED:
                break

    if not succeeded:
        record["status"] = "failed"
        record["error"] = last_error[:1000]
        record.pop("current_provider", None)
        record.pop("current_attempt", None)
        log("IMAGE FAILED:", item["scene_id"], "|", last_error[:350])


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
    if format_name not in ALLOWED_FORMATS:
        raise RuntimeError(
            f"Visual jobs manifest has invalid format: {format_name!r}."
        )

    expected_size = "720x1280" if format_name == "short" else "1280x720"
    if clean(metadata.get("image_size")) != expected_size:
        raise RuntimeError(
            f"{format_name.upper()} must use {expected_size}; "
            f"manifest has {metadata.get('image_size')!r}."
        )

    expected_fingerprint = clean(metadata.get("source_fingerprint"))
    expected_config = clean(metadata.get("config_signature"))
    if not expected_fingerprint or not expected_config:
        raise RuntimeError(
            "Visual jobs manifest is missing source/config fingerprints."
        )

    for item in items:
        if item["size_text"] != expected_size:
            raise RuntimeError(
                f"{item['scene_id']}: image size does not match {format_name.upper()}."
            )

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
        else:
            record = make_record(item, "pending")
        records.append(record)

    policy = build_policy()
    counters = {
        "external_api_calls": 0,
        "billable_api_calls": 0,
    }

    save_checkpoints(records, metadata, policy, counters)

    ready_before = sum(
        row["status"] == "existing" for row in records
    )
    pending_items = [
        (item, record)
        for item, record in zip(items, records)
        if record["status"] == "pending"
    ]

    log("-" * 64)
    log("Format:", format_name)
    log("Image dimensions:", expected_size)
    log("Total image jobs:", len(items))
    log("Verified reusable images:", ready_before)
    log("Images requiring generation:", len(pending_items))
    log("Configured provider order:", " -> ".join(PROVIDER_ORDER))
    log("Billable providers allowed:", ALLOW_BILLABLE)
    log("Fallback enabled:", IMAGE_FALLBACK_ENABLED)
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
        save_checkpoints(records, metadata, policy, counters)

    save_checkpoints(records, metadata, policy, counters)

    ready, failed, pending = count_status(records)
    failed_ids = [
        row["scene_id"]
        for row in records
        if row["status"] == "failed"
    ]

    log("=" * 64)
    log("IMAGE GENERATION SUMMARY")
    log("Images ready:", f"{ready}/{len(records)}")
    log("Failed:", failed)
    log("Pending:", pending)
    log("Verified reused:", ready_before)
    log("External provider calls:", counters["external_api_calls"])
    log("Billable provider calls:", counters["billable_api_calls"])
    log("Image manifest:", IMAGE_MANIFEST.relative_to(ROOT))
    log("Generation manifest:", GENERATION_MANIFEST.relative_to(ROOT))
    log("=" * 64)

    if failed or pending or ready != len(records):
        raise RuntimeError(
            "Image generation is incomplete. Failed scene IDs: "
            + (", ".join(failed_ids) if failed_ids else "none")
            + ". Successful image checkpoints have been preserved."
        )

    log("All image files passed format, dimensions, integrity and SHA256 checks.")
    log(
        "Story-event semantic verification is not claimed; "
        "no configured vision-verification stage is available in this file."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("IMAGE GENERATION CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log(
            "IMAGE GENERATION FAILED:",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)