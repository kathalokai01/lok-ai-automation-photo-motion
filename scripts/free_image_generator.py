#!/usr/bin/env python3
"""Validated, resumable, story-grounded image generation."""

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
from urllib.parse import quote

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
IMAGE_MANIFEST = VISUAL_DIR / "image_manifest.json"
GENERATION_MANIFEST = VISUAL_DIR / "image_generation_manifest.json"
INPUT_FILE = ROOT / "Input" / "topic.txt"
SELECTED_MODEL_FILE = ROOT / "output" / "config" / "selected_visual_model.json"

VERSION = "photo-motion-image-generator-v25-verified-role-resume"
MIN_IMAGE_BYTES = 1000
MIN_DIMENSION = 256
MAX_DIMENSION = 4096
BASE_SEED = 20261009

TIMEOUT = max(30, min(900, int(os.getenv("IMAGE_PROVIDER_TIMEOUT", "180"))))

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "KathaLokAI-PhotoMotion/25.0"
})

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
REPLICATE_KEY = os.getenv("REPLICATE_API_TOKEN", "").strip()
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()

GEMINI_MODEL = ""
REPLICATE_MODEL = ""
HF_SPACES = []
POLLINATIONS_MODELS = []
PROVIDER_ORDER = []

ALLOW_BILLABLE = False
IMAGE_FALLBACK_ENABLED = True

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

ALLOWED_ROLES = {"hook", "main", "suspense", "resolution"}

QUALITY = (
    "Photorealistic live-action cinematic photography. Real human actors, "
    "natural skin texture, realistic eyes, believable anatomy, physically "
    "plausible lighting, natural shadows and realistic materials. Preserve "
    "recurring character identity, hairstyle, clothing and story-world "
    "continuity. No cartoon, anime, illustration, CGI, text, logo or watermark."
)


def log(*values):
    print(*values, flush=True)


def split_values(value):
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[,;]", str(value or ""))

    result = []
    seen = set()

    for item in values:
        item = str(item or "").strip()
        key = item.casefold()

        if item and key not in seen:
            seen.add(key)
            result.append(item)

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
    digest = hashlib.sha256()

    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def read_json(path, default=None):
    path = Path(path)

    if not path.is_file():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
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
    global ALLOW_BILLABLE, IMAGE_FALLBACK_ENABLED

    from input_config import (
        get_image_provider_order,
        get_global_billable_permission,
        parse_bool,
    )

    PROVIDER_ORDER = get_image_provider_order(config)

    HF_SPACES = split_values([
        config.get("HF_IMAGE_SPACE", "mrfakename/Z-Image-Turbo"),
        *split_values(config.get("FREE_IMAGE_FALLBACK_SPACES", "")),
    ])

    POLLINATIONS_MODELS = split_values(
        config.get(
            "POLLINATIONS_FREE_IMAGE_MODELS",
            "black-forest-labs/flux.1-schnell,"
            "tongyi-mai/z-image-turbo,"
            "lykon/dreamshaper-8-lcm",
        )
    )

    GEMINI_MODEL = str(
        config.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
    ).strip()

    REPLICATE_MODEL = str(
        config.get("REPLICATE_IMAGE_MODEL", "black-forest-labs/flux-schnell")
    ).strip()

    ALLOW_BILLABLE = (
        get_global_billable_permission(config)
        and parse_bool(
            config.get("IMAGE_ALLOW_BILLABLE", True),
            "IMAGE_ALLOW_BILLABLE",
        )
    )

    IMAGE_FALLBACK_ENABLED = parse_bool(
        config.get("IMAGE_FALLBACK_ENABLED", True),
        "IMAGE_FALLBACK_ENABLED",
    )

    if not PROVIDER_ORDER:
        raise RuntimeError("IMAGE_PROVIDER_ORDER में कोई provider configured नहीं है.")

    for provider in PROVIDER_ORDER:
        if provider not in {"huggingface", "pollinations", "gemini", "replicate"}:
            raise RuntimeError(f"Unsupported configured image provider: {provider}")

    if "huggingface" in PROVIDER_ORDER and not HF_SPACES:
        raise RuntimeError("Hugging Face configured है, लेकिन Space सूची खाली है.")

    if "pollinations" in PROVIDER_ORDER and not POLLINATIONS_MODELS:
        raise RuntimeError("Pollinations configured है, लेकिन model सूची खाली है.")

    if "gemini" in PROVIDER_ORDER and not GEMINI_MODEL:
        raise RuntimeError("GEMINI_IMAGE_MODEL खाली है.")

    if "replicate" in PROVIDER_ORDER and not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        REPLICATE_MODEL,
    ):
        raise RuntimeError("REPLICATE_IMAGE_MODEL में owner/model आवश्यक है.")

    log("Configured provider order:", PROVIDER_ORDER)
    log("Billable providers allowed:", ALLOW_BILLABLE)
    log("Provider fallback enabled:", IMAGE_FALLBACK_ENABLED)


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: output path missing.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: output path project directory से बाहर है.")

    return path


