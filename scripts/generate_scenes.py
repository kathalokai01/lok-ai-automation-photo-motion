#!/usr/bin/env python3

import hashlib
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
    get_max_retries,
    get_topic,
    get_story_text,
)


OUTPUT_DIR = Path("output/scenes")
SCENES_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = Path("output/checkpoints/scenes_progress.json")

AI_STORY_FILE = Path("output/story/ai_story.json")
STORY_FILE = Path("output/story/story.json")
CHARACTER_BIBLE_FILE = Path("output/story/character_bible.json")
MODEL_FILE = Path("output/config/selected_model.json")

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
REQUEST_TIMEOUT = int(os.getenv("SCENE_REQUEST_TIMEOUT", "150"))
REQUEST_DELAY = max(0.0, float(os.getenv("SCENE_REQUEST_DELAY", "2")))
INITIAL_BACKOFF = max(1, int(os.getenv("SCENE_INITIAL_BACKOFF", "15")))
MAX_BACKOFF = max(INITIAL_BACKOFF, int(os.getenv("SCENE_MAX_BACKOFF", "180")))
MAX_RESPONSE_CHARS = 50000


# ============================================================
# LOGGING / CONFIG
# ============================================================

def log(message=""):
    print(message, flush=True)


def cfg_text(config, key, default=""):
    value = config.get(key, default)

    if value is None:
        return str(default)

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


def build_settings(config):
    return {
        "format": normalize_format(config),
        "audience": cfg_text(config, "AUDIENCE", "adult"),
        "story_length": cfg_text(config, "STORY_LENGTH", "auto"),
        "scene_duration": cfg_text(config, "SCENE_DURATION", "auto"),

        "part_hook": cfg_bool(config, "PART_HOOK", True),
        "part_suspense": cfg_bool(config, "PART_SUSPENSE", True),
        "final_resolution": cfg_bool(config, "FINAL_RESOLUTION", True),

        "visual_style": cfg_text(
            config, "VISUAL_STYLE", "cinematic_realistic"
        ),
        "realism": cfg_text(config, "REALISM", "high"),
        "quality": cfg_text(config, "QUALITY", "high"),
        "camera_style": cfg_text(config, "CAMERA_STYLE", "cinematic"),
        "cinematic_camera": cfg_bool(config, "CINEMATIC_CAMERA", True),
        "lighting": cfg_text(config, "LIGHTING", "cinematic"),
        "realistic_lighting": cfg_bool(config, "REALISTIC_LIGHTING", True),
        "mood": cfg_text(config, "MOOD", "dramatic"),
        "natural_motion": cfg_bool(config, "NATURAL_MOTION", True),

        "character_bible": cfg_bool(config, "CHARACTER_BIBLE", True),
        "character_consistency": cfg_bool(
            config, "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": cfg_bool(
            config, "WORLD_CONSISTENCY", True
        ),
        "scene_continuity": cfg_bool(
            config, "SCENE_CONTINUITY", True
        ),

        "negative_prompt": cfg_bool(config, "NEGATIVE_PROMPT", True),
        "avoid_cartoon": cfg_bool(config, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": cfg_bool(config, "AVOID_NEON", True),
        "avoid_glitch": cfg_bool(config, "AVOID_GLITCH_EFFECTS", True),

        "captions": cfg_text(config, "CAPTIONS", "hindi"),
        "music": cfg_bool(config, "MUSIC", True),
        "music_style": cfg_text(config, "MUSIC_STYLE", "cinematic"),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),

        "max_retries": max(1, get_max_retries(config)),
        "resume_enabled": cfg_bool(config, "RESUME_ENABLED", True),
        "skip_completed": cfg_bool(config, "SKIP_COMPLETED_SCENES", True),
        "save_checkpoint": cfg_bool(
            config, "SAVE_CHECKPOINT_AFTER_EACH_SCENE", True
        ),
    }


# ============================================================
# JSON / ATOMIC STORAGE
# ============================================================

def read_json(path, required=False):
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Required file not found: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, (dict, list)):
        raise RuntimeError(f"Unexpected JSON root type in {path}")

    return data


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")

    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp, path)


