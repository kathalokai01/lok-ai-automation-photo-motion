
#!/usr/bin/env python3
"""Generate a resumable, validated character and world bible."""

import hashlib
import json
import os
import re
import sys
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
INPUT = STORY_DIR / "ai_story.json"
OUTPUT = STORY_DIR / "character_bible.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"
CHECKPOINT = STORY_DIR / "character_bible_response_checkpoint.json"
VERSION = 3
TIMEOUT = max(30, int(os.getenv("BIBLE_REQUEST_TIMEOUT", "180")))


def log(message):
    print(message, flush=True)


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
        raise RuntimeError(
            "A selected Gemini model is required. "
            "Run scripts/select_gemini_model.py first."
        )

    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid selected model ID: {model!r}")
    return model


def build_config(source):
    try:
        retries = int(get_max_retries(source))
    except (TypeError, ValueError):
        retries = 1

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
        # Kept for config compatibility. Uncertain requests are never
        # automatically repeated merely because a response was not parsed.
        "max_retries_configured": max(1, min(5, retries)),
    }


def build_story_context(story):
    context = []
    expected_global = 0

    for part in story.get("parts", []):
        if not isinstance(part, dict):
            raise RuntimeError("AI story contains an invalid part.")

        for scene in part.get("scenes", []):
            if not isinstance(scene, dict):
                raise RuntimeError("AI story contains an invalid scene.")

            expected_global += 1
            context.append({
                "part": part.get("part"),
                "scene": scene.get("scene"),
                "global_scene_number": scene.get(
                    "global_scene_number", expected_global
                ),
                "role": scene.get("role", ""),
                "narration": scene.get("narration", ""),
                "dialogue": scene.get("dialogue", ""),
                "visual_prompt": scene.get("visual_prompt", ""),
            })

    if not context:
        raise RuntimeError("The AI story contains no usable scenes.")
    return context


STRING = {"type": "STRING"}
STRING_ARRAY = {"type": "ARRAY", "items": {"type": "STRING"}}


def character_schema():
    character_fields = {
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
    world_fields = {
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
                    "properties": character_fields,
                    "required": list(character_fields),
                },
            },
            "world": {
                "type": "OBJECT",
                "properties": world_fields,
                "required": list(world_fields),
            },
        },
        "required": ["characters", "world"],
    }


def make_prompt(story, topic, story_text, config):
    scenes = build_story_context(story)
    return f"""
You are the character-identity and world-continuity designer for
a photorealistic Hindi cinematic story.

TOPIC: {topic}
SOURCE STORY TEXT: {story_text or "(No separate story text supplied.)"}
STORY TITLE: {story.get("title", topic)}
FORMAT: {config["format"]}
AUDIENCE: {config["audience"]}
VISUAL STYLE: {config["visual_style"]}
REALISM: {config["realism"]}
QUALITY: {config["quality"]}
CAMERA: {config["camera_style"]}
LIGHTING: {config["lighting"]}
MOOD: {config["mood"]}

CHARACTER CONSISTENCY: {config["character_consistency"]}
WORLD CONSISTENCY: {config["world_consistency"]}
SCENE CONTINUITY: {config["scene_continuity"]}
NATURAL MOTION: {config["natural_motion"]}

SOURCE SCENES:
{json.dumps(scenes, ensure_ascii=False, indent=2)}

TASK:
Create a production-ready character bible and world bible.
The supplied story is the source of truth.

CHARACTERS:
- Include only characters supported by the story.
- Merge alternate descriptions of the same person.
- Assign stable IDs CHAR_001, CHAR_002, etc.
- Preserve face, age, skin tone, hair, eyes, body and distinctive traits.
- Clothing changes only when justified by the story.
- visual_identity must be detailed English text directly reusable
  in image-generation prompts.
- Do not invent major characters or change relationships.

WORLD:
- Describe setting, period, geography, architecture, environment,
  weather, and visual/light identity.
- Preserve locations and timeline.
- Do not invent details that contradict the story.

REALISM:
- Believable live-action people and locations.
- No cartoon, anime, comic, illustration or game styling.
- Avoid plastic skin, exaggerated anatomy and inconsistent faces.
- Keep lighting, shadows, and physical environments plausible.
- Apply enabled restrictions against cartoon styling, random neon
  and glitch effects in continuity rules.

Return ONLY JSON matching the required schema.
Every field must be present. Use English for visual identity,
appearance, clothing and continuity rules.
""".strip()


