#!/usr/bin/env python3
"""Generate story-aware YouTube metadata and all pipeline-required outputs."""

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


ROOT = Path(__file__).resolve().parents[1]

INPUT_TOPIC = ROOT / "Input" / "topic.txt"
INPUT_STORY = ROOT / "output" / "story" / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUTPUT_DIR = ROOT / "output" / "publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"

# Required by workflow and final-output validator.
OUTPUT_TEXT = ROOT / "output" / "youtube_metadata.txt"

MAX_RETRIES = 3
REQUEST_TIMEOUT = 120
MAX_STORY_CHARS = 60000
VERSION = "youtube-metadata-v3-validator-output-compatible"


def log(*values):
    print(*values, flush=True)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")

    return data


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: dict[str, Any]):
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def load_input_config() -> dict[str, Any]:
    if not INPUT_TOPIC.is_file():
        raise RuntimeError(f"Input file missing: {INPUT_TOPIC}")

    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        from input_config import load_input_config as parse_config
        return parse_config(str(INPUT_TOPIC))
    except Exception as exc:
        raise RuntimeError(
            f"Could not parse Input/topic.txt: {exc}"
        ) from exc


def load_selected_model() -> str:
    data = read_json(MODEL_FILE)

    if data.get("status") != "selected":
        raise RuntimeError(
            "selected_model.json does not contain status='selected'."
        )

    model = clean_text(data.get("model"))

    if not model:
        raise RuntimeError("Selected Gemini model is empty.")

    return model


def collect_story_text(value: Any, depth: int = 0) -> list[str]:
    if depth > 12:
        return []

    ignored = {
        "image", "image_path", "image_url", "visual_path",
        "audio", "audio_path", "audio_url", "video", "video_path",
        "prompt", "negative_prompt", "metadata", "tags",
        "status", "id", "scene_id", "part_id", "duration",
        "fps", "width", "height", "seed", "model", "provider",
        "created_at",
    }

    text_keys = {
        "story", "story_text", "full_story", "content",
        "narration", "narration_text", "script", "text",
        "paragraph", "description", "summary", "ending",
        "hook", "title", "dialogue", "voiceover",
    }

    result = []

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()

            if normalized in ignored:
                continue

            if isinstance(item, str):
                if normalized in text_keys and item.strip():
                    result.append(item.strip())
            elif isinstance(item, (dict, list)):
                result.extend(collect_story_text(item, depth + 1))

    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                result.extend(collect_story_text(item, depth + 1))
            elif isinstance(item, str) and item.strip():
                result.append(item.strip())

    return result


def prepare_story(story: dict[str, Any]) -> str:
    parts = []

    existing_title = clean_text(
        story.get("title") or story.get("final_title")
    )

    if existing_title:
        parts.append(
            "STORY'S EXISTING TITLE:\n" + existing_title
        )

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
        log(
            "WARNING: Story exceeds metadata input limit; "
            "using first", MAX_STORY_CHARS, "characters."
        )
        text = text[:MAX_STORY_CHARS]

    if len(text) < 30:
        raise RuntimeError(
            "Story content is too short for reliable metadata."
        )

    return text


def normalize_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)

    if not isinstance(raw, list):
        raise RuntimeError("Generated tags must be a list.")

    result = []
    seen = set()

    for item in raw:
        tag = clean_text(item).strip(" ,#")
        tag = re.sub(r"\s+", " ", tag)

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
        raise RuntimeError(
            "At least five relevant YouTube tags are required."
        )

    return result


def fingerprint(topic, story, model, format_name):
    source = {
        "version": VERSION,
        "topic": topic,
        "story": story,
        "model": model,
        "format": format_name,
    }

    encoded = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def validate_metadata(data, topic, model, digest, format_name):
    title = clean_text(data.get("title"))
    description = clean_text(data.get("description"))
    tags = normalize_tags(data.get("tags"))

    if not title:
        raise RuntimeError("Generated title is empty.")

    if not description:
        raise RuntimeError("Generated description is empty.")

    if len(title) > 100:
        title = title[:97].rstrip() + "..."

    if len(description) > 5000:
        description = description[:4997].rstrip() + "..."

    return {
        "status": "completed",
        "version": VERSION,
        "format": format_name,
        "topic": topic,
        "model": model,
        "title": title,
        "description": description,
        "tags": tags,
        "input_fingerprint": digest,
    }


def load_cached_metadata(digest, topic, model, format_name):
    if not OUTPUT_JSON.is_file():
        return None

    try:
        cached = read_json(OUTPUT_JSON)

        if cached.get("status") != "completed":
            return None

        if cached.get("version") != VERSION:
            return None

        if cached.get("input_fingerprint") != digest:
            return None

        if cached.get("model") != model:
            return None

        if cached.get("format") != format_name:
            return None

        return validate_metadata(
            cached, topic, model, digest, format_name
        )

    except Exception as exc:
        log("Metadata cache not reusable:", exc)
        return None


def extract_response_text(result):
    candidates = result.get("candidates") or []

    if not candidates:
        raise RuntimeError(
            "Gemini returned no candidates: "
            + json.dumps(result, ensure_ascii=False)[:1200]
        )

    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []

    response = "\n".join(
        item.get("text", "")
        for item in parts
        if isinstance(item, dict) and item.get("text")
    ).strip()

    if not response:
        raise RuntimeError("Gemini returned empty metadata.")

    return response


