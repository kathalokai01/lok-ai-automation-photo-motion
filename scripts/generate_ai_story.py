#!/usr/bin/env python3

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
INPUT = ROOT / "output/story/story.json"
OUTPUT = ROOT / "output/story/ai_story.json"
MODEL_FILE = ROOT / "output/config/selected_model.json"

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


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return default


def load_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file not found: {path}")
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON at {path}: {exc}") from exc


def save_json(data):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(OUTPUT)


def load_selected_model():
    data = load_json(MODEL_FILE)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError("A successfully selected Gemini model is required.")
    model = str(data.get("model", "")).strip()
    if (
        not model
        or model.startswith("models/")
        or "/" in model
        or any(char.isspace() for char in model)
    ):
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


def make_fingerprint(story, topic, story_text, config, model):
    payload = {
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


def word_count(text):
    return len(re.findall(r"\S+", str(text).strip()))


def hook_required(scene, scene_index, config):
    # First scene of the complete video ALWAYS requires a hook.
    # PART_HOOK additionally requires a hook at each part opening.
    return scene_index == 1 or (
        config["part_hook"] and scene_index == 1
    ) or bool(scene.get("opening_hook_required", False))


def suspense_required(scene, scene_index, total_scenes, config):
    return (
        config["part_suspense"] and scene_index == total_scenes
    ) or bool(scene.get("suspense_required", False))


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

            parts = candidate.get("content", {}).get("parts", [])
            response_text = "\n".join(
                item.get("text", "")
                for item in parts
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
            last_error = RuntimeError(f"Gemini HTTP {exc.code}: {body[:1000]}")
            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error from exc
        except Exception as exc:
            last_error = exc

        if attempt < max_retries:
            delay = min(5 * (2 ** (attempt - 1)), 30)
            print(f"Attempt {attempt} failed: {last_error}; retrying in {delay}s.")
            time.sleep(delay)

    raise RuntimeError(
        f"Gemini failed after {max_retries} attempts: {last_error}"
    )


def make_part_prompt(topic, story_text, story, part, config):
    part_number = int(part["part"])
    parts = story.get("parts", [])
    scenes = part.get("scenes", [])
    last_part = part_number == len(parts)
    last_scene_number = len(scenes)
    format_name = config["format"]

    if format_name == "short":
        hook_instruction = (
            "SHORT VIDEO HOOK: The FIRST scene narration must begin "
            "with an immediate, punchy curiosity gap, surprising event, "
            "urgent danger, emotional contradiction, or unanswered question. "
            "Use about 1-2 concise Hindi sentences. No greeting, backstory dump, "
            "or slow introduction. Reveal enough to create curiosity, not the "
            "whole answer."
        )
        hook_min_words = 7
    else:
        hook_instruction = (
            "FULL VIDEO HOOK: The FIRST scene narration must have a more "
            "developed hook than a short video: about 2-4 meaningful Hindi "
            "sentences, establishing a vivid mystery, emotional dilemma, "
            "unexpected event, danger, or powerful unanswered question. "
            "Create stakes and atmosphere, but do not reveal the whole plot "
            "or resolution. Do not begin with a generic greeting or biography."
        )
        hook_min_words = 14

    scene_context = []
    for scene in scenes:
        number = int(scene.get("scene", 0))
        scene_context.append({
            "scene": number,
            "role": scene.get("role", ""),
            "source_narration": scene.get("narration", ""),
            "source_visual": scene.get("visual", ""),
            "source_suspense": scene.get("suspense", ""),
            "opening_hook_required": (
                number == 1 and (part_number == 1 or config["part_hook"])
            ) or bool(scene.get("opening_hook_required", False)),
            "suspense_required": (
                number == last_scene_number and config["part_suspense"]
            ) or bool(scene.get("suspense_required", False)),
            "final_story_scene": bool(
                scene.get("final_story_scene", False)
            ) or (last_part and number == last_scene_number),
        })

    if config["part_suspense"]:
        suspense_instruction = (
            "SUSPENSE IS REQUIRED: The LAST scene of this part must end "
            "with a story-specific unresolved question, discovery, clue, "
            "approaching threat, reversal, or meaningful cliffhanger. "
            "The narration must communicate the suspense beat, and "
            "suspense_prompt must describe the exact visual/audio suspense "
            "moment. Do not use generic phrases unrelated to the plot."
        )
    else:
        suspense_instruction = (
            "Use story-appropriate suspense when it naturally fits."
        )

    if last_part and config["final_resolution"]:
        ending_instruction = (
            "FINAL PART: Resolve the central story conflict clearly and "
            "emotionally. The ending may retain a small sequel mystery, "
            "but must not leave the main conflict accidentally unresolved."
        )
    else:
        ending_instruction = (
            "PART ENDING: Make the final beat purposeful and connected "
            "to the next story development; if suspense is enabled, use "
            "a specific cliffhanger or reveal."
        )

    return f"""
You create production-ready scenes for a Hindi cinematic video.

TOPIC:
{topic}

SOURCE STORY TEXT:
{story_text or "(No separate story text supplied.)"}

STORY TITLE:
{story.get("title", topic)}

FORMAT:
{format_name}

AUDIENCE:
{config["audience"]}

STORY LENGTH:
{config["story_length"]}

TARGET SCENE DURATION:
{config["scene_duration"]}

PART:
{part_number} of {len(parts)}

SOURCE SCENE CONTEXT:
{json.dumps(scene_context, ensure_ascii=False, indent=2)}

OPENING HOOK REQUIREMENTS:
{hook_instruction}

PART SUSPENSE REQUIREMENTS:
{suspense_instruction}

ENDING REQUIREMENTS:
{ending_instruction}

CONTINUITY:
- Preserve characters, names, ages, clothing, locations and story facts.
- Keep cause and effect logical and consistent with the supplied story.
- Follow each scene's role and source narration.
- Do not replace the story with an unrelated plot.
- Keep Hindi narration natural and engaging; dialogue should sound spoken.
- Do not add channel greetings, subscribe requests, or filler.
- Make each scene distinct and avoid repeated narration.

REALISTIC VISUALS:
- Describe live-action people, believable environments and concrete actions.
- Include facial expression, body posture, setting and relevant objects.
- Avoid cartoon, anime, comic, illustration and slideshow aesthetics.
- Maintain character identity, wardrobe, lighting and spatial continuity.
- Prefer subtle, physically believable motion.

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

For each scene return:
- scene: exact supplied scene number
- narration: Hindi voice-over, including the required hook/suspense where applicable
- dialogue: natural Hindi dialogue or empty string
- visual_prompt: detailed realistic live-action visual matching the narration
- negative_prompt: unwanted visual elements
- camera_prompt: framing and plausible camera movement
- lighting_prompt: physically plausible lighting
- sfx_prompt: sound effects or empty string if disabled
- music_prompt: music direction or empty string if disabled
- suspense_prompt: specific suspense beat when required; otherwise empty string

IMPORTANT VALIDATION EXPECTATIONS:
- The opening hook must be part of the actual narration, not merely visual_prompt.
- In short format the opening hook is concise and immediate.
- In full format the opening hook is more developed and emotionally engaging.
- When a part hook is required, that part's first scene must also have a hook.
- Required suspense must appear in both narration and suspense_prompt.
- The suspense must relate to this story, not generic danger.
- Keep the supplied scene count and exact numbering.

Return exactly {len(scenes)} scenes in the supplied order.
Return ONLY JSON matching the supplied schema.
"""


def validate_source_parts(source_parts):
    if not isinstance(source_parts, list) or not source_parts:
        raise RuntimeError("No story parts found in story.json.")

    total = 0
    for part_index, part in enumerate(source_parts, start=1):
        if not isinstance(part, dict) or part.get("part") != part_index:
            raise RuntimeError("Story part numbering must be continuous from 1.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {part_index} has no scenes.")
        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict) or scene.get("scene") != scene_index:
                raise RuntimeError(
                    f"Part {part_index} scene numbering must be continuous from 1."
                )
        total += len(scenes)
    return total


def validate_generated_scenes(
    generated, source_scenes, part_number, config
):
    if not isinstance(generated, list) or len(generated) != len(source_scenes):
        raise RuntimeError(
            f"Part {part_number}: expected {len(source_scenes)} scenes."
        )

    validated = []
    total_scenes = len(source_scenes)

    for index, (source, scene) in enumerate(
        zip(source_scenes, generated), start=1
    ):
        if not isinstance(scene, dict) or scene.get("scene") != index:
            raise RuntimeError(
                f"Part {part_number}: scene numbering/order mismatch at {index}."
            )

        item = {
            "scene": index,
            "role": str(source.get("role", "")),
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
            raise RuntimeError(
                f"Part {part_number}, scene {index}: narration is empty."
            )
        if not item["visual_prompt"]:
            raise RuntimeError(
                f"Part {part_number}, scene {index}: visual_prompt is empty."
            )

        if hook_required(source, index, config):
            minimum = 7 if config["format"] == "short" else 14
            if word_count(item["narration"]) < minimum:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: opening hook is too "
                    f"short. Need at least {minimum} words in narration."
                )
            if len(item["visual_prompt"]) < 30:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: hook scene needs "
                    "a concrete visual_prompt."
                )

        if suspense_required(source, index, total_scenes, config):
            if word_count(item["suspense_prompt"]) < 4:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: required suspense_prompt "
                    "is missing or too short."
                )
            if word_count(item["narration"]) < 6:
                raise RuntimeError(
                    f"Part {part_number}, scene {index}: suspense narration "
                    "is too short."
                )

        validated.append(item)

    return validated