def parse_size(value):
    try:
        width, height = map(int, str(value).lower().strip().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid image size: {value!r}") from exc

    if not (
        MIN_DIMENSION <= width <= MAX_DIMENSION
        and MIN_DIMENSION <= height <= MAX_DIMENSION
    ):
        raise RuntimeError(f"Invalid image dimensions: {width}x{height}")

    return width, height


def get_jobs():
    document = read_json(JOBS_FILE)

    if isinstance(document, list):
        jobs, metadata = document, {}
    elif isinstance(document, dict):
        jobs = (
            document.get("jobs")
            or document.get("visual_jobs")
            or document.get("scenes")
            or []
        )
        metadata = document
    else:
        raise RuntimeError(f"Invalid visual jobs file: {JOBS_FILE}")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("visual_jobs.json में कोई image job नहीं है.")

    prepared = []
    seen_global = set()
    seen_local = set()
    seen_ids = set()
    seen_paths = set()

    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Image job {index} valid object नहीं है.")

        try:
            part = int(job.get("part", 1))
            local = int(job.get("local_scene", job.get("scene", index)))
            global_number = int(
                job.get("global_scene", job.get("scene_index", index))
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Image job {index} की numbering गलत है.") from exc

        if min(part, local, global_number) < 1:
            raise RuntimeError("Part और scene numbers positive होने चाहिए.")

        if global_number in seen_global:
            raise RuntimeError(f"Duplicate global scene number: {global_number}")

        if (part, local) in seen_local:
            raise RuntimeError(f"Duplicate image slot: part {part}, scene {local}")

        scene_id = str(
            job.get("scene_id")
            or job.get("id")
            or job.get("source_scene_id")
            or ""
        ).strip()

        if not scene_id:
            scene_id = f"part_{part:02d}_scene_{local:02d}"

        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", scene_id):
            raise RuntimeError(f"Invalid scene ID: {scene_id!r}")

        if scene_id in seen_ids:
            raise RuntimeError(f"Duplicate scene ID: {scene_id}")

        role = str(job.get("role") or job.get("scene_type") or "main").strip().lower()
        if role not in ALLOWED_ROLES:
            raise RuntimeError(f"Unsupported scene role: {role}")

        size_value = (
            job.get("image_size")
            or metadata.get("image_size")
        )

        if not size_value:
            format_name = str(metadata.get("format", "")).lower()
            if format_name == "short":
                size_value = "720x1280"
            elif format_name == "full":
                size_value = "1280x720"
            else:
                raise RuntimeError(
                    "Image size और SHORT/FULL format दोनों अनुपस्थित हैं."
                )

        size = parse_size(size_value)

        prompt = next(
            (
                str(job.get(key)).strip()
                for key in (
                    "prompt",
                    "image_prompt",
                    "visual_prompt",
                    "scene_prompt",
                    "description",
                )
                if isinstance(job.get(key), str) and job[key].strip()
            ),
            "",
        )

        if len(prompt) < 24:
            raise RuntimeError(f"{scene_id}: usable story visual prompt नहीं है.")

        destination = safe_path(
            job.get("output_path")
            or job.get("image_path")
            or job.get("image")
            or f"output/visuals/{scene_id}.png",
            scene_id,
        )

        relative = destination.relative_to(ROOT).as_posix()
        if relative in seen_paths:
            raise RuntimeError(f"Duplicate image output path: {relative}")

        seen_global.add(global_number)
        seen_local.add((part, local))
        seen_ids.add(scene_id)
        seen_paths.add(relative)

        prepared.append({
            "scene": global_number,
            "global_scene": global_number,
            "part": part,
            "local_scene": local,
            "scene_id": scene_id,
            "role": role,
            "prompt": prompt,
            "negative_prompt": str(job.get("negative_prompt", "")).strip(),
            "size": size,
            "size_text": f"{size[0]}x{size[1]}",
            "destination": destination,
            "duration": job.get("scene_duration", job.get("duration")),
            "source_job": job,
        })

    prepared.sort(key=lambda item: item["global_scene"])

    numbers = [item["global_scene"] for item in prepared]
    if numbers != list(range(1, len(prepared) + 1)):
        raise RuntimeError(
            "Global image numbering continuous 1 से शुरू होनी चाहिए: "
            + str(numbers)
        )

    slots = [(item["part"], item["local_scene"]) for item in prepared]
    if slots != sorted(slots):
        raise RuntimeError("Image jobs part और local slot के क्रम में नहीं हैं.")

    return prepared, metadata


def make_prompt(item):
    sections = [
        item["prompt"].strip(),
        "Photographic quality: " + QUALITY,
        (
            "Depict the exact story event, characters, location, period, "
            "objects, action and emotional meaning. Do not replace specific "
            "story details with generic imagery or invent new events."
        ),
        (
            "Compose the still image for later photo-motion camera movement. "
            "Keep faces, people and important objects inside safe margins. "
            "Do not simulate body, face or character animation."
        ),
    ]

    role = item["role"]

    if role == "hook":
        sections.append(
            "HOOK FRAME: a clear dramatic story moment that creates curiosity "
            "without revealing the final resolution."
        )
    elif role == "suspense":
        sections.append(
            "SUSPENSE FRAME: preserve the exact unresolved danger, mystery or "
            "emotional tension. Do not show the final resolution."
        )
    elif role == "resolution":
        sections.append(
            "FINAL RESOLUTION FRAME: show the actual story ending and its "
            "emotional consequence. Do not invent another ending."
        )

    if item["negative_prompt"]:
        sections.append("Avoid: " + item["negative_prompt"])

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
        "size": item["size_text"],
        "source_scene_signature": source.get("source_scene_signature"),
        "prompt_signature": source.get("prompt_signature"),
        "config_signature": source.get("config_signature"),
    })


