#!/usr/bin/env python3
"""Generate and validate a topic-specific Hindi cinematic story."""

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
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "output" / "story"
PLAN_FILE = STORY_DIR / "story_plan.json"
OUTPUT_FILE = STORY_DIR / "story.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MAX_RETRIES = 3


def read_json(path, required=False):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {exc}") from exc


def selected_model():
    data = read_json(MODEL_FILE, required=True)

    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "No selected Gemini model. Run scripts/select_gemini_model.py first."
        )

    model = str(data.get("model", "")).strip()

    if not model or model.startswith("models/"):
        raise RuntimeError(f"Invalid selected Gemini model: {model!r}")

    print(f"Selected model: {model}")
    return model


def text_value(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def bool_value(config, key, default=True):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()

    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0"}:
        return False

    return default


def normalize_text(value):
    value = str(value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value):
    return len(normalize_text(value).split())


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


def load_plan(format_type, topic, parts_count, scenes_per_part):
    plan = read_json(PLAN_FILE)

    if plan is None:
        print("WARNING: Story plan not found; using input settings.")
        return None

    if not isinstance(plan, dict):
        raise RuntimeError("Story plan root must be a JSON object.")

    if str(plan.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story plan format does not match Input/topic.txt.")

    plan_topic = str(plan.get("topic", "")).strip()
    if topic and plan_topic and plan_topic != topic:
        raise RuntimeError(
            "Story plan topic does not match current input. "
            "Regenerate the story plan first."
        )

    try:
        plan_parts_count = int(plan.get("parts_count", -1))
        plan_scenes_count = int(plan.get("scenes_per_part", -1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Story plan counts are invalid.") from exc

    if plan_parts_count != parts_count:
        raise RuntimeError("Story plan part count does not match input.")

    if plan_scenes_count != scenes_per_part:
        raise RuntimeError("Story plan scene count does not match input.")

    plan_parts = plan.get("parts")
    if not isinstance(plan_parts, list) or len(plan_parts) != parts_count:
        raise RuntimeError("Story plan contains an invalid number of parts.")

    for part_index, part in enumerate(plan_parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Story plan part {part_index} is invalid.")

        try:
            part_number = int(part.get("part", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Story plan part numbering is invalid.") from exc

        if part_number != part_index:
            raise RuntimeError("Story plan part numbering is not continuous.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Story plan part {part_index} must contain "
                f"{scenes_per_part} scenes."
            )

        for scene_index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise RuntimeError("Story plan contains an invalid scene.")

            try:
                scene_number = int(scene.get("scene", -1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Story plan scene numbering is invalid.") from exc

            if scene_number != scene_index:
                raise RuntimeError(
                    f"Part {part_index} scene numbering is invalid."
                )

    print(f"Story plan loaded: {PLAN_FILE}")
    return plan


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
        "scene", "purpose", "narration",
        "visual", "dialogue", "suspense",
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

RESPONSE_SCHEMA = {
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
        "status", "format", "title", "topic",
        "story_type", "hook", "ending_type", "parts",
    ],
}


def call_gemini(model, prompt):
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

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
            "temperature": 0.7,
            "topP": 0.9,
            "maxOutputTokens": 30000,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": API_KEY,
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
                raise RuntimeError("Gemini output was truncated.")

            response_parts = candidate.get("content", {}).get("parts", [])
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

        if attempt < MAX_RETRIES:
            delay = min(8 * (2 ** (attempt - 1)), 32)
            print(f"Attempt {attempt} failed: {last_error}")
            print(f"Retrying in {delay} seconds.")
            time.sleep(delay)

    raise RuntimeError(
        f"Story generation failed after {MAX_RETRIES} attempts: {last_error}"
    )


def build_prompt(
    config, plan, title, topic, story_text,
    format_type, parts_count, scenes_per_part,
):
    plan_context = ""

    if plan:
        plan_context = json.dumps(
            {
                "generation_notes": plan.get("generation_notes", {}),
                "parts": plan.get("parts", []),
            },
            ensure_ascii=False,
            indent=2,
        )

    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    if format_type == "short":
        hook_minimum = 7
        format_rule = (
            "Create a concise short-form story with rapid escalation, "
            "little filler, a strong payoff and a memorable ending."
        )
        hook_rule = (
            "The opening hook must contain at least 7 words. "
            "Make it immediate, specific and curiosity-driven."
        )
    else:
        hook_minimum = 14
        format_rule = (
            "Create a developed long-form cinematic story with setup, "
            "character development, escalating conflict, turning points, "
            "a climax and a meaningful resolution."
        )
        hook_rule = (
            "The opening hook must contain at least 14 words. "
            "Develop a vivid, emotionally engaging mystery, danger or "
            "surprising contradiction rather than a short generic teaser."
        )

    if part_hook:
        part_hook_rule = (
            "The first scene of EVERY part must also open with a strong, "
            "part-specific hook. Do not reuse the same wording."
        )
    else:
        part_hook_rule = (
            "Only the opening of the whole story has a mandatory hook. "
            "Other part openings should still be engaging but need not "
            "meet the opening-hook word minimum."
        )

    if part_suspense:
        suspense_rule = (
            "The final scene of EVERY part must have a non-empty, "
            "story-specific suspense field containing at least 4 words. "
            "It must create a meaningful unanswered question, revelation "
            "or consequence. In the final part, resolve the central "
            "conflict before introducing any final clue."
        )
    else:
        suspense_rule = (
            "Use suspense when it serves the story. Do not force a "
            "cliffhanger at every part ending."
        )

    ending_rule = (
        "Resolve the main conflict clearly in the final part. Any final "
        "suspense must come after the main payoff and must not erase it."
        if final_resolution else
        "Give the ending a deliberate outcome consistent with the story."
    )

    return f"""
You are a professional Hindi cinematic storyteller.

INPUT TOPIC:
{topic or "(No topic supplied)"}

USER STORY TEXT:
{story_text or "(Develop an original story from the topic)"}

TITLE:
{title}

FORMAT:
{format_type}

AUDIENCE:
{text_value(config, "AUDIENCE", "adult")}

STORY LENGTH:
{text_value(config, "STORY_LENGTH", "auto")}

SCENE DURATION:
{text_value(config, "SCENE_DURATION", "auto")}

FORMAT RULE:
{format_rule}

MANDATORY OPENING HOOK:
{hook_rule}

HOOK INTEGRATION — CRITICAL:
1. Write the complete opening hook in the top-level "hook" field.
2. Copy that hook VERBATIM to the very beginning of Part 1, Scene 1 narration.
3. The narration may continue after the copied hook, but it must not put an
   introduction, greeting, character biography or scene-setting before it.
4. The hook must be in natural Hindi and must relate directly to the topic.
5. Never satisfy this requirement with an empty, generic or unrelated hook.

PART OPENINGS:
{part_hook_rule}

PART ENDINGS:
{suspense_rule}

FINAL ENDING:
{ending_rule}

STORY PLAN — FOLLOW THIS PLAN:
{plan_context or "(No saved plan; follow the required structure below.)"}

STRICT STRUCTURE:
- Exactly {parts_count} parts.
- Exactly {scenes_per_part} scenes in each part.
- Exactly {parts_count * scenes_per_part} scenes overall.
- Scene numbering restarts at 1 in each part.
- Follow the role, purpose and order of each planned scene.
- Do not omit, merge, duplicate or reorder planned scenes.
- Treat the plan as the story structure, not optional inspiration.

STORY QUALITY:
- Write natural, clear Hindi narration.
- Use believable, character-specific dialogue.
- Every scene must advance the plot or reveal character.
- Maintain consistent character names, ages, clothing, locations and timeline.
- Keep cause and effect logical.
- Use supplied story text as the primary source when provided.
- Never replace supplied story text with an unrelated plot.
- Use realistic live-action cinematic visual descriptions.
- Describe concrete actions, expressions, locations and atmosphere.
- Avoid cartoon, anime, comic and slideshow aesthetics.
- Do not add a generic channel introduction.
- Make the title and plot relevant to the input topic.
- Do not repeat the same suspense beat across parts.

Return ONLY valid JSON with this structure:
{{
  "status": "completed",
  "format": "{format_type}",
  "title": "Story title",
  "topic": "Input topic",
  "story_type": "original_story",
  "hook": "Opening hook in Hindi, at least {hook_minimum} words",
  "ending_type": "payoff_with_final_suspense",
  "parts": [
    {{
      "part": 1,
      "title": "Part title",
      "scenes": [
        {{
          "scene": 1,
          "purpose": "Purpose based on the story plan",
          "narration": "Start with the exact top-level hook, copied verbatim",
          "visual": "Realistic live-action visual description",
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
    data, config, format_type, title, topic,
    parts_count, scenes_per_part, model,
):
    if not isinstance(data, dict):
        raise RuntimeError("Generated story must be a JSON object.")

    if str(data.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Generated story format does not match input.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        actual = len(parts) if isinstance(parts, list) else "invalid"
        raise RuntimeError(
            f"Expected {parts_count} parts; received {actual}."
        )

    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    hook_minimum = 7 if format_type == "short" else 14
    hook = str(data.get("hook", "")).strip()

    if word_count(hook) < hook_minimum:
        raise RuntimeError(
            f"Opening hook is too short: {word_count(hook)} words; "
            f"{format_type} requires at least {hook_minimum}."
        )

    first_scene_narration = ""

    for part_number, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Part {part_number} is invalid.")

        try:
            actual_part_number = int(part.get("part", part_number))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Generated part numbering is invalid.") from exc

        if actual_part_number != part_number:
            raise RuntimeError("Generated part numbering is invalid.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must contain exactly "
                f"{scenes_per_part} scenes."
            )

        part["part"] = part_number
        part["title"] = str(
            part.get("title") or f"भाग {part_number}"
        ).strip()

        for scene_number, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number} is invalid."
                )

            try:
                actual_scene_number = int(scene.get("scene", scene_number))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Generated scene numbering is invalid.") from exc

            if actual_scene_number != scene_number:
                raise RuntimeError(
                    f"Part {part_number} scene numbering is invalid."
                )

            scene["scene"] = scene_number

            for field in (
                "purpose", "narration", "visual", "dialogue", "suspense"
            ):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(
                        f"Part {part_number}, scene {scene_number}: "
                        f"{field} must be a string."
                    )
                scene[field] = value.strip()

            if not scene["narration"]:
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number} "
                    "has no narration."
                )

            if not scene["visual"]:
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number} "
                    "has no visual description."
                )

            if part_number == 1 and scene_number == 1:
                first_scene_narration = scene["narration"]

            if scene_number == 1 and (part_number == 1 or part_hook):
                minimum = hook_minimum if part_number == 1 else 7
                if word_count(scene["narration"]) < minimum:
                    raise RuntimeError(
                        f"Part {part_number}, scene 1 does not contain "
                        f"a sufficiently developed hook."
                    )

            if scene_number == scenes_per_part and part_suspense:
                if word_count(scene["suspense"]) < 4:
                    raise RuntimeError(
                        f"Part {part_number} final scene is missing a "
                        "meaningful suspense beat of at least 4 words."
                    )

    normalized_hook = normalize_text(hook)
    normalized_opening = normalize_text(first_scene_narration)

    if not normalized_hook or not normalized_opening.startswith(normalized_hook):
        raise RuntimeError(
            "The first scene narration must begin with the exact opening "
            "hook from the top-level hook field. Regenerate the story."
        )

    data["status"] = "completed"
    data["format"] = format_type
    data["title"] = title
    data["topic"] = topic
    data["story_type"] = (
        "original_short_form" if format_type == "short" else "long_form"
    )
    data["hook"] = hook
    data["ending_type"] = str(
        data.get("ending_type") or "payoff_with_final_suspense"
    ).strip()

    data["_generation"] = {
        "model": model,
        "format": format_type,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": parts_count * scenes_per_part,
        "story_plan_used": PLAN_FILE.is_file(),
        "opening_hook_required": True,
        "opening_hook_minimum_words": hook_minimum,
        "opening_hook_in_scene_1": True,
        "part_hook_required": part_hook,
        "part_suspense_required": part_suspense,
        "final_resolution_required": final_resolution,
    }

    return data


def main():
    print("=" * 60)
    print("KATHA LOK AI - STORY GENERATION")
    print("=" * 60)

    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    config = load_and_validate()
    model = selected_model()

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
        raise RuntimeError("Could not determine a story title.")

    plan = load_plan(
        format_type, topic, parts_count, scenes_per_part
    )

    print(f"Format          : {format_type}")
    print(f"Title           : {title}")
    print(f"Parts           : {parts_count}")
    print(f"Scenes per part : {scenes_per_part}")
    print(f"Total scenes    : {parts_count * scenes_per_part}")
    print(f"Story plan      : {'used' if plan else 'not available'}")
    print(f"Opening hook    : required ({7 if format_type == 'short' else 14}+ words)")
    print(f"Part hooks      : {bool_value(config, 'PART_HOOK', True)}")
    print(f"Part suspense   : {bool_value(config, 'PART_SUSPENSE', True)}")

    prompt = build_prompt(
        config, plan, title, topic, story_text,
        format_type, parts_count, scenes_per_part,
    )

    data = call_gemini(model, prompt)
    data = validate_story(
        data, config, format_type, title, topic,
        parts_count, scenes_per_part, model,
    )

    STORY_DIR.mkdir(parents=True, exist_ok=True)

    temporary_file = OUTPUT_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_file.replace(OUTPUT_FILE)

    title_temp = TITLE_FILE.with_suffix(".tmp")
    title_temp.write_text(title + "\n", encoding="utf-8")
    title_temp.replace(TITLE_FILE)

    print("=" * 60)
    print("STORY GENERATION SUCCESS")
    print(f"Story file : {OUTPUT_FILE}")
    print(f"Title file : {TITLE_FILE}")
    print(f"Model      : {model}")
    print(f"Parts      : {parts_count}")
    print(f"Scenes     : {parts_count * scenes_per_part}")
    print("Opening hook validation: PASSED")
    print("Part suspense validation: PASSED")
    print("JSON validation: PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)