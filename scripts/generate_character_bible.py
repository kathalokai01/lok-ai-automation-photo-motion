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


INPUT = Path("output/story/ai_story.json")
OUTPUT = Path("output/story/character_bible.json")
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


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    if not path.exists():
        raise SystemExit(
            f"ERROR: File not found: {path}"
        )

    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        raise SystemExit(
            f"ERROR: Could not read JSON file "
            f"{path}: {e}"
        )


def save_json(data):
    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = OUTPUT.with_suffix(".tmp")

    with temp.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

        f.write("\n")

    temp.replace(OUTPUT)


# ============================================================
# MODEL
# ============================================================

def load_selected_model():

    config = load_json(
        MODEL_FILE
    )

    if config.get("status") != "selected":
        raise SystemExit(
            "ERROR: Gemini model selection "
            "is not in selected state"
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
        "format": normalize_format(
            config
        ),

        "audience": cfg_text(
            config,
            "AUDIENCE",
            "adult"
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

        "quality": cfg_text(
            config,
            "QUALITY",
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

        "realistic_lighting": cfg_bool(
            config,
            "REALISTIC_LIGHTING",
            True
        ),

        "mood": cfg_text(
            config,
            "MOOD",
            "dramatic"
        ),

        "natural_motion": cfg_bool(
            config,
            "NATURAL_MOTION",
            True
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

        "max_retries": get_max_retries(
            config
        ),
    }


# ============================================================
# STORY CONTEXT
# ============================================================

def build_story_context(story):

    context = []

    for part in story.get(
        "parts",
        []
    ):

        part_number = part.get(
            "part"
        )

        for scene in part.get(
            "scenes",
            []
        ):

            context.append({

                "part": part_number,

                "scene": scene.get(
                    "scene"
                ),

                "role": scene.get(
                    "role",
                    ""
                ),

                "narration": scene.get(
                    "narration",
                    ""
                ),

                "dialogue": scene.get(
                    "dialogue",
                    ""
                ),

                "visual_prompt": scene.get(
                    "visual_prompt",
                    ""
                ),
            })

    return context


# ============================================================
# STRUCTURED OUTPUT SCHEMA
# ============================================================

def character_schema():

    return {
        "type": "object",

        "properties": {

            "characters": {

                "type": "array",

                "items": {

                    "type": "object",

                    "properties": {

                        "character_id": {
                            "type": "string"
                        },

                        "name": {
                            "type": "string"
                        },

                        "role": {
                            "type": "string"
                        },

                        "importance": {
                            "type": "string"
                        },

                        "age": {
                            "type": "string"
                        },

                        "gender": {
                            "type": "string"
                        },

                        "personality": {
                            "type": "string"
                        },

                        "appearance": {
                            "type": "string"
                        },

                        "face_features": {
                            "type": "string"
                        },

                        "skin_tone": {
                            "type": "string"
                        },

                        "hair": {
                            "type": "string"
                        },

                        "eyes": {
                            "type": "string"
                        },

                        "body_features": {
                            "type": "string"
                        },

                        "clothing": {
                            "type": "string"
                        },

                        "distinctive_features": {
                            "type": "string"
                        },

                        "visual_identity": {
                            "type": "string"
                        },

                        "continuity_notes": {
                            "type": "string"
                        },

                        "consistency_rules": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            }
                        }
                    },

                    "required": [
                        "character_id",
                        "name",
                        "role",
                        "importance",
                        "age",
                        "gender",
                        "personality",
                        "appearance",
                        "face_features",
                        "skin_tone",
                        "hair",
                        "eyes",
                        "body_features",
                        "clothing",
                        "distinctive_features",
                        "visual_identity",
                        "continuity_notes",
                        "consistency_rules"
                    ]
                }
            },

            "world": {

                "type": "object",

                "properties": {

                    "setting": {
                        "type": "string"
                    },

                    "time_period": {
                        "type": "string"
                    },

                    "geography": {
                        "type": "string"
                    },

                    "architecture": {
                        "type": "string"
                    },

                    "environment": {
                        "type": "string"
                    },

                    "weather_style": {
                        "type": "string"
                    },

                    "color_and_lighting": {
                        "type": "string"
                    },

                    "continuity_rules": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        }
                    }
                },

                "required": [
                    "setting",
                    "time_period",
                    "geography",
                    "architecture",
                    "environment",
                    "weather_style",
                    "color_and_lighting",
                    "continuity_rules"
                ]
            }
        },

        "required": [
            "characters",
            "world"
        ]
    }


