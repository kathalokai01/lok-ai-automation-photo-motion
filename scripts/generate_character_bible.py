
#!/usr/bin/env python3
"""Generate a validated character and world bible for photo-motion scenes."""

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
INPUT = ROOT / "output" / "story" / "ai_story.json"
OUTPUT = ROOT / "output" / "story" / "character_bible.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"


def log(message):
    print(message, flush=True)


def read_json(path, required=True):
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
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def as_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def as_bool(config, key, default=True):
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
        raise RuntimeError("A selected Gemini model is required.")

    model = str(data.get("model", "")).strip()

    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid selected model ID: {model!r}")

    return model


def build_config(source):
    return {
        "format": normalize_format(source),
        "audience": as_text(source, "AUDIENCE", "adult"),
        "character_bible": as_bool(source, "CHARACTER_BIBLE", True),
        "character_consistency": as_bool(
            source, "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": as_bool(source, "WORLD_CONSISTENCY", True),
        "scene_continuity": as_bool(source, "SCENE_CONTINUITY", True),
        "visual_style": as_text(
            source, "VISUAL_STYLE", "cinematic_realistic"
        ),
        "realism": as_text(source, "REALISM", "high"),
        "quality": as_text(source, "QUALITY", "high"),
        "camera_style": as_text(source, "CAMERA_STYLE", "cinematic"),
        "lighting": as_text(source, "LIGHTING", "cinematic"),
        "realistic_lighting": as_bool(source, "REALISTIC_LIGHTING", True),
        "mood": as_text(source, "MOOD", "dramatic"),
        "natural_motion": as_bool(source, "NATURAL_MOTION", True),
        "negative_prompt": as_bool(source, "NEGATIVE_PROMPT", True),
        "avoid_cartoon": as_bool(source, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": as_bool(source, "AVOID_NEON", True),
        "avoid_glitch": as_bool(source, "AVOID_GLITCH_EFFECTS", True),
        "max_retries": max(1, int(get_max_retries(source))),
    }


def source_fingerprint(story, topic, story_text, config, model):
    data = {
        "version": 2,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "config": config,
        "model": model,
    }
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_story_context(story):
    context = []

    for part in story.get("parts", []):
        if not isinstance(part, dict):
            continue

        for scene in part.get("scenes", []):
            if not isinstance(scene, dict):
                continue

            context.append({
                "part": part.get("part"),
                "scene": scene.get("scene"),
                "role": scene.get("role", scene.get("purpose", "")),
                "narration": scene.get("narration", ""),
                "dialogue": scene.get("dialogue", ""),
                "visual_prompt": scene.get(
                    "visual_prompt", scene.get("visual", "")
                ),
            })

    if not context:
        raise RuntimeError("The AI story contains no usable scenes.")

    return context


STRING = {"type": "STRING"}
STRING_ARRAY = {
    "type": "ARRAY",
    "items": {"type": "STRING"},
}


def character_schema():
    character_properties = {
        "character_id": STRING,
        "name": STRING,
        "role": STRING,
        "importance": STRING,
        "age": STRING,
        "gender": STRING,
        "personality": STRING,
        "appearance": STRING,
        "face_features": STRING,
        "skin_tone": STRING,
        "hair": STRING,
        "eyes": STRING,
        "body_features": STRING,
        "clothing": STRING,
        "distinctive_features": STRING,
        "visual_identity": STRING,
        "continuity_notes": STRING,
        "consistency_rules": STRING_ARRAY,
    }

    world_properties = {
        "setting": STRING,
        "time_period": STRING,
        "geography": STRING,
        "architecture": STRING,
        "environment": STRING,
        "weather_style": STRING,
        "color_and_lighting": STRING,
        "continuity_rules": STRING_ARRAY,
    }

    return {
        "type": "OBJECT",
        "properties": {
            "characters": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": character_properties,
                    "required": list(character_properties.keys()),
                },
            },
            "world": {
                "type": "OBJECT",
                "properties": world_properties,
                "required": list(world_properties.keys()),
            },
        },
        "required": ["characters", "world"],
    }


