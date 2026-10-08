#!/usr/bin/env python3

import json
import os
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


# ============================================================
# LOCAL CONFIG HELPERS
# ============================================================

def cfg_text(config, key, default=""):
    value = config.get(key, default)

    if value is None:
        return default

    return str(value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in ("true", "1", "yes", "on"):
        return True

    if text in ("false", "0", "no", "off"):
        return False

    return default


def get_story_length_value(config):
    return cfg_text(
        config,
        "STORY_LENGTH",
        "auto"
    )


def get_scene_duration_value(config):
    return cfg_text(
        config,
        "SCENE_DURATION",
        "auto"
    )


def resolve_topic_value(config):
    return get_topic(config)


def resolve_story_text_value(config):
    return get_story_text(config)


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    if not path.exists():
        raise SystemExit(f"ERROR: File not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    temp = OUTPUT.with_suffix(".tmp")

    with temp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    temp.replace(OUTPUT)


# ============================================================
# MODEL
# ============================================================

def load_selected_model():
    config = load_json(MODEL_FILE)

    if config.get("status") != "selected":
        raise SystemExit(
            "ERROR: Gemini model selection is not in selected state"
        )

    model = config.get("model")

    if not model:
        raise SystemExit(
            "ERROR: No selected Gemini model found"
        )

    return model


# ============================================================
# INPUT CONFIG
# ============================================================

def build_config():

    config = load_input_config()

    return {
        "format": normalize_format(config),

        "audience": cfg_text(
            config,
            "AUDIENCE",
            "adult"
        ),

        "story_length": get_story_length_value(config),

        "scene_duration": get_scene_duration_value(config),

        "part_hook": cfg_bool(
            config,
            "PART_HOOK",
            True
        ),

        "part_suspense": cfg_bool(
            config,
            "PART_SUSPENSE",
            True
        ),

        "final_resolution": cfg_bool(
            config,
            "FINAL_RESOLUTION",
            True
        ),

        "character_bible": cfg_bool(
            config,
            "CHARACTER_BIBLE",
            True
        ),

        "character_consistency": cfg_bool(
            config,
            "CHARACTER_CONSISTENCY",
            True
        ),

        "world_consistency": cfg_bool(
            config,
            "WORLD_CONSISTENCY",
            True
        ),

        "scene_continuity": cfg_bool(
            config,
            "SCENE_CONTINUITY",
            True
        ),

        "visual_style": cfg_text(
            config,
            "VISUAL_STYLE",
            "cinematic_realistic"
        ),

        "realism": cfg_text(
            config,
            "REALISM",
            "high"
        ),

        "cinematic_camera": cfg_bool(
            config,
            "CINEMATIC_CAMERA",
            True
        ),

        "camera_style": cfg_text(
            config,
            "CAMERA_STYLE",
            "cinematic"
        ),

        "lighting": cfg_text(
            config,
            "LIGHTING",
            "cinematic"
        ),

        "mood": cfg_text(
            config,
            "MOOD",
            "dramatic"
        ),

        "quality": cfg_text(
            config,
            "QUALITY",
            "high"
        ),

        "negative_prompt": cfg_bool(
            config,
            "NEGATIVE_PROMPT",
            True
        ),

        "avoid_cartoon": cfg_bool(
            config,
            "AVOID_CARTOON_LOOK",
            True
        ),

        "avoid_neon": cfg_bool(
            config,
            "AVOID_NEON",
            True
        ),

        "avoid_glitch": cfg_bool(
            config,
            "AVOID_GLITCH_EFFECTS",
            True
        ),

        "natural_motion": cfg_bool(
            config,
            "NATURAL_MOTION",
            True
        ),

        "realistic_lighting": cfg_bool(
            config,
            "REALISTIC_LIGHTING",
            True
        ),

        "music": cfg_bool(
            config,
            "MUSIC",
            True
        ),

        "music_style": cfg_text(
            config,
            "MUSIC_STYLE",
            "cinematic"
        ),

        "sfx": cfg_bool(
            config,
            "SFX",
            True
        ),

        "ambient_sound": cfg_bool(
            config,
            "AMBIENT_SOUND",
            True
        ),

        "transitions": cfg_text(
            config,
            "TRANSITIONS",
            "cinematic"
        ),

        "max_retries": get_max_retries(config),
    }


# ============================================================
# GEMINI PART GENERATION
# ============================================================

def generate_part(
    api_key,
    model,
    topic,
    story_text,
    part,
    pipeline_config
):

    scenes = part.get("scenes", [])

    scene_context = []

    for scene in scenes:
        scene_context.append({
            "scene": scene["scene"],
            "role": scene.get("role", ""),
            "hook_required": scene.get(
                "hook_required",
                False
            ),
            "suspense_required": scene.get(
                "suspense_required",
                False
            ),
            "final_resolution": scene.get(
                "final_resolution",
                False
            )
        })

    scene_schema = {
        "type": "object",
        "properties": {
            "scene": {
                "type": "integer"
            },
            "narration": {
                "type": "string"
            },
            "dialogue": {
                "type": "string"
            },
            "visual_prompt": {
                "type": "string"
            },
            "negative_prompt": {
                "type": "string"
            },
            "camera_prompt": {
                "type": "string"
            },
            "lighting_prompt": {
                "type": "string"
            },
            "sfx_prompt": {
                "type": "string"
            },
            "music_prompt": {
                "type": "string"
            }
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
            "music_prompt"
        ]
    }

    schema = {
        "type": "object",
        "properties": {
            "scenes": {
                "type": "array",
                "items": scene_schema
            }
        },
        "required": [
            "scenes"
        ]
    }

    format_type = pipeline_config["format"]

    if format_type == "short":

        format_instruction = """
This is a SHORT-FORM video.

Do NOT write it as a compressed full-length story.

The part must have its own:
- immediate hook
- curiosity
- escalating tension
- reveal/payoff
- memorable ending beat

The opening must create curiosity immediately.

The ending must feel intentional and suspenseful,
not like the video was simply cut off.
"""

    else:

        format_instruction = """
This is a FULL-LENGTH cinematic story.

Build the story progressively with:
- setup
- development
- conflict
- escalation
- climax
- resolution

Do not rush important story developments.
"""

    hook_instruction = (
        "A strong opening hook is REQUIRED."
        if pipeline_config["part_hook"]
        else
        "Do not force an artificial opening hook."
    )

    suspense_instruction = (
        "Build suspense and curiosity in every appropriate part."
        if pipeline_config["part_suspense"]
        else
        "Do not artificially force suspense where the story does not require it."
    )

    resolution_instruction = (
        "The final part must provide a satisfying resolution."
        if pipeline_config["final_resolution"]
        else
        "Do not force a complete resolution if the source story does not require one."
    )

    character_instruction = (
        "Maintain strict character consistency."
        if pipeline_config["character_consistency"]
        else
        "Character consistency is desirable but should not override the source story."
    )

    world_instruction = (
        "Maintain strict world/location consistency."
        if pipeline_config["world_consistency"]
        else
        "Preserve locations according to the source story."
    )

    continuity_instruction = (
        "Maintain strict scene-to-scene continuity."
        if pipeline_config["scene_continuity"]
        else
        "Maintain logical continuity between scenes."
    )

    prompt = f"""
You are the main story-generation AI for a Hindi cinematic
AI video pipeline.

SOURCE TOPIC:
{topic}

SOURCE STORY TEXT:
{
    story_text
    if story_text
    else "[No additional STORY_TEXT was provided. Build from the topic.]"
}

FORMAT:
{format_type}

AUDIENCE:
{pipeline_config["audience"]}

STORY LENGTH:
{pipeline_config["story_length"]}

TARGET SCENE DURATION:
{pipeline_config["scene_duration"]}

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

CINEMATIC CAMERA:
{pipeline_config["cinematic_camera"]}

REALISTIC LIGHTING:
{pipeline_config["realistic_lighting"]}

NATURAL MOTION:
{pipeline_config["natural_motion"]}

PART:
{part["part"]}

SCENE REQUIREMENTS:
{json.dumps(scene_context, ensure_ascii=False, indent=2)}

FORMAT RULE:
{format_instruction}

STORY STRUCTURE RULES:
- {hook_instruction}
- {suspense_instruction}
- {resolution_instruction}
- {character_instruction}
- {world_instruction}
- {continuity_instruction}

SOURCE FIDELITY:
- If SOURCE STORY TEXT is provided, use it as the primary story source.
- Do not invent a completely different story.
- Preserve important characters, events, relationships and locations
  from the supplied story.
- Expand or connect the source material only when necessary to create
  coherent scenes.
- Do not silently ignore supplied story information.

LANGUAGE:
- Narration must be natural Hindi.
- Dialogue must be natural Hindi.
- Avoid robotic or unnatural Hindi.
- Write narration suitable for voice-over.

SCENE RULES:
- Follow the supplied scene numbers exactly.
- Follow the supplied scene roles.
- Each scene must advance the story.
- Do not repeat the same information unnecessarily.
- Do not create filler scenes.
- Maintain continuity from the previous scene.
- The end of one scene should logically lead into the next.

VISUAL RULES:
- Visual prompts must describe realistic cinematic live-action visuals.
- Follow the requested visual style.
- Follow the requested realism level.
- Use real human appearance and believable environments.
- Do not describe cartoon, anime, comic or illustration aesthetics.

CAMERA:
- Use the requested camera style.
- Describe useful framing, movement and perspective.
- Avoid random camera movements.

LIGHTING:
- Follow the requested lighting style.
- Keep lighting physically believable.
- Preserve visual continuity between connected scenes.

NEGATIVE PROMPT:
{"Enabled." if pipeline_config["negative_prompt"] else "Do not force a negative prompt."}
{"Avoid cartoon/anime/comic/illustration appearance." if pipeline_config["avoid_cartoon"] else ""}
{"Avoid excessive neon appearance." if pipeline_config["avoid_neon"] else ""}
{"Avoid glitch effects." if pipeline_config["avoid_glitch"] else ""}

AUDIO:

MUSIC ENABLED:
{pipeline_config["music"]}

MUSIC STYLE:
{pipeline_config["music_style"]}

SFX ENABLED:
{pipeline_config["sfx"]}

AMBIENT SOUND ENABLED:
{pipeline_config["ambient_sound"]}

Generate appropriate music, SFX and ambient descriptions
according to these settings.

IMPORTANT:
- Return ONLY valid JSON.
- Do not return markdown.
- Do not return explanations.
- Do not change scene numbers.
- Return exactly one generated object for every requested scene.
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema
        }
    }

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    last_error = None
    max_retries = pipeline_config["max_retries"]

    for attempt in range(1, max_retries + 1):

        try:

            print(
                f"Generating Part {part['part']} "
                f"(attempt {attempt}/{max_retries}) "
                f"using model {model}..."
            )

            request = urllib.request.Request(
                api_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key
                },
                method="POST"
            )

            with urllib.request.urlopen(
                request,
                timeout=180
            ) as response:

                result = json.loads(
                    response.read().decode("utf-8")
                )

            text = (
                result["candidates"][0]
                ["content"]["parts"][0]["text"]
            )

            generated = json.loads(text)

            generated_scenes = generated.get(
                "scenes",
                []
            )

            if len(generated_scenes) != len(scenes):

                raise ValueError(
                    f"Expected {len(scenes)} scenes, "
                    f"received {len(generated_scenes)}"
                )

            expected_numbers = [
                scene["scene"]
                for scene in scenes
            ]

            actual_numbers = [
                scene.get("scene")
                for scene in generated_scenes
            ]

            if actual_numbers != expected_numbers:

                raise ValueError(
                    "Gemini returned incorrect scene numbers"
                )

            return generated_scenes

        except urllib.error.HTTPError as e:

            try:

                body = e.read().decode(
                    "utf-8",
                    errors="replace"
                )

            except Exception:

                body = ""

            last_error = (
                f"HTTP {e.code}: {body[:1000]}"
            )

            print(
                f"Part {part['part']} generation failed: "
                f"{last_error}"
            )

            if e.code not in (
                429,
                500,
                502,
                503,
                504
            ):

                break

            if attempt < max_retries:

                delay = min(
                    30,
                    3 * (2 ** (attempt - 1))
                )

                print(
                    f"Retrying in {delay} seconds..."
                )

                time.sleep(delay)

        except Exception as e:

            last_error = str(e)

            print(
                f"Part {part['part']} generation failed: "
                f"{e}"
            )

            if attempt < max_retries:

                delay = min(
                    30,
                    3 * (2 ** (attempt - 1))
                )

                print(
                    f"Retrying in {delay} seconds..."
                )

                time.sleep(delay)

    raise SystemExit(
        f"ERROR: Part {part['part']} failed after "
        f"{max_retries} attempts: {last_error}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:
        raise SystemExit(
            "ERROR: GEMINI_API_KEY is not set"
        )

    pipeline_config = build_config()

    model = load_selected_model()

    story = load_json(INPUT)

    input_config = load_input_config()

    topic = resolve_topic_value(input_config)
    story_text = resolve_story_text_value(input_config)

    if not topic:

        topic = str(
            story.get("topic", "")
        ).strip()

    if not topic and not story_text:

        raise SystemExit(
            "ERROR: Both TOPIC and STORY_TEXT are empty"
        )

    if not topic:

        topic = "Story generated from supplied STORY_TEXT"

    expected_parts = len(
        story.get("parts", [])
    )

    expected_scenes = sum(
        len(part.get("scenes", []))
        for part in story.get("parts", [])
    )

    if expected_parts < 1:

        raise SystemExit(
            "ERROR: No story parts found"
        )

    if expected_scenes < 1:

        raise SystemExit(
            "ERROR: No story scenes found"
        )

    print("==============================================")
    print("        GEMINI AI STORY GENERATION")
    print("==============================================")
    print(f"Selected model : {model}")
    print(f"Format         : {pipeline_config['format']}")
    print(f"Audience       : {pipeline_config['audience']}")
    print(f"Story length   : {pipeline_config['story_length']}")
    print(f"Scene duration : {pipeline_config['scene_duration']}")
    print(f"Topic          : {topic}")

    print(
        "Story text     : "
        f"{'provided' if story_text else 'not provided'}"
    )

    print(f"Parts          : {expected_parts}")
    print(f"Scenes         : {expected_scenes}")

    print(
        "Max retries    : "
        f"{pipeline_config['max_retries']}"
    )

    print("==============================================")


    ai_story = {
        "status": "generating",
        "topic": topic,
        "story_text": story_text,
        "format": pipeline_config["format"],
        "audience": pipeline_config["audience"],
        "story_length": pipeline_config["story_length"],
        "scene_duration": pipeline_config["scene_duration"],
        "model": model,
        "input_config": pipeline_config,
        "parts": []
    }


    # ========================================================
    # RESUME EXISTING AI STORY
    # ========================================================

    if OUTPUT.exists():

        try:

            existing = load_json(OUTPUT)

            same_source = (
                existing.get("topic") == topic
                and existing.get("story_text", "") == story_text
                and existing.get("format")
                == pipeline_config["format"]
            )

            if (
                same_source
                and existing.get("parts")
            ):

                ai_story = existing

                ai_story["model"] = model
                ai_story["input_config"] = pipeline_config

                print(
                    "Existing compatible AI story found. "
                    "Resuming..."
                )

        except Exception:

            print(
                "Existing AI story is invalid. "
                "Starting fresh."
            )


    # ========================================================
    # GENERATE PARTS
    # ========================================================

    for part in story.get("parts", []):

        part_number = part["part"]

        existing_part = next(
            (
                p
                for p in ai_story.get("parts", [])
                if p.get("part") == part_number
            ),
            None
        )

        expected_scene_count = len(
            part.get("scenes", [])
        )

        if (
            existing_part
            and existing_part.get("status") == "completed"
            and len(
                existing_part.get("scenes", [])
            ) == expected_scene_count
            and all(
                scene.get("status") == "completed"
                for scene in existing_part.get(
                    "scenes",
                    []
                )
            )
        ):

            print(
                f"Part {part_number} already completed. "
                "Skipping."
            )

            continue


        generated_scenes = generate_part(
            api_key,
            model,
            topic,
            story_text,
            part,
            pipeline_config
        )


        completed_part = {
            "part": part_number,
            "status": "completed",

            "hook_required": part.get(
                "hook_required",
                False
            ),

            "suspense_required": part.get(
                "suspense_required",
                False
            ),

            "final_resolution": part.get(
                "final_resolution",
                False
            ),

            "scenes": []
        }


        for scene in generated_scenes:

            source_scene = next(
                (
                    s
                    for s in part.get("scenes", [])
                    if s["scene"] == scene["scene"]
                ),
                {}
            )


            completed_part["scenes"].append({

                "scene": scene["scene"],

                "role": source_scene.get(
                    "role",
                    ""
                ),

                "status": "completed",

                "narration": scene["narration"],

                "dialogue": scene["dialogue"],

                "visual_prompt": scene[
                    "visual_prompt"
                ],

                "negative_prompt": scene[
                    "negative_prompt"
                ],

                "camera_prompt": scene[
                    "camera_prompt"
                ],

                "lighting_prompt": scene[
                    "lighting_prompt"
                ],

                "sfx_prompt": scene[
                    "sfx_prompt"
                ],

                "music_prompt": scene[
                    "music_prompt"
                ]
            })


        ai_story["parts"] = [
            p
            for p in ai_story.get("parts", [])
            if p.get("part") != part_number
        ]


        ai_story["parts"].append(
            completed_part
        )


        ai_story["parts"].sort(
            key=lambda p: p["part"]
        )


        ai_story["status"] = "in_progress"

        save_json(ai_story)

        print(
            f"Part {part_number} completed and saved."
        )


    # ========================================================
    # FINAL VALIDATION
    # ========================================================

    total_parts = len(
        ai_story.get("parts", [])
    )

    total_scenes = sum(
        len(part.get("scenes", []))
        for part in ai_story.get("parts", [])
    )


    if total_parts != expected_parts:

        raise SystemExit(
            f"ERROR: AI story parts mismatch: "
            f"{total_parts}/{expected_parts}"
        )


    if total_scenes != expected_scenes:

        raise SystemExit(
            f"ERROR: AI story scenes mismatch: "
            f"{total_scenes}/{expected_scenes}"
        )


    for part in ai_story["parts"]:

        if part.get("status") != "completed":

            raise SystemExit(
                f"ERROR: Part {part.get('part')} "
                "is not completed"
            )


        if not all(
            scene.get("status") == "completed"
            for scene in part.get("scenes", [])
        ):

            raise SystemExit(
                f"ERROR: Part {part.get('part')} "
                "contains incomplete scenes"
            )


    # ========================================================
    # FINAL SAVE
    # ========================================================

    ai_story["status"] = "completed"
    ai_story["model"] = model
    ai_story["topic"] = topic
    ai_story["story_text"] = story_text
    ai_story["format"] = pipeline_config["format"]
    ai_story["audience"] = pipeline_config["audience"]
    ai_story["story_length"] = pipeline_config["story_length"]
    ai_story["scene_duration"] = pipeline_config["scene_duration"]
    ai_story["input_config"] = pipeline_config

    save_json(ai_story)


    print("==============================================")
    print("       AI STORY GENERATION COMPLETED")
    print("==============================================")
    print(f"Topic        : {topic}")
    print(f"Format       : {pipeline_config['format']}")
    print(f"Parts        : {total_parts}")
    print(f"Scenes       : {total_scenes}")
    print(f"Model        : {model}")
    print(f"Output       : {OUTPUT}")
    print("==============================================")


if __name__ == "__main__":
    main()
