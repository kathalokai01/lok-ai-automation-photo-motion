
#!/usr/bin/env python3
"""Generate, validate, and checkpoint a Katha Lok AI story.

A valid JSON response is saved before story-schema validation so a later
workflow run can reuse it instead of automatically paying for another request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)
from gemini_api_guard import require_gemini_generation, GeminiBillingBlocked

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "output" / "story"
PLAN_FILE = STORY_DIR / "story_plan.json"
OUTPUT_FILE = STORY_DIR / "story.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
FINGERPRINT_FILE = STORY_DIR / "story.fingerprint"
RESPONSE_FILE = STORY_DIR / "story_response_checkpoint.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

API_KEY = (
    os.environ.get("GEMINI_API_KEY", "").strip()
    or os.environ.get("GOOGLE_API_KEY", "").strip()
)
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_RETRIES = 2
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
SCRIPT_VERSION = "story-checkpoint-5-cost-guard"


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, data: Any) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_text(value: Any) -> str:
    value = str(value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value: Any) -> int:
    return len(normalize_text(value).split())


def clean_title(value: Any) -> str:
    value = str(value or "").strip()
    value = re.sub(r"^['\"]+|['\"]+$", "", value)
    value = re.sub(
        r"^(title|शीर्षक)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip()


def bool_value(config: dict, key: str, default: bool = True) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()
    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0", ""}:
        return False
    return default


def selected_model() -> str:
    data = read_json(MODEL_FILE)

    if not isinstance(data, dict):
        raise RuntimeError(
            "Gemini model configuration is missing. Run "
            "scripts/select_gemini_model.py first."
        )

    status = str(data.get("status", "")).strip()
    if status not in {"selected", "configured_unverified"}:
        raise RuntimeError(
            f"Unsupported Gemini model status: {status!r}. "
            "Run scripts/select_gemini_model.py first."
        )

    model = str(data.get("model", "")).strip()

    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid selected Gemini model: {model!r}")

    if status == "configured_unverified":
        print(
            "WARNING: Gemini model is configured but not live-verified. "
            "The upcoming story-generation API request may incur usage "
            "or charges. Check account access and pricing first.",
            flush=True,
        )

    return model


def load_plan(
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    plan = read_json(PLAN_FILE)
    if not isinstance(plan, dict):
        raise RuntimeError(
            "Story plan missing or invalid. Run "
            "scripts/generate_story_plan.py first."
        )

    if str(plan.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story plan format does not match Input/topic.txt.")

    plan_topic = str(plan.get("topic", "")).strip()
    if topic and plan_topic and topic != plan_topic:
        raise RuntimeError("Story plan topic does not match current input.")

    try:
        plan_parts = int(plan.get("parts_count", -1))
        plan_scenes = int(plan.get("scenes_per_part", -1))
        plan_total = int(plan.get("total_scenes", -1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Story plan has invalid scene counts.") from exc

    expected_total = parts_count * scenes_per_part
    if (
        plan_parts != parts_count
        or plan_scenes != scenes_per_part
        or plan_total != expected_total
    ):
        raise RuntimeError(
            "Story plan counts do not match current input. "
            "Regenerate the story plan before continuing."
        )

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has an invalid parts list.")

    global_number = 0
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Invalid story plan part {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Plan part {part_number} must have exactly "
                f"{scenes_per_part} scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError("Story plan contains an invalid scene.")

            expected_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            try:
                actual_scene = int(scene.get("scene", -1))
                actual_global = int(scene.get("global_scene_number", -1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid plan scene numbering.") from exc

            if (
                actual_scene != scene_number
                or actual_global != global_number
                or scene.get("scene_id") != expected_id
            ):
                raise RuntimeError(
                    f"Scene mapping mismatch at {expected_id}. "
                    "Regenerate the story plan."
                )

    return plan


def make_fingerprint(
    config: dict,
    plan: dict,
    format_type: str,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
) -> str:
    relevant_keys = (
        "FORMAT",
        "AUDIENCE",
        "STORY_LENGTH",
        "SCENE_DURATION",
        "PART_HOOK",
        "PART_SUSPENSE",
        "FINAL_RESOLUTION",
    )
    source = {
        "version": SCRIPT_VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "config": {key: config.get(key) for key in relevant_keys},
        "plan": plan,
    }
    encoded = json.dumps(
        source, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_story(
    data: Any,
    config: dict,
    format_type: str,
    title: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    if not isinstance(data, dict):
        raise RuntimeError("Story response must be a JSON object.")

    if str(data.get("status", "")).lower() != "completed":
        raise RuntimeError("Story status must be 'completed'.")

    if str(data.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Generated story format mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} story parts.")

    hook = str(data.get("hook", "")).strip()
    hook_minimum = 7 if format_type == "short" else 14
    if word_count(hook) < hook_minimum:
        raise RuntimeError(
            f"Opening hook needs at least {hook_minimum} words."
        )

    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    all_ids = set()
    global_number = 0
    first_narration = ""

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {part_number}.")

        try:
            actual_part = int(part.get("part", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid part number {part_number}.") from exc

        if actual_part != part_number:
            raise RuntimeError(f"Expected story part {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must contain exactly "
                f"{scenes_per_part} scenes."
            )

        part["part_id"] = f"part_{part_number:02d}"
        part["title"] = str(part.get("title") or f"भाग {part_number}").strip()

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError("Story contains an invalid scene object.")

            try:
                actual_scene = int(scene.get("scene", -1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid story scene number.") from exc

            if actual_scene != scene_number:
                raise RuntimeError(
                    f"Part {part_number} scene numbering is incorrect."
                )

            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            scene["part"] = part_number
            scene["scene_id"] = scene_id
            scene["global_scene_number"] = global_number

            for field in ("purpose", "narration", "visual", "dialogue", "suspense"):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(
                        f"{scene_id}: field {field!r} must be text."
                    )
                scene[field] = value.strip()

            if not scene["narration"] or not scene["visual"]:
                raise RuntimeError(
                    f"{scene_id}: narration and visual description are required."
                )

            if scene_id in all_ids:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}")
            all_ids.add(scene_id)

            if part_number == 1 and scene_number == 1:
                first_narration = scene["narration"]

            if scene_number == 1 and (part_number == 1 or part_hook):
                minimum = hook_minimum if part_number == 1 else 7
                if word_count(scene["narration"]) < minimum:
                    raise RuntimeError(f"{scene_id}: opening hook is too short.")

            if scene_number == scenes_per_part and part_suspense:
                if word_count(scene["suspense"]) < 4:
                    raise RuntimeError(
                        f"{scene_id}: ending suspense is missing or too short."
                    )

    if len(all_ids) != parts_count * scenes_per_part:
        raise RuntimeError("Unique scene mapping validation failed.")

    if not normalize_text(first_narration).startswith(normalize_text(hook)):
        raise RuntimeError(
            "Part 1, Scene 1 narration must start with the exact story hook."
        )

    data["status"] = "completed"
    data["format"] = format_type
    data["title"] = title
    data["topic"] = topic
    data["mapping_version"] = 3
    data["_generation"] = {
        "script_version": SCRIPT_VERSION,
        "format": format_type,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": parts_count * scenes_per_part,
        "mapping_version": 3,
        "opening_hook_required": True,
        "opening_hook_minimum_words": hook_minimum,
        "part_hook_required": part_hook,
        "part_suspense_required": part_suspense,
        "scene_order": "part_then_scene",
    }
    return data


def extract_response_text(result: dict) -> str:
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        error_info = result.get("error", {})
        if isinstance(error_info, dict) and error_info.get("message"):
            raise RuntimeError(str(error_info["message"]))
        raise RuntimeError("Gemini returned no candidates.")

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise RuntimeError("Gemini returned an invalid candidate.")

    reason = str(candidate.get("finishReason", "")).upper()
    if reason in {"MAX_TOKENS", "LENGTH"}:
        raise RuntimeError(
            "Gemini output was truncated. No automatic second generation "
            "will be sent; this protects API usage."
        )
    if reason in {"SAFETY", "RECITATION", "BLOCKLIST"}:
        raise RuntimeError(f"Gemini blocked the response: {reason}.")

    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    text = "\n".join(
        item.get("text", "")
        for item in parts
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ).strip()

    if not text:
        raise RuntimeError("Gemini returned empty text.")
    return text


def build_prompt(
    config: dict,
    plan: dict,
    title: str,
    topic: str,
    story_text: str,
    format_type: str,
    parts_count: int,
    scenes_per_part: int,
) -> str:
    hook_minimum = 7 if format_type == "short" else 14
    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    format_rule = (
        "Short video: fast escalation, minimal filler, strong payoff."
        if format_type == "short"
        else (
            "Full video: developed cinematic story, character development, "
            "escalating conflict, turning points, climax and resolution."
        )
    )

    plan_text = json.dumps(plan, ensure_ascii=False, indent=2)
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

Opening hook must contain at least {hook_minimum} Hindi words.
Copy the complete top-level hook verbatim at the start of Part 1 Scene 1
narration. Do not add an introduction before it.

PART_HOOK={part_hook}: if true, start every part with a story-specific hook.
PART_SUSPENSE={part_suspense}: if true, give every part's final scene a
meaningful suspense field containing at least 4 words.
FINAL_RESOLUTION={final_resolution}: resolve the central conflict in the
final part when enabled.

STORY PLAN — preserve exact part/scene structure and IDs:
{plan_text}

Requirements:
- Exactly {parts_count} parts and {scenes_per_part} scenes per part.
- Scene numbering restarts at 1 in every part.
- Keep every scene in source order; never merge, omit or add scenes.
- Natural Hindi narration and believable character-specific dialogue.
- Maintain consistent character identity, clothing, location and timeline.
- Use supplied story text as the primary source when provided.
- Use realistic live-action visual descriptions, not cartoons or slideshows.
- Every scene advances the plot or reveals character.
- Return JSON only, without Markdown fences or commentary.

Return JSON with this structure:
{{
  "status": "completed",
  "format": "{format_type}",
  "title": {json.dumps(title, ensure_ascii=False)},
  "topic": {json.dumps(topic, ensure_ascii=False)},
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


def call_gemini(model: str, prompt: str, fingerprint: str) -> dict:
    # Guard is called immediately before a real API request, not when a
    # completed story or matching response checkpoint can be reused.
    try:
        guarded_api_key = require_gemini_generation(
            "generate_story",
            api_key=API_KEY,
        )
    except GeminiBillingBlocked as exc:
        raise RuntimeError(
            f"{exc}\n"
            "Story generation was not sent to Gemini. "
            "To explicitly authorize potentially billable generation, set "
            "ALLOW_GEMINI_GENERATION to "
            "I_ACCEPT_POTENTIAL_GEMINI_CHARGES in the workflow environment. "
            "Do not set this if you require strict no-billing operation."
        ) from exc

    url_model = urllib.parse.quote(model, safe="-._")
    url = f"{BASE_URL}/models/{url_model}:generateContent"
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
                "x-goog-api-key": guarded_api_key,
                "User-Agent": "Katha-Lok-AI-Story/5.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {details[:400]}"
            )
            if exc.code not in RETRYABLE_HTTP_CODES or attempt >= MAX_RETRIES:
                raise last_error from exc
            delay = min(5 * (2 ** (attempt - 1)), 15)
            print(
                f"Temporary Gemini HTTP {exc.code}; retrying once after {delay}s."
            )
            time.sleep(delay)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = RuntimeError(
                f"Gemini network error: {type(exc).__name__}"
            )
            if attempt >= MAX_RETRIES:
                raise last_error from exc
            delay = min(5 * (2 ** (attempt - 1)), 15)
            print(f"Temporary network error; retrying once after {delay}s.")
            time.sleep(delay)
            continue

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Gemini returned invalid API JSON. Stopping to avoid another "
                "generation request."
            ) from exc

        if not isinstance(result, dict):
            raise RuntimeError("Gemini API response was not an object.")

        response_text = extract_response_text(result)
        try:
            story_data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Gemini response was not valid story JSON. No automatic "
                "second generation request was sent."
            ) from exc

        if not isinstance(story_data, dict):
            raise RuntimeError("Gemini story response must be a JSON object.")

        # Save the response before schema validation. A matching checkpoint
        # can be retried through validation without generating another story.
        write_json(RESPONSE_FILE, {
            "status": "response_saved",
            "fingerprint": fingerprint,
            "model": model,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "story": story_data,
        })
        return story_data

    raise RuntimeError(f"Story generation failed: {last_error}")


def save_completed_story(
    data: dict,
    fingerprint: str,
    title: str,
) -> None:
    write_json(OUTPUT_FILE, data)
    atomic_write(TITLE_FILE, title + "\n")
    atomic_write(FINGERPRINT_FILE, fingerprint + "\n")
    print("Validated story saved:", OUTPUT_FILE)
    print("Title saved:", TITLE_FILE)
    print("Story fingerprint saved.")


def main() -> None:
    print("=" * 60)
    print("KATHA LOK AI - STORY GENERATION / RESUME")
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
        title = clean_title(story_text.splitlines()[0])[:100]
    else:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    if not title:
        raise RuntimeError("Could not determine a story title.")

    plan = load_plan(format_type, topic, parts_count, scenes_per_part)
    fingerprint = make_fingerprint(
        config,
        plan,
        format_type,
        topic,
        story_text,
        parts_count,
        scenes_per_part,
    )

    print("Format:", format_type)
    print("Topic/title:", title)
    print("Parts:", parts_count)
    print("Scenes per part:", scenes_per_part)
    print("Expected scenes:", parts_count * scenes_per_part)
    print("Input fingerprint:", fingerprint[:16] + "...")

    saved_fingerprint = ""
    if FINGERPRINT_FILE.is_file():
        try:
            saved_fingerprint = FINGERPRINT_FILE.read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            pass

    if saved_fingerprint == fingerprint:
        saved_story = read_json(OUTPUT_FILE)
        try:
            valid = validate_story(
                saved_story, config, format_type, title, topic,
                parts_count, scenes_per_part,
            )
            save_completed_story(valid, fingerprint, title)
            print("RESUME: Valid matching story reused; no story API call.")
            return
        except Exception as exc:
            print("Saved story is not reusable:", exc)

    # Reuse a matching API response before considering any new request.
    response_checkpoint = read_json(RESPONSE_FILE)
    if (
        isinstance(response_checkpoint, dict)
        and response_checkpoint.get("fingerprint") == fingerprint
        and isinstance(response_checkpoint.get("story"), dict)
    ):
        print("RESUME: Found matching saved Gemini response.")
        data = response_checkpoint["story"]
    else:
        model = selected_model()
        prompt = build_prompt(
            config, plan, title, topic, story_text, format_type,
            parts_count, scenes_per_part,
        )
        print("Selected model:", model)
        print("No matching saved response; checking generation authorization.")
        data = call_gemini(model, prompt, fingerprint)

    # Validation failures stop here. A matching response checkpoint remains
    # on disk, so fixing code/schema issues does not automatically call Gemini.
    data = validate_story(
        data, config, format_type, title, topic,
        parts_count, scenes_per_part,
    )
    save_completed_story(data, fingerprint, title)
    print("STORY GENERATION SUCCESS")
    print("Scene mapping validation: PASSED")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Story generation cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
