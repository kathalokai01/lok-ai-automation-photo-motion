#!/usr/bin/env python3
"""Generate resumable AI story scenes with hook and suspense validation."""

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
INPUT = ROOT / "output" / "story" / "story.json"
OUTPUT = ROOT / "output" / "story" / "ai_story.json"
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


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
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


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file not found: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON at {path}: {exc}") from exc


def save_json(data):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT)


def load_selected_model():
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
        "audience": cfg_text(source, "AUDIENCE", "adult"),
        "story_length": cfg_text(source, "STORY_LENGTH", "auto"),
        "scene_duration": cfg_text(source, "SCENE_DURATION", "auto"),
        "voice": cfg_text(source, "VOICE", "male"),
        "captions": cfg_text(source, "CAPTIONS", "hindi"),
        "part_hook": cfg_bool(source, "PART_HOOK", True),
        "part_suspense": cfg_bool(source, "PART_SUSPENSE", True),
        "final_resolution": cfg_bool(source, "FINAL_RESOLUTION", True),
        "character_bible": cfg_bool(source, "CHARACTER_BIBLE", True),
        "character_consistency": cfg_bool(
            source, "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": cfg_bool(source, "WORLD_CONSISTENCY", True),
        "scene_continuity": cfg_bool(source, "SCENE_CONTINUITY", True),
        "visual_style": cfg_text(source, "VISUAL_STYLE", "cinematic_realistic"),
        "realism": cfg_text(source, "REALISM", "high"),
        "cinematic_camera": cfg_bool(source, "CINEMATIC_CAMERA", True),
        "camera_style": cfg_text(source, "CAMERA_STYLE", "cinematic"),
        "lighting": cfg_text(source, "LIGHTING", "cinematic"),
        "realistic_lighting": cfg_bool(source, "REALISTIC_LIGHTING", True),
        "mood": cfg_text(source, "MOOD", "dramatic"),
        "quality": cfg_text(source, "QUALITY", "high"),
        "natural_motion": cfg_bool(source, "NATURAL_MOTION", True),
        "negative_prompt": cfg_bool(source, "NEGATIVE_PROMPT", True),
        "avoid_cartoon": cfg_bool(source, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": cfg_bool(source, "AVOID_NEON", True),
        "avoid_glitch": cfg_bool(source, "AVOID_GLITCH_EFFECTS", True),
        "music": cfg_bool(source, "MUSIC", True),
        "music_style": cfg_text(source, "MUSIC_STYLE", "cinematic"),
        "sfx": cfg_bool(source, "SFX", True),
        "ambient_sound": cfg_bool(source, "AMBIENT_SOUND", True),
        "transitions": cfg_text(source, "TRANSITIONS", "cinematic"),
        "max_retries": max(1, get_max_retries(source)),
    }


def fingerprint(story, topic, story_text, config, model):
    payload = {
        "version": 5,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "config": config,
        "model": model,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_source_parts(parts):
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("No story parts found in story.json.")

    total = 0

    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Source part {part_index} is invalid.")

        try:
            part_number = int(part.get("part", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Source part numbering is invalid.") from exc

        if part_number != part_index:
            raise RuntimeError("Story part numbering must start at 1 and be continuous.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {part_index} has no source scenes.")

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
                    f"Part {part_index} scene numbering must be continuous."
                )

        total += len(scenes)

    return total


def call_gemini(api_key, model, prompt, max_retries):
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
            "temperature": 0.65,
            "topP": 0.9,
            "maxOutputTokens": 16000,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
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
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))

            candidates = result.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini returned no candidates.")

            candidate = candidates[0]
            reason = str(candidate.get("finishReason", "")).upper()

            if reason in {"MAX_TOKENS", "LENGTH"}:
                raise RuntimeError("Gemini response was truncated.")

            response_parts = candidate.get("content", {}).get("parts", [])
            response_text = "\n".join(
                item.get("text", "")
                for item in response_parts
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
            ).strip()

            if not response_text:
                raise RuntimeError("Gemini returned empty text.")

            data = json.loads(response_text)

            if not isinstance(data, dict):
                raise RuntimeError("Gemini response must be a JSON object.")

            return data

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body[:1000]}"
            )

            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error from exc

        except Exception as exc:
            last_error = exc

        if attempt < max_retries:
            delay = min(5 * (2 ** (attempt - 1)), 30)
            log(f"Attempt {attempt} failed: {last_error}")
            log(f"Retrying in {delay} seconds.")
            time.sleep(delay)

    raise RuntimeError(
        f"Gemini failed after {max_retries} attempts: {last_error}"
    )


