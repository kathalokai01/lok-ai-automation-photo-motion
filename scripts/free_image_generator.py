
#!/usr/bin/env python3
"""Katha Lok AI: free-only image generation with resumable checkpoints."""

from __future__ import annotations

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
IMAGE_MANIFEST_FILE = VISUAL_DIR / "image_manifest.json"
GENERATION_MANIFEST_FILE = (
    VISUAL_DIR / "image_generation_manifest.json"
)

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()

TIMEOUT = 150
MAX_ATTEMPTS = 2
BASE_SEED = 20261009
MIN_IMAGE_BYTES = 5000

DEFAULT_SPACE = "mrfakename/Z-Image-Turbo"

FALLBACK_SPACES = [
    item.strip()
    for item in os.getenv("FREE_IMAGE_FALLBACK_SPACES", "").split(",")
    if item.strip()
]

PROVIDER_SPACES = list(dict.fromkeys(
    [DEFAULT_SPACE, *FALLBACK_SPACES]
))

POLLINATIONS_MODELS = [
    item.strip()
    for item in os.getenv("POLLINATIONS_FREE_IMAGE_MODELS", "").split(",")
    if item.strip()
]

QUALITY = (
    "Photorealistic live-action cinematic photography. "
    "Realistic Indian people, clothing, locations and architecture. "
    "Natural skin texture, believable anatomy, realistic lighting "
    "and shadows. Keep recurring characters visually consistent. "
    "No cartoon, anime, illustration, CGI, distorted faces, extra "
    "limbs, text, logos or watermarks."
)

SESSION = requests.Session()


def log(*items):
    print(*items, flush=True)


def read_json(path, default=None):
    if not path.is_file():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def scene_number(job, fallback):
    try:
        number = int(job.get("global_scene", job.get("scene", fallback)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid scene number in job {fallback}."
        ) from exc

    if number < 1:
        raise RuntimeError(f"Scene number must be positive: {number}")

    return number


def load_jobs():
    data = read_json(JOBS_FILE)

    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = (
            data.get("jobs")
            or data.get("visual_jobs")
            or data.get("scenes")
            or []
        )
    else:
        jobs = []

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("visual_jobs.json contains no scene jobs.")

    seen = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Scene job {index} is not an object.")

        number = scene_number(job, index)

        if number in seen:
            raise RuntimeError(f"Duplicate scene number: {number}")

        seen.add(number)

    if sorted(seen) != list(range(1, len(jobs) + 1)):
        raise RuntimeError(
            "Global scene numbers must run continuously from 1."
        )

    return jobs, data


def image_size(job):
    try:
        width, height = map(
            int,
            str(job.get("image_size", "720x1280")).lower().split("x", 1),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid image_size for scene {job.get('scene')}."
        ) from exc

    if not (256 <= width <= 2048 and 256 <= height <= 2048):
        raise RuntimeError(f"Unsupported image dimensions: {width}x{height}")

    return width, height


def image_path(job, number):
    supplied = job.get("image_path") or (
        f"output/visuals/scene_{number:02d}.png"
    )

    candidate = Path(str(supplied))

    if not candidate.is_absolute():
        candidate = ROOT / candidate

    candidate = candidate.resolve()

    if not candidate.is_relative_to(ROOT.resolve()):
        raise RuntimeError(
            f"Scene {number}: image path escapes the repository."
        )

    return candidate


def valid_image(path, expected_size=None):
    try:
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return False

        with Image.open(path) as image:
            image.verify()

        if expected_size:
            with Image.open(path) as image:
                if image.size != expected_size:
                    return False

        return True
    except Exception:
        return False


def save_pil(image, destination, size):
    if not isinstance(image, Image.Image):
        raise ValueError("Provider did not return a valid image.")

    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.png")

    try:
        image.save(temporary, "PNG", optimize=True)

        if not valid_image(temporary, size):
            raise ValueError("Saved image failed validation.")

        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def save_bytes(raw, destination, size):
    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_pil(image, destination, size)


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
            if isinstance(job.get(key), str) and job[key].strip()
        ),
        "",
    )

    if not prompt:
        raise RuntimeError(
            f"Scene {job.get('scene')} has no image prompt."
        )

    negative = str(job.get("negative_prompt", "")).strip()

    return (
        f"{prompt}\n\nQuality requirements: {QUALITY}"
        f"\n\nAvoid: {negative}"
    )


