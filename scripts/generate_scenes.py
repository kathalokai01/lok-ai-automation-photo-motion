
#!/usr/bin/env python3

import json
import os
import random
import re
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

# ============================================================
# PATHS / SETTINGS
# ============================================================

OUTPUT_DIR = Path("output/scenes")
SCENES_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = Path("output/checkpoints/scenes_progress.json")
SELECTED_MODEL_FILE = Path("output/config/selected_model.json")

REQUEST_DELAY = max(0.0, float(os.getenv("SCENE_REQUEST_DELAY", "2")))
REQUEST_TIMEOUT = max(1, int(os.getenv("SCENE_REQUEST_TIMEOUT", "120")))
INITIAL_BACKOFF = max(1, int(os.getenv("SCENE_INITIAL_BACKOFF", "15")))
MAX_BACKOFF = max(INITIAL_BACKOFF, int(os.getenv("SCENE_MAX_BACKOFF", "300")))


# ============================================================
# LOG
# ============================================================

def log(message=""):
    print(message, flush=True)


# ============================================================
# INPUT CONFIG HELPERS
# ============================================================

def cfg_text(config, key, default=""):
    value = config.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default

    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


def get_scene_duration_value(config):
    value = config.get("SCENE_DURATION", "auto")
    if value is None:
        return "auto"
    text = str(value).strip()
    return text if text else "auto"


def resolve_topic_value(config):
    try:
        return str(get_topic(config) or "").strip()
    except Exception:
        return str(config.get("TOPIC", "") or "").strip()


def resolve_story_text_value(config):
    try:
        return str(get_story_text(config) or "").strip()
    except Exception:
        return str(config.get("STORY_TEXT", "") or "").strip()


# ============================================================
# JSON HELPERS
# ============================================================

def read_json(path):
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"WARNING: Could not read JSON {path}: {exc}")
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


# ============================================================
# CHECKPOINT
# ============================================================

def scene_key(part_number, scene_number):
    return f"{part_number}:{scene_number}"


def save_checkpoint(total_scenes, completed_scenes, failed_scene=None):
    completed = sorted(list(completed_scenes))

    write_json(
        CHECKPOINT_FILE,
        {
            "status": (
                "completed"
                if len(completed) >= total_scenes
                else "in_progress"
            ),
            "total_scenes": total_scenes,
            "completed_scenes": completed,
            "completed_count": len(completed),
            "remaining_scenes": max(0, total_scenes - len(completed)),
            "failed_scene": failed_scene,
            "updated_at": int(time.time()),
        },
    )


def load_checkpoint():
    data = read_json(CHECKPOINT_FILE)
    if not isinstance(data, dict):
        return set()

    completed = data.get("completed_scenes", [])
    if not isinstance(completed, list):
        return set()

    return {
        item for item in completed
        if isinstance(item, str) and ":" in item
    }


# ============================================================
# EXISTING SCENES
# ============================================================

def scene_already_saved(existing_scenes, part_number, scene_number):
    target_key = scene_key(part_number, scene_number)
    target_ids = {
        target_key,
        f"scene_{part_number}_{scene_number}",
        f"part_{part_number}_scene_{scene_number}",
    }

    for scene in existing_scenes:
        if not isinstance(scene, dict):
            continue

        try:
            part = int(scene.get("part", -1))
            number = int(scene.get("scene", -1))
            if part == part_number and number == scene_number:
                return True
        except (TypeError, ValueError):
            pass

        scene_id = str(scene.get("id", "")).strip()
        if scene_id in target_ids:
            return True

    return False


# ============================================================
# SELECTED GEMINI MODEL
# ============================================================

