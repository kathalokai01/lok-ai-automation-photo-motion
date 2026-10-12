#!/usr/bin/env python3
"""Katha Lok AI — safe image generation, fallbacks and resumable checkpoints."""

from __future__ import annotations

import base64
import hashlib
import json
import os
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

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()

# Safety: "true" is NOT permission to spend.
# Paid providers require this exact, explicit opt-in value.
PAID_OPT_IN = "I_ACCEPT_POTENTIAL_CHARGES"
ALLOW_BILLABLE_PROVIDERS = (
    os.getenv("ALLOW_BILLABLE_PROVIDERS", "").strip()
    == PAID_OPT_IN
)

GEMINI_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"
).strip()

REPLICATE_MODEL = os.getenv(
    "REPLICATE_IMAGE_MODEL", "black-forest-labs/flux-schnell"
).strip()

POLLINATIONS_MODELS = list(dict.fromkeys(
    item.strip()
    for item in os.getenv(
        "POLLINATIONS_FREE_IMAGE_MODELS", "flux,turbo"
    ).split(",")
    if item.strip()
))

HF_SPACES = list(dict.fromkeys(
    item.strip()
    for item in [
        os.getenv("HF_IMAGE_SPACE", "mrfakename/Z-Image-Turbo"),
        *os.getenv("FREE_IMAGE_FALLBACK_SPACES", "").split(","),
    ]
    if item.strip()
))

REQUEST_TIMEOUT = 180
MAX_HF_ATTEMPTS = 1
MAX_POLLINATIONS_ATTEMPTS = 2
BASE_SEED = 20261009
MIN_IMAGE_BYTES = 5000