def valid_image(path, size):
    try:
        path = Path(path)

        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return False

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            return image.format in {"PNG", "JPEG", "WEBP"} and image.size == tuple(size)
    except Exception:
        return False


def save_image(image, destination, size):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(destination.stem + ".tmp.png")

    try:
        image = ImageOps.exif_transpose(image).convert("RGB")

        # Preserve the entire generated composition; do not crop people or props.
        image = ImageOps.pad(
            image,
            tuple(size),
            method=Image.Resampling.LANCZOS,
            color=(18, 18, 18),
            centering=(0.5, 0.5),
        )

        image.save(temporary, "PNG", optimize=True)

        if not valid_image(temporary, size):
            raise ValueError("Generated image integrity check failed.")

        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def save_image_bytes(raw, destination, size):
    if not raw:
        raise ValueError("Provider ने empty image data दिया.")

    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_image(image, destination, size)


def response_image(response, destination, size, provider):
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"{provider} ने image के बजाय non-image response दिया.")

    save_image_bytes(response.content, destination, size)


def load_rows(path):
    document = read_json(path, {})
    rows = document.get("scenes", []) if isinstance(document, dict) else document

    by_id, by_global = {}, {}
    if not isinstance(rows, list):
        return by_id, by_global

    for row in rows:
        if not isinstance(row, dict):
            continue

        scene_id = str(row.get("scene_id") or "").strip()
        if scene_id:
            by_id[scene_id] = row

        try:
            number = int(row.get("global_scene") or row.get("scene"))
        except (TypeError, ValueError):
            continue

        if number > 0:
            by_global[number] = row

    return by_id, by_global


