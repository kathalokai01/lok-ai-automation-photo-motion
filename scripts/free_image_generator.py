
#!/usr/bin/env python3
"""Resumable image generation with validated provider fallback and manifests."""

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
VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
IMAGE_MANIFEST = VISUAL_DIR / "image_manifest.json"
GENERATION_MANIFEST = VISUAL_DIR / "image_generation_manifest.json"

VERSION = "photo-motion-image-generator-v19-checkpoint-safe"
MIN_IMAGE_BYTES = 1000
TIMEOUT = int(os.getenv("IMAGE_PROVIDER_TIMEOUT", "180"))
BASE_SEED = 20261009

ALLOW_BILLABLE = os.getenv(
    "ALLOW_BILLABLE_PROVIDERS", "true"
).strip().lower() in {"1", "true", "yes", "on"}

HF_SPACES = list(dict.fromkeys(
    value.strip()
    for value in [
        os.getenv("HF_IMAGE_SPACE", "mrfakename/Z-Image-Turbo"),
        *os.getenv("FREE_IMAGE_FALLBACK_SPACES", "").split(","),
    ]
    if value.strip()
))

POLLINATIONS_MODELS = list(dict.fromkeys(
    value.strip()
    for value in os.getenv(
        "POLLINATIONS_FREE_IMAGE_MODELS",
        "black-forest-labs/flux.1-schnell,"
        "tongyi-mai/z-image-turbo,"
        "lykon/dreamshaper-8-lcm",
    ).split(",")
    if value.strip()
))

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
REPLICATE_KEY = os.getenv("REPLICATE_API_TOKEN", "").strip()
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"
).strip()

REPLICATE_MODEL = os.getenv(
    "REPLICATE_IMAGE_MODEL", "black-forest-labs/flux-schnell"
).strip()

QUALITY = (
    "Photorealistic live-action cinematic photography. Realistic Indian "
    "people, locations, clothing and architecture. Natural skin texture, "
    "believable anatomy, realistic eyes, natural lighting and shadows. "
    "Maintain recurring character identity, age, hairstyle and clothing. "
    "No cartoon, anime, illustration, CGI, text, logo or watermark."
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "KathaLokAI-PhotoMotion/19.0"
})


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing path for {label}")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path escapes project directory")

    return path


def parse_size(value):
    try:
        width, height = map(
            int, str(value).lower().strip().split("x", 1)
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid image size {value!r}; expected WIDTHxHEIGHT"
        ) from exc

    if not (256 <= width <= 4096 and 256 <= height <= 4096):
        raise RuntimeError(
            f"Unsupported image dimensions: {width}x{height}"
        )

    return width, height


def get_jobs():
    document = read_json(JOBS_FILE, {})

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
        raise RuntimeError(
            "visual_jobs.json must be an object or list"
        )

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(
            "visual_jobs.json contains no image jobs"
        )

    prepared = []
    seen_global = set()
    seen_local = set()

    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(
                f"Image job {index} is not an object"
            )

        try:
            part = int(job.get("part", 1))
            local_scene = int(
                job.get("local_scene", job.get("scene", index))
            )
            global_scene = int(
                job.get(
                    "global_scene",
                    job.get("scene_index", index),
                )
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene numbering in image job {index}"
            ) from exc

        if min(part, local_scene, global_scene) < 1:
            raise RuntimeError(
                "Part and scene numbers must be positive"
            )

        if global_scene in seen_global:
            raise RuntimeError(
                f"Duplicate global scene: {global_scene}"
            )

        if (part, local_scene) in seen_local:
            raise RuntimeError(
                f"Duplicate part/local scene: {part}/{local_scene}"
            )

        seen_global.add(global_scene)
        seen_local.add((part, local_scene))

        size = parse_size(job.get(
            "image_size",
            metadata.get("image_size", "1024x576"),
        ))

        prompt = next((
            job[key].strip()
            for key in (
                "prompt",
                "image_prompt",
                "visual_prompt",
                "scene_prompt",
                "description",
            )
            if isinstance(job.get(key), str)
            and job[key].strip()
        ), "")

        if not prompt:
            raise RuntimeError(
                f"Scene {global_scene}: image prompt is missing"
            )

        destination = safe_path(
            str(
                job.get("output_path")
                or job.get("image_path")
                or job.get("image")
                or (
                    f"output/visuals/part_{part:02d}/"
                    f"scene_{local_scene:02d}.png"
                )
            ),
            f"scene {global_scene} output",
        )

        prepared.append({
            "scene": global_scene,
            "global_scene": global_scene,
            "part": part,
            "local_scene": local_scene,
            "prompt": prompt,
            "negative_prompt": str(
                job.get("negative_prompt", "")
            ).strip(),
            "size": size,
            "size_text": f"{size[0]}x{size[1]}",
            "destination": destination,
            "duration": job.get(
                "scene_duration",
                job.get("duration"),
            ),
            "scene_id": job.get("id") or job.get("scene_id"),
            "source_job": job,
        })

    prepared.sort(key=lambda item: item["scene"])
    numbers = [item["scene"] for item in prepared]

    if numbers != list(range(1, len(prepared) + 1)):
        raise RuntimeError(
            "Global scene numbers must be continuous from 1. "
            f"Received: {numbers}"
        )

    return prepared, metadata


