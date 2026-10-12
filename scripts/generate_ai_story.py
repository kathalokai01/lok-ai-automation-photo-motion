#!/usr/bin/env python3
"""Generate resumable, source-aligned AI story scenes with stable IDs."""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from input_config import (
    load_input_config,
    normalize_format,
    get_max_retries,
    get_topic,
    get_story_text,
)

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "output" / "story"
INPUT = STORY_DIR / "story.json"
OUTPUT = STORY_DIR / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TIMEOUT = max(30, int(os.getenv("SCENE_REQUEST_TIMEOUT", "120")))

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


def log(message=""):
    print(message, flush=True)


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON: {path}: {exc}") from exc


def save_json(data):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(OUTPUT)


def normalize_text(value):
    value = str(value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value):
    return len(normalize_text(value).split())


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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

    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "No successfully selected Gemini model. "
            "Run scripts/select_gemini_model.py first."
        )

    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid Gemini model ID: {model!r}")

    return model


def build_config(source):
    return {
        "format": normalize_format(source),
        "audience": text_value(source, "AUDIENCE", "adult"),
        "story_length": text_value(source, "STORY_LENGTH", "auto"),
        "scene_duration": text_value(source, "SCENE_DURATION", "auto"),
        "part_hook": bool_value(source, "PART_HOOK", True),
        "part_suspense": bool_value(source, "PART_SUSPENSE", True),
        "final_resolution": bool_value(source, "FINAL_RESOLUTION", True),
        "visual_style": text_value(
            source, "VISUAL_STYLE", "cinematic_realistic"
        ),
        "realism": text_value(source, "REALISM", "high"),
        "camera_style": text_value(source, "CAMERA_STYLE", "cinematic"),
        "lighting": text_value(source, "LIGHTING", "cinematic"),
        "mood": text_value(source, "MOOD", "dramatic"),
        "music": bool_value(source, "MUSIC", True),
        "music_style": text_value(source, "MUSIC_STYLE", "cinematic"),
        "sfx": bool_value(source, "SFX", True),
        "ambient_sound": bool_value(source, "AMBIENT_SOUND", True),
        "max_retries": max(1, min(5, int(get_max_retries(source)))),
    }


def validate_source(story, expected_parts=None, expected_scenes=None):
    parts = story.get("parts")

    if not isinstance(parts, list) or not parts:
        raise RuntimeError("story.json has no parts.")

    if expected_parts is not None and len(parts) != expected_parts:
        raise RuntimeError(
            f"story.json has {len(parts)} parts; "
            f"Input/topic.txt requests {expected_parts}."
        )

    total = 0

    for p_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Part {p_index} is invalid.")

        if int(part.get("part", -1)) != p_index:
            raise RuntimeError(f"Part numbering mismatch at part {p_index}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {p_index} has no scenes.")

        if expected_scenes is not None and len(scenes) != expected_scenes:
            raise RuntimeError(
                f"Part {p_index} has {len(scenes)} scenes; "
                f"Input/topic.txt requests {expected_scenes}."
            )

        for s_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {p_index}, scene {s_index} is invalid."
                )

            if int(scene.get("scene", -1)) != s_index:
                raise RuntimeError(
                    f"Scene numbering mismatch at part {p_index}, "
                    f"scene {s_index}."
                )

        total += len(scenes)

    return total


def call_gemini(model, prompt):
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

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
            "x-goog-api-key": API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini network error: {exc}") from exc

    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError(
            str(result.get("error", {}).get(
                "message", "Gemini returned no candidates."
            ))
        )

    candidate = candidates[0]
    if str(candidate.get("finishReason", "")).upper() in {
        "MAX_TOKENS", "LENGTH"
    }:
        raise RuntimeError("Gemini response was truncated.")

    response_text = "\n".join(
        item.get("text", "")
        for item in candidate.get("content", {}).get("parts", [])
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ).strip()

    if not response_text:
        raise RuntimeError("Gemini returned empty text.")

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned invalid JSON.") from exc

    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise RuntimeError("Gemini response must contain a scenes array.")

    return data


