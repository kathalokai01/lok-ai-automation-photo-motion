
#!/usr/bin/env python3
"""Free-only, per-image fallback, prompt-aware and resumable image generation."""

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
TIMEOUT = 150
MAX_ATTEMPTS = 2
BASE_SEED = 20261009
MIN_IMAGE_BYTES = 5000

DEFAULT_SPACE = "mrfakename/Z-Image-Turbo"

# Add only Spaces that are verified to be free and support the same API:
# /generate_image
# Comma-separated example:
# FREE_IMAGE_FALLBACK_SPACES=owner/space-one,owner/space-two
FALLBACK_SPACES = [
    item.strip()
    for item in os.getenv("FREE_IMAGE_FALLBACK_SPACES", "").split(",")
    if item.strip()
]

PROVIDER_SPACES = list(
    dict.fromkeys([DEFAULT_SPACE, *FALLBACK_SPACES])
)

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
        raise RuntimeError(
            "Global scene numbers must run continuously from 1."
        )

    return jobs


def scene_number(job, index):
    try:
        number = int(job.get("global_scene", job.get("scene", index)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid scene number at job {index}."
        ) from exc

    if number < 1:
        raise RuntimeError(f"Scene number must be positive: {number}")

    return number


def image_size(job):
    try:
        width, height = map(
            int,
            str(job.get("image_size", "720x1280")).lower().split("x", 1),
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
    supplied = job.get("image_path") or (
        f"output/visuals/scene_{number:02d}.png"
    )

    candidate = Path(str(supplied))

    if not candidate.is_absolute():
        candidate = ROOT / candidate

    candidate = candidate.resolve()

    if not candidate.is_relative_to(ROOT):
        raise RuntimeError(
            f"Scene {number} image path escapes the repository."
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
        raise ValueError("Provider did not return a PIL image.")

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
        f"{prompt}\n\n"
        f"Quality requirements: {QUALITY}\n\n"
        f"Avoid: {negative}"
    )


def prompt_hash(job):
    supplied = job.get("prompt_sha256")

    if isinstance(supplied, str) and len(supplied) == 64:
        return supplied

    return hashlib.sha256(
        prompt_for(job).encode("utf-8")
    ).hexdigest()


def generate_hf_space(
    prompt,
    destination,
    size,
    seed,
    space_name,
):
    """Call a Gradio Space with the expected image-generation endpoint."""

    if Client is None:
        raise RuntimeError(
            "gradio_client is not installed."
        )

    kwargs = {"verbose": False}

    if HF_TOKEN:
        kwargs["token"] = HF_TOKEN

    client = Client(space_name, **kwargs)
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
            raise ValueError("Provider returned an empty result.")
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

            content_type = response.headers.get(
                "content-type", ""
            ).lower()

            if not content_type.startswith("image/"):
                raise ValueError(
                    "Provider URL did not return image content."
                )

            save_bytes(response.content, destination, size)
            return

    raise ValueError("Provider returned no usable image.")


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
            number = int(
                row.get("global_scene", row.get("scene"))
            )
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
            "local_scene": job.get(
                "local_scene", previous.get("local_scene", number)
            ),
            "image": record["file"],
            "image_path": record["file"],
            "duration": float(
                job.get("duration", previous.get("duration", 5.0))
            ),
            "status": record["status"],
            "provider": record.get("provider"),
            "prompt_sha256": record["prompt_sha256"],
        })

    ready = sum(
        row["status"] in ("existing", "generated")
        for row in records
    )

    save_json(IMAGE_MANIFEST_FILE, {
        "version": "photo-motion-4.1",
        "format": mode,
        "total_scenes": len(jobs),
        "images_ready": ready,
        "images_required": len(jobs),
        "scenes": rows,
    })


