#!/usr/bin/env python3
"""Generate resumable cinematic scenes with mandatory hook and suspense checks."""

import hashlib
import json
import os
import random
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
    get_parts,
    get_scenes,
    get_max_retries,
    get_topic,
    get_story_text,
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output" / "scenes"
SCENES_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TIMEOUT = max(30, int(os.getenv("SCENE_REQUEST_TIMEOUT", "120")))
DELAY = max(0.0, float(os.getenv("SCENE_REQUEST_DELAY", "2")))


def log(message=""):
    print(message, flush=True)


def read_json(path, required=False):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON at {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def fingerprint(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    return default


def word_count(value):
    return len(re.findall(r"\S+", str(value).strip()))


def selected_model():
    data = read_json(MODEL_FILE, required=True)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "No tested Gemini model selected. Run "
            "scripts/select_gemini_model.py first."
        )
    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError("selected_model.json has an invalid model ID.")
    return model


def load_story():
    # Prefer the AI-expanded story, then fall back to the original story.
    for path in (
        ROOT / "output" / "story" / "ai_story.json",
        ROOT / "output" / "story" / "story.json",
    ):
        data = read_json(path)
        if isinstance(data, dict) and data.get("parts"):
            return data
    raise RuntimeError("No usable story parts found in ai_story.json/story.json.")


def load_character_bible(config):
    data = read_json(BIBLE_FILE)
    if data is None:
        if cfg_bool(config, "CHARACTER_BIBLE", True):
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but character_bible.json is missing."
            )
        return {"status": "unavailable", "characters": [], "world": {}}
    if not isinstance(data, dict):
        raise RuntimeError("character_bible.json must be a JSON object.")
    return data


def build_settings(config):
    return {
        "format": normalize_format(config),
        "audience": cfg_text(config, "AUDIENCE", "adult"),
        "story_length": cfg_text(config, "STORY_LENGTH", "auto"),
        "scene_duration": cfg_text(config, "SCENE_DURATION", "auto"),
        "visual_style": cfg_text(config, "VISUAL_STYLE", "cinematic_realistic"),
        "realism": cfg_text(config, "REALISM", "high"),
        "quality": cfg_text(config, "QUALITY", "high"),
        "camera_style": cfg_text(config, "CAMERA_STYLE", "cinematic"),
        "lighting": cfg_text(config, "LIGHTING", "cinematic"),
        "mood": cfg_text(config, "MOOD", "dramatic"),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),
        "captions": cfg_text(config, "CAPTIONS", "hindi"),
        "part_hook": cfg_bool(config, "PART_HOOK", True),
        "part_suspense": cfg_bool(config, "PART_SUSPENSE", True),
        "final_resolution": cfg_bool(config, "FINAL_RESOLUTION", True),
        "character_bible": cfg_bool(config, "CHARACTER_BIBLE", True),
        "character_consistency": cfg_bool(config, "CHARACTER_CONSISTENCY", True),
        "world_consistency": cfg_bool(config, "WORLD_CONSISTENCY", True),
        "scene_continuity": cfg_bool(config, "SCENE_CONTINUITY", True),
        "cinematic_camera": cfg_bool(config, "CINEMATIC_CAMERA", True),
        "natural_motion": cfg_bool(config, "NATURAL_MOTION", True),
        "realistic_lighting": cfg_bool(config, "REALISTIC_LIGHTING", True),
        "avoid_cartoon": cfg_bool(config, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": cfg_bool(config, "AVOID_NEON", True),
        "avoid_glitch": cfg_bool(config, "AVOID_GLITCH_EFFECTS", True),
        "negative_prompt": cfg_bool(config, "NEGATIVE_PROMPT", True),
        "music": cfg_bool(config, "MUSIC", True),
        "music_style": cfg_text(config, "MUSIC_STYLE", "cinematic"),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
    }


def make_source_fingerprint(config, settings, story, bible, model, topic, text):
    return fingerprint({
        "version": 4,
        "topic": topic,
        "story_text": text,
        "story": story,
        "character_bible": bible,
        "model": model,
        "settings": settings,
        "parts": int(get_parts(config)),
        "scenes_per_part": int(get_scenes(config)),
    })


def scene_key(part, scene):
    return f"{part}:{scene}"


def needs_hook(part, scene, settings):
    # The first scene of the complete video always needs a hook.
    # PART_HOOK adds a hook to the opening scene of every part.
    return (part == 1 and scene == 1) or (
        settings["part_hook"] and scene == 1
    )