def generate_metadata(
    api_key,
    model,
    topic,
    story_text,
    format_name,
    digest,
):
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
        "This is a YouTube Short. Use a concise title and "
        "description appropriate for a vertical short video."
        if format_name == "short"
        else
        "This is a full-length YouTube story video. "
        "Write metadata suitable for a complete story."
    )

    prompt = f"""
You are a professional Hindi YouTube metadata editor.

Read the COMPLETE story before creating the title.
Do not create the title from the first sentence or TOPIC alone.

TOPIC:
{topic or "Not supplied"}

FORMAT:
{format_name}

FORMAT INSTRUCTION:
{format_instruction}

COMPLETE STORY:
{story_text}

TASK:
1. Create a compelling natural Hindi title based on the complete
   plot, central character, conflict and actual ending.
2. Do not merely copy the topic or existing title.
3. Do not invent characters, events, twists or claims.
4. Preserve mystery without revealing the complete twist.
5. Write a useful Hindi description explaining the actual story.
6. Generate 10 to 20 specific story-related tags.
7. Avoid unrelated trending tags and keyword stuffing.
8. Return only JSON matching the requested schema.
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.65,
            },
        }

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            log(
                f"Generating metadata: attempt "
                f"{attempt}/{MAX_RETRIES}; model={model}"
            )

            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

            parsed = json.loads(extract_response_text(result))

            if not isinstance(parsed, dict):
                raise RuntimeError("Gemini response is not a JSON object.")

            return validate_metadata(
                parsed, topic, model, digest, format_name
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8", errors="replace"
            )[:2000]

            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body}"
            )

            if exc.code in (400, 401, 403, 404):
                break

        except Exception as exc:
            last_error = exc

        log("Metadata attempt failed:", last_error)

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Metadata generation failed after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def save_outputs(metadata):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Structured outputs used by downstream tools.
    atomic_write_json(OUTPUT_JSON, metadata)

    atomic_write(
        OUTPUT_TITLE,
        metadata["title"] + "\n",
    )

    atomic_write(
        OUTPUT_DESCRIPTION,
        metadata["description"] + "\n",
    )

    atomic_write(
        OUTPUT_TAGS,
        ", ".join(metadata["tags"]) + "\n",
    )

    # Required combined text artifact:
    # validate_final_output.py reads TITLE, DESCRIPTION and TAGS sections.
    combined = (
        "TITLE:\n"
        + metadata["title"]
        + "\n\nDESCRIPTION:\n"
        + metadata["description"]
        + "\n\nTAGS:\n"
        + ", ".join(metadata["tags"])
        + "\n\nLANGUAGE:\nHindi\n"
    )

    atomic_write(OUTPUT_TEXT, combined)

    # Verify every required output immediately.
    required = (
        OUTPUT_JSON,
        OUTPUT_TITLE,
        OUTPUT_DESCRIPTION,
        OUTPUT_TAGS,
        OUTPUT_TEXT,
    )

    for path in required:
        if not path.is_file() or path.stat().st_size < 5:
            raise RuntimeError(
                f"Metadata output missing or empty: {path}"
            )

    saved = read_json(OUTPUT_JSON)

    if (
        saved.get("status") != "completed"
        or saved.get("format") != metadata["format"]
        or saved.get("title") != metadata["title"]
    ):
        raise RuntimeError("Saved metadata JSON verification failed.")

    text_content = OUTPUT_TEXT.read_text(
        encoding="utf-8-sig"
    )

    for section in ("TITLE:", "DESCRIPTION:", "TAGS:"):
        if section not in text_content:
            raise RuntimeError(
                f"Combined metadata section missing: {section}"
            )


def main():
    config = load_input_config()

    topic = clean_text(config.get("TOPIC"))
    format_name = clean_text(
        config.get("FORMAT", "full")
    ).lower()

    if format_name not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {format_name}"
        )

    story = read_json(INPUT_STORY)
    story_text = prepare_story(story)
    model = load_selected_model()

    digest = fingerprint(
        topic,
        story_text,
        model,
        format_name,
    )

    metadata = load_cached_metadata(
        digest,
        topic,
        model,
        format_name,
    )

    if metadata:
        log("Matching metadata cache found; Gemini API call skipped.")
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()

        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is missing and no valid metadata cache exists."
            )

        metadata = generate_metadata(
            api_key,
            model,
            topic,
            story_text,
            format_name,
            digest,
        )

    save_outputs(metadata)

    log("=" * 65)
    log("YOUTUBE METADATA COMPLETED")
    log("FORMAT:", format_name)
    log("MODEL:", model)
    log("TITLE:", metadata["title"])
    log("TAGS:", len(metadata["tags"]))
    log("JSON:", OUTPUT_JSON.relative_to(ROOT))
    log("TEXT:", OUTPUT_TEXT.relative_to(ROOT))
    log("TITLE FILE:", OUTPUT_TITLE.relative_to(ROOT))
    log("DESCRIPTION FILE:", OUTPUT_DESCRIPTION.relative_to(ROOT))
    log("TAGS FILE:", OUTPUT_TAGS.relative_to(ROOT))
    log("GEMINI API:", "USED" if not metadata.get("cache_reused") else "SKIPPED")
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("YOUTUBE METADATA FAILED:", exc)
        sys.exit(1)