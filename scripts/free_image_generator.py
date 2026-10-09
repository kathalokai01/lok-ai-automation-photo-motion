#!/usr/bin/env python3
"""Katha Lok AI: free-only, resumable scene-image generation."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageOps
from io import BytesIO

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
Natural skin texture, believable anatomy and realistic lighting.
Consistent recurring characters and clothing.
No cartoon, anime, illustration, CGI, plastic skin,
distorted faces, extra limbs, text, logos or watermarks.
"""

session = requests.Session()


def log(message):
    print(message, flush=True)


def read_json(path, default=None):
    if not path.is_file():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Could not read JSON file {path}: {exc}"
        ) from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def image_size(job):
    try:
        width, height = map(
            int,
            str(job.get("image_size", "720x1280"))
            .lower()
            .split("x", 1),
        )

        if 256 <= width <= 2048 and 256 <= height <= 2048:
            return width, height
    except (TypeError, ValueError):
        pass

    return 720, 1280


def image_path(job, index):
    supplied = job.get("image_path")

    if supplied:
        candidate = (ROOT / str(supplied)).resolve()

        if candidate.is_relative_to(ROOT):
            return candidate

        raise ValueError(
            f"Scene {index} image path escapes the repository."
        )

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
        raise ValueError("Provider did not return a PIL image.")

    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_suffix(".tmp.png")
    image.save(temporary, "PNG", optimize=True)

    if not valid_image(temporary):
        temporary.unlink(missing_ok=True)
        raise ValueError("Generated image failed validation.")

    temporary.replace(destination)


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
            if job.get(key)
        ),
        "A realistic scene from an Indian story",
    )

    negative = str(job.get("negative_prompt", "")).strip()

    return f"{prompt}\n\n{QUALITY}\n\nAvoid: {negative}"


def generate_hf_space(prompt, destination, size, seed):
    """Use the configured Hugging Face Z-Image-Turbo Space."""

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
            raise ValueError(
                "Hugging Face Space returned an empty result."
            )
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
        if Path(result).is_file():
            with Image.open(result) as image:
                save_pil(image, destination, size)
            return

        if result.startswith(("https://", "http://")):
            response = session.get(
                result,
                timeout=TIMEOUT,
            )
            response.raise_for_status()

            content_type = response.headers.get(
                "content-type", ""
            ).lower()

            if not content_type.startswith("image/"):
                raise ValueError(
                    "Hugging Face returned a non-image URL response."
                )

            save_bytes(response.content, destination, size)
            return

    raise ValueError(
        "Hugging Face Space returned no usable image."
    )


def make_provider_chain():
    providers = [
        {
            "name": "Hugging Face ZeroGPU: Z-Image-Turbo",
            "run": generate_hf_space,
            "enabled": True,
            "potentially_billable": False,
            "reason": "",
        }
    ]

    # Safety first: never invoke potentially billable providers
    # unless the repository workflow explicitly enables them.
    if ALLOW_BILLABLE:
        log(
            "WARNING: Billable providers are enabled by configuration. "
            "This script does not automatically add unverified "
            "paid endpoints."
        )

    return providers


def sync_image_manifest(jobs, records):
    """Update the manifest expected by validate_final_output.py."""

    current = read_json(IMAGE_MANIFEST_FILE, {})

    if not isinstance(current, dict):
        current = {}

    existing_scenes = current.get("scenes", [])
    if not isinstance(existing_scenes, list):
        existing_scenes = []

    old_by_scene = {}

    for row in existing_scenes:
        if not isinstance(row, dict):
            continue

        try:
            number = int(
                row.get("global_scene", row.get("scene"))
            )
        except (TypeError, ValueError):
            continue

        old_by_scene[number] = row

    updated_scenes = []

    for job, record in zip(jobs, records):
        number = int(record["scene"])
        previous = old_by_scene.get(number, {})

        scene = dict(previous)

        scene.update({
            "scene": number,
            "global_scene": number,
            "part": job.get("part", scene.get("part", 1)),
            "local_scene": job.get(
                "local_scene",
                scene.get("local_scene", number),
            ),
            "image": record["file"],
            "image_path": record["file"],
            "duration": float(
                job.get("duration", scene.get("duration", 5.0))
            ),
            "status": record["status"],
            "provider": record.get("provider"),
        })

        updated_scenes.append(scene)

    ready = sum(
        row["status"] in ("existing", "generated")
        for row in records
    )

    current.update({
        "version": "photo-motion-3.0",
        "format": current.get("format", "unknown"),
        "total_scenes": len(jobs),
        "images_ready": ready,
        "images_required": len(jobs),
        "scenes": updated_scenes,
    })

    save_json(IMAGE_MANIFEST_FILE, current)


