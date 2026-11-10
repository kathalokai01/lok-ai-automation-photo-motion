#!/usr/bin/env python3
"""Create a static index of current storyboard and render preview artifacts."""
from __future__ import annotations
import html,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    candidates=[ROOT/"output/visuals/storyboard.jpg",ROOT/"output/visuals/storyboard_manifest.json",ROOT/"output/scene_renders/render_manifest.json"]
    rows=[]
    for path in candidates:
        if path.is_file():
            relative=path.relative_to(ROOT).as_posix()
            rows.append(f'<li><a href="../../{html.escape(relative)}">{html.escape(relative)}</a> ({path.stat().st_size} bytes)</li>')
    status="completed" if rows else "unavailable"
    message="".join(rows) if rows else "<li>Preview unavailable: upstream image or render stage did not complete.</li>"
    target=ROOT/"output/production/preview.html"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Photo Motion preview</title><h1>Production preview</h1><ul>'+message+"</ul>",
        encoding="utf-8",
    )
    manifest={"status":status,"path":target.relative_to(ROOT).as_posix(),"sources":[path.relative_to(ROOT).as_posix() for path in candidates if path.is_file()]}
    (target.parent/"preview_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    print("PREVIEW INDEX:",target,"STATUS:",status)
    return 0
if __name__=="__main__": raise SystemExit(main())
