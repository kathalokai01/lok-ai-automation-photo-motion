#!/usr/bin/env python3
"""Generate resumable Hindi story scenes with a bounded retry budget."""

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

SCENE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "scene": {"type": "INTEGER"},
        "narration": {"type": "STRING"},
        "dialogue": {"type": "STRING"},
        "visual_prompt": {"type": "STRING"},
        "negative_prompt": {"type": "STRING"},
        "camera_prompt": {"type": "STRING"},
        "lighting_prompt": {"type": "STRING"},
        "sfx_prompt": {"type": "STRING"},
        "music_prompt": {"type": "STRING"},
        "suspense_prompt": {"type": "STRING"},
    },
    "required": ["scene", *SCENE_FIELDS],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "scenes": {
            "type": "ARRAY",
            "items": SCENE_SCHEMA,
        },
    },
    "required": ["scenes"],
}


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
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(data):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT)


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


def normalize_text(value):
    value = str(value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value):
    return len(normalize_text(value).split())


def selected_model():
    data = read_json(MODEL_FILE)

    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "A successfully selected Gemini model is required."
        )

    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid Gemini model identifier: {model!r}")

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
        "quality": text_value(source, "QUALITY", "high"),
        "natural_motion": bool_value(source, "NATURAL_MOTION", True),
        "negative_prompt": bool_value(source, "NEGATIVE_PROMPT", True),
        "avoid_cartoon": bool_value(source, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": bool_value(source, "AVOID_NEON", True),
        "avoid_glitch": bool_value(source, "AVOID_GLITCH_EFFECTS", True),
        "music": bool_value(source, "MUSIC", True),
        "music_style": text_value(source, "MUSIC_STYLE", "cinematic"),
        "sfx": bool_value(source, "SFX", True),
        "ambient_sound": bool_value(source, "AMBIENT_SOUND", True),
        "transitions": text_value(source, "TRANSITIONS", "cinematic"),
        # One bounded budget for API and validation attempts per part.
        "max_retries": max(1, int(get_max_retries(source))),
    }