def get_model():
    """
    Require a model explicitly selected and tested by
    scripts/select_gemini_model.py. Never silently choose a fallback.
    """
    if not SELECTED_MODEL_FILE.is_file():
        raise RuntimeError(
            "Selected Gemini model file is missing: "
            f"{SELECTED_MODEL_FILE}. Run scripts/select_gemini_model.py first."
        )

    data = read_json(SELECTED_MODEL_FILE)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{SELECTED_MODEL_FILE} is not valid JSON."
        )

    status = str(data.get("status", "")).strip().lower()
    if status != "selected":
        raise RuntimeError(
            "Gemini model selection is not marked as selected. "
            f"Current status: {status or 'missing'}. "
            "Run scripts/select_gemini_model.py first."
        )

    model = ""
    for key in ("model", "selected_model", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            model = value.strip()
            break

    # Gemini model identifiers should be simple IDs, not URLs or paths.
    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(
            "selected_model.json contains a missing or invalid model ID."
        )

    log(f"Validated selected Gemini model: {model}")
    return model


def get_api_key():
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    return key


# ============================================================
# GEMINI API
# ============================================================

def call_gemini(api_key, model, prompt):
    # API key is deliberately sent in a header, not in the URL.
    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.70,
            "topP": 0.90,
            "responseMimeType": "application/json",
        },
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw)

    except urllib.error.HTTPError:
        # The retry handler below decides which HTTP errors to retry.
        raise
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Gemini network error: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned an invalid JSON API response."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Gemini API response was not a JSON object.")

    candidates = data.get("candidates", [])
    if not candidates:
        api_error = data.get("error", {})
        detail = (
            api_error.get("message", "Gemini returned no candidates.")
            if isinstance(api_error, dict)
            else "Gemini returned no candidates."
        )
        raise RuntimeError(detail)

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise RuntimeError("Gemini candidate has an invalid structure.")

    content = candidate.get("content", {})
    if not isinstance(content, dict):
        raise RuntimeError("Gemini returned invalid content.")

    parts = content.get("parts", [])
    if not isinstance(parts, list) or not parts:
        finish_reason = candidate.get("finishReason", "unknown")
        raise RuntimeError(
            f"Gemini returned empty content (finishReason={finish_reason})."
        )

    text = parts[0].get("text", "") if isinstance(parts[0], dict) else ""
    text = str(text).strip()

    if not text:
        raise RuntimeError("Gemini returned empty text.")

    return text


# ============================================================
# JSON RESPONSE PARSER
# ============================================================

def parse_json_response(text):
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            pass

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    raise ValueError("Gemini response was not valid JSON.")


# ============================================================
# STORY
# ============================================================

def load_story():
    paths = [
        Path("output/story/ai_story.json"),
        Path("output/story/story.json"),
    ]

    for path in paths:
        data = read_json(path)
        if data is not None:
            return data

    raise FileNotFoundError("No story file found.")


# ============================================================
# CHARACTER BIBLE
# ============================================================

def load_character_bible():
    path = Path("output/story/character_bible.json")
    data = read_json(path)

    if not isinstance(data, dict):
        return {
            "status": "unavailable",
            "characters": [],
            "world": {},
        }

    characters = data.get("characters", [])
    if not isinstance(characters, list):
        characters = []

    world = data.get("world", {})
    if not isinstance(world, dict):
        world = {}

    return {
        "status": data.get("status", "completed"),
        "characters": characters,
        "world": world,
    }


# ============================================================
# PREVIOUS SCENES
# ============================================================

def load_previous_scene_context(
    existing_scenes,
    part_number,
    scene_number,
):
    previous = []

    for scene in existing_scenes:
        if not isinstance(scene, dict):
            continue

        try:
            part = int(scene.get("part", 0))
            number = int(scene.get("scene", 0))
        except (TypeError, ValueError):
            continue

        if part < part_number or (
            part == part_number and number < scene_number
        ):
            previous.append(scene)

    def sort_key(item):
        try:
            return (
                int(item.get("part", 0)),
                int(item.get("scene", 0)),
            )
        except (TypeError, ValueError):
            return (0, 0)

    previous.sort(key=sort_key)
    return previous[-3:]


# ============================================================
# INPUT SETTINGS
# ============================================================

