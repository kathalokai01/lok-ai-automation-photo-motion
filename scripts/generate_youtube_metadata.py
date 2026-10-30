#!/usr/bin/env python3
"""
Story-aware YouTube metadata generator.

- Uses the complete generated story, not just TOPIC.
- Uses the already selected Gemini model.
- Reuses metadata when the story, topic and model have not changed.
- Produces JSON, title, description and tags files.
- Does not change provider/model fallback configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(".")
INPUT_TOPIC = ROOT / "Input/topic.txt"
INPUT_STORY = ROOT / "output/story/ai_story.json"
MODEL_FILE = ROOT / "output/config/selected_model.json"

OUTPUT_DIR = ROOT / "output/publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"

MAX_RETRIES = 3
REQUEST_TIMEOUT = 120
MAX_STORY_CHARS = 60000


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"ERROR: Required file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"ERROR: Invalid JSON in {path}: {exc}")

    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: Expected a JSON object in {path}")

    return data


def load_input_config() -> dict[str, Any]:
    if not INPUT_TOPIC.is_file():
        raise SystemExit(f"ERROR: Input file not found: {INPUT_TOPIC}")

    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        from input_config import load_input_config as parse_config
        return parse_config(str(INPUT_TOPIC))
    except Exception as exc:
        raise SystemExit(f"ERROR: Could not parse Input/topic.txt: {exc}")


def load_selected_model() -> str:
    config = load_json(MODEL_FILE)

    if config.get("status") != "selected":
        raise SystemExit(
            "ERROR: output/config/selected_model.json "
            "does not contain status='selected'."
        )

    model = str(config.get("model", "")).strip()

    if not model:
        raise SystemExit("ERROR: Selected Gemini model is empty.")

    return model


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, (int, float, bool)):
        return str(value)

    return ""


def collect_story_text(value: Any, depth: int = 0) -> list[str]:
    """
    Extract story text from nested JSON while ignoring image,
    audio, prompt and technical metadata fields.
    """

    if depth > 12:
        return []

    ignored_keys = {
        "image", "image_path", "image_url", "visual_path",
        "audio", "audio_path", "audio_url", "video",
        "video_path", "prompt", "negative_prompt",
        "metadata", "tags", "status", "id", "scene_id",
        "part_id", "duration", "fps", "width", "height",
        "seed", "model", "provider", "created_at",
    }

    text_keys = {
        "story", "story_text", "full_story", "content",
        "narration", "narration_text", "script", "text",
        "paragraph", "description", "summary", "ending",
        "hook", "title", "dialogue", "voiceover",
    }

    collected: list[str] = []

    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).strip().lower()

            if normalized_key in ignored_keys:
                continue

            if isinstance(item, str):
                if normalized_key in text_keys and item.strip():
                    collected.append(item.strip())
            elif isinstance(item, (dict, list)):
                collected.extend(
                    collect_story_text(item, depth + 1)
                )

    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                collected.extend(
                    collect_story_text(item, depth + 1)
                )
            elif isinstance(item, str) and item.strip():
                collected.append(item.strip())

    return collected


def prepare_story(story: dict[str, Any]) -> str:
    parts: list[str] = []

    existing_title = clean_text(
        story.get("title") or story.get("final_title")
    )

    if existing_title:
        parts.append(f"STORY'S EXISTING TITLE:\n{existing_title}")

    extracted = collect_story_text(story)

    if extracted:
        parts.append("COMPLETE STORY CONTENT:")
        parts.extend(extracted)

    if not parts:
        parts.append(
            json.dumps(story, ensure_ascii=False, indent=2)
        )

    text = "\n\n".join(parts).strip()

    if len(text) > MAX_STORY_CHARS:
        print(
            f"WARNING: Story text exceeds {MAX_STORY_CHARS} characters; "
            "metadata input will be limited."
        )
        text = text[:MAX_STORY_CHARS]

    if len(text.strip()) < 30:
        raise SystemExit(
            "ERROR: Story content is too short for reliable metadata."
        )

    return text


def normalize_tags(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,;\n]", raw_tags)

    if not isinstance(raw_tags, list):
        raise ValueError("Tags must be a JSON array.")

    result: list[str] = []
    seen: set[str] = set()

    for item in raw_tags:
        tag = clean_text(item)
        tag = re.sub(r"\s+", " ", tag)
        tag = tag.strip(" ,#")

        if not tag:
            continue

        key = tag.casefold()

        if key in seen:
            continue

        seen.add(key)
        result.append(tag[:100])

        if len(result) >= 20:
            break

    if len(result) < 5:
        raise ValueError(
            "At least 5 relevant, non-empty tags are required."
        )

    return result


def validate_metadata(
    metadata: dict[str, Any],
    topic: str,
    model: str,
    fingerprint: str,
) -> dict[str, Any]:

    title = clean_text(metadata.get("title"))
    description = clean_text(metadata.get("description"))
    tags = normalize_tags(metadata.get("tags"))

    if not title:
        raise ValueError("Generated title is empty.")

    if len(title) > 100:
        title = title[:97].rstrip() + "..."

    if not description:
        raise ValueError("Generated description is empty.")

    if len(description) > 5000:
        description = description[:4997].rstrip() + "..."

    return {
        "status": "completed",
        "topic": topic,
        "model": model,
        "title": title,
        "description": description,
        "tags": tags,
        "input_fingerprint": fingerprint,
    }


def make_fingerprint(
    topic: str,
    story_text: str,
    model: str,
    format_name: str,
) -> str:
    source = {
        "topic": topic,
        "story": story_text,
        "model": model,
        "format": format_name,
        "version": 2,
    }

    encoded = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def load_cached_metadata(
    fingerprint: str,
    topic: str,
    model: str,
) -> dict[str, Any] | None:

    if not OUTPUT_JSON.is_file():
        return None

    try:
        cached = json.loads(
            OUTPUT_JSON.read_text(encoding="utf-8")
        )

        if not isinstance(cached, dict):
            return None

        if cached.get("status") != "completed":
            return None

        if cached.get("input_fingerprint") != fingerprint:
            return None

        if cached.get("model") != model:
            return None

        return validate_metadata(
            cached,
            topic,
            model,
            fingerprint,
        )

    except Exception as exc:
        print(f"Existing metadata cache is unusable: {exc}")
        return None


def save_outputs(metadata: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    temporary_json = OUTPUT_JSON.with_suffix(".json.tmp")

    temporary_json.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    temporary_json.replace(OUTPUT_JSON)

    OUTPUT_TITLE.write_text(
        metadata["title"] + "\n",
        encoding="utf-8",
    )

    OUTPUT_DESCRIPTION.write_text(
        metadata["description"] + "\n",
        encoding="utf-8",
    )

    OUTPUT_TAGS.write_text(
        ", ".join(metadata["tags"]) + "\n",
        encoding="utf-8",
    )


def extract_response_text(result: dict[str, Any]) -> str:
    candidates = result.get("candidates") or []

    if not candidates:
        raise ValueError(
            f"Gemini returned no candidates: "
            f"{json.dumps(result, ensure_ascii=False)[:1500]}"
        )

    candidate = candidates[0]
    content = candidate.get("content") or {}
    response_parts = content.get("parts") or []

    chunks = [
        part.get("text", "")
        for part in response_parts
        if isinstance(part, dict) and part.get("text")
    ]

    response_text = "\n".join(chunks).strip()

    if not response_text:
        raise ValueError(
            "Gemini returned an empty metadata response."
        )

    return response_text


def generate_metadata(
    api_key: str,
    model: str,
    topic: str,
    story_text: str,
    format_name: str,
    fingerprint: str,
) -> dict[str, Any]:

    schema = {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "description": {"type": "STRING"},
            "tags": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
        },
        "required": ["title", "description", "tags"],
    }

    format_instruction = (
        "This is a YouTube Short. Make the title concise and "
        "the description suitable for a short vertical video."
        if format_name == "short"
        else
        "This is a full-length YouTube story video. Make the "
        "title and description suitable for a complete story."
    )

    prompt = f"""
