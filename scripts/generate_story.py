#!/usr/bin/env python3
"""Katha Lok AI story generation using the model selected by the workflow."""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output" / "story"
OUTPUT_FILE = OUTPUT_DIR / "story.json"
TITLE_FILE = OUTPUT_DIR / "final_title.txt"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MAX_RETRIES = 3


def get_selected_model():
    """Use the model selected and tested by select_gemini_model.py."""
    if MODEL_FILE.is_file():
        try:
            data = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
            model = str(data.get("model", "")).strip()
            status = str(data.get("status", "")).strip().lower()

            if model and status == "selected":
                print(f"Model source: {MODEL_FILE}")
                return model

            print(
                "WARNING: selected model file is incomplete; "
                "checking environment fallback."
            )
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARNING: cannot read selected model file: {exc}")

    fallback = os.environ.get("GEMINI_MODEL", "").strip()
    if fallback:
        print("Model source: GEMINI_MODEL environment fallback")
        return fallback

    raise RuntimeError(
        "No tested Gemini model is available. Run "
        "scripts/select_gemini_model.py before story generation."
    )


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=True):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()

    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0"}:
        return False

    return default


def clean_title(value):
    value = str(value or "").strip()
    value = re.sub(r"^['\"]+|['\"]+$", "", value)
    value = re.sub(
        r"^(title|शीर्षक)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip()


SCENE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "scene": {"type": "INTEGER"},
        "purpose": {"type": "STRING"},
        "narration": {"type": "STRING"},
        "visual": {"type": "STRING"},
        "dialogue": {"type": "STRING"},
        "suspense": {"type": "STRING"},
    },
    "required": [
        "scene",
        "purpose",
        "narration",
        "visual",
        "dialogue",
        "suspense",
    ],
}

PART_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "part": {"type": "INTEGER"},
        "title": {"type": "STRING"},
        "scenes": {
            "type": "ARRAY",
            "items": SCENE_SCHEMA,
        },
    },
    "required": ["part", "title", "scenes"],
}


def extract_json(response_text):
    text = str(response_text or "").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start < 0 or end <= start:
            raise RuntimeError("Gemini did not return valid JSON.")

        data = json.loads(text[start:end + 1])

    if not isinstance(data, dict):
        raise RuntimeError("Gemini JSON root must be an object.")

    return data


def call_gemini(model, prompt, response_schema):
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "temperature": 0.7,
            "topP": 0.9,
            "maxOutputTokens": 30000,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(
            url,
            data=json.dumps(
                payload,
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": API_KEY,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=180,
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

            candidates = result.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini returned no candidates.")

            candidate = candidates[0]
            finish_reason = str(
                candidate.get("finishReason", "")
            ).upper()

            if finish_reason in {"MAX_TOKENS", "LENGTH"}:
                raise RuntimeError("Gemini output was truncated.")

            response_parts = candidate.get(
                "content", {}
            ).get("parts", [])

            text = "\n".join(
                item.get("text", "")
                for item in response_parts
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
            ).strip()

            if not text:
                raise RuntimeError("Gemini returned empty text.")

            return text

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )
            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body[:1200]}"
            )

            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error

        except Exception as exc:
            last_error = exc

        if attempt < MAX_RETRIES:
            delay = min(10 * (2 ** (attempt - 1)), 40)
            print(
                f"Attempt {attempt} failed: {last_error}. "
                f"Retrying in {delay}s."
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Gemini failed after {MAX_RETRIES} attempts: {last_error}"
    )


