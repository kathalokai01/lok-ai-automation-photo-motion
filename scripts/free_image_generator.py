
#!/usr/bin/env python3
"""Katha Lok AI photo-motion image generation with provider fallback."""

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

GEMINI_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"
).strip()

REPLICATE_MODEL = os.getenv(
    "REPLICATE_IMAGE_MODEL", "black-forest-labs/flux-schnell"
).strip()

POLLINATIONS_MODELS = [
    x.strip()
    for x in os.getenv("POLLINATIONS_FREE_IMAGE_MODELS", "").split(",")
    if x.strip()
]

HF_SPACES = list(dict.fromkeys([
    os.getenv("HF_IMAGE_SPACE", "mrfakename/Z-Image-Turbo").strip(),
    *[
        x.strip()
        for x in os.getenv("FREE_IMAGE_FALLBACK_SPACES", "").split(",")
        if x.strip()
    ],
]))

HF_SPACES = [x for x in HF_SPACES if x]

MAX_ATTEMPTS = 2
REQUEST_TIMEOUT = 150
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def get_jobs():
    document = read_json(JOBS_FILE)

    if isinstance(document, list):
        jobs = document
        metadata = {}
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
    for i, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Job {i} must be an object.")

        number = int(job.get("global_scene", job.get("scene", i)))
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
    raw = job.get("image_path") or (
        f"output/visuals/scene_{number:02d}.png"
    )
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
        with Image.open(path) as im:
            im.verify()
        if size:
            with Image.open(path) as im:
                if im.size != size:
                    return False
        return True
    except Exception:
        return False


def save_image(image, destination, size):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image, size, method=Image.Resampling.LANCZOS
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".tmp.png")
    try:
        image.save(tmp, "PNG", optimize=True)
        if not valid_image(tmp, size):
            raise ValueError("Image failed PNG validation.")
        tmp.replace(destination)
    finally:
        tmp.unlink(missing_ok=True)


def save_image_bytes(raw, destination, size):
    with Image.open(BytesIO(raw)) as im:
        im.load()
        save_image(im, destination, size)


def prompt_for(job):
    prompt = next(
        (
            job[key].strip()
            for key in (
                "prompt", "image_prompt", "visual_prompt",
                "scene_prompt", "description",
            )
            if isinstance(job.get(key), str) and job[key].strip()
        ),
        "",
    )
    if not prompt:
        raise RuntimeError(
            f"Scene {job.get('scene')} has no image prompt."
        )

    negative = str(job.get("negative_prompt", "")).strip()
    return f"{prompt}\n\nQuality: {QUALITY}\n\nAvoid: {negative}"


def get_hash(job):
    supplied = job.get("prompt_sha256")
    if isinstance(supplied, str) and len(supplied) == 64:
        return supplied.lower()
    return hashlib.sha256(
        prompt_for(job).encode("utf-8")
    ).hexdigest()


def get_old_rows():
    doc = read_json(IMAGE_MANIFEST, {})
    rows = doc.get("scenes", []) if isinstance(doc, dict) else []
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


# ---------------- Hugging Face Spaces ----------------