def is_quota_or_access_error(error_text):
    text = error_text.lower()

    tokens = (
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

    return any(token in text for token in tokens)


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    jobs = load_jobs()
    jobs_document = read_json(JOBS_FILE, {})

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
        same_prompt = previous.get("prompt_sha256") == digest

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

        # Reuse only valid images associated with the same prompt.
        if valid_image(destination, size) and same_prompt:
            record["status"] = "existing"
            record["provider"] = (
                previous.get("provider") or "previous run"
            )
        else:
            if destination.exists() and not valid_image(destination, size):
                destination.unlink(missing_ok=True)

            pending.append((job, record, destination, size))

        records.append(record)

    manifest = {
        "version": "photo-motion-image-generation-4.1",
        "allow_billable_providers": False,
        "policy": "No paid-provider fallback is implemented.",
        "provider_order": [
            f"Hugging Face Space: {space}"
            for space in PROVIDER_SPACES
        ],
        "fallback_policy": (
            "Per-image fallback; checkpoint each successful image."
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
                item["status"] == "pending"
                for item in records
            ),
            "failed": sum(
                item["status"] == "failed"
                for item in records
            ),
        }

        save_json(GENERATION_MANIFEST_FILE, manifest)
        sync_image_manifest(
            jobs, records, previous_rows, mode
        )

    checkpoint()

    log("=" * 60)
    log("KATHA LOK AI — FREE IMAGE FALLBACK")
    log("Total scenes:", len(jobs))
    log("Images already ready:", len(jobs) - len(pending))
    log("Configured providers:", PROVIDER_SPACES)
    log("Paid providers: DISABLED")
    log("=" * 60)

    # A provider that has quota/auth errors is skipped for the rest of
    # this run. The next provider is attempted for the CURRENT scene.
    blocked_spaces = set()

    for job, record, destination, size in pending:
        prompt = prompt_for(job)
        seed = BASE_SEED + record["scene"]
        success = False
        last_error = ""

        for space_name in PROVIDER_SPACES:
            provider_name = f"Hugging Face Space: {space_name}"

            if space_name in blocked_spaces:
                record["attempts"].append({
                    "provider": provider_name,
                    "status": "skipped",
                    "reason": "Quota/access failure earlier in this run.",
                })
                continue

            provider_succeeded_or_blocked = False

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    log(
                        f"Scene {record['scene']} — "
                        f"{provider_name} — "
                        f"attempt {attempt}/{MAX_ATTEMPTS}"
                    )

                    generate_hf_space(
                        prompt=prompt,
                        destination=destination,
                        size=size,
                        seed=seed,
                        space_name=space_name,
                    )

                    if not valid_image(destination, size):
                        raise ValueError(
                            "Generated image failed validation."
                        )

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
                    last_error = (
                        f"{type(exc).__name__}: {exc}"
                    )

                    log("PROVIDER FAILED:", last_error[:300])

                    if (
                        destination.exists()
                        and not valid_image(destination, size)
                    ):
                        destination.unlink(missing_ok=True)

                    if is_quota_or_access_error(last_error):
                        blocked_spaces.add(space_name)
                        record["attempts"].append({
                            "provider": provider_name,
                            "status": "blocked",
                            "error": last_error[:800],
                        })
                        provider_succeeded_or_blocked = True
                        log(
                            "Quota/access failure: moving to next provider."
                        )
                        break

                    if attempt < MAX_ATTEMPTS:
                        time.sleep(2)

            if success:
                break

            if not provider_succeeded_or_blocked:
                record["attempts"].append({
                    "provider": provider_name,
                    "status": "exhausted",
                    "error": last_error[:800] or "Unknown failure.",
                })

            # Continue to the next provider for this same scene.

        if not success:
            record["status"] = "failed"
            remaining_error = last_error or (
                "All configured free providers failed or were unavailable."
            )
            record["error"] = remaining_error[:1000]
            checkpoint()

            log(
                f"Scene {record['scene']} failed on all configured "
                "providers; continuing to the next scene."
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
    log(
        "Generation manifest:",
        GENERATION_MANIFEST_FILE.relative_to(ROOT),
    )
    log("Image manifest:", IMAGE_MANIFEST_FILE.relative_to(ROOT))

    if failed:
        raise RuntimeError(
            f"Image generation failed for scenes {failed}. "
            "Successful images were checkpointed. "
            "No paid provider was called."
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
