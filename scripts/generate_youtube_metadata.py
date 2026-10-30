#!/usr/bin/env python3
"""Generate complete-story YouTube metadata with validated config and cache."""

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
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
)

INPUT_TOPIC = ROOT / "Input" / "topic.txt"
INPUT_STORY = ROOT / "output" / "story" / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUTPUT_DIR = ROOT / "output" / "publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"
OUTPUT_TEXT = ROOT / "output" / "youtube_metadata.txt"

VERSION = "youtube-metadata-v4-complete-story-cache-safe"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 180
MAX_TAGS = 20
MIN_TAGS = 5


def log(*values):
    print(*values, flush=True)


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return ""


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON: {path}: {exc}") from exc

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


def atomic_write_json(path: Path, data: dict):
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def load_config():
    if not INPUT_TOPIC.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_TOPIC)

    topic = get_topic(config)
    format_name = get_format(config)

    if format_name not in {"short", "full"}:
        raise RuntimeError(f"Unsupported normalized FORMAT: {format_name}")

    return config, clean(topic), format_name


def load_selected_model():
    data = read_json(MODEL_FILE)

    if data.get("status") != "selected":
        raise RuntimeError(
            "selected_model.json does not contain a selected model."
        )

    model = clean(data.get("model"))

    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(
            f"Invalid selected Gemini model identifier: {model!r}"
        )

    return model


def ordered_parts(story):
    parts = story.get("parts")

    if not isinstance(parts, list):
        return []

    indexed = []

    for index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            continue

        try:
            number = int(part.get("part", index))
        except (TypeError, ValueError):
            number = index

        indexed.append((number, part))

    indexed.sort(key=lambda item: item[0])
    return [part for _, part in indexed]


def extract_canonical_story(story):
    """
    Prefer the original complete story_text.
    Do not append scene narration to it a second time.
    """

    for key in ("story_text", "full_story", "original_story"):
        value = clean(story.get(key))
        if len(value) >= 30:
            return value, key

    # Fallback: reconstruct story from ordered part/scene narration.
    sections = []

    for part_number, part in enumerate(ordered_parts(story), 1):
        scenes = part.get("scenes")

        if not isinstance(scenes, list):
            continue

        ordered_scenes = []

        for index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                continue

            try:
                scene_number = int(
                    scene.get("scene", scene.get("local_scene", index))
                )
            except (TypeError, ValueError):
                scene_number = index

            ordered_scenes.append((scene_number, scene))

        ordered_scenes.sort(key=lambda item: item[0])

        part_text = []

        for scene_number, scene in ordered_scenes:
            narration = clean(
                scene.get("narration")
                or scene.get("narration_text")
                or scene.get("text")
            )

            if narration:
                part_text.append(
                    f"[Part {part_number}, Scene {scene_number}]\n"
                    + narration
                )

        if part_text:
            sections.extend(part_text)

    reconstructed = "\n\n".join(sections).strip()

    if len(reconstructed) >= 30:
        return reconstructed, "ordered_parts_and_scenes"

    # Last fallback: known story-bearing top-level fields.
    for key in (
        "story",
        "content",
        "narration",
        "script",
        "description",
        "summary",
    ):
        value = clean(story.get(key))
        if len(value) >= 30:
            return value, key

    raise RuntimeError(
        "Complete story text was not found in ai_story.json. "
        "No metadata will be generated from TOPIC alone."
    )


def prepare_story(story):
    story_text, source = extract_canonical_story(story)

    title = clean(
        story.get("title")
        or story.get("final_title")
        or story.get("story_title")
    )

    return {
        "text": story_text,
        "source": source,
        "existing_title": title,
        "character_count": len(story_text),
    }


