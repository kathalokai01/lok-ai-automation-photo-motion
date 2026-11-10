#!/usr/bin/env python3
"""Persist batch job definitions; queue creation never dispatches pipeline runs."""
from __future__ import annotations
import argparse,json,uuid
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; QUEUE=ROOT/"output/production/batch_queue.json"
def load():
    if not QUEUE.is_file(): return {"version":1,"jobs":[]}
    doc=json.loads(QUEUE.read_text(encoding="utf-8"))
    if not isinstance(doc,dict) or not isinstance(doc.get("jobs"),list): raise RuntimeError("Invalid batch queue")
    return doc
def add(topic,format_name="short",preset="default"):
    if format_name not in {"short","full"}: raise ValueError("format must be short or full")
    if not str(topic).strip(): raise ValueError("topic cannot be empty")
    doc=load(); job={"job_id":uuid.uuid4().hex,"topic":str(topic).strip(),"format":format_name,"preset":preset,"status":"queued","created_at":datetime.now(timezone.utc).isoformat()}
    doc["jobs"].append(job); QUEUE.parent.mkdir(parents=True,exist_ok=True); QUEUE.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); return job
def main():
    p=argparse.ArgumentParser(); p.add_argument("--topic",required=True); p.add_argument("--format",choices=("short","full"),default="short"); p.add_argument("--preset",default="default"); a=p.parse_args()
    job=add(a.topic,a.format,a.preset); print("QUEUED (not dispatched):",job["job_id"]); return 0
if __name__=="__main__": raise SystemExit(main())