def make_prompt(story, part, config, topic, story_text):
    part_number = int(part["part"])
    source_scenes = part["scenes"]
    first_part = part_number == 1
    last_part = part_number == len(story["parts"])
    hook = str(story.get("hook", "")).strip()

    scene_context = []
    for source in source_scenes:
        scene_context.append({
            "scene": source["scene"],
            "scene_id": source.get("scene_id", ""),
            "global_scene_number": source.get("global_scene_number"),
            "purpose": source.get("purpose", source.get("role", "")),
            "narration": source.get("narration", ""),
            "visual_prompt": source.get(
                "visual_prompt", source.get("visual", "")
            ),
            "dialogue": source.get("dialogue", ""),
            "suspense_prompt": source.get("suspense_prompt", ""),
        })

    hook_rule = (
        "First scene must begin with the exact story hook supplied below."
        if first_part else
        "Create a distinct part-opening hook in scene 1 when part hooks are enabled."
    )

    suspense_rule = (
        "The final scene of this part needs story-specific suspense in "
        "narration and suspense_prompt."
        if config["part_suspense"] else
        "Use suspense only when it naturally fits the source story."
    )

    ending_rule = (
        "This is the final story part. Resolve the central conflict clearly."
        if last_part and config["final_resolution"] else
        "Preserve the story progression and lead naturally to the next part."
    )

    return f"""
You are the Hindi cinematic story writer for Katha Lok AI.
Return only valid JSON. Do not return Markdown.

TOPIC: {topic}
USER STORY TEXT: {story_text or "(not supplied)"}
STORY TITLE: {story.get("title", topic)}
EXACT STORY HOOK: {hook}
FORMAT: {config["format"]}
AUDIENCE: {config["audience"]}
STORY LENGTH: {config["story_length"]}
SCENE DURATION: {config["scene_duration"]}
PART: {part_number} of {len(story["parts"])}

SOURCE SCENES:
{json.dumps(scene_context, ensure_ascii=False, indent=2)}

OPENING:
{hook_rule}
For short format, first hook must have at least 7 words.
For full format, first hook must have at least 14 words.
Do not add greetings, channel intros or subscribe requests.

PART ENDING:
{suspense_rule}

STORY ENDING:
{ending_rule}

CONTINUITY:
- Return exactly {len(source_scenes)} scenes in the original order.
- Do not merge, omit, duplicate or add scenes.
- Preserve characters, ages, names, relationships, clothing and timeline.
- Keep narration natural Hindi and dialogue believable.
- Keep every scene aligned to its corresponding source event.
- Maintain photorealistic live-action style; no cartoon or slideshow look.
- Write detailed visual prompts with plausible actions, light and camera framing.
- Keep optional dialogue and audio prompts as empty strings when not needed.

STYLE:
visual_style={config["visual_style"]}
realism={config["realism"]}
camera={config["camera_style"]}
lighting={config["lighting"]}
mood={config["mood"]}
music_enabled={config["music"]}
music_style={config["music_style"]}
sfx_enabled={config["sfx"]}
ambient_sound_enabled={config["ambient_sound"]}

Every scene must contain these string fields:
scene, narration, dialogue, visual_prompt, negative_prompt,
camera_prompt, lighting_prompt, sfx_prompt, music_prompt, suspense_prompt.

Return JSON in this exact overall shape:
{{
  "scenes": [
    {{
      "scene": 1,
      "narration": "...",
      "dialogue": "",
      "visual_prompt": "...",
      "negative_prompt": "...",
      "camera_prompt": "...",
      "lighting_prompt": "...",
      "sfx_prompt": "",
      "music_prompt": "",
      "suspense_prompt": ""
    }}
  ]
}}
""".strip()


def validate_generated(generated, source_part, config, hook):
    source_scenes = source_part["scenes"]

    if not isinstance(generated, list) or len(generated) != len(source_scenes):
        raise RuntimeError(
            f"Part {source_part['part']}: expected "
            f"{len(source_scenes)} scenes, received "
            f"{len(generated) if isinstance(generated, list) else 'invalid'}."
        )

    result = []
    part_number = int(source_part["part"])

    for index, (source, scene) in enumerate(
        zip(source_scenes, generated), start=1
    ):
        if not isinstance(scene, dict) or int(scene.get("scene", -1)) != index:
            raise RuntimeError(
                f"Part {part_number}: scene order mismatch at scene {index}."
            )

        item = {
            "part": part_number,
            "scene": index,
            "part_id": f"part_{part_number:02d}",
            "scene_id": f"part_{part_number:02d}_scene_{index:02d}",
            "global_scene_number": (
                source.get("global_scene_number")
                if isinstance(source.get("global_scene_number"), int)
                else sum(
                    len(p["scenes"])
                    for p in []
                )
            ),
            "mapping_version": 2,
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
                    f"Part {part_number}, scene {index}: "
                    f"{field} must be a string."
                )
            item[field] = value.strip()

        if not item["narration"]:
            raise RuntimeError(
                f"Part {part_number}, scene {index}: narration is empty."
            )
        if len(item["visual_prompt"]) < 30:
            raise RuntimeError(
                f"Part {part_number}, scene {index}: visual prompt is too short."
            )

        if index == 1 and (part_number == 1 or config["part_hook"]):
            minimum = (
                7 if config["format"] == "short" else 14
            ) if part_number == 1 else 7
            if word_count(item["narration"]) < minimum:
                raise RuntimeError(
                    f"Part {part_number}, scene 1: hook needs "
                    f"at least {minimum} words."
                )

        if part_number == 1 and index == 1 and hook:
            if not normalize_text(item["narration"]).startswith(
                normalize_text(hook)
            ):
                raise RuntimeError(
                    "First narration does not begin with the exact story hook."
                )

        if config["part_suspense"] and index == len(source_scenes):
            if word_count(item["suspense_prompt"]) < 4:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: suspense prompt "
                    "must contain at least 4 words."
                )

        if not config["sfx"]:
            item["sfx_prompt"] = ""
        if not config["music"]:
            item["music_prompt"] = ""

        result.append(item)

    return result