def validate_bible(data):
    if not isinstance(data, dict):
        raise RuntimeError("Response must be a JSON object.")

    characters = data.get("characters")
    world = data.get("world")
    if not isinstance(characters, list):
        raise RuntimeError("Missing valid characters array.")
    if not isinstance(world, dict):
        raise RuntimeError("Missing valid world object.")

    character_fields = [
        "character_id", "name", "role", "importance", "age", "gender",
        "personality", "appearance", "face_features", "skin_tone", "hair",
        "eyes", "body_features", "clothing", "distinctive_features",
        "visual_identity", "continuity_notes", "consistency_rules",
    ]
    ids, names = set(), set()

    for index, character in enumerate(characters, 1):
        if not isinstance(character, dict):
            raise RuntimeError(f"Character {index} must be an object.")

        for field in character_fields:
            if field not in character:
                raise RuntimeError(
                    f"Character {index} missing field {field!r}."
                )

        char_id = str(character["character_id"] or "").strip()
        name = str(character["name"] or "").strip()
        if not char_id or not name:
            raise RuntimeError(f"Character {index} has empty ID/name.")
        if char_id in ids:
            raise RuntimeError(f"Duplicate character ID: {char_id}")

        normalized_name = re.sub(r"\s+", " ", name).casefold()
        if normalized_name in names:
            raise RuntimeError(f"Duplicate character name: {name}")

        ids.add(char_id)
        names.add(normalized_name)

        for field in character_fields:
            if field != "consistency_rules":
                character[field] = str(character[field] or "").strip()

        if len(character["visual_identity"]) < 20:
            raise RuntimeError(
                f"{char_id}: visual_identity is too short."
            )

        rules = character["consistency_rules"]
        if not isinstance(rules, list):
            raise RuntimeError(f"{char_id}: consistency_rules must be a list.")

        character["consistency_rules"] = [
            str(rule).strip() for rule in rules if str(rule).strip()
        ]
        if not character["consistency_rules"]:
            raise RuntimeError(f"{char_id}: add at least one consistency rule.")

    world_fields = [
        "setting", "time_period", "geography", "architecture",
        "environment", "weather_style", "color_and_lighting",
        "continuity_rules",
    ]

    for field in world_fields:
        if field not in world:
            raise RuntimeError(f"World bible missing {field!r}.")

    for field in world_fields:
        if field != "continuity_rules":
            world[field] = str(world[field] or "").strip()

    if not world["setting"]:
        raise RuntimeError("World setting cannot be empty.")
    if not isinstance(world["continuity_rules"], list):
        raise RuntimeError("World continuity_rules must be a list.")

    world["continuity_rules"] = [
        str(rule).strip()
        for rule in world["continuity_rules"]
        if str(rule).strip()
    ]
    if not world["continuity_rules"]:
        raise RuntimeError("World must contain continuity rules.")

    return data


def request_gemini(api_key, model, prompt):
    encoded_model = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{encoded_model}:generateContent"
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
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Gemini request outcome may be uncertain; automatic retry "
            f"was stopped to avoid duplicate charges: {exc}"
        ) from exc

    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError(str(
            result.get("error", {}).get(
                "message", "Gemini returned no candidates."
            )
        ))

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
        raise RuntimeError("Gemini returned empty response text.")

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid JSON; raw response could not be parsed."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Gemini response must be a JSON object.")
    return data


def main():
    source = load_input_config()
    config = build_config(source)
    model = selected_model()
    story = read_json(INPUT)

    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("ai_story.json is missing or not completed.")

    topic = str(get_topic(source) or story.get("topic", "")).strip()
    story_text = str(
        get_story_text(source) or story.get("story_text", "")
    ).strip()
    if not topic and not story_text:
        raise RuntimeError("No topic or source story text is available.")

    # Validate scene structure before making any API request.
    build_story_context(story)

    source_fp = fingerprint({
        "version": VERSION,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "config": config,
        "model": model,
    })

    if not config["character_bible"]:
        write_json(OUTPUT, {
            "status": "disabled",
            "version": VERSION,
            "topic": topic,
            "model": model,
            "source_fingerprint": source_fp,
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
        })
        log("Character Bible disabled by configuration.")
        return

    existing = read_json(OUTPUT, required=False)
    if (
        isinstance(existing, dict)
        and existing.get("status") == "completed"
        and existing.get("source_fingerprint") == source_fp
        and existing.get("version") == VERSION
    ):
        try:
            validate_bible({
                "characters": existing.get("characters"),
                "world": existing.get("world"),
            })
            log("Matching validated Character Bible reused; no API call.")
            return
        except (RuntimeError, TypeError, ValueError):
            log("Saved Character Bible is invalid; checking response checkpoint.")

    checkpoint = read_json(CHECKPOINT, required=False)
    response = None
    if (
        isinstance(checkpoint, dict)
        and checkpoint.get("source_fingerprint") == source_fp
        and checkpoint.get("version") == VERSION
        and isinstance(checkpoint.get("response"), dict)
    ):
        response = checkpoint["response"]
        log("Matching raw response checkpoint found; no API call needed.")

    if response is None:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY secret is missing.")

        prompt = make_prompt(story, topic, story_text, config)
        log(f"Generating character/world bible with {model}.")

        try:
            response = request_gemini(api_key, model, prompt)
        except Exception as exc:
            # Do not retry uncertain failures automatically.
            raise RuntimeError(
                f"Generation stopped safely: {exc}. No automatic retry "
                "was made; inspect the run and provider quota before retrying."
            ) from exc

        # Persist the raw JSON response before schema validation.
        write_json(CHECKPOINT, {
            "status": "response_saved",
            "version": VERSION,
            "source_fingerprint": source_fp,
            "model": model,
            "response": response,
        })
        log("Raw Gemini response checkpoint saved.")

    try:
        generated = validate_bible(response)
    except (RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Character/world response failed validation: {exc}. "
            f"Raw response is preserved at {CHECKPOINT}. "
            "No second API request was made."
        ) from exc

    result = {
        "status": "completed",
        "version": VERSION,
        "topic": topic,
        "model": model,
        "format": config["format"],
        "visual_style": config["visual_style"],
        "realism": config["realism"],
        "source_fingerprint": source_fp,
        "characters": generated["characters"],
        "world": generated["world"],
        "input_config": config,
    }
    write_json(OUTPUT, result)

    log("CHARACTER / WORLD BIBLE COMPLETED")
    log(f"Characters: {len(result['characters'])}")
    log(f"Output: {OUTPUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
