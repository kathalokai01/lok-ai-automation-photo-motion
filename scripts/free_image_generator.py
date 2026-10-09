#!/usr/bin/env python3
"""Katha Lok AI - resumable scene image fallback."""

from __future__ import annotations

import base64
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
VISUAL_DIR = ROOT / "output/visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_generation_manifest.json"

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
REPLICATE_KEY = os.getenv("REPLICATE_API_TOKEN", "").strip()

ALLOW_BILLABLE = (
    os.getenv("ALLOW_BILLABLE_PROVIDERS", "false").strip().lower()
    in {"true", "1", "yes"}
)

TIMEOUT = 150
MAX_ATTEMPTS = 2
BASE_SEED = 20261009

QUALITY = """
Photorealistic live-action cinematic photography.
Realistic Indian people, clothing, locations and architecture.
Natural skin texture, believable anatomy, realistic lighting.
Consistent recurring characters and clothing.
No cartoon, anime, illustration, CGI, plastic skin,
distorted faces, extra limbs, text, logos or watermarks.
"""

session = requests.Session()


def log(message):
    print(message, flush=True)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def image_size(job):
    try:
        width, height = map(
            int, str(job.get("image_size", "720x1280")).lower().split("x", 1)
        )
        if 256 <= width <= 2048 and 256 <= height <= 2048:
            return width, height
    except (ValueError, TypeError):
        pass
    return 720, 1280


def image_path(job, index):
    supplied = job.get("image_path")
    if supplied:
        candidate = (ROOT / str(supplied)).resolve()
        if candidate.is_relative_to(ROOT):
            return candidate
    return VISUAL_DIR / f"scene_{index:02d}.png"


def valid_image(path):
    try:
        if not path.is_file() or path.stat().st_size < 5000:
            return False
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def save_pil(image, destination, size):
    if not isinstance(image, Image.Image):
        raise ValueError("Provider did not return a PIL image")

    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp.png")
    image.save(temp, "PNG", optimize=True)

    if not valid_image(temp):
        temp.unlink(missing_ok=True)
        raise ValueError("Generated image failed validation")

    temp.replace(destination)


def save_bytes(raw, destination, size):
    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_pil(image, destination, size)


def download_image(url, destination, size, headers=None):
    response = session.get(
        url, headers=headers or {}, timeout=TIMEOUT
    )
    response.raise_for_status()

    if not response.headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError("Provider returned non-image content")

    save_bytes(response.content, destination, size)


def prompt_for(job):
    prompt = next(
        (
            str(job[key]).strip()
            for key in (
                "prompt",
                "image_prompt",
                "visual_prompt",
                "scene_prompt",
                "description",
            )
            if job.get(key)
        ),
        "A realistic scene from an Indian story",
    )
    negative = str(job.get("negative_prompt", "")).strip()
    return f"{prompt}\n\n{QUALITY}\n\nAvoid: {negative}"


# 1. Hugging Face ZeroGPU Space
def generate_hf_space(prompt, destination, size, seed):
    if Client is None:
        raise RuntimeError("gradio_client is not installed")

    kwargs = {"verbose": False}
    if HF_TOKEN:
        kwargs["token"] = HF_TOKEN

    client = Client("mrfakename/Z-Image-Turbo", **kwargs)
    width, height = size

    result = client.predict(
        prompt,
        height,
        width,
        9,
        seed,
        False,
        api_name="/generate_image",
    )

    if isinstance(result, (tuple, list)):
        if not result:
            raise ValueError("Space returned an empty result")
        result = result[0]

    if isinstance(result, dict):
        local = next(
            (
                result[key]
                for key in ("path", "filepath", "file")
                if result.get(key)
            ),
            None,
        )
        if local and Path(str(local)).is_file():
            with Image.open(str(local)) as image:
                save_pil(image, destination, size)
            return
        result = result.get("url")

    if isinstance(result, Image.Image):
        save_pil(result, destination, size)
        return

    if isinstance(result, str):
        if Path(result).is_file():
            with Image.open(result) as image:
                save_pil(image, destination, size)
            return
        if result.startswith(("https://", "http://")):
            download_image(result, destination, size)
            return

    raise ValueError("Space returned no usable image")