You are a professional Hindi YouTube metadata editor.

Read the COMPLETE story below before deciding its title.
Do not create the title from the first sentence or from TOPIC alone.

TOPIC / USER INPUT:
{topic or "Not supplied"}

VIDEO FORMAT:
{format_name}

FORMAT REQUIREMENT:
{format_instruction}

COMPLETE STORY:
{story_text}

TASK:
1. Create one compelling, natural Hindi title based on the central
   plot, main character, conflict and actual ending of the story.
2. Do not merely copy the topic or existing title.
3. Do not invent characters, events, twists or claims.
4. If the story is a mystery, preserve curiosity without revealing
   the entire twist in the title.
5. Write a useful Hindi description explaining what the story is
   about. Include relevant search phrases naturally.
6. Generate 10 to 20 specific tags based on this story.
7. Include Hindi storytelling/cinematic terms only when relevant.
8. Avoid unrelated trending tags, keyword stuffing and fake claims.
9. Do not add URLs or fabricated facts.
10. Return only the JSON object matching the schema.
"""

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.65,
            },
        }

        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            print(
                f"Generating story-based metadata "
                f"(attempt {attempt}/{MAX_RETRIES})..."
            )

            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                raw = response.read().decode("utf-8")

            result = json.loads(raw)
            response_text = extract_response_text(result)
            parsed = json.loads(response_text)

            if not isinstance(parsed, dict):
                raise ValueError(
                    "Gemini response is not a JSON object."
                )

            return validate_metadata(
                parsed,
                topic,
                model,
                fingerprint,
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )[:2000]

            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body}"
            )

            # Do not retry permanent authentication/model errors.
            if exc.code in (400, 401, 403, 404):
                break

        except Exception as exc:
            last_error = exc

        print(f"Metadata attempt failed: {last_error}")

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    raise SystemExit(
        f"ERROR: Metadata generation failed after "
        f"{MAX_RETRIES} attempts: {last_error}"
    )


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise SystemExit("ERROR: GEMINI_API_KEY is not set.")

    config = load_input_config()

    topic = str(config.get("TOPIC", "")).strip()
    format_name = str(
        config.get("FORMAT", "full")
    ).strip().lower()

    if format_name not in {"short", "full"}:
        raise SystemExit(
            f"ERROR: Unsupported FORMAT: {format_name}"
        )

    story = load_json(INPUT_STORY)
    story_text = prepare_story(story)
    model = load_selected_model()

    fingerprint = make_fingerprint(
        topic,
        story_text,
        model,
        format_name,
    )

    cached = load_cached_metadata(
        fingerprint,
        topic,
        model,
    )

    if cached:
        print("Matching metadata cache found; API call skipped.")
        metadata = cached
    else:
        metadata = generate_metadata(
            api_key,
            model,
            topic,
            story_text,
            format_name,
            fingerprint,
        )

    save_outputs(metadata)

    print("======================================")
    print("YOUTUBE METADATA READY")
    print("======================================")
    print(f"Format : {format_name}")
    print(f"Model  : {model}")
    print(f"Title  : {metadata['title']}")
    print(f"Tags   : {len(metadata['tags'])}")
    print(f"JSON   : {OUTPUT_JSON}")
    print(f"Title  : {OUTPUT_TITLE}")
    print(f"Desc   : {OUTPUT_DESCRIPTION}")
    print(f"Tags   : {OUTPUT_TAGS}")
    print("======================================")


if __name__ == "__main__":
    main()