def needs_suspense(scene, scenes_per_part, settings):
    return settings["part_suspense"] and scene == scenes_per_part


def validate_scene(scene, part, number, settings, scenes_per_part):
    if not isinstance(scene, dict):
        raise RuntimeError("Generated scene must be a JSON object.")

    scene["part"] = part
    scene["scene"] = number

    text_fields = (
        "title", "narration", "visual_prompt", "negative_prompt",
        "duration", "transition", "camera", "lighting", "mood",
        "sfx", "ambient_sound", "music_direction", "world_context",
        "suspense_prompt",
    )
    for key in text_fields:
        value = scene.get(key, "")
        if not isinstance(value, str):
            raise RuntimeError(
                f"Part {part}, scene {number}: {key} must be text."
            )
        scene[key] = value.strip()

    if not scene["narration"]:
        raise RuntimeError(f"Part {part}, scene {number}: narration is empty.")
    if not scene["visual_prompt"]:
        raise RuntimeError(
            f"Part {part}, scene {number}: visual_prompt is empty."
        )

    # Structural hook validation. Semantic quality still depends on the model.
    if needs_hook(part, number, settings):
        minimum = 7 if settings["format"] == "short" else 14
        if word_count(scene["narration"]) < minimum:
            raise RuntimeError(
                f"Part {part}, scene {number}: opening hook needs at least "
                f"{minimum} narration words for {settings['format']} format."
            )
        if len(scene["visual_prompt"]) < 30:
            raise RuntimeError(
                f"Part {part}, scene {number}: hook scene needs a detailed visual."
            )

    # The last scene of each part must contain an explicit suspense direction.
    if needs_suspense(number, scenes_per_part, settings):
        if word_count(scene["suspense_prompt"]) < 4:
            raise RuntimeError(
                f"Part {part}, scene {number}: required suspense_prompt "
                "is missing or too short."
            )
        if word_count(scene["narration"]) < 6:
            raise RuntimeError(
                f"Part {part}, scene {number}: suspense narration is too short."
            )

    ids = scene.get("character_ids", [])
    scene["character_ids"] = ids if isinstance(ids, list) else []
    scene["title"] = scene["title"] or f"Part {part}, Scene {number}"
    scene["status"] = "completed"
    scene["opening_hook_required"] = needs_hook(part, number, settings)
    scene["suspense_required"] = needs_suspense(
        number, scenes_per_part, settings
    )
    return scene


def validate_saved_scene(scene, part, number, settings, scenes_per_part):
    if not isinstance(scene, dict):
        return False
    if scene.get("status") != "completed":
        return False
    try:
        validate_scene(
            dict(scene), part, number, settings, scenes_per_part
        )
        return True
    except (RuntimeError, TypeError, ValueError):
        return False


def load_compatible_scenes(source_fp, parts, scenes_per_part, settings):
    data = read_json(SCENES_FILE)
    if not isinstance(data, dict):
        log("No reusable scene file found.")
        return {}
    if data.get("source_fingerprint") != source_fp:
        log("Scene fingerprint changed; old checkpoints will not be reused.")
        return {}

    items = data.get("scenes", [])
    if not isinstance(items, list):
        log("Saved scenes list is invalid; regenerating.")
        return {}

    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            part = int(item.get("part", -1))
            number = int(item.get("scene", -1))
        except (TypeError, ValueError):
            continue
        if not (1 <= part <= parts and 1 <= number <= scenes_per_part):
            continue
        if validate_saved_scene(item, part, number, settings, scenes_per_part):
            result[scene_key(part, number)] = item

    log(f"Reusable validated scenes: {len(result)}/{parts * scenes_per_part}")
    return result


def save_progress(
    scenes, total, source_fp, model, settings, topic, story_text,
    status="in_progress", failed_scene=None,
):
    ordered = sorted(
        scenes.values(),
        key=lambda item: (int(item["part"]), int(item["scene"])),
    )
    write_json(SCENES_FILE, {
        "status": status,
        "model": model,
        "source_fingerprint": source_fp,
        "total_scenes": total,
        "completed_scenes": len(ordered),
        "input_config": settings,
        "topic": topic,
        "story_text": story_text,
        "scenes": ordered,
    })
    keys = sorted(
        scenes.keys(),
        key=lambda key: tuple(int(x) for x in key.split(":")),
    )
    write_json(CHECKPOINT_FILE, {
        "status": status,
        "source_fingerprint": source_fp,
        "model": model,
        "total_scenes": total,
        "completed_scenes": keys,
        "completed_count": len(keys),
        "remaining_scenes": max(0, total - len(keys)),
        "failed_scene": failed_scene,
        "updated_at": int(time.time()),
    })