def normalize_tags(raw):
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)

    if not isinstance(raw, list):
        raise RuntimeError("Generated tags must be a list.")

    tags = []
    seen = set()

    for item in raw:
        tag = clean(item).strip(" ,#")
        tag = re.sub(r"\s+", " ", tag)

        if not tag:
            continue

        if len(tag) > 100:
            tag = tag[:100].rstrip()

        key = tag.casefold()

        if key in seen:
            continue

        seen.add(key)
        tags.append(tag)

        if len(tags) >= MAX_TAGS:
            break

    if len(tags) < MIN_TAGS:
        raise RuntimeError(
            f"Only {len(tags)} valid tags generated; "
            f"minimum required is {MIN_TAGS}."
        )

    return tags


def validate_metadata(data, topic, model, digest, format_name):
    title = clean(data.get("title"))
    description = clean(data.get("description"))
    tags = normalize_tags(data.get("tags"))

    if not title:
        raise RuntimeError("Generated YouTube title is empty.")

    if not description:
        raise RuntimeError("Generated YouTube description is empty.")

    if len(title) > 100:
        raise RuntimeError(
            f"Generated title exceeds 100 characters: {len(title)}"
        )

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


def make_fingerprint(topic, story_text, existing_title, model, mode):
    payload = {
        "version": VERSION,
        "topic": topic,
        "story_text": story_text,
        "existing_title": existing_title,
        "model": model,
        "format": mode,
    }

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def load_cache(digest, topic, model, mode):
    if not OUTPUT_JSON.is_file():
        return None

    try:
        data = read_json(OUTPUT_JSON)

        if data.get("status") != "completed":
            return None

        if data.get("version") != VERSION:
            return None

        if data.get("input_fingerprint") != digest:
            return None

        if data.get("model") != model:
            return None

        if data.get("format") != mode:
            return None

        return validate_metadata(
            data, topic, model, digest, mode
        )

    except Exception as exc:
        log("Metadata cache rejected:", exc)
        return None


def extract_response_text(result):
    candidates = result.get("candidates")

    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError(
            "Gemini returned no candidates: "
            + json.dumps(result, ensure_ascii=False)[:1500]
        )

    candidate = candidates[0]

    finish_reason = clean(candidate.get("finishReason"))

    if finish_reason in {"SAFETY", "RECITATION", "BLOCKLIST"}:
        raise RuntimeError(
            f"Gemini blocked metadata generation: {finish_reason}"
        )

    content = candidate.get("content") or {}
    parts = content.get("parts") or []

    response = "\n".join(
        item.get("text", "")
        for item in parts
        if isinstance(item, dict) and item.get("text")
    ).strip()

    if not response:
        raise RuntimeError(
            f"Gemini returned empty metadata. finishReason={finish_reason}"
        )

    return response


