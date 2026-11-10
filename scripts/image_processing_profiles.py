#!/usr/bin/env python3
"""Optional local image finishing; ComfyUI is never a required dependency."""
from __future__ import annotations
import os
from pathlib import Path
from PIL import Image,ImageEnhance
PROFILES={"none":(1.0,1.0,0.0),"natural":(1.04,1.02,0.15),"cinematic":(1.08,1.06,0.25),"soft":(1.02,0.98,0.0)}
def apply_profile(source,destination,profile="none"):
    name=str(profile or "none").lower()
    if name not in PROFILES: raise ValueError(f"Unknown image profile: {name}")
    contrast,color,sharp=PROFILES[name]; src=Path(source); dst=Path(destination); dst.parent.mkdir(parents=True,exist_ok=True)
    with Image.open(src) as im:
        image=im.convert("RGB")
        if contrast!=1: image=ImageEnhance.Contrast(image).enhance(contrast)
        if color!=1: image=ImageEnhance.Color(image).enhance(color)
        if sharp: image=ImageEnhance.Sharpness(image).enhance(1+sharp)
        image.save(dst,format="JPEG",quality=94,optimize=True)
    return dst
def comfyui_enabled(): return bool(os.getenv("COMFYUI_API_URL","").strip())
def comfyui_submit(workflow,timeout=300):
    if not comfyui_enabled(): raise RuntimeError("ComfyUI is optional; COMFYUI_API_URL is not configured")
    import requests
    response=requests.post(os.getenv("COMFYUI_API_URL","").rstrip("/")+"/prompt",json={"prompt":workflow},timeout=timeout); response.raise_for_status()
    payload=response.json()
    if not payload.get("prompt_id"): raise RuntimeError("ComfyUI did not return prompt_id")
    return payload