def make_prompt(item):
    return (
        f"{item['prompt']}\n\n"
        f"Quality requirements: {QUALITY}\n\n"
        f"Avoid: {item['negative_prompt']}"
    )


def prompt_hash(item):
    return sha256_text(make_prompt(item))


def valid_image(path, size):
    try:
        path = Path(path)
        if (
            not path.is_file()
            or path.stat().st_size < MIN_IMAGE_BYTES
        ):
            return False

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            return (
                image.format in ("PNG", "JPEG", "WEBP")
                and image.size == tuple(size)
            )
    except Exception:
        return False


def save_image(image, destination, size):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.stem + ".tmp.png"
    )

    try:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image = ImageOps.fit(
            image,
            tuple(size),
            method=Image.Resampling.LANCZOS,
        )
        image.save(temporary, "PNG", optimize=True)

        if not valid_image(temporary, size):
            raise ValueError(
                "Generated image failed validation"
            )

        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def save_image_bytes(raw, destination, size):
    if not raw:
        raise ValueError("Provider returned empty image data")

    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_image(image, destination, size)


def load_rows(path):
    document = read_json(path, {})
    rows = (
        document.get("scenes", [])
        if isinstance(document, dict)
        else document if isinstance(document, list) else []
    )

    result = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                number = int(
                    row.get("global_scene", row.get("scene"))
                )
            except (TypeError, ValueError):
                continue
            if number > 0:
                result[number] = row

    return result


def previous_scene_rows():
    current = load_rows(GENERATION_MANIFEST)
    older = load_rows(IMAGE_MANIFEST)
    current.update({
        key: value
        for key, value in older.items()
        if key not in current
    })
    return current


def access_error(message):
    message = str(message).lower()
    return any(term in message for term in (
        "429",
        "401",
        "403",
        "402",
        "quota",
        "rate limit",
        "billing",
        "payment required",
        "insufficient credit",
        "unauthorized",
        "forbidden",
        "resource exhausted",
    ))


def response_image(response, destination, size, provider):
    response.raise_for_status()
    content_type = response.headers.get(
        "content-type", ""
    ).lower()

    if not content_type.startswith("image/"):
        raise ValueError(
            f"{provider} returned non-image content"
        )

    save_image_bytes(
        response.content, destination, size
    )


def parameter_name(parameter):
    if not isinstance(parameter, dict):
        return ""
    return str(
        parameter.get("parameter_name")
        or parameter.get("name")
        or ""
    ).strip()


def parameter_required(parameter):
    if not isinstance(parameter, dict):
        return False
    if "parameter_has_default" in parameter:
        return not bool(parameter["parameter_has_default"])
    return bool(parameter.get("required", False))


def build_hf_arguments(parameters, prompt, size, seed, negative):
    width, height = size
    arguments = {}
    prompt_found = False

    for parameter in parameters:
        name = parameter_name(parameter)
        if not name:
            continue

        normalized = re.sub(
            r"[^a-z0-9]", "", name.lower()
        )
        required = parameter_required(parameter)

        if any(token in normalized for token in (
            "prompt",
            "description",
            "textprompt",
            "positiveprompt",
        )):
            arguments[name] = prompt
            prompt_found = True
        elif normalized in {
            "width", "imagewidth", "outputwidth"
        }:
            arguments[name] = width
        elif normalized in {
            "height", "imageheight", "outputheight"
        }:
            arguments[name] = height
        elif normalized in {"seed", "randomseed"}:
            arguments[name] = seed
        elif normalized in {"negativeprompt", "negative"}:
            arguments[name] = negative
        elif normalized in {
            "guidancescale", "guidance", "cfgscale"
        }:
            arguments[name] = 7
        elif normalized in {
            "steps", "numsteps", "inference_steps"
        }:
            arguments[name] = 9
        elif normalized in {"randomize", "randomizeseed"}:
            arguments[name] = False
        elif required:
            raise RuntimeError(
                f"Unsupported required Hugging Face input: {name}"
            )

    if not prompt_found:
        raise RuntimeError(
            "Hugging Face endpoint has no recognized prompt input"
        )

    return arguments


