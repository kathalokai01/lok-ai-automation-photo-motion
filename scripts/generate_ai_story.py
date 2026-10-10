#!/usr/bin/env python3

import hashlib
import json
import os
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
    "required": [
        "scene",
        "narration",
        "dialogue",
        "visual_prompt",
        "negative_prompt",
        "camera_prompt",
        "lighting_prompt",
        "sfx_prompt",
        "music_prompt",
        "suspense_prompt",
    ],
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
        raise RuntimeError(
            f"Cannot read valid JSON from {path}: {exc}"
        ) from exc


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

    if not isinstance(data, dict):
        raise RuntimeError("Selected Gemini model file must be an object.")

    if data.get("status") != "selected":
        raise RuntimeError(
            "Gemini model selection is not in selected state."
        )

    model = str(data.get("model", "")).strip()

    if (
        not model
        or model.startswith("models/")
        or "/" in model
        or any(char.isspace() for char in model)
    ):
        raise RuntimeError(
            f"Invalid selected Gemini model identifier: {model!r}"
        )

    return model


def build_config(source_config):
    return {
        "format": normalize_format(source_config),
        "audience": cfg_text(source_config, "AUDIENCE", "adult"),
        "story_length": cfg_text(
            source_config, "STORY_LENGTH", "auto"
        ),
        "scene_duration": cfg_text(
            source_config, "SCENE_DURATION", "auto"
        ),
        "voice": cfg_text(source_config, "VOICE", "male"),
        "captions": cfg_text(source_config, "CAPTIONS", "hindi"),
        "part_hook": cfg_bool(source_config, "PART_HOOK", True),
        "part_suspense": cfg_bool(
            source_config, "PART_SUSPENSE", True
        ),
        "final_resolution": cfg_bool(
            source_config, "FINAL_RESOLUTION", True
        ),
        "character_bible": cfg_bool(
            source_config, "CHARACTER_BIBLE", True
        ),
        "character_consistency": cfg_bool(
            source_config, "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": cfg_bool(
            source_config, "WORLD_CONSISTENCY", True
        ),
        "scene_continuity": cfg_bool(
            source_config, "SCENE_CONTINUITY", True
        ),
        "visual_style": cfg_text(
            source_config, "VISUAL_STYLE", "cinematic_realistic"
        ),
        "realism": cfg_text(source_config, "REALISM", "high"),
        "cinematic_camera": cfg_bool(
            source_config, "CINEMATIC_CAMERA", True
        ),
        "camera_style": cfg_text(
            source_config, "CAMERA_STYLE", "cinematic"
        ),
        "lighting": cfg_text(
            source_config, "LIGHTING", "cinematic"
        ),
        "realistic_lighting": cfg_bool(
            source_config, "REALISTIC_LIGHTING", True
        ),
        "mood": cfg_text(source_config, "MOOD", "dramatic"),
        "quality": cfg_text(source_config, "QUALITY", "high"),
        "natural_motion": cfg_bool(
            source_config, "NATURAL_MOTION", True
        ),
        "negative_prompt": cfg_bool(
            source_config, "NEGATIVE_PROMPT", True
        ),
        "avoid_cartoon": cfg_bool(
            source_config, "AVOID_CARTOON_LOOK", True
        ),
        "avoid_neon": cfg_bool(
            source_config, "AVOID_NEON", True
        ),
        "avoid_glitch": cfg_bool(
            source_config, "AVOID_GLITCH_EFFECTS", True
        ),
        "music": cfg_bool(source_config, "MUSIC", True),
        "music_style": cfg_text(
            source_config, "MUSIC_STYLE", "cinematic"
        ),
        "sfx": cfg_bool(source_config, "SFX", True),
        "ambient_sound": cfg_bool(
            source_config, "AMBIENT_SOUND", True
        ),
        "transitions": cfg_text(
            source_config, "TRANSITIONS", "cinematic"
        ),
        "max_retries": max(1, get_max_retries(source_config)),
    }


def make_fingerprint(story, topic, story_text, config, model):
    source = {
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "model": model,
        "config": config,
    }

    encoded = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


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
            data=json.dumps(
                payload, ensure_ascii=False
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=180
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

            candidates = result.get("candidates", [])

            if not candidates:
                raise RuntimeError("Gemini returned no candidates.")

            candidate = candidates[0]
            reason = str(
                candidate.get("finishReason", "")
            ).upper()

            if reason in {"MAX_TOKENS", "LENGTH"}:
                raise RuntimeError(
                    "Gemini response was truncated."
                )

            response_parts = (
                candidate.get("content", {}).get("parts", [])
            )

            response_text = "\n".join(
                item.get("text", "")
                for item in response_parts
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
            ).strip()

            if not response_text:
                raise RuntimeError("Gemini returned empty text.")

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Gemini returned invalid JSON: {exc}"
                ) from exc

            if not isinstance(data, dict):
                raise RuntimeError(
                    "Gemini response must be a JSON object."
                )

            return data

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8", errors="replace"
            )
            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body[:1200]}"
            )

            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error from exc

        except Exception as exc:
            last_error = exc

        if attempt < max_retries:
            delay = min(5 * (2 ** (attempt - 1)), 30)
            print(
                f"Attempt {attempt} failed: {last_error}. "
                f"Retrying in {delay}s."
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Gemini generation failed after {max_retries} attempts: "
        f"{last_error}"
    )