def previous_context(scenes, part, number):
    earlier = sorted(
        (
            scene for scene in scenes.values()
            if (int(scene["part"]), int(scene["scene"])) < (part, number)
        ),
        key=lambda item: (int(item["part"]), int(item["scene"])),
    )
    return earlier[-3:]


def build_prompt(
    settings, story, bible, previous, part, number,
    scenes_per_part, topic, story_text, total_parts,
):
    hook = needs_hook(part, number, settings)
    suspense = needs_suspense(number, scenes_per_part, settings)
    final_scene = (
        part == total_parts
        and number == scenes_per_part
        and settings["final_resolution"]
    )

    if settings["format"] == "short":
        hook_rule = (
            "HOOK REQUIRED: Start the narration with a punchy curiosity gap, "
            "surprising event, danger, mystery or emotional conflict. Aim for "
            "1-2 concise Hindi sentences; no greeting or slow introduction."
        )
    else:
        hook_rule = (
            "HOOK REQUIRED: Start with a more developed, emotionally engaging "
            "hook of about 2-4 meaningful Hindi sentences. Establish mystery, "
            "stakes, danger or an emotional dilemma without revealing the ending."
        )

    if suspense:
        suspense_rule = (
            "SUSPENSE REQUIRED: End this scene with a story-specific clue, "
            "reveal, unresolved question, reversal or approaching threat. "
            "Include it in narration and describe the exact beat in suspense_prompt."
        )
    else:
        suspense_rule = (
            "Do not force a cliffhanger into this scene. Keep it consistent "
            "with the story and scene role."
        )

    ending_rule = (
        "Resolve the central conflict clearly in this final story scene. "
        "A small sequel mystery is allowed, but the main conflict must be resolved."
        if final_scene else
        "Do not accidentally resolve the entire story early."
    )

    return f"""
You are a professional Hindi cinematic scene director.
Return ONLY one valid JSON object.

TOPIC:
{topic or "(not supplied)"}

USER STORY TEXT:
{story_text or "(not supplied)"}

FULL STORY SOURCE:
{json.dumps(story, ensure_ascii=False)}

CHARACTER AND WORLD BIBLE:
{json.dumps(bible, ensure_ascii=False)}

PREVIOUS SCENES:
{json.dumps(previous, ensure_ascii=False)}

CURRENT POSITION:
Part {part} of {total_parts}; scene {number} of {scenes_per_part}.

FORMAT: {settings["format"]}
AUDIENCE: {settings["audience"]}
STORY LENGTH: {settings["story_length"]}
SCENE DURATION: {settings["scene_duration"]}
VISUAL STYLE: {settings["visual_style"]}
REALISM: {settings["realism"]}
QUALITY: {settings["quality"]}
CAMERA: {settings["camera_style"]}
LIGHTING: {settings["lighting"]}
MOOD: {settings["mood"]}
TRANSITIONS: {settings["transitions"]}

OPENING HOOK REQUIRED FOR THIS SCENE: {hook}
{hook_rule if hook else "Continue the story naturally; do not add a forced opening hook."}

SUSPENSE REQUIRED FOR THIS SCENE: {suspense}
{suspense_rule}

FINAL RESOLUTION:
{ending_rule}

CONTINUITY:
- Preserve names, age, face, hair, clothing, locations and story facts.
- Keep cause and effect logical and connect naturally with previous scenes.
- Use natural Hindi narration and believable spoken dialogue.
- No channel greeting, subscribe request or filler.
- Describe photorealistic live-action people and believable real locations.
- Include physical action, facial expression, posture, environment and lighting.
- Avoid cartoon, anime, comic, illustration and slideshow aesthetics.
- Use subtle, believable motion and consistent character identity.
- Music enabled={settings["music"]}; style={settings["music_style"]}.
- SFX enabled={settings["sfx"]}; ambient sound enabled={settings["ambient_sound"]}.

Return this exact field structure:
{{
  "part": {part},
  "scene": {number},
  "title": "short scene title",
  "character_ids": [],
  "world_context": "location and world context",
  "narration": "natural Hindi narration",
  "visual_prompt": "detailed photorealistic live-action visual",
  "negative_prompt": "unwanted visual elements",
  "duration": "{settings["scene_duration"]}",
  "transition": "{settings["transitions"]}",
  "camera": "shot size, lens feel and camera movement",
  "lighting": "{settings["lighting"]}",
  "mood": "{settings["mood"]}",
  "sfx": "sound effects or empty string",
  "ambient_sound": "ambient sound or empty string",
  "music_direction": "music direction or empty string",
  "suspense_prompt": "specific suspense beat when required; otherwise empty string"
}}

The hook must be in narration, not only visual_prompt.
The suspense beat must relate to this exact story, not generic danger.
Return exactly one scene and keep the supplied part and scene numbers.
""".strip()