def previous_scene_rows():
    by_id, by_global = {}, {}

    for path in (IMAGE_MANIFEST, GENERATION_MANIFEST):
        current_id, current_global = load_rows(path)
        by_id.update(current_id)
        by_global.update(current_global)

    return by_id, by_global


def build_negative_prompt(item):
    values = [
        "cartoon", "anime", "comic-book illustration", "2D drawing",
        "3D animated character", "plastic skin", "waxy face",
        "deformed anatomy", "extra fingers", "missing fingers",
        "duplicated people", "unrelated objects", "wrong story event",
        "wrong location", "wrong time period", "captions", "subtitles",
        "written text", "logo", "watermark", "low resolution",
        "blurry main subject",
    ]

    if item["negative_prompt"]:
        values.append(item["negative_prompt"])

    return ", ".join(dict.fromkeys(values))


def hf_parameter_name(parameter):
    if not isinstance(parameter, dict):
        return ""

    return str(parameter.get("parameter_name") or parameter.get("name") or "").strip()


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
        raise RuntimeError("Hugging Face endpoint में prompt input नहीं मिला.")

    return arguments


def generate_huggingface(prompt, destination, size, seed, space, negative):
    if Client is None:
        raise RuntimeError("gradio_client installed नहीं है.")

    options = {"verbose": False}
    if HF_TOKEN:
        options["hf_token"] = HF_TOKEN

    client = Client(space, **options)
    api = client.view_api(return_format="dict")
    endpoints = api.get("named_endpoints", {})

    if not isinstance(endpoints, dict) or not endpoints:
        raise RuntimeError(f"{space}: कोई named Gradio endpoint नहीं मिला.")

    preferred = ("/generate_image", "/generate", "/predict", "/run")
    endpoint = next((name for name in preferred if name in endpoints), next(iter(endpoints)))

    parameters = endpoints[endpoint].get("parameters", [])
    if not isinstance(parameters, list):
        raise RuntimeError(f"{space}: endpoint parameters invalid हैं.")

    arguments = build_hf_arguments(parameters, prompt, size, seed, negative)
    result = client.predict(api_name=endpoint, **arguments)

    if isinstance(result, (list, tuple)):
        result = result[0] if result else None

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

    if isinstance(result, str):
        candidate = Path(result)
        if candidate.is_file():
            with Image.open(candidate) as image:
                save_image(image, destination, size)
            return

        if result.startswith(("https://", "http://")):
            response = SESSION.get(result, timeout=TIMEOUT)
            response_image(response, destination, size, "Hugging Face")
            return

    raise ValueError(f"{space}: usable image output नहीं मिला.")

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
        raise RuntimeError("Gemini Image blocked: billable permission disabled.")

    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY उपलब्ध नहीं है.")

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
                    "text": prompt + f"\nGenerate one image in {aspect} composition."
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
        content = candidate.get("content", {})

        for part in content.get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")

            if isinstance(inline, dict) and inline.get("data"):
                raw = base64.b64decode(inline["data"], validate=True)
                save_image_bytes(raw, destination, size)
                return

    raise ValueError("Gemini response में image data नहीं मिला.")