def make_part_prompt(topic, story_text, story, part, config):
    part_number = part["part"]
    scenes = part.get("scenes", [])

    scene_context = []

    for scene in scenes:
        scene_context.append({
            "scene": scene.get("scene"),
            "role": scene.get("role", ""),
            "source_narration": scene.get("narration", ""),
            "source_visual": scene.get("visual", ""),
            "source_suspense": scene.get("suspense", ""),
            "opening_hook_required": (
                scene.get("opening_hook_required", False)
                or (
                    config["part_hook"]
                    and scene.get("scene") == 1
                )
            ),
            "suspense_required": (
                scene.get("suspense_required", False)
                or (
                    config["part_suspense"]
                    and scene.get("scene") == len(scenes)
                )
            ),
            "final_story_scene": scene.get(
                "final_story_scene", False
            ),
        })

    format_instructions = (
        """
SHORT-FORM:
Keep the story direct and engaging. The first scene must hook
the viewer immediately. Avoid long introductions and filler.
Each part should feel intentional, not like an arbitrary cut.
"""
        if config["format"] == "short"
        else
        """
FULL-LENGTH:
Build the story naturally with setup, development, rising conflict,
turning points, climax and resolution. Do not rush emotional moments.
"""
    )

    hook_instruction = (
        "The first scene of this part MUST start with a compelling "
        "moment, urgent question, mystery, surprising action or "
        "emotional conflict. Do not start with a generic introduction."
        if config["part_hook"]
        else "Use an organic opening suited to the story."
    )

    suspense_instruction = (
        "The last scene of this part MUST have a story-specific "
        "suspense beat, meaningful reveal, unresolved question, "
        "or approaching danger. Fill suspense_prompt with the beat."
        if config["part_suspense"]
        else "Use suspense only when it fits the story."
    )

    final_instruction = (
        "For the last scene of the final part, pay off the central "
        "conflict first. Then introduce a new relevant clue, question, "
        "or threat as the final beat. Do not leave the main conflict "
        "unintentionally unresolved."
        if config["final_resolution"] and config["part_suspense"]
        else "Give the final scene a deliberate, story-appropriate ending."
    )

    return f"""
You are generating production-ready scenes for a Hindi cinematic
video pipeline.

TOPIC:
{topic}

SOURCE STORY TEXT:
{story_text or "(No separate story text supplied.)"}

STORY TITLE:
{story.get("title", topic)}

STORY FORMAT:
{config["format"]}

AUDIENCE:
{config["audience"]}

STORY LENGTH:
{config["story_length"]}

TARGET SCENE DURATION:
{config["scene_duration"]}

PART:
{part_number} of {len(story.get("parts", []))}

SCENE CONTEXT:
{json.dumps(scene_context, ensure_ascii=False, indent=2)}

FORMAT:
{format_instructions}

OPENING HOOK:
{hook_instruction}

PART SUSPENSE:
{suspense_instruction}

FINAL STORY SCENE:
{final_instruction}

CONTINUITY:
- Preserve the story's characters, names, ages, clothing and locations.
- Maintain continuity with earlier scenes.
- Keep cause and effect logical.
- Follow the scene role and source narration.
- Do not replace the supplied story with a different plot.
- Keep character expressions and actions believable.
- Use natural Hindi narration and dialogue.
- Avoid repetitive narration.
- Do not add a channel greeting or subscribe request.

REALISTIC VISUALS:
- Describe live-action people and believable real environments.
- Include concrete physical actions, expressions and surroundings.
- Avoid cartoon, anime, comic, illustration and slideshow aesthetics.
- Avoid random camera movement.
- Maintain consistent lighting and character appearance.
- Prefer subtle natural motion over exaggerated motion.

VISUAL STYLE:
{config["visual_style"]}

REALISM:
{config["realism"]}

CAMERA STYLE:
{config["camera_style"]}

CINEMATIC CAMERA ENABLED:
{config["cinematic_camera"]}

LIGHTING:
{config["lighting"]}

REALISTIC LIGHTING:
{config["realistic_lighting"]}

MOOD:
{config["mood"]}

QUALITY:
{config["quality"]}

NATURAL MOTION:
{config["natural_motion"]}

NEGATIVE PROMPT ENABLED:
{config["negative_prompt"]}

AVOID CARTOON:
{config["avoid_cartoon"]}

AVOID NEON:
{config["avoid_neon"]}

AVOID GLITCH:
{config["avoid_glitch"]}

MUSIC ENABLED:
{config["music"]}

MUSIC STYLE:
{config["music_style"]}

SFX ENABLED:
{config["sfx"]}

AMBIENT SOUND ENABLED:
{config["ambient_sound"]}

TRANSITIONS:
{config["transitions"]}

For each scene, return:
- scene: the exact supplied scene number
- narration: Hindi voice-over text
- dialogue: natural Hindi dialogue or empty string
- visual_prompt: detailed realistic live-action visual
- negative_prompt: visual elements to avoid
- camera_prompt: framing and believable camera movement
- lighting_prompt: physically plausible lighting
- sfx_prompt: sound effects or empty string if disabled
- music_prompt: music direction or empty string if disabled
- suspense_prompt: the suspense beat, or empty string if not required

Return exactly {len(scenes)} scenes in the supplied order.
Do not change scene numbers.
Return ONLY JSON matching the supplied schema.
"""


