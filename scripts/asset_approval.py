#!/usr/bin/env python3
"""Hash-bound human review ledger for generated still-image assets."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from input_config import load_and_validate
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/"output/visuals/storyboard_manifest.json"
TARGET=ROOT/"output/visuals/asset_approval.json"
APPROVAL_SOURCE=ROOT/"Input/asset_approval.json"
def read_json(path: Path) -> dict:
    try: value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,json.JSONDecodeError) as exc: raise RuntimeError(f"Cannot read approval file {path}: {exc}") from exc
    if not isinstance(value,dict): raise RuntimeError(f"Approval file must contain an object: {path}")
    return value
def load() -> dict:
    if not SOURCE.is_file(): raise RuntimeError("Storyboard manifest missing")
    storyboard=read_json(SOURCE)
    if storyboard.get("status")!="completed": raise RuntimeError("Storyboard is not completed")
    entries=storyboard.get("images",[])
    if not isinstance(entries,list) or not entries: raise RuntimeError("Storyboard has no image entries")
    prior_by_id={}
    for path in (APPROVAL_SOURCE,TARGET):
        if not path.is_file(): continue
        previous=read_json(path)
        for row in previous.get("assets",[]):
            if isinstance(row,dict) and row.get("scene_id"): prior_by_id[str(row["scene_id"])]=row
    assets=[]
    for item in entries:
        if not isinstance(item,dict): raise RuntimeError("Storyboard contains an invalid image record")
        sid=str(item.get("scene_id") or "").strip()
        digest=str(item.get("sha256") or "").strip().lower()
        if not sid or len(digest)!=64: raise RuntimeError("Storyboard image is missing scene ID or SHA256")
        prior=prior_by_id.get(sid,{})
        same_asset=prior.get("sha256")==digest
        decision=prior.get("decision","pending") if same_asset else "pending"
        if decision not in {"approved","rejected","pending"}: decision="pending"
        assets.append({"scene_id":sid,"path":item.get("path"),"sha256":digest,"decision":decision,"review_note":prior.get("review_note","") if same_asset else ""})
    return {"status":"reviewed" if all(x["decision"]=="approved" for x in assets) else "review_required","source_storyboard_sha256":hashlib.sha256(SOURCE.read_bytes()).hexdigest(),"assets":assets}
def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--scene")
    parser.add_argument("--decision",choices=("approved","rejected"))
    parser.add_argument("--note",default="")
    parser.add_argument("--enforce-required",action="store_true")
    args=parser.parse_args()
    if bool(args.scene)!=bool(args.decision): parser.error("--scene and --decision must be supplied together")
    doc=load()
    if args.scene:
        found=[x for x in doc["assets"] if x["scene_id"]==args.scene]
        if not found: raise RuntimeError("Unknown scene ID: "+args.scene)
        found[0]["decision"]=args.decision
        found[0]["review_note"]=args.note
        doc["status"]="reviewed" if all(x["decision"]=="approved" for x in doc["assets"]) else "review_required"
    TARGET.parent.mkdir(parents=True,exist_ok=True)
    TARGET.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    config=load_and_validate(ROOT/"Input"/"topic.txt")
    required=config.get("ASSET_APPROVAL_REQUIRED") is True
    print("ASSET APPROVAL:",doc["status"],"| assets:",len(doc["assets"]),"| required:",required)
    if args.enforce_required and required and doc["status"]!="reviewed":
        raise RuntimeError("ASSET_APPROVAL_REQUIRED=true but image assets are not all approved. Add hash-matched approvals to Input/asset_approval.json.")
    return 0
if __name__=="__main__": raise SystemExit(main())
