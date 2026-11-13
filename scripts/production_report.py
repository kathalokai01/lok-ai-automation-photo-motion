#!/usr/bin/env python3
"""Create a factual production, model-usage, reuse and failure report."""
from __future__ import annotations
import hashlib,json,os
from collections import Counter
from pathlib import Path
from input_config import load_and_validate, get_image_provider_order
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"output"
TARGET=OUT/"production_report.json"
SOURCES={
 "story":OUT/"story"/"story.json",
 "ai_story":OUT/"story"/"ai_story.json",
 "story_progression":OUT/"story"/"story_model_progression.json",
 "visual_jobs":OUT/"visuals"/"visual_jobs_manifest.json",
 "images":OUT/"visuals"/"image_manifest.json",
 "image_generation":OUT/"visuals"/"image_generation_manifest.json",
 "storyboard":OUT/"visuals"/"storyboard_manifest.json",
 "render_quality":OUT/"quality"/"render_quality_report.json",
 "audio":OUT/"narration"/"audio_jobs.json",
 "music":OUT/"music"/"flow_music_manifest.json",
 "sound":OUT/"sound_design"/"sound_design_manifest.json",
 "sfx_validation":OUT/"sound_design"/"sfx_timing_validation.json",
}
def digest(path):
 return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):
 if not path.is_file(): return None
 try:
  value=json.loads(path.read_text(encoding="utf-8"))
  return value if isinstance(value,dict) else None
 except Exception: return None
def rows(doc):
 if not isinstance(doc,dict): return []
 for key in ("scenes","jobs","records","images"):
  if isinstance(doc.get(key),list): return [x for x in doc[key] if isinstance(x,dict)]
 return []
def effective_music(music,sound):
 tracks=sound.get("tracks") if isinstance(sound.get("tracks"),dict) else {}
 track=tracks.get("music") if isinstance(tracks.get("music"),dict) else {}
 external_provider=music.get("provider") or music.get("selected_provider")
 external_model=music.get("model")
 if str(track.get("status","")).lower() in {"completed","complete","success","reused"}:
  provider=track.get("provider") or track.get("generation") or "local_synthesis"
  return {
   "status":"completed",
   "provider":provider,
   "model":track.get("model") or (external_model if provider != "local_synthesis" else None),
   "reason":None,
   "external_status":music.get("status","missing"),
   "external_reason":music.get("reason"),
   "external_provider":external_provider,
   "external_model":external_model,
   "external_api_calls":music.get("external_api_calls"),
   "paid_services_used":music.get("paid_services_used"),
   "paid_provider_api_calls":music.get("paid_provider_api_calls",0),
  }
 return {
  "status":music.get("status","missing"),
  "provider":external_provider,
  "model":external_model,
  "reason":music.get("reason"),
  "external_status":music.get("status","missing"),
  "external_reason":music.get("reason"),
  "external_provider":external_provider,
  "external_model":external_model,
  "external_api_calls":music.get("external_api_calls"),
  "paid_services_used":music.get("paid_services_used"),
  "paid_provider_api_calls":music.get("paid_provider_api_calls",0),
 }
def apply_effective_music_stage(stages, music_result):
 stage=dict(stages.get("music") or {})
 stage["status"]=music_result["status"]
 stage["external_status"]=music_result["external_status"]
 stage["provider"]=music_result["provider"]
 stage["model"]=music_result["model"]
 stages["music"]=stage
 return stages

def enabled(config, *keys, default=True):
 for key in keys:
  if key not in config: continue
  value=config[key]
  if isinstance(value,bool): return value
  value=str(value).strip().lower()
  if value in {"true","yes","1","on","enabled"}: return True
  if value in {"false","no","0","off","disabled"}: return False
  raise RuntimeError(f"{key} must be true or false.")

def sound_quality_summary(sound, validation):
 tracks=sound.get("tracks") if isinstance(sound.get("tracks"),dict) else {}
 music_track=tracks.get("music") if isinstance(tracks.get("music"),dict) else {}
 ambient_track=tracks.get("ambient") if isinstance(tracks.get("ambient"),dict) else {}
 sfx_rows=sound.get("sfx_cues") if isinstance(sound.get("sfx_cues"),list) else []
 return {
  "sound_design_status":sound.get("status","missing"),
  "music_track_status":music_track.get("status","missing"),
  "music_provider":music_track.get("provider") or music_track.get("generation") or "local_synthesis",
  "music_loudness":music_track.get("loudness"),
  "ambient_track_status":ambient_track.get("status","missing"),
  "sfx_cue_count":len(sfx_rows) if sfx_rows else sound.get("sfx_cue_count",0),
  "sfx_validation_status":validation.get("status","missing"),
  "sfx_invalid_cues":validation.get("invalid_cues"),
  "sfx_timing_errors":validation.get("timing_errors"),
  "sfx_failed_checks":validation.get("failed_checks"),
 }