def make_prompt(
    config,
    title,
    topic,
    story_text,
    format_type,
    parts_count,
    scenes_per_part,
):
    audience = cfg_text(config, "AUDIENCE", "adult")
    story_length = cfg_text(config, "STORY_LENGTH", "auto")
    scene_duration = cfg_text(config, "SCENE_DURATION", "auto")

    part_hook = cfg_bool(config, "PART_HOOK", True)
    part_suspense = cfg_bool(config, "PART_SUSPENSE", True)
    final_resolution = cfg_bool(config, "FINAL_RESOLUTION", True)

    hook_rule = (
        "The first scene of EVERY part must begin with a strong "
        "Hindi story hook: mystery, danger, surprise, emotional "
        "tension or an intriguing action. Start inside the moment."
        if part_hook else
        "Use natural openings appropriate to the story."
    )

    suspense_rule = (
        "The last scene of EVERY part must end with meaningful "
        "story-specific suspense, a new clue, reversal, approaching "
        "danger or unanswered question. Do not repeat cliffhangers."
        if part_suspense else
        "Use suspense only where it serves the story."
    )

    resolution_rule = (
        "In the final part, resolve the central conflict BEFORE "
        "the last suspense beat. Then introduce a new, relevant "
        "clue or mystery without cancelling the main payoff."
        if final_resolution and part_suspense else
        "Give the story a deliberate, satisfying ending."
    )

    if format_type == "short":
        format_rule = (
            "Create a short-form story, not a compressed long story. "
            "Move quickly from hook to conflict, escalation, reveal "
            "and payoff. Remove filler."
        )
    else:
        format_rule = (
            "Create a complete long-form cinematic story with "
            "character development, setup, rising conflict, turning "
            "points, climax and meaningful resolution."
        )

    return f"""
You are a professional Hindi cinematic storyteller.

TITLE: {title}
INPUT TOPIC: {topic or "(No topic supplied)"}
USER STORY TEXT: {story_text or "(Develop from the topic)"}
FORMAT: {format_type}
AUDIENCE: {audience}
STORY LENGTH: {story_length}
TARGET SCENE DURATION: {scene_duration}

FORMAT REQUIREMENTS:
{format_rule}

OPENING HOOK:
{hook_rule}

PART ENDINGS:
{suspense_rule}

FINAL ENDING:
{resolution_rule}

STRUCTURE:
- Exactly {parts_count} parts.
- Exactly {scenes_per_part} scenes in each part.
- Exactly {parts_count * scenes_per_part} scenes overall.
- Scene numbering restarts at 1 in each part.

STORY QUALITY:
- Write natural, clear Hindi narration.
- Make dialogue believable and character-specific.
- Every scene must advance the plot or reveal character.
- Maintain character, location and timeline continuity.
- Keep cause and effect logical.
- Use supplied story text as the primary source when provided.
- Do not replace the supplied story with an unrelated story.
- Use realistic live-action cinematic visual descriptions.
- Describe concrete actions, locations, expressions and atmosphere.
- Do not describe cartoon, anime, comic or slideshow aesthetics.
- Do not start with a generic channel introduction.
- Make the story title and plot relevant to the input topic.
- The suspense must be earned by the preceding events.
- The final part must pay off the main conflict before its final clue.

Return ONLY valid JSON matching this structure:
{{
  "status": "completed",
  "format": "{format_type}",
  "title": "Story title",
  "topic": "Input topic",
  "story_type": "original_story",
  "hook": "Opening hook in Hindi",
  "ending_type": "payoff_with_final_suspense",
  "parts": [
    {{
      "part": 1,
      "title": "Part title in Hindi",
      "scenes": [
        {{
          "scene": 1,
          "purpose": "Scene purpose",
          "narration": "Hindi narration",
          "visual": "Realistic cinematic visual description",
          "dialogue": "Hindi dialogue or empty string",
          "suspense": "Suspense beat or empty string"
        }}
      ]
    }}
  ]
}}

Do not return markdown or explanations.
"""