def make_part_prompt(
    topic, story_text, story, part, config, story_hook
):
    part_number = int(part["part"])
    parts = story.get("parts", [])
    source_scenes = part.get("scenes", [])
    last_part = part_number == len(parts)
    last_scene_number = len(source_scenes)
    is_first_part = part_number == 1

    if config["format"] == "short":
        opening_rule = (
            "SHORT FORMAT: Make the opening hook immediate, punchy and "
            "curiosity-driven. Use natural Hindi, preferably 1-2 concise "
            "sentences. The first scene narration must start with the exact "
            "provided story hook, without a greeting or introduction."
        )
        hook_minimum = 7
    else:
        opening_rule = (
            "FULL FORMAT: The opening hook must be more developed than a "
            "short video: use meaningful Hindi sentences that establish a "
            "vivid mystery, danger, emotional dilemma or unexpected event. "
            "The first scene narration must start with the exact provided "
            "story hook, without a greeting or introductory biography."
        )
        hook_minimum = 14

    scene_context = []

    for source_scene in source_scenes:
        number = int(source_scene["scene"])
        scene_context.append({
            "scene": number,
            "purpose": source_scene.get(
                "purpose", source_scene.get("role", "")
            ),
            "source_narration": source_scene.get("narration", ""),
            "source_visual": source_scene.get(
                "visual", source_scene.get("visual_prompt", "")
            ),
            "source_dialogue": source_scene.get("dialogue", ""),
            "source_suspense": source_scene.get("suspense", ""),
            "opening_hook_required": (
                number == 1 and (is_first_part or config["part_hook"])
            ),
            "suspense_required": (
                number == last_scene_number and config["part_suspense"]
            ),
            "is_final_story_scene": last_part and number == last_scene_number,
        })

    if config["part_hook"]:
        part_hook_rule = (
            "The first scene of every part must begin with a distinct, "
            "story-specific hook. For part 1, preserve the exact supplied "
            "story hook. For later parts, use a fresh curiosity gap or reveal."
        )
    else:
        part_hook_rule = (
            "The first scene of the complete story must preserve the exact "
            "supplied story hook. Other part openings should remain engaging."
        )

    if config["part_suspense"]:
        suspense_rule = (
            "The final scene of this part must have a specific suspense beat. "
            "Its narration must communicate the reveal, unanswered question, "
            "approaching threat or consequence. suspense_prompt must describe "
            "the matching visual/audio suspense moment in at least 4 words."
        )
    else:
        suspense_rule = (
            "Use suspense when it naturally serves the story; do not force "
            "cliffhangers at every part ending."
        )

    if last_part and config["final_resolution"]:
        ending_rule = (
            "Resolve the central conflict clearly in the final part. Any "
            "remaining mystery must come after the main emotional payoff."
        )
    else:
        ending_rule = (
            "Keep the ending connected to the story's next development and "
            "respect the supplied scene plan."
        )

    return f"""
You are the cinematic scene-writing AI for Katha Lok AI.

TOPIC:
{topic}

USER STORY TEXT:
{story_text or "(No separate story text supplied.)"}

STORY TITLE:
{story.get("title", topic)}

STORY'S REQUIRED OPENING HOOK:
{story_hook}

FORMAT:
{config["format"]}

AUDIENCE:
{config["audience"]}

STORY LENGTH:
{config["story_length"]}

TARGET SCENE DURATION:
{config["scene_duration"]}

PART:
{part_number} of {len(parts)}

SOURCE SCENES:
{json.dumps(scene_context, ensure_ascii=False, indent=2)}

OPENING HOOK RULE:
{opening_rule}

PART OPENINGS:
{part_hook_rule}

PART ENDINGS:
{suspense_rule}

FINAL ENDING:
{ending_rule}

CONTINUITY:
- Follow every source scene in order; do not merge or omit scenes.
- Preserve the source story, names, ages, clothing, locations and timeline.
- Use natural, clear Hindi narration and believable spoken dialogue.
- Do not replace the supplied story with an unrelated plot.
- Avoid generic greetings, channel introductions and filler.
- Do not repeat the same hook or suspense wording across parts.
- Keep the narration appropriate for the audience and video format.

REALISTIC VISUALS:
- Use live-action cinematic descriptions of people and believable environments.
- Describe concrete actions, facial expressions, posture, setting and objects.
- Maintain character identity, wardrobe, lighting and spatial continuity.
- Avoid cartoon, anime, comic, illustration and slideshow aesthetics.
- Prefer physically plausible movement and realistic camera framing.

VISUAL STYLE: {config["visual_style"]}
REALISM: {config["realism"]}
CAMERA STYLE: {config["camera_style"]}
CINEMATIC CAMERA: {config["cinematic_camera"]}
LIGHTING: {config["lighting"]}
REALISTIC LIGHTING: {config["realistic_lighting"]}
MOOD: {config["mood"]}
QUALITY: {config["quality"]}
NATURAL MOTION: {config["natural_motion"]}
NEGATIVE PROMPT ENABLED: {config["negative_prompt"]}
AVOID CARTOON: {config["avoid_cartoon"]}
AVOID NEON: {config["avoid_neon"]}
AVOID GLITCH: {config["avoid_glitch"]}
MUSIC ENABLED: {config["music"]}
MUSIC STYLE: {config["music_style"]}
SFX ENABLED: {config["sfx"]}
AMBIENT SOUND ENABLED: {config["ambient_sound"]}
TRANSITIONS: {config["transitions"]}

FOR EACH SCENE RETURN:
- scene: exact source scene number
- narration: Hindi voice-over; the opening hook must be actual narration
- dialogue: natural Hindi dialogue or empty string
- visual_prompt: detailed realistic live-action visual
- negative_prompt: unwanted visual elements
- camera_prompt: framing and plausible camera movement
- lighting_prompt: physically plausible lighting
- sfx_prompt: sound effects or empty string if disabled
- music_prompt: music direction or empty string if disabled
- suspense_prompt: specific suspense beat when required, otherwise empty

VALIDATION REQUIREMENTS:
- Part 1, scene 1 narration must begin with the exact story hook.
- The opening hook must contain at least {hook_minimum} words.
- If part hooks are enabled, every other part's first scene needs a distinct hook.
- Required part-ending suspense must appear in narration and suspense_prompt.
- suspense_prompt must have at least 4 words when required.
- Return exactly {len(source_scenes)} scenes in the supplied order.
- Return ONLY valid JSON matching the required schema.
"""