def stable_hash(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(raw).hexdigest()


# ============================================================
# SELECTED MODEL — DO NOT SUBSTITUTE AN UNCONFIGURED MODEL
# ============================================================

def get_selected_model():
    data = read_json(MODEL_FILE, required=True)

    if not isinstance(data, dict):
        raise RuntimeError("selected_model.json must contain an object.")

    if data.get("status") != "selected":
        raise RuntimeError(
            "Gemini model status is not 'selected'. "
            "Run the configured model selection/test steps first."
        )

    model = str(data.get("model", "")).strip()
    model = model.removeprefix("models/")

    if not model:
        raise RuntimeError("No selected Gemini model is recorded.")

    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid selected Gemini model identifier: {model}")

    return model


def get_api_key():
    key = os.getenv("GEMINI_API_KEY", "").strip()

    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    return key


# ============================================================
# SOURCE STORY AND EXACT SCENE PLAN
# ============================================================

def load_story_sources():
    ai_story = read_json(AI_STORY_FILE, required=True)

    if not isinstance(ai_story, dict):
        raise RuntimeError("ai_story.json must contain an object.")

    if ai_story.get("status") != "completed":
        raise RuntimeError(
            "AI story is not completed. "
            "Scene generation will not use an incomplete story."
        )

    if ai_story.get("format") not in {"short", "full"}:
        raise RuntimeError(
            f"Invalid AI story format: {ai_story.get('format')!r}"
        )

    source_story = read_json(STORY_FILE, required=False)

    if not isinstance(source_story, dict):
        source_story = {}

    return ai_story, source_story


def flatten_story_scenes(ai_story):
    parts = ai_story.get("parts")

    if not isinstance(parts, list) or not parts:
        raise RuntimeError("AI story contains no parts.")

    flattened = []
    part_numbers = []

    for expected_part, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid part object at index {expected_part}.")

        part_number = part.get("part")

        if part_number != expected_part:
            raise RuntimeError(
                f"Part order mismatch: expected {expected_part}, "
                f"received {part_number}."
            )

        part_numbers.append(part_number)

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {part_number} contains no scenes.")

        for expected_scene, source_scene in enumerate(scenes, start=1):
            if not isinstance(source_scene, dict):
                raise RuntimeError(
                    f"Invalid scene in part {part_number}, "
                    f"scene index {expected_scene}."
                )

            scene_number = source_scene.get("scene")

            if scene_number != expected_scene:
                raise RuntimeError(
                    f"Scene order mismatch in part {part_number}: "
                    f"expected {expected_scene}, received {scene_number}."
                )

            narration = source_scene.get("narration", "")

            if not isinstance(narration, str) or not narration.strip():
                raise RuntimeError(
                    f"AI story narration is empty: "
                    f"Part {part_number}, Scene {scene_number}."
                )

            flattened.append({
                "part": part_number,
                "scene": scene_number,
                "role": source_scene.get("role", ""),
                "narration": narration.strip(),
                "dialogue": str(source_scene.get("dialogue", "")).strip(),
                "source_visual_prompt": str(
                    source_scene.get("visual_prompt", "")
                ).strip(),
                "source_negative_prompt": str(
                    source_scene.get("negative_prompt", "")
                ).strip(),
                "source_duration": source_scene.get(
                    "scene_duration",
                    source_scene.get("duration", ai_story.get("scene_duration", "auto")),
                ),
                "source_scene": source_scene,
                "part_hook": part.get("hook_required", False),
                "part_suspense": part.get("suspense_required", False),
                "final_resolution": part.get("final_resolution", False),
                "part_title": part.get("title", ""),
            })

    if len(flattened) != len({
        (item["part"], item["scene"]) for item in flattened
    }):
        raise RuntimeError("Duplicate part/scene identifiers found in AI story.")

    declared_total = ai_story.get("total_scenes")

    if declared_total is not None and declared_total != len(flattened):
        raise RuntimeError(
            f"AI story total_scenes mismatch: "
            f"{declared_total}/{len(flattened)}."
        )

    return flattened


def load_character_bible():
    data = read_json(CHARACTER_BIBLE_FILE, required=False)

    if not isinstance(data, dict):
        if cfg_bool(
            load_input_config(),
            "CHARACTER_BIBLE",
            True,
        ):
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but character_bible.json is missing."
            )

        return {
            "status": "unavailable",
            "characters": [],
            "world": {},
        }

    characters = data.get("characters", [])
    world = data.get("world", {})

    if not isinstance(characters, list):
        characters = []

    if not isinstance(world, dict):
        world = {}

    return {
        "status": data.get("status", "unknown"),
        "characters": characters,
        "world": world,
    }


