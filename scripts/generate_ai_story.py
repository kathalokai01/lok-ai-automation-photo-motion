#!/usr/bin/env python3

import hashlib
import json
import os
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


INPUT = Path("output/story/story.json")
OUTPUT = Path("output/story/ai_story.json")
MODEL_FILE = Path("output/config/selected_model.json")

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
REQUEST_TIMEOUT = 180
MAX_RESPONSE_CHARS = 100000


# ============================================================
# LOGGING AND CONFIG
# ============================================================

def log(message=""):
    print(message, flush=True)


def cfg_text(config, key, default=""):
    value = config.get(key, default)

    if value is None:
        return str(default)

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

    return bool(default)


def build_config():
    config = load_input_config()

    return {
        "format": normalize_format(config),
        "audience": cfg_text(config, "AUDIENCE", "adult"),
        "story_length": cfg_text(config, "STORY_LENGTH", "auto"),
        "scene_duration": cfg_text(config, "SCENE_DURATION", "auto"),

        "part_hook": cfg_bool(config, "PART_HOOK", True),
        "part_suspense": cfg_bool(config, "PART_SUSPENSE", True),
        "final_resolution": cfg_bool(config, "FINAL_RESOLUTION", True),

        "character_bible": cfg_bool(config, "CHARACTER_BIBLE", True),
        "character_consistency": cfg_bool(config, "CHARACTER_CONSISTENCY", True),
        "world_consistency": cfg_bool(config, "WORLD_CONSISTENCY", True),
        "scene_continuity": cfg_bool(config, "SCENE_CONTINUITY", True),

        "visual_style": cfg_text(
            config, "VISUAL_STYLE", "cinematic_realistic"
        ),
        "realism": cfg_text(config, "REALISM", "high"),
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

        "music": cfg_bool(config, "MUSIC", True),
        "music_style": cfg_text(config, "MUSIC_STYLE", "cinematic"),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),

        "max_retries": max(1, get_max_retries(config)),
        "resume_enabled": cfg_bool(config, "RESUME_ENABLED", True),
        "skip_completed": cfg_bool(config, "SKIP_COMPLETED_SCENES", True),
    }


# ============================================================
# JSON AND FILE SAFETY
# ============================================================

def load_json(path, required=True):
    if not path.is_file():
        if required:
            raise SystemExit(f"ERROR: Required file not found: {path}")
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as exc:
        raise SystemExit(f"ERROR: Invalid JSON in {path}: {exc}")

    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: JSON root must be an object: {path}")

    return data


def save_json(data):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    temp = OUTPUT.with_suffix(".tmp")

    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp, OUTPUT)


def stable_hash(data):
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def load_selected_model():
    config = load_json(MODEL_FILE)

    if config.get("status") != "selected":
        raise SystemExit(
            "ERROR: Gemini model selection is not in selected state."
        )

    model = str(config.get("model", "")).strip()

    if not model:
        raise SystemExit("ERROR: selected_model.json has no model value.")

    model = model.removeprefix("models/")

    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise SystemExit("ERROR: Invalid Gemini model identifier.")

    return model


# ============================================================
# SOURCE PLAN VALIDATION
# ============================================================

def validate_source_plan(story, expected_format):
    if story.get("status") not in {"planned", "completed", "in_progress"}:
        raise SystemExit(
            f"ERROR: Story plan has unexpected status: {story.get('status')}"
        )

    if story.get("format") != expected_format:
        raise SystemExit(
            "ERROR: Story plan format does not match Input/topic.txt. "
            f"Input={expected_format}, plan={story.get('format')}"
        )

    parts = story.get("parts")

    if not isinstance(parts, list) or not parts:
        raise SystemExit("ERROR: Story plan contains no parts.")

    seen_parts = set()
    total_scenes = 0

    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise SystemExit(f"ERROR: Part {part_index} is not an object.")

        part_number = part.get("part")

        if not isinstance(part_number, int) or part_number != part_index:
            raise SystemExit(
                f"ERROR: Part numbering must be sequential. "
                f"Expected {part_index}, got {part_number}"
            )

        if part_number in seen_parts:
            raise SystemExit(f"ERROR: Duplicate part number: {part_number}")

        seen_parts.add(part_number)

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or not scenes:
            raise SystemExit(f"ERROR: Part {part_number} has no scenes.")

        scene_numbers = []

        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise SystemExit(
                    f"ERROR: Part {part_number}, scene {scene_index} "
                    "is not an object."
                )

            number = scene.get("scene")

            if not isinstance(number, int) or number != scene_index:
                raise SystemExit(
                    f"ERROR: Invalid scene numbering in part {part_number}. "
                    f"Expected {scene_index}, got {number}"
                )

            scene_numbers.append(number)

        if len(scene_numbers) != len(set(scene_numbers)):
            raise SystemExit(
                f"ERROR: Duplicate scenes found in part {part_number}."
            )

        total_scenes += len(scenes)

    declared_parts = story.get("parts_count")
    declared_scenes = story.get("total_scenes")

    if declared_parts is not None and declared_parts != len(parts):
        raise SystemExit(
            f"ERROR: Plan parts_count mismatch: {declared_parts}/{len(parts)}"
        )

    if declared_scenes is not None and declared_scenes != total_scenes:
        raise SystemExit(
            f"ERROR: Plan total_scenes mismatch: "
            f"{declared_scenes}/{total_scenes}"
        )

    return len(parts), total_scenes


