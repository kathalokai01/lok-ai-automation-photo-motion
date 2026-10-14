#!/usr/bin/env python3
"""Cost-guarded, resumable AI story enhancement, saved one part at a time."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_input_config,
    normalize_format,
    get_topic,
    get_story_text,
)
from gemini_api_guard import (
    require_gemini_generation,
    GeminiBillingBlocked,
)

STORY_DIR = ROOT / "output" / "story"
SOURCE_FILE = STORY_DIR / "story.json"
OUTPUT_FILE = STORY_DIR / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

VERSION = "ai-story-v3-guarded-no-auto-retry"
TIMEOUT = max(30, min(300, int(os.getenv("SCENE_REQUEST_TIMEOUT", "120"))))

SCENE_FIELDS = (
    "narration",
    "dialogue",
    "visual_prompt",
    "negative_prompt",
    "camera_prompt",
    "lighting_prompt",
    "sfx_prompt",
    "music_prompt",
    "suspense_prompt",
)


def log(*items):
    print(*items, flush=True)


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def save_json(data):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT_FILE.with_suffix(".json.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, OUTPUT_FILE)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_text(value):
    value = str(value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value):
    return len(normalize_text(value).split())


def text_value(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def bool_value(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "1", "yes", "on", "y"}:
        return True
    if value in {"false", "0", "no", "off", "n"}:
        return False
    return default


def selected_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict):
        raise RuntimeError(
            "Selected model file missing. Run select_gemini_model.py first."
        )

    if data.get("status") != "selected":
        raise RuntimeError(
            "Gemini model is not marked selected. "
            "No generation request will be sent."
        )

    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid Gemini model ID: {model!r}")
    return model


def validate_source(story, config):
    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("story.json has no parts.")

    try:
        requested_parts = int(config.get("PARTS", len(parts)))
        requested_scenes = int(config.get("SCENES", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("PARTS or SCENES is not a valid integer.") from exc

    if len(parts) != requested_parts:
        raise RuntimeError(
            f"Story has {len(parts)} parts; input requests {requested_parts}."
        )

    total = 0
    for p_index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Part {p_index} is invalid.")
        if int(part.get("part", -1)) != p_index:
            raise RuntimeError(f"Part numbering mismatch at part {p_index}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {p_index} has no scenes.")
        if requested_scenes > 0 and len(scenes) != requested_scenes:
            raise RuntimeError(
                f"Part {p_index} has {len(scenes)} scenes; "
                f"input requests {requested_scenes}."
            )

        for s_index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid source scene {p_index}:{s_index}.")
            if int(scene.get("scene", -1)) != s_index:
                raise RuntimeError(
                    f"Scene numbering mismatch at {p_index}:{s_index}."
                )
            if not str(scene.get("narration", "")).strip():
                raise RuntimeError(f"Empty source narration at {p_index}:{s_index}.")
            if not str(scene.get("visual", scene.get("visual_prompt", ""))).strip():
                raise RuntimeError(f"Empty source visual at {p_index}:{s_index}.")
        total += len(scenes)

    return total


def make_prompt(story, source_part, config, topic, story_text):
    part_number = int(source_part["part"])
    source_scenes = source_part["scenes"]
    first_part = part_number == 1
    last_part = part_number == len(story["parts"])
    hook = str(story.get("hook", "")).strip()
    format_type = normalize_format(config)

    context = []
    for scene in source_scenes:
        context.append({
            "scene": scene["scene"],
            "scene_id": scene.get("scene_id", ""),
            "global_scene_number": scene.get("global_scene_number"),
            "purpose": scene.get("purpose", scene.get("role", "")),
            "narration": scene.get("narration", ""),
            "visual": scene.get("visual", scene.get("visual_prompt", "")),
            "dialogue": scene.get("dialogue", ""),
            "suspense": scene.get("suspense", ""),
        })

    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    resolution = bool_value(config, "FINAL_RESOLUTION", True)

    hook_rule = (
        f'Part 1 Scene 1 narration must begin with this exact hook: {hook!r}.'
        if first_part else
        "Give scene 1 a distinct part-opening hook when PART_HOOK is enabled."
    )
    suspense_rule = (
        "The final scene must include a meaningful suspense_prompt of at "
        "least four words."
        if part_suspense else
        "Use suspense only when it fits the source story."
    )
    ending_rule = (
        "This is the final part: resolve the central conflict clearly."
        if last_part and resolution else
        "Preserve continuity and lead naturally into the next part."
    )

    return f"""