def call_gemini(model, prompt, max_attempts):
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
            "temperature": 0.7,
            "topP": 0.9,
            "responseMimeType": "application/json",
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = None

    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": API_KEY,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))

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

            parts = candidate.get("content", {}).get("parts", [])
            text = "\n".join(
                item.get("text", "")
                for item in parts
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
            ).strip()
            if not text:
                raise RuntimeError("Gemini returned empty scene text.")

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start < 0 or end <= start:
                    raise RuntimeError("Gemini did not return valid scene JSON.")
                data = json.loads(text[start:end + 1])

            if not isinstance(data, dict):
                raise RuntimeError("Generated scene must be a JSON object.")
            return data

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            last_error = RuntimeError(f"Gemini HTTP {exc.code}: {detail}")
            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                wait = max(1, float(retry_after)) if retry_after else min(
                    60, 5 * (2 ** (attempt - 1))
                )
            except ValueError:
                wait = min(60, 5 * (2 ** (attempt - 1)))
        except Exception as exc:
            last_error = exc
            wait = min(30, 3 * (2 ** (attempt - 1)))

        if attempt < max_attempts:
            wait += random.uniform(0, min(3, wait * 0.1))
            log(f"Generation failed: {last_error}; retrying in {wait:.1f}s.")
            time.sleep(wait)

    raise RuntimeError(f"Scene generation failed: {last_error}")


def main():
    log("=== KATHA LOK AI: HOOK + SUSPENSE SCENE GENERATION ===")
    config = load_input_config()
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must both be positive.")

    settings = build_settings(config)
    model = selected_model()
    story = load_story()
    bible = load_character_bible(config)
    topic = str(get_topic(config) or "").strip()
    story_text = str(get_story_text(config) or "").strip()

    source_fp = make_source_fingerprint(
        config, settings, story, bible, model, topic, story_text
    )
    total = parts * scenes_per_part
    attempts = max(1, min(10, int(get_max_retries(config))))

    scenes = load_compatible_scenes(
        source_fp, parts, scenes_per_part, settings
    )

    log(f"Model: {model}")
    log(f"Format: {settings['format']}")
    log(f"Total scenes: {total}")
    log("First video scene hook: REQUIRED")
    log(f"Hook at every part opening: {settings['part_hook']}")
    log(f"Suspense at every part ending: {settings['part_suspense']}")
    log(f"Matching checkpoints: {len(scenes)}")

    for part in range(1, parts + 1):
        for number in range(1, scenes_per_part + 1):
            key = scene_key(part, number)
            if key in scenes:
                log(f"Part {part}, scene {number}: validated checkpoint reused.")
                continue

            prompt = build_prompt(
                settings, story, bible, previous_context(scenes, part, number),
                part, number, scenes_per_part, topic, story_text, parts,
            )

            try:
                generated = call_gemini(model, prompt, attempts)
                scene = validate_scene(
                    generated, part, number, settings, scenes_per_part
                )
            except Exception:
                save_progress(
                    scenes, total, source_fp, model, settings,
                    topic, story_text, failed_scene=key,
                )
                raise

            scenes[key] = scene
            save_progress(
                scenes, total, source_fp, model, settings, topic, story_text
            )
            log(
                f"Saved Part {part}, scene {number}: "
                f"{len(scenes)}/{total}; hook/suspense checks passed."
            )
            if len(scenes) < total:
                time.sleep(DELAY)

    expected = {
        scene_key(part, number)
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    }
    if set(scenes) != expected:
        missing = sorted(expected - set(scenes))
        raise RuntimeError(f"Missing scenes: {missing[:20]}")

    # Validate every scene again before marking the whole file completed.
    for part in range(1, parts + 1):
        for number in range(1, scenes_per_part + 1):
            key = scene_key(part, number)
            if not validate_saved_scene(
                scenes[key], part, number, settings, scenes_per_part
            ):
                raise RuntimeError(
                    f"Final validation failed for Part {part}, scene {number}."
                )

    save_progress(
        scenes, total, source_fp, model, settings, topic, story_text,
        status="completed",
    )
    log(f"SUCCESS: all {total} scenes passed structural validation.")
    log(f"Output: {SCENES_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)