# ============================================================
# RESPONSE SCHEMA
# ============================================================

def build_response_schema():
    scene_schema = {
        "type": "object",
        "properties": {
            "scene": {"type": "integer"},
            "narration": {"type": "string"},
            "dialogue": {"type": "string"},
            "visual_prompt": {"type": "string"},
            "negative_prompt": {"type": "string"},
            "camera_prompt": {"type": "string"},
            "lighting_prompt": {"type": "string"},
            "sfx_prompt": {"type": "string"},
            "music_prompt": {"type": "string"},
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
        ],
    }

    return {
        "type": "object",
        "properties": {
            "scenes": {
                "type": "array",
                "items": scene_schema,
            }
        },
        "required": ["scenes"],
    }


# ============================================================
# GEMINI RESPONSE PARSING
# ============================================================

def extract_response_text(result):
    candidates = result.get("candidates", [])

    if not candidates:
        feedback = result.get("promptFeedback", {})
        raise ValueError(
            f"Gemini returned no candidates. Feedback: {feedback}"
        )

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason", "")

    if finish_reason == "SAFETY":
        raise ValueError("Gemini blocked the generated story for safety.")

    content = candidate.get("content", {})
    parts = content.get("parts", [])

    text_parts = [
        item.get("text", "")
        for item in parts
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]

    response_text = "\n".join(text_parts).strip()

    if not response_text:
        raise ValueError(
            f"Gemini returned empty content. Finish reason: {finish_reason}"
        )

    if len(response_text) > MAX_RESPONSE_CHARS:
        raise ValueError("Gemini response exceeded the allowed size.")

    return response_text


def parse_json_response(text):
    text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:
        data = json.loads(cleaned[start:end + 1])

        if isinstance(data, dict):
            return data

    raise ValueError("Gemini response did not contain valid JSON.")


# ============================================================
# PART WORD COUNT AND SCENE VALIDATION
# ============================================================

def word_count(text):
    return len(re.findall(r"\S+", text.strip()))


def validate_generated_scenes(generated, source_part, pipeline_config):
    if not isinstance(generated, list):
        raise ValueError("Generated scenes must be a list.")

    source_scenes = source_part.get("scenes", [])

    if len(generated) != len(source_scenes):
        raise ValueError(
            f"Expected {len(source_scenes)} scenes, "
            f"received {len(generated)}."
        )

    expected_numbers = [scene["scene"] for scene in source_scenes]
    actual_numbers = [scene.get("scene") for scene in generated]

    if actual_numbers != expected_numbers:
        raise ValueError(
            f"Scene numbering mismatch. Expected {expected_numbers}; "
            f"received {actual_numbers}."
        )

    required_fields = (
        "narration",
        "dialogue",
        "visual_prompt",
        "negative_prompt",
        "camera_prompt",
        "lighting_prompt",
        "sfx_prompt",
        "music_prompt",
    )

    clean_scenes = []

    for scene in generated:
        clean = {"scene": scene["scene"]}

        for field in required_fields:
            value = scene.get(field)

            if not isinstance(value, str):
                raise ValueError(
                    f"Scene {scene['scene']} field {field} is not text."
                )

            value = value.strip()

            if field in {
                "narration",
                "visual_prompt",
                "camera_prompt",
                "lighting_prompt",
            } and not value:
                raise ValueError(
                    f"Scene {scene['scene']} has empty {field}."
                )

            clean[field] = value

        clean_scenes.append(clean)

    narration = " ".join(
        scene["narration"] for scene in clean_scenes
    )

    count = word_count(narration)

    if pipeline_config["format"] == "short":
        if count < 100 or count > 175:
            raise ValueError(
                f"Short part narration is {count} words; "
                "target is approximately 120-150 words."
            )

    return clean_scenes