You are a Hindi cinematic story and visual-prompt specialist.
Return valid JSON only. No Markdown fences or commentary.

TOPIC: {topic}
USER STORY TEXT: {story_text or "(not supplied)"}
TITLE: {story.get("title", topic)}
FORMAT: {format_type}
AUDIENCE: {config.get("AUDIENCE", "adult")}
STORY LENGTH: {config.get("STORY_LENGTH", "auto")}
SCENE DURATION: {config.get("SCENE_DURATION", "auto")}
PART: {part_number} of {len(story["parts"])}
EXACT STORY HOOK: {hook}

SOURCE SCENES:
{json.dumps(context, ensure_ascii=False, indent=2)}

OPENING RULE:
{hook_rule}
First story hook: minimum 7 words for short, 14 for full.
Do not add greetings, channel introductions, or subscribe requests.

PART SUSPENSE:
{suspense_rule}

ENDING:
{ending_rule}

CONTINUITY RULES:
- Return exactly {len(source_scenes)} scenes in original order.
- Do not merge, omit, duplicate, or add scenes.
- Preserve plot events, character identity, names, ages, clothing and timeline.
- Keep narration natural Hindi and dialogue believable.
- Maintain realistic live-action visuals, not cartoons or slideshows.
- Describe plausible actions, locations, lighting and camera framing.
- Do not invent events that contradict the source scenes.
- Keep optional dialogue and sound prompts empty if unnecessary.

STYLE:
VISUAL_STYLE={config.get("VISUAL_STYLE", "cinematic_realistic")}
REALISM={config.get("REALISM", "high")}
CAMERA_STYLE={config.get("CAMERA_STYLE", "cinematic")}
LIGHTING={config.get("LIGHTING", "cinematic")}
MOOD={config.get("MOOD", "dramatic")}
MUSIC={config.get("MUSIC", True)}
MUSIC_STYLE={config.get("MUSIC_STYLE", "cinematic")}
SFX={config.get("SFX", True)}
AMBIENT_SOUND={config.get("AMBIENT_SOUND", True)}

Every scene must contain string fields:
scene, narration, dialogue, visual_prompt, negative_prompt,
camera_prompt, lighting_prompt, sfx_prompt, music_prompt, suspense_prompt.