def generate_huggingface(
    prompt, destination, size, seed, space, negative
):
    if Client is None:
        raise RuntimeError(
            "gradio_client is not installed"
        )

    options = {"verbose": False}
    if HF_TOKEN:
        options["hf_token"] = HF_TOKEN

    client = Client(space, **options)
    api = client.view_api(return_format="dict")
    endpoints = api.get("named_endpoints", {})

    if not isinstance(endpoints, dict) or not endpoints:
        raise RuntimeError(
            f"{space}: no named Gradio endpoints found"
        )

    preferred = (
        "/generate_image",
        "/generate",
        "/predict",
        "/run",
    )
    endpoint = next(
        (name for name in preferred if name in endpoints),
        next(iter(endpoints)),
    )

    parameters = endpoints[endpoint].get(
        "parameters", []
    )
    if not isinstance(parameters, list):
        raise RuntimeError(
            f"{space}: invalid endpoint parameter schema"
        )

    arguments = build_hf_arguments(
        parameters, prompt, size, seed, negative
    )

    result = client.predict(
        api_name=endpoint, **arguments
    )

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
        try:
            candidate = Path(result)
            if candidate.is_file():
                with Image.open(candidate) as image:
                    save_image(image, destination, size)
                return
        except OSError:
            pass

        if result.startswith(("https://", "http://")):
            response = SESSION.get(
                result, timeout=TIMEOUT
            )
            response_image(
                response,
                destination,
                size,
                "Hugging Face",
            )
            return

    raise ValueError(
        f"{space}: endpoint returned no usable image"
    )


def generate_pollinations(
    prompt, destination, size, seed, model
):
    width, height = size
    url = (
        "https://gen.pollinations.ai/image/"
        + quote(prompt, safe="")
    )

    headers = {}
    if POLLINATIONS_KEY:
        headers["Authorization"] = (
            f"Bearer {POLLINATIONS_KEY}"
        )

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

    response_image(
        response,
        destination,
        size,
        f"Pollinations ({model})",
    )


def generate_gemini(prompt, destination, size):
    if not ALLOW_BILLABLE:
        raise RuntimeError(
            "Gemini blocked: billable providers disabled"
        )
    if not GEMINI_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing"
        )

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
                        + f"\nGenerate one image in {aspect} composition."
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
        content = candidate.get("content", {})
        for item in content.get("parts", []):
            inline = (
                item.get("inlineData")
                or item.get("inline_data")
            )
            if isinstance(inline, dict) and inline.get("data"):
                raw = base64.b64decode(
                    inline["data"], validate=True
                )
                save_image_bytes(raw, destination, size)
                return

    raise ValueError(
        "Gemini response contained no inline image data"
    )


