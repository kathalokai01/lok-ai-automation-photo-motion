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
def provider_summary():
    latest = {}
    current_run_id = str(
        os.getenv("GITHUB_RUN_ID") or os.getenv("RUN_ID") or "local"
    )
    for event in read_events():
        if event.get("stage") != "provider_attempt":
            continue
        # Checkpoint restoration can carry event journals from earlier runs.
        # Production reports must describe this workflow run only.
        event_run_id = str(event.get("run_id") or "local")
        if event_run_id != current_run_id:
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        attempt_id = str(metadata.get("attempt_id") or "")
        provider = str(metadata.get("provider") or "unknown")
        if attempt_id:
            # Fallback providers can share a logical TTS/music job attempt ID.
            # Include provider identity so each actual provider request remains
            # separately accounted while lifecycle events update that request.
            latest[(event_run_id, provider, attempt_id)] = metadata

    by_provider = {}
    billable_marked = 0
    unknown_cost = 0
    verified_costs_by_currency = {}
    verified_receipts = 0
    failed = 0
    accepted = 0
    pending = 0
    for metadata in latest.values():
        provider = str(metadata.get("provider") or "unknown")
        item = by_provider.setdefault(provider, {
            "attempts": 0, "billable_marked": 0,
            "failed": 0, "accepted": 0, "pending": 0,
        })
        item["attempts"] += 1
        billable = metadata.get("billable_marked") is True
        if billable:
            billable_marked += 1
            item["billable_marked"] += 1
        status = str(metadata.get("request_status") or "")
        if status in {"request_failed", "quality_rejected", "response_received_quality_rejected", "response_received_validation_failed"}:
            failed += 1
            item["failed"] += 1
        elif status in {"verification_pending", "response_received", "request_started"}:
            pending += 1
            item["pending"] += 1
        elif status == "accepted":
            accepted += 1
            item["accepted"] += 1
        cost = metadata.get("actual_cost")
        if metadata.get("cost_status") == "receipt_verified" and isinstance(cost, (int, float)) and not isinstance(cost, bool):
            currency = str(metadata.get("currency") or "unspecified")
            verified_costs_by_currency[currency] = (
                verified_costs_by_currency.get(currency, 0.0) + float(cost)
            )
            verified_receipts += 1
        else:
            unknown_cost += 1

    return {
        "attempt_count": len(latest),
        "billable_marked_attempts": billable_marked,
        "failed_attempts": failed,
        "accepted_attempts": accepted,
        "verification_pending_attempts": pending,
        "actual_cost_receipt_count": verified_receipts,
        "actual_cost_total": (
            round(next(iter(verified_costs_by_currency.values())), 6)
            if len(verified_costs_by_currency) == 1 else None
        ),
        "actual_cost_currency": (
            next(iter(verified_costs_by_currency))
            if len(verified_costs_by_currency) == 1 else None
        ),
        "actual_cost_by_currency": {
            key: round(value, 6) for key, value in verified_costs_by_currency.items()
        },
        "actual_cost_status": (
            "receipt_verified" if len(verified_costs_by_currency) == 1
            else "receipt_verified_multiple_currencies" if verified_costs_by_currency
            else "unknown_no_provider_receipts"
        ),
        "unknown_cost_attempts": unknown_cost,
        "by_provider": by_provider,
    }


def stage_summary():
    latest={}
    for row in read_events(): latest[str(row.get("stage","unknown"))]=row
    return {
        "stage_count":len(latest),
        "completed":sum(x.get("status")=="completed" for x in latest.values()),
        "failed":sum(x.get("status")=="failed" for x in latest.values()),
        "running":sum(x.get("status")=="running" for x in latest.values()),
        "stages":latest,
        "provider_usage":provider_summary(),
    }
