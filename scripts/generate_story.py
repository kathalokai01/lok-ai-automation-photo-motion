#!/usr/bin/env python3

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

OUTPUT_DIR = Path("output/story")
OUTPUT_FILE = OUTPUT_DIR / "story.json"
TITLE_FILE = OUTPUT_DIR / "final_title.txt"

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite",
).strip()

MAX_RETRIES = 3


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
    value = re.sub(r"^(title|शीर्षक)\s*:\s*", "", value,
                   flags=re.IGNORECASE)
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


def call_gemini(prompt, response_schema):
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{MODEL}:generateContent"
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
                raise RuntimeError(
                    "Gemini returned no candidates."
                )

            candidate = candidates[0]
            finish_reason = str(
                candidate.get("finishReason", "")
            ).upper()

            if finish_reason in {"MAX_TOKENS", "LENGTH"}:
                raise RuntimeError(
                    "Gemini output was truncated."
                )

            content = candidate.get("content", {})
            response_parts = content.get("parts", [])

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

            # Authentication, invalid model and other permanent
            # errors should not be retried repeatedly.
            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error

        except Exception as exc:
            last_error = exc

        if attempt < MAX_RETRIES:
            delay = min(10 * (2 ** (attempt - 1)), 40)
            print(
                f"Generation attempt {attempt} failed: "
                f"{last_error}. Retrying in {delay}s."
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
        "hook: mystery, danger, surprise, emotional tension, or "
        "an intriguing action. Start inside the moment; no long intro."
        if part_hook else
        "Use natural openings without forcing an artificial hook."
    )

    suspense_rule = (
        "Every part must end on meaningful suspense, an unanswered "
        "question, a new clue, a reversal, or an approaching danger. "
        "Do not repeat the same cliffhanger."
        if part_suspense else
        "Use suspense only when it naturally serves the story."
    )

    resolution_rule = (
        "In the final part, resolve the central conflict BEFORE "
        "the final suspense beat. Then end with a new, relevant "
        "mystery, unexpected clue, or unanswered question. "
        "The final suspense must not erase the main payoff."
        if final_resolution and part_suspense else
        "Give the ending a deliberate, story-appropriate payoff."
    )

    if format_type == "short":
        format_rule = """
Write an original short-form story, not a compressed summary
of a long video. Move quickly from hook to conflict, escalation,
reveal and payoff. Remove filler and unnecessary exposition.
"""
    else:
        format_rule = """
Write a complete long-form cinematic story with character
development, setup, rising conflict, turning points, climax
and a meaningful resolution. Do not rush important events.
"""

    return f"""
You are a professional Hindi cinematic storyteller.

FINAL TITLE:
{title}

INPUT TOPIC:
{topic or "(No topic supplied)"}

USER STORY TEXT:
{story_text or "(No story text supplied; develop the story from the topic.)"}

FORMAT:
{format_type}

AUDIENCE:
{audience}

STORY LENGTH:
{story_length}

TARGET SCENE DURATION:
{scene_duration}

REQUIRED STRUCTURE:
- Exactly {parts_count} parts.
- Exactly {scenes_per_part} scenes in each part.
- Exactly {parts_count * scenes_per_part} scenes overall.

FORMAT INSTRUCTIONS:
{format_rule}

OPENING HOOK:
{hook_rule}

PART ENDINGS:
{suspense_rule}

FINAL ENDING:
{resolution_rule}

STORY QUALITY:
- Write natural, clear Hindi narration.
- Keep dialogue believable and character-specific.
- Make every scene advance the plot or reveal character.
- Maintain character, location and timeline continuity.
- Keep cause and effect logical between scenes.
- Avoid repeated exposition and filler.
- Do not invent a different story when user story text is supplied.
- Use the topic and supplied story text as the primary source.
- Use realistic live-action cinematic visual descriptions.
- Describe concrete actions, locations, expressions, and atmosphere.
- Do not describe cartoon, anime, comic, or slideshow aesthetics.
- Do not use a generic greeting or channel introduction as the hook.

SCENE RULES:
- Scene numbers restart at 1 in every part.
- Every scene must have a purpose.
- Scene 1 of each part must deliver its hook when hooks are enabled.
- The final scene of each non-final part must create a reason to continue.
- In the final part, the central conflict must be paid off before
  its last suspense beat, when those options are enabled.
- Suspense must connect to this story, not be random.
- The "suspense" field describes the suspense beat or its absence.
- Do not promise a reveal that the story never delivers.

Return ONLY valid JSON matching this structure:
{{
  "status": "completed",
  "format": "{format_type}",
  "title": "The final title",
  "topic": "The input topic",
  "story_type": "original_story",
  "hook": "The opening hook in Hindi",
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
    config,
    format_type,
    title,
    topic,
    parts_count,
    scenes_per_part,
):
    if str(data.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story format does not match the input FORMAT.")

    parts = data.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(
            f"Expected {parts_count} parts; received "
            f"{len(parts) if isinstance(parts, list) else 'invalid data'}."
        )

    part_hook = cfg_bool(config, "PART_HOOK", True)
    part_suspense = cfg_bool(config, "PART_SUSPENSE", True)
    final_resolution = cfg_bool(config, "FINAL_RESOLUTION", True)

    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Part {part_index} is not an object.")

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
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
                if not scene["narration"] and not scene["dialogue"]:
                    raise RuntimeError(
                        f"Part {part_index} has no opening hook content."
                    )

            if scene_index == scenes_per_part and part_suspense:
                if not scene["suspense"]:
                    raise RuntimeError(
                        f"Part {part_index}, final scene is missing "
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
        "model": MODEL,
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

    config = load_and_validate()
    format_type = get_format(config)
    topic = get_topic(config)
    story_text = get_story_text(config)
    parts_count = get_parts(config)
    scenes_per_part = get_scenes(config)

    if topic:
        title = clean_title(topic)
    elif story_text:
        # The title is story-specific; do not invent a new topic.
        title = clean_title(story_text.splitlines()[0])[:100]
    else:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    if not title:
        raise RuntimeError("Could not determine a title.")

    print(f"Model          : {MODEL}")
    print(f"Format         : {format_type}")
    print(f"Title          : {title}")
    print(f"Audience       : {cfg_text(config, 'AUDIENCE', 'adult')}")
    print(f"Parts          : {parts_count}")
    print(f"Scenes/part    : {scenes_per_part}")
    print(f"Total scenes   : {parts_count * scenes_per_part}")
    print(f"Opening hook   : {cfg_bool(config, 'PART_HOOK', True)}")
    print(f"Part suspense  : {cfg_bool(config, 'PART_SUSPENSE', True)}")
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

    response = call_gemini(prompt, schema)
    data = extract_json(response)

    data = validate_story(
        data,
        config,
        format_type,
        title,
        topic,
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
    print(f"Story file     : {OUTPUT_FILE}")
    print(f"Title file     : {TITLE_FILE}")
    print(f"Parts          : {parts_count}")
    print(f"Total scenes   : {parts_count * scenes_per_part}")
    print("JSON validation: PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)