
#!/usr/bin/env python3
"""Free-first, resumable and strictly validated image generation."""

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

PAID_OPT_IN = "I_ACCEPT_POTENTIAL_CHARGES"
ALLOW_BILLABLE = (
    os.getenv("ALLOW_BILLABLE_PROVIDERS", "").strip() == PAID_OPT_IN
)

IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720
IMAGE_SIZE = (IMAGE_WIDTH, IMAGE_HEIGHT)
IMAGE_SIZE_TEXT = f"{IMAGE_WIDTH}x{IMAGE_HEIGHT}"
MIN_IMAGE_BYTES = 5000
TIMEOUT = 180
BASE_SEED = 20261009
VERSION = "photo-motion-safe-14-landscape-checkpoint-integrity"

HF_SPACES = list(dict.fromkeys(
    value.strip()
    for value in [
        os.getenv("HF_IMAGE_SPACE", "mrfakename/Z-Image-Turbo"),
        *os.getenv("FREE_IMAGE_FALLBACK_SPACES", "").split(","),
    ]
    if value.strip()
))

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
REPLICATE_KEY = os.getenv("REPLICATE_API_TOKEN", "").strip()
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"
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
    "User-Agent": "KathaLokAI-PhotoMotion/8.0",
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
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


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
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def scene_number(job, fallback):
    try:
        return int(job.get("global_scene", job.get("scene", fallback)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid scene number in job {fallback}."
        ) from exc


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
        raise RuntimeError("visual_jobs.json must be an object or list.")

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("visual_jobs.json contains no jobs.")

    numbers = []
    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise RuntimeError(f"Image job {index} is not an object.")
        number = scene_number(job, index)
        if number < 1:
            raise RuntimeError(f"Invalid scene number: {number}")
        numbers.append(number)

    if numbers != list(range(1, len(jobs) + 1)):
        raise RuntimeError(
            "Image jobs must be ordered and numbered continuously from 1."
        )

    return jobs, metadata


def image_size(job):
    raw = job.get("image_size", IMAGE_SIZE_TEXT)
    try:
        width, height = map(int, str(raw).lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid image_size {raw!r}; expected WIDTHxHEIGHT."
        ) from exc

    if (width, height) != IMAGE_SIZE:
        raise RuntimeError(
            f"Scene {job.get('scene', '?')}: expected landscape "
            f"{IMAGE_SIZE_TEXT}, got {width}x{height}. "
            "Regenerate output/visuals/visual_jobs.json first."
        )
    return width, height


def image_path(job, number):
    raw = job.get("image_path") or (
        f"output/visuals/scene_{number:04d}.png"
    )
    path = Path(str(raw))
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(
            f"Scene {number}: image path escapes the repository."
        )
    return path


def valid_image(path, size=IMAGE_SIZE):
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return False
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.format in ("PNG", "JPEG", "WEBP") and image.size == size
    except Exception:
        return False


def save_image(image, destination, size):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp.png")
    try:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image = ImageOps.fit(
            image,
            size,
            method=Image.Resampling.LANCZOS,
        )
        image.save(temporary, "PNG", optimize=True)
        if not valid_image(temporary, size):
            raise ValueError("Saved image failed format, size or integrity checks.")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def save_image_bytes(raw, destination, size):
    if not raw:
        raise ValueError("Provider returned empty image data.")
    with Image.open(BytesIO(raw)) as image:
        image.load()
        save_image(image, destination, size)


def make_prompt(job):
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
            f"Scene {job.get('global_scene', job.get('scene', '?'))} "
            "has no usable image prompt."
        )

    negative = str(job.get("negative_prompt", "")).strip()
    return f"{prompt}\n\nQuality: {QUALITY}\n\nAvoid: {negative}"


def prompt_hash(job):
    calculated = sha256_text(make_prompt(job))
    declared = str(job.get("prompt_sha256", "")).strip()
    if declared and declared != calculated:
        raise RuntimeError(
            f"Scene {job.get('scene', '?')}: prompt hash mismatch. "
            "Regenerate visual_jobs.json before image generation."
        )
    return calculated


