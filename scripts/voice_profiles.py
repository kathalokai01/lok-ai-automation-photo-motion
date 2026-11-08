#!/usr/bin/env python3
"""Deterministic voice profiles and delivery metadata for Katha Lok AI."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

VERSION = "voice-profiles-v1"

PRESETS: dict[str, dict[str, Any]] = {
    "child_friendly": {
        "label": "Child-friendly youthful synthetic voice",
        "delivery": "warm, youthful, playful, clear, gentle; do not imitate a real child",
        "pitch": 1.08,
        "speed": 0.96,
        "emotion": "playful",
    },
    "female_storyteller": {
        "label": "Warm female storyteller",
        "delivery": "warm, expressive, natural Hindi storytelling with clear diction",
        "pitch": 1.0,
        "speed": 0.98,
        "emotion": "warm",
    },
    "male_storyteller": {
        "label": "Expressive male storyteller",
        "delivery": "confident, expressive, natural Hindi storytelling with clear diction",
        "pitch": 0.96,
        "speed": 0.98,
        "emotion": "serious",
    },
    "character": {
        "label": "Character voice",
        "delivery": "distinctive but intelligible character performance; maintain identity",
        "pitch": 1.0,
        "speed": 1.0,
        "emotion": "neutral",
    },
}

EMOTION_GUIDANCE = {
    "curiosity": "sound curious and inviting; slightly lift energy at the end of key phrases",
    "happy": "sound genuinely happy and bright, without shouting",
    "sad": "use a softer, slower, compassionate delivery",
    "fear": "use restrained tension and cautious pacing; keep words clear",
    "suspense": "use controlled low energy, deliberate pauses and rising anticipation",
    "angry": "use firm emphasis and controlled intensity; avoid distortion or shouting",
    "excited": "increase energy and pace slightly while keeping diction clear",
    "dramatic": "use cinematic emphasis, measured pauses and dynamic phrasing",
    "calm": "use relaxed pacing, gentle emphasis and even volume",
    "warm": "use reassuring, affectionate and natural delivery",
    "neutral": "use clear, conversational delivery",
}

def _clean(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]

def resolve_voice_profile(config: dict[str, Any], record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve a stable voice profile without inventing provider voice IDs."""
    record = record if isinstance(record, dict) else {}
    audience = _clean(config.get("AUDIENCE", "adult")).lower()
    role = _clean(record.get("role", "main")).lower()
    speaker = _clean(record.get("speaker") or record.get("speaker_name") or record.get("character_name"))
    configured = _clean(config.get("TTS_PROFILE", "")).lower()
    character_mode = str(config.get("CHARACTER_VOICES", "true")).lower() in {"1", "true", "yes", "on"}
    if configured in PRESETS:
        preset = configured
    elif audience == "kids":
        preset = "child_friendly"
    elif role == "character" or (speaker and character_mode):
        preset = "character"
    else:
        voice = _clean(config.get("VOICE", "male")).lower()
        preset = "female_storyteller" if voice in {"female", "woman", "feminine"} else "male_storyteller"
    profile = dict(PRESETS[preset])
    profile["profile_id"] = preset
    profile["speaker_id"] = _clean(record.get("speaker_id") or speaker or "narrator").casefold()
    profile["speaker_name"] = speaker or "Narrator"
    emotion = _clean(record.get("emotion") or record.get("emotion_tag") or profile["emotion"]).lower()
    profile["emotion"] = emotion if emotion in EMOTION_GUIDANCE else "neutral"
    profile["emotion_instruction"] = EMOTION_GUIDANCE[profile["emotion"]]
    profile["delivery"] = profile["delivery"] + "; " + profile["emotion_instruction"]
    profile["voice_id"] = _clean(config.get("TTS_VOICE_ID", ""))
    profile["provider_voice_id"] = _clean(config.get("TTS_VOICE_ID_" + preset.upper(), ""))
    profile["fingerprint"] = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return profile

def profile_summary(config: dict[str, Any], record: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = resolve_voice_profile(config, record)
    return {key: profile[key] for key in ("profile_id", "speaker_id", "speaker_name", "emotion", "pitch", "speed", "fingerprint")}