def build_input_settings(config):
    # normalize_format expects the COMPLETE config dict.
    return {
        "format": normalize_format(config),
        "audience": cfg_text(config, "AUDIENCE", "adult"),
        "story_length": cfg_text(config, "STORY_LENGTH", "auto"),
        "scene_duration": get_scene_duration_value(config),
        "part_hook": cfg_bool(config, "PART_HOOK", True),
        "part_suspense": cfg_bool(config, "PART_SUSPENSE", True),
        "final_resolution": cfg_bool(config, "FINAL_RESOLUTION", True),
        "visual_style": cfg_text(
            config, "VISUAL_STYLE", "cinematic_realistic"
        ),
        "realism": cfg_text(config, "REALISM", "high"),
        "character_bible": cfg_bool(config, "CHARACTER_BIBLE", True),
        "character_consistency": cfg_bool(
            config, "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": cfg_bool(
            config, "WORLD_CONSISTENCY", True
        ),
        "scene_continuity": cfg_bool(config, "SCENE_CONTINUITY", True),
        "cinematic_camera": cfg_bool(config, "CINEMATIC_CAMERA", True),
        "camera_style": cfg_text(config, "CAMERA_STYLE", "cinematic"),
        "lighting": cfg_text(config, "LIGHTING", "cinematic"),
        "mood": cfg_text(config, "MOOD", "dramatic"),
        "quality": cfg_text(config, "QUALITY", "high"),
        "negative_prompt": cfg_bool(config, "NEGATIVE_PROMPT", True),
        "avoid_cartoon": cfg_bool(config, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": cfg_bool(config, "AVOID_NEON", True),
        "avoid_glitch": cfg_bool(config, "AVOID_GLITCH_EFFECTS", True),
        "natural_motion": cfg_bool(config, "NATURAL_MOTION", True),
        "realistic_lighting": cfg_bool(config, "REALISTIC_LIGHTING", True),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),
        "captions": cfg_text(config, "CAPTIONS", "hindi"),
        "music": cfg_bool(config, "MUSIC", True),
        "music_style": cfg_text(config, "MUSIC_STYLE", "cinematic"),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
    }


# ============================================================
# PROMPT
# ============================================================

def build_prompt(
    config,
    settings,
    story,
    character_bible,
    previous_scenes,
    part_number,
    scene_number,
    scenes_per_part,
):
    story_text = json.dumps(story, ensure_ascii=False, indent=2)
    bible_text = json.dumps(character_bible, ensure_ascii=False, indent=2)
    previous_text = json.dumps(previous_scenes, ensure_ascii=False, indent=2)

    topic = resolve_topic_value(config)
    story_source = resolve_story_text_value(config)

    if settings["format"] == "short":
        story_structure = """
This is SHORT-FORM video.

Do NOT write scenes as if they are merely chopped sections from a long video.
The complete short must feel independently structured.

Use:
- immediate hook
- curiosity
- mystery or tension
- escalation
- reveal/payoff
- final memorable beat

Scene 1 must create immediate viewer curiosity.
The final scene must provide payoff or deliberate suspense according to FINAL_RESOLUTION.
""".strip()
    else:
        story_structure = """
This is FULL-LENGTH video.

Maintain a coherent long-form progression.
Do not rush the story simply to fit a short.

Build:
- setup
- development
- conflict
- escalation
- resolution

naturally.
""".strip()

    hook_rule = (
        "PART HOOK is ENABLED."
        if settings["part_hook"]
        else "PART HOOK is DISABLED."
    )
    suspense_rule = (
        "PART SUSPENSE is ENABLED."
        if settings["part_suspense"]
        else "PART SUSPENSE is DISABLED."
    )
    resolution_rule = (
        "FINAL RESOLUTION is ENABLED."
        if settings["final_resolution"]
        else "FINAL RESOLUTION is DISABLED."
    )

    consistency_rules = []

    if settings["character_consistency"]:
        consistency_rules.append(
            "Preserve exact recurring character identity, face, age, hair, "
            "eyes, skin tone, body features and clothing unless the story "
            "explicitly changes them."
        )

    if settings["world_consistency"]:
        consistency_rules.append(
            "Preserve geography, architecture, time period, environment, "
            "weather and world details."
        )

    if settings["scene_continuity"]:
        consistency_rules.append(
            "Continue naturally from previous scenes. Do not randomly "
            "teleport characters or change their positions, clothing or environment."
        )

    consistency_text = "\n".join(f"- {item}" for item in consistency_rules)

    visual_rules = [
        f"Visual style: {settings['visual_style']}",
        f"Realism level: {settings['realism']}",
        f"Quality: {settings['quality']}",
        f"Camera style: {settings['camera_style']}",
        f"Lighting: {settings['lighting']}",
        f"Mood: {settings['mood']}",
    ]

    if settings["cinematic_camera"]:
        visual_rules.append(
            "Use cinematic camera language: shot size, lens feel, camera "
            "movement, composition and depth."
        )

    if settings["natural_motion"]:
        visual_rules.append(
            "Describe natural human, body and environment motion suitable "
            "for image-to-video generation."
        )

    if settings["realistic_lighting"]:
        visual_rules.append("Use physically believable realistic lighting.")

    if settings["avoid_cartoon"]:
        visual_rules.append(
            "Avoid cartoon, anime, comic, illustration or stylized animated appearance."
        )

    if settings["avoid_neon"]:
        visual_rules.append(
            "Avoid unnecessary neon colors and artificial glow."
        )

    if settings["avoid_glitch"]:
        visual_rules.append("Avoid glitch effects.")

    visual_rules_text = "\n".join(f"- {item}" for item in visual_rules)

    audio_rules = [
        f"Caption language/mode: {settings['captions']}",
        f"Music enabled: {settings['music']}",
        f"Music style: {settings['music_style']}",
        f"SFX enabled: {settings['sfx']}",
        f"Ambient sound enabled: {settings['ambient_sound']}",
    ]
    audio_rules_text = "\n".join(f"- {item}" for item in audio_rules)

    negative_items = []

    if settings["avoid_cartoon"]:
        negative_items.extend([
            "cartoon", "anime", "comic", "illustration", "2D animation",
            "3D cartoon", "game character",
        ])

    if settings["avoid_neon"]:
        negative_items.extend([
            "unnecessary neon", "oversaturated artificial glow",
        ])

    if settings["avoid_glitch"]:
        negative_items.extend(["glitch", "digital distortion"])

    negative_items.extend([
        "plastic skin",
        "wax face",
        "deformed hands",
        "extra fingers",
        "duplicate people",
        "incorrect anatomy",
        "floating objects",
        "text artifacts",
        "watermark",
        "logo",
    ])

    negative_prompt = ", ".join(negative_items)
    direct_story = story_source or "(No direct STORY_TEXT supplied.)"
    continuity_text = (
        consistency_text
        if consistency_text
        else "- Follow story continuity naturally."
    )

    return f"""
You are the scene director for a professional AI-generated Hindi video.

Return ONLY valid JSON.

============================================================
PRIMARY STORY SOURCE

TOPIC:
{topic}

USER STORY TEXT:
{direct_story}

The user's STORY_TEXT, when present, is a PRIMARY SOURCE.
Do not replace its meaning with an unrelated invented story.

============================================================
FULL STORY DATA

{story_text}

============================================================
CHARACTER BIBLE + WORLD BIBLE

{bible_text}

============================================================
RECENT SCENE CONTEXT

{previous_text}

============================================================
VIDEO INPUT SETTINGS

Format: {settings["format"]}
Audience: {settings["audience"]}
Story length: {settings["story_length"]}
Scene duration: {settings["scene_duration"]}

{hook_rule}
{suspense_rule}
{resolution_rule}

============================================================
STORY STRUCTURE

{story_structure}

============================================================
VISUAL DIRECTOR SETTINGS

{visual_rules_text}

============================================================
CONTINUITY RULES

{continuity_text}

============================================================
AUDIO / EDITING SETTINGS

{audio_rules_text}

Transitions:
{settings["transitions"]}

============================================================
CURRENT SCENE

Part: {part_number}
Scene: {scene_number}
Scenes in this part: {scenes_per_part}

Create exactly ONE scene.

The scene must belong naturally to this exact position in the story.

If recurring characters appear, identify them using their character_id
from the Character Bible.

Do NOT invent a new version of an existing character.

The visual_prompt must describe:
- real human appearance where humans are present
- exact character identity
- facial appearance
- clothing
- body language
- location
- environment
- time of day
- lighting
- camera framing
- camera movement
- depth
- realistic physical details
- natural motion
- emotional expression
- continuity with previous scene

For image-to-video generation, describe MOTION, not merely a static photograph.

For short-form:
Scene 1 must immediately create curiosity.
Middle scenes must escalate or reveal information.
The final scene must create payoff, twist or suspense, depending on the configured story.

Narration must be Hindi.

============================================================
NEGATIVE VISUAL RULES

{negative_prompt}

============================================================
OUTPUT JSON

Return exactly:

{{
  "part": {part_number},
  "scene": {scene_number},
  "title": "short scene title",
  "character_ids": [],
  "world_context": "specific world/location context",
  "narration": "Hindi narration",
  "visual_prompt": "detailed photorealistic live-action cinematic prompt with natural motion",
  "negative_prompt": "{negative_prompt}",
  "duration": "{settings["scene_duration"]}",
  "transition": "{settings["transitions"]}",
  "camera": "shot size, lens feel and camera movement",
  "lighting": "{settings["lighting"]}",
  "mood": "{settings["mood"]}",
  "sfx": "scene-specific sound effects if appropriate",
  "ambient_sound": "scene-specific ambient sound",
  "music_direction": "music direction based on configured music settings"
}}

Return JSON only.
""".strip()


# ============================================================
# RETRY / BACKOFF
# ============================================================

def extract_retry_after(error):
    try:
        value = error.headers.get("Retry-After")
        if value:
            seconds = float(value)
            if seconds >= 0:
                return seconds
    except Exception:
        pass

    return None


def calculate_backoff(attempt):
    base = INITIAL_BACKOFF * (2 ** max(0, attempt - 1))
    base = min(base, MAX_BACKOFF)

    jitter = random.uniform(0, min(5, base * 0.10))
    return round(base + jitter, 2)


def generate_scene_with_retry(
    api_key,
    model,
    prompt,
    part_number,
    scene_number,
    max_attempts,
):
    last_error = None
    max_attempts = max(1, int(max_attempts))

    for attempt in range(1, max_attempts + 1):
        log(
            f"Generating Part {part_number} Scene {scene_number} "
            f"(attempt {attempt}/{max_attempts}) using model {model}..."
        )

        try:
            response = call_gemini(api_key, model, prompt)
            scene = parse_json_response(response)

            if not isinstance(scene, dict):
                raise ValueError("Scene response must be a JSON object.")

            scene["part"] = part_number
            scene["scene"] = scene_number
            return scene

        except urllib.error.HTTPError as exc:
            last_error = exc

            if exc.code == 429:
                if attempt >= max_attempts:
                    break

                retry_after = extract_retry_after(exc)
                calculated = calculate_backoff(attempt)
                wait_seconds = (
                    max(retry_after, calculated)
                    if retry_after is not None
                    else calculated
                )

                log(f"HTTP 429 rate limit. Waiting {wait_seconds:.1f}s...")
                time.sleep(wait_seconds)
                continue

            if exc.code in {500, 502, 503, 504}:
                if attempt >= max_attempts:
                    break

                wait_seconds = calculate_backoff(attempt)
                log(
                    f"Gemini HTTP {exc.code}. "
                    f"Waiting {wait_seconds:.1f}s..."
                )
                time.sleep(wait_seconds)
                continue

            log(f"Gemini HTTP Error {exc.code}")
            # Authentication, permission, invalid-model and other
            # non-transient HTTP errors are not retried needlessly.
            raise RuntimeError(
                f"Gemini HTTP {exc.code}: {exc.reason}"
            ) from exc

        except Exception as exc:
            last_error = exc
            log(f"Scene generation failed: {exc}")

        if attempt < max_attempts:
            wait_seconds = calculate_backoff(attempt)
            log(f"Waiting {wait_seconds:.1f}s...")
            time.sleep(wait_seconds)

    raise RuntimeError(
        f"Part {part_number} Scene {scene_number} failed after "
        f"{max_attempts} attempts: {last_error}"
    )


# ============================================================
# SCENE VALIDATION
# ============================================================

def validate_scene(scene):
    if not isinstance(scene, dict):
        raise ValueError("Scene must be a JSON object.")

    required = ["part", "scene", "narration", "visual_prompt"]

    for key in required:
        value = scene.get(key)
        if value is None:
            raise ValueError(f"Scene missing required field: {key}")
        if isinstance(value, str) and not value.strip():
            raise ValueError(f"Scene field is empty: {key}")

    character_ids = scene.get("character_ids", [])
    if not isinstance(character_ids, list):
        scene["character_ids"] = []

    return scene


# ============================================================
# MAIN
# ============================================================

def main():
    log("======================================")
    log("       GENERATING STORY SCENES")
    log("======================================")

    config = load_input_config()
    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            f"Invalid scene counts: parts={parts}, "
            f"scenes_per_part={scenes_per_part}"
        )

    total_scenes = parts * scenes_per_part
    settings = build_input_settings(config)
    max_attempts = max(1, int(get_max_retries(config)))

    # Validate model selection before making API requests.
    model = get_model()
    api_key = get_api_key()

    log()
    log("========== INPUT CONFIG ==========")
    log(f"FORMAT             : {settings['format']}")
    log(f"AUDIENCE           : {settings['audience']}")
    log(f"STORY_LENGTH       : {settings['story_length']}")
    log(f"SCENE_DURATION     : {settings['scene_duration']}")
    log(f"PARTS              : {parts}")
    log(f"SCENES PER PART    : {scenes_per_part}")
    log(f"TOTAL SCENES       : {total_scenes}")
    log(f"VISUAL_STYLE       : {settings['visual_style']}")
    log(f"REALISM            : {settings['realism']}")
    log(f"CAMERA_STYLE       : {settings['camera_style']}")
    log(f"LIGHTING           : {settings['lighting']}")
    log(f"MOOD               : {settings['mood']}")
    log(f"CHARACTER_BIBLE    : {settings['character_bible']}")
    log(f"CHARACTER_CONSIST. : {settings['character_consistency']}")
    log(f"WORLD_CONSISTENCY  : {settings['world_consistency']}")
    log(f"SCENE_CONTINUITY   : {settings['scene_continuity']}")
    log(f"NATURAL_MOTION     : {settings['natural_motion']}")
    log(f"MAX_RETRIES        : {max_attempts}")
    log("==================================")
    log()
    log(f"Selected model: {model}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    story = load_story()
    character_bible = load_character_bible()

    if (
        settings["character_bible"]
        and character_bible.get("status") == "unavailable"
    ):
        raise RuntimeError(
            "CHARACTER_BIBLE is enabled but "
            "character_bible.json is unavailable."
        )

    existing_data = read_json(SCENES_FILE)

    if isinstance(existing_data, dict):
        existing_scenes = existing_data.get("scenes", [])
    elif isinstance(existing_data, list):
        existing_scenes = existing_data
    else:
        existing_scenes = []

    if not isinstance(existing_scenes, list):
        existing_scenes = []

    completed = load_checkpoint()

    # Existing scenes are also treated as completed.
    for part_number in range(1, parts + 1):
        for scene_number in range(1, scenes_per_part + 1):
            key = scene_key(part_number, scene_number)

            if scene_already_saved(
                existing_scenes,
                part_number,
                scene_number,
            ):
                completed.add(key)

    valid_keys = {
        scene_key(p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes_per_part + 1)
    }

    completed = {item for item in completed if item in valid_keys}

    log(f"Existing completed scenes: {len(completed)}/{total_scenes}")
    save_checkpoint(total_scenes, completed)

    # ========================================================
    # GENERATE
    # ========================================================

    for part_number in range(1, parts + 1):
        for scene_number in range(1, scenes_per_part + 1):
            key = scene_key(part_number, scene_number)

            if key in completed:
                log(
                    f"Part {part_number} Scene {scene_number} "
                    "already completed. Skipping."
                )
                continue

            previous_scenes = load_previous_scene_context(
                existing_scenes,
                part_number,
                scene_number,
            )

            prompt = build_prompt(
                config,
                settings,
                story,
                character_bible,
                previous_scenes,
                part_number,
                scene_number,
                scenes_per_part,
            )

            try:
                scene = generate_scene_with_retry(
                    api_key,
                    model,
                    prompt,
                    part_number,
                    scene_number,
                    max_attempts,
                )
                scene = validate_scene(scene)

            except Exception as exc:
                save_checkpoint(
                    total_scenes,
                    completed,
                    failed_scene=key,
                )
                log(f"ERROR: {exc}")
                raise

            replaced = False

            for index, old_scene in enumerate(existing_scenes):
                if not isinstance(old_scene, dict):
                    continue

                try:
                    old_part = int(old_scene.get("part", -1))
                    old_number = int(old_scene.get("scene", -1))
                except (TypeError, ValueError):
                    continue

                if old_part == part_number and old_number == scene_number:
                    existing_scenes[index] = scene
                    replaced = True
                    break

            if not replaced:
                existing_scenes.append(scene)

            def sort_key(item):
                try:
                    return (
                        int(item.get("part", 999999)),
                        int(item.get("scene", 999999)),
                    )
                except (TypeError, ValueError, AttributeError):
                    return (999999, 999999)

            existing_scenes.sort(key=sort_key)
            completed.add(key)

            write_json(
                SCENES_FILE,
                {
                    "status": "in_progress",
                    "model": model,
                    "total_scenes": total_scenes,
                    "completed_scenes": len(completed),
                    "input_config": {
                        "format": settings["format"],
                        "audience": settings["audience"],
                        "story_length": settings["story_length"],
                        "scene_duration": settings["scene_duration"],
                        "visual_style": settings["visual_style"],
                        "realism": settings["realism"],
                        "quality": settings["quality"],
                        "camera_style": settings["camera_style"],
                        "lighting": settings["lighting"],
                        "mood": settings["mood"],
                        "character_bible": settings["character_bible"],
                        "character_consistency": settings["character_consistency"],
                        "world_consistency": settings["world_consistency"],
                        "scene_continuity": settings["scene_continuity"],
                        "natural_motion": settings["natural_motion"],
                        "negative_prompt": settings["negative_prompt"],
                        "transitions": settings["transitions"],
                    },
                    "scenes": existing_scenes,
                },
            )

            save_checkpoint(total_scenes, completed)

            log(
                f"Saved Part {part_number} Scene {scene_number} "
                f"({len(completed)}/{total_scenes})"
            )

            if len(completed) < total_scenes:
                time.sleep(REQUEST_DELAY)

    # ========================================================
    # FINAL VALIDATION
    # ========================================================

    expected_keys = {
        scene_key(p, s)
        for p in range(1, parts + 1)
        for s in range(1, scenes_per_part + 1)
    }

    actual_keys = set()

    for scene in existing_scenes:
        if not isinstance(scene, dict):
            continue

        try:
            p = int(scene["part"])
            s = int(scene["scene"])
            key = scene_key(p, s)

            if key in expected_keys:
                validate_scene(scene)
                actual_keys.add(key)
        except Exception:
            pass

    missing = sorted(expected_keys - actual_keys)

    if missing:
        raise RuntimeError(
            "Scene generation incomplete. "
            f"Missing {len(missing)} scenes: {', '.join(missing[:20])}"
        )

    final_data = {
        "status": "completed",
        "model": model,
        "total_scenes": total_scenes,
        "completed_scenes": len(actual_keys),
        "input_config": {
            "format": settings["format"],
            "audience": settings["audience"],
            "story_length": settings["story_length"],
            "scene_duration": settings["scene_duration"],
            "visual_style": settings["visual_style"],
            "realism": settings["realism"],
            "quality": settings["quality"],
            "camera_style": settings["camera_style"],
            "lighting": settings["lighting"],
            "mood": settings["mood"],
            "character_bible": settings["character_bible"],
            "character_consistency": settings["character_consistency"],
            "world_consistency": settings["world_consistency"],
            "scene_continuity": settings["scene_continuity"],
            "natural_motion": settings["natural_motion"],
            "negative_prompt": settings["negative_prompt"],
            "transitions": settings["transitions"],
        },
        "character_bible_status": character_bible.get("status", "unknown"),
        "scenes": existing_scenes,
    }

    write_json(SCENES_FILE, final_data)
    save_checkpoint(total_scenes, actual_keys, failed_scene=None)

    log()
    log("======================================")
    log("       SCENE GENERATION COMPLETE")
    log("======================================")
    log(f"Completed scenes: {len(actual_keys)}/{total_scenes}")


if __name__ == "__main__":
    main()
