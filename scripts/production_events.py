#!/usr/bin/env python3
"""Append-only, factual production event journal for Photo Motion runs."""
from __future__ import annotations
import json, os, time
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
EVENTS=ROOT/"output"/"production"/"events.jsonl"
STATE=ROOT/"output"/"production"/"stage_state.json"
def record_event(stage,status,*,error=None,metadata=None,started_at=None):
    event={"timestamp":datetime.now(timezone.utc).isoformat(),"run_id":os.getenv("GITHUB_RUN_ID") or os.getenv("RUN_ID") or "local","stage":str(stage),"status":str(status)}
    if error: event["error"]=str(error)[:1200]
    if metadata is not None: event["metadata"]=metadata
    if started_at is not None:
        try: event["duration_seconds"]=round(max(0,time.monotonic()-float(started_at)),3)
        except (TypeError,ValueError): pass
    try:
        EVENTS.parent.mkdir(parents=True,exist_ok=True)
        with EVENTS.open("a",encoding="utf-8") as f: f.write(json.dumps(event,ensure_ascii=False,sort_keys=True)+"\n")
        state={}
        if STATE.is_file():
            try: state=json.loads(STATE.read_text(encoding="utf-8"))
            except Exception: state={}
        if not isinstance(state,dict): state={}
        state.setdefault("run_id",event["run_id"]); state.setdefault("stages",{})[event["stage"]]=event
        tmp=STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); tmp.replace(STATE)
    except OSError as exc: print(f"WARNING: production event journal unavailable: {exc}",flush=True)
    return event
def read_events():
    if not EVENTS.is_file(): return []
    rows=[]
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        try:
            row=json.loads(line)
            if isinstance(row,dict): rows.append(row)
        except json.JSONDecodeError: continue
    return rows
def stage_summary():
    latest={}
    for row in read_events(): latest[str(row.get("stage","unknown"))]=row
    return {"stage_count":len(latest),"completed":sum(x.get("status")=="completed" for x in latest.values()),"failed":sum(x.get("status")=="failed" for x in latest.values()),"running":sum(x.get("status")=="running" for x in latest.values()),"stages":latest}