def load_rows(path, key="scenes"):
    document = read_json(path, {})
    rows = document if isinstance(document, list) else (
        document.get(key, []) if isinstance(document, dict) else []
    )
    result = {}
    if isinstance(rows, list):
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


def previous_scene_rows():
    # The generation manifest stores image checksums and provider attempts.
    rows = load_rows(GENERATION_MANIFEST)
    return rows if rows else load_rows(IMAGE_MANIFEST)


def access_error(message):
    message = str(message).lower()
    return any(term in message for term in (
        "429", "401", "403", "402", "quota", "rate limit",
        "billing", "payment required", "insufficient credit",
        "unauthorized", "forbidden", "resource exhausted",
    ))


def response_image(response, destination, size, provider):
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"{provider} returned non-image content.")
    save_image_bytes(response.content, destination, size)


def generate_huggingface(prompt, destination, size, seed, space):
    if Client is None:
        raise RuntimeError("gradio_client is not installed.")

    # Do not forward secrets to public Spaces.
    client = Client(space, verbose=False)
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
            with Image.open(candidate) as image:
                save_image(image, destination, size)
            return
        if result.startswith(("https://", "http://")):
            response = SESSION.get(result, timeout=TIMEOUT)
            response_image(response, destination, size, "Hugging Face")
            return

    raise ValueError(f"Hugging Face Space {space} returned no usable image.")