def generate_replicate(prompt, destination, size, seed):
    if not ALLOW_BILLABLE:
        raise RuntimeError("Replicate blocked: billable permission disabled.")

    if not REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_TOKEN उपलब्ध नहीं है.")

    owner, separator, model = REPLICATE_MODEL.partition("/")
    if not separator or not owner or not model:
        raise RuntimeError("REPLICATE_IMAGE_MODEL में owner/model आवश्यक है.")

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

        time.sleep(2)

        poll_url = (
            prediction.get("urls", {}).get("get")
            or "https://api.replicate.com/v1/predictions/"
            + str(prediction.get("id", ""))
        )

        if poll_url.endswith("/"):
            raise RuntimeError("Replicate ने prediction ID नहीं दिया.")

        check = SESSION.get(
            poll_url,
            headers={"Authorization": f"Bearer {REPLICATE_KEY}"},
            timeout=30,
        )
        check.raise_for_status()
        prediction = check.json()

    if prediction.get("status") != "succeeded":
        raise RuntimeError(
            "Replicate failed: "
            + str(prediction.get("error") or prediction.get("status"))
        )

    output = prediction.get("output")
    if isinstance(output, str):
        output = [output]
    elif isinstance(output, dict):
        output = list(output.values())

    image_url = next(
        (
            value for value in (output or [])
            if isinstance(value, str)
            and value.startswith(("https://", "http://"))
        ),
        None,
    )

    if not image_url:
        raise ValueError("Replicate ने image URL नहीं दिया.")

    image_response = SESSION.get(image_url, timeout=TIMEOUT)
    response_image(image_response, destination, size, "Replicate")


def build_provider_attempts(prompt, destination, size, seed, negative, order):
    attempts = []

    for provider in order:
        if provider == "huggingface":
            spaces = HF_SPACES if IMAGE_FALLBACK_ENABLED else HF_SPACES[:1]

            for space in spaces:
                attempts.append((
                    f"Hugging Face: {space}",
                    lambda space=space: generate_huggingface(
                        prompt, destination, size, seed, space, negative
                    ),
                    "hf:" + space,
                    False,
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
                    lambda model=model: generate_pollinations(
                        prompt, destination, size, seed, model
                    ),
                    "pollinations:" + model,
                    False,
                ))

        elif provider == "gemini":
            if ALLOW_BILLABLE and GEMINI_KEY:
                attempts.append((
                    f"Gemini Image: {GEMINI_MODEL}",
                    lambda: generate_gemini(prompt, destination, size),
                    "gemini",
                    True,
                ))

        elif provider == "replicate":
            if ALLOW_BILLABLE and REPLICATE_KEY:
                attempts.append((
                    f"Replicate: {REPLICATE_MODEL}",
                    lambda: generate_replicate(
                        prompt, destination, size, seed
                    ),
                    "replicate",
                    True,
                ))

        if not IMAGE_FALLBACK_ENABLED:
            break

    return attempts


def make_record(item, status, provider=None):
    destination = item["destination"]
    valid = valid_image(destination, item["size"])
    relative = destination.relative_to(ROOT).as_posix()
    source = item["source_job"]

    return {
        "scene": item["scene"],
        "global_scene": item["global_scene"],
        "global_scene_number": item["global_scene"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "source_scene_number": source.get(
            "source_scene_number",
            source.get("source_local_scene"),
        ),
        "scene_id": item["scene_id"],
        "role": item["role"],
        "is_extra_scene": item["role"] != "main",
        "file": relative,
        "image": relative,
        "image_path": relative,
        "width": item["size"][0],
        "height": item["size"][1],
        "image_size": item["size_text"],
        "status": status,
        "provider": provider,
        "prompt_sha256": prompt_hash(item),
        "prompt_signature": source.get("prompt_signature"),
        "job_signature": job_signature(item),
        "source_scene_signature": source.get("source_scene_signature"),
        "image_sha256": sha256_file(destination) if valid else None,
        "duration": item["duration"],
    }


def build_image_manifest(records, metadata):
    ready = sum(
        row["status"] in {"existing", "generated"}
        for row in records
    )
    failed = sum(row["status"] == "failed" for row in records)
    pending = len(records) - ready - failed

    return {
        "version": VERSION,
        "status": "completed" if ready == len(records) else "in_progress",
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "format": metadata.get("format", "unknown"),
        "image_size": metadata.get("image_size"),
        "total_scenes": len(records),
        "images_ready": ready,
        "images_required": len(records),
        "images_failed": failed,
        "images_pending": pending,
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
                    "source_scene_signature",
                )
            }
            for row in records
        ],
    }


