
#!/usr/bin/env python3
"""Generate a Hindi cinematic story and reuse valid matching story checkpoints."""

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
FINGERPRINT_FILE = STORY_DIR / "story.fingerprint"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MAX_RETRIES = 3


def read_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def selected_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "Selected Gemini model missing. Run select_gemini_model.py first."
        )
    model = str(data.get("model", "")).strip()
    if not model or model.startswith("models/"):
        raise RuntimeError(f"Invalid Gemini model: {model!r}")
    return model


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
        r"^(title|शीर्षक)\s*:\s*", "", value, flags=re.IGNORECASE
    )
    return re.sub(r"\s+", " ", value).strip()


def load_plan(format_type, topic, parts_count, scenes_per_part):
    plan = read_json(PLAN_FILE)
    if plan is None:
        print("WARNING: Story plan not found.")
        return None

    if not isinstance(plan, dict):
        raise RuntimeError("Story plan must be a JSON object.")

    if str(plan.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story plan format does not match current input.")

    plan_topic = str(plan.get("topic", "")).strip()
    if topic and plan_topic and plan_topic != topic:
        raise RuntimeError("Story plan topic does not match current input.")

    if int(plan.get("parts_count", -1)) != parts_count:
        raise RuntimeError("Story plan part count mismatch.")

    if int(plan.get("scenes_per_part", -1)) != scenes_per_part:
        raise RuntimeError("Story plan scene count mismatch.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has an invalid parts list.")

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Invalid story plan part {part_number}.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(f"Invalid scene count in plan part {part_number}.")
        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict) or int(scene.get("scene", -1)) != scene_number:
                raise RuntimeError(
                    f"Invalid scene numbering in plan part {part_number}."
                )

    return plan


def make_fingerprint(config, plan, format_type, topic, story_text,
                     parts_count, scenes_per_part):
    """Hash every input that materially affects the generated story."""
    relevant_config = {
        key: config.get(key)
        for key in (
            "FORMAT", "AUDIENCE", "STORY_LENGTH", "SCENE_DURATION",
            "PART_HOOK", "PART_SUSPENSE", "FINAL_RESOLUTION",
        )
    }
    source = {
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "config": relevant_config,
        "plan": plan,
    }
    encoded = json.dumps(
        source, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_story(data, config, format_type, title, topic,
                   parts_count, scenes_per_part):
    if not isinstance(data, dict):
        raise RuntimeError("Story must be a JSON object.")

    if str(data.get("status", "")).lower() != "completed":
        raise RuntimeError("Story status is not completed.")

    if str(data.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story format mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} parts.")

    hook = str(data.get("hook", "")).strip()
    hook_minimum = 7 if format_type == "short" else 14
    if word_count(hook) < hook_minimum:
        raise RuntimeError("Opening hook is too short.")

    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    first_narration = ""

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Invalid story part {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must have {scenes_per_part} scenes."
            )

        part["title"] = str(part.get("title") or f"भाग {part_number}").strip()

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError("Invalid scene object.")

            if int(scene.get("scene", -1)) != scene_number:
                raise RuntimeError(
                    f"Invalid scene numbering in part {part_number}."
                )

            for field in ("purpose", "narration", "visual", "dialogue", "suspense"):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(f"Scene field {field} must be text.")
                scene[field] = value.strip()

            if not scene["narration"] or not scene["visual"]:
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number} is incomplete."
                )

            if part_number == 1 and scene_number == 1:
                first_narration = scene["narration"]

            if scene_number == 1 and (part_number == 1 or part_hook):
                minimum = hook_minimum if part_number == 1 else 7
                if word_count(scene["narration"]) < minimum:
                    raise RuntimeError(f"Part {part_number} opening hook is too short.")

            if scene_number == scenes_per_part and part_suspense:
                if word_count(scene["suspense"]) < 4:
                    raise RuntimeError(
                        f"Part {part_number} ending suspense is missing."
                    )

    if not normalize_text(first_narration).startswith(normalize_text(hook)):
        raise RuntimeError("Scene 1 narration must begin with the story hook.")

    data["status"] = "completed"
    data["format"] = format_type
    data["title"] = title
    data["topic"] = topic
    data["_generation"] = {
        "format": format_type,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": parts_count * scenes_per_part,
        "opening_hook_required": True,
        "opening_hook_minimum_words": hook_minimum,
        "part_hook_required": part_hook,
        "part_suspense_required": part_suspense,
    }
    return data


def call_gemini(model, prompt):
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    encoded_model = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{encoded_model}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "topP": 0.9,
            "maxOutputTokens": 30000,
            "responseMimeType": "application/json",
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

            chunks = candidate.get("content", {}).get("parts", [])
            response_text = "\n".join(
                item.get("text", "") for item in chunks
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ).strip()
            if not response_text:
                raise RuntimeError("Gemini returned empty text.")

            return json.loads(response_text)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Gemini HTTP {exc.code}: {body[:800]}")
            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error from exc
        except Exception as exc:
            last_error = exc

        if attempt < MAX_RETRIES:
            delay = min(8 * (2 ** (attempt - 1)), 32)
            print(f"Attempt {attempt} failed: {last_error}")
            time.sleep(delay)

    raise RuntimeError(f"Story generation failed: {last_error}")