def is_reusable_part(saved_part, source_part, config):
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
        # Re-run the same hook/suspense checks before trusting a checkpoint.
        validate_generated_scenes(
            [
                {key: scene.get(key, "") for key in ("scene", *SCENE_FIELDS)}
                for scene in saved_scenes
            ],
            source_scenes,
            int(source_part["part"]),
            config,
        )
    except (RuntimeError, TypeError, ValueError):
        return False

    for index, saved in enumerate(saved_scenes, start=1):
        if not isinstance(saved, dict):
            return False
        if saved.get("status") != "completed" or saved.get("scene") != index:
            return False

    return True


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    source_config = load_input_config()
    config = build_config(source_config)
    model = load_selected_model()
    story = load_json(INPUT)

    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("story.json is missing or not completed.")

    topic = get_topic(source_config) or str(story.get("topic", "")).strip()
    story_text = get_story_text(source_config)
    if not topic and not story_text:
        raise RuntimeError("Both TOPIC and STORY_TEXT are empty.")
    if not topic:
        topic = "Story from supplied STORY_TEXT"

    source_parts = story.get("parts", [])
    total_expected_scenes = validate_source_parts(source_parts)
    fingerprint = make_fingerprint(story, topic, story_text, config, model)

    print("=" * 60)
    print("KATHA LOK AI - HOOK + SUSPENSE VALIDATED SCENE GENERATION")
    print("=" * 60)
    print(f"Model           : {model}")
    print(f"Format          : {config['format']}")
    print(f"Topic           : {topic}")
    print(f"Parts           : {len(source_parts)}")
    print(f"Total scenes    : {total_expected_scenes}")
    print("First-scene hook: REQUIRED")
    print(f"Part hooks      : {config['part_hook']}")
    print(f"Part suspense   : {config['part_suspense']}")
    print(f"Final resolution: {config['final_resolution']}")

    existing_parts = {}
    existing = load_json(OUTPUT, required=False)

    if isinstance(existing, dict):
        if existing.get("source_fingerprint") == fingerprint:
            for saved in existing.get("parts", []):
                if isinstance(saved, dict):
                    existing_parts[saved.get("part")] = saved
            print("Matching fingerprint: checking saved parts before reuse.")
        else:
            print("Source/settings changed: old checkpoints will not be reused.")

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
        "source_fingerprint": fingerprint,
        "parts": [],
    }

    for source_part in source_parts:
        part_number = int(source_part["part"])
        source_scenes = source_part["scenes"]
        saved_part = existing_parts.get(part_number)

        if is_reusable_part(saved_part, source_part, config):
            print(f"Part {part_number}: reusing validated checkpoint.")
            completed_part = saved_part
        else:
            prompt = make_part_prompt(
                topic, story_text, story, source_part, config
            )
            print(
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
            )

            completed_part = {
                "part": part_number,
                "status": "completed",
                "title": str(source_part.get("title") or f"Part {part_number}"),
                "hook_required": part_number == 1 or config["part_hook"],
                "suspense_required": config["part_suspense"],
                "final_resolution": (
                    config["final_resolution"]
                    and part_number == len(source_parts)
                ),
                "scenes": generated_scenes,
            }
            print(f"Part {part_number}: hook/suspense validation passed.")

        result["parts"] = [
            part for part in result["parts"]
            if part.get("part") != part_number
        ]
        result["parts"].append(completed_part)
        result["parts"].sort(key=lambda item: int(item["part"]))
        save_json(result)
        print(f"Part {part_number}: checkpoint saved.")

    if len(result["parts"]) != len(source_parts):
        raise RuntimeError("Final part count mismatch.")

    actual_scenes = 0
    for index, part in enumerate(result["parts"], start=1):
        if part.get("part") != index or part.get("status") != "completed":
            raise RuntimeError(f"Part {index} is incomplete or misnumbered.")

        validated = validate_generated_scenes(
            [
                {key: scene.get(key, "") for key in ("scene", *SCENE_FIELDS)}
                for scene in part.get("scenes", [])
            ],
            source_parts[index - 1]["scenes"],
            index,
            config,
        )
        for scene in validated:
            actual_scenes += 1

    if actual_scenes != total_expected_scenes:
        raise RuntimeError(
            f"Scene count mismatch: {actual_scenes}/{total_expected_scenes}."
        )

    result["status"] = "completed"
    save_json(result)

    print("=" * 60)
    print("AI SCENE GENERATION COMPLETED")
    print(f"Output       : {OUTPUT}")
    print(f"Parts        : {len(result['parts'])}")
    print(f"Scenes       : {actual_scenes}")
    print("Hook check   : PASSED")
    print("Suspense check: PASSED")
    print(f"Model        : {model}")
    print("Status       : completed")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)