def selected_model_notice(order):
    selection = read_json(SELECTED_MODEL_FILE, {})

    if not isinstance(selection, dict):
        return

    if str(selection.get("status", "")).lower() == "selected":
        provider = str(selection.get("provider") or "").lower()
        model = str(selection.get("model") or "").strip()

        normalized = PROVIDER_ALIASES.get(
            provider.replace("-", "_"),
            provider,
        )

        if normalized and normalized not in order:
            log("Selected visual model provider IMAGE_PROVIDER_ORDER में नहीं है:", provider)

        log("Visual selector record:", provider or "unknown", model or "unknown")
        log("Actual generation follows Input IMAGE_PROVIDER_ORDER only.")


def find_previous_record(item, previous_by_id, previous_by_global):
    old = previous_by_id.get(item["scene_id"])

    if old is None:
        possible = previous_by_global.get(item["global_scene"])
        if isinstance(possible, dict) and possible.get("scene_id") == item["scene_id"]:
            old = possible

    return old if isinstance(old, dict) else {}


def can_reuse_image(item, old):
    destination = item["destination"]

    if not valid_image(destination, item["size"]):
        return False

    if old.get("scene_id") != item["scene_id"]:
        return False

    if old.get("prompt_sha256") != prompt_hash(item):
        return False

    if old.get("job_signature") != job_signature(item):
        return False

    old_hash = str(old.get("image_sha256") or "").strip()
    if not old_hash:
        return False

    return sha256_file(destination) == old_hash


def build_policy():
    return {
        "billable_providers_enabled": ALLOW_BILLABLE,
        "fallback_enabled": IMAGE_FALLBACK_ENABLED,
        "provider_order": list(PROVIDER_ORDER),
        "huggingface_spaces": (
            HF_SPACES if "huggingface" in PROVIDER_ORDER else []
        ),
        "pollinations_models": (
            POLLINATIONS_MODELS if "pollinations" in PROVIDER_ORDER else []
        ),
        "gemini_model": (
            GEMINI_MODEL if "gemini" in PROVIDER_ORDER else None
        ),
        "replicate_model": (
            REPLICATE_MODEL if "replicate" in PROVIDER_ORDER else None
        ),
    }