def prompt_hash(job):
    # Keep the hash compatible with generate_visuals.py.
    supplied = job.get("prompt_sha256")

    if (
        isinstance(supplied, str)
        and len(supplied) == 64
        and all(char in "0123456789abcdefABCDEF" for char in supplied)
    ):
        return supplied.lower()

    return hashlib.sha256(
        prompt_for(job).encode("utf-8")
    ).hexdigest()


def generate_hf_space(prompt, destination, size, seed, space_name):
    if Client is None:
        raise RuntimeError("gradio_client is not installed.")

    kwargs = {"verbose": False}

    if HF_TOKEN:
        kwargs["token"] = HF_TOKEN

    client = Client(space_name, **kwargs)
    width, height = size

    # This Space API signature must match the live Space.
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
            raise ValueError("Hugging Face returned an empty result.")
        result = result[0]

    if isinstance(result, dict):
        local_path = next(
            (
                result[key]
                for key in ("path", "filepath", "file")
                if result.get(key)
            ),
            None,
        )

        if local_path and Path(str(local_path)).is_file():
            with Image.open(str(local_path)) as image:
                save_pil(image, destination, size)
            return

        result = result.get("url")

    if isinstance(result, Image.Image):
        save_pil(result, destination, size)
        return

    if isinstance(result, str):
        local = Path(result)

        if local.is_file():
            with Image.open(local) as image:
                save_pil(image, destination, size)
            return

        if result.startswith(("https://", "http://")):
            response = SESSION.get(result, timeout=TIMEOUT)
            response.raise_for_status()

            if not response.headers.get(
                "content-type", ""
            ).lower().startswith("image/"):
                raise ValueError("Space URL did not return image content.")

            save_bytes(response.content, destination, size)
            return

    raise ValueError("Hugging Face Space returned no usable image.")


def model_id(item):
    if not isinstance(item, dict):
        return ""

    for key in ("id", "name", "model", "slug"):
        value = item.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def explicitly_zero_priced(value):
    """Require a recognized price field with an explicit numeric zero."""
    if not isinstance(value, dict):
        return False

    price_keys = {
        "price",
        "cost",
        "pollen",
        "pollen_cost",
        "price_per_image",
        "image_price",
        "completion_image_price",
        "request_price",
    }

    for key, item in value.items():
        normalized = str(key).lower().replace("-", "_")

        if normalized in price_keys:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if item == 0:
                    return True

            elif isinstance(item, str):
                try:
                    if float(item.strip()) == 0:
                        return True
                except ValueError:
                    pass

        if isinstance(item, dict) and explicitly_zero_priced(item):
            return True

    return False


def is_image_model(item):
    if not isinstance(item, dict):
        return False

    description = json.dumps(item, ensure_ascii=False).lower()

    return any(
        marker in description
        for marker in (
            '"image"',
            "text-to-image",
            "text_to_image",
            "image-generation",
            "image_generation",
        )
    )


def get_pollinations_free_models():
    # No key means no Pollinations request.
    if not POLLINATIONS_API_KEY:
        log("Pollinations skipped: API key not configured.")
        return []

    catalog = None

    for url in (
        "https://gen.pollinations.ai/image/models",
        "https://gen.pollinations.ai/v1/models",
    ):
        try:
            response = SESSION.get(url, timeout=25)
            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, list):
                catalog = payload
            elif isinstance(payload, dict):
                catalog = (
                    payload.get("data")
                    or payload.get("models")
                    or payload.get("items")
                )

            if isinstance(catalog, list):
                break

            catalog = None

        except Exception as exc:
            log("Pollinations catalog unavailable:", str(exc)[:200])

    if not isinstance(catalog, list):
        log("Pollinations skipped: no verifiable model catalog.")
        return []

    requested = set(POLLINATIONS_MODELS)
    approved = []

    for item in catalog:
        name = model_id(item)

        if not name or not is_image_model(item):
            continue

        if requested and name not in requested:
            continue

        if explicitly_zero_priced(item):
            approved.append(name)

    approved = list(dict.fromkeys(approved))

    if not approved:
        log("Pollinations skipped: no explicitly zero-priced image model.")

    return approved