def generate_replicate(
    prompt, destination, size, seed
):
    if not ALLOW_BILLABLE:
        raise RuntimeError(
            "Replicate blocked: billable providers disabled"
        )
    if not REPLICATE_KEY:
        raise RuntimeError(
            "REPLICATE_API_TOKEN is missing"
        )

    model_parts = REPLICATE_MODEL.split("/", 1)
    if len(model_parts) != 2 or not all(model_parts):
        raise RuntimeError(
            "REPLICATE_IMAGE_MODEL must be owner/model-name"
        )

    width, height = size
    aspect = "9:16" if height > width else "16:9"

    response = SESSION.post(
        (
            "https://api.replicate.com/v1/models/"
            f"{model_parts[0]}/{model_parts[1]}/predictions"
        ),
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

    while prediction.get("status") in (
        "starting", "processing"
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Replicate prediction timed out"
            )

        time.sleep(2)
        poll_url = prediction.get("urls", {}).get("get")

        if not poll_url:
            prediction_id = prediction.get("id")
            if not prediction_id:
                raise ValueError(
                    "Replicate returned no prediction ID"
                )
            poll_url = (
                "https://api.replicate.com/v1/predictions/"
                + str(prediction_id)
            )

        check = SESSION.get(
            poll_url,
            headers={
                "Authorization": f"Bearer {REPLICATE_KEY}"
            },
            timeout=30,
        )
        check.raise_for_status()
        prediction = check.json()

    if prediction.get("status") != "succeeded":
        raise RuntimeError(
            "Replicate failed: "
            + str(
                prediction.get("error")
                or prediction.get("status")
            )
        )

    output = prediction.get("output")
    if isinstance(output, str):
        output = [output]
    elif isinstance(output, dict):
        output = list(output.values())

    image_url = next((
        item for item in (output or [])
        if isinstance(item, str)
        and item.startswith(("https://", "http://"))
    ), None)

    if not image_url:
        raise ValueError(
            "Replicate returned no image URL"
        )

    image_response = SESSION.get(
        image_url, timeout=TIMEOUT
    )
    response_image(
        image_response,
        destination,
        size,
        "Replicate",
    )


def make_record(item, status, provider=None):
    destination = item["destination"]
    relative = destination.relative_to(ROOT).as_posix()

    return {
        "scene": item["scene"],
        "global_scene": item["scene"],
        "global_scene_number": item["scene"],
        "part": item["part"],
        "local_scene": item["local_scene"],
        "scene_id": item["scene_id"],
        "file": relative,
        "image": relative,
        "image_path": relative,
        "width": item["size"][0],
        "height": item["size"][1],
        "image_size": item["size_text"],
        "status": status,
        "provider": provider,
        "prompt_sha256": prompt_hash(item),
        "image_sha256": (
            sha256_file(destination)
            if valid_image(destination, item["size"])
            else None
        ),
        "duration": item["duration"],
    }


def build_image_manifest(items, records, metadata):
    rows = []

    for record in records:
        rows.append({
            "scene": record["scene"],
            "global_scene": record["scene"],
            "global_scene_number": record["scene"],
            "part": record["part"],
            "local_scene": record["local_scene"],
            "scene_id": record["scene_id"],
            "image": record["image"],
            "image_path": record["image_path"],
            "image_sha256": record["image_sha256"],
            "width": record["width"],
            "height": record["height"],
            "duration": record["duration"],
            "status": record["status"],
            "provider": record["provider"],
            "prompt_sha256": record["prompt_sha256"],
        })

    ready = sum(
        row["status"] in ("existing", "generated")
        for row in rows
    )

    return {
        "version": VERSION,
        "status": (
            "completed" if ready == len(rows) else "in_progress"
        ),
        "source_fingerprint": metadata.get(
            "source_fingerprint"
        ),
        "config_signature": metadata.get(
            "config_signature"
        ),
        "format": metadata.get("format", "unknown"),
        "image_size": metadata.get("image_size"),
        "total_scenes": len(rows),
        "images_ready": ready,
        "images_required": len(rows),
        "scenes": rows,
    }


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    items, metadata = get_jobs()
    previous = previous_scene_rows()

    records = []
    pending = []

    for item in items:
        old = previous.get(item["scene"], {})
        destination = item["destination"]
        digest = prompt_hash(item)

        image_ok = valid_image(
            destination, item["size"]
        )
        checksum_matches = (
            image_ok
            and (
                not old.get("image_sha256")
                or sha256_file(destination)
                == old["image_sha256"]
            )
        )

        if (
            image_ok
            and old.get("prompt_sha256") == digest
            and checksum_matches
        ):
            record = make_record(
                item,
                "existing",
                old.get("provider") or "previous run",
            )
        else:
            record = make_record(item, "pending")
            pending.append((item, record))

        records.append(record)

    manifest = {
        "version": VERSION,
        "status": "in_progress",
        "source_fingerprint": metadata.get(
            "source_fingerprint"
        ),
        "config_signature": metadata.get(
            "config_signature"
        ),
        "format": metadata.get("format", "unknown"),
        "image_size": metadata.get("image_size"),
        "policy": {
            "potentially_billable_providers_enabled": (
                ALLOW_BILLABLE
            ),
            "provider_order": [
                *[
                    f"Hugging Face: {space}"
                    for space in HF_SPACES
                ],
                *[
                    f"Pollinations: {model}"
                    for model in POLLINATIONS_MODELS
                ],
                "Gemini Image",
                "Replicate",
            ],
            "pollinations_models": POLLINATIONS_MODELS,
        },
        "total_scenes": len(records),
        "completed_count": 0,
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        ready = sum(
            row["status"] in ("existing", "generated")
            for row in records
        )
        failed = sum(
            row["status"] == "failed"
            for row in records
        )
        pending_count = sum(
            row["status"] == "pending"
            for row in records
        )

        manifest["completed_count"] = ready
        manifest["status"] = (
            "completed"
            if ready == len(records)
            else "in_progress"
        )
        manifest["summary"] = {
            "ready": ready,
            "failed": failed,
            "pending": pending_count,
        }

        save_json(GENERATION_MANIFEST, manifest)
        save_json(
            IMAGE_MANIFEST,
            build_image_manifest(items, records, metadata),
        )

    checkpoint()

    log("KATHA LOK AI — IMAGE GENERATION")
    log("Version:", VERSION)
    log("Format:", metadata.get("format", "unknown"))
    log("Scenes:", len(items))
    log("Already valid:", len(items) - len(pending))
    log("Pending:", len(pending))
    log("Billable providers enabled:", ALLOW_BILLABLE)

    if not pending:
        log(
            "All image checkpoints are valid. "
            "No provider calls needed."
        )
        return 0

    blocked_hf = set()
    blocked_pollinations = set()
    blocked_paid = set()

    for item, record in pending:
        prompt = make_prompt(item)
        destination = item["destination"]
        size = item["size"]
        seed = BASE_SEED + item["scene"]
        negative = item["negative_prompt"]

        providers = []

        for space in HF_SPACES:
            if space in blocked_hf:
                continue

            providers.append((
                f"Hugging Face: {space}",
                lambda space=space: generate_huggingface(
                    prompt,
                    destination,
                    size,
                    seed,
                    space,
                    negative,
                ),
                "hf:" + space,
                False,
            ))

        for model in POLLINATIONS_MODELS:
            if model in blocked_pollinations:
                continue

            providers.append((
                f"Pollinations: {model}",
                lambda model=model: generate_pollinations(
                    prompt,
                    destination,
                    size,
                    seed,
                    model,
                ),
                "pollinations:" + model,
                False,
            ))

        if ALLOW_BILLABLE:
            if GEMINI_KEY and "gemini" not in blocked_paid:
                providers.append((
                    f"Gemini Image: {GEMINI_MODEL}",
                    lambda: generate_gemini(
                        prompt, destination, size
                    ),
                    "gemini",
                    True,
                ))

            if (
                REPLICATE_KEY
                and "replicate" not in blocked_paid
            ):
                providers.append((
                    f"Replicate: {REPLICATE_MODEL}",
                    lambda: generate_replicate(
                        prompt,
                        destination,
                        size,
                        seed,
                    ),
                    "replicate",
                    True,
                ))

        succeeded = False
        last_error = "No configured provider succeeded."

        for name, generate, provider_key, billable in providers:
            try:
                log(
                    f"Scene {item['scene']} "
                    f"({item['part']}/{item['local_scene']}): "
                    f"trying {name}"
                )

                destination.parent.mkdir(
                    parents=True, exist_ok=True
                )
                generate()

                if not valid_image(destination, size):
                    destination.unlink(missing_ok=True)
                    raise ValueError(
                        "Provider output failed image validation"
                    )

                record.update(
                    make_record(item, "generated", name)
                )
                record.setdefault("attempts", []).append({
                    "provider": name,
                    "status": "success",
                })

                succeeded = True
                checkpoint()

                log(
                    f"Scene {item['scene']}: "
                    "checkpoint saved"
                )
                break

            except Exception as exc:
                last_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                destination.unlink(missing_ok=True)

                log(
                    f"Provider failed: {name}: "
                    f"{last_error[:250]}"
                )

                record.setdefault("attempts", []).append({
                    "provider": name,
                    "status": "failed",
                    "error": last_error[:500],
                })

                if access_error(last_error):
                    if provider_key.startswith("hf:"):
                        blocked_hf.add(provider_key[3:])
                    elif provider_key.startswith("pollinations:"):
                        blocked_pollinations.add(
                            provider_key.split(":", 1)[1]
                        )
                    elif billable:
                        blocked_paid.add(provider_key)

        if not succeeded:
            record["status"] = "failed"
            record["error"] = last_error[:800]

            log(
                f"Scene {item['scene']} failed. "
                "Successful checkpoints preserved."
            )

        checkpoint()

    failed = [
        row["scene"]
        for row in records
        if row["status"] == "failed"
    ]
    ready = sum(
        row["status"] in ("existing", "generated")
        for row in records
    )

    log("Images ready:", f"{ready}/{len(records)}")
    log("Failed scenes:", failed)
    log("Image manifest:", IMAGE_MANIFEST)
    log("Generation manifest:", GENERATION_MANIFEST)

    if failed:
        raise RuntimeError(
            f"Image generation failed for scenes {failed}. "
            "Successful checkpoints have been preserved."
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(
            "IMAGE GENERATION CANCELLED",
            file=sys.stderr,
        )
        sys.exit(130)
    except Exception as exc:
        print(
            f"IMAGE GENERATION ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
