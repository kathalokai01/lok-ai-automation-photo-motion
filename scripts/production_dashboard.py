#!/usr/bin/env python3
"""Generate a static production dashboard artifact; no persistent server."""
from __future__ import annotations
import html,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"output"/"production"
def main():
    from production_events import stage_summary
    summary=stage_summary(); report_path=ROOT/"output"/"production_report.json"; report={}
    if report_path.is_file():
        try: report=json.loads(report_path.read_text(encoding="utf-8"))
        except Exception: report={}
    rows=[]
    for name,row in sorted(summary["stages"].items()):
        rows.append("<tr><td>"+html.escape(name)+"</td><td>"+html.escape(str(row.get("status","unknown")))+"</td><td>"+html.escape(str(row.get("duration_seconds","—")))+"</td><td>"+html.escape(str(row.get("error","") or ""))+"</td></tr>")
    title=html.escape(str(report.get("story_title") or "Photo Motion production"))
    body=f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Production dashboard</title><style>body{{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#10151d;color:#edf2f7}}h1{{font-size:1.5rem}}.stats{{display:flex;gap:1rem;flex-wrap:wrap}}.stats div{{padding:1rem;background:#202a38;border-radius:8px}}table{{width:100%;border-collapse:collapse;margin-top:1rem}}td,th{{padding:.7rem;border-bottom:1px solid #394454;text-align:left;overflow-wrap:anywhere}}</style><h1>{title}</h1><div class="stats"><div>Stages: {summary["stage_count"]}</div><div>Completed: {summary["completed"]}</div><div>Failed: {summary["failed"]}</div><div>Running: {summary["running"]}</div></div><table><thead><tr><th>Stage</th><th>Status</th><th>Seconds</th><th>Failure detail</th></tr></thead><tbody>{''.join(rows)}</tbody></table><p>Static workflow artifact; not a live hosted service.</p></html>"""
    OUT.mkdir(parents=True,exist_ok=True); target=OUT/"dashboard.html"; target.write_text(body,encoding="utf-8")
    (OUT/"dashboard_manifest.json").write_text(json.dumps({"status":"completed","path":target.relative_to(ROOT).as_posix(),"stage_summary":{k:v for k,v in summary.items() if k!="stages"}},indent=2)+"\n",encoding="utf-8")
    print("PRODUCTION DASHBOARD:",target); return 0
if __name__=="__main__": raise SystemExit(main())