# ============================================================
# EXISTING OUTPUT / RESUME
# ============================================================

def scene_key(part, scene):
    return f"{int(part)}:{int(scene)}"


def scene_is_valid(scene, expected):
    if not isinstance(scene, dict):
        return False

    if scene.get("part") != expected["part"]:
        return False

    if scene.get("scene") != expected["scene"]:
        return False

    if scene.get("status") != "completed":
        return False

    for field in ("narration", "visual_prompt"):
        value = scene.get(field)

        if not isinstance(value, str) or not value.strip():
            return False

    return True


def load_existing_scenes(signature, settings):
    if not settings["resume_enabled"]:
        return {}

    data = read_json(SCENES_FILE, required=False)

    if not isinstance(data, dict):
        return {}

    if data.get("generation_signature") != signature:
        log("Existing scene output belongs to different input/settings; not reused.")
        return {}

    items = data.get("scenes", [])

    if not isinstance(items, list):
        return {}

    expected_map = {}

    for item in items:
        if not isinstance(item, dict):
            continue

        try:
            key = scene_key(item.get("part"), item.get("scene"))
        except (TypeError, ValueError):
            continue

        expected_map[key] = item

    return expected_map


def load_checkpoint(signature, settings):
    if not settings["resume_enabled"]:
        return set()

    data = read_json(CHECKPOINT_FILE, required=False)

    if not isinstance(data, dict):
        return set()

    if data.get("generation_signature") != signature:
        return set()

    values = data.get("completed_scenes", [])

    if not isinstance(values, list):
        return set()

    return {
        value
        for value in values
        if isinstance(value, str)
    }


def save_checkpoint(signature, scenes, total, failed_scene=None):
    completed = sorted(scenes)

    write_json(
        CHECKPOINT_FILE,
        {
            "status": "completed" if len(completed) == total else "in_progress",
            "generation_signature": signature,
            "total_scenes": total,
            "completed_scenes": completed,
            "completed_count": len(completed),
            "remaining_scenes": max(0, total - len(completed)),
            "failed_scene": failed_scene,
            "updated_at": int(time.time()),
        },
    )


# ============================================================
# GEMINI RESPONSE SCHEMA
# ============================================================

def response_schema():
    return {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "character_ids": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "world_context": {"type": "STRING"},
            "narration": {"type": "STRING"},
            "visual_prompt": {"type": "STRING"},
            "negative_prompt": {"type": "STRING"},
            "duration": {"type": "STRING"},
            "transition": {"type": "STRING"},
            "camera": {"type": "STRING"},
            "lighting": {"type": "STRING"},
            "mood": {"type": "STRING"},
            "sfx": {"type": "STRING"},
            "ambient_sound": {"type": "STRING"},
            "music_direction": {"type": "STRING"},
        },
        "required": [
            "title",
            "character_ids",
            "world_context",
            "narration",
            "visual_prompt",
            "negative_prompt",
            "duration",
            "transition",
            "camera",
            "lighting",
            "mood",
            "sfx",
            "ambient_sound",
            "music_direction",
        ],
    }


# ============================================================
# GEMINI REQUEST / RESPONSE PARSING
# ============================================================

def extract_retry_after(error):
    try:
        value = error.headers.get("Retry-After")

        if value:
            return max(0.0, float(value))
    except Exception:
        pass

    return None


def backoff_seconds(attempt):
    base = min(
        MAX_BACKOFF,
        INITIAL_BACKOFF * (2 ** max(0, attempt - 1)),
    )

    return round(
        base + random.uniform(0, min(5, base * 0.1)),
        2,
    )


def call_gemini(api_key, model, prompt):
    url = f"{API_BASE}/models/{model}:generateContent"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.65,
            "topP": 0.9,
            "responseMimeType": "application/json",
            "responseSchema": response_schema(),
        },
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        raw = response.read().decode("utf-8")

    result = json.loads(raw)
    candidates = result.get("candidates", [])

    if not candidates:
        raise ValueError(
            f"Gemini returned no candidates: "
            f"{result.get('promptFeedback', {})}"
        )

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason", "")

    if finish_reason == "SAFETY":
        raise ValueError("Gemini blocked the scene response for safety.")

    parts = candidate.get("content", {}).get("parts", [])

    text_parts = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
    ]

    text = "\n".join(text_parts).strip()

    if not text:
        raise ValueError(
            f"Gemini returned empty scene content. "
            f"Finish reason: {finish_reason}"
        )

    if len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("Gemini scene response exceeded size limit.")

    return text


