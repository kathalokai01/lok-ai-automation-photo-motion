File path: "scripts/generate_scenes.py"

#!/usr/bin/env python3
"""Generate resumable, fingerprint-validated cinematic scenes."""

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
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
REQUEST_TIMEOUT = max(30, int(os.getenv("SCENE_REQUEST_TIMEOUT", "120")))
REQUEST_DELAY = max(0.0, float(os.getenv("SCENE_REQUEST_DELAY", "2")))


def log(message=""):
    print(message, flush=True)


def read_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON file {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def fingerprint(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def selected_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "No tested Gemini model selected. Run "
            "scripts/select_gemini_model.py first."
        )

    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError("selected_model.json contains an invalid model ID.")

    return model


def load_story():
    for path in (
        ROOT / "output" / "story" / "ai_story.json",
        ROOT / "output" / "story" / "story.json",
    ):
        data = read_json(path)
        if isinstance(data, dict):
            return data
    raise RuntimeError("Neither ai_story.json nor story.json is available.")


def load_character_bible(config):
    data = read_json(BIBLE_FILE)
    enabled = cfg_bool(config, "CHARACTER_BIBLE", True)

    if data is None:
        if enabled:
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but character_bible.json is missing."
            )
        return {"status": "unavailable", "characters": [], "world": {}}

    if not isinstance(data, dict):
        raise RuntimeError("character_bible.json must contain a JSON object.")

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


def make_fingerprint(config, settings, story, bible, model, topic, story_text):
    # Include the actual source content and settings, not just scene numbers.
    return fingerprint({
        "version": 2,
        "topic": topic,
        "story_text": story_text,
        "story": story,
        "character_bible": bible,
        "model": model,
        "settings": settings,
        "parts": int(get_parts(config)),
        "scenes_per_part": int(get_scenes(config)),
    })


def scene_key(part, scene):
    return f"{part}:{scene}"


def valid_scene(scene, part, number):
    if not isinstance(scene, dict):
        return False
    try:
        if int(scene.get("part", -1)) != part:
            return False
        if int(scene.get("scene", -1)) != number:
            return False
    except (TypeError, ValueError):
        return False

    return bool(
        isinstance(scene.get("narration"), str)
        and scene["narration"].strip()
        and isinstance(scene.get("visual_prompt"), str)
        and scene["visual_prompt"].strip()
    )


def load_compatible_scenes(source_fingerprint, parts, scenes_per_part):
    data = read_json(SCENES_FILE)
    if not isinstance(data, dict):
        log("No reusable scene file found.")
        return {}

    if data.get("source_fingerprint") != source_fingerprint:
        log("Scene fingerprint changed; discarding old scene checkpoints.")
        return {}

    result = {}
    for item in data.get("scenes", []):
        if not isinstance(item, dict):
            continue
        try:
            part = int(item.get("part", -1))
            number = int(item.get("scene", -1))
        except (TypeError, ValueError):
            continue

        if 1 <= part <= parts and 1 <= number <= scenes_per_part:
            if valid_scene(item, part, number):
                result[scene_key(part, number)] = item

    log(f"Reusable matching scenes: {len(result)}/{parts * scenes_per_part}")
    return result


def save_progress(scenes, total, source_fingerprint, model, settings,
                  topic, story_text, status="in_progress", failed_scene=None):
    ordered = sorted(
        scenes.values(),
        key=lambda item: (int(item["part"]), int(item["scene"])),
    )

    document = {
        "status": status,
        "model": model,
        "source_fingerprint": source_fingerprint,
        "total_scenes": total,
        "completed_scenes": len(ordered),
        "input_config": settings,
        "topic": topic,
        "story_text": story_text,
        "scenes": ordered,
    }
    write_json(SCENES_FILE, document)

    completed_keys = sorted(scenes.keys())
    write_json(CHECKPOINT_FILE, {
        "status": status,
        "source_fingerprint": source_fingerprint,
        "model": model,
        "total_scenes": total,
        "completed_scenes": completed_keys,
        "completed_count": len(completed_keys),
        "remaining_scenes": max(0, total - len(completed_keys)),
        "failed_scene": failed_scene,
        "updated_at": int(time.time()),
    })


def previous_context(scenes, part, number):
    ordered = sorted(
        scenes.values(),
        key=lambda item: (int(item["part"]), int(item["scene"])),
    )
    earlier = [
        item for item in ordered
        if (int(item["part"]), int(item["scene"])) < (part, number)
    ]
    return earlier[-3:]


