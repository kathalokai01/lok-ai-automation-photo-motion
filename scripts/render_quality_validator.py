#!/usr/bin/env python3
"""Technical ffprobe validation for every final captioned Part video."""
from __future__ import annotations
import hashlib,json,math,subprocess
from fractions import Fraction
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"output"
INPUT=ROOT/"Input"/"topic.txt"; RENDER=OUT/"scene_renders"/"render_manifest.json"
REPORT=OUT/"quality"/"render_quality_report.json"
def sha(p):
 h=hashlib.sha256()
 with p.open("rb") as f:
  for b in iter(lambda:f.read(1024*1024),b""):h.update(b)
 return h.hexdigest()
def main():
 from scripts.input_config import load_and_validate,get_format,get_fps,get_selected_parts
 cfg=load_and_validate(INPUT); mode=get_format(cfg); fps=float(get_fps(cfg)); selected=int(get_selected_parts(cfg,required=True)); expected=(720,1280) if mode=="short" else (1280,720)
 if not RENDER.is_file(): raise RuntimeError("Render manifest is missing")
 doc=json.loads(RENDER.read_text(encoding="utf-8")); parts=doc.get("parts")
 if not isinstance(parts,list) or not parts: raise RuntimeError("Render manifest has no Part outputs")
 if len(parts)!=selected: raise RuntimeError(f"Render Part count {len(parts)} does not match selected PARTS {selected}")
 part_numbers=[]
 rows=[]
 for row in parts:
  if not isinstance(row,dict): raise RuntimeError("Invalid render Part record")
  part_number=int(row.get("part",0))
  if part_number<1: raise RuntimeError("Render Part number is missing or invalid")
  part_numbers.append(part_number)
  raw=row.get("path") or row.get("video_path") or row.get("output_path")
  if not raw: raise RuntimeError("Render Part has no video path")
  p=Path(raw); p=p if p.is_absolute() else ROOT/p
  try: p.resolve().relative_to(ROOT.resolve())
  except ValueError: raise RuntimeError(f"Rendered video escapes repository: {p}")
  if not p.is_file() or p.is_symlink() or p.stat().st_size<1000: raise RuntimeError(f"Missing/unsafe rendered video: {p}")
  cmd=["ffprobe","-v","error","-show_entries","format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate","-of","json",str(p)]
  result=subprocess.run(cmd,capture_output=True,text=True,timeout=120,check=False)
  if result.returncode: raise RuntimeError(f"ffprobe failed for {p}: {result.stderr[-500:]}")
  data=json.loads(result.stdout); streams=data.get("streams",[])
  video=next((x for x in streams if x.get("codec_type")=="video"),None); audio=next((x for x in streams if x.get("codec_type")=="audio"),None)
  if not video or not audio: raise RuntimeError(f"Video/audio stream missing: {p}")
  dims=(int(video.get("width",0)),int(video.get("height",0)))
  if dims!=expected: raise RuntimeError(f"Wrong {mode} dimensions for {p}: {dims}")
  rate=video.get("avg_frame_rate") or video.get("r_frame_rate")
  actual=float(Fraction(rate))
  if not math.isfinite(actual) or abs(actual-fps)>0.05: raise RuntimeError(f"FPS mismatch for {p}: {actual}")
  duration=float(data["format"]["duration"])
  if not math.isfinite(duration) or duration<=0: raise RuntimeError(f"Invalid duration for {p}")
  rows.append({"part":row.get("part"),"path":p.relative_to(ROOT).as_posix(),"sha256":sha(p),"bytes":p.stat().st_size,"dimensions":list(dims),"fps":actual,"duration":round(duration,4),"audio_stream":True})
 if sorted(part_numbers)!=list(range(1,selected+1)) or len(set(part_numbers))!=selected:
  raise RuntimeError("Render manifest does not cover each selected Part exactly once")
 REPORT.parent.mkdir(parents=True,exist_ok=True)
 report={"status":"completed","version":"render-quality-v1","format":mode,"expected_dimensions":list(expected),"expected_fps":fps,"part_count":len(rows),"parts":rows}
 REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
 print("RENDER QUALITY VALIDATION PASSED:",len(rows),"parts")


# _PHOTO_MOTION_EVENT_WRAPPED: preserve original stage behavior and record factual outcomes.
_photo_motion_original_main = main
def main():
    import time as _pm_time
    _pm_started = _pm_time.monotonic()
    try:
        try:
            from production_events import record_event as _pm_record
        except ImportError:
            from scripts.production_events import record_event as _pm_record
        _pm_record("render_quality_validator", "running")
    except Exception:
        _pm_record = None
    try:
        _pm_result = _photo_motion_original_main()
        if _pm_record:
            _pm_record("render_quality_validator", "completed" if _pm_result in (None, 0) else "failed", metadata={"return_code": _pm_result}, started_at=_pm_started)
        return _pm_result
    except BaseException as _pm_exc:
        if _pm_record:
            _pm_record("render_quality_validator", "failed", error=f"{type(_pm_exc).__name__}: {_pm_exc}", started_at=_pm_started)
        raise

if __name__=="__main__": main()