def generate_pollinations(prompt, destination, size, seed, model):
    if not POLLINATIONS_API_KEY:
        raise RuntimeError("POLLINATIONS_API_KEY is missing.")

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
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    if not response.headers.get(
        "content-type", ""
    ).lower().startswith("image/"):
        raise ValueError("Pollinations returned non-image content.")

    save_bytes(response.content, destination, size)


def is_quota_or_access_error(error_text):
    text = error_text.lower()

    return any(
        token in text
        for token in (
            "quota",
            "429",
            "401",
            "403",
            "402",
            "payment required",
            "billing",
            "unauthorized",
            "forbidden",
            "zero gpu",
            "zero-gpu",
            "insufficient credit",
            "rate limit",
            "gpu minutes",
            "exceeded your",
            "not enough",
        )
    )


def old_manifest_by_scene():
    document = read_json(IMAGE_MANIFEST_FILE, {})

    if not isinstance(document, dict):
        return {}

    rows = document.get("scenes", [])

    if not isinstance(rows, list):
        return {}

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            number = int(row.get("global_scene", row.get("scene")))
        except (TypeError, ValueError):
            continue

        if number > 0:
            result[number] = row

    return result


def sync_image_manifest(jobs, records, mode):
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

    ready = sum(
        row["status"] in ("existing", "generated")
        for row in rows
    )

    save_json(IMAGE_MANIFEST_FILE, {
        "version": "photo-motion-4.3",
        "format": mode,
        "total_scenes": len(rows),
        "images_ready": ready,
        "images_required": len(rows),
        "scenes": rows,
    })


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    jobs, jobs_document = load_jobs()
    mode = (
        jobs_document.get("format", "unknown")
        if isinstance(jobs_document, dict)
        else "unknown"
    )

    previous_rows = old_manifest_by_scene()
    records = []
    pending = []

    for index, job in enumerate(jobs, start=1):
        number = scene_number(job, index)
        destination = image_path(job, number)
        size = image_size(job)
        digest = prompt_hash(job)
        previous = previous_rows.get(number, {})

        record = {
            "scene": number,
            "file": destination.relative_to(ROOT).as_posix(),
            "width": size[0],
            "height": size[1],
            "status": "pending",
            "provider": None,
            "prompt_sha256": digest,
            "attempts": [],
        }

        same_prompt = previous.get("prompt_sha256") == digest

        if valid_image(destination, size) and same_prompt:
            record["status"] = "existing"
            record["provider"] = previous.get("provider") or "previous run"
        else:
            if destination.exists() and not valid_image(destination, size):
                destination.unlink(missing_ok=True)

            pending.append((job, record, destination, size))

        records.append(record)

    manifest = {
        "version": "photo-motion-image-generation-4.3",
        "allow_billable_providers": False,
        "policy": (
            "Hugging Face Spaces first. Pollinations only after the live "
            "catalog explicitly confirms image capability and zero price. "
            "Gemini Image and Replicate are disabled."
        ),
        "total_scenes": len(jobs),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(
                item["status"] in ("existing", "generated")
                for item in records
            ),
            "pending": sum(
                item["status"] == "pending" for item in records
            ),
            "failed": sum(
                item["status"] == "failed" for item in records
            ),
        }

        save_json(GENERATION_MANIFEST_FILE, manifest)
        sync_image_manifest(jobs, records, mode)

    checkpoint()

    log("=" * 60)
    log("KATHA LOK AI — FREE-ONLY IMAGE GENERATION")
    log("Total scenes:", len(jobs))
    log("Already ready:", len(jobs) - len(pending))
    log("Scenes requiring generation:", len(pending))
    log("Hugging Face Spaces:", PROVIDER_SPACES)
    log("Paid providers: DISABLED")
    log("=" * 60)

    # Crucially, don't call any provider/catalog if nothing is pending.
    pollinations_models = (
        get_pollinations_free_models() if pending else []
    )

    manifest["provider_order"] = (
        [f"Hugging Face Space: {name}" for name in PROVIDER_SPACES]
        + [
            f"Pollinations zero-price model: {name}"
            for name in pollinations_models
        ]
    )
    checkpoint()

    blocked_spaces = set()
    blocked_pollinations = set()

    for job, record, destination, size in pending:
        prompt = prompt_for(job)
        seed = BASE_SEED + record["scene"]
        success = False
        last_error = ""

        for space_name in PROVIDER_SPACES:
            provider = f"Hugging Face Space: {space_name}"

            if space_name in blocked_spaces:
                record["attempts"].append({
                    "provider": provider,
                    "status": "skipped",
                    "reason": "Provider blocked after quota/access failure.",
                })
                continue

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    log(
                        f"Scene {record['scene']}: {provider}, "
                        f"attempt {attempt}/{MAX_ATTEMPTS}"
                    )

                    generate_hf_space(
                        prompt,
                        destination,
                        size,
                        seed,
                        space_name,
                    )

                    if not valid_image(destination, size):
                        raise ValueError("Generated image failed validation.")

                    record["status"] = "generated"
                    record["provider"] = provider
                    record["attempts"].append({
                        "provider": provider,
                        "status": "success",
                        "attempt": attempt,
                    })

                    success = True
                    checkpoint()
                    log("SAVED:", record["file"])
                    break

                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log("PROVIDER FAILED:", last_error[:300])

                    if destination.exists() and not valid_image(
                        destination, size
                    ):
                        destination.unlink(missing_ok=True)

                    record["attempts"].append({
                        "provider": provider,
                        "status": "failed",
                        "attempt": attempt,
                        "error": last_error[:800],
                    })

                    if is_quota_or_access_error(last_error):
                        blocked_spaces.add(space_name)
                        log("Provider blocked for remaining scenes.")
                        break

                    if attempt < MAX_ATTEMPTS:
                        time.sleep(2)

            if success:
                break

        if not success:
            for model in pollinations_models:
                provider = f"Pollinations zero-price model: {model}"

                if model in blocked_pollinations:
                    record["attempts"].append({
                        "provider": provider,
                        "status": "skipped",
                        "reason": "Provider blocked after quota/access failure.",
                    })
                    continue

                try:
                    log(f"Scene {record['scene']}: {provider}")

                    generate_pollinations(
                        prompt,
                        destination,
                        size,
                        seed,
                        model,
                    )

                    if not valid_image(destination, size):
                        raise ValueError("Generated image failed validation.")

                    record["status"] = "generated"
                    record["provider"] = provider
                    record["attempts"].append({
                        "provider": provider,
                        "status": "success",
                    })

                    success = True
                    checkpoint()
                    log("SAVED:", record["file"])
                    break

                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log("PROVIDER FAILED:", last_error[:300])

                    record["attempts"].append({
                        "provider": provider,
                        "status": "failed",
                        "error": last_error[:800],
                    })

                    if is_quota_or_access_error(last_error):
                        blocked_pollinations.add(model)

                    if destination.exists() and not valid_image(
                        destination, size
                    ):
                        destination.unlink(missing_ok=True)

        if not success:
            record["status"] = "failed"
            record["error"] = (
                last_error[:1000]
                or "All verified free providers were unavailable."
            )

            checkpoint()
            log(
                f"Scene {record['scene']} failed; "
                "successful scene checkpoints are preserved."
            )

    checkpoint()

    ready = sum(
        item["status"] in ("existing", "generated")
        for item in records
    )
    failed = [
        item["scene"]
        for item in records
        if item["status"] == "failed"
    ]

    log("\nFINAL SUMMARY")
    log(f"Images ready: {ready}/{len(records)}")
    log("Failed scenes:", failed)
    log("Generation manifest:", GENERATION_MANIFEST_FILE)
    log("Image manifest:", IMAGE_MANIFEST_FILE)

    if failed:
        raise RuntimeError(
            f"Image generation failed for scenes {failed}. "
            "Successful images were checkpointed. "
            "No paid image provider was called."
        )

    log("ALL SCENE IMAGES READY")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("IMAGE GENERATION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"IMAGE GENERATION ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