# 2. Gemini image endpoint; disabled by default
def generate_gemini(prompt, destination, size, seed, model):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing")

    width, height = size
    ratio = "9:16" if height > width else "16:9"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + model
        + ":generateContent"
    )

    response = session.post(
        url,
        headers={
            "x-goog-api-key": GEMINI_KEY,
            "Content-Type": "application/json",
        },
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": ratio},
            },
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                save_bytes(
                    base64.b64decode(inline["data"]),
                    destination,
                    size,
                )
                return

    raise ValueError("Gemini response contained no image data")


# 3. Pollinations model adapter
def generate_pollinations(prompt, destination, size, seed, model):
    if not POLLINATIONS_KEY:
        raise RuntimeError("POLLINATIONS_API_KEY is missing")

    width, height = size
    url = "https://gen.pollinations.ai/image/" + quote(prompt, safe="")

    response = session.get(
        url,
        params={
            "model": model,
            "width": width,
            "height": height,
            "seed": seed,
        },
        headers={"Authorization": f"Bearer {POLLINATIONS_KEY}"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    if not response.headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError(
            "Pollinations returned non-image response: "
            + response.text[:250]
        )

    save_bytes(response.content, destination, size)


# 4. Hugging Face Inference Providers
def generate_hf_inference(prompt, destination, size, seed, model):
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is missing")

    from huggingface_hub import InferenceClient

    width, height = size
    client = InferenceClient(
        provider="auto",
        api_key=HF_TOKEN,
        timeout=TIMEOUT,
    )
    image = client.text_to_image(
        prompt=prompt,
        model=model,
        width=width,
        height=height,
        seed=seed,
    )
    save_pil(image, destination, size)


# 5. Replicate model adapter
def generate_replicate(prompt, destination, size, seed, model):
    if not REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_TOKEN is missing")

    width, height = size
    owner, model_name = model.split("/", 1)

    inputs = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "seed": seed,
        "num_outputs": 1,
        "output_format": "png",
    }

    # Imagen and Ideogram models can use different input schemas.
    if model == "google/imagen-4":
        inputs = {
            "prompt": prompt,
            "aspect_ratio": "9:16" if height > width else "16:9",
        }
    elif model == "ideogram-ai/ideogram-v3-turbo":
        inputs = {
            "prompt": prompt,
            "aspect_ratio": "9:16" if height > width else "16:9",
            "magic_prompt_option": "AUTO",
        }
    elif "flux-kontext" in model:
        inputs = {
            "prompt": prompt,
            "aspect_ratio": "9:16" if height > width else "16:9",
            "output_format": "png",
        }

    response = session.post(
        f"https://api.replicate.com/v1/models/{owner}/{model_name}/predictions",
        headers={
            "Authorization": f"Bearer {REPLICATE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={"input": inputs},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    for _ in range(24):
        status = data.get("status")

        if status in ("succeeded", "failed", "canceled"):
            break

        poll_url = data.get("urls", {}).get("get")
        if not poll_url:
            break

        time.sleep(5)
        poll = session.get(
            poll_url,
            headers={"Authorization": f"Bearer {REPLICATE_KEY}"},
            timeout=TIMEOUT,
        )
        poll.raise_for_status()
        data = poll.json()

    if data.get("status") != "succeeded":
        raise RuntimeError(
            f"Replicate status={data.get('status')}; "
            f"error={str(data.get('error', ''))[:250]}"
        )

    output = data.get("output")
    if isinstance(output, list):
        output = output[0] if output else None

    if isinstance(output, dict):
        output = output.get("url")

    if not isinstance(output, str):
        raise ValueError("Replicate returned no image URL")

    download_image(output, destination, size)


def provider_chain():
    """Provider order. Model names are attempted individually."""

    providers = [
        {
            "name": "Hugging Face ZeroGPU: Z-Image-Turbo",
            "run": generate_hf_space,
            "enabled": True,
            "reason": "",
        },
        {
            "name": "Gemini 3.1 Flash Image",
            "run": lambda p, d, s, seed: generate_gemini(
                p, d, s, seed, "gemini-3.1-flash-image"
            ),
            "enabled": ALLOW_BILLABLE and bool(GEMINI_KEY),
            "reason": "Billable providers disabled or Gemini key missing",
        },
        {
            "name": "Nano Banana 2.1",
            "run": lambda p, d, s, seed: generate_gemini(
                p, d, s, seed, "gemini-nano-banana-2.1"
            ),
            "enabled": ALLOW_BILLABLE and bool(GEMINI_KEY),
            "reason": "API model ID unverified, or billing disabled/key missing",
        },
    ]

    # User-listed Pollinations models. The API may reject unsupported IDs.
    pollinations_models = [
        "black-forest-labs/flux.2-flex",
        "google/gemini-3.1-flash-image",
        "google/gemini-nano-banana-2.1",
        "bytedance/seedream-5.0-flash",
        "qwen/qwen-image-2.1",
        "black-forest-labs/flux.1-schnell",
    ]

    for model in pollinations_models:
        providers.append({
            "name": f"Pollinations: {model}",
            "run": lambda p, d, s, seed, m=model:
                generate_pollinations(p, d, s, seed, m),
            "enabled": ALLOW_BILLABLE and bool(POLLINATIONS_KEY),
            "reason": "Billing disabled, key missing, or model may be unsupported",
        })

    # HF inference providers may require credits or provider-specific access.
    for model in [
        "black-forest-labs/FLUX.1-Krea-dev",
        "Qwen/Qwen-Image",
    ]:
        providers.append({
            "name": f"Hugging Face Inference: {model}",
            "run": lambda p, d, s, seed, m=model:
                generate_hf_inference(p, d, s, seed, m),
            "enabled": ALLOW_BILLABLE and bool(HF_TOKEN),
            "reason": "Billing disabled or HF_TOKEN missing",
        })

    # User-listed Replicate models.
    replicate_models = [
        "google/imagen-4",
        "black-forest-labs/flux-kontext-pro",
        "ideogram-ai/ideogram-v3-turbo",
        "black-forest-labs/flux-1.1-pro",
        "black-forest-labs/flux-dev",
        "black-forest-labs/flux-schnell",
    ]

    for model in replicate_models:
        providers.append({
            "name": f"Replicate: {model}",
            "run": lambda p, d, s, seed, m=model:
                generate_replicate(p, d, s, seed, m),
            "enabled": ALLOW_BILLABLE and bool(REPLICATE_KEY),
            "reason": "Billing disabled, key missing, or model schema unsupported",
        })

    # These are intentionally not falsely presented as working image APIs.
    for name, reason in [
        ("Google Flow", "No verified supported automation API configured"),
        ("Veo", "Video model, not a still-image endpoint in this script"),
        ("NVIDIA Cosmos", "No verified still-image endpoint configured"),
    ]:
        providers.append({
            "name": name,
            "run": None,
            "enabled": False,
            "reason": reason,
        })

    return providers


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    if not JOBS_FILE.is_file():
        raise SystemExit(f"Missing scene jobs file: {JOBS_FILE}")

    data = load_json(JOBS_FILE)
    jobs = data if isinstance(data, list) else (
        data.get("jobs") or data.get("visual_jobs") or data.get("scenes") or []
    )

    if not isinstance(jobs, list) or not jobs:
        raise SystemExit("visual_jobs.json contains no scene jobs")

    records = []
    pending = []

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise SystemExit(f"Invalid scene job at index {index}")

        try:
            number = int(job.get("global_scene", job.get("scene", index)))
        except (TypeError, ValueError):
            number = index

        destination = image_path(job, number)
        size = image_size(job)

        record = {
            "scene": number,
            "file": str(destination.relative_to(ROOT)),
            "width": size[0],
            "height": size[1],
            "status": "pending",
            "provider": None,
            "attempts": [],
        }

        if valid_image(destination):
            record["status"] = "existing"
            record["provider"] = "previous run"
        else:
            pending.append((job, record, destination, size))

        records.append(record)

    manifest = {
        "version": "fallback-2.0",
        "allow_billable_providers": ALLOW_BILLABLE,
        "provider_order": [],
        "total_scenes": len(jobs),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(
                r["status"] in ("existing", "generated") for r in records
            ),
            "pending": sum(r["status"] == "pending" for r in records),
            "failed": sum(r["status"] == "failed" for r in records),
        }
        save_json(MANIFEST_FILE, manifest)

    checkpoint()

    log("=" * 65)
    log("KATHA LOK AI - ORDERED IMAGE FALLBACK")
    log(f"Total scenes: {len(jobs)}")
    log(f"Already ready: {len(jobs) - len(pending)}")
    log(f"Billable providers enabled: {ALLOW_BILLABLE}")
    log("=" * 65)

    for provider in provider_chain():
        if not pending:
            break

        name = provider["name"]
        manifest["provider_order"].append(name)

        if not provider["enabled"] or provider["run"] is None:
            log(f"SKIPPED: {name} - {provider['reason']}")
            for _, record, _, _ in pending:
                record["attempts"].append({
                    "provider": name,
                    "status": "skipped",
                    "reason": provider["reason"],
                })
            checkpoint()
            continue

        log(f"\nPROVIDER: {name}")
        remaining = []

        for job, record, destination, size in pending:
            success = False
            last_error = ""
            prompt = prompt_for(job)
            seed = BASE_SEED + int(record["scene"])

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    log(
                        f"Scene {record['scene']}: "
                        f"attempt {attempt}/{MAX_ATTEMPTS}"
                    )

                    provider["run"](prompt, destination, size, seed)

                    if not valid_image(destination):
                        raise ValueError("Image failed validation")

                    record["status"] = "generated"
                    record["provider"] = name
                    record["attempts"].append({
                        "provider": name,
                        "status": "success",
                        "attempt": attempt,
                    })
                    log(f"SAVED: {record['file']}")
                    success = True
                    checkpoint()
                    break

                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(f"FAILED: {last_error[:300]}")

                    if destination.exists() and not valid_image(destination):
                        destination.unlink(missing_ok=True)

                    # Quota/auth/billing failures generally affect the whole
                    # provider, so stop retrying this provider for this scene.
                    lower = last_error.lower()
                    if any(token in lower for token in (
                        "quota", "429", "401", "403", "402",
                        "payment", "billing", "unauthorized",
                        "forbidden", "zero gpu", "zero-gpu",
                    )):
                        break

                    if attempt < MAX_ATTEMPTS:
                        time.sleep(2)

            if not success:
                record["status"] = "pending"
                record["attempts"].append({
                    "provider": name,
                    "status": "failed",
                    "error": last_error[:800],
                })
                remaining.append((job, record, destination, size))
                checkpoint()

        pending = remaining
        log(f"Scenes still needing images: {len(pending)}")

    for _, record, _, _ in pending:
        record["status"] = "failed"

    checkpoint()

    ready = sum(
        r["status"] in ("existing", "generated") for r in records
    )
    failed = [r["scene"] for r in records if r["status"] == "failed"]

    log("\nFINAL SUMMARY")
    log(f"Images ready: {ready}/{len(records)}")
    log(f"Missing scenes: {failed}")
    log(f"Manifest: {MANIFEST_FILE}")

    if failed:
        raise SystemExit(
            "Image fallbacks exhausted. Missing scene images: "
            + str(failed)
        )

    log("ALL SCENE IMAGES READY")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IMAGE GENERATION ERROR: {exc}", file=sys.stderr)
        sys.exit(1)