# ============================================================
# GEMINI CHARACTER BIBLE GENERATION
# ============================================================

def generate_character_bible(
    api_key,
    model,
    story,
    source_topic,
    source_story_text,
    pipeline_config
):

    story_context = build_story_context(
        story
    )

    prompt = f"""
You are the CHARACTER AND WORLD CONSISTENCY AI
for a realistic cinematic Hindi AI video pipeline.

Create a production-ready character bible and world bible
for the COMPLETE STORY.

SOURCE TOPIC:
{source_topic}

SOURCE STORY TEXT:
{
    source_story_text
    if source_story_text
    else "[No separate STORY_TEXT was supplied.]"
}

FORMAT:
{pipeline_config["format"]}

AUDIENCE:
{pipeline_config["audience"]}

VISUAL STYLE:
{pipeline_config["visual_style"]}

REALISM:
{pipeline_config["realism"]}

QUALITY:
{pipeline_config["quality"]}

CAMERA STYLE:
{pipeline_config["camera_style"]}

LIGHTING:
{pipeline_config["lighting"]}

MOOD:
{pipeline_config["mood"]}

STORY SCENES:
{json.dumps(
    story_context,
    ensure_ascii=False,
    indent=2
)}

==================================================
SOURCE FIDELITY
==================================================

The supplied STORY_TEXT and generated story are the source
of truth.

Do not replace the story with a different story.

Identify characters from:

1. STORY_TEXT
2. Narration
3. Dialogue
4. Existing visual prompts

If the same person appears under different descriptions,
combine them into ONE stable character.

Do not create unnecessary characters.

If an important character has no explicit name,
create a stable descriptive name.

==================================================
REAL PERSON / REAL WORLD REQUIREMENT
==================================================

Characters must be suitable for photorealistic
live-action generation.

Use:

- realistic human anatomy
- realistic skin texture
- believable facial structure
- believable hair
- believable clothing
- natural body proportions
- realistic age appearance
- realistic environments

Do NOT create:

- cartoon characters
- anime characters
- comic characters
- illustration characters
- game characters
- plastic-looking people
- exaggerated facial features
- fantasy-looking humans unless the SOURCE STORY
  explicitly requires them

==================================================
CHARACTER CONSISTENCY
==================================================

CHARACTER CONSISTENCY ENABLED:
{pipeline_config["character_consistency"]}

If enabled, every recurring character MUST retain:

- same character_id
- same face identity
- same approximate age
- same skin tone
- same hair identity
- same eye characteristics
- same body structure
- same distinctive features
- same general clothing identity

Do NOT randomly redesign a recurring character.

Clothing may change ONLY when the story logically
requires a change.

If clothing changes, the change must be explainable
by the story.

==================================================
WORLD CONSISTENCY
==================================================

WORLD CONSISTENCY ENABLED:
{pipeline_config["world_consistency"]}

Define a stable world identity.

Keep consistent:

- geographical environment
- architecture
- locations
- historical/modern setting
- weather logic
- environmental appearance
- lighting logic
- overall visual atmosphere

Do not randomly move the story to another location.

==================================================
SCENE CONTINUITY
==================================================

SCENE CONTINUITY ENABLED:
{pipeline_config["scene_continuity"]}

The bible must provide information that allows later
scene generation to preserve continuity.

==================================================
CAMERA / LIGHTING
==================================================

CINEMATIC CAMERA:
{pipeline_config["cinematic_camera"]}

CAMERA STYLE:
{pipeline_config["camera_style"]}

LIGHTING:
{pipeline_config["lighting"]}

REALISTIC LIGHTING:
{pipeline_config["realistic_lighting"]}

NATURAL MOTION:
{pipeline_config["natural_motion"]}

These settings should influence the visual identity
and continuity guidance.

==================================================
NEGATIVE VISUAL RULES
==================================================

NEGATIVE PROMPT ENABLED:
{pipeline_config["negative_prompt"]}

AVOID CARTOON:
{pipeline_config["avoid_cartoon"]}

AVOID NEON:
{pipeline_config["avoid_neon"]}

AVOID GLITCH:
{pipeline_config["avoid_glitch"]}

When enabled, consistency rules must explicitly help
prevent these visual problems.

==================================================
CHARACTER IDENTIFICATION
==================================================

Every important recurring character must receive:

CHAR_001
CHAR_002
CHAR_003

etc.

Never create two IDs for the same person.

Each character must have a strong VISUAL_IDENTITY
string that can later be inserted directly into
an image-generation prompt.

The visual_identity should combine the most important
stable traits into one concise production description.

==================================================
WORLD BIBLE
==================================================

Create one stable WORLD description for the story.

It must describe:

- setting
- time period
- geography
- architecture
- environment
- weather style
- color and lighting identity
- continuity rules

Do not invent unnecessary world details that contradict
the story.

==================================================
IMPORTANT
==================================================

- Preserve source-story facts.
- Do not invent major characters.
- Do not change character relationships.
- Do not change important locations.
- Do not contradict the story.
- Do not use cartoon/anime/comic terminology.
- Visual fields must be written in English.
- Personality and role can be concise.
- Return ONLY valid JSON.
- Return no markdown.
- Return no explanation.
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
            "responseSchema": character_schema()
        }
    }

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    last_error = None

    max_retries = pipeline_config[
        "max_retries"
    ]

    for attempt in range(
        1,
        max_retries + 1
    ):

        try:

            print(
                "Generating Character + World Bible "
                f"(attempt {attempt}/{max_retries})..."
            )

            request = urllib.request.Request(
                api_url,
                data=json.dumps(
                    payload
                ).encode("utf-8"),

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
                    response.read().decode(
                        "utf-8"
                    )
                )

            candidates = result.get(
                "candidates",
                []
            )

            if not candidates:

                raise ValueError(
                    "Gemini returned no candidates"
                )

            parts = (
                candidates[0]
                .get("content", {})
                .get("parts", [])
            )

            if not parts:

                raise ValueError(
                    "Gemini returned no response parts"
                )

            text = parts[0].get(
                "text",
                ""
            ).strip()

            if not text:

                raise ValueError(
                    "Gemini returned empty response"
                )

            generated = json.loads(
                text
            )

            characters = generated.get(
                "characters"
            )

            world = generated.get(
                "world"
            )

            if not isinstance(
                characters,
                list
            ):

                raise ValueError(
                    "Invalid characters array"
                )

            if not isinstance(
                world,
                dict
            ):

                raise ValueError(
                    "Invalid world object"
                )

            seen_ids = set()

            required_fields = [
                "character_id",
                "name",
                "role",
                "importance",
                "age",
                "gender",
                "personality",
                "appearance",
                "face_features",
                "skin_tone",
                "hair",
                "eyes",
                "body_features",
                "clothing",
                "distinctive_features",
                "visual_identity",
                "continuity_notes",
                "consistency_rules"
            ]

            for character in characters:

                for field in required_fields:

                    if field not in character:

                        raise ValueError(
                            "Character missing field: "
                            f"{field}"
                        )

                char_id = str(
                    character["character_id"]
                ).strip()

                if not char_id:

                    raise ValueError(
                        "Empty character_id"
                    )

                if char_id in seen_ids:

                    raise ValueError(
                        "Duplicate character_id: "
                        f"{char_id}"
                    )

                seen_ids.add(
                    char_id
                )

                if not isinstance(
                    character[
                        "consistency_rules"
                    ],
                    list
                ):

                    raise ValueError(
                        "Invalid consistency_rules "
                        f"for {char_id}"
                    )

            world_required = [
                "setting",
                "time_period",
                "geography",
                "architecture",
                "environment",
                "weather_style",
                "color_and_lighting",
                "continuity_rules"
            ]

            for field in world_required:

                if field not in world:

                    raise ValueError(
                        "World missing field: "
                        f"{field}"
                    )

            if not isinstance(
                world["continuity_rules"],
                list
            ):

                raise ValueError(
                    "Invalid world continuity_rules"
                )

            return generated

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
                "Character Bible request failed: "
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
                    3 * (
                        2 ** (
                            attempt - 1
                        )
                    )
                )

                print(
                    f"Retrying in {delay} seconds..."
                )

                time.sleep(
                    delay
                )

        except Exception as e:

            last_error = str(e)

            print(
                "Character Bible generation failed: "
                f"{e}"
            )

            if attempt < max_retries:

                delay = min(
                    30,
                    3 * (
                        2 ** (
                            attempt - 1
                        )
                    )
                )

                print(
                    f"Retrying in {delay} seconds..."
                )

                time.sleep(
                    delay
                )

    raise SystemExit(
        "ERROR: Character Bible generation failed "
        f"after {max_retries} attempts: "
        f"{last_error}"
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

    input_config = load_input_config()

    source_topic = get_topic(
        input_config
    )

    source_story_text = get_story_text(
        input_config
    )

    story = load_json(
        INPUT
    )

    if story.get("status") != "completed":

        raise SystemExit(
            "ERROR: AI story is not completed"
        )

    if not source_topic:

        source_topic = str(
            story.get(
                "topic",
                ""
            )
        ).strip()

    if not source_story_text:

        source_story_text = str(
            story.get(
                "story_text",
                ""
            )
        ).strip()

    if not source_topic and not source_story_text:

        raise SystemExit(
            "ERROR: No topic or story text available"
        )

    print(
        "=============================================="
    )

    print(
        "       CHARACTER / WORLD BIBLE"
    )

    print(
        "=============================================="
    )

    print(
        f"Model              : {model}"
    )

    print(
        f"Format             : "
        f"{pipeline_config['format']}"
    )

    print(
        f"Audience           : "
        f"{pipeline_config['audience']}"
    )

    print(
        "Character Bible     : "
        f"{pipeline_config['character_bible']}"
    )

    print(
        "Character consistency: "
        f"{pipeline_config['character_consistency']}"
    )

    print(
        "World consistency   : "
        f"{pipeline_config['world_consistency']}"
    )

    print(
        "Scene continuity    : "
        f"{pipeline_config['scene_continuity']}"
    )

    print(
        "Visual style        : "
        f"{pipeline_config['visual_style']}"
    )

    print(
        "Realism             : "
        f"{pipeline_config['realism']}"
    )

    print(
        f"Topic               : {source_topic}"
    )

    print(
        "Story text          : "
        f"{'provided' if source_story_text else 'not provided'}"
    )

    print(
        "=============================================="
    )


    # ========================================================
    # CHARACTER_BIBLE DISABLED
    # ========================================================

    if not pipeline_config[
        "character_bible"
    ]:

        disabled_output = {

            "status": "disabled",

            "topic": source_topic,

            "model": model,

            "characters": [],

            "world": {

                "setting": "",

                "time_period": "",

                "geography": "",

                "architecture": "",

                "environment": "",

                "weather_style": "",

                "color_and_lighting": "",

                "continuity_rules": []
            },

            "input_config": pipeline_config
        }

        save_json(
            disabled_output
        )

        print(
            "CHARACTER_BIBLE=false"
        )

        print(
            "Character Bible generation skipped."
        )

        return


    # ========================================================
    # RESUME
    # ========================================================

    if OUTPUT.exists():

        try:

            existing = load_json(
                OUTPUT
            )

            if (
                existing.get(
                    "status"
                ) == "completed"

                and existing.get(
                    "topic"
                ) == source_topic

                and isinstance(
                    existing.get(
                        "characters"
                    ),
                    list
                )

                and isinstance(
                    existing.get(
                        "world"
                    ),
                    dict
                )
            ):

                print(
                    "Existing compatible Character "
                    "Bible found. Skipping regeneration."
                )

                existing["model"] = model

                existing["input_config"] = (
                    pipeline_config
                )

                save_json(
                    existing
                )

                print(
                    "Characters: "
                    f"{len(existing['characters'])}"
                )

                return

        except Exception:

            print(
                "Existing Character Bible is invalid. "
                "Starting fresh."
            )


    # ========================================================
    # GENERATE
    # ========================================================

    generated = generate_character_bible(
        api_key,
        model,
        story,
        source_topic,
        source_story_text,
        pipeline_config
    )

    characters = generated.get(
        "characters",
        []
    )

    world = generated.get(
        "world",
        {}
    )

    result = {

        "status": "completed",

        "topic": source_topic,

        "model": model,

        "format": pipeline_config[
            "format"
        ],

        "visual_style": pipeline_config[
            "visual_style"
        ],

        "realism": pipeline_config[
            "realism"
        ],

        "characters": characters,

        "world": world,

        "input_config": pipeline_config
    }

    save_json(
        result
    )

    print(
        "=============================================="
    )

    print(
        "   CHARACTER / WORLD BIBLE COMPLETED"
    )

    print(
        "=============================================="
    )

    print(
        f"Topic      : {source_topic}"
    )

    print(
        f"Model      : {model}"
    )

    print(
        f"Characters : {len(characters)}"
    )

    print(
        "World      : "
        f"{'created' if world else 'missing'}"
    )

    print(
        f"Output     : {OUTPUT}"
    )

    print(
        "=============================================="
    )


if __name__ == "__main__":
    main()