def generate_pollinations(prompt, destination, size, seed):
    """Use the public endpoint; free availability and limits are not guaranteed."""
    width, height = size
    url = "https://image.pollinations.ai/prompt/" + quote(prompt, safe="")
    headers = {}
    if POLLINATIONS_KEY:
        headers["Authorization"] = f"Bearer {POLLINATIONS_KEY}"

    response = SESSION.get(
        url,
        params={
            "width": width,
            "height": height,
            "seed": seed,
            "nologo": "true",
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    response_image(response, destination, size, "Pollinations")


def generate_gemini(prompt, destination, size):
    if not ALLOW_BILLABLE:
        raise RuntimeError("Gemini blocked: explicit billing opt-in is absent.")
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    width, height = size
    response = SESSION.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={
            "x-goog-api-key": GEMINI_KEY,
            "Content-Type": "application/json",
        },
        json={
            "model": GEMINI_MODEL,
            "input": prompt,
            "response_format": {
                "type": "image",
                "mime_type": "image/png",
                "aspect_ratio": "16:9",
                "image_size": "1K",
            },
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    candidates = [data.get("output_image")]
    output = data.get("output", [])
    if isinstance(output, list):
        candidates.extend(output)

    for item in candidates:
        if not isinstance(item, dict) or not item.get("data"):
            continue
        raw = base64.b64decode(item["data"], validate=True)
        save_image_bytes(raw, destination, size)
        return

    raise ValueError("Gemini response did not contain image data.")


def generate_replicate(prompt, destination, size):
    if not ALLOW_BILLABLE:
        raise RuntimeError("Replicate blocked: explicit billing opt-in is absent.")
    if not REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_TOKEN is missing.")

    parts = REPLICATE_MODEL.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise RuntimeError("REPLICATE_IMAGE_MODEL must be owner/model-name.")

    response = SESSION.post(
        f"https://api.replicate.com/v1/models/{parts[0]}/{parts[1]}/predictions",
        headers={
            "Authorization": f"Bearer {REPLICATE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={
            "input": {
                "prompt": prompt,
                "aspect_ratio": "16:9",
                "output_format": "png",
            }
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    prediction = response.json()
    deadline = time.monotonic() + TIMEOUT

    while prediction.get("status") in ("starting", "processing"):
        if time.monotonic() >= deadline:
            raise TimeoutError("Replicate prediction timed out.")

        time.sleep(2)
        poll_url = prediction.get("urls", {}).get("get")
        if not poll_url:
            prediction_id = prediction.get("id")
            if not prediction_id:
                raise ValueError("Replicate returned no prediction ID.")
            poll_url = (
                "https://api.replicate.com/v1/predictions/"
                + str(prediction_id)
            )

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

    url = next(
        (
            item for item in (output or [])
            if isinstance(item, str)
            and item.startswith(("https://", "http://"))
        ),
        None,
    )
    if not url:
        raise ValueError("Replicate returned no image URL.")

    response = SESSION.get(url, timeout=TIMEOUT)
    response_image(response, destination, size, "Replicate")


def build_image_manifest(jobs, records, metadata):
    rows = []
    for job, record in zip(jobs, records):
        try:
            duration = float(job.get("duration", 5))
            if not 0.5 <= duration <= 3600:
                duration = 5.0
        except (TypeError, ValueError):
            duration = 5.0

        try:
            part = int(job.get("part", 1))
            local_scene = int(job.get("local_scene", record["scene"]))
            if part < 1 or local_scene < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise RuntimeError(
                f"Scene {record['scene']}: invalid part/local_scene mapping."
            )

        rows.append({
            "scene": record["scene"],
            "global_scene": record["scene"],
            "global_scene_number": record["scene"],
            "scene_id": job.get("scene_id"),
            "part": part,
            "local_scene": local_scene,
            "image": record["file"],
            "image_path": record["file"],
            "image_sha256": record.get("image_sha256"),
            "width": IMAGE_WIDTH,
            "height": IMAGE_HEIGHT,
            "duration": duration,
            "status": record["status"],
            "provider": record.get("provider"),
            "prompt_sha256": record["prompt_sha256"],
        })

    return {
        "version": VERSION,
        "source_fingerprint": metadata.get("source_fingerprint"),
        "format": metadata.get("format", "unknown"),
        "image_size": IMAGE_SIZE_TEXT,
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "total_scenes": len(rows),
        "images_ready": sum(
            row["status"] in ("existing", "generated") for row in rows
        ),
        "images_required": len(rows),
        "scenes": rows,
    }


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)
    jobs, metadata = get_jobs()
    previous_rows = previous_scene_rows()

    records = []
    pending = []

    for index, job in enumerate(jobs, 1):
        number = scene_number(job, index)
        size = image_size(job)
        destination = image_path(job, number)
        digest = prompt_hash(job)
        previous = previous_rows.get(number, {})

        record = {
            "scene": number,
            "global_scene": number,
            "file": destination.relative_to(ROOT).as_posix(),
            "width": size[0],
            "height": size[1],
            "status": "pending",
            "provider": None,
            "prompt_sha256": digest,
            "image_sha256": None,
            "attempts": [],
        }

        prior_hash = str(previous.get("image_sha256", "")).strip()
        image_ok = valid_image(destination, size)
        checksum_matches = (
            not prior_hash
            or (
                image_ok
                and sha256_file(destination) == prior_hash
            )
        )

        if (
            previous.get("prompt_sha256") == digest
            and image_ok
            and checksum_matches
        ):
            record["status"] = "existing"
            record["provider"] = previous.get("provider") or "previous run"
            record["image_sha256"] = sha256_file(destination)
        else:
            if destination.exists() and not image_ok:
                destination.unlink(missing_ok=True)
            pending.append((job, record, destination, size))

        records.append(record)

    manifest = {
        "version": VERSION,
        "source_fingerprint": metadata.get("source_fingerprint"),
        "format": metadata.get("format", "unknown"),
        "image_size": IMAGE_SIZE_TEXT,
        "policy": {
            "potentially_billable_providers_enabled": ALLOW_BILLABLE,
            "free_first_order": ["Hugging Face Spaces", "Pollinations"],
            "paid_providers": ["Gemini Image", "Replicate"],
            "note": (
                "Free endpoint availability and limits can change. "
                "Public endpoint use is not a guarantee of permanent free service. "
                "Gemini and Replicate are blocked unless explicit opt-in is set."
            ),
        },
        "total_scenes": len(records),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(
                row["status"] in ("existing", "generated")
                for row in records
            ),
            "failed": sum(row["status"] == "failed" for row in records),
            "pending": sum(row["status"] == "pending" for row in records),
        }
        save_json(GENERATION_MANIFEST, manifest)
        save_json(
            IMAGE_MANIFEST,
            build_image_manifest(jobs, records, metadata),
        )

    checkpoint()

    log("KATHA LOK AI — RESUMABLE IMAGE GENERATION")
    log(f"Scenes: {len(jobs)}")
    log(f"Already valid: {len(jobs) - len(pending)}")
    log(f"Pending: {len(pending)}")
    log(f"Image canvas: {IMAGE_SIZE_TEXT}")
    log(f"Billable opt-in enabled: {ALLOW_BILLABLE}")

    if not pending:
        log("All image checkpoints match; no provider calls needed.")
        return

    blocked_hf = set()
    blocked_paid = set()

    for job, record, destination, size in pending:
        prompt = make_prompt(job)
        seed = BASE_SEED + record["scene"]
        providers = []

        for space in HF_SPACES:
            if space not in blocked_hf:
                providers.append((
                    f"Hugging Face: {space}",
                    lambda space=space: generate_huggingface(
                        prompt, destination, size, seed, space
                    ),
                    "hf:" + space,
                    False,
                ))

        providers.append((
            "Public Pollinations",
            lambda: generate_pollinations(prompt, destination, size, seed),
            "pollinations",
            False,
        ))

        if ALLOW_BILLABLE:
            if GEMINI_KEY and "gemini" not in blocked_paid:
                providers.append((
                    f"Gemini Image ({GEMINI_MODEL})",
                    lambda: generate_gemini(prompt, destination, size),
                    "gemini",
                    True,
                ))
            if REPLICATE_KEY and "replicate" not in blocked_paid:
                providers.append((
                    f"Replicate ({REPLICATE_MODEL})",
                    lambda: generate_replicate(prompt, destination, size),
                    "replicate",
                    True,
                ))

        succeeded = False
        last_error = "No provider succeeded."

        for name, generate, provider_key, potentially_billable in providers:
            try:
                log(f"Scene {record['scene']}: trying {name}")
                generate()

                if not valid_image(destination, size):
                    destination.unlink(missing_ok=True)
                    raise ValueError(
                        "Provider output failed image integrity or size checks."
                    )

                record.update(
                    status="generated",
                    provider=name,
                    image_sha256=sha256_file(destination),
                )
                record["attempts"].append({
                    "provider": name,
                    "status": "success",
                })
                succeeded = True
                checkpoint()
                break

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                destination.unlink(missing_ok=True)
                log(f"Provider failed: {name}: {last_error[:250]}")

                record["attempts"].append({
                    "provider": name,
                    "status": "failed",
                    "error": last_error[:500],
                })

                if access_error(last_error):
                    if provider_key.startswith("hf:"):
                        blocked_hf.add(provider_key[3:])
                    elif potentially_billable:
                        blocked_paid.add(provider_key)

        if not succeeded:
            record["status"] = "failed"
            record["error"] = last_error[:800]
            log(
                f"Scene {record['scene']} failed; "
                "earlier successful images remain checkpointed."
            )

        checkpoint()

    failed = [
        row["scene"] for row in records if row["status"] == "failed"
    ]
    ready = sum(
        row["status"] in ("existing", "generated")
        for row in records
    )

    log(f"Images ready: {ready}/{len(records)}")
    log("Failed scenes:", failed)
    log("Image manifest:", IMAGE_MANIFEST)
    log("Generation manifest:", GENERATION_MANIFEST)

    if failed:
        raise RuntimeError(
            f"Image generation failed for scenes {failed}. "
            "Successful image checkpoints have been preserved."
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