def build_prompt(config, settings, story, bible, previous, part, number,
                 scenes_per_part, topic, story_text):
    consistency = []
    if settings["character_consistency"]:
        consistency.append(
            "Maintain exact recurring character identity, face, age, hair, "
            "skin tone, body features and clothing unless the plot changes them."
        )
    if settings["world_consistency"]:
        consistency.append(
            "Maintain consistent locations, geography, architecture, time period "
            "and environment."
        )
    if settings["scene_continuity"]:
        consistency.append(
            "Continue naturally from earlier scenes; avoid unexplained jumps."
        )
    if settings["natural_motion"]:
        consistency.append(
            "Describe believable human and environmental motion suitable for "
            "photo-motion video generation."
        )

    if settings["format"] == "short":
        format_rule = (
            "Short-form story: strong curiosity, quick escalation and a clear "
            "payoff or intentional suspense. Avoid filler."
        )
    else:
        format_rule = (
            "Long-form story: maintain a coherent progression, character "
            "development, escalating conflict and meaningful resolution."
        )

    suspense_rule = (
        "The final scene of each part must contain a story-specific suspense beat."
        if settings["part_suspense"] else
        "Use suspense only where it naturally serves the story."
    )

    negative = []
    if settings["avoid_cartoon"]:
        negative.extend(["cartoon", "anime", "comic", "illustration", "game art"])
    if settings["avoid_neon"]:
        negative.extend(["unnecessary neon", "artificial glow"])
    if settings["avoid_glitch"]:
        negative.extend(["glitch", "digital distortion"])
    negative.extend([
        "plastic skin", "wax face", "deformed hands", "extra fingers",
        "duplicate people", "incorrect anatomy", "floating objects",
        "text artifacts", "watermark", "logo",
    ])

    return f"""
You are a professional Hindi cinematic scene director.
Return ONLY one valid JSON object. Do not return Markdown.

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
Part {part}, scene {number} of {scenes_per_part} in this part.

FORMAT:
{settings["format"]}

AUDIENCE: {settings["audience"]}
STORY LENGTH: {settings["story_length"]}
SCENE DURATION: {settings["scene_duration"]}
VISUAL STYLE: {settings["visual_style"]}
REALISM: {settings["realism"]}
QUALITY: {settings["quality"]}
CAMERA STYLE: {settings["camera_style"]}
LIGHTING: {settings["lighting"]}
MOOD: {settings["mood"]}
TRANSITIONS: {settings["transitions"]}

STRUCTURE RULE:
{format_rule}

PART HOOK ENABLED: {settings["part_hook"]}
PART SUSPENSE ENABLED: {settings["part_suspense"]}
FINAL RESOLUTION ENABLED: {settings["final_resolution"]}

CONTINUITY RULES:
{chr(10).join("- " + item for item in consistency) or "- Follow natural continuity."}

Create exactly ONE scene belonging to this story position.
Use natural, clear Hindi narration. The user's supplied story is authoritative;
do not replace it with an unrelated plot.

The visual_prompt must be a detailed photorealistic live-action description,
including character identity, clothing, body language, location, time of day,
lighting, framing, depth, expression, physical detail and natural motion.
Do not describe only a static photograph. Avoid cartoon or slideshow aesthetics.
When recurring characters appear, use character_ids from the character bible.

Audio preferences:
Music enabled={settings["music"]}; style={settings["music_style"]};
SFX enabled={settings["sfx"]}; ambient sound enabled={settings["ambient_sound"]}.

Negative visual prompt:
{", ".join(negative)}

Return this exact field structure:
{{
  "part": {part},
  "scene": {number},
  "title": "short scene title",
  "character_ids": [],
  "world_context": "specific location and world context",
  "narration": "Hindi narration",
  "visual_prompt": "detailed photorealistic live-action visual prompt with motion",
  "negative_prompt": "{", ".join(negative)}",
  "duration": "{settings["scene_duration"]}",
  "transition": "{settings["transitions"]}",
  "camera": "shot size, lens feel and camera movement",
  "lighting": "{settings["lighting"]}",
  "mood": "{settings["mood"]}",
  "sfx": "scene-specific sound effects or empty string",
  "ambient_sound": "scene-specific ambient sound or empty string",
  "music_direction": "music direction or empty string"
}}
""".strip()