def make_prompt(story, topic, story_text, config):
    scenes = build_story_context(story)

    return f"""
You are the character-identity and world-continuity designer for
a photorealistic Hindi cinematic story.

TOPIC:
{topic}

SOURCE STORY TEXT:
{story_text or "(No separate story text supplied.)"}

STORY TITLE:
{story.get("title", topic)}

FORMAT:
{config["format"]}

AUDIENCE:
{config["audience"]}

VISUAL STYLE:
{config["visual_style"]}

REALISM:
{config["realism"]}

QUALITY:
{config["quality"]}

CAMERA:
{config["camera_style"]}

LIGHTING:
{config["lighting"]}

MOOD:
{config["mood"]}

CHARACTER CONSISTENCY:
{config["character_consistency"]}

WORLD CONSISTENCY:
{config["world_consistency"]}

SCENE CONTINUITY:
{config["scene_continuity"]}

NATURAL MOTION:
{config["natural_motion"]}

STORY SCENES:
{json.dumps(scenes, ensure_ascii=False, indent=2)}

TASK:
Create a production-ready character bible and world bible for the
entire supplied story. The supplied story is the source of truth.

CHARACTERS:
- Identify only characters supported by the story.
- Merge alternate descriptions of the same person into one identity.
- Give each character a unique stable ID: CHAR_001, CHAR_002, etc.
- Keep the same face, age, skin tone, hair, eyes, body and distinctive
  features across all scenes.
- Clothing may change only when the story justifies the change.
- Write visual_identity as a concise English description that can be
  inserted directly into an image-generation prompt.
- Include concrete facial details, realistic hair, believable body
  proportions and consistent clothing.
- Do not invent major characters or change character relationships.

WORLD:
- Describe setting, time period, geography, architecture, environment,
  weather and color/lighting identity.
- Preserve the story's locations and timeline.
- Avoid unsupported world details that contradict the source.

REALISM:
- Use believable live-action people and locations.
- No cartoon, anime, comic, illustration or game-character styling.
- Avoid plastic skin, exaggerated anatomy and inconsistent faces.
- Use realistic light, shadows and physical environments.
- When enabled, consistency rules must explicitly guard against
  cartoon styling, random neon and glitch effects.

OUTPUT:
Return ONLY JSON matching the required schema.
Every listed field must be present.
Use English for visual identity, appearance, clothing and continuity rules.
Do not include Markdown or explanations.
"""


def validate_bible(data):
    if not isinstance(data, dict):
        raise RuntimeError("Gemini response must be a JSON object.")

    characters = data.get("characters")
    world = data.get("world")

    if not isinstance(characters, list):
        raise RuntimeError("Character Bible has no valid characters array.")

    if not isinstance(world, dict):
        raise RuntimeError("Character Bible has no valid world object.")

    character_fields = [
        "character_id", "name", "role", "importance", "age", "gender",
        "personality", "appearance", "face_features", "skin_tone", "hair",
        "eyes", "body_features", "clothing", "distinctive_features",
        "visual_identity", "continuity_notes", "consistency_rules",
    ]

    seen_ids = set()
    seen_names = set()

    for index, character in enumerate(characters, start=1):
        if not isinstance(character, dict):
            raise RuntimeError(f"Character {index} must be an object.")

        for field in character_fields:
            if field not in character:
                raise RuntimeError(
                    f"Character {index} is missing field {field!r}."
                )

        char_id = str(character["character_id"]).strip()
        name = str(character["name"]).strip()

        if not char_id or not name:
            raise RuntimeError(
                f"Character {index} has an empty ID or name."
            )

        if char_id in seen_ids:
            raise RuntimeError(f"Duplicate character ID: {char_id}")

        normalized_name = re.sub(r"\s+", " ", name).casefold()
        if normalized_name in seen_names:
            raise RuntimeError(f"Duplicate character name: {name}")

        seen_ids.add(char_id)
        seen_names.add(normalized_name)

        for field in character_fields:
            if field != "consistency_rules":
                character[field] = str(character[field] or "").strip()

        if len(character["visual_identity"]) < 20:
            raise RuntimeError(
                f"{char_id} has an incomplete visual_identity."
            )

        rules = character["consistency_rules"]
        if not isinstance(rules, list) or not rules:
            raise RuntimeError(
                f"{char_id} must have at least one consistency rule."
            )

        character["consistency_rules"] = [
            str(rule).strip() for rule in rules if str(rule).strip()
        ]

    world_fields = [
        "setting", "time_period", "geography", "architecture",
        "environment", "weather_style", "color_and_lighting",
        "continuity_rules",
    ]

    for field in world_fields:
        if field not in world:
            raise RuntimeError(f"World bible is missing {field!r}.")

    for field in world_fields:
        if field != "continuity_rules":
            world[field] = str(world[field] or "").strip()

    if not isinstance(world["continuity_rules"], list):
        raise RuntimeError("World continuity_rules must be a list.")

    world["continuity_rules"] = [
        str(rule).strip()
        for rule in world["continuity_rules"]
        if str(rule).strip()
    ]

    if not world["setting"]:
        raise RuntimeError("World setting cannot be empty.")

    return data