def validate_generated_scenes(
    generated,
    source_scenes,
    part_number,
    config,
    story_hook,
):
    if not isinstance(generated, list) or len(generated) != len(source_scenes):
        raise RuntimeError(
            f"Part {part_number}: expected {len(source_scenes)} scenes."
        )

    validated = []
    last_scene_number = len(source_scenes)
    hook_minimum = 7 if config["format"] == "short" else 14

    for index, (source, scene) in enumerate(
        zip(source_scenes, generated), start=1
    ):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Part {part_number}, scene {index} is not an object."
            )

        try:
            scene_number = int(scene.get("scene", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Generated scene number is invalid.") from exc

        if scene_number != index:
            raise RuntimeError(
                f"Part {part_number}: scene order mismatch at {index}."
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

        if not item["visual_prompt"] or len(item["visual_prompt"]) < 30:
            raise RuntimeError(
                f"Part {part_number}, scene {index}: visual_prompt is missing "
                "or too short."
            )

        hook_required = (
            (part_number == 1 and index == 1)
            or (config["part_hook"] and index == 1)
        )

        if hook_required:
            minimum = hook_minimum if part_number == 1 else 7
            if word_count(item["narration"]) < minimum:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: hook requires at "
                    f"least {minimum} narration words."
                )

        if part_number == 1 and index == 1:
            normalized_hook = normalize_text(story_hook)
            normalized_narration = normalize_text(item["narration"])

            if not normalized_hook:
                raise RuntimeError("The source story hook is empty.")

            if not normalized_narration.startswith(normalized_hook):
                raise RuntimeError(
                    "Part 1, scene 1 narration must begin with the exact "
                    "story hook from story.json."
                )

        suspense_required = (
            config["part_suspense"] and index == last_scene_number
        ) or bool(source.get("suspense_required", False))

        if suspense_required:
            if word_count(item["suspense_prompt"]) < 4:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: required "
                    "suspense_prompt is missing or too short."
                )

            if word_count(item["narration"]) < 6:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: suspense narration "
                    "is too short."
                )

        validated.append(item)

    return validated