def build_prompt(config, plan, title, topic, story_text,
                 format_type, parts_count, scenes_per_part):
    hook_minimum = 7 if format_type == "short" else 14
    plan_text = json.dumps(plan, ensure_ascii=False, indent=2) if plan else "No plan"

    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    format_rule = (
        "Short, rapid escalation, little filler, strong payoff."
        if format_type == "short"
        else "Developed cinematic story with setup, character development, "
             "escalating conflict, turning points, climax and resolution."
    )

    return f"""
You are a professional Hindi cinematic storyteller.

TOPIC: {topic or "(not supplied)"}
USER STORY TEXT: {story_text or "(create an original story from the topic)"}
TITLE: {title}
FORMAT: {format_type}
AUDIENCE: {config.get("AUDIENCE", "adult")}
STORY LENGTH: {config.get("STORY_LENGTH", "auto")}
SCENE DURATION: {config.get("SCENE_DURATION", "auto")}

FORMAT RULE:
{format_rule}

Opening hook: at least {hook_minimum} Hindi words, immediate, specific,
curiosity-driven and directly relevant to the topic.
Copy the complete top-level hook verbatim to the start of Part 1 Scene 1 narration.
Do not place an introduction before the hook.

PART_HOOK={part_hook}: when true, every part must start with its own strong hook.
PART_SUSPENSE={part_suspense}: when true, the last scene of every part needs
a meaningful suspense field of at least 4 words.
FINAL_RESOLUTION={final_resolution}: resolve the central conflict in the final part.

STORY PLAN — follow its order, purpose and scene structure:
{plan_text}

Requirements:
- Exactly {parts_count} parts.
- Exactly {scenes_per_part} scenes per part.
- Scene numbering restarts at 1 in every part.
- Natural Hindi narration and believable character-specific dialogue.
- Every scene advances the plot or reveals character.
- Maintain consistent character identity, clothing, location and timeline.
- Use supplied story text as the primary source when provided.
- Realistic live-action visual descriptions, not cartoon, anime, comic or slideshow.
- Strong hook and suspense; no generic channel introduction.
- Final part must resolve the central conflict.

Return ONLY valid JSON in this structure:
{{
  "status": "completed",
  "format": "{format_type}",
  "title": "{title}",
  "topic": "{topic}",
  "story_type": "original_story",
  "hook": "Hindi opening hook",
  "ending_type": "payoff_with_final_suspense",
  "parts": [
    {{
      "part": 1,
      "title": "Part title",
      "scenes": [
        {{
          "scene": 1,
          "purpose": "Scene purpose",
          "narration": "Must begin with the exact opening hook",
          "visual": "Realistic live-action visual description",
          "dialogue": "Hindi dialogue or empty string",
          "suspense": "Suspense beat or empty string"
        }}
      ]
    }}
  ]
}}
"""


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def main():
    print("=" * 60)
    print("KATHA LOK AI - STORY GENERATION / RESUME")
    print("=" * 60)

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

    plan = load_plan(format_type, topic, parts_count, scenes_per_part)
    fingerprint = make_fingerprint(
        config, plan, format_type, topic, story_text,
        parts_count, scenes_per_part,
    )

    print(f"Format: {format_type}")
    print(f"Topic/title: {title}")
    print(f"Total scenes: {parts_count * scenes_per_part}")
    print(f"Input fingerprint: {fingerprint[:16]}...")

    # Reuse only when both the saved fingerprint and story validation match.
    saved_fingerprint = ""
    if FINGERPRINT_FILE.is_file():
        saved_fingerprint = FINGERPRINT_FILE.read_text(
            encoding="utf-8"
        ).strip()

    if saved_fingerprint == fingerprint and OUTPUT_FILE.is_file():
        try:
            saved_story = read_json(OUTPUT_FILE)
            saved_story = validate_story(
                saved_story, config, format_type, title, topic,
                parts_count, scenes_per_part,
            )
            atomic_write(
                OUTPUT_FILE,
                json.dumps(saved_story, ensure_ascii=False, indent=2) + "\n",
            )
            atomic_write(TITLE_FILE, title + "\n")
            print("RESUME: Valid matching story reused.")
            print("Gemini story-generation request skipped.")
            return
        except Exception as exc:
            print(f"Saved story failed validation; regenerating: {exc}")
    else:
        print("No matching fingerprint; story must be generated.")

    prompt = build_prompt(
        config, plan, title, topic, story_text,
        format_type, parts_count, scenes_per_part,
    )

    data = call_gemini(model, prompt)
    data = validate_story(
        data, config, format_type, title, topic,
        parts_count, scenes_per_part,
    )

    # Write the validated story first. Save its fingerprint only after success.
    atomic_write(
        OUTPUT_FILE,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write(TITLE_FILE, title + "\n")
    atomic_write(FINGERPRINT_FILE, fingerprint + "\n")

    print("STORY GENERATION SUCCESS")
    print("Story:", OUTPUT_FILE)
    print("Title:", TITLE_FILE)
    print("Fingerprint:", FINGERPRINT_FILE)
    print("Opening hook and scene validation: PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