# ============================================================
# PROMPT CONSTRUCTION
# ============================================================

def build_part_prompt(topic, story_text, part, pipeline_config):
    scenes = part.get("scenes", [])

    scene_context = []

    for scene in scenes:
        scene_context.append({
            "scene": scene["scene"],
            "role": scene.get("role", ""),
            "hook_required": scene.get("hook_required", False),
            "suspense_required": scene.get("suspense_required", False),
            "final_resolution": scene.get("final_resolution", False),
        })

    if pipeline_config["format"] == "short":
        format_instruction = """
SHORT VIDEO:
- This is an independently structured short-form story part.
- Use approximately 120-150 Hindi words across the entire part.
- Do not write a compressed summary of a full-length story.
- Open with an immediate curiosity or danger hook.
- Escalate tension naturally and avoid filler.
- Deliver a reveal, payoff or deliberate suspense ending.
- The ending must feel intentional, not accidentally cut off.
"""
    else:
        format_instruction = """
FULL-LENGTH VIDEO:
- Build a coherent long-form cinematic story.
- Develop setup, conflict, escalation, climax and resolution.
- Do not compress important story developments.
- Maintain natural pacing and meaningful scene progression.
"""

    final_part = part["part"] == len(
        part.get("_total_parts", [])
    ) if isinstance(part.get("_total_parts"), list) else False

    prompt = f"""
You are the Hindi story and cinematic scene-writing engine.

SOURCE TOPIC:
{topic}

SOURCE STORY TEXT:
{story_text if story_text else "[No supplied story text. Build from the topic.]"}

FORMAT:
{pipeline_config["format"]}

AUDIENCE:
{pipeline_config["audience"]}

STORY LENGTH:
{pipeline_config["story_length"]}

SCENE DURATION:
{pipeline_config["scene_duration"]}

PART NUMBER:
{part["part"]}

SCENE REQUIREMENTS:
{json.dumps(scene_context, ensure_ascii=False, indent=2)}

FORMAT REQUIREMENTS:
{format_instruction}

STORY RULES:
- Supplied STORY_TEXT is the primary source when present.
- Preserve its characters, relationships, events and locations.
- Never silently replace the supplied story with an unrelated story.
- If only a topic is supplied, create an original coherent story.
- Use natural, fluent Hindi narration and dialogue.
- Every scene must advance the story.
- Follow the exact scene numbers and roles supplied.
- Do not repeat the same information or create filler.
- Maintain continuity between scenes and parts.
- Give every part a strong opening when PART_HOOK is enabled.
- Build suspense when PART_SUSPENSE is enabled.
- The final part should resolve the central conflict when FINAL_RESOLUTION is enabled.
- The last scene of a non-final part should create a meaningful reason to continue.

VISUAL STYLE:
{pipeline_config["visual_style"]}

REALISM:
{pipeline_config["realism"]}

CAMERA STYLE:
{pipeline_config["camera_style"]}

LIGHTING:
{pipeline_config["lighting"]}

MOOD:
{pipeline_config["mood"]}

QUALITY:
{pipeline_config["quality"]}

VISUAL REQUIREMENTS:
- Describe cinematic, believable visuals.
- Use realistic human appearance and physical environments.
- Describe shot composition, perspective and useful camera movement.
- Maintain character identity, clothing and location continuity.
- Avoid cartoon, anime, comic and illustration aesthetics when configured.
- Avoid unnecessary neon or glitch effects when configured.
- Describe natural movement and believable lighting.

NEGATIVE PROMPT ENABLED:
{pipeline_config["negative_prompt"]}

AVOID CARTOON:
{pipeline_config["avoid_cartoon"]}

AVOID NEON:
{pipeline_config["avoid_neon"]}

AVOID GLITCH:
{pipeline_config["avoid_glitch"]}

NATURAL MOTION:
{pipeline_config["natural_motion"]}

REALISTIC LIGHTING:
{pipeline_config["realistic_lighting"]}

AUDIO:
MUSIC ENABLED: {pipeline_config["music"]}
MUSIC STYLE: {pipeline_config["music_style"]}
SFX ENABLED: {pipeline_config["sfx"]}
AMBIENT SOUND ENABLED: {pipeline_config["ambient_sound"]}

Write scene-specific sound descriptions only when the corresponding
audio setting is enabled. Do not add unrelated music or sound effects.

OUTPUT RULES:
- Return only valid JSON matching the supplied response schema.
- Return exactly one object for each requested scene.
- Do not change scene numbers.
- Do not add or remove scenes.
- Narration and dialogue must be natural Hindi.
- Each narration must be usable as voice-over.
- Each visual_prompt must be a complete image-generation prompt.
- camera_prompt and lighting_prompt must contain practical visual directions.
- Return no markdown and no explanation.
"""

    return prompt