def parse_json_response(text):
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start < 0 or end <= start:
            raise ValueError("Gemini response does not contain a JSON object.")

        result = json.loads(cleaned[start:end + 1])

    if not isinstance(result, dict):
        raise ValueError("Gemini scene response must be a JSON object.")

    return result

# ============================================================
# SCENE PROMPT
# ============================================================

def build_prompt(
    expected,
    settings,
    ai_story,
    character_bible,
    previous_scenes,
    total_parts,
):
    source_scene = expected["source_scene"]

    source_narration = expected["narration"]
    source_dialogue = expected["dialogue"]
    source_visual = expected["source_visual_prompt"]

    part_number = expected["part"]
    scene_number = expected["scene"]

    if settings["format"] == "short":
        format_rules = """
SHORT-FORM VIDEO:
- Scene 1 must create immediate visual curiosity.
- Keep visual action engaging from the first frame.
- Middle scenes must increase tension or reveal information.
- The final scene must deliver a payoff, twist or deliberate suspense.
- Do not turn the scene into a static portrait.
"""
    else:
        format_rules = """
FULL-LENGTH VIDEO:
- Preserve cinematic pacing and story progression.
- Use meaningful action rather than decorative filler.
- Build visual tension where the story requires it.
- Do not rush the scene or invent unrelated events.
"""

    if part_number == 1 and scene_number == 1 and settings["part_hook"]:
        hook_rule = (
            "Opening hook is enabled: the visual must immediately "
            "communicate a compelling question, danger or unusual event."
        )
    elif scene_number == 1 and settings["part_hook"]:
        hook_rule = (
            "Part opening hook is enabled: connect to previous events "
            "while creating a fresh reason to continue watching."
        )
    else:
        hook_rule = "Do not add an unrelated opening hook."

    if (
        part_number < total_parts
        and settings["part_suspense"]
        and scene_number == max(
            item["scene"]
            for item in [expected]
        )
    ):
        suspense_rule = (
            "Maintain the part's suspense direction. "
            "Do not prematurely resolve the central mystery."
        )
    else:
        suspense_rule = (
            "Follow the existing story's tension and reveal timing."
        )

    if part_number == total_parts and settings["final_resolution"]:
        ending_rule = (
            "This is the final part: respect the planned resolution "
            "and do not introduce an unrelated cliffhanger."
        )
    else:
        ending_rule = (
            "Preserve the planned continuation and unresolved story threads."
        )

    character_rules = []

    if settings["character_consistency"]:
        character_rules.append(
            "Keep recurring characters' faces, age, hairstyle, "
            "skin tone, body shape and clothing consistent."
        )

    if settings["world_consistency"]:
        character_rules.append(
            "Keep the same location, architecture, period, weather "
            "and environmental details unless the story changes them."
        )

    if settings["scene_continuity"]:
        character_rules.append(
            "Respect the previous scene's character positions, "
            "actions, direction of movement and continuity."
        )

    continuity = "\n".join(
        f"- {rule}" for rule in character_rules
    ) or "- Follow the story's established continuity."

    visual_rules = [
        f"Visual style: {settings['visual_style']}",
        f"Realism: {settings['realism']}",
        f"Quality: {settings['quality']}",
        f"Camera style: {settings['camera_style']}",
        f"Lighting: {settings['lighting']}",
        f"Mood: {settings['mood']}",
    ]

    if settings["cinematic_camera"]:
        visual_rules.append(
            "Specify shot size, perspective, lens character, "
            "composition and purposeful camera movement."
        )

    if settings["natural_motion"]:
        visual_rules.append(
            "Describe visible physical motion: body movement, "
            "facial expression, clothing, hair, environmental motion "
            "and camera movement where appropriate."
        )

    if settings["realistic_lighting"]:
        visual_rules.append(
            "Use physically believable light direction, shadows, "
            "exposure and natural skin texture."
        )

    if settings["avoid_cartoon"]:
        visual_rules.append(
            "Avoid cartoon, anime, comic, illustration, "
            "game-art and animated appearances."
        )

    if settings["avoid_neon"]:
        visual_rules.append(
            "Avoid unnecessary neon colors and artificial glow."
        )

    if settings["avoid_glitch"]:
        visual_rules.append(
            "Avoid glitch, digital distortion and artificial artifacts."
        )

    visual_settings = "\n".join(
        f"- {rule}" for rule in visual_rules
    )

    negative_terms = []

    if settings["negative_prompt"]:
        negative_terms.extend([
            "cartoon",
            "anime",
            "comic",
            "illustration",
            "plastic skin",
            "waxy face",
            "deformed anatomy",
            "extra fingers",
            "duplicate people",
            "floating objects",
            "text artifacts",
            "watermark",
            "logo",
        ])

        if settings["avoid_neon"]:
            negative_terms.extend([
                "unnecessary neon",
                "artificial glow",
            ])

        if settings["avoid_glitch"]:
            negative_terms.extend([
                "glitch",
                "digital distortion",
            ])

    if expected["source_negative_prompt"]:
        negative_terms.append(expected["source_negative_prompt"])

    negative_prompt = ", ".join(dict.fromkeys(negative_terms))

    if settings["music"]:
        music_instruction = (
            f"Music direction: {settings['music_style']}; "
            "match the scene's emotional intensity."
        )
    else:
        music_instruction = "Music direction: none; music is disabled."

    if settings["sfx"]:
        sfx_instruction = (
            "Provide only relevant, scene-specific sound effects."
        )
    else:
        sfx_instruction = "Sound effects are disabled; return an empty string."

    if settings["ambient_sound"]:
        ambient_instruction = (
            "Provide realistic location-specific ambient sound."
        )
    else:
        ambient_instruction = (
            "Ambient sound is disabled; return an empty string."
        )

    return f"""
You are a professional live-action film scene director
and visual continuity supervisor.

Create visual-generation instructions for ONE existing story scene.

Do not rewrite the story.
Do not change its events, characters or meaning.
Do not add unrelated characters or locations.

FORMAT:
{settings["format"]}

AUDIENCE:
{settings["audience"]}

PART:
{part_number} of {total_parts}

SCENE:
{scene_number}

SCENE ROLE:
{expected["role"]}

PART TITLE:
{expected["part_title"]}

ORIGINAL SCENE DATA:
{json.dumps(source_scene, ensure_ascii=False, indent=2)}

EXACT NARRATION — PRESERVE VERBATIM:
{source_narration}

EXACT DIALOGUE:
{source_dialogue}

EXISTING VISUAL DIRECTION:
{source_visual or "Create a visual direction faithful to the narration."}

CHARACTER BIBLE:
{json.dumps(character_bible, ensure_ascii=False, indent=2)}

PREVIOUS SCENE CONTEXT:
{json.dumps(previous_scenes, ensure_ascii=False, indent=2)}

STORY CONTEXT:
{json.dumps({
    "title": ai_story.get("title", ""),
    "topic": ai_story.get("topic", ""),
    "format": ai_story.get("format", ""),
    "story_text": ai_story.get("story_text", ""),
}, ensure_ascii=False, indent=2)}

STORY STRUCTURE:
{format_rules}

HOOK:
{hook_rule}

SUSPENSE:
{suspense_rule}

ENDING:
{ending_rule}

VISUAL DIRECTION:
{visual_settings}

CONTINUITY:
{continuity}

MOTION REQUIREMENTS:
- Create a realistic live-action cinematic shot.
- Describe what the characters physically do during the shot.
- Include natural body movement and believable expressions.
- Include environmental movement when relevant.
- Use purposeful camera movement where it improves the scene.
- Do not rely only on zoom-in, zoom-out or a static photograph.
- Avoid impossible movement and inconsistent character positions.
- Make the prompt usable for image generation and image-to-video animation.
- Keep the scene visually faithful to its narration.
- Do not insert subtitles, captions, written text or logos into the image.

AUDIO DIRECTION:
{music_instruction}
{sfx_instruction}
{ambient_instruction}

NEGATIVE PROMPT:
{negative_prompt}

OUTPUT:
Return one JSON object with these exact fields:
{{
  "title": "short scene title",
  "character_ids": [],
  "world_context": "location and world continuity details",
  "narration": "exact original narration copied without changes",
  "visual_prompt": "complete photorealistic live-action visual and motion prompt",
  "negative_prompt": "negative visual prompt",
  "duration": "configured scene duration",
  "transition": "configured transition",
  "camera": "shot size, lens character and camera movement",
  "lighting": "scene-specific lighting",
  "mood": "scene-specific emotional mood",
  "sfx": "relevant sound effects or empty string",
  "ambient_sound": "ambient sound or empty string",
  "music_direction": "music direction or empty string"
}}

Return valid JSON only. No markdown or explanation.
""".strip()


