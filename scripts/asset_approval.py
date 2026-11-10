#!/usr/bin/env python3
"""Human review ledger; asset approval is explicit and never inferred."""
from __future__ import annotations
import argparse,json,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; SOURCE=ROOT/"output/visuals/storyboard_manifest.json"; TARGET=ROOT/"output/visuals/asset_approval.json"
def load():
    if not SOURCE.is_file(): raise RuntimeError("Storyboard manifest missing")
    doc=json.loads(SOURCE.read_text(encoding="utf-8"))
    if doc.get("status")!="completed": raise RuntimeError("Storyboard is not completed")
    entries=doc.get("images",[])
    if not isinstance(entries,list) or not entries: raise RuntimeError("Storyboard has no image entries")
    old={}
    if TARGET.is_file():
        try: old={str(x.get("scene_id")):x for x in json.loads(TARGET.read_text(encoding="utf-8")).get("assets",[]) if isinstance(x,dict)}
        except Exception: old={}
    assets=[]
    for item in entries:
        sid=str(item.get("scene_id") or ""); prior=old.get(sid,{})
        assets.append({"scene_id":sid,"path":item.get("path"),"sha256":item.get("sha256"),"decision":prior.get("decision","pending"),"review_note":prior.get("review_note","")})
    return {"status":"review_required" if any(x["decision"]=="pending" for x in assets) else "reviewed","source_storyboard_sha256":hashlib.sha256(SOURCE.read_bytes()).hexdigest(),"assets":assets}
def main():
    p=argparse.ArgumentParser(); p.add_argument("--scene"); p.add_argument("--decision",choices=("approved","rejected")); p.add_argument("--note",default=""); a=p.parse_args()
    doc=load()
    if a.scene:
        if not a.decision: p.error("--decision is required with --scene")
        found=[x for x in doc["assets"] if x["scene_id"]==a.scene]
        if not found: raise RuntimeError("Unknown scene ID: "+a.scene)
        found[0]["decision"]=a.decision; found[0]["review_note"]=a.note
        doc["status"]="review_required" if any(x["decision"]=="pending" for x in doc["assets"]) else "reviewed"
    TARGET.parent.mkdir(parents=True,exist_ok=True); TARGET.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("ASSET APPROVAL:",doc["status"],"assets:",len(doc["assets"])); return 0
if __name__=="__main__": raise SystemExit(main())
