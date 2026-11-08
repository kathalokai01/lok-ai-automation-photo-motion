#!/usr/bin/env python3
"""Shared immutable story-text validation helpers for the photo-motion pipeline."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable


def normalize_story_text(value: Any) -> str:
    """Normalize whitespace only; preserve every word, punctuation mark and order."""
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text).strip()


def story_word_count(value: Any) -> int:
    return len(normalize_story_text(value).split())


def story_text_sha256(value: Any) -> str:
    return hashlib.sha256(
        normalize_story_text(value).encode("utf-8")
    ).hexdigest()


def require_locked_text(
    record: Any,
    *,
    text_field: str = "narration",
    hash_field: str = "locked_text_sha256",
    count_field: str = "locked_word_count",
    label: str = "story text",
) -> str:
    if not isinstance(record, dict):
        raise ValueError(f"{label}: record must be an object.")
    text = normalize_story_text(record.get(text_field))
    if not text:
        raise ValueError(f"{label}: text is empty.")
    if record.get("text_locked") is not True:
        raise ValueError(f"{label}: text is not marked as locked.")
    if record.get(hash_field) != story_text_sha256(text):
        raise ValueError(f"{label}: text fingerprint mismatch.")
    if record.get(count_field) != story_word_count(text):
        raise ValueError(f"{label}: locked word count mismatch.")
    return text


def require_exact_reconstruction(
    source_text: Any,
    segments: Iterable[Any],
    *,
    label: str = "story text",
) -> str:
    source = normalize_story_text(source_text)
    pieces = [normalize_story_text(item) for item in segments]
    if not source:
        raise ValueError(f"{label}: source text is empty.")
    if any(not item for item in pieces):
        raise ValueError(f"{label}: one or more segments are empty.")
    rebuilt = normalize_story_text(" ".join(pieces))
    if rebuilt != source:
        raise ValueError(
            f"{label}: segments contain missing, duplicated, added, "
            "or reordered text."
        )
    if story_word_count(rebuilt) != story_word_count(source):
        raise ValueError(f"{label}: reconstructed word count mismatch.")
    return rebuilt


def require_scene_source(
    scene: Any,
    *,
    label: str = "scene",
    narration_field: str = "narration",
    hash_field: str = "source_text_sha256",
    count_field: str = "source_word_count",
) -> str:
    if not isinstance(scene, dict):
        raise ValueError(f"{label}: scene must be an object.")
    narration = normalize_story_text(scene.get(narration_field))
    if not narration:
        raise ValueError(f"{label}: narration is empty.")
    if scene.get(hash_field) != story_text_sha256(narration):
        raise ValueError(f"{label}: source narration hash mismatch.")
    if scene.get(count_field) != story_word_count(narration):
        raise ValueError(f"{label}: source narration word count mismatch.")
    return narration


def require_visual_only_role(record: Any, *, label: str = "extra scene") -> None:
    if not isinstance(record, dict):
        raise ValueError(f"{label}: record must be an object.")
    if normalize_story_text(record.get("narration")):
        raise ValueError(f"{label}: visual-only narration must be empty.")
    if record.get("voiceover_enabled") is not False:
        raise ValueError(f"{label}: voiceover must be disabled.")
    if record.get("narration_source") != "visual_only":
        raise ValueError(f"{label}: narration_source must be visual_only.")
