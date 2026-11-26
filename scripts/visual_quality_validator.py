#!/usr/bin/env python3
"""Technical and Gemini-vision quality checks for generated photo-motion images."""

from __future__ import annotations

import base64
import json
import os
import re
import hashlib
import time
import csv
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests
from production_events import record_provider_attempt

from gemini_http import retry_after_seconds
from PIL import Image, ImageStat


class VisionVerificationUnavailable(RuntimeError):
    """Raised when visual verification cannot be completed reliably."""


class VisionQuotaExhausted(VisionVerificationUnavailable):
    """Raised when the Gemini project has no usable Vision quota."""


def is_hard_vision_quota_error(status: int | None, response_text: str) -> bool:
    """Detect exhausted project/tier quota without treating ordinary 429s as permanent."""
    if status not in {403, 429}:
        return False
    message = _clean(response_text).casefold()
    hard_quota_markers = (
        "limit: 0",
        "limit : 0",
        "free tier",
        "quota exceeded",
        "resource_exhausted",
        "resource exhausted",
        "billing account",
        "billing is disabled",
        "requests per day",
        "daily quota",
    )
    return any(marker in message for marker in hard_quota_markers)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _record_vision_provider_attempt(*args, **kwargs):
    """Keep mocked unit-test requests out of real provider usage accounting."""
    if os.getenv("PHOTO_MOTION_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}:
        return None
    return record_provider_attempt(*args, **kwargs)


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


def detect_embedded_text(image_path: str | Path) -> list[dict]:
    """Offline OCR gate: reject readable text before optional AI vision review."""
    executable = shutil.which("tesseract")
    if not executable:
        raise RuntimeError(
            "Tesseract OCR is required for the zero-text image gate."
        )
    try:
        languages = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True, text=True, timeout=20, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Cannot inspect Tesseract languages: {exc}") from exc
    available = {
        line.strip()
        for line in languages.stdout.splitlines()
        if line.strip() and not line.startswith("List of")
    }
    if not {"eng", "hin"}.issubset(available):
        raise RuntimeError(
            "Tesseract requires both eng and hin traineddata for image text checks."
        )
    try:
        result = subprocess.run(
            [
                executable, str(image_path), "stdout",
                "-l", "eng+hin", "--psm", "11", "tsv",
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Image OCR failed: {exc}") from exc

    detections = []
    reader = csv.DictReader(result.stdout.splitlines(), delimiter="\t")
    for row in reader:
        word = _clean(row.get("text"))
        try:
            confidence = float(row.get("conf", "-1"))
        except (TypeError, ValueError):
            continue
        if (
            word
            and len(word) >= 2
            and confidence >= 60
            and re.search(r"[A-Za-z0-9\u0900-\u097F]", word)
        ):
            detections.append({
                "text": word[:80],
                "confidence": round(confidence, 1),
            })
    return detections


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
    instruction = _vision_instruction(prompt)
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
        _record_vision_provider_attempt(
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
                _record_vision_provider_attempt(
                    "gemini_vision", model, "request_failed", True,
                    scene_id=scene_id, attempt_id=attempt_id,
                    http_status=status, error=str(response.text or "")[:500],
                )
            else:
                _record_vision_provider_attempt(
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
                _record_vision_provider_attempt(
                    "gemini_vision", model,
                    "response_received_validation_failed" if response_received else "request_failed",
                    True, scene_id=scene_id, attempt_id=attempt_id,
                    error=f"{type(exc).__name__}: {exc}", http_status=status,
                )
            response_text = ""
            error_response = getattr(exc, "response", None)
            if error_response is not None:
                response_text = _clean(
                    getattr(error_response, "text", "")
                    or getattr(error_response, "content", "")
                )
            if is_hard_vision_quota_error(status, response_text):
                raise VisionQuotaExhausted(
                    "Gemini Vision project/tier quota is exhausted; "
                    f"HTTP {status}. Further models share this project quota."
                ) from exc

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
            _record_vision_provider_attempt(
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

    _record_vision_provider_attempt(
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
        "vision_provider": "gemini",
        "reference_images_used": reference_count,
    }



def _openai_vision_check(
    image_path: Path,
    prompt: str,
    model: str,
    api_key: str,
    timeout: int,
    reference_image_paths: list[Path] | None = None,
    scene_id: str = "",
    max_attempts: int = 2,
) -> dict:
    """Independent OpenAI vision verifier with a separate API/quota."""
    try:
        image_bytes = image_path.read_bytes()
        encoded = base64.b64encode(image_bytes).decode("ascii")
    except OSError as exc:
        raise VisionVerificationUnavailable(
            f"Cannot read image for OpenAI verification: {exc}"
        ) from exc

    image_sha = hashlib.sha256(image_bytes).hexdigest()
    content = [{"type": "text", "text": _vision_instruction(prompt)}]
    reference_count = 0
    for reference_path in reference_image_paths or []:
        try:
            reference_bytes = Path(reference_path).read_bytes()
        except OSError:
            continue
        if not reference_bytes or len(reference_bytes) > 80 * 1024 * 1024:
            continue
        reference_count += 1
        content.append({
            "type": "text",
            "text": f"Earlier scene character reference image {reference_count}:",
        })
        content.append({
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(reference_bytes).decode("ascii"),
                "detail": "high",
            },
        })
    content.append({"type": "text", "text": "Candidate image to accept or reject:"})
    content.append({
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + encoded, "detail": "high"},
    })

    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 700,
    }
    attempts = max(1, min(2, int(max_attempts or 2)))
    for attempt in range(1, attempts + 1):
        attempt_id = f"openai-vision:{scene_id or image_sha[:12]}:{image_sha[:12]}:{model}:{attempt}"
        _record_vision_provider_attempt(
            "openai_vision", model, "request_started", True,
            scene_id=scene_id, attempt_id=attempt_id,
        )
        try:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=max(10, min(45, int(timeout))),
            )
            status = int(response.status_code)
            _record_vision_provider_attempt(
                "openai_vision", model,
                "response_received" if status < 400 else "request_failed",
                True, scene_id=scene_id, attempt_id=attempt_id,
                http_status=status,
                error=str(response.text or "")[:500] if status >= 400 else None,
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices else None
            raw = message.get("content") if isinstance(message, dict) else None
            if isinstance(raw, list):
                raw = " ".join(
                    _clean(part.get("text"))
                    for part in raw if isinstance(part, dict) and part.get("text")
                )
            result = json.loads(_clean(raw))
            if not isinstance(result, dict):
                raise ValueError("not a JSON object")
            score = result.get("quality_score")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not 1 <= float(score) <= 10
                or not isinstance(result.get("prompt_match"), bool)
                or not isinstance(result.get("character_consistency"), bool)
            ):
                raise ValueError("required vision fields missing or invalid")
            issues = result.get("issues", [])
            if not isinstance(issues, list):
                issues = [_clean(issues)] if issues else []
            _record_vision_provider_attempt(
                "openai_vision", model, "accepted", True,
                scene_id=scene_id, attempt_id=attempt_id,
            )
            return {
                "prompt_match": result["prompt_match"],
                "character_consistency": result["character_consistency"],
                "quality_score": round(float(score), 2),
                "issues": [_clean(item)[:240] for item in issues if _clean(item)][:12],
                "reason": _clean(result.get("reason"))[:800],
                "vision_model": model,
                "vision_provider": "openai",
                "reference_images_used": reference_count,
            }
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status in VISION_RETRYABLE_STATUSES or isinstance(
                exc, (requests.Timeout, requests.ConnectionError)
            )
            if not retryable or attempt >= attempts:
                detail = f"HTTP {status}" if status else type(exc).__name__
                raise VisionVerificationUnavailable(
                    f"Independent OpenAI vision verifier unavailable: {detail}."
                ) from exc
            time.sleep(min(4, attempt * 2))
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise VisionVerificationUnavailable(
                f"OpenAI vision response could not be validated: {type(exc).__name__}."
            ) from exc

    raise VisionVerificationUnavailable(
        "Independent OpenAI vision verifier failed without a response."
    )



def _vision_instruction(prompt: str) -> str:
    return (
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
        "C. ZERO-TEXT POLICY (mandatory): this pipeline overlays its own separate "
        "captions after image generation. Therefore the image itself must contain NO "
        "readable words, letters, Hindi/Devanagari, English text, digits, subtitles, "
        "captions, signs, labels, logos, watermarks, UI text or pseudo-writing. This "
        "rule has no story-based exception. If any legible text is visible anywhere "
        "in the candidate, add the issue 'unwanted_text_in_image' and set prompt_match "
        "false. Blank screens, blank signboards and unmarked paper are acceptable.\\n"
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
    allow_technical_only_on_vision_unavailable: bool = False,
) -> dict:
    """Return quality evidence; technical-only acceptance is explicitly marked."""
    path = Path(image_path)
    prompt = _clean(prompt)
    if len(prompt) < 24:
        raise RuntimeError("Quality validation requires the complete scene prompt.")
    technical = _technical_checks(path, expected_size)
    text_detections = detect_embedded_text(path)
    if text_detections:
        print(
            "OCR REJECTED IMAGE:",
            path.name,
            "| detection_count:",
            len(text_detections),
            "| samples:",
            json.dumps(text_detections[:12], ensure_ascii=False),
            flush=True,
        )
        return {
            "status": "rejected",
            "accepted": False,
            "vision_accepted": False,
            "vision_degraded": False,
            "technical": technical,
            "vision": None,
            "verification_warning": "OCR detected readable text inside the generated image.",
            "semantic_story_match": "not_verified",
            "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "text_detection": {
                "status": "readable_text_detected",
                "detections": text_detections[:20],
            },
            "verification_contract": "mandatory_technical_integrity+offline_ocr_zero_text_gate",
        }

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
        reference_paths = [
            Path(value) for value in (reference_image_paths or [])[:2]
        ]
        configured_fallbacks = (
            os.getenv("GEMINI_VISION_MODEL_FALLBACKS", "").strip()
            or os.getenv("GEMINI_MODEL_FALLBACKS", "").strip()
        )
        vision_models = [model] if key else []
        if not key:
            vision_errors = ["Gemini API key is missing; attempting independent verifier."]
        else:
            vision_errors = []
        for candidate in (re.split(r"[,;\s]+", configured_fallbacks) if key else []):
            candidate = _clean(candidate)
            if (
                candidate
                and candidate not in vision_models
                and re.fullmatch(r"[A-Za-z0-9_.-]+", candidate)
            ):
                vision_models.append(candidate)
            if len(vision_models) >= 4:
                break

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
            except VisionQuotaExhausted as exc:
                vision_errors.append(
                    f"{vision_model}: {type(exc).__name__}: {exc}"
                )
                print(
                    "Gemini Vision project quota exhausted; skipping all "
                    "configured model fallbacks that share this project.",
                    flush=True,
                )
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
            openai_key = _clean(os.getenv("OPENAI_API_KEY"))
            if openai_key:
                openai_model = _clean(os.getenv("OPENAI_VISION_MODEL") or "gpt-4.1-mini")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", openai_model):
                    raise ValueError("OPENAI_VISION_MODEL contains invalid characters.")
                print(
                    "Gemini Vision unavailable; trying independent OpenAI verifier:",
                    openai_model, flush=True,
                )
                try:
                    vision = _openai_vision_check(
                        path, prompt, openai_model, openai_key,
                        max(10, min(35, int(timeout))), reference_paths,
                        scene_id=_clean(scene_id), max_attempts=2,
                    )
                    model = openai_model
                except VisionVerificationUnavailable as exc:
                    vision_errors.append(
                        f"openai:{openai_model}: {type(exc).__name__}: {exc}"
                    )
            else:
                print(
                    "Independent OpenAI Vision fallback skipped: "
                    "OPENAI_API_KEY is not configured.", flush=True,
                )

        if vision is None:
            details = " | ".join(vision_errors[-4:])
            vision_error = (
                "Gemini Vision unavailable; only technical image integrity "
                "was verified. Semantic scene match and character continuity "
                "remain unverified. " + details
            )
            if not allow_technical_only_on_vision_unavailable:
                raise VisionVerificationUnavailable(
                    "Gemini Vision verification failed on the primary and "
                    f"configured fallback models. {details}"
                )
            print(
                "WARNING: Gemini Vision unavailable; accepting this image "
                "only after mandatory technical integrity checks. Semantic "
                "story match is NOT verified.",
                flush=True,
            )
    else:
        vision_error = "Vision verification is disabled by configuration."

    technical_only_fallback = bool(
        enabled and vision is None and allow_technical_only_on_vision_unavailable
    )
    vision_issues = vision.get("issues", []) if isinstance(vision, dict) else []
    unwanted_text_detected = any(
        "unwanted_text_in_image" in _clean(item).casefold()
        for item in vision_issues
    ) if isinstance(vision_issues, list) else False
    vision_accepted = bool(
        isinstance(vision, dict)
        and not unwanted_text_detected
        and vision.get("prompt_match") is True
        and vision.get("character_consistency") is True
        and isinstance(vision.get("quality_score"), (int, float))
        and not isinstance(vision.get("quality_score"), bool)
        and vision.get("quality_score") >= max(7, min(10, int(minimum_score)))
    )
    accepted = vision_accepted or technical_only_fallback if enabled else True
    if enabled and not vision_accepted and not technical_only_fallback:
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
            else "accepted_without_vision" if technical_only_fallback or not enabled
            else "rejected"
        ),
        "accepted": accepted,
        "vision_accepted": vision_accepted,
        "vision_degraded": technical_only_fallback,
        "technical": technical,
        "vision": vision,
        "verification_warning": vision_error,
        "semantic_story_match": "verified" if vision_accepted else "not_verified",
        "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "verification_contract": (
            "mandatory_technical_integrity+mandatory_vision_gate"
            if vision_accepted
            else "mandatory_technical_integrity+vision_unavailable_degraded"
            if technical_only_fallback
            else "mandatory_technical_integrity+vision_disabled"
        ),
    }



def processing_profile_is_optional() -> bool:
    """ComfyUI/profile processing is optional and must not gate image validation."""
    import os
    return not bool(os.getenv("COMFYUI_API_URL", "").strip())
