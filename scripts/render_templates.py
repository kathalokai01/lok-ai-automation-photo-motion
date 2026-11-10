#!/usr/bin/env python3
"""Reusable composition presets compatible with the existing FFmpeg renderer."""
TEMPLATES={"documentary":{"intro_seconds":0.0,"outro_seconds":0.0,"transition":"cut","caption_safe_bottom":0.12},"cinematic":{"intro_seconds":0.0,"outro_seconds":0.0,"transition":"crossfade","caption_safe_bottom":0.14},"minimal":{"intro_seconds":0.0,"outro_seconds":0.0,"transition":"cut","caption_safe_bottom":0.10}}
def get_template(name="documentary"):
    key=str(name or "documentary").strip().lower()
    if key not in TEMPLATES: raise ValueError(f"Unknown render template: {key}")
    return dict(TEMPLATES[key])
def template_names(): return tuple(sorted(TEMPLATES))