def validate_source_parts(source_parts):
    if not isinstance(source_parts, list) or not source_parts:
        raise RuntimeError("No story parts found in story.json.")

    expected_part_numbers = list(range(1, len(source_parts) + 1))

    actual_part_numbers = []

    for index, part in enumerate(source_parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Story part {index} is invalid.")

        try:
            part_number = int(part.get("part", -1))
        except (TypeError, ValueError):
            raise RuntimeError(
                f"Story part {index} has an invalid part number."
            )

        actual_part_numbers.append(part_number)

        if part_number != index:
            raise RuntimeError(
                "Story part numbering must be continuous from 1."
            )

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(
                f"Part {part_number} has no scenes."
            )

        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_index} is invalid."
                )

            try:
                scene_number = int(scene.get("scene", -1))
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_index} "
                    "has an invalid scene number."
                )

            if scene_number != scene_index:
                raise RuntimeError(
                    f"Part {part_number} scene numbering must be "
                    "continuous from 1."
                )

    if actual_part_numbers != expected_part_numbers:
        raise RuntimeError("Story part numbering is invalid.")

    return sum(len(part["scenes"]) for part in source_parts)


def validate_generated_scenes(generated, source_scenes, part_number):
    if not isinstance(generated, list):
        raise RuntimeError(
            f"Part {part_number}: generated scenes are not a list."
        )

    if len(generated) != len(source_scenes):
        raise RuntimeError(
            f"Part {part_number}: expected {len(source_scenes)} scenes, "
            f"received {len(generated)}."
        )

    validated = []

    for index, (source, scene) in enumerate(
        zip(source_scenes, generated), start=1
    ):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Part {part_number}, scene {index} is invalid."
            )

        try:
            scene_number = int(scene.get("scene", -1))
        except (TypeError, ValueError):
            scene_number = -1

        if scene_number != index:
            raise RuntimeError(
                f"Part {part_number}: scene numbering/order mismatch."
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
                    f"Part {part_number}, scene {index}: "
                    f"{field} must be text."
                )

            item[field] = value.strip()

        if not item["narration"]:
            raise RuntimeError(
                f"Part {part_number}, scene {index} has empty narration."
            )

        if not item["visual_prompt"]:
            raise RuntimeError(
                f"Part {part_number}, scene {index} "
                "has empty visual prompt."
            )

        opening_hook_required = (
            source.get("opening_hook_required", False)
            and index == 1
        )

        if opening_hook_required and not item["narration"]:
            raise RuntimeError(
                f"Part {part_number} is missing its opening hook."
            )

        if source.get("suspense_required") and not item["suspense_prompt"]:
            raise RuntimeError(
                f"Part {part_number}, scene {index} "
                "is missing its required suspense prompt."
            )

        validated.append(item)

    return validated