# ============================================================
# GENERATED SCENE VALIDATION
# ============================================================

def validate_scene(result, expected, settings):
    if not isinstance(result, dict):
        raise ValueError("Generated scene must be a JSON object.")

    required_text = (
        "title",
        "world_context",
        "narration",
        "visual_prompt",
        "negative_prompt",
        "duration",
        "transition",
        "camera",
        "lighting",
        "mood",
        "sfx",
        "ambient_sound",
        "music_direction",
    )

    for field in required_text:
        value = result.get(field)

        if not isinstance(value, str):
            raise ValueError(
                f"Generated scene field '{field}' must be text."
            )

    for field in (
        "title",
        "world_context",
        "narration",
        "visual_prompt",
        "camera",
        "lighting",
    ):
        if not result[field].strip():
            raise ValueError(
                f"Generated scene field '{field}' is empty."
            )

    # Narration must remain exactly as approved in ai_story.json.
    if result["narration"].strip() != expected["narration"].strip():
        raise ValueError(
            f"Narration changed during visual scene generation: "
            f"Part {expected['part']} Scene {expected['scene']}."
        )

    character_ids = result.get("character_ids", [])

    if not isinstance(character_ids, list):
        raise ValueError("character_ids must be a JSON array.")

    character_ids = [
        str(item).strip()
        for item in character_ids
        if str(item).strip()
    ]

    duration = result["duration"].strip()

    if not duration:
        duration = str(expected["source_duration"] or "auto")

    if not settings["music"]:
        result["music_direction"] = ""

    if not settings["sfx"]:
        result["sfx"] = ""

    if not settings["ambient_sound"]:
        result["ambient_sound"] = ""

    return {
        "part": expected["part"],
        "scene": expected["scene"],
        "title": result["title"].strip(),
        "character_ids": character_ids,
        "world_context": result["world_context"].strip(),
        "role": expected["role"],
        "narration": expected["narration"],
        "dialogue": expected["dialogue"],
        "visual_prompt": result["visual_prompt"].strip(),
        "negative_prompt": result["negative_prompt"].strip(),
        "duration": duration,
        "transition": result["transition"].strip(),
        "camera": result["camera"].strip(),
        "lighting": result["lighting"].strip(),
        "mood": result["mood"].strip(),
        "sfx": result["sfx"].strip(),
        "ambient_sound": result["ambient_sound"].strip(),
        "music_direction": result["music_direction"].strip(),
        "status": "completed",
    }