def fingerprint(story, topic, story_text, config, model):
    payload = {
        "version": 8,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "config": config,
        "model": model,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_source_parts(parts):
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("story.json contains no story parts.")

    total = 0

    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Source part {part_index} is invalid.")

        try:
            part_number = int(part.get("part", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Source part numbering is invalid.") from exc

        if part_number != part_index:
            raise RuntimeError("Source part numbers must be continuous from 1.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {part_index} contains no scenes.")

        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {part_index}, scene {scene_index} is invalid."
                )

            try:
                scene_number = int(scene.get("scene", -1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Source scene numbering is invalid.") from exc

            if scene_number != scene_index:
                raise RuntimeError(
                    f"Part {part_index} scene numbers must start at 1 "
                    "and remain continuous."
                )

        total += len(scenes)

    return total


def call_gemini(api_key, model, prompt):
    """Make exactly one HTTP request; the caller owns the retry budget."""
    model_name = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent"
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.65,
            "topP": 0.9,
            "maxOutputTokens": 16000,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
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
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini HTTP {exc.code}: {body[:800]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini network error: {exc}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"Gemini request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid HTTP JSON: {exc}") from exc

    candidates = result.get("candidates", [])
    if not candidates:
        error = result.get("error", {}).get(
            "message", "Gemini returned no candidates."
        )
        raise RuntimeError(str(error))

    candidate = candidates[0]
    reason = str(candidate.get("finishReason", "")).upper()
    if reason in {"MAX_TOKENS", "LENGTH"}:
        raise RuntimeError("Gemini response was truncated.")

    chunks = candidate.get("content", {}).get("parts", [])
    response_text = "\n".join(
        item.get("text", "")
        for item in chunks
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ).strip()

    if not response_text:
        raise RuntimeError("Gemini returned empty text.")

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid response JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Gemini response must be a JSON object.")

    return data


def make_part_prompt(
    topic, story_text, story, part, config, story_hook,
    validation_feedback="",
):
    parts = story["parts"]
    part_number = int(part["part"])
    source_scenes = part["scenes"]
    is_first_part = part_number == 1
    is_last_part = part_number == len(parts)
    hook_minimum = 7 if config["format"] == "short" else 14

    if config["format"] == "short":
        opening_rule = (
            "SHORT VIDEO: begin immediately with a concise, high-impact "
            "Hindi hook that creates curiosity. No greeting or channel intro."
        )
    else:
        opening_rule = (
            "FULL VIDEO: use a more developed hook than a short. Build "
            "mystery, danger or an emotional dilemma in 2-4 meaningful "
            "Hindi sentences. No greeting or channel intro."
        )

    if config["part_hook"]:
        part_hook_rule = (
            "Every part must begin with a distinct story-specific hook. "
            "Part 1 narration must begin with the exact supplied story hook. "
            "Later part openings must create fresh curiosity."
        )
    else:
        part_hook_rule = (
            "Only the first scene of the complete story must use the supplied "
            "opening hook. Other part openings should continue naturally."
        )

    if config["part_suspense"]:
        suspense_rule = (
            "The final scene of this part must contain a concrete suspense "
            "beat in narration AND a matching suspense_prompt of at least "
            "4 words. Create a threat, consequence, reveal or unanswered "
            "question. Avoid generic statements about mystery."
        )
    else:
        suspense_rule = "Use suspense only where it naturally serves the story."

    if is_last_part and config["final_resolution"]:
        ending_rule = (
            "Resolve the central conflict clearly in the final part. Give "
            "the main character's choices and consequences a meaningful payoff. "
            "A small future-facing mystery is allowed, but do not leave the "
            "central conflict unresolved."
        )
    else:
        ending_rule = (
            "Respect the planned progression and end this part in a way "
            "that leads naturally into the next development."
        )

    context = []
    for source in source_scenes:
        number = int(source["scene"])
        context.append({
            "scene": number,
            "purpose": source.get("purpose", source.get("role", "")),
            "source_narration": source.get("narration", ""),
            "source_visual": source.get("visual", source.get("visual_prompt", "")),
            "source_dialogue": source.get("dialogue", ""),
            "source_suspense": source.get("suspense", ""),
            "opening_hook_required": (
                number == 1 and (is_first_part or config["part_hook"])
            ),
            "suspense_required": (
                number == len(source_scenes) and config["part_suspense"]
            ),
            "is_final_story_scene": (
                is_last_part and number == len(source_scenes)
            ),
        })

    retry_block = ""
    if validation_feedback:
        retry_block = f"""
PREVIOUS ATTEMPT ERROR:
{validation_feedback}

Correct every listed error. Return the complete corrected scene array.
Do not explain the error or return only a partial answer.
"""

    return f"""
You are the Hindi cinematic scene writer for Katha Lok AI.

TOPIC: {topic}
USER STORY TEXT: {story_text or "(No separate story text supplied.)"}
STORY TITLE: {story.get("title", topic)}
REQUIRED STORY HOOK: {story_hook}
FORMAT: {config["format"]}
AUDIENCE: {config["audience"]}
STORY LENGTH: {config["story_length"]}
TARGET SCENE DURATION: {config["scene_duration"]}
PART: {part_number} of {len(parts)}

SOURCE SCENES:
{json.dumps(context, ensure_ascii=False, indent=2)}

OPENING RULES:
{opening_rule}
{part_hook_rule}
The first narration of the complete story must begin with the exact
REQUIRED STORY HOOK text. Do not paraphrase, shorten, or put anything
before it. Minimum first-scene hook length: {hook_minimum} words.
A later part hook, when enabled, must contain at least 7 narration words.

PART ENDING:
{suspense_rule}

FINAL ENDING:
{ending_rule}

CONTINUITY:
- Keep all scenes in source order. Do not merge, omit or add scenes.
- Preserve names, relationships, ages, clothing, locations and timeline.
- Write natural Hindi narration and believable dialogue.
- Avoid filler, generic greetings and unrelated plot changes.
- Do not repeat the same hook or suspense wording across parts.

VISUALS:
- Use photorealistic live-action cinematic descriptions.
- Describe people, actions, expressions, posture, setting and objects.
- Maintain identity, wardrobe, lighting and spatial continuity.
- No cartoon, anime, comic, illustration, collage or slideshow aesthetics.
- Use plausible camera framing, light and physical movement.

STYLE:
visual_style={config["visual_style"]}
realism={config["realism"]}
camera_style={config["camera_style"]}
lighting={config["lighting"]}
mood={config["mood"]}
quality={config["quality"]}
natural_motion={config["natural_motion"]}
negative_prompt={config["negative_prompt"]}
avoid_cartoon={config["avoid_cartoon"]}
avoid_neon={config["avoid_neon"]}
avoid_glitch={config["avoid_glitch"]}
music={config["music"]}, music_style={config["music_style"]}
sfx={config["sfx"]}, ambient_sound={config["ambient_sound"]}
transitions={config["transitions"]}

{retry_block}

Return exactly {len(source_scenes)} scenes in the original order.
Every scene must contain: scene, narration, dialogue, visual_prompt,
negative_prompt, camera_prompt, lighting_prompt, sfx_prompt,
music_prompt, suspense_prompt.
Use strings for every field. Use empty strings for optional dialogue
or disabled audio prompts. Return only valid JSON matching the schema.
""".strip()


def validate_generated_scenes(
    generated, source_scenes, part_number, config, story_hook
):
    if not isinstance(generated, list) or len(generated) != len(source_scenes):
        raise RuntimeError(
            f"Part {part_number}: expected exactly {len(source_scenes)} scenes."
        )

    hook_minimum = 7 if config["format"] == "short" else 14
    last_scene = len(source_scenes)
    validated = []

    for index, (source, scene) in enumerate(
        zip(source_scenes, generated), start=1
    ):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Part {part_number}, scene {index} is not an object."
            )

        try:
            number = int(scene.get("scene", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Generated scene number is invalid.") from exc

        if number != index:
            raise RuntimeError(
                f"Part {part_number}: scene order mismatch at scene {index}."
            )

        item = {
            "scene": index,
            "role": str(source.get("role", source.get("purpose", ""))),
            "status": "completed",
        }

        for field in SCENE_FIELDS:
            value = scene.get(field, "")
            if not isinstance(value, str):
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: "
                    f"{field} must be text."
                )
            item[field] = value.strip()

        if not item["narration"]:
            raise RuntimeError(
                f"Part {part_number}, scene {index}: narration is empty."
            )

        if len(item["visual_prompt"]) < 30:
            raise RuntimeError(
                f"Part {part_number}, scene {index}: "
                "visual_prompt must contain at least 30 characters."
            )

        hook_required = index == 1 and (
            part_number == 1 or config["part_hook"]
        )

        if hook_required:
            minimum = hook_minimum if part_number == 1 else 7
            if word_count(item["narration"]) < minimum:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: hook needs at "
                    f"least {minimum} narration words."
                )

        if part_number == 1 and index == 1:
            expected = normalize_text(story_hook)
            actual = normalize_text(item["narration"])
            if not expected or not actual.startswith(expected):
                raise RuntimeError(
                    "Part 1 scene 1 narration must begin with the story hook."
                )

        suspense_required = (
            config["part_suspense"] and index == last_scene
        ) or bool(source.get("suspense_required", False))

        if suspense_required:
            if word_count(item["suspense_prompt"]) < 4:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: suspense_prompt "
                    "must contain at least 4 words."
                )
            if word_count(item["narration"]) < 6:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: "
                    "suspense narration is too short."
                )

        if not config["sfx"]:
            item["sfx_prompt"] = ""
        if not config["music"]:
            item["music_prompt"] = ""

        validated.append(item)

    return validated


def generate_validated_part(
    api_key, model, topic, story_text, story, source_part, config, story_hook
):
    """Use no more than max_retries total API requests for this part."""
    budget = config["max_retries"]
    last_error = None

    for attempt in range(1, budget + 1):
        prompt = make_part_prompt(
            topic,
            story_text,
            story,
            source_part,
            config,
            story_hook,
            validation_feedback=str(last_error) if last_error else "",
        )

        log(
            f"Part {source_part['part']}: total request "
            f"{attempt}/{budget}."
        )

        try:
            generated = call_gemini(api_key, model, prompt)
            scenes = validate_generated_scenes(
                generated.get("scenes", []),
                source_part["scenes"],
                int(source_part["part"]),
                config,
                story_hook,
            )
            return scenes

        except (
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
            urllib.error.URLError,
        ) as exc:
            last_error = exc
            log(f"Part {source_part['part']} attempt failed: {exc}")

            if attempt < budget:
                delay = min(3 * (2 ** (attempt - 1)), 15)
                log(f"Retrying within the same {budget}-request budget "
                    f"in {delay} seconds.")
                time.sleep(delay)

    raise RuntimeError(
        f"Part {source_part['part']} exhausted its {budget}-request budget. "
        f"Last error: {last_error}"
    )


def reusable_part(saved, source_part, config, story_hook):
    if not isinstance(saved, dict) or saved.get("status") != "completed":
        return False

    try:
        if int(saved.get("part", -1)) != int(source_part.get("part", -2)):
            return False
    except (TypeError, ValueError):
        return False

    saved_scenes = saved.get("scenes")
    source_scenes = source_part.get("scenes")
    if not isinstance(saved_scenes, list) or not isinstance(source_scenes, list):
        return False
    if len(saved_scenes) != len(source_scenes):
        return False

    try:
        checked = validate_generated_scenes(
            saved_scenes,
            source_scenes,
            int(source_part["part"]),
            config,
            story_hook,
        )
    except (RuntimeError, TypeError, ValueError, KeyError):
        return False

    return all(scene.get("status") == "completed" for scene in checked)


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    source_config = load_input_config()
    config = build_config(source_config)
    model = selected_model()
    story = read_json(INPUT)

    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("story.json is missing or not marked completed.")

    topic = get_topic(source_config) or str(story.get("topic", "")).strip()
    story_text = get_story_text(source_config)

    if not topic and not story_text:
        raise RuntimeError("Both TOPIC and STORY_TEXT are empty.")
    if not topic:
        topic = "Story from supplied STORY_TEXT"

    story_hook = str(story.get("hook", "")).strip()
    hook_minimum = 7 if config["format"] == "short" else 14

    if word_count(story_hook) < hook_minimum:
        raise RuntimeError(
            f"story.json hook must contain at least {hook_minimum} words "
            f"for {config['format']} format."
        )

    source_parts = story.get("parts", [])
    total_expected = validate_source_parts(source_parts)
    source_fingerprint = fingerprint(
        story, topic, story_text, config, model
    )

    log("=" * 58)
    log("KATHA LOK AI - HOOK AND SUSPENSE VALIDATION")
    log(f"Model: {model}")
    log(f"Format: {config['format']}")
    log(f"Topic: {topic}")
    log(f"Parts: {len(source_parts)} | Scenes: {total_expected}")
    log(f"Opening hook minimum: {hook_minimum} words")
    log(f"Part hooks: {config['part_hook']}")
    log(f"Part suspense: {config['part_suspense']}")
    log(f"Final resolution requested: {config['final_resolution']}")
    log(f"Maximum requests per part: {config['max_retries']}")

    existing_parts = {}
    existing = read_json(OUTPUT, required=False)

    if (
        isinstance(existing, dict)
        and existing.get("source_fingerprint") == source_fingerprint
        and isinstance(existing.get("parts"), list)
    ):
        for saved in existing["parts"]:
            if isinstance(saved, dict):
                try:
                    existing_parts[int(saved.get("part", -1))] = saved
                except (TypeError, ValueError):
                    continue
        log("Matching fingerprint found; validating saved checkpoints.")
    else:
        log("No matching checkpoint; generating from current story/settings.")

    result = {
        "status": "in_progress",
        "topic": topic,
        "story_text": story_text,
        "format": config["format"],
        "audience": config["audience"],
        "story_length": config["story_length"],
        "scene_duration": config["scene_duration"],
        "model": model,
        "input_config": config,
        "source_fingerprint": source_fingerprint,
        "opening_hook": story_hook,
        "parts": [],
    }

    for source_part in source_parts:
        part_number = int(source_part["part"])
        saved = existing_parts.get(part_number)

        if reusable_part(saved, source_part, config, story_hook):
            completed_part = saved
            log(f"Part {part_number}: reusing validated checkpoint.")
        else:
            scenes = generate_validated_part(
                api_key,
                model,
                topic,
                story_text,
                story,
                source_part,
                config,
                story_hook,
            )

            completed_part = {
                "part": part_number,
                "status": "completed",
                "title": str(
                    source_part.get("title") or f"Part {part_number}"
                ),
                "hook_required": part_number == 1 or config["part_hook"],
                "suspense_required": config["part_suspense"],
                "final_resolution": (
                    config["final_resolution"]
                    and part_number == len(source_parts)
                ),
                "scenes": scenes,
            }
            log(f"Part {part_number}: hook/suspense validation passed.")

        result["parts"].append(completed_part)
        save_json(result)
        log(f"Part {part_number}: checkpoint saved.")

    actual = 0
    for part_index, part in enumerate(result["parts"], start=1):
        if (
            int(part.get("part", -1)) != part_index
            or part.get("status") != "completed"
        ):
            raise RuntimeError(
                f"Part {part_index} is incomplete or misnumbered."
            )

        validate_generated_scenes(
            part.get("scenes", []),
            source_parts[part_index - 1]["scenes"],
            part_index,
            config,
            story_hook,
        )
        actual += len(part["scenes"])

    if actual != total_expected:
        raise RuntimeError(
            f"Final scene count mismatch: {actual}/{total_expected}."
        )

    result["status"] = "completed"
    result["validation"] = {
        "opening_hook_required": True,
        "opening_hook_minimum_words": hook_minimum,
        "opening_hook_preserved_in_first_narration": True,
        "part_hooks_required": config["part_hook"],
        "part_suspense_required": config["part_suspense"],
        "final_resolution_requested": config["final_resolution"],
        "total_scenes": actual,
        "generation_validation_retries_enabled": True,
        "max_api_requests_per_part": config["max_retries"],
    }

    save_json(result)

    log("=" * 58)
    log("AI STORY SCENES COMPLETED")
    log(f"Output: {OUTPUT}")
    log(f"Validated scenes: {actual}")
    log("Opening hook: PASSED")
    log("Required part-ending suspense: PASSED")
    log("Status: completed")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)