def main():
    source_config = load_input_config()
    config = build_config(source_config)
    model = selected_model()
    story = read_json(INPUT)

    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("story.json is missing or not marked completed.")

    parts_requested = int(source_config.get("PARTS", len(story.get("parts", []))))
    scenes_requested = int(source_config.get("SCENES", 0))
    total = validate_source(
        story,
        expected_parts=parts_requested,
        expected_scenes=scenes_requested if scenes_requested > 0 else None,
    )

    topic = str(get_topic(source_config) or story.get("topic", "")).strip()
    story_text = str(get_story_text(source_config) or "").strip()
    if not topic and not story_text:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    hook = str(story.get("hook", "")).strip()
    minimum_hook = 7 if config["format"] == "short" else 14
    if word_count(hook) < minimum_hook:
        raise RuntimeError(
            f"story.json hook must contain at least {minimum_hook} words."
        )

    source_fp = fingerprint({
        "mapping_version": 2,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "config": config,
        "model": model,
    })

    existing = read_json(OUTPUT, required=False)
    saved_parts = {}

    if (
        isinstance(existing, dict)
        and existing.get("source_fingerprint") == source_fp
        and existing.get("mapping_version") == 2
        and isinstance(existing.get("parts"), list)
    ):
        for part in existing["parts"]:
            if isinstance(part, dict):
                try:
                    saved_parts[int(part.get("part", -1))] = part
                except (TypeError, ValueError):
                    pass

    result = {
        "status": "in_progress",
        "mapping_version": 2,
        "scene_order": "part_then_scene",
        "topic": topic,
        "story_text": story_text,
        "format": config["format"],
        "model": model,
        "source_fingerprint": source_fp,
        "total_scenes": total,
        "parts": [],
    }

    log(f"Model: {model}")
    log(f"Format: {config['format']}")
    log(f"Parts: {len(story['parts'])}")
    log(f"Scenes per part: {len(story['parts'][0]['scenes'])}")
    log(f"Total scenes: {total}")
    log(f"Maximum API requests per part: {config['max_retries']}")

    global_number = 0

    for source_part in story["parts"]:
        part_number = int(source_part["part"])
        saved = saved_parts.get(part_number)
        checked = None

        if (
            isinstance(saved, dict)
            and saved.get("status") == "completed"
            and isinstance(saved.get("scenes"), list)
            and len(saved["scenes"]) == len(source_part["scenes"])
        ):
            try:
                checked = validate_generated(
                    saved["scenes"], source_part, config, hook
                )
                if any(
                    saved_scene.get("scene_id") != checked_scene["scene_id"]
                    for saved_scene, checked_scene in zip(
                        saved["scenes"], checked
                    )
                ):
                    checked = None
            except (RuntimeError, TypeError, ValueError, KeyError):
                checked = None

        if checked is not None:
            scenes = checked
            log(f"Part {part_number}: matching validated checkpoint reused.")
        else:
            last_error = None
            scenes = None

            for attempt in range(1, config["max_retries"] + 1):
                log(
                    f"Part {part_number}: API request "
                    f"{attempt}/{config['max_retries']}."
                )
                try:
                    response = call_gemini(
                        model,
                        make_prompt(
                            story, source_part, config, topic, story_text
                        ),
                    )
                    scenes = validate_generated(
                        response.get("scenes", []),
                        source_part,
                        config,
                        hook,
                    )
                    break
                except (
                    RuntimeError, TypeError, ValueError, KeyError,
                    urllib.error.URLError,
                ) as exc:
                    last_error = exc
                    log(f"Part {part_number} attempt failed: {exc}")
                    if attempt < config["max_retries"]:
                        time.sleep(min(3 * (2 ** (attempt - 1)), 12))

            if scenes is None:
                save_json(result)
                raise RuntimeError(
                    f"Part {part_number} failed after "
                    f"{config['max_retries']} attempts: {last_error}"
                )

        for scene in scenes:
            global_number += 1
            scene["global_scene_number"] = global_number

        result["parts"].append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "status": "completed",
            "title": str(
                source_part.get("title") or f"Part {part_number}"
            ),
            "hook_required": part_number == 1 or config["part_hook"],
            "suspense_required": config["part_suspense"],
            "final_resolution": (
                config["final_resolution"]
                and part_number == len(story["parts"])
            ),
            "scenes": scenes,
        })
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
        for p, part in enumerate(result["parts"], start=1)
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
        "mapping_version": 2,
        "total_scenes": actual,
        "stable_scene_ids": True,
        "sequential_scene_mapping": True,
        "opening_hook_validated": True,
        "part_suspense_validated": config["part_suspense"],
    }
    save_json(result)

    log("AI STORY GENERATION: SUCCESS")
    log(f"Output: {OUTPUT}")
    log(f"Validated scenes: {actual}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