# ============================================================
# GEMINI API REQUEST
# ============================================================

def request_gemini(api_key, model, prompt):
    url = (
        f"{API_BASE}/models/{model}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.75,
            "topP": 0.90,
            "responseMimeType": "application/json",
            "responseSchema": build_response_schema(),
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

    return parse_json_response(
        extract_response_text(result)
    )


def generate_part(
    api_key,
    model,
    topic,
    story_text,
    part,
    pipeline_config,
):
    prompt = build_part_prompt(
        topic,
        story_text,
        part,
        pipeline_config,
    )

    last_error = None
    retries = pipeline_config["max_retries"]

    for attempt in range(1, retries + 1):
        try:
            log(
                f"Generating part {part['part']} "
                f"(attempt {attempt}/{retries}) "
                f"with {model}"
            )

            result = request_gemini(
                api_key,
                model,
                prompt,
            )

            generated = result.get("scenes")

            return validate_generated_scenes(
                generated,
                part,
                pipeline_config,
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            last_error = (
                f"HTTP {exc.code}: {body[:1200]}"
            )

            log(
                f"Part {part['part']} failed: {last_error}"
            )

            retryable = exc.code in {
                408,
                425,
                429,
                500,
                502,
                503,
                504,
            }

            if not retryable:
                break

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            last_error = str(exc)

            log(
                f"Part {part['part']} failed: {last_error}"
            )

        except Exception as exc:
            last_error = str(exc)

            log(
                f"Unexpected part generation error: {last_error}"
            )

        if attempt < retries:
            delay = min(
                30,
                3 * (2 ** (attempt - 1)),
            )

            log(f"Retrying after {delay} seconds.")
            time.sleep(delay)

    raise RuntimeError(
        f"Part {part['part']} failed after {retries} attempts: "
        f"{last_error}"
    )


# ============================================================
# CACHED OUTPUT VALIDATION
# ============================================================

def is_completed_part_valid(existing_part, source_part):
    if not isinstance(existing_part, dict):
        return False

    if existing_part.get("status") != "completed":
        return False

    if existing_part.get("part") != source_part.get("part"):
        return False

    source_scenes = source_part.get("scenes", [])
    cached_scenes = existing_part.get("scenes", [])

    if not isinstance(cached_scenes, list):
        return False

    if len(cached_scenes) != len(source_scenes):
        return False

    required_fields = (
        "narration",
        "dialogue",
        "visual_prompt",
        "negative_prompt",
        "camera_prompt",
        "lighting_prompt",
        "sfx_prompt",
        "music_prompt",
    )

    for expected, cached in zip(source_scenes, cached_scenes):
        if not isinstance(cached, dict):
            return False

        if cached.get("scene") != expected.get("scene"):
            return False

        if cached.get("status") != "completed":
            return False

        for field in required_fields:
            value = cached.get(field)

            if not isinstance(value, str):
                return False

            if field in {
                "narration",
                "visual_prompt",
                "camera_prompt",
                "lighting_prompt",
            } and not value.strip():
                return False

    return True


def make_completed_part(source_part, generated_scenes):
    completed = {
        "part": source_part["part"],
        "status": "completed",
        "hook_required": source_part.get(
            "hook_required",
            False,
        ),
        "suspense_required": source_part.get(
            "suspense_required",
            False,
        ),
        "final_resolution": source_part.get(
            "final_resolution",
            False,
        ),
        "title": source_part.get("title", ""),
        "scenes": [],
    }

    source_by_number = {
        scene["scene"]: scene
        for scene in source_part.get("scenes", [])
    }

    for generated in generated_scenes:
        source = source_by_number[generated["scene"]]

        completed["scenes"].append({
            "scene": generated["scene"],
            "role": source.get("role", ""),
            "status": "completed",

            "narration": generated["narration"],
            "dialogue": generated["dialogue"],

            "visual_prompt": generated["visual_prompt"],
            "negative_prompt": generated["negative_prompt"],
            "camera_prompt": generated["camera_prompt"],
            "lighting_prompt": generated["lighting_prompt"],

            "sfx_prompt": generated["sfx_prompt"],
            "music_prompt": generated["music_prompt"],

            "scene_duration": source.get(
                "scene_duration",
                source.get("duration", ""),
            ),
        })

    return completed


# ============================================================
# RESUME AND SOURCE FINGERPRINT
# ============================================================

def build_generation_signature(
    story,
    pipeline_config,
    topic,
    story_text,
):
    return stable_hash({
        "story_plan": story,
        "topic": topic,
        "story_text": story_text,
        "pipeline_config": pipeline_config,
    })


def load_resumable_output(signature, pipeline_config):
    if not pipeline_config["resume_enabled"]:
        log("Resume disabled. Starting a fresh AI story.")
        return None

    existing = load_json(
        OUTPUT,
        required=False,
    )

    if not isinstance(existing, dict):
        return None

    if existing.get("generation_signature") != signature:
        log(
            "Existing AI story does not match current input/settings. "
            "Starting fresh."
        )
        return None

    existing_parts = existing.get("parts", [])

    if not isinstance(existing_parts, list):
        return None

    log(
        f"Compatible AI story found. "
        f"Cached parts: {len(existing_parts)}"
    )

    return existing


def find_existing_part(parts, part_number):
    for part in parts:
        if (
            isinstance(part, dict)
            and part.get("part") == part_number
        ):
            return part

    return None


# ============================================================
# FINAL OUTPUT VALIDATION
# ============================================================

def validate_final_output(ai_story, source_story):
    source_parts = source_story.get("parts", [])
    output_parts = ai_story.get("parts", [])

    if len(output_parts) != len(source_parts):
        raise RuntimeError(
            f"Final part count mismatch: "
            f"{len(output_parts)}/{len(source_parts)}"
        )

    expected_total = 0
    actual_total = 0

    for source_part, output_part in zip(
        source_parts,
        output_parts,
    ):
        if output_part.get("part") != source_part.get("part"):
            raise RuntimeError("Final output part order is incorrect.")

        if output_part.get("status") != "completed":
            raise RuntimeError(
                f"Part {output_part.get('part')} is incomplete."
            )

        expected_scenes = source_part.get("scenes", [])
        actual_scenes = output_part.get("scenes", [])

        if len(actual_scenes) != len(expected_scenes):
            raise RuntimeError(
                f"Part {output_part.get('part')} scene count mismatch."
            )

        for expected, actual in zip(expected_scenes, actual_scenes):
            if actual.get("scene") != expected.get("scene"):
                raise RuntimeError(
                    f"Incorrect scene order in part {output_part['part']}."
                )

            if actual.get("status") != "completed":
                raise RuntimeError(
                    f"Incomplete scene: "
                    f"Part {output_part['part']} "
                    f"Scene {actual.get('scene')}"
                )

            for field in (
                "narration",
                "visual_prompt",
                "camera_prompt",
                "lighting_prompt",
            ):
                if not str(actual.get(field, "")).strip():
                    raise RuntimeError(
                        f"Empty {field}: "
                        f"Part {output_part['part']} "
                        f"Scene {actual.get('scene')}"
                    )

        expected_total += len(expected_scenes)
        actual_total += len(actual_scenes)

    if actual_total != expected_total:
        raise RuntimeError(
            f"Final scene count mismatch: "
            f"{actual_total}/{expected_total}"
        )

    return len(output_parts), actual_total


# ============================================================
# MAIN
# ============================================================

def main():
    api_key = os.environ.get(
        "GEMINI_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise SystemExit("ERROR: GEMINI_API_KEY is not set.")

    input_config = load_input_config()
    pipeline_config = build_config()
    model = load_selected_model()

    story = load_json(INPUT)

    total_parts, total_scenes = validate_source_plan(
        story,
        pipeline_config["format"],
    )

    topic = get_topic(input_config)
    story_text = get_story_text(input_config)

    if not topic:
        topic = str(story.get("topic", "")).strip()

    if not topic and not story_text:
        raise SystemExit(
            "ERROR: Both TOPIC and STORY_TEXT are empty."
        )

    if not topic:
        topic = str(
            story.get("title", "")
        ).strip() or "Story from supplied STORY_TEXT"

    signature = build_generation_signature(
        story,
        pipeline_config,
        topic,
        story_text,
    )

    existing = load_resumable_output(
        signature,
        pipeline_config,
    )

    ai_story = {
        "status": "generating",
        "topic": topic,
        "title": story.get("title", topic),
        "story_text": story_text,
        "format": pipeline_config["format"],
        "audience": pipeline_config["audience"],
        "story_length": pipeline_config["story_length"],
        "scene_duration": pipeline_config["scene_duration"],
        "model": model,
        "generation_signature": signature,
        "input_config": pipeline_config,
        "parts": [],
    }

    if existing:
        ai_story.update(existing)

        # Keep current source metadata authoritative.
        ai_story["topic"] = topic
        ai_story["title"] = story.get("title", topic)
        ai_story["story_text"] = story_text
        ai_story["format"] = pipeline_config["format"]
        ai_story["model"] = model
        ai_story["generation_signature"] = signature
        ai_story["input_config"] = pipeline_config

    log("=" * 58)
    log("GEMINI AI STORY GENERATION")
    log("=" * 58)
    log(f"Model          : {model}")
    log(f"Format         : {pipeline_config['format']}")
    log(f"Topic          : {topic}")
    log(f"Story text     : {'provided' if story_text else 'not provided'}")
    log(f"Parts          : {total_parts}")
    log(f"Scenes         : {total_scenes}")
    log(f"Retries        : {pipeline_config['max_retries']}")
    log("=" * 58)

    # Save a valid in-progress state before the first API request.
    ai_story["status"] = "in_progress"
    save_json(ai_story)

    for source_part in story["parts"]:
        part_number = source_part["part"]

        cached_part = find_existing_part(
            ai_story.get("parts", []),
            part_number,
        )

        if (
            pipeline_config["skip_completed"]
            and is_completed_part_valid(
                cached_part,
                source_part,
            )
        ):
            log(
                f"Part {part_number} already completed. "
                "Skipping API call."
            )
            continue

        generated_scenes = generate_part(
            api_key,
            model,
            topic,
            story_text,
            source_part,
            pipeline_config,
        )

        completed_part = make_completed_part(
            source_part,
            generated_scenes,
        )

        ai_story["parts"] = [
            item
            for item in ai_story.get("parts", [])
            if item.get("part") != part_number
        ]

        ai_story["parts"].append(completed_part)
        ai_story["parts"].sort(
            key=lambda item: item["part"]
        )

        ai_story["status"] = "in_progress"

        # Checkpoint immediately after every successful part.
        save_json(ai_story)

        log(
            f"Part {part_number}/{total_parts} "
            "generated and checkpoint saved."
        )

    completed_parts, completed_scenes = validate_final_output(
        ai_story,
        story,
    )

    if completed_parts != total_parts:
        raise RuntimeError("Not all story parts were completed.")

    if completed_scenes != total_scenes:
        raise RuntimeError("Not all story scenes were completed.")

    ai_story["status"] = "completed"
    ai_story["model"] = model
    ai_story["topic"] = topic
    ai_story["title"] = story.get("title", topic)
    ai_story["story_text"] = story_text
    ai_story["format"] = pipeline_config["format"]
    ai_story["audience"] = pipeline_config["audience"]
    ai_story["story_length"] = pipeline_config["story_length"]
    ai_story["scene_duration"] = pipeline_config["scene_duration"]
    ai_story["generation_signature"] = signature
    ai_story["input_config"] = pipeline_config

    save_json(ai_story)

    log("=" * 58)
    log("AI STORY GENERATION COMPLETED")
    log(f"Parts          : {completed_parts}")
    log(f"Scenes         : {completed_scenes}")
    log(f"Model          : {model}")
    log(f"Output         : {OUTPUT}")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(
            "ERROR: Interrupted. Completed part checkpoints are preserved."
        )
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}")