def generate(api_key, model, prompt, max_retries):
    encoded_model = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{encoded_model}:generateContent"
    )

    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "temperature": 0.25,
            "topP": 0.8,
            "maxOutputTokens": 12000,
            "responseMimeType": "application/json",
            "responseSchema": character_schema(),
        },
    }

    last_error = None

    for attempt in range(1, max_retries + 1):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            log(f"Generating character/world bible: attempt {attempt}/{max_retries}")

            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))

            candidates = result.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini returned no candidates.")

            candidate = candidates[0]
            finish_reason = str(candidate.get("finishReason", "")).upper()

            if finish_reason in {"MAX_TOKENS", "LENGTH"}:
                raise RuntimeError("Gemini output was truncated.")

            response_parts = candidate.get("content", {}).get("parts", [])
            response_text = "\n".join(
                item.get("text", "")
                for item in response_parts
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
            ).strip()

            if not response_text:
                raise RuntimeError("Gemini returned empty text.")

            generated = json.loads(response_text)
            return validate_bible(generated)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body[:800]}"
            )

            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error from exc

        except Exception as exc:
            last_error = exc

        if attempt < max_retries:
            delay = min(30, 3 * (2 ** (attempt - 1)))
            log(f"Generation/validation failed: {last_error}")
            log(f"Retrying in {delay} seconds.")
            time.sleep(delay)

    raise RuntimeError(
        f"Character Bible failed after {max_retries} attempts: {last_error}"
    )


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    source = load_input_config()
    config = build_config(source)
    model = selected_model()

    story = read_json(INPUT)

    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("ai_story.json is missing or not completed.")

    topic = get_topic(source) or str(story.get("topic", "")).strip()
    story_text = get_story_text(source) or str(
        story.get("story_text", "")
    ).strip()

    if not topic and not story_text:
        raise RuntimeError("No topic or source story text is available.")

    fingerprint = source_fingerprint(
        story, topic, story_text, config, model
    )

    log("=" * 60)
    log("KATHA LOK AI - CHARACTER / WORLD BIBLE")
    log("=" * 60)
    log(f"Model        : {model}")
    log(f"Format       : {config['format']}")
    log(f"Topic        : {topic}")
    log(f"Character ID : consistent")
    log(f"World rules  : consistent")
    log(f"Visual style : {config['visual_style']}")

    if not config["character_bible"]:
        result = {
            "status": "disabled",
            "topic": topic,
            "model": model,
            "source_fingerprint": fingerprint,
            "characters": [],
            "world": {
                "setting": "As specified by each scene",
                "time_period": "",
                "geography": "",
                "architecture": "",
                "environment": "",
                "weather_style": "",
                "color_and_lighting": "",
                "continuity_rules": [],
            },
            "input_config": config,
        }
        write_json(OUTPUT, result)
        log("Character Bible disabled by configuration.")
        return

    existing = read_json(OUTPUT, required=False)

    if (
        isinstance(existing, dict)
        and existing.get("status") == "completed"
        and existing.get("source_fingerprint") == fingerprint
    ):
        try:
            validate_bible({
                "characters": existing.get("characters"),
                "world": existing.get("world"),
            })
            log("Matching, validated Character Bible already exists; reusing.")
            return
        except (RuntimeError, TypeError, ValueError):
            log("Saved Character Bible is invalid; regenerating.")

    prompt = make_prompt(story, topic, story_text, config)
    generated = generate(
        api_key, model, prompt, config["max_retries"]
    )

    result = {
        "status": "completed",
        "topic": topic,
        "model": model,
        "format": config["format"],
        "visual_style": config["visual_style"],
        "realism": config["realism"],
        "source_fingerprint": fingerprint,
        "characters": generated["characters"],
        "world": generated["world"],
        "input_config": config,
    }

    write_json(OUTPUT, result)

    log("=" * 60)
    log("CHARACTER / WORLD BIBLE COMPLETED")
    log(f"Characters : {len(result['characters'])}")
    log(f"Output     : {OUTPUT}")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)