def validate_story(
    data,
    format_type,
    title,
    topic,
    config,
    model,
    parts_count,
    scenes_per_part,
):
    if str(data.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story format does not match input FORMAT.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        actual = len(parts) if isinstance(parts, list) else "invalid"
        raise RuntimeError(
            f"Expected {parts_count} parts; received {actual}."
        )

    part_hook = cfg_bool(config, "PART_HOOK", True)
    part_suspense = cfg_bool(config, "PART_SUSPENSE", True)
    final_resolution = cfg_bool(config, "FINAL_RESOLUTION", True)

    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Part {part_index} is not an object.")

        scenes = part.get("scenes")
        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Part {part_index} must contain exactly "
                f"{scenes_per_part} scenes."
            )

        part["part"] = part_index
        part["title"] = str(
            part.get("title") or f"भाग {part_index}"
        ).strip()

        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {part_index}, scene {scene_index} is invalid."
                )

            scene["scene"] = scene_index

            for field in (
                "purpose",
                "narration",
                "visual",
                "dialogue",
                "suspense",
            ):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(
                        f"Part {part_index}, scene {scene_index}: "
                        f"{field} must be a string."
                    )
                scene[field] = value.strip()

            if not scene["narration"]:
                raise RuntimeError(
                    f"Missing narration in part {part_index}, "
                    f"scene {scene_index}."
                )

            if not scene["visual"]:
                raise RuntimeError(
                    f"Missing visual in part {part_index}, "
                    f"scene {scene_index}."
                )

            if scene_index == 1 and part_hook:
                if not (
                    scene["narration"] or scene["dialogue"]
                ):
                    raise RuntimeError(
                        f"Part {part_index} has no opening content."
                    )

            if (
                scene_index == scenes_per_part
                and part_suspense
                and not scene["suspense"]
            ):
                raise RuntimeError(
                    f"Part {part_index} final scene is missing "
                    "its required suspense beat."
                )

    hook = str(data.get("hook", "")).strip()
    if part_hook and not hook:
        raise RuntimeError("The story-level opening hook is missing.")

    data["status"] = "completed"
    data["format"] = format_type
    data["title"] = title
    data["topic"] = topic
    data["story_type"] = (
        "original_short_form"
        if format_type == "short"
        else "long_form"
    )
    data["ending_type"] = str(
        data.get("ending_type") or "payoff_with_final_suspense"
    ).strip()

    data["_generation"] = {
        "model": model,
        "format": format_type,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": parts_count * scenes_per_part,
        "opening_hook_required": part_hook,
        "part_suspense_required": part_suspense,
        "final_resolution_required": final_resolution,
        "final_suspense_required": part_suspense,
        "story_length": cfg_text(config, "STORY_LENGTH", "auto"),
        "scene_duration": cfg_text(config, "SCENE_DURATION", "auto"),
    }

    return data


def main():
    print("=" * 60)
    print("KATHA LOK AI - STORY GENERATION")
    print("=" * 60)

    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    model = get_selected_model()
    config = load_and_validate()
    format_type = get_format(config)
    topic = get_topic(config)
    story_text = get_story_text(config)
    parts_count = get_parts(config)
    scenes_per_part = get_scenes(config)

    if topic:
        title = clean_title(topic)
    elif story_text:
        title = clean_title(story_text.splitlines()[0])[:100]
    else:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    if not title:
        raise RuntimeError("Could not determine a title.")

    print(f"Model           : {model}")
    print(f"Format          : {format_type}")
    print(f"Title           : {title}")
    print(f"Audience        : {cfg_text(config, 'AUDIENCE', 'adult')}")
    print(f"Parts           : {parts_count}")
    print(f"Scenes per part : {scenes_per_part}")
    print(f"Total scenes    : {parts_count * scenes_per_part}")
    print(f"Opening hook    : {cfg_bool(config, 'PART_HOOK', True)}")
    print(f"Part suspense   : {cfg_bool(config, 'PART_SUSPENSE', True)}")
    print(f"Final resolution: {cfg_bool(config, 'FINAL_RESOLUTION', True)}")

    schema = {
        "type": "OBJECT",
        "properties": {
            "status": {"type": "STRING"},
            "format": {"type": "STRING"},
            "title": {"type": "STRING"},
            "topic": {"type": "STRING"},
            "story_type": {"type": "STRING"},
            "hook": {"type": "STRING"},
            "ending_type": {"type": "STRING"},
            "parts": {
                "type": "ARRAY",
                "items": PART_SCHEMA,
            },
        },
        "required": [
            "status",
            "format",
            "title",
            "topic",
            "story_type",
            "hook",
            "ending_type",
            "parts",
        ],
    }

    prompt = make_prompt(
        config,
        title,
        topic,
        story_text,
        format_type,
        parts_count,
        scenes_per_part,
    )

    response = call_gemini(model, prompt, schema)
    data = extract_json(response)

    data = validate_story(
        data,
        format_type,
        title,
        topic,
        config,
        model,
        parts_count,
        scenes_per_part,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    temporary_file = OUTPUT_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_file.replace(OUTPUT_FILE)

    TITLE_FILE.write_text(title + "\n", encoding="utf-8")

    print("=" * 60)
    print("STORY GENERATION SUCCESS")
    print(f"Story file  : {OUTPUT_FILE}")
    print(f"Title file  : {TITLE_FILE}")
    print(f"Model used  : {model}")
    print(f"Parts       : {parts_count}")
    print(f"Total scenes: {parts_count * scenes_per_part}")
    print("JSON validation: PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)