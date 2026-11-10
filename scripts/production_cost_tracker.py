#!/usr/bin/env python3
"""Provider usage and cost ledger; costs remain unknown unless explicitly reported."""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SOURCES={"images":ROOT/"output/visuals/image_generation_manifest.json","music":ROOT/"output/music/flow_music_manifest.json","audio":ROOT/"output/narration/audio_jobs.json"}
TARGET=ROOT/"output/production/cost_report.json"
def read(path):
    if not path.is_file(): return {}
    try:
        v=json.loads(path.read_text(encoding="utf-8")); return v if isinstance(v,dict) else {}
    except Exception: return {}
def main():
    counts=Counter(); calls=Counter(); total=0.0; known=0
    for kind,path in SOURCES.items():
        doc=read(path)
        for key in ("records","scenes","jobs","images","tracks"):
            rows=doc.get(key)
            if isinstance(rows,list):
                for row in rows:
                    if not isinstance(row,dict): continue
                    counts[f"{kind}:{row.get('provider') or row.get('provider_key') or 'unknown'}"]+=1
                    amount=row.get("cost_usd")
                    if isinstance(amount,(int,float)) and amount>=0: total+=float(amount); known+=1
        for key in ("external_api_calls","api_calls","billable_api_calls"):
            value=doc.get(key)
            if isinstance(value,int) and value>=0: calls[f"{kind}:{key}"]+=value
    result={"status":"completed","usage_counts":dict(counts),"reported_api_calls":dict(calls),"reported_cost_usd":round(total,6) if known else None,"cost_record_count":known,"cost_coverage":"partial" if known else "unknown","note":"Only explicit cost_usd values are summed; no provider price is guessed."}
    TARGET.parent.mkdir(parents=True,exist_ok=True); TARGET.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("COST REPORT:",TARGET); return 0
if __name__=="__main__": raise SystemExit(main())