# ============================================================
# RETRIES — SAME CONFIGURED MODEL ONLY
# ============================================================

def generate_scene(
    api_key,
    model,
    prompt,
    expected,
    settings,
):
    last_error = None

    for attempt in range(1, settings["max_retries"] + 1):
        log(
            f"Part {expected['part']} Scene {expected['scene']} "
            f"(attempt {attempt}/{settings['max_retries']}) "
            f"using selected model {model}"
        )

        try:
            response = call_gemini(
                api_key,
                model,
                prompt,
            )

            parsed = parse_json_response(response)

            return validate_scene(
                parsed,
                expected,
                settings,
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            last_error = f"HTTP {exc.code}: {body[:1500]}"

            retryable = exc.code in {
                408,
                425,
                429,
                500,
                502,
                503,
                504,
            }

            log(f"Gemini request failed: {last_error}")

            if not retryable:
                break

            retry_after = extract_retry_after(exc)
            wait = backoff_seconds(attempt)

            if retry_after is not None:
                wait = max(wait, retry_after)

        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            last_error = str(exc)
            wait = backoff_seconds(attempt)
            log(f"Scene generation error: {last_error}")

        except Exception as exc:
            last_error = str(exc)
            wait = backoff_seconds(attempt)
            log(f"Unexpected scene error: {last_error}")

        if attempt < settings["max_retries"]:
            log(f"Retrying same selected model after {wait:.1f}s.")
            time.sleep(wait)

    raise RuntimeError(
        f"Part {expected['part']} Scene {expected['scene']} failed "
        f"after {settings['max_retries']} attempts: {last_error}"
    )


# ============================================================
# SORT / OUTPUT VALIDATION
# ============================================================

def sort_scene_key(scene):
    return (
        int(scene.get("part", 0)),
        int(scene.get("scene", 0)),
    )


def validate_final_scenes(scenes, expected_scenes):
    expected_keys = {
        scene_key(item["part"], item["scene"])
        for item in expected_scenes
    }

    actual_keys = set()

    for scene in scenes:
        if not isinstance(scene, dict):
            raise RuntimeError("Output contains a non-object scene.")

        key = scene_key(
            scene.get("part"),
            scene.get("scene"),
        )

        if key in actual_keys:
            raise RuntimeError(f"Duplicate scene in final output: {key}")

        actual_keys.add(key)

        if key not in expected_keys:
            raise RuntimeError(f"Unexpected scene in final output: {key}")

        if scene.get("status") != "completed":
            raise RuntimeError(f"Scene is not completed: {key}")

        if not str(scene.get("narration", "")).strip():
            raise RuntimeError(f"Empty narration in scene: {key}")

        if not str(scene.get("visual_prompt", "")).strip():
            raise RuntimeError(f"Empty visual prompt in scene: {key}")

    missing = sorted(expected_keys - actual_keys)

    if missing:
        raise RuntimeError(
            f"Missing {len(missing)} scenes: {', '.join(missing[:30])}"
        )

    if len(scenes) != len(expected_scenes):
        raise RuntimeError(
            f"Final scene count mismatch: "
            f"{len(scenes)}/{len(expected_scenes)}"
        )


# ============================================================
# MAIN
# ============================================================

def main():
    log("=" * 58)
    log("KATHA LOK AI — SCENE GENERATION")
    log("=" * 58)

    config = load_input_config()
    settings = build_settings(config)

    ai_story, source_story = load_story_sources()
    expected_scenes = flatten_story_scenes(ai_story)

    if not expected_scenes:
        raise RuntimeError("No valid scenes found in ai_story.json.")

    # The generated story is the source of truth for scene count.
    # Input PARTS/SCENES cannot silently create extra scenes.
    total_parts = len(ai_story["parts"])
    total_scenes = len(expected_scenes)

    if ai_story.get("format") != settings["format"]:
        raise RuntimeError(
            "FORMAT mismatch between Input/topic.txt and ai_story.json: "
            f"{settings['format']} != {ai_story.get('format')}"
        )

    character_bible = read_json(
        CHARACTER_BIBLE_FILE,
        required=False,
    )

    if not isinstance(character_bible, dict):
        if settings["character_bible"]:
            raise RuntimeError(
                "CHARACTER_BIBLE is enabled but character_bible.json "
                "is missing or invalid."
            )

        character_bible = {
            "status": "unavailable",
            "characters": [],
            "world": {},
        }

    if not isinstance(character_bible.get("characters", []), list):
        raise RuntimeError("Character Bible characters must be an array.")

    if not isinstance(character_bible.get("world", {}), dict):
        raise RuntimeError("Character Bible world must be an object.")

    model = get_selected_model()
    api_key = get_api_key()

    topic = get_topic(config) or str(
        ai_story.get("topic", "")
    ).strip()

    story_text = get_story_text(config) or str(
        ai_story.get("story_text", "")
    ).strip()

    signature = stable_hash({
        "ai_story": ai_story,
        "character_bible": character_bible,
        "settings": settings,
        "model": model,
        "topic": topic,
        "story_text": story_text,
    })

    log(f"Selected model : {model}")
    log(f"Format         : {settings['format']}")
    log(f"Parts          : {total_parts}")
    log(f"Total scenes   : {total_scenes}")
    log(f"Visual style   : {settings['visual_style']}")
    log(f"Realism        : {settings['realism']}")
    log(f"Resume         : {settings['resume_enabled']}")
    log("=" * 58)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing_map = load_existing_scenes(
        signature,
        settings,
    )

    checkpoint = load_checkpoint(
        signature,
        settings,
    )

    expected_map = {
        scene_key(item["part"], item["scene"]): item
        for item in expected_scenes
    }

    completed = {}

    for key, expected in expected_map.items():
        cached = existing_map.get(key)

        if (
            settings["skip_completed"]
            and key in checkpoint
            and scene_is_valid(cached, expected)
        ):
            completed[key] = cached

    # Also reuse valid scenes from a matching output even if
    # an older checkpoint was interrupted before its last write.
    for key, expected in expected_map.items():
        cached = existing_map.get(key)

        if (
            settings["skip_completed"]
            and key not in completed
            and scene_is_valid(cached, expected)
        ):
            completed[key] = cached

    log(f"Reusable completed scenes: {len(completed)}/{total_scenes}")

    output = {
        "status": "in_progress",
        "model": model,
        "format": settings["format"],
        "topic": topic,
        "generation_signature": signature,
        "total_parts": total_parts,
        "total_scenes": total_scenes,
        "completed_scenes": len(completed),
        "input_config": settings,
        "character_bible_status": character_bible.get(
            "status", "unknown"
        ),
        "scenes": sorted(
            completed.values(),
            key=sort_scene_key,
        ),
    }

    write_json(SCENES_FILE, output)
    save_checkpoint(
        signature,
        completed.keys(),
        total_scenes,
    )

    for index, expected in enumerate(expected_scenes, start=1):
        key = scene_key(
            expected["part"],
            expected["scene"],
        )

        if key in completed:
            log(
                f"[{index}/{total_scenes}] "
                f"Part {expected['part']} Scene {expected['scene']} "
                "already completed; skipping API call."
            )
            continue

        previous_scenes = [
            item
            for item in completed.values()
            if (
                item["part"] < expected["part"]
                or (
                    item["part"] == expected["part"]
                    and item["scene"] < expected["scene"]
                )
            )
        ]

        previous_scenes.sort(key=sort_scene_key)
        previous_scenes = previous_scenes[-3:]

        prompt = build_prompt(
            expected,
            settings,
            ai_story,
            character_bible,
            previous_scenes,
            total_parts,
        )

        try:
            scene = generate_scene(
                api_key,
                model,
                prompt,
                expected,
                settings,
            )
        except Exception as exc:
            save_checkpoint(
                signature,
                completed.keys(),
                total_scenes,
                failed_scene=key,
            )
            log(f"ERROR: {exc}")
            raise

        completed[key] = scene

        ordered_scenes = sorted(
            completed.values(),
            key=sort_scene_key,
        )

        output.update({
            "status": "in_progress",
            "model": model,
            "format": settings["format"],
            "topic": topic,
            "generation_signature": signature,
            "total_parts": total_parts,
            "total_scenes": total_scenes,
            "completed_scenes": len(ordered_scenes),
            "input_config": settings,
            "character_bible_status": character_bible.get(
                "status", "unknown"
            ),
            "scenes": ordered_scenes,
        })

        # Save scene output first, then checkpoint.
        # A crash between these writes still permits safe reuse.
        write_json(SCENES_FILE, output)

        if settings["save_checkpoint"]:
            save_checkpoint(
                signature,
                completed.keys(),
                total_scenes,
            )

        log(
            f"Saved Part {expected['part']} "
            f"Scene {expected['scene']} "
            f"({len(completed)}/{total_scenes})"
        )

        if len(completed) < total_scenes and REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)

    final_scenes = sorted(
        completed.values(),
        key=sort_scene_key,
    )

    validate_final_scenes(
        final_scenes,
        expected_scenes,
    )

    output.update({
        "status": "completed",
        "model": model,
        "format": settings["format"],
        "topic": topic,
        "generation_signature": signature,
        "total_parts": total_parts,
        "total_scenes": total_scenes,
        "completed_scenes": len(final_scenes),
        "input_config": settings,
        "character_bible_status": character_bible.get(
            "status", "unknown"
        ),
        "scenes": final_scenes,
        "completed_at": int(time.time()),
    })

    write_json(SCENES_FILE, output)

    save_checkpoint(
        signature,
        completed.keys(),
        total_scenes,
    )

    log("=" * 58)
    log("SCENE GENERATION COMPLETED")
    log(f"Model          : {model}")
    log(f"Parts          : {total_parts}")
    log(f"Scenes         : {len(final_scenes)}/{total_scenes}")
    log(f"Output         : {SCENES_FILE}")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(
            "ERROR: Interrupted. Completed scene checkpoints are preserved."
        )
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}")