def main():
    config = parse_input_settings()
    apply_input_settings(config)
    order = list(PROVIDER_ORDER)

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    items, metadata = get_jobs()
    previous_by_id, previous_by_global = previous_scene_rows()

    records = []
    pending = []

    for item in items:
        old = find_previous_record(item, previous_by_id, previous_by_global)

        if can_reuse_image(item, old):
            record = make_record(
                item,
                "existing",
                old.get("provider") or "previous run",
            )
            record["attempts"] = old.get("attempts", [])
        else:
            record = make_record(item, "pending")
            record["attempts"] = []
            pending.append((item, record))

        records.append(record)

    manifest = {
        "version": VERSION,
        "status": "in_progress",
        "source_fingerprint": metadata.get("source_fingerprint"),
        "config_signature": metadata.get("config_signature"),
        "format": metadata.get("format", "unknown"),
        "image_size": metadata.get("image_size"),
        "policy": build_policy(),
        "total_scenes": len(records),
        "completed_count": 0,
        "failed_count": 0,
        "pending_count": len(pending),
        "external_api_calls": 0,
        "billable_api_calls": 0,
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        ready = sum(
            row["status"] in {"existing", "generated"}
            for row in records
        )
        failed = sum(row["status"] == "failed" for row in records)
        pending_count = sum(row["status"] == "pending" for row in records)

        manifest["completed_count"] = ready
        manifest["failed_count"] = failed
        manifest["pending_count"] = pending_count
        manifest["status"] = (
            "completed" if ready == len(records) else "in_progress"
        )
        manifest["summary"] = {
            "ready": ready,
            "failed": failed,
            "pending": pending_count,
            "total": len(records),
        }

        save_json(GENERATION_MANIFEST, manifest)
        save_json(IMAGE_MANIFEST, build_image_manifest(records, metadata))

    checkpoint()

    log("=" * 64)
    log("KATHA LOK AI — PHOTO MOTION IMAGE GENERATION")
    log("Version:", VERSION)
    log("Format:", metadata.get("format", "unknown"))
    log("Total image jobs:", len(items))
    log("Already valid:", len(items) - len(pending))
    log("Pending:", len(pending))
    log("Provider order:", " -> ".join(order))
    log("Billable providers allowed:", ALLOW_BILLABLE)
    log("Fallback enabled:", IMAGE_FALLBACK_ENABLED)
    log("=" * 64)

    selected_model_notice(order)

    if not pending:
        log("All images match saved signatures and SHA256.")
        log("No image provider calls required.")
        return 0

    for item, record in pending:
        prompt = make_prompt(item)
        destination = item["destination"]
        size = item["size"]
        seed = BASE_SEED + item["global_scene"]
        negative = build_negative_prompt(item)

        attempts = build_provider_attempts(
            prompt,
            destination,
            size,
            seed,
            negative,
            order,
        )

        succeeded = False
        last_error = "कोई configured provider उपलब्ध नहीं है."

        if not attempts:
            record["status"] = "failed"
            record["error"] = (
                "Configured provider order में कोई provider उपलब्ध नहीं है. "
                "Provider key और billable permission जाँचें."
            )
            checkpoint()
            log("No available provider for:", item["scene_id"])
            continue

        for provider_name, generate, provider_key, billable in attempts:
            try:
                log(
                    "Generating:", item["scene_id"],
                    "| Part:", item["part"],
                    "| Role:", item["role"],
                    "| Provider:", provider_name,
                )

                destination.parent.mkdir(parents=True, exist_ok=True)

                # Record an actual provider attempt before making the call.
                manifest["external_api_calls"] += 1
                if billable:
                    manifest["billable_api_calls"] += 1
                checkpoint()

                generate()

                if not valid_image(destination, size):
                    destination.unlink(missing_ok=True)
                    raise ValueError(
                        "Provider output failed image format, dimensions "
                        "or integrity validation."
                    )

                record.update(make_record(item, "generated", provider_name))
                record.setdefault("attempts", []).append({
                    "provider": provider_name,
                    "provider_key": provider_key,
                    "billable": billable,
                    "status": "success",
                    "timestamp": int(time.time()),
                })

                succeeded = True
                checkpoint()
                log("IMAGE CHECKPOINT SAVED:", item["scene_id"])
                break

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                destination.unlink(missing_ok=True)

                record.setdefault("attempts", []).append({
                    "provider": provider_name,
                    "provider_key": provider_key,
                    "billable": billable,
                    "status": "failed",
                    "error": last_error[:800],
                    "timestamp": int(time.time()),
                })

                log("Provider failed:", provider_name, last_error[:350])
                checkpoint()

        if not succeeded:
            record["status"] = "failed"
            record["error"] = last_error[:1000]
            log("IMAGE FAILED:", item["scene_id"], "|", last_error[:350])

        checkpoint()

    failed = [
        row["scene_id"]
        for row in records
        if row["status"] == "failed"
    ]

    ready = sum(
        row["status"] in {"existing", "generated"}
        for row in records
    )

    log("=" * 64)
    log("IMAGE GENERATION SUMMARY")
    log("Images ready:", f"{ready}/{len(records)}")
    log("Failed scene IDs:", failed)
    log("Provider calls:", manifest["external_api_calls"])
    log("Billable calls:", manifest["billable_api_calls"])
    log("Image manifest:", IMAGE_MANIFEST.relative_to(ROOT))
    log("Generation manifest:", GENERATION_MANIFEST.relative_to(ROOT))
    log("=" * 64)

    if failed:
        raise RuntimeError(
            "Image generation failed for scene IDs: "
            + ", ".join(failed)
            + ". Successful scene checkpoints सुरक्षित हैं."
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("IMAGE GENERATION CANCELLED", flush=True)
        sys.exit(130)
    except Exception as exc:
        print(
            f"IMAGE GENERATION FAILED: {exc}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)