def main():
 config_data=load_and_validate(ROOT/"Input"/"topic.txt")
 configured_provider_order=get_image_provider_order(config_data)
 docs={key:read(path) for key,path in SOURCES.items()}
 image_doc=docs["image_generation"] or docs["images"] or {}
 image_rows=rows(image_doc)
 image_status=Counter(str(row.get("status","unknown")).lower() for row in image_rows)
 image_providers=Counter(str(row.get("provider") or "not_recorded") for row in image_rows)
 image_attempts=Counter(); image_attempt_status=Counter()
 for row in image_rows:
  for attempt in row.get("attempts",[]) if isinstance(row.get("attempts"),list) else []:
   if isinstance(attempt,dict):
    image_attempts[str(attempt.get("provider_key") or attempt.get("provider") or "unknown")]+=1
    image_attempt_status[str(attempt.get("status") or "unknown").lower()]+=1
 audio_doc=docs["audio"] or {}
 audio_rows=rows(audio_doc)
 audio_providers=Counter(str(row.get("provider") or row.get("tts_provider") or "unknown") for row in audio_rows)
 audio_status=Counter(str(row.get("status","unknown")).lower() for row in audio_rows)
 audio_summary=audio_doc.get("summary") if isinstance(audio_doc.get("summary"),dict) else {}
 progression=docs["story_progression"] or {}
 ai_story=docs["ai_story"] or {}
 story=docs["story"] or {}
 music=docs["music"] or {}
 sound=docs["sound"] or {}
 sfx_validation=docs["sfx_validation"] or {}
 music_result=effective_music(music,sound)
 sound_quality=sound_quality_summary(sound,sfx_validation)
 music_enabled=enabled(config_data,"MUSIC","BACKGROUND_MUSIC","MUSIC_ENABLED",default=True)
 sfx_enabled=enabled(config_data,"SFX","SFX_ENABLED","SOUND_EFFECTS",default=True)
 sound_checks_passed=(
  (not music_enabled or music_result["status"]=="completed")
  and (not sfx_enabled or (
   sound.get("status")=="completed"
   and sfx_validation.get("status")=="completed"
   and int(sfx_validation.get("failed_checks",0) or 0)==0
  ))
 )
 stages={}
 for name,path in SOURCES.items():
  doc=docs[name]
  stages[name]={"status":str(doc.get("status","missing")) if doc else "missing","path":path.relative_to(ROOT).as_posix() if path.is_file() else None,"sha256":digest(path) if path.is_file() else None}
 stages=apply_effective_music_stage(stages,music_result)
 input_path=ROOT/"Input"/"topic.txt"
 image_reused=sum(value for key,value in image_status.items() if key in {"existing","reused"})
 image_generated=image_status.get("generated",0)
 report={
  "status":"completed" if image_doc.get("status")=="completed" and stages["render_quality"]["status"] in {"passed","completed"} and sound_checks_passed else "incomplete",
  "version":"production-report-v5-sound-track-and-sfx-audibility-gates",
  "run_id":os.getenv("GITHUB_RUN_ID") or "local",
  "story_title":story.get("title") or story.get("story_title") or ai_story.get("title"),
  "format":(docs["visual_jobs"] or {}).get("format") or story.get("format"),
  "configured_image_provider_order":configured_provider_order,
  "model_usage":{
   "story_correction_model":story.get("model"),
   "ai_story_model":ai_story.get("model"),
   "ai_story_last_successful_provider":ai_story.get("last_successful_provider") or progression.get("successful_provider"),
   "ai_story_last_successful_model":ai_story.get("last_successful_model") or progression.get("successful_model"),
   "ai_story_progression_cursor":progression.get("cursor_model"),
   "tts_provider_counts":dict(audio_providers),
   "music_provider":music_result["provider"],
   "music_model":music_result["model"],
   "music_external_provider":music_result["external_provider"],
   "music_external_model":music_result["external_model"],
   "image_progression_cursor":(image_doc.get("model_progression") or {}).get("model_cursor_key") or (image_doc.get("policy") or {}).get("model_cursor_key"),
   "image_last_successful_model":(image_doc.get("model_progression") or {}).get("last_successful_model"),
  },
  "image_records":len(image_rows),
  "image_status_counts":dict(image_status),
  "image_provider_record_counts":dict(image_providers),
  "image_provider_attempt_counts":dict(image_attempts),
  "image_provider_attempt_status_counts":dict(image_attempt_status),
  "external_image_api_calls":image_doc.get("external_api_calls"),
  "billable_image_api_calls":image_doc.get("billable_api_calls"),
  "verified_reused_images":image_reused,
  "generated_images":image_generated,
  "audio_jobs":len(audio_rows),
  "audio_status_counts":dict(audio_status),
  "audio_reuse_summary":{
   "reused_scene_audio":audio_summary.get("reused_scene_audio",0),
   "generated_scene_audio":audio_summary.get("generated_scene_audio",0),
   "reused_chunks":audio_summary.get("reused_chunks",0),
   "generated_chunks":audio_summary.get("generated_chunks",0),
  },
  "music_status":music_result["status"],
  "sound_quality":sound_quality,
  "sound_checks_passed":sound_checks_passed,
  "music_reason":music_result["reason"],
  "music_external_provider_status":music_result["external_status"],
  "music_external_provider_reason":music_result["external_reason"],
  "music_external_api_calls":music_result["external_api_calls"],
  "music_paid_services_used":music_result["paid_services_used"],
  "music_paid_provider_api_calls":music_result["paid_provider_api_calls"],
  "costs":{"status":"not_reported_by_all_providers","amount":None,"currency":None},
  "stages":stages,
  "input_sha256":digest(input_path) if input_path.is_file() else None,
 }
 TARGET.parent.mkdir(parents=True,exist_ok=True)
 TARGET.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
 print("PRODUCTION REPORT:",TARGET)
 return 0
# _PHOTO_MOTION_EVENT_WRAPPED: report failures as well as completed runs.
_photo_motion_original_main=main
def main():
 import time
 started=time.monotonic()
 try:
  try:
   from production_events import record_event
  except ImportError:
   from scripts.production_events import record_event
  record_event("production_report","running")
 except Exception:
  record_event=None
 try:
  result=_photo_motion_original_main()
  if record_event: record_event("production_report","completed",metadata={"return_code":result},started_at=started)
  return result
 except BaseException as exc:
  if record_event: record_event("production_report","failed",error=f"{type(exc).__name__}: {exc}",started_at=started)
  raise
if __name__=="__main__": raise SystemExit(main())
