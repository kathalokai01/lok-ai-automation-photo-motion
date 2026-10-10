
#!/usr/bin/env python3
"""Free-only, prompt-aware and resumable scene-image generation."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path

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
GENERATION_MANIFEST_FILE = VISUAL_DIR / "image_generation_manifest.json"

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
ALLOW_BILLABLE = (
    os.getenv("ALLOW_BILLABLE_PROVIDERS", "false").strip().lower()
    in {"true", "1", "yes"}
)

TIMEOUT = 150
MAX_ATTEMPTS = 2
BASE_SEED = 20261009
MIN_IMAGE_BYTES = 5000

QUALITY = (
    "Photorealistic live-action cinematic photography. "
    "Realistic Indian people, clothing, locations and architecture. "
    "Natural skin texture, believable anatomy, realistic lighting "
    "and shadows. Maintain recurring character identity and clothing. "
    "No cartoon, anime, illustration, CGI, plastic skin, distorted "
    "faces, extra limbs, text, logos or watermarks."
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
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


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
            raise RuntimeError(f"Duplicate global scene number: {number}")
        seen.add(number)

    if sorted(seen) != list(range(1, len(jobs) + 1)):
        raise RuntimeError("Global scene numbers must run continuously from 1.")

    return jobs


def scene_number(job, index):
    try:
        number = int(job.get("global_scene", job.get("scene", index)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid scene number at job {index}.") from exc

    if number < 1:
        raise RuntimeError(f"Scene number must be positive: {number}")

    return number


def image_size(job):
    try:
        width, height = map(
            int, str(job.get("image_size", "720x1280")).lower().split("x", 1)
        )
        if 256 <= width <= 2048 and 256 <= height <= 2048:
            return width, height
    except (TypeError, ValueError):
        pass

    raise RuntimeError(
        f"Invalid image_size for scene {job.get('scene')}: "
        f"{job.get('image_size')!r}"
    )


def image_path(job, number):
    supplied = job.get("image_path") or f"output/visuals/scene_{number:02d}.png"
    candidate = Path(str(supplied))
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    candidate = candidate.resolve()
    if not candidate.is_relative_to(ROOT):
        raise RuntimeError(f"Scene {number} image path escapes the repository.")

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
        raise ValueError("Provider did not return a PIL image.")

    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image, size, method=Image.Resampling.LANCZOS
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
    return f"{prompt}\n\nQuality requirements: {QUALITY}\n\nAvoid: {negative}"


def prompt_hash(job):
    supplied = job.get("prompt_sha256")
    if isinstance(supplied, str) and len(supplied) == 64:
        return supplied

    return hashlib.sha256(
        prompt_for(job).encode("utf-8")
    ).hexdigest()


def generate_hf_space(prompt, destination, size, seed):
    """Generate through the configured Hugging Face Gradio Space."""
    if Client is None:
        raise RuntimeError("gradio_client is not installed.")

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

            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                raise ValueError("Provider URL did not return image content.")

            save_bytes(response.content, destination, size)
            return

    raise ValueError("Hugging Face returned no usable image.")


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
        result[number] = row

    return result


def sync_image_manifest(jobs, records, previous_rows, mode):
    rows = []

    for job, record in zip(jobs, records):
        number = record["scene"]
        previous = previous_rows.get(number, {})

        rows.append({
            "scene": number,
            "global_scene": number,
            "part": job.get("part", previous.get("part", 1)),
            "local_scene": job.get("local_scene", previous.get("local_scene", number)),
            "image": record["file"],
            "image_path": record["file"],
            "duration": float(job.get("duration", previous.get("duration", 5.0))),
            "status": record["status"],
            "provider": record.get("provider"),
            "prompt_sha256": record["prompt_sha256"],
        })

    ready = sum(row["status"] in ("existing", "generated") for row in records)

    save_json(IMAGE_MANIFEST_FILE, {
        "version": "photo-motion-4.0",
        "format": mode,
        "total_scenes": len(jobs),
        "images_ready": ready,
        "images_required": len(jobs),
        "scenes": rows,
    })


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)
    jobs = load_jobs()
    jobs_document = read_json(JOBS_FILE, {})
    mode = jobs_document.get("format", "unknown") if isinstance(jobs_document, dict) else "unknown"

    previous_rows = old_manifest_by_scene()
    records = []
    pending = []

    for index, job in enumerate(jobs, start=1):
        number = scene_number(job, index)
        destination = image_path(job, number)
        size = image_size(job)
        digest = prompt_hash(job)
        previous = previous_rows.get(number, {})
        old_digest = previous.get("prompt_sha256")
        same_prompt = isinstance(old_digest, str) and old_digest == digest

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

        # Reuse only if the image is valid AND the prior manifest confirms
        # it was made for this exact prompt.
        if valid_image(destination, size) and same_prompt:
            record["status"] = "existing"
            record["provider"] = previous.get("provider") or "previous run"
        else:
            if destination.exists() and not valid_image(destination, size):
                destination.unlink(missing_ok=True)
            pending.append((job, record, destination, size))

        records.append(record)

    manifest = {
        "version": "photo-motion-image-generation-4.0",
        "allow_billable_providers": ALLOW_BILLABLE,
        "policy": "No paid-provider fallback is configured.",
        "provider_order": ["Hugging Face ZeroGPU: Z-Image-Turbo"],
        "total_scenes": len(jobs),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(r["status"] in ("existing", "generated") for r in records),
            "pending": sum(r["status"] == "pending" for r in records),
            "failed": sum(r["status"] == "failed" for r in records),
        }
        save_json(GENERATION_MANIFEST_FILE, manifest)
        sync_image_manifest(jobs, records, previous_rows, mode)

    checkpoint()

    log("=" * 60)
    log("KATHA LOK AI — FREE SCENE IMAGE GENERATION")
    log("Scenes:", len(jobs))
    log("Already ready for same prompt:", len(jobs) - len(pending))
    log("Paid providers allowed:", ALLOW_BILLABLE)
    log("=" * 60)

    # This script intentionally has one configured free provider.
    # A failed quota/authentication check must not trigger paid services.
    provider_name = "Hugging Face ZeroGPU: Z-Image-Turbo"
    provider_blocked = False
    remaining = []

    for job, record, destination, size in pending:
        if provider_blocked:
            record["status"] = "failed"
            record["attempts"].append({
                "provider": provider_name,
                "status": "skipped",
                "reason": "Provider previously hit a quota/access failure.",
            })
            checkpoint()
            continue

        prompt = prompt_for(job)
        seed = BASE_SEED + record["scene"]
        success = False
        last_error = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                log(
                    f"Scene {record['scene']}: "
                    f"attempt {attempt}/{MAX_ATTEMPTS}"
                )

                generate_hf_space(prompt, destination, size, seed)

                if not valid_image(destination, size):
                    raise ValueError("Generated image failed validation.")

                record["status"] = "generated"
                record["provider"] = provider_name
                record["attempts"].append({
                    "provider": provider_name,
                    "status": "success",
                    "attempt": attempt,
                })
                log("SAVED:", record["file"])
                success = True
                checkpoint()
                break

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log("FAILED:", last_error[:300])

                if destination.exists() and not valid_image(destination, size):
                    destination.unlink(missing_ok=True)

                lower = last_error.lower()
                stop_tokens = (
                    "quota", "429", "401", "403", "402",
                    "payment", "billing", "unauthorized",
                    "forbidden", "zero gpu", "zero-gpu",
                    "insufficient credit", "rate limit",
                )

                if any(token in lower for token in stop_tokens):
                    provider_blocked = True
                    log("Stopping provider after quota/access error.")
                    break

                if attempt < MAX_ATTEMPTS:
                    time.sleep(2)

        if not success:
            record["status"] = "failed"
            record["attempts"].append({
                "provider": provider_name,
                "status": "failed",
                "error": last_error[:800] or "Unknown provider failure.",
            })
            remaining.append(record)
            checkpoint()

    checkpoint()

    ready = sum(r["status"] in ("existing", "generated") for r in records)
    failed = [r["scene"] for r in records if r["status"] == "failed"]

    log("\nFINAL SUMMARY")
    log(f"Images ready: {ready}/{len(records)}")
    log("Failed scenes:", failed)
    log("Generation manifest:", GENERATION_MANIFEST_FILE.relative_to(ROOT))
    log("Image manifest:", IMAGE_MANIFEST_FILE.relative_to(ROOT))

    if failed:
        raise RuntimeError(
            f"Image generation failed for scenes {failed}. "
            "No paid-provider fallback was attempted."
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
