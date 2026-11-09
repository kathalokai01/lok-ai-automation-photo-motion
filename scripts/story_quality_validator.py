#!/usr/bin/env python3
"""Deterministic quality gates for generated Story artifacts.

This module does not make external API calls and does not validate TTS/audio.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

VERSION = "story-quality-validator-v1"
MIN_SCORE = 8


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\\s+", " ", str(value)).strip()


def _sha(value: str) -> str:
    return hashlib.sha256(_clean(value).encode("utf-8")).hexdigest()


def _source_part_signature(part: dict) -> str:
    payload = {
        "part": part.get("part"),
        "scenes": part.get("scenes"),
        "hook_scene": part.get("hook_scene"),
        "suspense_scene": part.get("suspense_scene"),
        "resolution_scene": part.get("resolution_scene"),
        "final_part_merged": bool(part.get("final_part_merged")),
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_story_part(
    generated: dict,
    source_part: dict,
    *,
    total_parts: int,
    minimum_score: int = MIN_SCORE,
) -> dict:
    """Validate one AI-story Part against the locked source plan."""
    issues: list[str] = []
    checks: dict[str, bool] = {}
    source_scenes = source_part.get("scenes")
    actual_scenes = generated.get("scenes")
    checks["part_identity"] = (
        generated.get("part") == source_part.get("part")
        and generated.get("status") == "completed"
    )
    if not checks["part_identity"]:
        issues.append("Part identity/status mismatch.")

    checks["scene_count"] = (
        isinstance(source_scenes, list)
        and isinstance(actual_scenes, list)
        and len(source_scenes) == len(actual_scenes)
        and bool(actual_scenes)
    )
    if not checks["scene_count"]:
        issues.append("Generated scene count differs from the locked Story Plan.")
        source_scenes = source_scenes if isinstance(source_scenes, list) else []
        actual_scenes = actual_scenes if isinstance(actual_scenes, list) else []

    narration_hashes: set[str] = set()
    prompt_hashes: set[str] = set()
    source_anchor_counts: dict[str, int] = {}
    for source in source_scenes:
        digest = _sha(_clean(source.get("story_anchor")))
        source_anchor_counts[digest] = source_anchor_counts.get(digest, 0) + 1
    scene_checks = []
    for index, source in enumerate(source_scenes):
        if index >= len(actual_scenes):
            scene_checks.append(False)
            continue
        row = actual_scenes[index]
        if not isinstance(row, dict):
            scene_checks.append(False)
            issues.append(f"Scene {index + 1} is not an object.")
            continue
        narration = _clean(row.get("narration"))
        anchor = _clean(source.get("story_anchor"))
        visual_prompt = _clean(row.get("visual_prompt"))
        valid = (
            row.get("scene") == source.get("scene")
            and row.get("scene_id") == source.get("scene_id")
            and _clean(row.get("story_anchor")) == anchor
            and row.get("role") == "main"
            and row.get("status") == "completed"
            and bool(narration)
            and narration == anchor
            and bool(visual_prompt)
            and len(visual_prompt) >= 30
            and bool(_clean(row.get("camera_prompt")))
            and bool(_clean(row.get("lighting_prompt")))
        )
        if narration:
            digest = _sha(narration)
            if digest in narration_hashes and source_anchor_counts.get(digest, 0) <= 1:
                valid = False
                issues.append(f"Repeated narration detected in Part scene {index + 1}.")
            narration_hashes.add(digest)
        if visual_prompt:
            digest = _sha(visual_prompt)
            if digest in prompt_hashes:
                valid = False
                issues.append(f"Identical visual prompt repeated in Part scene {index + 1}.")
            prompt_hashes.add(digest)
        if not valid:
            issues.append(f"Scene {index + 1} failed source-lock or prompt-quality checks.")
        scene_checks.append(valid)

    checks["scene_integrity"] = bool(scene_checks) and all(scene_checks)
    if not checks["scene_integrity"] and not scene_checks:
        issues.append("No valid main scenes were available for quality checks.")

    expected_signature = generated.get("source_part_signature")
    checks["source_signature"] = (
        isinstance(expected_signature, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected_signature) is not None
        and expected_signature == _source_part_signature(source_part)
    )
    if not checks["source_signature"]:
        issues.append("Source Part signature is missing or invalid.")

    title = _clean(generated.get("story_title"))
    checks["story_title"] = bool(title)
    if not checks["story_title"]:
        issues.append("Story title is missing.")

    score = round(10 * sum(checks.values()) / max(1, len(checks)), 2)
    passed = all(checks.values()) and score >= int(minimum_score)
    return {
        "version": VERSION,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "score": score,
        "minimum_score": int(minimum_score),
        "checks": checks,
        "issues": issues,
        "part": source_part.get("part"),
        "source_part_signature": expected_signature,
        "narration_sha256": _sha(" ".join(
            _clean(row.get("narration"))
            for row in actual_scenes if isinstance(row, dict)
        )),
    }


def validate_story_output(
    ai_story: dict,
    source_plan: dict,
    *,
    minimum_score: int = MIN_SCORE,
) -> dict:
    """Validate every completed Part and the final story-level invariants."""
    source_parts = source_plan.get("parts")
    output_parts = ai_story.get("parts")
    issues: list[str] = []
    reports: list[dict] = []
    if not isinstance(source_parts, list) or not isinstance(output_parts, list):
        return {
            "version": VERSION, "status": "failed", "passed": False,
            "score": 0, "issues": ["Story or Story Plan Parts are missing."],
            "parts": [],
        }
    if len(source_parts) != len(output_parts):
        issues.append("Story and Story Plan Part counts differ.")
    by_number = {
        item.get("part"): item for item in output_parts
        if isinstance(item, dict) and isinstance(item.get("part"), int)
    }
    for source in source_parts:
        number = source.get("part")
        generated = by_number.get(number)
        if generated is None:
            issues.append(f"Part {number} is missing.")
            continue
        report = validate_story_part(
            generated, source, total_parts=len(source_parts),
            minimum_score=minimum_score,
        )
        reports.append(report)
        if not report["passed"]:
            issues.extend(f"Part {number}: {item}" for item in report["issues"])

    titles = {
        _clean(item.get("story_title")).casefold()
        for item in output_parts if isinstance(item, dict)
    }
    titles.discard("")
    if len(titles) != 1:
        issues.append("Story title is missing or inconsistent across Parts.")

    scores = [item["score"] for item in reports]
    score = round(sum(scores) / len(scores), 2) if scores else 0
    passed = (
        len(reports) == len(source_parts)
        and all(item["passed"] for item in reports)
        and len(titles) == 1
        and not issues
    )
    return {
        "version": VERSION,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "score": score,
        "minimum_score": int(minimum_score),
        "part_count": len(source_parts),
        "validated_part_count": len(reports),
        "issues": issues,
        "parts": reports,
    }
