#!/usr/bin/env python3
"""Named optional production presets; never silently override topic.txt."""
from __future__ import annotations
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PRESETS={"default":{"render_template":"documentary","image_profile":"none","preview_enabled":True,"asset_review_required":False},"cinematic":{"render_template":"cinematic","image_profile":"cinematic","preview_enabled":True,"asset_review_required":False},"natural":{"render_template":"documentary","image_profile":"natural","preview_enabled":True,"asset_review_required":False},"review":{"render_template":"minimal","image_profile":"none","preview_enabled":True,"asset_review_required":True}}
def get_preset(name="default"):
    key=str(name or "default").lower()
    if key not in PRESETS: raise ValueError(f"Unknown preset: {key}")
    return dict(PRESETS[key])
def write_catalog():
    target=ROOT/"output/production/presets.json"; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(json.dumps({"status":"completed","presets":PRESETS},indent=2)+"\n",encoding="utf-8"); return target
if __name__=="__main__": print(write_catalog())