QUALITY = (
    "Photorealistic live-action cinematic photography. Realistic Indian "
    "people, clothing, locations and architecture. Natural skin texture, "
    "believable anatomy, realistic eyes, natural lighting and shadows. "
    "Keep recurring character identity, age, hairstyle and clothing "
    "consistent. No cartoon, anime, illustration, CGI, text, logo or "
    "watermark."
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KathaLokAI-PhotoMotion/2.0"})


def log(*args):
    print(*args, flush=True)


def read_json(path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


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
        jobs, metadata = [], {}

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("visual_jobs.json contains no jobs.")

    seen = set()
    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Job {index} must be an object.")
        number = int(job.get("global_scene", job.get("scene", index)))
        if number < 1 or number in seen:
            raise RuntimeError(f"Invalid or duplicate scene number: {number}")
        seen.add(number)

    if sorted(seen) != list(range(1, len(jobs) + 1)):
        raise RuntimeError("Scene numbers must be continuous, starting at 1.")

    return jobs, metadata


def get_number(job, fallback):
    return int(job.get("global_scene", job.get("scene", fallback)))


def get_size(job):
    try:
        width, height = map(
            int, str(job.get("image_size", "720x1280")).split("x", 1)
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid scene image_size.") from exc

    if not (256 <= width <= 2048 and 256 <= height <= 2048):
        raise RuntimeError(f"Invalid image dimensions: {width}x{height}")

    return width, height


def get_path(job, number):
    raw = job.get("image_path") or f"output/visuals/scene_{number:02d}.png"
    path = Path(str(raw))
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"Scene {number}: image path outside repository.")
    return path


def valid_image(path, size=None):
    try:
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return False
        with Image.open(path) as image:
            image.verify()
        if size:
            with Image.open(path) as image:
                return image.size == size
        return True
    except Exception:
        return False


def save_image(image, destination, size):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image, size, method=Image.Resampling.LANCZOS
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp.png")
    try:
        image.save(temp, "PNG", optimize=True)
        if not valid_image(temp, size):
            raise ValueError("Image failed PNG validation.")
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def save_image_bytes(raw, destination, size):
    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_image(image, destination, size)


def prompt_for(job):
    prompt = next(
        (
            job[key].strip()
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
    if not prompt:
        raise RuntimeError(
            f"Scene {job.get('scene', job.get('global_scene'))} "
            "has no image prompt."
        )
    negative = str(job.get("negative_prompt", "")).strip()
    return f"{prompt}\n\nQuality: {QUALITY}\n\nAvoid: {negative}"


def get_hash(job):
    return hashlib.sha256(prompt_for(job).encode("utf-8")).hexdigest()


def get_old_rows():
    document = read_json(IMAGE_MANIFEST, {})
    rows = document.get("scenes", []) if isinstance(document, dict) else []
    result = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                try:
                    result[int(row.get("global_scene", row.get("scene")))] = row
                except (TypeError, ValueError):
                    pass
    return result


def is_access_or_quota_error(message):
    message = message.lower()
    return any(term in message for term in (
        "429", "401", "403", "402", "quota", "rate limit",
        "billing", "payment required", "insufficient credit",
        "unauthorized", "forbidden", "exceeded your",
    ))


def check_image(destination, size):
    if not valid_image(destination, size):
        destination.unlink(missing_ok=True)
        raise ValueError("Provider returned an invalid image.")


# ---------------------------------------------------------
# Free-tier Hugging Face Spaces. Space availability and
# queue limits can change; this adapter is not guaranteed.
# ---------------------------------------------------------
def generate_hf(prompt, destination, size, seed, space):
    if Client is None:
        raise RuntimeError("gradio_client is not installed.")

    kwargs = {"verbose": False}
    if HF_TOKEN:
        kwargs["token"] = HF_TOKEN

    client = Client(space, **kwargs)
    width, height = size
    result = client.predict(
        prompt, height, width, 9, seed, False,
        api_name="/generate_image",
    )

    if isinstance(result, (list, tuple)):
        result = result[0] if result else None

    if isinstance(result, Image.Image):
        save_image(result, destination, size)
        return

    if isinstance(result, dict):
        result = result.get("path") or result.get("filepath") or result.get("url")

    if isinstance(result, str):
        candidate = Path(result)
        if candidate.is_file():
            with Image.open(candidate) as image:
                save_image(image, destination, size)
            return

        if result.startswith(("https://", "http://")):
            response = SESSION.get(result, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            if not response.headers.get("content-type", "").lower().startswith("image/"):
                raise ValueError("Hugging Face returned non-image content.")
            save_image_bytes(response.content, destination, size)
            return

    raise ValueError(
        f"Space {space} returned no usable image; its API schema may differ."
    )


# ---------------------------------------------------------
# Pollinations legacy endpoint.
# This does not send the configured API key. Its current
# availability, model support and pricing are not guaranteed.
# ---------------------------------------------------------
def generate_pollinations(prompt, destination, size, seed, model):
    width, height = size
    url = "https://image.pollinations.ai/prompt/" + quote(prompt, safe="")
    errors = []

    for attempt in range(1, MAX_POLLINATIONS_ATTEMPTS + 1):
        try:
            response = SESSION.get(
                url,
                params={
                    "model": model,
                    "width": width,
                    "height": height,
                    "seed": seed,
                    "nologo": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (401, 402, 403, 429):
                raise RuntimeError(
                    f"Pollinations access/quota response: HTTP {response.status_code}"
                )

            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                raise ValueError(
                    f"Non-image response: {content_type or 'unknown content type'}"
                )
            if len(response.content) < MIN_IMAGE_BYTES:
                raise ValueError("Image response was too small.")

            save_image_bytes(response.content, destination, size)
            check_image(destination, size)
            return

        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:180]}")
            log(f"Pollinations {model} attempt {attempt} failed:", errors[-1])
            if is_access_or_quota_error(str(exc)):
                break
            if attempt < MAX_POLLINATIONS_ATTEMPTS:
                time.sleep(3 * attempt)

    raise RuntimeError("Pollinations failed: " + " | ".join(errors[-3:]))


# ---------------------------------------------------------
# Potentially billable providers. These are never called
# unless ALLOW_BILLABLE_PROVIDERS equals the exact opt-in phrase.
# ---------------------------------------------------------
def generate_gemini(prompt, destination, size):
    if not ALLOW_BILLABLE_PROVIDERS:
        raise RuntimeError("Paid image providers are disabled.")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    width, height = size
    aspect = "9:16" if height > width else "16:9"
    response = SESSION.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "model": GEMINI_MODEL,
            "input": prompt,
            "response_format": {
                "type": "image",
                "mime_type": "image/png",
                "aspect_ratio": aspect,
                "image_size": "1K",
            },
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    image_item = data.get("output_image")
    if isinstance(image_item, dict) and image_item.get("data"):
        raw = base64.b64decode(image_item["data"], validate=True)
        save_image_bytes(raw, destination, size)
        return

    for item in data.get("output", []):
        if isinstance(item, dict) and item.get("type") == "image" and item.get("data"):
            raw = base64.b64decode(item["data"], validate=True)
            save_image_bytes(raw, destination, size)
            return

    raise ValueError("Gemini response contained no image bytes.")


def generate_replicate(prompt, destination, size):
    if not ALLOW_BILLABLE_PROVIDERS:
        raise RuntimeError("Paid image providers are disabled.")
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not configured.")

    parts = REPLICATE_MODEL.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise RuntimeError("REPLICATE_IMAGE_MODEL must be owner/model-name.")

    owner, model = parts
    width, height = size
    response = SESSION.post(
        f"https://api.replicate.com/v1/models/{owner}/{model}/predictions",
        headers={
            "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={"input": {
            "prompt": prompt,
            "aspect_ratio": "9:16" if height > width else "16:9",
            "output_format": "png",
        }},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    prediction = response.json()
    deadline = time.monotonic() + REQUEST_TIMEOUT

    while prediction.get("status") in ("starting", "processing"):
        if time.monotonic() >= deadline:
            raise TimeoutError("Replicate prediction timed out.")

        time.sleep(2)
        poll_url = prediction.get("urls", {}).get("get")
        if not poll_url:
            prediction_id = prediction.get("id")
            if not prediction_id:
                raise ValueError("Replicate returned no prediction ID.")
            poll_url = "https://api.replicate.com/v1/predictions/" + quote(str(prediction_id))

        check = SESSION.get(
            poll_url,
            headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"},
            timeout=30,
        )
        check.raise_for_status()
        prediction = check.json()

    if prediction.get("status") != "succeeded":
        raise RuntimeError(
            "Replicate failed: " + str(prediction.get("error") or prediction.get("status"))
        )

    output = prediction.get("output")
    if isinstance(output, str):
        output = [output]
    elif isinstance(output, dict):
        output = list(output.values())

    image_url = next(
        (item for item in (output or [])
         if isinstance(item, str) and item.startswith(("https://", "http://"))),
        None,
    )
    if not image_url:
        raise ValueError("Replicate output contained no image URL.")

    image_response = SESSION.get(image_url, timeout=REQUEST_TIMEOUT)
    image_response.raise_for_status()
    if not image_response.headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError("Replicate output URL did not return an image.")
    save_image_bytes(image_response.content, destination, size)


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)
    jobs, job_document = get_jobs()
    mode = job_document.get("format", "unknown")
    old_rows = get_old_rows()

    records = []
    pending = []

    for index, job in enumerate(jobs, 1):
        number = get_number(job, index)
        size = get_size(job)
        destination = get_path(job, number)
        digest = get_hash(job)
        previous = old_rows.get(number, {})

        record = {
            "scene": number,
            "global_scene": number,
            "file": destination.relative_to(ROOT).as_posix(),
            "width": size[0],
            "height": size[1],
            "status": "pending",
            "provider": None,
            "prompt_sha256": digest,
            "attempts": [],
        }

        if previous.get("prompt_sha256") == digest and valid_image(destination, size):
            record["status"] = "existing"
            record["provider"] = previous.get("provider") or "previous run"
        else:
            if destination.exists() and not valid_image(destination, size):
                destination.unlink(missing_ok=True)
            pending.append((job, record, destination, size))

        records.append(record)

    manifest = {
        "version": "photo-motion-safe-8.0",
        "policy": {
            "paid_providers_enabled": ALLOW_BILLABLE_PROVIDERS,
            "opt_in_required": PAID_OPT_IN,
            "note": "Free-tier services may still have quotas, limits or changing terms.",
        },
        "total_scenes": len(records),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(r["status"] in ("existing", "generated") for r in records),
            "failed": sum(r["status"] == "failed" for r in records),
            "pending": sum(r["status"] == "pending" for r in records),
        }
        save_json(GENERATION_MANIFEST, manifest)

        rows = []
        for job, record in zip(jobs, records):
            try:
                duration = float(job.get("duration", 5.0))
            except (TypeError, ValueError):
                duration = 5.0

            rows.append({
                "scene": record["scene"],
                "global_scene": record["scene"],
                "part": job.get("part", 1),
                "local_scene": job.get("local_scene", record["scene"]),
                "image": record["file"],
                "image_path": record["file"],
                "duration": duration,
                "status": record["status"],
                "provider": record.get("provider"),
                "prompt_sha256": record["prompt_sha256"],
            })

        save_json(IMAGE_MANIFEST, {
            "version": "photo-motion-safe-8.0",
            "format": mode,
            "total_scenes": len(rows),
            "images_ready": sum(r["status"] in ("existing", "generated") for r in rows),
            "images_required": len(rows),
            "scenes": rows,
        })

    checkpoint()

    log("=" * 60)
    log("KATHA LOK AI — SAFE IMAGE GENERATION")
    log("Total scenes:", len(jobs))
    log("Already ready:", len(jobs) - len(pending))
    log("Pending:", len(pending))
    log("Configured Hugging Face Spaces:", HF_SPACES)
    log("Pollinations legacy models:", POLLINATIONS_MODELS)
    log("Paid providers explicitly enabled:", ALLOW_BILLABLE_PROVIDERS)
    if not ALLOW_BILLABLE_PROVIDERS:
        log("Gemini Image and Replicate are blocked regardless of API key presence.")
        log("Authenticated Pollinations endpoint is not used.")
    log("=" * 60)

    if not pending:
        log("All scene images are ready; no provider calls required.")
        return

    blocked_hf = set()
    blocked_pollinations = set()
    blocked_gemini = False
    blocked_replicate = False

    for job, record, destination, size in pending:
        prompt = prompt_for(job)
        seed = BASE_SEED + record["scene"]
        success = False
        last_error = "No configured provider succeeded."
        providers = []

        for space in HF_SPACES:
            if space not in blocked_hf:
                providers.append((
                    f"Hugging Face: {space}",
                    lambda space=space: generate_hf(
                        prompt, destination, size, seed, space
                    ),
                    lambda space=space: blocked_hf.add(space),
                    "hf",
                ))

        for model in POLLINATIONS_MODELS:
            if model not in blocked_pollinations:
                providers.append((
                    f"Pollinations legacy: {model}",
                    lambda model=model: generate_pollinations(
                        prompt, destination, size, seed, model
                    ),
                    lambda model=model: blocked_pollinations.add(model),
                    "pollinations",
                ))

        if ALLOW_BILLABLE_PROVIDERS and GEMINI_API_KEY and not blocked_gemini:
            providers.append((
                f"Gemini Image: {GEMINI_MODEL}",
                lambda: generate_gemini(prompt, destination, size),
                lambda: None,
                "gemini",
            ))

        if ALLOW_BILLABLE_PROVIDERS and REPLICATE_API_TOKEN and not blocked_replicate:
            providers.append((
                f"Replicate: {REPLICATE_MODEL}",
                lambda: generate_replicate(prompt, destination, size),
                lambda: None,
                "replicate",
            ))

        if ALLOW_BILLABLE_PROVIDERS and POLLINATIONS_API_KEY:
            # Authenticated Pollinations requests are only allowed with explicit opt-in.
            def authenticated_pollinations(model=POLLINATIONS_MODELS[0] if POLLINATIONS_MODELS else "flux"):
                url = "https://gen.pollinations.ai/image/" + quote(prompt, safe="")
                response = SESSION.get(
                    url,
                    params={
                        "model": model,
                        "width": size[0],
                        "height": size[1],
                        "seed": seed,
                        "nologo": "true",
                    },
                    headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"},
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                if not response.headers.get("content-type", "").lower().startswith("image/"):
                    raise ValueError("Authenticated Pollinations returned non-image content.")
                save_image_bytes(response.content, destination, size)

            providers.append((
                "Pollinations authenticated",
                authenticated_pollinations,
                lambda: None,
                "pollinations_auth",
            ))

        for provider_name, generate, block_provider, provider_type in providers:
            if success:
                break

            try:
                log(f"Scene {record['scene']}: trying {provider_name}")
                generate()
                check_image(destination, size)

                record.update(status="generated", provider=provider_name)
                record["attempts"].append({
                    "provider": provider_name,
                    "status": "success",
                })
                success = True
                checkpoint()
                break

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log("PROVIDER FAILED:", provider_name, last_error[:240])
                record["attempts"].append({
                    "provider": provider_name,
                    "status": "failed",
                    "error": last_error[:500],
                })

                if is_access_or_quota_error(last_error):
                    block_provider()
                    if provider_type == "gemini":
                        blocked_gemini = True
                    elif provider_type == "replicate":
                        blocked_replicate = True

                # Do not repeat a provider for the same scene.
                # Continue to the next fallback provider.

        if not success:
            record["status"] = "failed"
            record["error"] = last_error[:800]
            log(f"Scene {record['scene']} failed; earlier checkpoints preserved.")

        checkpoint()

    failed = [r["scene"] for r in records if r["status"] == "failed"]
    ready = sum(r["status"] in ("existing", "generated") for r in records)

    log("=" * 60)
    log(f"Images ready: {ready}/{len(records)}")
    log("Failed scenes:", failed)
    log("Image manifest:", IMAGE_MANIFEST)
    log("Generation manifest:", GENERATION_MANIFEST)
    log("=" * 60)

    if failed:
        raise RuntimeError(
            f"Image generation failed for scenes {failed}; "
            "successful checkpoints were preserved."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("IMAGE GENERATION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"IMAGE GENERATION ERROR: {exc}", file=sys.stderr)
        sys.exit(1)