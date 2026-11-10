#!/usr/bin/env python3
"""Create a factual production-stage/provider/cost evidence report."""
from __future__ import annotations
import hashlib,json,os
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"output"
TARGET=OUT/"production_report.json"
SOURCES={
 "story":OUT/"story"/"story.json",
 "visual_jobs":OUT/"visuals"/"visual_jobs_manifest.json",
 "images":OUT/"visuals"/"image_manifest.json",
 "image_generation":OUT/"visuals"/"image_generation_manifest.json",
 "storyboard":OUT/"visuals"/"storyboard_manifest.json",
 "render_quality":OUT/"quality"/"render_quality_report.json",
 "audio":OUT/"narration"/"audio_jobs.json",
 "music":OUT/"music"/"flow_music_manifest.json",
 "sound":OUT/"sound_design"/"sound_design_manifest.json",
}
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):
 if not p.is_file(): return None
 try:
  v=json.loads(p.read_text(encoding="utf-8"))
  return v if isinstance(v,dict) else None
 except Exception: return None
def rows(doc):
 if not isinstance(doc,dict): return []
 for k in ("scenes","jobs","records","images"):
  if isinstance(doc.get(k),list): return doc[k]
 return []
def main():
 docs={k:read(p) for k,p in SOURCES.items()}
 if docs["story"] is None: raise RuntimeError("Story manifest missing; cannot report production")
 image_rows=rows(docs["images"]); counts=Counter(); providers=Counter(); attempt_counts=Counter(); attempt_status=Counter(); reused=0; failed=0
 for r in image_rows:
  if not isinstance(r,dict): continue
  status=str(r.get("status","unknown")).lower(); counts[status]+=1
  provider=str(r.get("provider") or "not_recorded"); providers[provider]+=1
  if status in {"existing","reused"}: reused+=1
  if status in {"failed","error"}: failed+=1
  attempts=r.get("attempts",[])
  if isinstance(attempts,list):
   for attempt in attempts:
    if isinstance(attempt,dict):
     provider_key=str(attempt.get("provider_key") or attempt.get("provider") or "unknown")
     attempt_counts[provider_key]+=1
     attempt_status[str(attempt.get("status") or "unknown").lower()]+=1
 stages={}
 for name,path in SOURCES.items():
  d=docs[name]
  stages[name]={"status":str(d.get("status","missing")) if d else "missing","path":path.relative_to(ROOT).as_posix() if path.exists() else None,"sha256":digest(path) if path.is_file() else None}
 config=ROOT/"Input"/"topic.txt"
 report={"status":"completed","version":"production-report-v1","story_title":docs["story"].get("title") or docs["story"].get("story_title"),"format":docs["visual_jobs"].get("format") if docs["visual_jobs"] else None,"configured_image_provider_order":os.getenv("IMAGE_PROVIDER_ORDER","see Input/topic.txt"),"image_records":sum(counts.values()),"image_status_counts":dict(counts),"provider_record_counts":dict(providers),"provider_attempt_counts":dict(attempt_counts),"provider_attempt_status_counts":dict(attempt_status),"external_image_api_calls":docs["image_generation"].get("external_api_calls") if docs["image_generation"] else None,"billable_image_api_calls":docs["image_generation"].get("billable_api_calls") if docs["image_generation"] else None,"verified_reused_images":reused,"failed_image_records":failed,"costs":{"status":"not_reported_by_all_providers","amount":None,"currency":None},"stages":stages,"input_sha256":digest(config) if config.is_file() else None}
 TARGET.parent.mkdir(parents=True,exist_ok=True); TARGET.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
 print("PRODUCTION REPORT:",TARGET)
if __name__=="__main__": main()
