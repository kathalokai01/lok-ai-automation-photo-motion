
#!/usr/bin/env python3
"""Katha Lok AI: resumable multi-provider scene image fallback."""

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

HF_SPACE = "mrfakename/Z-Image-Turbo"
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
REPLICATE_KEY = os.getenv("REPLICATE_API_TOKEN", "").strip()

# Billable or uncertain services remain disabled unless explicitly enabled.
ALLOW_BILLABLE = (
    os.getenv("ALLOW_BILLABLE_PROVIDERS", "false").strip().lower()
    in {"true", "1", "yes"}
)

BASE_SEED = 20261009
MAX_ATTEMPTS = 2
TIMEOUT = 150

QUALITY = """
Photorealistic live-action cinematic photography.
Realistic Indian people, locations, clothing and architecture.
Natural skin texture, believable anatomy, realistic lighting and shadows.
Preserve recurring character appearance and clothing.
No cartoon, anime, illustration, painting, CGI, plastic skin,
extra limbs, distorted faces, text, logos or watermarks.
"""

session = requests.Session()


def log(message):
    print(message, flush=True)


def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def image_size(job):
    value = str(job.get("image_size", "720x1280")).lower()
    try:
        width, height = map(int, value.split("x", 1))
        if width >= 256 and height >= 256:
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
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def save_pil(image, destination, size):
    """Save a verified RGB/RGBA image at the required scene dimensions."""
    if not isinstance(image, Image.Image):
        raise ValueError("Provider returned no PIL image")

    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp.png")
    image.save(temp, format="PNG", optimize=True)

    if not valid_image(temp):
        temp.unlink(missing_ok=True)
        raise ValueError("Saved image failed file validation")

    temp.replace(destination)


def save_bytes(data, destination, size):
    with Image.open(BytesIO(data)) as im:
        im.load()
        save_pil(im, destination, size)


def download_image(url, destination, size, headers=None):
    response = session.get(
        url, headers=headers or {}, timeout=TIMEOUT
    )
    response.raise_for_status()

    if not response.headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError(
            f"Expected image response, got "
            f"{response.headers.get('content-type', 'unknown')}"
        )

    save_bytes(response.content, destination, size)