def is_reusable_part(saved_part, source_part):
    if not isinstance(saved_part, dict):
        return False

    if saved_part.get("status") != "completed":
        return False

    if saved_part.get("part") != source_part.get("part"):
        return False

    saved_scenes = saved_part.get("scenes")
    source_scenes = source_part.get("scenes")

    if not isinstance(saved_scenes, list):
        return False

    if not isinstance(source_scenes, list):
        return False

    if len(saved_scenes) != len(source_scenes):
        return False

    for index, (saved, source) in enumerate(
        zip(saved_scenes, source_scenes), start=1
    ):
        if not isinstance(saved, dict):
            return False

        if saved.get("status") != "completed":
            return False

        if saved.get("scene") != index:
            return False

        if saved.get("scene") != source.get("scene"):
            return False

        if not str(saved.get("narration", "")).strip():
            return False

        if not str(saved.get("visual_prompt", "")).strip():
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

    if not isinstance(story, dict):
        raise RuntimeError("story.json root must be a JSON object.")

    if story.get("status") != "completed":
        raise RuntimeError(
            "story.json is not completed. Generate the story first."
        )

    topic = get_topic(source_config) or str(
        story.get("topic", "")
    ).strip()

    story_text = get_story_text(source_config)

    if not topic and not story_text:
        raise RuntimeError("Both TOPIC and STORY_TEXT are empty.")

    if not topic:
        topic = "Story from supplied STORY_TEXT"

    source_parts = story.get("parts", [])
    total_expected_scenes = validate_source_parts(source_parts)

    fingerprint = make_fingerprint(
        story, topic, story_text, config, model
    )

    print("=" * 60)
    print("KATHA LOK AI - AI SCENE GENERATION")
    print("=" * 60)
    print(f"Model           : {model}")
    print(f"Format          : {config['format']}")
    print(f"Topic           : {topic}")
    print(f"Parts           : {len(source_parts)}")
    print(f"Total scenes    : {total_expected_scenes}")
    print(f"Opening hook    : {config['part_hook']}")
    print(f"Part suspense   : {config['part_suspense']}")
    print(f"Final resolution: {config['final_resolution']}")

    existing_parts = {}

    existing = load_json(OUTPUT, required=False)

    if isinstance(existing, dict):
        if existing.get("source_fingerprint") == fingerprint:
            saved_parts = existing.get("parts", [])

            if isinstance(saved_parts, list):
                for saved_part in saved_parts:
                    if isinstance(saved_part, dict):
                        existing_parts[saved_part.get("part")] = saved_part

            print(
                "Matching source fingerprint found; "
                "valid completed parts may resume."
            )
        else:
            print(
                "Source or settings changed, or old checkpoint has no "
                "fingerprint. Ignoring old parts and starting fresh."
            )

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
        part_number = source_part["part"]
        source_scenes = source_part["scenes"]
        saved_part = existing_parts.get(part_number)

        if is_reusable_part(saved_part, source_part):
            print(f"Part {part_number}: reusing verified checkpoint.")
            completed_part = saved_part
        else:
            prompt = make_part_prompt(
                topic,
                story_text,
                story,
                source_part,
                config,
            )

            print(
                f"Generating part {part_number} "
                f"({len(source_scenes)} scenes)..."
            )

            generated_data = call_gemini(
                api_key,
                model,
                prompt,
                config["max_retries"],
            )

            generated_scenes = validate_generated_scenes(
                generated_data.get("scenes", []),
                source_scenes,
                part_number,
            )

            completed_part = {
                "part": part_number,
                "status": "completed",
                "title": str(
                    source_part.get("title") or f"Part {part_number}"
                ),
                "hook_required": config["part_hook"],
                "suspense_required": config["part_suspense"],
                "final_resolution": (
                    config["final_resolution"]
                    and part_number == len(source_parts)
                ),
                "scenes": generated_scenes,
            }

            print(f"Part {part_number}: generated and validated.")

        result["parts"] = [
            part for part in result["parts"]
            if part.get("part") != part_number
        ]
        result["parts"].append(completed_part)
        result["parts"].sort(key=lambda item: item["part"])

        # Save after every part so a later failure does not discard progress.
        save_json(result)
        print(f"Part {part_number}: checkpoint saved.")

    actual_parts = len(result["parts"])
    actual_scenes = sum(
        len(part.get("scenes", []))
        for part in result["parts"]
    )

    if actual_parts != len(source_parts):
        raise RuntimeError(
            f"Part count mismatch: {actual_parts}/{len(source_parts)}."
        )

    if actual_scenes != total_expected_scenes:
        raise RuntimeError(
            f"Scene count mismatch: {actual_scenes}/"
            f"{total_expected_scenes}."
        )

    for index, part in enumerate(result["parts"], start=1):
        if part.get("part") != index:
            raise RuntimeError("Final part numbering is not continuous.")

        if part.get("status") != "completed":
            raise RuntimeError(
                f"Part {part.get('part')} is not completed."
            )

        scenes = part.get("scenes", [])

        for scene_index, scene in enumerate(scenes, start=1):
            if scene.get("status") != "completed":
                raise RuntimeError(
                    f"Part {index}, scene {scene_index} is incomplete."
                )

            if scene.get("scene") != scene_index:
                raise RuntimeError(
                    f"Part {index} scene numbering is invalid."
                )

            if not str(scene.get("narration", "")).strip():
                raise RuntimeError(
                    f"Part {index}, scene {scene_index} "
                    "has empty narration."
                )

            if not str(scene.get("visual_prompt", "")).strip():
                raise RuntimeError(
                    f"Part {index}, scene {scene_index} "
                    "has empty visual prompt."
                )

    result["status"] = "completed"
    result["model"] = model
    result["topic"] = topic
    result["story_text"] = story_text
    result["format"] = config["format"]
    result["input_config"] = config
    result["source_fingerprint"] = fingerprint
    save_json(result)

    print("=" * 60)
    print("AI SCENE GENERATION COMPLETED")
    print(f"Output          : {OUTPUT}")
    print(f"Parts           : {actual_parts}")
    print(f"Scenes          : {actual_scenes}")
    print(f"Model           : {model}")
    print(f"Fingerprint     : {fingerprint[:16]}...")
    print("Status          : completed")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)