def reusable_part(saved_part, source_part, config, story_hook):
    if not isinstance(saved_part, dict):
        return False

    if saved_part.get("status") != "completed":
        return False

    if saved_part.get("part") != source_part.get("part"):
        return False

    saved_scenes = saved_part.get("scenes")
    source_scenes = source_part.get("scenes")

    if not isinstance(saved_scenes, list) or not isinstance(source_scenes, list):
        return False

    if len(saved_scenes) != len(source_scenes):
        return False

    try:
        validate_generated_scenes(
            [
                {
                    key: scene.get(key, "")
                    for key in ("scene", *SCENE_FIELDS)
                }
                for scene in saved_scenes
            ],
            source_scenes,
            int(source_part["part"]),
            config,
            story_hook,
        )
    except (RuntimeError, TypeError, ValueError, AttributeError):
        return False

    return all(
        isinstance(scene, dict)
        and scene.get("status") == "completed"
        and scene.get("scene") == index
        for index, scene in enumerate(saved_scenes, start=1)
    )


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    source_config = load_input_config()
    config = build_config(source_config)
    model = load_selected_model()
    story = read_json(INPUT)

    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("story.json is missing or not completed.")

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
    total_expected_scenes = validate_source_parts(source_parts)
    source_fingerprint = fingerprint(
        story, topic, story_text, config, model
    )

    log("=" * 60)
    log("KATHA LOK AI - HOOK + SUSPENSE VALIDATED SCENE GENERATION")
    log("=" * 60)
    log(f"Model            : {model}")
    log(f"Format           : {config['format']}")
    log(f"Topic            : {topic}")
    log(f"Parts            : {len(source_parts)}")
    log(f"Total scenes     : {total_expected_scenes}")
    log(f"Opening hook     : REQUIRED ({hook_minimum}+ words)")
    log(f"Part hooks       : {config['part_hook']}")
    log(f"Part suspense    : {config['part_suspense']}")
    log(f"Final resolution : {config['final_resolution']}")

    existing_parts = {}
    existing = read_json(OUTPUT, required=False)

    if isinstance(existing, dict):
        if existing.get("source_fingerprint") == source_fingerprint:
            for saved_part in existing.get("parts", []):
                if isinstance(saved_part, dict):
                    existing_parts[saved_part.get("part")] = saved_part
            log("Matching fingerprint found; validating saved checkpoints.")
        else:
            log("Story/settings changed; old checkpoints will not be reused.")

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
        source_scenes = source_part["scenes"]
        saved_part = existing_parts.get(part_number)

        if reusable_part(saved_part, source_part, config, story_hook):
            log(f"Part {part_number}: reusing validated checkpoint.")
            completed_part = saved_part
        else:
            prompt = make_part_prompt(
                topic,
                story_text,
                story,
                source_part,
                config,
                story_hook,
            )

            log(
                f"Generating part {part_number} "
                f"({len(source_scenes)} scenes)..."
            )

            generated_data = call_gemini(
                api_key, model, prompt, config["max_retries"]
            )

            generated_scenes = validate_generated_scenes(
                generated_data.get("scenes", []),
                source_scenes,
                part_number,
                config,
                story_hook,
            )

            completed_part = {
                "part": part_number,
                "status": "completed",
                "title": str(
                    source_part.get("title") or f"Part {part_number}"
                ),
                "hook_required": (
                    part_number == 1 or config["part_hook"]
                ),
                "suspense_required": config["part_suspense"],
                "final_resolution": (
                    config["final_resolution"]
                    and part_number == len(source_parts)
                ),
                "scenes": generated_scenes,
            }

            log(f"Part {part_number}: hook/suspense validation passed.")

        result["parts"] = [
            part for part in result["parts"]
            if part.get("part") != part_number
        ]
        result["parts"].append(completed_part)
        result["parts"].sort(key=lambda item: int(item["part"]))
        save_json(result)
        log(f"Part {part_number}: checkpoint saved.")

    if len(result["parts"]) != len(source_parts):
        raise RuntimeError("Final part count mismatch.")

    actual_scenes = 0

    for index, part in enumerate(result["parts"], start=1):
        if part.get("part") != index or part.get("status") != "completed":
            raise RuntimeError(f"Part {index} is incomplete or misnumbered.")

        validate_generated_scenes(
            [
                {
                    key: scene.get(key, "")
                    for key in ("scene", *SCENE_FIELDS)
                }
                for scene in part.get("scenes", [])
            ],
            source_parts[index - 1]["scenes"],
            index,
            config,
            story_hook,
        )
        actual_scenes += len(part["scenes"])

    if actual_scenes != total_expected_scenes:
        raise RuntimeError(
            f"Scene count mismatch: {actual_scenes}/{total_expected_scenes}."
        )

    result["status"] = "completed"
    result["validation"] = {
        "opening_hook_required": True,
        "opening_hook_minimum_words": hook_minimum,
        "opening_hook_preserved_in_first_narration": True,
        "part_hooks_required": config["part_hook"],
        "part_suspense_required": config["part_suspense"],
        "total_scenes": actual_scenes,
    }

    save_json(result)

    log("=" * 60)
    log("AI SCENE GENERATION COMPLETED")
    log(f"Output        : {OUTPUT}")
    log(f"Parts         : {len(result['parts'])}")
    log(f"Scenes        : {actual_scenes}")
    log("Opening hook  : PASSED")
    log("Suspense check: PASSED")
    log(f"Model         : {model}")
    log("Status        : completed")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)