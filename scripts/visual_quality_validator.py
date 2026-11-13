#!/usr/bin/env python3
"""Technical and Gemini-vision quality checks for generated photo-motion images."""

from __future__ import annotations

import base64
import json
import os
import re
import hashlib
import time
from pathlib import Path
from typing import Any

import requests
from production_events import record_provider_attempt

from gemini_http import retry_after_seconds
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


VISION_HTTP_MAX_ATTEMPTS = 4
VISION_RETRYABLE_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def _vision_check(
    image_path: Path,
    prompt: str,
    model: str,
    api_key: str,
    timeout: int,
    reference_image_paths: list[Path] | None = None,
    scene_id: str = "",
    max_attempts: int | None = None,
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
    instruction = (
        "You are a careful visual quality inspector for a still-image photo-motion "
        "story pipeline. Inspect the actual candidate pixels; do not judge from the "
        "filename, metadata, provider, or assumptions. Compare the image with the "
        "complete scene prompt below. The prompt is the authority for what must be "
        "visible. Do not invent requirements that the prompt does not state.\\n\\n"
        "A. EXACT STORY/SCENE MATCH (highest priority): identify the single scene "
        "action and its key event from the prompt. Confirm that the candidate visibly "
        "depicts that event, its named objects, location, time period and stated "
        "cause/consequence where specified. A generic image that only shares the "
        "story mood or topic is NOT a match. Mark prompt_match false only for a clear, "
        "material contradiction, missing central event, wrong subject, wrong setting "
        "or unrelated scene. Do not fail for harmless composition differences.\\n"
        "B. REQUIRED DETAILS: check explicitly required visible subjects, objects, "
        "animals, vehicles, clothing, actions and environmental details. Distinguish "
        "required details from optional descriptive language. Do not demand tiny "
        "background details that cannot be inspected at this image size.\\n"
        "C. TEXT POLICY: flag random letters, gibberish, pseudo-writing, unrequested "
        "captions, subtitles, title cards, speech bubbles, logos and watermarks. Text "
        "is allowed only when the prompt explicitly requires a specific written object "
        "or exact wording. Do not call natural textures, architectural patterns or "
        "distant indistinct marks text unless readable characters are clearly visible.\\n"
        "D. IMAGE DEFECTS: inspect for severe blur, pixelation, compression damage, "
        "blank/near-blank output, extreme exposure, important subject cut-off, duplicate "
        "persons, extra/fused limbs, malformed hands/fingers, distorted faces, broken "
        "anatomy, impossible object geometry, inconsistent shadows, floating objects, "
        "unwanted collage/split panels, borders and obvious AI artifacts. Report only "
        "defects clearly visible in the candidate.\\n"
        "E. CHARACTER CONTINUITY: if references are provided, compare identity-defining "
        "traits (face, age appearance, skin tone, hair and stable features). Do not "
        "require identical pose, expression, camera angle, lighting or clothing unless "
        "the prompt locks that clothing. If no character is described, set "
        "character_consistency true. If identity cannot reasonably be judged, do not "
        "invent a mismatch; set it true and briefly mention uncertainty.\\n"
        "F. FAIR SCORING: quality_score is an integer 1-10. Use 9-10 for clean, clear, "
        "scene-accurate images; 7-8 for usable images with minor imperfections; 5-6 "
        "for noticeable but non-fatal defects; 1-4 only for severe defects or major "
        "scene mismatch. Do not lower the score for subjective style preference, minor "
        "harmless variation or details the prompt never required. Do not give a perfect "
        "score automatically.\\n"
        "Return ONLY valid JSON with exactly these keys: prompt_match (boolean), "
        "character_consistency (boolean), quality_score (integer 1-10), "
        "issues (array of concise strings naming only visible problems), and reason "
        "(short evidence-based explanation). If no visible defect exists, use an empty "
        "issues array. Never reject based on speculation.\\n\\n"
        "COMPLETE SCENE PROMPT:\\n" + prompt
    )
    parts = [{"text": instruction}]
    reference_count = 0
    for reference_path in reference_image_paths or []:
        try:
            reference_bytes = Path(reference_path).read_bytes()
        except OSError:
            continue
        if not reference_bytes or len(reference_bytes) > 80 * 1024 * 1024:
            continue
        reference_count += 1
        parts.append({
            "text": f"Earlier scene character reference image {reference_count}:"
        })
        parts.append({
            "inlineData": {
                "mimeType": "image/png",
                "data": base64.b64encode(reference_bytes).decode("ascii"),
            }
        })
    parts.append({
        "text": "Candidate image to accept or reject:"
    })
    parts.append({
        "inlineData": {
            "mimeType": "image/png",
            "data": encoded,
        }
    })

    request_body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    payload = None
    last_http_error = None
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    attempt_limit = max(
        1,
        min(
            VISION_HTTP_MAX_ATTEMPTS,
            int(max_attempts or VISION_HTTP_MAX_ATTEMPTS),
        ),
    )
    for attempt in range(1, attempt_limit + 1):
        attempt_id = (
            f"vision:{scene_id or image_sha[:12]}:{image_sha[:12]}:"
            f"{model}:{attempt}"
        )
        response_received = False
        record_provider_attempt(
            "gemini_vision", model, "request_started", True,
            scene_id=scene_id, attempt_id=attempt_id,
        )
        try:
            response = requests.post(
                endpoint,
                params={"key": api_key},
                json=request_body,
                timeout=timeout,
            )
            response_received = True
            status = int(response.status_code)
            if status >= 400:
                record_provider_attempt(
                    "gemini_vision", model, "request_failed", True,
                    scene_id=scene_id, attempt_id=attempt_id,
                    http_status=status, error=str(response.text or "")[:500],
                )
            else:
                record_provider_attempt(
                    "gemini_vision", model, "response_received", True,
                    scene_id=scene_id, attempt_id=attempt_id,
                    http_status=status,
                )
            response.raise_for_status()
            payload = response.json()
            break
        except requests.RequestException as exc:
            last_http_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is None or status < 400:
                record_provider_attempt(
                    "gemini_vision", model,
                    "response_received_validation_failed" if response_received else "request_failed",
                    True, scene_id=scene_id, attempt_id=attempt_id,
                    error=f"{type(exc).__name__}: {exc}", http_status=status,
                )
            transient_transport = isinstance(
                exc, (requests.Timeout, requests.ConnectionError)
            )
            retryable = (
                status in VISION_RETRYABLE_STATUSES or transient_transport
            )
            if not retryable or attempt >= attempt_limit:
                detail = f"HTTP {status}" if status else type(exc).__name__
                raise VisionVerificationUnavailable(
                    f"Gemini vision verification request failed after {attempt} attempt(s): {detail}."
                ) from exc
            delay = retry_after_seconds(exc, attempt, default_base=4, maximum=20)
            failure_kind = f"HTTP {status}" if status else type(exc).__name__
            print(
                f"Gemini Vision transient {failure_kind}; retry "
                f"{attempt + 1}/{attempt_limit} in {delay}s.",
                flush=True,
            )
            time.sleep(delay)
        except ValueError as exc:
            record_provider_attempt(
                "gemini_vision", model, "response_received_validation_failed", True,
                scene_id=scene_id, attempt_id=attempt_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise VisionVerificationUnavailable(
                "Gemini vision verification returned invalid JSON."
            ) from exc
    if payload is None:
        detail = type(last_http_error).__name__ if last_http_error else "empty response"
        raise VisionVerificationUnavailable(
            f"Gemini vision verification unavailable: {detail}."
        )

    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise VisionVerificationUnavailable(
            "Gemini vision verification returned no candidate."
        )
    candidate = candidates[0]
    candidate_content = candidate.get("content") if isinstance(candidate, dict) else None
    response_parts = (
        candidate_content.get("parts", [])
        if isinstance(candidate_content, dict) else []
    )
    response_text = next(
        (_clean(part.get("text")) for part in response_parts
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

    record_provider_attempt(
        "gemini_vision", model, "accepted", True,
        scene_id=scene_id, attempt_id=attempt_id,
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
        "reference_images_used": reference_count,
    }


def validate_scene_image(
    image_path: str | Path,
    prompt: str,
    expected_size: tuple[int, int],
    *,
    enabled: bool = True,
    minimum_score: int = 7,
    model: str = "gemini-3.8-flash",
    api_key: str | None = None,
    timeout: int = 30,
    reference_image_paths: list[str | Path] | None = None,
    scene_id: str = "",
) -> dict:
    """Return accepted quality evidence; never claim semantic success without vision."""
    path = Path(image_path)
    prompt = _clean(prompt)
    if len(prompt) < 24:
        raise RuntimeError("Quality validation requires the complete scene prompt.")
    technical = _technical_checks(path, expected_size)

    model = _clean(model or "gemini-2.5-flash")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
        raise ValueError("IMAGE_QUALITY_VISION_MODEL contains invalid characters.")

    # Technical integrity is mandatory. When Vision is enabled, semantic scene
    # match, character consistency and minimum clarity score are also mandatory.
    # The caller preserves a generated candidate when Vision is unavailable, then
    # retries verification later without paying for image generation again.
    vision = None
    vision_error = None
    if enabled:
        key = _clean(api_key or os.getenv("GEMINI_API_KEY"))
        if not key:
            raise VisionVerificationUnavailable(
                "GEMINI_API_KEY is missing; image quality cannot be verified."
            )
        reference_paths = [
            Path(value) for value in (reference_image_paths or [])[:2]
        ]
        configured_fallbacks = (
            os.getenv("GEMINI_VISION_MODEL_FALLBACKS", "").strip()
            or os.getenv("GEMINI_MODEL_FALLBACKS", "").strip()
        )
        vision_models = [model]
        for candidate in re.split(r"[,;\s]+", configured_fallbacks):
            candidate = _clean(candidate)
            if (
                candidate
                and candidate not in vision_models
                and re.fullmatch(r"[A-Za-z0-9_.-]+", candidate)
            ):
                vision_models.append(candidate)
            if len(vision_models) >= 4:
                break

        vision_errors = []
        for model_index, vision_model in enumerate(vision_models):
            try:
                vision = _vision_check(
                    path, prompt, vision_model, key,
                    max(10, min(30, int(timeout))),
                    reference_paths,
                    scene_id=_clean(scene_id),
                    max_attempts=(
                        VISION_HTTP_MAX_ATTEMPTS
                        if model_index == 0 else 2
                    ),
                )
                model = vision_model
                break
            except VisionVerificationUnavailable as exc:
                vision_errors.append(
                    f"{vision_model}: {type(exc).__name__}: {exc}"
                )
                if model_index + 1 < len(vision_models):
                    print(
                        "Gemini Vision model unavailable; trying configured "
                        f"fallback {vision_models[model_index + 1]}.",
                        flush=True,
                    )
            except Exception as exc:
                vision_errors.append(
                    f"{vision_model}: {type(exc).__name__}: {exc}"
                )

        if vision is None:
            details = " | ".join(vision_errors[-4:])
            raise VisionVerificationUnavailable(
                "Gemini Vision verification failed on the primary and "
                f"configured fallback models. {details}"
            )
    else:
        vision_error = "Vision verification is disabled by configuration."

    vision_accepted = bool(
        isinstance(vision, dict)
        and vision.get("prompt_match") is True
        and vision.get("character_consistency") is True
        and isinstance(vision.get("quality_score"), (int, float))
        and not isinstance(vision.get("quality_score"), bool)
        and vision.get("quality_score") >= max(7, min(10, int(minimum_score)))
    )
    accepted = vision_accepted if enabled else True
    if enabled and not vision_accepted:
        issues = vision.get("issues", []) if isinstance(vision, dict) else []
        details = "; ".join(_clean(item) for item in issues[:4] if _clean(item)) if isinstance(issues, list) else ""
        vision_error = (
            "Vision rejected the scene match, character continuity or clarity score."
        )
        if details:
            vision_error += " " + details[:500]

    return {
        "status": (
            "passed" if vision_accepted
            else "rejected" if enabled
            else "accepted_without_vision"
        ),
        "accepted": accepted,
        "vision_accepted": vision_accepted,
        "technical": technical,
        "vision": vision,
        "verification_warning": vision_error,
        "semantic_story_match": "verified" if vision_accepted else "not_verified",
        "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "verification_contract": (
            "mandatory_technical_integrity+mandatory_vision_gate"
            if enabled else "mandatory_technical_integrity+vision_disabled"
        ),
    }



def processing_profile_is_optional() -> bool:
    """ComfyUI/profile processing is optional and must not gate image validation."""
    import os
    return not bool(os.getenv("COMFYUI_API_URL", "").strip())
