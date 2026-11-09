#!/usr/bin/env python3
"""Technical and Gemini-vision quality checks for generated photo-motion images."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageStat


class VisionVerificationUnavailable(RuntimeError):
    """Raised when visual verification cannot be completed reliably."""


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _technical_checks(image_path: Path, expected_size: tuple[int, int]) -> dict:
    if image_path.is_symlink() or not image_path.is_file():
        raise RuntimeError("Image file is missing or unsafe.")
    size_bytes = image_path.stat().st_size
    if size_bytes < 1024 or size_bytes > 80 * 1024 * 1024:
        raise RuntimeError(f"Image byte size is outside allowed limits: {size_bytes}.")
    try:
        with Image.open(image_path) as image:
            image.verify()
        with Image.open(image_path) as image:
            image.load()
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise RuntimeError(f"Unsupported image format: {image.format}.")
            if image.size != expected_size:
                raise RuntimeError(
                    f"Image dimensions {image.size} do not match {expected_size}."
                )
            gray = image.convert("L")
            stats = ImageStat.Stat(gray)
            variance = float(stats.var[0])
            extrema = gray.getextrema()
            if extrema[0] == extrema[1] or variance < 2.0:
                raise RuntimeError("Image has insufficient visual variation.")
            return {
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "size_bytes": size_bytes,
                "grayscale_variance": round(variance, 3),
            }
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Image decode/integrity check failed: {exc}") from exc


def _vision_check(
    image_path: Path,
    prompt: str,
    model: str,
    api_key: str,
    timeout: int,
) -> dict:
    try:
        image_bytes = image_path.read_bytes()
        encoded = base64.b64encode(image_bytes).decode("ascii")
    except OSError as exc:
        raise RuntimeError(f"Cannot read image for vision validation: {exc}") from exc

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + model
        + ":generateContent"
    )
    request_body = {
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "text": (
                        "Evaluate this generated still image against the exact scene prompt. "
                        "This is a photo-motion project: the image itself must be a coherent "
                        "still photograph, not an animation or video. Check whether the main "
                        "visible event, objects, location and time implied by the prompt match. "
                        "Check whether described character identity, gender evidence, face, "
                        "hair, body proportions and clothing are consistent with the prompt. "
                        "Do not invent details that are not visible. If a required detail cannot "
                        "be judged, set the relevant boolean to false and explain uncertainty. "
                        "Score overall technical/visual quality from 1 to 10. Reject obvious "
                        "distortion, malformed anatomy, duplicate limbs/people, severe blur, "
                        "unintended text/watermarks, unrelated scenes or major prompt mismatch. "
                        "Return ONLY JSON with keys: prompt_match (boolean), "
                        "character_consistency (boolean), quality_score (integer 1-10), "
                        "issues (array of short strings), reason (short string). "
                        "Scene prompt follows:\n" + prompt
                    )
                },
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": encoded,
                    }
                },
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = requests.post(
            endpoint,
            params={"key": api_key},
            json=request_body,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise VisionVerificationUnavailable(
            f"Gemini vision verification request failed: {type(exc).__name__}: {exc}"
        ) from exc

    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise VisionVerificationUnavailable(
            "Gemini vision verification returned no candidate."
        )
    parts = candidates[0].get("content", {}).get("parts", [])
    response_text = next(
        (_clean(part.get("text")) for part in parts
         if isinstance(part, dict) and part.get("text")),
        "",
    )
    if not response_text:
        raise VisionVerificationUnavailable(
            "Gemini vision verification returned empty text."
        )
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response_text, flags=re.DOTALL)
        if not match:
            raise VisionVerificationUnavailable(
                "Gemini vision verification did not return valid JSON."
            )
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise VisionVerificationUnavailable(
                "Gemini vision verification returned malformed JSON."
            ) from exc

    if not isinstance(result, dict):
        raise VisionVerificationUnavailable(
            "Gemini vision verification result is not a JSON object."
        )
    score = result.get("quality_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise VisionVerificationUnavailable(
            "Gemini vision verification omitted a numeric quality_score."
        )
    if not 1 <= float(score) <= 10:
        raise VisionVerificationUnavailable(
            "Gemini vision verification quality_score is outside 1-10."
        )
    if not isinstance(result.get("prompt_match"), bool):
        raise VisionVerificationUnavailable(
            "Gemini vision verification omitted prompt_match boolean."
        )
    if not isinstance(result.get("character_consistency"), bool):
        raise VisionVerificationUnavailable(
            "Gemini vision verification omitted character_consistency boolean."
        )

    issues = result.get("issues", [])
    if not isinstance(issues, list):
        issues = [_clean(issues)] if issues else []
    return {
        "prompt_match": result["prompt_match"],
        "character_consistency": result["character_consistency"],
        "quality_score": round(float(score), 2),
        "issues": [_clean(item)[:240] for item in issues if _clean(item)][:12],
        "reason": _clean(result.get("reason"))[:800],
        "vision_model": model,
    }


def validate_scene_image(
    image_path: str | Path,
    prompt: str,
    expected_size: tuple[int, int],
    *,
    enabled: bool = True,
    minimum_score: int = 7,
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
    timeout: int = 90,
) -> dict:
    """Return accepted quality evidence; never claim semantic success without vision."""
    path = Path(image_path)
    prompt = _clean(prompt)
    if len(prompt) < 24:
        raise RuntimeError("Quality validation requires the complete scene prompt.")
    technical = _technical_checks(path, expected_size)

    if not enabled:
        raise VisionVerificationUnavailable(
            "Vision quality validation is disabled; semantic matching cannot be claimed."
        )

    key = _clean(api_key or os.getenv("GEMINI_API_KEY"))
    if not key:
        raise VisionVerificationUnavailable(
            "GEMINI_API_KEY is missing; scene prompt and character matching cannot be verified."
        )
    model = _clean(model or "gemini-2.5-flash")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
        raise ValueError("IMAGE_QUALITY_VISION_MODEL contains invalid characters.")

    vision = _vision_check(path, prompt, model, key, max(15, min(180, int(timeout))))
    accepted = (
        vision["prompt_match"] is True
        and vision["character_consistency"] is True
        and vision["quality_score"] >= int(minimum_score)
    )
    return {
        "status": "passed" if accepted else "rejected",
        "accepted": accepted,
        "technical": technical,
        "vision": vision,
        "semantic_story_match": "verified" if accepted else "failed",
    }
