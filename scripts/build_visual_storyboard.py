#!/usr/bin/env python3
"""Build a contact-sheet storyboard from verified generated still images."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"output"
MANIFEST=OUT/"visuals"/"image_manifest.json"
JOBS=OUT/"visuals"/"visual_jobs.json"
TARGET=OUT/"visuals"/"storyboard.jpg"
REPORT=OUT/"visuals"/"storyboard_manifest.json"

def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def rows(doc):
    for key in ("scenes","images","records","jobs"):
        value=doc.get(key)
        if isinstance(value,list): return value
    return []

def main():
    if not MANIFEST.is_file():
        raise RuntimeError("Image manifest is missing")
    manifest=json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(manifest,dict):
        raise RuntimeError("Storyboard image manifest must be a JSON object")
    if str(manifest.get("status", "")).lower() != "completed":
        raise RuntimeError("Image manifest is not fully completed")
    if str(manifest.get("format", "")).lower() not in {"short", "full"}:
        raise RuntimeError("Image manifest FORMAT is invalid")
    records=rows(manifest)
    if not records: raise RuntimeError("Image manifest contains no scene records")
    accepted=[]
    for row in records:
        if not isinstance(row,dict): continue
        if str(row.get("status","")).lower() not in {"generated","existing","completed","complete","success","reused","valid"}: continue
        raw=row.get("path") or row.get("image_path") or row.get("output_path")
        if not raw: continue
        p=Path(raw); p=p if p.is_absolute() else ROOT/p
        try: p.resolve().relative_to(ROOT.resolve())
        except ValueError: raise RuntimeError(f"Storyboard image escapes repository: {p}")
        if not p.is_file() or p.is_symlink(): continue
        digest=sha(p)
        declared=row.get("image_sha256") or row.get("sha256")
        if declared and declared != digest: raise RuntimeError(f"Storyboard image hash mismatch: {p}")
        with Image.open(p) as im:
            im.verify()
        accepted.append((row,p,digest))
    if len(accepted)!=len(records):
        raise RuntimeError(f"Storyboard requires every image to be verified; {len(accepted)}/{len(records)} usable")
    accepted.sort(key=lambda x:int(x[0].get("global_scene",x[0].get("scene",0)) or 0))
    cols=4; thumb_w=360; thumb_h=250; label_h=48; pad=18
    rows_count=(len(accepted)+cols-1)//cols
    canvas=Image.new("RGB",(pad+cols*(thumb_w+pad),pad+rows_count*(thumb_h+label_h+pad)),(18,20,25))
    draw=ImageDraw.Draw(canvas)
    try: font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",18)
    except OSError: font=ImageFont.load_default()
    for i,(row,path,digest) in enumerate(accepted):
        x=pad+(i%cols)*(thumb_w+pad); y=pad+(i//cols)*(thumb_h+label_h+pad)
        with Image.open(path) as im:
            thumb=im.convert("RGB"); thumb.thumbnail((thumb_w,thumb_h),Image.Resampling.LANCZOS)
            tile=Image.new("RGB",(thumb_w,thumb_h),(0,0,0))
            tile.paste(thumb,((thumb_w-thumb.width)//2,(thumb_h-thumb.height)//2))
            canvas.paste(tile,(x,y))
        scene_id=str(row.get("scene_id") or path.stem)
        role=str(row.get("role") or "main")
        draw.text((x,y+thumb_h+5),f"{i+1:03d}  {role}  {scene_id[:34]}",font=font,fill=(245,245,245))
    TARGET.parent.mkdir(parents=True,exist_ok=True)
    tmp=TARGET.with_suffix(".tmp.jpg"); canvas.save(tmp,"JPEG",quality=88,optimize=True); tmp.replace(TARGET)
    report={"status":"completed","version":"storyboard-v1","format":manifest.get("format"),"image_count":len(accepted),"path":TARGET.relative_to(ROOT).as_posix(),"bytes":TARGET.stat().st_size,"sha256":sha(TARGET),"source_image_manifest_sha256":sha(MANIFEST),"images":[{"scene_id":str(r.get("scene_id") or p.stem),"path":p.relative_to(ROOT).as_posix(),"sha256":d} for r,p,d in accepted]}
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("STORYBOARD:",TARGET,"IMAGES:",len(accepted))
if __name__=="__main__": main()