def call_gemini(model, prompt, max_attempts):
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
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
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))

            candidates = result.get("candidates", [])
            if not candidates:
                raise RuntimeError(
                    str(result.get("error", {}).get(
                        "message", "Gemini returned no candidates."
                    ))
                )

            candidate = candidates[0]
            finish = str(candidate.get("finishReason", "")).upper()
            if finish in {"MAX_TOKENS", "LENGTH"}:
                raise RuntimeError("Gemini response was truncated.")

            parts = candidate.get("content", {}).get("parts", [])
            text = "\n".join(
                item.get("text", "")
                for item in parts
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ).strip()

            if not text:
                raise RuntimeError("Gemini returned empty scene text.")

            try:
                scene = json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start < 0 or end <= start:
                    raise RuntimeError("Gemini did not return valid scene JSON.")
                scene = json.loads(text[start:end + 1])

            if not isinstance(scene, dict):
                raise RuntimeError("Generated scene must be a JSON object.")
            return scene

        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:1000]
            last_error = RuntimeError(f"Gemini HTTP {exc.code}: {body_text}")
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
            log(f"Scene request failed: {last_error}; retrying in {wait:.1f}s")
            time.sleep(wait)

    raise RuntimeError(f"Scene generation failed: {last_error}")


def validate_scene(scene, part, number):
    if not isinstance(scene, dict):
        raise RuntimeError("Generated scene is not a JSON object.")

    scene["part"] = part
    scene["scene"] = number

    for key in (
        "title", "narration", "visual_prompt", "negative_prompt",
        "duration", "transition", "camera", "lighting", "mood",
        "sfx", "ambient_sound", "music_direction", "world_context",
    ):
        value = scene.get(key, "")
        if not isinstance(value, str):
            value = str(value)
        scene[key] = value.strip()

    if not scene["narration"]:
        raise RuntimeError(f"Part {part} scene {number}: narration is empty.")
    if not scene["visual_prompt"]:
        raise RuntimeError(f"Part {part} scene {number}: visual_prompt is empty.")

    character_ids = scene.get("character_ids", [])
    scene["character_ids"] = (
        character_ids if isinstance(character_ids, list) else []
    )
    scene.setdefault("title", f"Part {part}, Scene {number}")
    return scene


def main():
    log("=== KATHA LOK AI: SCENE GENERATION ===")

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
    source_fingerprint = make_fingerprint(
        config, settings, story, bible, model, topic, story_text
    )

    total = parts * scenes_per_part
    max_attempts = max(1, min(10, int(get_max_retries(config))))
    scenes = load_compatible_scenes(
        source_fingerprint, parts, scenes_per_part
    )

    log(f"Model: {model}")
    log(f"Format: {settings['format']}")
    log(f"Scenes: {len(scenes)}/{total} reusable")
    log(f"Fingerprint: {source_fingerprint[:16]}...")

    for part in range(1, parts + 1):
        for number in range(1, scenes_per_part + 1):
            key = scene_key(part, number)

            if key in scenes:
                log(f"Part {part} scene {number}: matching checkpoint reused.")
                continue

            prompt = build_prompt(
                config,
                settings,
                story,
                bible,
                previous_context(scenes, part, number),
                part,
                number,
                scenes_per_part,
                topic,
                story_text,
            )

            try:
                scene = call_gemini(model, prompt, max_attempts)
                scene = validate_scene(scene, part, number)
            except Exception:
                save_progress(
                    scenes, total, source_fingerprint, model, settings,
                    topic, story_text, failed_scene=key,
                )
                raise

            scenes[key] = scene
            save_progress(
                scenes, total, source_fingerprint, model, settings,
                topic, story_text,
            )
            log(f"Saved Part {part} scene {number} ({len(scenes)}/{total})")

            if len(scenes) < total:
                time.sleep(REQUEST_DELAY)

    expected = {
        scene_key(part, number)
        for part in range(1, parts + 1)
        for number in range(1, scenes_per_part + 1)
    }
    if set(scenes) != expected:
        missing = sorted(expected - set(scenes))
        raise RuntimeError(f"Missing scenes after generation: {missing[:20]}")

    save_progress(
        scenes, total, source_fingerprint, model, settings,
        topic, story_text, status="completed",
    )
    log(f"SUCCESS: all {total} scenes validated and saved.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