def main():
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    if not JOBS_FILE.is_file():
        raise SystemExit(
            f"Missing scene jobs file: {JOBS_FILE}"
        )

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
        raise SystemExit(
            "visual_jobs.json contains no scene jobs."
        )

    records = []
    pending = []
    seen_numbers = set()

    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise SystemExit(
                f"Invalid scene job at index {index}."
            )

        try:
            number = int(
                job.get("global_scene", job.get("scene", index))
            )
        except (TypeError, ValueError):
            number = index

        if number < 1 or number in seen_numbers:
            raise SystemExit(
                f"Invalid or duplicate global scene number: {number}"
            )

        seen_numbers.add(number)

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
            pending.append(
                (job, record, destination, size)
            )

        records.append(record)

    manifest = {
        "version": "photo-motion-image-generation-4.0",
        "allow_billable_providers": ALLOW_BILLABLE,
        "policy": (
            "Billable providers are not called unless explicitly enabled."
        ),
        "provider_order": [],
        "total_scenes": len(jobs),
        "scenes": records,
        "summary": {},
    }

    def checkpoint():
        manifest["summary"] = {
            "ready": sum(
                row["status"] in ("existing", "generated")
                for row in records
            ),
            "pending": sum(
                row["status"] == "pending"
                for row in records
            ),
            "failed": sum(
                row["status"] == "failed"
                for row in records
            ),
        }

        save_json(GENERATION_MANIFEST_FILE, manifest)
        sync_image_manifest(jobs, records)

    checkpoint()

    log("=" * 65)
    log("KATHA LOK AI — SCENE IMAGE GENERATION")
    log(f"Total scenes: {len(jobs)}")
    log(f"Already ready: {len(jobs) - len(pending)}")
    log(f"Billable providers allowed: {ALLOW_BILLABLE}")
    log("=" * 65)

    for provider in make_provider_chain():
        if not pending:
            break

        name = provider["name"]
        manifest["provider_order"].append(name)

        if not provider["enabled"]:
            log(f"SKIPPED: {name} — {provider['reason']}")

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
        provider_blocked = False

        for job, record, destination, size in pending:
            if provider_blocked:
                record["attempts"].append({
                    "provider": name,
                    "status": "skipped",
                    "reason": (
                        "Provider stopped after quota, "
                        "authentication, or access failure."
                    ),
                })

                remaining.append(
                    (job, record, destination, size)
                )
                continue

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

                    provider["run"](
                        prompt,
                        destination,
                        size,
                        seed,
                    )

                    if not valid_image(destination):
                        raise ValueError(
                            "Generated image failed validation."
                        )

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
                    last_error = (
                        f"{type(exc).__name__}: {exc}"
                    )

                    log(f"FAILED: {last_error[:300]}")

                    if (
                        destination.exists()
                        and not valid_image(destination)
                    ):
                        destination.unlink(missing_ok=True)

                    lower = last_error.lower()

                    stop_tokens = (
                        "quota",
                        "429",
                        "401",
                        "403",
                        "402",
                        "payment",
                        "billing",
                        "unauthorized",
                        "forbidden",
                        "zero gpu",
                        "zero-gpu",
                        "insufficient credit",
                        "rate limit",
                    )

                    if any(
                        token in lower
                        for token in stop_tokens
                    ):
                        provider_blocked = True
                        log(f"STOPPING PROVIDER: {name}")
                        break

                    if attempt < MAX_ATTEMPTS:
                        time.sleep(2)

            if not success:
                record["status"] = "pending"
                record["attempts"].append({
                    "provider": name,
                    "status": "failed",
                    "error": (
                        last_error[:800]
                        if last_error
                        else "Provider failed without an error message."
                    ),
                })

                remaining.append(
                    (job, record, destination, size)
                )

                checkpoint()

        pending = remaining
        log(f"Scenes still needing images: {len(pending)}")

    for _, record, _, _ in pending:
        record["status"] = "failed"

    checkpoint()

    ready = sum(
        row["status"] in ("existing", "generated")
        for row in records
    )
    failed = [
        row["scene"]
        for row in records
        if row["status"] == "failed"
    ]

    log("\nFINAL SUMMARY")
    log(f"Images ready: {ready}/{len(records)}")
    log(f"Missing scenes: {failed}")
    log(f"Generation manifest: {GENERATION_MANIFEST_FILE}")
    log(f"Image manifest: {IMAGE_MANIFEST_FILE}")

    if failed:
        raise SystemExit(
            "Image generation failed for scene(s): "
            + str(failed)
            + ". No paid-provider fallback was attempted."
        )

    log("ALL SCENE IMAGES READY")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"IMAGE GENERATION ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)