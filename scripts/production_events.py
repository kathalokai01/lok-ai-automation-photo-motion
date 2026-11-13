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
def record_provider_attempt(
    provider, model, status, billable, *, scene_id=None, attempt_id=None,
    error=None, http_status=None, actual_cost=None, currency=None, receipt_id=None,
):
    """Record one provider request without guessing actual charges."""
    cost_verified = actual_cost is not None and receipt_id not in (None, "")
    metadata = {
        "provider": str(provider),
        "model": str(model or "unknown"),
        "scene_id": str(scene_id or ""),
        "attempt_id": str(attempt_id or ""),
        "billable_marked": bool(billable),
        "request_status": str(status),
        "actual_cost": actual_cost if cost_verified else None,
        "currency": str(currency) if cost_verified and currency else None,
        "receipt_id": str(receipt_id) if cost_verified else None,
        "cost_status": "receipt_verified" if cost_verified else "unknown_without_receipt",
    }
    if http_status is not None:
        metadata["http_status"] = http_status
    request_status = str(status)
    event_status = (
        "failed" if request_status in {
            "request_failed", "quality_rejected",
            "response_received_quality_rejected",
            "response_received_validation_failed",
        }
        else "running" if request_status == "request_started"
        else "pending" if request_status == "verification_pending"
        else "completed"
    )
    return record_event(
        "provider_attempt",
        event_status,
        error=error,
        metadata=metadata,
    )


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