def generate_hf(prompt, destination, size, seed, space):
    if Client is None:
        raise RuntimeError("gradio_client is not installed.")

    kwargs = {"verbose": False}
    if HF_TOKEN:
        kwargs["token"] = HF_TOKEN

    client = Client(space, **kwargs)
    width, height = size

    # The endpoint and arguments depend on each Space's published API.
    result = client.predict(
        prompt,
        height,
        width,
        9,
        seed,
        False,
        api_name="/generate_image",
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
        )

    if isinstance(result, str):
        candidate = Path(result)
        if candidate.is_file():
            with Image.open(candidate) as im:
                save_image(im, destination, size)
            return

        if result.startswith(("https://", "http://")):
            response = SESSION.get(result, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                raise ValueError("Hugging Face returned non-image content.")
            save_image_bytes(response.content, destination, size)
            return

    raise ValueError(
        f"Space {space} returned no usable image; verify its API schema."
    )


# ---------------- Pollinations ----------------

def pollinations_zero_price_models():
    # Only use explicitly configured model names; never guess free pricing.
    if not POLLINATIONS_API_KEY or not POLLINATIONS_MODELS:
        return []

    return list(dict.fromkeys(POLLINATIONS_MODELS))


def generate_pollinations(prompt, destination, size, seed, model):
    if not POLLINATIONS_API_KEY:
        raise RuntimeError("POLLINATIONS_API_KEY is not configured.")

    width, height = size
    url = "https://gen.pollinations.ai/image/" + quote(prompt, safe="")
    response = SESSION.get(
        url,
        params={
            "model": model,
            "width": width,
            "height": height,
            "seed": seed,
            "nologo": "true",
        },
        headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    if not response.headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError("Pollinations returned non-image content.")

    save_image_bytes(response.content, destination, size)


# ---------------- Gemini Image ----------------

def generate_gemini(prompt, destination, size):
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

    # Current Interactions responses can expose output_image directly.
    image_item = data.get("output_image")
    if isinstance(image_item, dict) and image_item.get("data"):
        save_image_bytes(
            base64.b64decode(image_item["data"], validate=True),
            destination,
            size,
        )
        return

    # Also inspect output blocks if the response uses that representation.
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        encoded = item.get("data")
        if item.get("type") == "image" and encoded:
            save_image_bytes(
                base64.b64decode(encoded, validate=True),
                destination,
                size,
            )
            return

    raise ValueError(
        "Gemini response contained no image bytes; inspect model/API response."
    )


# ---------------- Replicate ----------------

def generate_replicate(prompt, destination, size):
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not configured.")

    parts = REPLICATE_MODEL.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise RuntimeError(
            "REPLICATE_IMAGE_MODEL must be owner/model-name."
        )

    owner, model = parts
    width, height = size

    response = SESSION.post(
        f"https://api.replicate.com/v1/models/{owner}/{model}/predictions",
        headers={
            "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={
            "input": {
                "prompt": prompt,
                "aspect_ratio": "9:16" if height > width else "16:9",
                "output_format": "png",
            },
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    prediction = response.json()
    deadline = time.monotonic() + REQUEST_TIMEOUT

    while prediction.get("status") in ("starting", "processing"):
        if time.monotonic() >= deadline:
            raise TimeoutError("Replicate prediction timed out.")

        time.sleep(2)
        url = prediction.get("urls", {}).get("get")
        if not url:
            prediction_id = prediction.get("id")
            if not prediction_id:
                raise ValueError("Replicate returned no prediction ID.")
            url = f"https://api.replicate.com/v1/predictions/{quote(str(prediction_id))}"

        check = SESSION.get(
            url,
            headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"},
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

    if not isinstance(output, list):
        raise ValueError("Replicate output was not a list.")

    image_url = next(
        (
            item for item in output
            if isinstance(item, str)
            and item.startswith(("https://", "http://"))
        ),
        None,
    )
    if not image_url:
        raise ValueError(
            "Replicate output contained no image URL. "
            "The selected model may have a different output schema."
        )

    image_response = SESSION.get(image_url, timeout=REQUEST_TIMEOUT)
    image_response.raise_for_status()
    if not image_response.headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError("Replicate output URL did not return an image.")

    save_image_bytes(image_response.content, destination, size)


# ---------------- Checkpoints and fallback ----------------

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

        if (
            previous.get("prompt_sha256") == digest
            and valid_image(destination, size)
        ):
            record["status"] = "existing"
            record["provider"] = previous.get("provider") or "previous run"
        else:
            if destination.exists() and not valid_image(destination, size):
                destination.unlink(missing_ok=True)
            pending.append((job, record, destination, size))

        records.append(record)

    manifest = {
        "version": "photo-motion-5.0",
        "policy": (
            "Hugging Face, configured Pollinations models, Gemini Image, "
            "then Replicate. Gemini and Replicate may incur charges."
        ),
        "total_scenes": len(records),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(
                r["status"] in ("existing", "generated") for r in records
            ),
            "failed": sum(r["status"] == "failed" for r in records),
            "pending": sum(r["status"] == "pending" for r in records),
        }
        save_json(GENERATION_MANIFEST, manifest)

        rows = []
        for job, record in zip(jobs, records):
            rows.append({
                "scene": record["scene"],
                "global_scene": record["scene"],
                "part": job.get("part", 1),
                "local_scene": job.get("local_scene", record["scene"]),
                "image": record["file"],
                "image_path": record["file"],
                "duration": float(job.get("duration", 5.0)),
                "status": record["status"],
                "provider": record.get("provider"),
                "prompt_sha256": record["prompt_sha256"],
            })

        save_json(IMAGE_MANIFEST, {
            "version": "photo-motion-5.0",
            "format": mode,
            "total_scenes": len(rows),
            "images_ready": sum(
                row["status"] in ("existing", "generated") for row in rows
            ),
            "images_required": len(rows),
            "scenes": rows,
        })

    checkpoint()

    log("=" * 60)
    log("KATHA LOK AI — IMAGE GENERATION")
    log("Total scenes:", len(jobs))
    log("Already ready:", len(jobs) - len(pending))
    log("Pending:", len(pending))
    log("Gemini configured:", bool(GEMINI_API_KEY))
    log("Replicate configured:", bool(REPLICATE_API_TOKEN))
    log("=" * 60)

    if not pending:
        log("All scene images are ready; no provider calls required.")
        return

    pollinations = pollinations_zero_price_models()
    blocked_hf = set()
    blocked_pollinations = set()
    blocked_gemini = False
    blocked_replicate = False

    for job, record, destination, size in pending:
        prompt = prompt_for(job)
        seed = BASE_SEED + record["scene"]
        success = False
        last_error = ""

        # First try Hugging Face Spaces.
        for space in HF_SPACES:
            if space in blocked_hf:
                continue

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    log(f"Scene {record['scene']}: Hugging Face {space}, attempt {attempt}")
                    generate_hf(prompt, destination, size, seed, space)

                    if not valid_image(destination, size):
                        raise ValueError("Image validation failed.")

                    record.update(
                        status="generated",
                        provider=f"Hugging Face: {space}",
                    )
                    record["attempts"].append({
                        "provider": f"Hugging Face: {space}",
                        "status": "success",
                        "attempt": attempt,
                    })
                    success = True
                    checkpoint()
                    break

                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log("HF FAILED:", last_error[:250])
                    record["attempts"].append({
                        "provider": f"Hugging Face: {space}",
                        "status": "failed",
                        "attempt": attempt,
                        "error": last_error[:600],
                    })

                    if is_access_or_quota_error(last_error):
                        blocked_hf.add(space)
                        break
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(2)

            if success:
                break

        # Pollinations is only attempted for explicitly configured models.
        if not success:
            for model in pollinations:
                if model in blocked_pollinations:
                    continue
                try:
                    log(f"Scene {record['scene']}: Pollinations {model}")
                    generate_pollinations(
                        prompt, destination, size, seed, model
                    )
                    if not valid_image(destination, size):
                        raise ValueError("Image validation failed.")

                    record.update(
                        status="generated",
                        provider=f"Pollinations: {model}",
                    )
                    record["attempts"].append({
                        "provider": f"Pollinations: {model}",
                        "status": "success",
                    })
                    success = True
                    checkpoint()
                    break

                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log("POLLINATIONS FAILED:", last_error[:250])
                    record["attempts"].append({
                        "provider": f"Pollinations: {model}",
                        "status": "failed",
                        "error": last_error[:600],
                    })
                    if is_access_or_quota_error(last_error):
                        blocked_pollinations.add(model)

        # Gemini Image is the next fallback.
        if not success and GEMINI_API_KEY and not blocked_gemini:
            try:
                log(f"Scene {record['scene']}: Gemini Image {GEMINI_MODEL}")
                generate_gemini(prompt, destination, size)

                if not valid_image(destination, size):
                    raise ValueError("Gemini image validation failed.")

                record.update(
                    status="generated",
                    provider=f"Gemini Image: {GEMINI_MODEL}",
                )
                record["attempts"].append({
                    "provider": f"Gemini Image: {GEMINI_MODEL}",
                    "status": "success",
                })
                success = True
                checkpoint()

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log("GEMINI FAILED:", last_error[:250])
                record["attempts"].append({
                    "provider": f"Gemini Image: {GEMINI_MODEL}",
                    "status": "failed",
                    "error": last_error[:600],
                })
                if is_access_or_quota_error(last_error):
                    blocked_gemini = True

        # Replicate is the final fallback.
        if not success and REPLICATE_API_TOKEN and not blocked_replicate:
            try:
                log(f"Scene {record['scene']}: Replicate {REPLICATE_MODEL}")
                generate_replicate(prompt, destination, size)

                if not valid_image(destination, size):
                    raise ValueError("Replicate image validation failed.")

                record.update(
                    status="generated",
                    provider=f"Replicate: {REPLICATE_MODEL}",
                )
                record["attempts"].append({
                    "provider": f"Replicate: {REPLICATE_MODEL}",
                    "status": "success",
                })
                success = True
                checkpoint()

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log("REPLICATE FAILED:", last_error[:250])
                record["attempts"].append({
                    "provider": f"Replicate: {REPLICATE_MODEL}",
                    "status": "failed",
                    "error": last_error[:600],
                })
                if is_access_or_quota_error(last_error):
                    blocked_replicate = True

        if not success:
            record["status"] = "failed"
            record["error"] = (
                last_error[:800] or "No configured provider succeeded."
            )
            log(
                f"Scene {record['scene']} failed. "
                "Previously completed scene images remain checkpointed."
            )

        checkpoint()

    failed = [
        r["scene"] for r in records if r["status"] == "failed"
    ]
    ready = sum(
        r["status"] in ("existing", "generated") for r in records
    )

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