Return this JSON structure:
{{
  "scenes": [
    {{
      "scene": 1,
      "narration": "...",
      "dialogue": "",
      "visual_prompt": "Detailed realistic live-action visual prompt",
      "negative_prompt": "cartoon, slideshow, inconsistent identity",
      "camera_prompt": "Camera framing and movement",
      "lighting_prompt": "Scene lighting",
      "sfx_prompt": "",
      "music_prompt": "",
      "suspense_prompt": ""
    }}
  ]
}}
""".strip()


def call_gemini(model, prompt):
    # Do not send a request unless the explicit billing guard authorizes it.
    api_key = (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )
    try:
        api_key = require_gemini_generation(
            "generate_ai_story",
            api_key=api_key,
        )
    except GeminiBillingBlocked as exc:
        raise RuntimeError(
            f"{exc}\nNo Gemini request was sent by generate_ai_story.py."
        ) from exc

    model_path = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_path}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.65,
            "topP": 0.9,
            "maxOutputTokens": 16000,
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    # Intentionally no automatic retry: an uncertain response must not
    # trigger a second potentially billable generation request.
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(
            f"Gemini HTTP {exc.code}: {detail}. No automatic retry was sent."
        ) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RuntimeError(
            f"Gemini network failure ({type(exc).__name__}). "
            "No automatic retry was sent; inspect the checkpoint first."
        ) from exc

    try:
        response = json.loads(raw)
        candidates = response.get("candidates", [])
        if not candidates:
            error = response.get("error", {})
            raise RuntimeError(
                str(error.get("message", "Gemini returned no candidates."))
            )

        candidate = candidates[0]
        reason = str(candidate.get("finishReason", "")).upper()
        if reason in {"MAX_TOKENS", "LENGTH"}:
            raise RuntimeError("Gemini response was truncated.")
        if reason in {"SAFETY", "RECITATION", "BLOCKLIST"}:
            raise RuntimeError(f"Gemini blocked the response: {reason}.")

        content = candidate.get("content", {})
        response_text = "\n".join(
            item.get("text", "")
            for item in content.get("parts", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ).strip()
        if not response_text:
            raise RuntimeError("Gemini returned empty text.")

        result = json.loads(response_text)
        if not isinstance(result, dict) or not isinstance(result.get("scenes"), list):
            raise RuntimeError("Gemini response must contain a scenes array.")
        return result
    except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
        raise RuntimeError(
            "Gemini response was invalid. No automatic second request was sent."
        ) from exc


def validate_generated(generated, source_part, config, hook):
    source_scenes = source_part["scenes"]
    part_number = int(source_part["part"])

    if not isinstance(generated, list) or len(generated) != len(source_scenes):
        raise RuntimeError(
            f"Part {part_number}: expected {len(source_scenes)} scenes; "
            f"received {len(generated) if isinstance(generated, list) else 'invalid'}."
        )

    result = []
    for index, (source, scene) in enumerate(zip(source_scenes, generated), 1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Invalid generated scene {part_number}:{index}.")
        try:
            scene_number = int(scene.get("scene", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid generated scene number.") from exc
        if scene_number != index:
            raise RuntimeError(f"Scene order mismatch at {part_number}:{index}.")

        item = {
            "part": part_number,
            "scene": index,
            "part_id": f"part_{part_number:02d}",
            "scene_id": f"part_{part_number:02d}_scene_{index:02d}",
            "global_scene_number": source.get("global_scene_number"),
            "mapping_version": 3,
            "source_scene_id": source.get(
                "scene_id", f"part_{part_number:02d}_scene_{index:02d}"
            ),
            "role": str(source.get("role", source.get("purpose", ""))),
            "status": "completed",
        }

        for field in SCENE_FIELDS:
            value = scene.get(field, "")
            if not isinstance(value, str):
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: {field} must be text."
                )
            item[field] = value.strip()

        if not item["narration"]:
            raise RuntimeError(f"Empty narration at {part_number}:{index}.")
        if len(item["visual_prompt"]) < 30:
            raise RuntimeError(f"Visual prompt too short at {part_number}:{index}.")

        if index == 1 and (part_number == 1 or bool_value(config, "PART_HOOK", True)):
            minimum = (
                7 if normalize_format(config) == "short" else 14
            ) if part_number == 1 else 7
            if word_count(item["narration"]) < minimum:
                raise RuntimeError(
                    f"Hook too short at part {part_number}, scene 1."
                )

        if part_number == 1 and index == 1 and hook:
            if not normalize_text(item["narration"]).startswith(normalize_text(hook)):
                raise RuntimeError("First narration does not start with the story hook.")

        if bool_value(config, "PART_SUSPENSE", True) and index == len(source_scenes):
            if word_count(item["suspense_prompt"]) < 4:
                raise RuntimeError(
                    f"Ending suspense too short at {part_number}:{index}."
                )

        if not bool_value(config, "SFX", True):
            item["sfx_prompt"] = ""
        if not bool_value(config, "MUSIC", True):
            item["music_prompt"] = ""

        result.append(item)

    return result


def main():
    config = load_input_config()
    format_type = normalize_format(config)
    model = selected_model()

    story = read_json(SOURCE_FILE)
    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("story.json is missing or not marked completed.")

    total = validate_source(story, config)
    topic = str(get_topic(config) or story.get("topic", "")).strip()
    story_text = str(get_story_text(config) or "").strip()
    if not topic and not story_text:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    hook = str(story.get("hook", "")).strip()
    minimum_hook = 7 if format_type == "short" else 14
    if word_count(hook) < minimum_hook:
        raise RuntimeError(
            f"story.json hook must contain at least {minimum_hook} words."
        )

    source_fingerprint = fingerprint({
        "version": VERSION,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "format": format_type,
        "model": model,
        "config": config,
    })

    existing = read_json(OUTPUT_FILE, required=False)
    saved_parts = {}

    if (
        isinstance(existing, dict)
        and existing.get("source_fingerprint") == source_fingerprint
        and isinstance(existing.get("parts"), list)
    ):
        for saved in existing["parts"]:
            if isinstance(saved, dict):
                try:
                    saved_parts[int(saved.get("part", -1))] = saved
                except (TypeError, ValueError):
                    pass

    result = {
        "status": "in_progress",
        "version": VERSION,
        "mapping_version": 3,
        "scene_order": "part_then_scene",
        "topic": topic,
        "story_text": story_text,
        "format": format_type,
        "model": model,
        "source_fingerprint": source_fingerprint,
        "total_scenes": total,
        "parts": [],
    }

    log("Format:", format_type)
    log("Parts:", len(story["parts"]))
    log("Total scenes:", total)
    log("Automatic generation retries: disabled")
    log("Gemini generation requires explicit billing authorization.")

    global_number = 0

    for source_part in story["parts"]:
        part_number = int(source_part["part"])
        saved = saved_parts.get(part_number)
        scenes = None

        if (
            isinstance(saved, dict)
            and saved.get("status") == "completed"
            and isinstance(saved.get("scenes"), list)
        ):
            try:
                scenes = validate_generated(
                    saved["scenes"], source_part, config, hook
                )
                log(f"Part {part_number}: validated checkpoint reused.")
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                log(f"Part {part_number}: checkpoint invalid; needs regeneration: {exc}")
                scenes = None

        if scenes is None:
            prompt = make_prompt(
                story, source_part, config, topic, story_text
            )
            log(f"Part {part_number}: sending at most one generation request.")
            response = call_gemini(model, prompt)
            scenes = validate_generated(
                response.get("scenes", []), source_part, config, hook
            )

        for scene in scenes:
            global_number += 1
            scene["global_scene_number"] = global_number

        result["parts"].append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "status": "completed",
            "title": str(source_part.get("title") or f"Part {part_number}"),
            "hook_required": part_number == 1 or bool_value(config, "PART_HOOK", True),
            "suspense_required": bool_value(config, "PART_SUSPENSE", True),
            "final_resolution": (
                bool_value(config, "FINAL_RESOLUTION", True)
                and part_number == len(story["parts"])
            ),
            "scenes": scenes,
        })

        # Save after every part, so later failures preserve completed work.
        save_json(result)
        log(
            f"Part {part_number} saved: "
            f"{sum(len(p['scenes']) for p in result['parts'])}/{total} scenes."
        )

    actual = sum(len(part["scenes"]) for part in result["parts"])
    if actual != total:
        raise RuntimeError(f"Final scene count mismatch: {actual}/{total}.")

    expected_ids = [
        f"part_{p:02d}_scene_{s:02d}"
        for p, part in enumerate(result["parts"], 1)
        for s in range(1, len(part["scenes"]) + 1)
    ]
    actual_ids = [
        scene["scene_id"]
        for part in result["parts"]
        for scene in part["scenes"]
    ]
    if actual_ids != expected_ids:
        raise RuntimeError("Stable scene ID sequence validation failed.")

    result["status"] = "completed"
    result["validation"] = {
        "version": VERSION,
        "total_scenes": actual,
        "stable_scene_ids": True,
        "sequential_scene_mapping": True,
        "opening_hook_validated": True,
        "part_suspense_validated": bool_value(config, "PART_SUSPENSE", True),
        "automatic_retries": False,
    }
    save_json(result)

    log("AI STORY GENERATION: SUCCESS")
    log("Output:", OUTPUT_FILE)
    log("Validated scenes:", actual)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("ERROR:", exc)
        sys.exit(1)