def generate_metadata(
    api_key,
    model,
    topic,
    story,
    mode,
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

    if mode == "short":
        format_instruction = (
            "The video is a YouTube Short. Create a concise, "
            "high-impact title and a short-video description."
        )
    else:
        format_instruction = (
            "The video is a full-length story. Create a complete-story "
            "title and a detailed description."
        )

    prompt = f"""
You are a professional Hindi YouTube metadata editor.

You must read the complete story below before deciding the title.
The story text is the primary source. TOPIC and existing title are
only supporting context.

TOPIC:
{topic or "Not supplied"}

EXISTING STORY TITLE:
{story["existing_title"] or "Not supplied"}

VIDEO FORMAT:
{mode}

FORMAT REQUIREMENT:
{format_instruction}

COMPLETE STORY:
{story["text"]}

TASK:
1. Understand the full plot, main character, conflict, turning point,
   important events and actual ending.
2. Create a natural, compelling Hindi YouTube title based on the
   complete story, not merely its opening sentences.
3. Do not copy TOPIC as the title unless it genuinely describes
   the complete story.
4. Do not invent characters, events, locations or twists.
5. If the story contains mystery or suspense, create curiosity
   without revealing the entire ending.
6. Write a useful Hindi description that accurately explains
   the story and its central conflict.
7. Generate 10 to 20 relevant Hindi/English search tags.
8. Avoid unrelated trending tags and keyword stuffing.
9. Do not add fabricated claims, links or hashtags unrelated
   to the actual story.
10. Return only the requested JSON object.
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    last_error = None

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
                "temperature": 0.55,
            },
        }

        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            log(
                f"Gemini metadata request {attempt}/{MAX_RETRIES}; "
                f"model={model}"
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
                raise RuntimeError(
                    "Gemini metadata response is not a JSON object."
                )

            return validate_metadata(
                parsed, topic, model, digest, mode
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )[:2000]

            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body}"
            )

            # Permanent errors should not consume repeated requests.
            if exc.code in (400, 401, 403, 404):
                break

            # Retry temporary quota, server and gateway errors.
            if exc.code not in (408, 409, 429, 500, 502, 503, 504):
                break

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            last_error = exc

        except Exception as exc:
            last_error = exc

        log("Metadata attempt failed:", last_error)

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Metadata generation failed after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def build_combined_text(metadata):
    return (
        "TITLE:\n"
        + metadata["title"]
        + "\n\nDESCRIPTION:\n"
        + metadata["description"]
        + "\n\nTAGS:\n"
        + ", ".join(metadata["tags"])
        + "\n\nLANGUAGE:\nHindi\n"
    )


def save_outputs(metadata):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    combined = build_combined_text(metadata)

    # Keep the JSON and all text files consistent with one result.
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

    atomic_write(OUTPUT_TEXT, combined)

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

    if saved.get("title") != metadata["title"]:
        raise RuntimeError("Saved metadata title verification failed.")

    if saved.get("format") != metadata["format"]:
        raise RuntimeError("Saved metadata FORMAT verification failed.")

    if saved.get("input_fingerprint") != metadata["input_fingerprint"]:
        raise RuntimeError("Saved metadata fingerprint verification failed.")

    text = OUTPUT_TEXT.read_text(encoding="utf-8-sig")

    for section in ("TITLE:", "DESCRIPTION:", "TAGS:", "LANGUAGE:"):
        if section not in text:
            raise RuntimeError(
                f"Combined metadata section missing: {section}"
            )

    if metadata["title"] not in text:
        raise RuntimeError("Combined metadata title does not match JSON.")

    if metadata["description"] not in text:
        raise RuntimeError(
            "Combined metadata description does not match JSON."
        )


def main():
    config, topic, mode = load_config()

    story = read_json(INPUT_STORY)
    prepared = prepare_story(story)
    model = load_selected_model()

    story_text = prepared["text"]
    existing_title = prepared["existing_title"]

    digest = make_fingerprint(
        topic,
        story_text,
        existing_title,
        model,
        mode,
    )

    log("=" * 65)
    log("KATHA LOK AI — YOUTUBE METADATA")
    log("=" * 65)
    log("FORMAT:", mode)
    log("MODEL:", model)
    log("STORY SOURCE:", prepared["source"])
    log("STORY CHARACTERS:", prepared["character_count"])

    metadata = load_cache(
        digest,
        topic,
        model,
        mode,
    )

    cache_reused = metadata is not None

    if cache_reused:
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
            prepared,
            mode,
            digest,
        )

    save_outputs(metadata)

    log("")
    log("YOUTUBE METADATA COMPLETED")
    log("FORMAT:", mode)
    log("TITLE:", metadata["title"])
    log("TAGS:", len(metadata["tags"]))
    log("STORY SOURCE:", prepared["source"])
    log("CACHE REUSED:", cache_reused)
    log("GEMINI API CALLED:", not cache_reused)
    log("JSON:", OUTPUT_JSON.relative_to(ROOT))
    log("TEXT:", OUTPUT_TEXT.relative_to(ROOT))
    log("TITLE:", OUTPUT_TITLE.relative_to(ROOT))
    log("DESCRIPTION:", OUTPUT_DESCRIPTION.relative_to(ROOT))
    log("TAGS:", OUTPUT_TAGS.relative_to(ROOT))
    log("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("YOUTUBE METADATA FAILED:", exc)
        sys.exit(1)