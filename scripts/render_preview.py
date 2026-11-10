#!/usr/bin/env python3
"""Create a static index of current storyboard and render preview artifacts."""
from __future__ import annotations
import html,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    candidates=[ROOT/"output/visuals/storyboard.jpg",ROOT/"output/visuals/storyboard_manifest.json",ROOT/"output/scene_renders/render_manifest.json"]; rows=[]
    for p in candidates:
        if p.is_file():
            rel=p.relative_to(ROOT).as_posix(); rows.append(f'<li><a href="../../{html.escape(rel)}">{html.escape(rel)}</a> ({p.stat().st_size} bytes)</li>')
    preview_status="completed" if rows else "unavailable"
    target=ROOT/"output/production/preview.html"; target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text("<!doctype html><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>Photo Motion preview</title><h1>Production preview</h1><ul>"+"".join(rows)+"</ul>",encoding="utf-8")
    (target.parent/"preview_manifest.json").write_text(json.dumps({"status":preview_status,"path":target.relative_to(ROOT).as_posix(),"sources":[p.relative_to(ROOT).as_posix() for p in candidates if p.is_file()]},indent=2)+"\n",encoding="utf-8")
    print("PREVIEW INDEX:",target); return 0
if __name__=="__main__": raise SystemExit(main())