def prompt_for(job):
    prompt = ""
    for key in ("prompt", "image_prompt", "visual_prompt", "scene_prompt"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            prompt = value.strip()
            break

    if not prompt:
        prompt = str(job.get("description", "A scene from an Indian story"))

    negative = str(job.get("negative_prompt", "")).strip()
    return f"{prompt}\n\n{QUALITY}\n\nAvoid: {negative}"


def generate_huggingface_space(prompt, destination, size, seed):
    if Client is None:
        raise RuntimeError("gradio_client dependency is missing")

    width, height = size
    kwargs = {"verbose": False}
    if HF_TOKEN:
        kwargs["token"] = HF_TOKEN

    client = Client(HF_SPACE, **kwargs)
    result = client.predict(
        prompt,
        height,
        width,
        9,
        seed,
        False,
        api_name="/generate_image",
    )

    # This Space has previously returned an image path in a tuple/list.
    if isinstance(result, (tuple, list)):
        if not result:
            raise ValueError("Space returned an empty result")
        result = result[0]

    if isinstance(result, dict):
        for key in ("path", "filepath", "file"):
            value = result.get(key)
            if value and Path(str(value)).is_file():
                with Image.open(str(value)) as im:
                    save_pil(im, destination, size)
                return
        result = result.get("url")

    if isinstance(result, Image.Image):
        save_pil(result, destination, size)
        return

    if isinstance(result, str):
        source = Path(result)
        if source.is_file():
            with Image.open(source) as im:
                save_pil(im, destination, size)
            return
        if result.startswith(("http://", "https://")):
            download_image(result, destination, size)
            return

    raise ValueError("Space returned no usable image file or URL")


def generate_gemini_image(prompt, destination, size, model):
    """Gemini image API. Requires explicit opt-in because billing may apply."""
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

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
                raw = base64.b64decode(inline["data"])
                save_bytes(raw, destination, size)
                return

    raise ValueError(
        "Gemini returned no image data: "
        + json.dumps(data, ensure_ascii=False)[:500]
    )


def generate_pollinations(prompt, destination, size, seed):
    if not POLLINATIONS_KEY:
        raise RuntimeError("POLLINATIONS_API_KEY is not configured")

    width, height = size
    url = "https://gen.pollinations.ai/image/" + quote(prompt, safe="")
    response = session.get(
        url,
        params={
            "model": "flux",
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
            "Pollinations returned non-image content: "
            + response.text[:300]
        )

    save_bytes(response.content, destination, size)


def generate_hf_inference(prompt, destination, size, seed):
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is not configured")

    try:
        from huggingface_hub import InferenceClient
    except ImportError as exc:
        raise RuntimeError("huggingface_hub dependency is missing") from exc

    width, height = size
    client = InferenceClient(
        provider="auto",
        api_key=HF_TOKEN,
        timeout=TIMEOUT,
    )
    image = client.text_to_image(
        prompt=prompt,
        model="black-forest-labs/FLUX.1-Krea-dev",
        width=width,
        height=height,
        seed=seed,
    )
    save_pil(image, destination, size)


def generate_replicate(prompt, destination, size, seed):
    if not REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_TOKEN is not configured")

    width, height = size
    response = session.post(
        "https://api.replicate.com/v1/models/"
        "black-forest-labs/flux-schnell/predictions",
        headers={
            "Authorization": f"Bearer {REPLICATE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={
            "input": {
                "prompt": prompt,
                "width": width,
                "height": height,
                "seed": seed,
                "num_outputs": 1,
                "output_format": "png",
            }
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    # Poll asynchronous predictions, if the model takes longer than the wait.
    for _ in range(18):
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
            "Replicate prediction status: "
            + str(data.get("status", "unknown"))
            + " "
            + str(data.get("error", ""))[:300]
        )

    output = data.get("output")
    if isinstance(output, list):
        output = output[0] if output else None
    if not isinstance(output, str):
        raise ValueError("Replicate returned no image URL")

    download_image(output, destination, size)


def provider_chain():
    """Ordered fallbacks. Unsupported UI-only/video-only options are logged."""
    providers = [
        {
            "name": "Hugging Face ZeroGPU Space",
            "run": generate_huggingface_space,
            "enabled": True,
            "reason": "",
        },
        {
            "name": "Gemini Image API",
            "run": lambda p, d, s, seed: generate_gemini_image(
                p, d, s, "gemini-3.1-flash-image"
            ),
            "enabled": ALLOW_BILLABLE and bool(GEMINI_KEY),
            "reason": "Paid/uncertain API disabled or GEMINI_API_KEY missing",
        },
        {
            "name": "Google Flow",
            "run": None,
            "enabled": False,
            "reason": "No supported official GitHub Actions image-generation API verified",
        },
        {
            "name": "Nano Banana 2.1",
            "run": lambda p, d, s, seed: generate_gemini_image(
                p, d, s, "gemini-nano-banana-2.1"
            ),
            "enabled": ALLOW_BILLABLE and bool(GEMINI_KEY),
            "reason": "Paid/uncertain API disabled or GEMINI_API_KEY missing",
        },
        {
            "name": "Veo",
            "run": None,
            "enabled": False,
            "reason": "Video-generation API, not a still-image fallback; billing not enabled here",
        },
        {
            "name": "Pollinations",
            "run": generate_pollinations,
            "enabled": ALLOW_BILLABLE and bool(POLLINATIONS_KEY),
            "reason": "Paid/uncertain API disabled or POLLINATIONS_API_KEY missing",
        },
        {
            "name": "Hugging Face Inference Providers",
            "run": generate_hf_inference,
            "enabled": ALLOW_BILLABLE and bool(HF_TOKEN),
            "reason": "May incur provider charges; disabled or HF_TOKEN missing",
        },
        {
            "name": "Replicate FLUX Schnell",
            "run": generate_replicate,
            "enabled": ALLOW_BILLABLE and bool(REPLICATE_KEY),
            "reason": "Paid service disabled or REPLICATE_API_TOKEN missing",
        },
        {
            "name": "NVIDIA Cosmos",
            "run": None,
            "enabled": False,
            "reason": "No verified general-purpose still-image API endpoint configured",
        },
    ]
    return providers


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    if not JOBS_FILE.is_file():
        raise SystemExit(f"ERROR: Missing jobs file: {JOBS_FILE}")

    data = load_json(JOBS_FILE)
    jobs = data if isinstance(data, list) else (
        data.get("jobs") or data.get("visual_jobs") or data.get("scenes") or []
    )
    if not isinstance(jobs, list) or not jobs:
        raise SystemExit("ERROR: visual_jobs.json contains no scene jobs")

    # Build resumable records. Existing valid images are never deleted.
    records = []
    pending = []

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise SystemExit(f"ERROR: Invalid scene job at index {index}")

        number = job.get("global_scene", job.get("scene", index))
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = index

        destination = image_path(job, number)
        size = image_size(job)
        record = {
            "scene": number,
            "part": job.get("part"),
            "local_scene": job.get("local_scene"),
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
        "version": "fallback-1.0",
        "provider_order": [],
        "allow_billable_providers": ALLOW_BILLABLE,
        "total_scenes": len(jobs),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(
                r["status"] in ("existing", "generated")
                for r in records
            ),
            "pending": sum(r["status"] == "pending" for r in records),
            "failed": sum(r["status"] == "failed" for r in records),
        }
        save_json(MANIFEST_FILE, manifest)

    checkpoint()

    log("=" * 68)
    log("KATHA LOK AI — IMAGE PROVIDER FALLBACK")
    log(f"Total scenes: {len(jobs)}")
    log(f"Already ready: {len(jobs) - len(pending)}")
    log(f"Billable/uncertain providers enabled: {ALLOW_BILLABLE}")
    log("=" * 68)

    for provider in provider_chain():
        name = provider["name"]
        manifest["provider_order"].append(name)

        if not pending:
            break

        if not provider["enabled"] or provider["run"] is None:
            log(f"\nSKIPPED: {name} — {provider['reason']}")
            for _, record, _, _ in pending:
                record["attempts"].append({
                    "provider": name,
                    "status": "skipped",
                    "reason": provider["reason"],
                })
            checkpoint()
            continue

        log(f"\nPROVIDER: {name}")
        still_pending = []
        consecutive_errors = 0
        provider_blocked = False

        for job, record, destination, size in pending:
            prompt = prompt_for(job)
            seed = BASE_SEED + int(record["scene"])
            success = False
            last_error = ""

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    log(
                        f"Scene {record['scene']}: "
                        f"attempt {attempt}/{MAX_ATTEMPTS}"
                    )
                    provider["run"](prompt, destination, size, seed)

                    if not valid_image(destination):
                        raise ValueError("Image file failed validation")

                    record["status"] = "generated"
                    record["provider"] = name
                    record["attempts"].append({
                        "provider": name,
                        "status": "success",
                        "attempt": attempt,
                    })
                    log(f"SAVED: {destination.relative_to(ROOT)}")
                    success = True
                    consecutive_errors = 0
                    checkpoint()
                    break

                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(f"Provider error: {last_error[:350]}")
                    time.sleep(2)

            if success:
                continue

            # Do not retain corrupt/partial outputs as successful images.
            if destination.exists() and not valid_image(destination):
                destination.unlink(missing_ok=True)

            record["status"] = "pending"
            record["attempts"].append({
                "provider": name,
                "status": "failed",
                "error": last_error[:800],
            })
            still_pending.append((job, record, destination, size))
            checkpoint()

            consecutive_errors += 1

            # A quota, authentication, billing or provider outage often affects
            # every remaining scene. Stop wasting retries and switch provider.
            error_lower = last_error.lower()
            if any(term in error_lower for term in (
                "quota", "exceeded", "429", "401", "403", "402",
                "payment", "billing", "unauthorized", "forbidden",
                "zero gpu", "zero-gpu",
            )):
                provider_blocked = True
                log(f"Provider appears unavailable/quota-limited: {name}")
                break

            if consecutive_errors >= 2:
                provider_blocked = True
                log(f"Provider repeatedly failed; moving on: {name}")
                break

        if provider_blocked:
            # Unprocessed scenes remain pending for the next provider.
            processed_ids = {id(item[1]) for item in still_pending}
            for item in pending:
                if id(item[1]) not in processed_ids and item[1]["status"] == "pending":
                    still_pending.append(item)

        pending = still_pending
        checkpoint()
        log(f"Remaining scenes after {name}: {len(pending)}")

    # Final status and manifest are saved even when some scenes could not be made.
    for _, record, _, _ in pending:
        record["status"] = "failed"

    checkpoint()

    ready = sum(r["status"] in ("existing", "generated") for r in records)
    failed = [r["scene"] for r in records if r["status"] == "failed"]

    log("\nFINAL IMAGE SUMMARY")
    log(f"Ready: {ready}/{len(records)}")
    log(f"Failed scenes: {failed}")
    log(f"Manifest: {MANIFEST_FILE}")

    if failed:
        raise SystemExit(
            "ERROR: All configured fallbacks exhausted. "
            f"Images still missing for scenes: {failed}"
        )

    log("ALL SCENE IMAGES READY.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IMAGE FALLBACK FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

Commit message:

"Add resumable multi-provider image generation fallback"